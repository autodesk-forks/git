#!/usr/bin/env python
#
# git-p4.py -- A tool for bidirectional operation between a Perforce depot and git.
#
# Author: Simon Hausmann <simon@lst.de>
# Copyright: 2007 Simon Hausmann <simon@lst.de>
#            2007 Trolltech ASA
# License: MIT <http://www.opensource.org/licenses/mit-license.php>
#
import sys
if sys.hexversion < 0x02040000:
    # The limiter is the subprocess module
    sys.stderr.write("git-p4: requires Python 2.4 or later.\n")
    sys.exit(1)
import os
import optparse
import marshal
import subprocess
import tempfile
import time
import platform
import re
import shutil
import stat
import zipfile
import zlib
import ctypes

try:
    from subprocess import CalledProcessError
except ImportError:
    # from python2.7:subprocess.py
    # Exception classes used by this module.
    class CalledProcessError(Exception):
        """This exception is raised when a process run by check_call() returns
        a non-zero exit status.  The exit status will be stored in the
        returncode attribute."""
        def __init__(self, returncode, cmd):
            self.returncode = returncode
            self.cmd = cmd
        def __str__(self):
            return "Command '%s' returned non-zero exit status %d" % (self.cmd, self.returncode)

verbose = False

# Only labels/tags matching this will be imported/exported
defaultLabelRegexp = r'[a-zA-Z0-9_\-.]+$'

# Grab changes in blocks of this many revisions, unless otherwise requested
defaultBlockSize = 512

def p4_build_cmd(cmd):
    """Build a suitable p4 command line.

    This consolidates building and returning a p4 command line into one
    location. It means that hooking into the environment, or other configuration
    can be done more easily.
    """
    real_cmd = ["p4"]

    user = gitConfig("git-p4.user")
    if len(user) > 0:
        real_cmd += ["-u",user]

    password = gitConfig("git-p4.password")
    if len(password) > 0:
        real_cmd += ["-P", password]

    port = gitConfig("git-p4.port")
    if len(port) > 0:
        real_cmd += ["-p", port]

    host = gitConfig("git-p4.host")
    if len(host) > 0:
        real_cmd += ["-H", host]

    client = gitConfig("git-p4.client")
    if len(client) > 0:
        real_cmd += ["-c", client]


    if isinstance(cmd,basestring):
        real_cmd = ' '.join(real_cmd) + ' ' + cmd
    else:
        real_cmd += cmd
    return real_cmd

def chdir(path, is_client_path=False):
    """Do chdir to the given path, and set the PWD environment
       variable for use by P4.  It does not look at getcwd() output.
       Since we're not using the shell, it is necessary to set the
       PWD environment variable explicitly.

       Normally, expand the path to force it to be absolute.  This
       addresses the use of relative path names inside P4 settings,
       e.g. P4CONFIG=.p4config.  P4 does not simply open the filename
       as given; it looks for .p4config using PWD.

       If is_client_path, the path was handed to us directly by p4,
       and may be a symbolic link.  Do not call os.getcwd() in this
       case, because it will cause p4 to think that PWD is not inside
       the client path.
       """

    os.chdir(path)
    if not is_client_path:
        path = os.getcwd()
    os.environ['PWD'] = path

def calcDiskFree():
    """Return free space in bytes on the disk of the given dirname."""
    if platform.system() == 'Windows':
        free_bytes = ctypes.c_ulonglong(0)
        ctypes.windll.kernel32.GetDiskFreeSpaceExW(ctypes.c_wchar_p(os.getcwd()), None, None, ctypes.pointer(free_bytes))
        return free_bytes.value
    else:
        st = os.statvfs(os.getcwd())
        return st.f_bavail * st.f_frsize

def die(msg):
    if verbose:
        raise Exception(msg)
    else:
        sys.stderr.write(msg + "\n")
        sys.exit(1)

def write_pipe(c, stdin):
    if verbose:
        sys.stderr.write('Writing pipe: %s\n' % str(c))

    expand = isinstance(c,basestring)
    p = subprocess.Popen(c, stdin=subprocess.PIPE, shell=expand)
    pipe = p.stdin
    val = pipe.write(stdin)
    pipe.close()
    if p.wait():
        die('Command failed: %s' % str(c))

    return val

def p4_write_pipe(c, stdin):
    real_cmd = p4_build_cmd(c)
    return write_pipe(real_cmd, stdin)

def read_pipe(c, ignore_error=False):
    if verbose:
        sys.stderr.write('Reading pipe: %s\n' % str(c))

    expand = isinstance(c,basestring)
    p = subprocess.Popen(c, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=expand)
    (out, err) = p.communicate()
    if p.returncode != 0 and not ignore_error:
        die('Command failed: %s\nError: %s' % (str(c), err))
    return out

def p4_read_pipe(c, ignore_error=False):
    real_cmd = p4_build_cmd(c)
    return read_pipe(real_cmd, ignore_error)

def read_pipe_lines(c):
    if verbose:
        sys.stderr.write('Reading pipe: %s\n' % str(c))

    expand = isinstance(c, basestring)
    p = subprocess.Popen(c, stdout=subprocess.PIPE, shell=expand)
    pipe = p.stdout
    val = pipe.readlines()
    if pipe.close() or p.wait():
        die('Command failed: %s' % str(c))

    return val

def p4_read_pipe_lines(c):
    """Specifically invoke p4 on the command supplied. """
    real_cmd = p4_build_cmd(c)
    return read_pipe_lines(real_cmd)

def p4_has_command(cmd):
    """Ask p4 for help on this command.  If it returns an error, the
       command does not exist in this version of p4."""
    real_cmd = p4_build_cmd(["help", cmd])
    p = subprocess.Popen(real_cmd, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
    p.communicate()
    return p.returncode == 0

def p4_has_move_command():
    """See if the move command exists, that it supports -k, and that
       it has not been administratively disabled.  The arguments
       must be correct, but the filenames do not have to exist.  Use
       ones with wildcards so even if they exist, it will fail."""

    if not p4_has_command("move"):
        return False
    cmd = p4_build_cmd(["move", "-k", "@from", "@to"])
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    (out, err) = p.communicate()
    # return code will be 1 in either case
    if err.find("Invalid option") >= 0:
        return False
    if err.find("disabled") >= 0:
        return False
    # assume it failed because @... was invalid changelist
    return True

def system(cmd, ignore_error=False):
    expand = isinstance(cmd,basestring)
    if verbose:
        sys.stderr.write("executing %s\n" % str(cmd))
    retcode = subprocess.call(cmd, shell=expand)
    if retcode and not ignore_error:
        raise CalledProcessError(retcode, cmd)

    return retcode

def p4_system(cmd):
    """Specifically invoke p4 as the system command. """
    real_cmd = p4_build_cmd(cmd)
    expand = isinstance(real_cmd, basestring)
    retcode = subprocess.call(real_cmd, shell=expand)
    if retcode:
        raise CalledProcessError(retcode, real_cmd)

_p4_version_string = None
def p4_version_string():
    """Read the version string, showing just the last line, which
       hopefully is the interesting version bit.

       $ p4 -V
       Perforce - The Fast Software Configuration Management System.
       Copyright 1995-2011 Perforce Software.  All rights reserved.
       Rev. P4/NTX86/2011.1/393975 (2011/12/16).
    """
    global _p4_version_string
    if not _p4_version_string:
        a = p4_read_pipe_lines(["-V"])
        _p4_version_string = a[-1].rstrip()
    return _p4_version_string

def p4_integrate(src, dest):
    p4_system(["integrate", "-Dt", wildcard_encode(src), wildcard_encode(dest)])

def p4_sync(f, *options):
    p4_system(["sync"] + list(options) + [wildcard_encode(f)])

def p4_add(f):
    # forcibly add file names with wildcards
    if wildcard_present(f):
        p4_system(["add", "-f", f])
    else:
        p4_system(["add", f])

def p4_delete(f):
    p4_system(["delete", wildcard_encode(f)])

def p4_edit(f, *options):
    p4_system(["edit"] + list(options) + [wildcard_encode(f)])

def p4_revert(f):
    p4_system(["revert", wildcard_encode(f)])

def p4_reopen(type, f):
    p4_system(["reopen", "-t", type, wildcard_encode(f)])

def p4_move(src, dest):
    p4_system(["move", "-k", wildcard_encode(src), wildcard_encode(dest)])

def p4_last_change():
    results = p4CmdList(["changes", "-m", "1"])
    return int(results[0]['change'])

def p4_describe(change):
    """Make sure it returns a valid result by checking for
       the presence of field "time".  Return a dict of the
       results."""

    ds = p4CmdList(["-r", "10", "describe", "-s", str(change)])
    if len(ds) != 1:
        die("p4 describe -s %d did not return 1 result: %s" % (change, str(ds)))

    d = ds[0]

    if "p4ExitCode" in d:
        die("p4 describe -s %d exited with %d: %s" % (change, d["p4ExitCode"],
                                                      str(d)))
    if "code" in d:
        if d["code"] == "error":
            die("p4 describe -s %d returned error code: %s" % (change, str(d)))

    if "time" not in d:
        die("p4 describe -s %d returned no \"time\": %s" % (change, str(d)))

    return d

#
# Canonicalize the p4 type and return a tuple of the
# base type, plus any modifiers.  See "p4 help filetypes"
# for a list and explanation.
#
def split_p4_type(p4type):

    p4_filetypes_historical = {
        "ctempobj": "binary+Sw",
        "ctext": "text+C",
        "cxtext": "text+Cx",
        "ktext": "text+k",
        "kxtext": "text+kx",
        "ltext": "text+F",
        "tempobj": "binary+FSw",
        "ubinary": "binary+F",
        "uresource": "resource+F",
        "uxbinary": "binary+Fx",
        "xbinary": "binary+x",
        "xltext": "text+Fx",
        "xtempobj": "binary+Swx",
        "xtext": "text+x",
        "xunicode": "unicode+x",
        "xutf16": "utf16+x",
    }
    if p4type in p4_filetypes_historical:
        p4type = p4_filetypes_historical[p4type]
    mods = ""
    s = p4type.split("+")
    base = s[0]
    mods = ""
    if len(s) > 1:
        mods = s[1]
    return (base, mods)

#
# return the raw p4 type of a file (text, text+ko, etc)
#
def p4_type(f):
    results = p4CmdList(["fstat", "-T", "headType", wildcard_encode(f)])
    return results[0]['headType']

#
# Given a type base and modifier, return a regexp matching
# the keywords that can be expanded in the file
#
def p4_keywords_regexp_for_type(base, type_mods):
    if base in ("text", "unicode", "binary"):
        kwords = None
        if "ko" in type_mods:
            kwords = 'Id|Header'
        elif "k" in type_mods:
            kwords = 'Id|Header|Author|Date|DateTime|Change|File|Revision'
        else:
            return None
        pattern = r"""
            \$              # Starts with a dollar, followed by...
            (%s)            # one of the keywords, followed by...
            (:[^$\n]+)?     # possibly an old expansion, followed by...
            \$              # another dollar
            """ % kwords
        return pattern
    else:
        return None

#
# Given a file, return a regexp matching the possible
# RCS keywords that will be expanded, or None for files
# with kw expansion turned off.
#
def p4_keywords_regexp_for_file(file):
    if not os.path.exists(file):
        return None
    else:
        (type_base, type_mods) = split_p4_type(p4_type(file))
        return p4_keywords_regexp_for_type(type_base, type_mods)

def setP4ExecBit(file, mode):
    # Reopens an already open file and changes the execute bit to match
    # the execute bit setting in the passed in mode.

    p4Type = "+x"

    if not isModeExec(mode):
        p4Type = getP4OpenedType(file)
        p4Type = re.sub('^([cku]?)x(.*)', '\\1\\2', p4Type)
        p4Type = re.sub('(.*?\+.*?)x(.*?)', '\\1\\2', p4Type)
        if p4Type[-1] == "+":
            p4Type = p4Type[0:-1]

    p4_reopen(p4Type, file)

def getP4OpenedType(file):
    # Returns the perforce file type for the given file.

    result = p4_read_pipe(["opened", wildcard_encode(file)])
    match = re.match(".*\((.+)\)( \*exclusive\*)?\r?$", result)
    if match:
        return match.group(1)
    else:
        die("Could not determine file type for %s (result: '%s')" % (file, result))

# Return the set of all p4 labels
def getP4Labels(depotPaths):
    labels = set()
    if isinstance(depotPaths,basestring):
        depotPaths = [depotPaths]

    for l in p4CmdList(["labels"] + ["%s..." % p for p in depotPaths]):
        label = l['label']
        labels.add(label)

    return labels

# Return the set of all git tags
def getGitTags():
    gitTags = set()
    for line in read_pipe_lines(["git", "tag"]):
        tag = line.strip()
        gitTags.add(tag)
    return gitTags

def diffTreePattern():
    # This is a simple generator for the diff tree regex pattern. This could be
    # a class variable if this and parseDiffTreeEntry were a part of a class.
    pattern = re.compile(':(\d+) (\d+) (\w+) (\w+) ([A-Z])(\d+)?\t(.*?)((\t(.*))|$)')
    while True:
        yield pattern

def parseDiffTreeEntry(entry):
    """Parses a single diff tree entry into its component elements.

    See git-diff-tree(1) manpage for details about the format of the diff
    output. This method returns a dictionary with the following elements:

    src_mode - The mode of the source file
    dst_mode - The mode of the destination file
    src_sha1 - The sha1 for the source file
    dst_sha1 - The sha1 fr the destination file
    status - The one letter status of the diff (i.e. 'A', 'M', 'D', etc)
    status_score - The score for the status (applicable for 'C' and 'R'
                   statuses). This is None if there is no score.
    src - The path for the source file.
    dst - The path for the destination file. This is only present for
          copy or renames. If it is not present, this is None.

    If the pattern is not matched, None is returned."""

    match = diffTreePattern().next().match(entry)
    if match:
        return {
            'src_mode': match.group(1),
            'dst_mode': match.group(2),
            'src_sha1': match.group(3),
            'dst_sha1': match.group(4),
            'status': match.group(5),
            'status_score': match.group(6),
            'src': match.group(7),
            'dst': match.group(10)
        }
    return None

def isModeExec(mode):
    # Returns True if the given git mode represents an executable file,
    # otherwise False.
    return mode[-3:] == "755"

def isModeExecChanged(src_mode, dst_mode):
    return isModeExec(src_mode) != isModeExec(dst_mode)

def p4CmdList(cmd, stdin=None, stdin_mode='w+b', cb=None):

    if isinstance(cmd,basestring):
        cmd = "-G " + cmd
        expand = True
    else:
        cmd = ["-G"] + cmd
        expand = False

    cmd = p4_build_cmd(cmd)
    if verbose:
        sys.stderr.write("Opening pipe: %s\n" % str(cmd))

    # Use a temporary file to avoid deadlocks without
    # subprocess.communicate(), which would put another copy
    # of stdout into memory.
    stdin_file = None
    if stdin is not None:
        stdin_file = tempfile.TemporaryFile(prefix='p4-stdin', mode=stdin_mode)
        if isinstance(stdin,basestring):
            stdin_file.write(stdin)
        else:
            for i in stdin:
                stdin_file.write(i + '\n')
        stdin_file.flush()
        stdin_file.seek(0)

    p4 = subprocess.Popen(cmd,
                          shell=expand,
                          stdin=stdin_file,
                          stdout=subprocess.PIPE)

    result = []
    try:
        while True:
            entry = marshal.load(p4.stdout)
            if cb is not None:
                cb(entry)
            else:
                result.append(entry)
    except EOFError:
        pass
    exitCode = p4.wait()
    if exitCode != 0:
        entry = {}
        entry["p4ExitCode"] = exitCode
        result.append(entry)

    return result

def p4Cmd(cmd):
    list = p4CmdList(cmd)
    result = {}
    for entry in list:
        result.update(entry)
    return result;

def p4Where(depotPath):
    if not depotPath.endswith("/"):
        depotPath += "/"
    depotPathLong = depotPath + "..."
    outputList = p4CmdList(["where", depotPathLong])
    output = None
    for entry in outputList:
        if "depotFile" in entry:
            # Search for the base client side depot path, as long as it starts with the branch's P4 path.
            # The base path always ends with "/...".
            if entry["depotFile"].find(depotPath) == 0 and entry["depotFile"][-4:] == "/...":
                output = entry
                break
        elif "data" in entry:
            data = entry.get("data")
            space = data.find(" ")
            if data[:space] == depotPath:
                output = entry
                break
    if output == None:
        return ""
    if output["code"] == "error":
        return ""
    clientPath = ""
    if "path" in output:
        clientPath = output.get("path")
    elif "data" in output:
        data = output.get("data")
        lastSpace = data.rfind(" ")
        clientPath = data[lastSpace + 1:]

    if clientPath.endswith("..."):
        clientPath = clientPath[:-3]
    return clientPath

def currentGitBranch():
    retcode = system(["git", "symbolic-ref", "-q", "HEAD"], ignore_error=True)
    if retcode != 0:
        # on a detached head
        return None
    else:
        return read_pipe(["git", "name-rev", "HEAD"]).split(" ")[1].strip()

def isValidGitDir(path):
    if (os.path.exists(path + "/HEAD")
        and os.path.exists(path + "/refs") and os.path.exists(path + "/objects")):
        return True;
    return False

def parseRevision(ref):
    return read_pipe("git rev-parse %s" % ref).strip()

def branchExists(ref):
    rev = read_pipe(["git", "rev-parse", "-q", "--verify", ref],
                     ignore_error=True)
    return len(rev) > 0

def extractLogMessageFromGitCommit(commit):
    logMessage = ""

    ## fixme: title is first line of commit, not 1st paragraph.
    foundTitle = False
    for log in read_pipe_lines("git cat-file commit %s" % commit):
       if not foundTitle:
           if len(log) == 1:
               foundTitle = True
           continue

       logMessage += log
    return logMessage

def extractSettingsGitLog(log):
    values = {}
    for line in log.split("\n"):
        line = line.strip()
        m = re.search (r"^ *\[git-p4: (.*)\]$", line)
        if not m:
            continue

        assignments = m.group(1).split (':')
        for a in assignments:
            vals = a.split ('=')
            key = vals[0].strip()
            val = ('='.join (vals[1:])).strip()
            if val.endswith ('\"') and val.startswith('"'):
                val = val[1:-1]

            values[key] = val

    paths = values.get("depot-paths")
    if not paths:
        paths = values.get("depot-path")
    if paths:
        values['depot-paths'] = paths.split(',')
    return values

def gitBranchExists(branch):
    proc = subprocess.Popen(["git", "rev-parse", branch],
                            stderr=subprocess.PIPE, stdout=subprocess.PIPE);
    return proc.wait() == 0;

_gitConfig = {}

def gitConfig(key, typeSpecifier=None):
    if not _gitConfig.has_key(key):
        cmd = [ "git", "config" ]
        if typeSpecifier:
            cmd += [ typeSpecifier ]
        cmd += [ key ]
        s = read_pipe(cmd, ignore_error=True)
        _gitConfig[key] = s.strip()
    return _gitConfig[key]

def gitConfigBool(key):
    """Return a bool, using git config --bool.  It is True only if the
       variable is set to true, and False if set to false or not present
       in the config."""

    if not _gitConfig.has_key(key):
        _gitConfig[key] = gitConfig(key, '--bool') == "true"
    return _gitConfig[key]

def gitConfigInt(key):
    if not _gitConfig.has_key(key):
        cmd = [ "git", "config", "--int", key ]
        s = read_pipe(cmd, ignore_error=True)
        v = s.strip()
        try:
            _gitConfig[key] = int(gitConfig(key, '--int'))
        except ValueError:
            _gitConfig[key] = None
    return _gitConfig[key]

def gitConfigList(key):
    if not _gitConfig.has_key(key):
        s = read_pipe(["git", "config", "--get-all", key], ignore_error=True)
        _gitConfig[key] = s.strip().split(os.linesep)
        if _gitConfig[key] == ['']:
            _gitConfig[key] = []
    return _gitConfig[key]

def p4BranchesInGit(branchesAreInRemotes=True):
    """Find all the branches whose names start with "p4/", looking
       in remotes or heads as specified by the argument.  Return
       a dictionary of { branch: revision } for each one found.
       The branch names are the short names, without any
       "p4/" prefix."""

    branches = {}

    cmdline = "git rev-parse --symbolic "
    if branchesAreInRemotes:
        cmdline += "--remotes"
    else:
        cmdline += "--branches"

    for line in read_pipe_lines(cmdline):
        line = line.strip()

        # only import to p4/
        if not line.startswith('p4/'):
            continue
        # special symbolic ref to p4/master
        if line == "p4/HEAD":
            continue

        # strip off p4/ prefix
        branch = line[len("p4/"):]

        branches[branch] = parseRevision(line)

    return branches

def branch_exists(branch):
    """Make sure that the given ref name really exists."""

    cmd = [ "git", "rev-parse", "--symbolic", "--verify", branch ]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, _ = p.communicate()
    if p.returncode:
        return False
    # expect exactly one line of output: the branch name
    return out.rstrip() == branch

def findUpstreamBranchPoint(head = "HEAD"):
    branches = p4BranchesInGit()
    # map from depot-path to branch name
    branchByDepotPath = {}
    for branch in branches.keys():
        tip = branches[branch]
        log = extractLogMessageFromGitCommit(tip)
        settings = extractSettingsGitLog(log)
        if settings.has_key("depot-paths"):
            paths = ",".join(settings["depot-paths"])
            branchByDepotPath[paths] = "remotes/p4/" + branch

    settings = None
    parent = 0
    while parent < 65535:
        commit = head + "~%s" % parent
        log = extractLogMessageFromGitCommit(commit)
        settings = extractSettingsGitLog(log)
        if settings.has_key("depot-paths"):
            paths = ",".join(settings["depot-paths"])
            if branchByDepotPath.has_key(paths):
                return [branchByDepotPath[paths], settings]

        parent = parent + 1

    return ["", settings]

def createOrUpdateBranchesFromOrigin(localRefPrefix = "refs/remotes/p4/", silent=True):
    if not silent:
        print ("Creating/updating branch(es) in %s based on origin branch(es)"
               % localRefPrefix)

    originPrefix = "origin/p4/"

    for line in read_pipe_lines("git rev-parse --symbolic --remotes"):
        line = line.strip()
        if (not line.startswith(originPrefix)) or line.endswith("HEAD"):
            continue

        headName = line[len(originPrefix):]
        remoteHead = localRefPrefix + headName
        originHead = line

        original = extractSettingsGitLog(extractLogMessageFromGitCommit(originHead))
        if (not original.has_key('depot-paths')
            or not original.has_key('change')):
            continue

        update = False
        if not gitBranchExists(remoteHead):
            if verbose:
                print "creating %s" % remoteHead
            update = True
        else:
            settings = extractSettingsGitLog(extractLogMessageFromGitCommit(remoteHead))
            if settings.has_key('change') > 0:
                if settings['depot-paths'] == original['depot-paths']:
                    originP4Change = int(original['change'])
                    p4Change = int(settings['change'])
                    if originP4Change > p4Change:
                        print ("%s (%s) is newer than %s (%s). "
                               "Updating p4 branch from origin."
                               % (originHead, originP4Change,
                                  remoteHead, p4Change))
                        update = True
                else:
                    print ("Ignoring: %s was imported from %s while "
                           "%s was imported from %s"
                           % (originHead, ','.join(original['depot-paths']),
                              remoteHead, ','.join(settings['depot-paths'])))

        if update:
            system("git update-ref %s %s" % (remoteHead, originHead))

def originP4BranchesExist():
        return gitBranchExists("origin") or gitBranchExists("origin/p4") or gitBranchExists("origin/p4/master")


def p4ParseNumericChangeRange(parts):
    changeStart = int(parts[0][1:])
    if parts[1] == '#head':
        changeEnd = p4_last_change()
    else:
        changeEnd = int(parts[1])

    return (changeStart, changeEnd)

def chooseBlockSize(blockSize):
    if blockSize:
        return blockSize
    else:
        return defaultBlockSize

def p4ChangesForPaths(depotPaths, changeRange, requestedBlockSize):
    assert depotPaths

    # Parse the change range into start and end. Try to find integer
    # revision ranges as these can be broken up into blocks to avoid
    # hitting server-side limits (maxrows, maxscanresults). But if
    # that doesn't work, fall back to using the raw revision specifier
    # strings, without using block mode.

    if changeRange is None or changeRange == '':
        changeStart = 1
        changeEnd = p4_last_change()
        block_size = chooseBlockSize(requestedBlockSize)
    else:
        parts = changeRange.split(',')
        assert len(parts) == 2
        try:
            (changeStart, changeEnd) = p4ParseNumericChangeRange(parts)
            block_size = chooseBlockSize(requestedBlockSize)
        except:
            changeStart = parts[0][1:]
            changeEnd = parts[1]
            if requestedBlockSize:
                die("cannot use --changes-block-size with non-numeric revisions")
            block_size = None

    changes = []

    # Retrieve changes a block at a time, to prevent running
    # into a MaxResults/MaxScanRows error from the server.

    while True:
        cmd = ['changes']

        if block_size:
            end = min(changeEnd, changeStart + block_size)
            revisionRange = "%d,%d" % (changeStart, end)
        else:
            revisionRange = "%s,%s" % (changeStart, changeEnd)

        for p in depotPaths:
            cmd += ["%s...@%s" % (p, revisionRange)]

        # Insert changes in chronological order
        for line in reversed(p4_read_pipe_lines(cmd)):
            changes.append(int(line.split(" ")[1]))

        if not block_size:
            break

        if end >= changeEnd:
            break

        changeStart = end + 1

    changes = sorted(changes)
    return changes

def p4PathStartsWith(path, prefix):
    # This method tries to remedy a potential mixed-case issue:
    #
    # If UserA adds  //depot/DirA/file1
    # and UserB adds //depot/dira/file2
    #
    # we may or may not have a problem. If you have core.ignorecase=true,
    # we treat DirA and dira as the same directory
    if gitConfigBool("core.ignorecase"):
        return path.lower().startswith(prefix.lower())
    return path.startswith(prefix)

def getClientSpec():
    """Look at the p4 client spec, create a View() object that contains
       all the mappings, and return it."""

    specList = p4CmdList("client -o")
    if len(specList) != 1:
        die('Output from "client -o" is %d lines, expecting 1' %
            len(specList))

    # dictionary of all client parameters
    entry = specList[0]

    # the //client/ name
    client_name = entry["Client"]

    # just the keys that start with "View"
    view_keys = [ k for k in entry.keys() if k.startswith("View") ]

    # hold this new View
    view = View(client_name)

    # append the lines, in order, to the view
    for view_num in range(len(view_keys)):
        k = "View%d" % view_num
        if k not in view_keys:
            die("Expected view key %s missing" % k)
        view.append(entry[k])

    return view

def getClientRoot():
    """Grab the client directory."""

    output = p4CmdList("client -o")
    if len(output) != 1:
        die('Output from "client -o" is %d lines, expecting 1' % len(output))

    entry = output[0]
    if "Root" not in entry:
        die('Client has no "Root"')

    return entry["Root"]

#
# P4 wildcards are not allowed in filenames.  P4 complains
# if you simply add them, but you can force it with "-f", in
# which case it translates them into %xx encoding internally.
#
def wildcard_decode(path):
    # Search for and fix just these four characters.  Do % last so
    # that fixing it does not inadvertently create new %-escapes.
    # Cannot have * in a filename in windows; untested as to
    # what p4 would do in such a case.
    if not platform.system() == "Windows":
        path = path.replace("%2A", "*")
    path = path.replace("%23", "#") \
               .replace("%40", "@") \
               .replace("%25", "%")
    return path

def wildcard_encode(path):
    # do % first to avoid double-encoding the %s introduced here
    path = path.replace("%", "%25") \
               .replace("*", "%2A") \
               .replace("#", "%23") \
               .replace("@", "%40")
    return path

def wildcard_present(path):
    m = re.search("[*#@%]", path)
    return m is not None

class LargeFileSystem(object):
    """Base class for large file system support."""

    def __init__(self, writeToGitStream):
        self.largeFiles = set()
        self.writeToGitStream = writeToGitStream

    def generatePointer(self, cloneDestination, contentFile):
        """Return the content of a pointer file that is stored in Git instead of
           the actual content."""
        assert False, "Method 'generatePointer' required in " + self.__class__.__name__

    def pushFile(self, localLargeFile):
        """Push the actual content which is not stored in the Git repository to
           a server."""
        assert False, "Method 'pushFile' required in " + self.__class__.__name__

    def hasLargeFileExtension(self, relPath):
        return reduce(
            lambda a, b: a or b,
            [relPath.endswith('.' + e) for e in gitConfigList('git-p4.largeFileExtensions')],
            False
        )

    def generateTempFile(self, contents):
        contentFile = tempfile.NamedTemporaryFile(prefix='git-p4-large-file', delete=False)
        for d in contents:
            contentFile.write(d)
        contentFile.close()
        return contentFile.name

    def exceedsLargeFileThreshold(self, relPath, contents):
        if gitConfigInt('git-p4.largeFileThreshold'):
            contentsSize = sum(len(d) for d in contents)
            if contentsSize > gitConfigInt('git-p4.largeFileThreshold'):
                return True
        if gitConfigInt('git-p4.largeFileCompressedThreshold'):
            contentsSize = sum(len(d) for d in contents)
            if contentsSize <= gitConfigInt('git-p4.largeFileCompressedThreshold'):
                return False
            contentTempFile = self.generateTempFile(contents)
            compressedContentFile = tempfile.NamedTemporaryFile(prefix='git-p4-large-file', delete=False)
            zf = zipfile.ZipFile(compressedContentFile.name, mode='w')
            zf.write(contentTempFile, compress_type=zipfile.ZIP_DEFLATED)
            zf.close()
            compressedContentsSize = zf.infolist()[0].compress_size
            os.remove(contentTempFile)
            os.remove(compressedContentFile.name)
            if compressedContentsSize > gitConfigInt('git-p4.largeFileCompressedThreshold'):
                return True
        return False

    def addLargeFile(self, relPath):
        self.largeFiles.add(relPath)

    def removeLargeFile(self, relPath):
        self.largeFiles.remove(relPath)

    def isLargeFile(self, relPath):
        return relPath in self.largeFiles

    def processContent(self, git_mode, relPath, contents):
        """Processes the content of git fast import. This method decides if a
           file is stored in the large file system and handles all necessary
           steps."""
        if self.exceedsLargeFileThreshold(relPath, contents) or self.hasLargeFileExtension(relPath):
            contentTempFile = self.generateTempFile(contents)
            (git_mode, contents, localLargeFile) = self.generatePointer(contentTempFile)

            # Move temp file to final location in large file system
            largeFileDir = os.path.dirname(localLargeFile)
            if not os.path.isdir(largeFileDir):
                os.makedirs(largeFileDir)
            shutil.move(contentTempFile, localLargeFile)
            self.addLargeFile(relPath)
            if gitConfigBool('git-p4.largeFilePush'):
                self.pushFile(localLargeFile)
            if verbose:
                sys.stderr.write("%s moved to large file system (%s)\n" % (relPath, localLargeFile))
        return (git_mode, contents)

class MockLFS(LargeFileSystem):
    """Mock large file system for testing."""

    def generatePointer(self, contentFile):
        """The pointer content is the original content prefixed with "pointer-".
           The local filename of the large file storage is derived from the file content.
           """
        with open(contentFile, 'r') as f:
            content = next(f)
            gitMode = '100644'
            pointerContents = 'pointer-' + content
            localLargeFile = os.path.join(os.getcwd(), '.git', 'mock-storage', 'local', content[:-1])
            return (gitMode, pointerContents, localLargeFile)

    def pushFile(self, localLargeFile):
        """The remote filename of the large file storage is the same as the local
           one but in a different directory.
           """
        remotePath = os.path.join(os.path.dirname(localLargeFile), '..', 'remote')
        if not os.path.exists(remotePath):
            os.makedirs(remotePath)
        shutil.copyfile(localLargeFile, os.path.join(remotePath, os.path.basename(localLargeFile)))

class GitLFS(LargeFileSystem):
    """Git LFS as backend for the git-p4 large file system.
       See https://git-lfs.github.com/ for details."""

    def __init__(self, *args):
        LargeFileSystem.__init__(self, *args)
        self.baseGitAttributes = []

    def generatePointer(self, contentFile):
        """Generate a Git LFS pointer for the content. Return LFS Pointer file
           mode and content which is stored in the Git repository instead of
           the actual content. Return also the new location of the actual
           content.
           """
        pointerProcess = subprocess.Popen(
            ['git', 'lfs', 'pointer', '--file=' + contentFile],
            stdout=subprocess.PIPE
        )
        pointerFile = pointerProcess.stdout.read()
        if pointerProcess.wait():
            os.remove(contentFile)
            die('git-lfs pointer command failed. Did you install the extension?')

        # Git LFS removed the preamble in the output of the 'pointer' command
        # starting from version 1.2.0. Check for the preamble here to support
        # earlier versions.
        # c.f. https://github.com/github/git-lfs/commit/da2935d9a739592bc775c98d8ef4df9c72ea3b43
        if pointerFile.startswith('Git LFS pointer for'):
            pointerFile = re.sub(r'Git LFS pointer for.*\n\n', '', pointerFile)

        oid = re.search(r'^oid \w+:(\w+)', pointerFile, re.MULTILINE).group(1)
        localLargeFile = os.path.join(
            os.getcwd(),
            '.git', 'lfs', 'objects', oid[:2], oid[2:4],
            oid,
        )
        # LFS Spec states that pointer files should not have the executable bit set.
        gitMode = '100644'
        return (gitMode, pointerFile, localLargeFile)

    def pushFile(self, localLargeFile):
        uploadProcess = subprocess.Popen(
            ['git', 'lfs', 'push', '--object-id', 'origin', os.path.basename(localLargeFile)]
        )
        if uploadProcess.wait():
            die('git-lfs push command failed. Did you define a remote?')

    def generateGitAttributes(self):
        return (
            self.baseGitAttributes +
            [
                '\n',
                '#\n',
                '# Git LFS (see https://git-lfs.github.com/)\n',
                '#\n',
            ] +
            ['*.' + f.replace(' ', '[[:space:]]') + ' filter=lfs -text\n'
                for f in sorted(gitConfigList('git-p4.largeFileExtensions'))
            ] +
            ['/' + f.replace(' ', '[[:space:]]') + ' filter=lfs -text\n'
                for f in sorted(self.largeFiles) if not self.hasLargeFileExtension(f)
            ]
        )

    def addLargeFile(self, relPath):
        LargeFileSystem.addLargeFile(self, relPath)
        self.writeToGitStream('100644', '.gitattributes', self.generateGitAttributes())

    def removeLargeFile(self, relPath):
        LargeFileSystem.removeLargeFile(self, relPath)
        self.writeToGitStream('100644', '.gitattributes', self.generateGitAttributes())

    def processContent(self, git_mode, relPath, contents):
        if relPath == '.gitattributes':
            self.baseGitAttributes = contents
            return (git_mode, self.generateGitAttributes())
        else:
            return LargeFileSystem.processContent(self, git_mode, relPath, contents)

class Command:
    def __init__(self):
        self.usage = "usage: %prog [options]"
        self.needsGit = True
        self.verbose = False

class P4UserMap:
    def __init__(self):
        self.userMapFromPerforceServer = False
        self.myP4UserId = None

    def p4UserId(self):
        if self.myP4UserId:
            return self.myP4UserId

        results = p4CmdList("user -o")
        for r in results:
            if r.has_key('User'):
                self.myP4UserId = r['User']
                return r['User']
        die("Could not find your p4 user id")

    def p4UserIsMe(self, p4User):
        # return True if the given p4 user is actually me
        me = self.p4UserId()
        if not p4User or p4User != me:
            return False
        else:
            return True

    def getUserCacheFilename(self):
        home = os.environ.get("HOME", os.environ.get("USERPROFILE"))
        return home + "/.gitp4-usercache.txt"

    def getUserMapFromPerforceServer(self):
        if self.userMapFromPerforceServer:
            return
        self.users = {}
        self.emails = {}

        for output in p4CmdList("users"):
            if not output.has_key("User"):
                continue
            self.users[output["User"]] = output["FullName"] + " <" + output["Email"] + ">"
            self.emails[output["Email"]] = output["User"]

        mapUserConfigRegex = re.compile(r"^\s*(\S+)\s*=\s*(.+)\s*<(\S+)>\s*$", re.VERBOSE)
        for mapUserConfig in gitConfigList("git-p4.mapUser"):
            mapUser = mapUserConfigRegex.findall(mapUserConfig)
            if mapUser and len(mapUser[0]) == 3:
                user = mapUser[0][0]
                fullname = mapUser[0][1]
                email = mapUser[0][2]
                self.users[user] = fullname + " <" + email + ">"
                self.emails[email] = user

        s = ''
        for (key, val) in self.users.items():
            s += "%s\t%s\n" % (key.expandtabs(1), val.expandtabs(1))

        open(self.getUserCacheFilename(), "wb").write(s)
        self.userMapFromPerforceServer = True

    def loadUserMapFromCache(self):
        self.users = {}
        self.userMapFromPerforceServer = False
        try:
            cache = open(self.getUserCacheFilename(), "rb")
            lines = cache.readlines()
            cache.close()
            for line in lines:
                entry = line.strip().split("\t")
                self.users[entry[0]] = entry[1]
        except IOError:
            self.getUserMapFromPerforceServer()

class P4Debug(Command):
    def __init__(self):
        Command.__init__(self)
        self.options = []
        self.description = "A tool to debug the output of p4 -G."
        self.needsGit = False

    def run(self, args):
        j = 0
        for output in p4CmdList(args):
            print 'Element: %d' % j
            j += 1
            print output
        return True

class P4RollBack(Command):
    def __init__(self):
        Command.__init__(self)
        self.options = [
            optparse.make_option("--local", dest="rollbackLocalBranches", action="store_true")
        ]
        self.description = "A tool to debug the multi-branch import. Don't use :)"
        self.rollbackLocalBranches = False

    def run(self, args):
        if len(args) != 1:
            return False
        maxChange = int(args[0])

        if "p4ExitCode" in p4Cmd("changes -m 1"):
            die("Problems executing p4");

        if self.rollbackLocalBranches:
            refPrefix = "refs/heads/"
            lines = read_pipe_lines("git rev-parse --symbolic --branches")
        else:
            refPrefix = "refs/remotes/"
            lines = read_pipe_lines("git rev-parse --symbolic --remotes")

        for line in lines:
            if self.rollbackLocalBranches or (line.startswith("p4/") and line != "p4/HEAD\n"):
                line = line.strip()
                ref = refPrefix + line
                log = extractLogMessageFromGitCommit(ref)
                settings = extractSettingsGitLog(log)

                depotPaths = settings['depot-paths']
                change = settings['change']

                changed = False

                if len(p4Cmd("changes -m 1 "  + ' '.join (['%s...@%s' % (p, maxChange)
                                                           for p in depotPaths]))) == 0:
                    print "Branch %s did not exist at change %s, deleting." % (ref, maxChange)
                    system("git update-ref -d %s `git rev-parse %s`" % (ref, ref))
                    continue

                while change and int(change) > maxChange:
                    changed = True
                    if self.verbose:
                        print "%s is at %s ; rewinding towards %s" % (ref, change, maxChange)
                    system("git update-ref %s \"%s^\"" % (ref, ref))
                    log = extractLogMessageFromGitCommit(ref)
                    settings =  extractSettingsGitLog(log)


                    depotPaths = settings['depot-paths']
                    change = settings['change']

                if changed:
                    print "%s rewound to %s" % (ref, change)

        return True

class P4Submit(Command, P4UserMap):

    conflict_behavior_choices = ("ask", "skip", "quit")

    def __init__(self):
        Command.__init__(self)
        P4UserMap.__init__(self)
        self.options = [
                optparse.make_option("--origin", dest="origin"),
                optparse.make_option("-M", dest="detectRenames", action="store_true"),
                # preserve the user, requires relevant p4 permissions
                optparse.make_option("--preserve-user", dest="preserveUser", action="store_true"),
                optparse.make_option("--export-labels", dest="exportLabels", action="store_true"),
                optparse.make_option("--dry-run", "-n", dest="dry_run", action="store_true"),
                optparse.make_option("--prepare-p4-only", dest="prepare_p4_only", action="store_true"),
                optparse.make_option("--conflict", dest="conflict_behavior",
                                     choices=self.conflict_behavior_choices),
                optparse.make_option("--branch", dest="branch"),
        ]
        self.description = "Submit changes from git to the perforce depot."
        self.usage += " [name of git branch to submit into perforce depot]"
        self.origin = ""
        self.detectRenames = False
        self.preserveUser = gitConfigBool("git-p4.preserveUser")
        self.dry_run = False
        self.prepare_p4_only = False
        self.conflict_behavior = None
        self.isWindows = (platform.system() == "Windows")
        self.exportLabels = False
        self.p4HasMoveCommand = p4_has_move_command()
        self.branch = None

        if gitConfig('git-p4.largeFileSystem'):
            die("Large file system not supported for git-p4 submit command. Please remove it from config.")

    def check(self):
        if len(p4CmdList("opened ...")) > 0:
            die("You have files opened with perforce! Close them before starting the sync.")

    def separate_jobs_from_description(self, message):
        """Extract and return a possible Jobs field in the commit
           message.  It goes into a separate section in the p4 change
           specification.

           A jobs line starts with "Jobs:" and looks like a new field
           in a form.  Values are white-space separated on the same
           line or on following lines that start with a tab.

           This does not parse and extract the full git commit message
           like a p4 form.  It just sees the Jobs: line as a marker
           to pass everything from then on directly into the p4 form,
           but outside the description section.

           Return a tuple (stripped log message, jobs string)."""

        m = re.search(r'^Jobs:', message, re.MULTILINE)
        if m is None:
            return (message, None)

        jobtext = message[m.start():]
        stripped_message = message[:m.start()].rstrip()
        return (stripped_message, jobtext)

    def prepareLogMessage(self, template, message, jobs):
        """Edits the template returned from "p4 change -o" to insert
           the message in the Description field, and the jobs text in
           the Jobs field."""
        result = ""

        inDescriptionSection = False

        for line in template.split("\n"):
            if line.startswith("#"):
                result += line + "\n"
                continue

            if inDescriptionSection:
                if line.startswith("Files:") or line.startswith("Jobs:"):
                    inDescriptionSection = False
                    # insert Jobs section
                    if jobs:
                        result += jobs + "\n"
                else:
                    continue
            else:
                if line.startswith("Description:"):
                    inDescriptionSection = True
                    line += "\n"
                    for messageLine in message.split("\n"):
                        line += "\t" + messageLine + "\n"

            result += line + "\n"

        return result

    def patchRCSKeywords(self, file, pattern):
        # Attempt to zap the RCS keywords in a p4 controlled file matching the given pattern
        (handle, outFileName) = tempfile.mkstemp(dir='.')
        try:
            outFile = os.fdopen(handle, "w+")
            inFile = open(file, "r")
            regexp = re.compile(pattern, re.VERBOSE)
            for line in inFile.readlines():
                line = regexp.sub(r'$\1$', line)
                outFile.write(line)
            inFile.close()
            outFile.close()
            # Forcibly overwrite the original file
            os.unlink(file)
            shutil.move(outFileName, file)
        except:
            # cleanup our temporary file
            os.unlink(outFileName)
            print "Failed to strip RCS keywords in %s" % file
            raise

        print "Patched up RCS keywords in %s" % file

    def p4UserForCommit(self,id):
        # Return the tuple (perforce user,git email) for a given git commit id
        self.getUserMapFromPerforceServer()
        gitEmail = read_pipe(["git", "log", "--max-count=1",
                              "--format=%ae", id])
        gitEmail = gitEmail.strip()
        if not self.emails.has_key(gitEmail):
            return (None,gitEmail)
        else:
            return (self.emails[gitEmail],gitEmail)

    def checkValidP4Users(self,commits):
        # check if any git authors cannot be mapped to p4 users
        for id in commits:
            (user,email) = self.p4UserForCommit(id)
            if not user:
                msg = "Cannot find p4 user for email %s in commit %s." % (email, id)
                if gitConfigBool("git-p4.allowMissingP4Users"):
                    print "%s" % msg
                else:
                    die("Error: %s\nSet git-p4.allowMissingP4Users to true to allow this." % msg)

    def lastP4Changelist(self):
        # Get back the last changelist number submitted in this client spec. This
        # then gets used to patch up the username in the change. If the same
        # client spec is being used by multiple processes then this might go
        # wrong.
        results = p4CmdList("client -o")        # find the current client
        client = None
        for r in results:
            if r.has_key('Client'):
                client = r['Client']
                break
        if not client:
            die("could not get client spec")
        results = p4CmdList(["changes", "-c", client, "-m", "1"])
        for r in results:
            if r.has_key('change'):
                return r['change']
        die("Could not get changelist number for last submit - cannot patch up user details")

    def modifyChangelistUser(self, changelist, newUser):
        # fixup the user field of a changelist after it has been submitted.
        changes = p4CmdList("change -o %s" % changelist)
        if len(changes) != 1:
            die("Bad output from p4 change modifying %s to user %s" %
                (changelist, newUser))

        c = changes[0]
        if c['User'] == newUser: return   # nothing to do
        c['User'] = newUser
        input = marshal.dumps(c)

        result = p4CmdList("change -f -i", stdin=input)
        for r in result:
            if r.has_key('code'):
                if r['code'] == 'error':
                    die("Could not modify user field of changelist %s to %s:%s" % (changelist, newUser, r['data']))
            if r.has_key('data'):
                print("Updated user field for changelist %s to %s" % (changelist, newUser))
                return
        die("Could not modify user field of changelist %s to %s" % (changelist, newUser))

    def canChangeChangelists(self):
        # check to see if we have p4 admin or super-user permissions, either of
        # which are required to modify changelists.
        results = p4CmdList(["protects", self.depotPath])
        for r in results:
            if r.has_key('perm'):
                if r['perm'] == 'admin':
                    return 1
                if r['perm'] == 'super':
                    return 1
        return 0

    def prepareSubmitTemplate(self):
        """Run "p4 change -o" to grab a change specification template.
           This does not use "p4 -G", as it is nice to keep the submission
           template in original order, since a human might edit it.

           Remove lines in the Files section that show changes to files
           outside the depot path we're committing into."""

        [upstream, settings] = findUpstreamBranchPoint()

        template = ""
        inFilesSection = False
        for line in p4_read_pipe_lines(['change', '-o']):
            if line.endswith("\r\n"):
                line = line[:-2] + "\n"
            if inFilesSection:
                if line.startswith("\t"):
                    # path starts and ends with a tab
                    path = line[1:]
                    lastTab = path.rfind("\t")
                    if lastTab != -1:
                        path = path[:lastTab]
                        if settings.has_key('depot-paths'):
                            if not [p for p in settings['depot-paths']
                                    if p4PathStartsWith(path, p)]:
                                continue
                        else:
                            if not p4PathStartsWith(path, self.depotPath):
                                continue
                else:
                    inFilesSection = False
            else:
                if line.startswith("Files:"):
                    inFilesSection = True

            template += line

        return template

    def edit_template(self, template_file):
        """Invoke the editor to let the user change the submission
           message.  Return true if okay to continue with the submit."""

        # if configured to skip the editing part, just submit
        if gitConfigBool("git-p4.skipSubmitEdit"):
            return True

        # look at the modification time, to check later if the user saved
        # the file
        mtime = os.stat(template_file).st_mtime

        # invoke the editor
        if os.environ.has_key("P4EDITOR") and (os.environ.get("P4EDITOR") != ""):
            editor = os.environ.get("P4EDITOR")
        else:
            editor = read_pipe("git var GIT_EDITOR").strip()
        system(["sh", "-c", ('%s "$@"' % editor), editor, template_file])

        # If the file was not saved, prompt to see if this patch should
        # be skipped.  But skip this verification step if configured so.
        if gitConfigBool("git-p4.skipSubmitEditCheck"):
            return True

        # modification time updated means user saved the file
        if os.stat(template_file).st_mtime > mtime:
            return True

        while True:
            response = raw_input("Submit template unchanged. Submit anyway? [y]es, [n]o (skip this patch) ")
            if response == 'y':
                return True
            if response == 'n':
                return False

    def get_diff_description(self, editedFiles, filesToAdd):
        # diff
        if os.environ.has_key("P4DIFF"):
            del(os.environ["P4DIFF"])
        diff = ""
        for editedFile in editedFiles:
            diff += p4_read_pipe(['diff', '-du',
                                  wildcard_encode(editedFile)])

        # new file diff
        newdiff = ""
        for newFile in filesToAdd:
            newdiff += "==== new file ====\n"
            newdiff += "--- /dev/null\n"
            newdiff += "+++ %s\n" % newFile
            f = open(newFile, "r")
            for line in f.readlines():
                newdiff += "+" + line
            f.close()

        return (diff + newdiff).replace('\r\n', '\n')

    def applyCommit(self, id):
        """Apply one commit, return True if it succeeded."""

        print "Applying", read_pipe(["git", "show", "-s",
                                     "--format=format:%h %s", id])

        (p4User, gitEmail) = self.p4UserForCommit(id)

        diff = read_pipe_lines("git diff-tree -r %s \"%s^\" \"%s\"" % (self.diffOpts, id, id))
        filesToAdd = set()
        filesToChangeType = set()
        filesToDelete = set()
        editedFiles = set()
        pureRenameCopy = set()
        filesToChangeExecBit = {}

        for line in diff:
            diff = parseDiffTreeEntry(line)
            modifier = diff['status']
            path = diff['src']
            if modifier == "M":
                p4_edit(path)
                if isModeExecChanged(diff['src_mode'], diff['dst_mode']):
                    filesToChangeExecBit[path] = diff['dst_mode']
                editedFiles.add(path)
            elif modifier == "A":
                filesToAdd.add(path)
                filesToChangeExecBit[path] = diff['dst_mode']
                if path in filesToDelete:
                    filesToDelete.remove(path)
            elif modifier == "D":
                filesToDelete.add(path)
                if path in filesToAdd:
                    filesToAdd.remove(path)
            elif modifier == "C":
                src, dest = diff['src'], diff['dst']
                p4_integrate(src, dest)
                pureRenameCopy.add(dest)
                if diff['src_sha1'] != diff['dst_sha1']:
                    p4_edit(dest)
                    pureRenameCopy.discard(dest)
                if isModeExecChanged(diff['src_mode'], diff['dst_mode']):
                    p4_edit(dest)
                    pureRenameCopy.discard(dest)
                    filesToChangeExecBit[dest] = diff['dst_mode']
                if self.isWindows:
                    # turn off read-only attribute
                    os.chmod(dest, stat.S_IWRITE)
                os.unlink(dest)
                editedFiles.add(dest)
            elif modifier == "R":
                src, dest = diff['src'], diff['dst']
                if self.p4HasMoveCommand:
                    p4_edit(src)        # src must be open before move
                    p4_move(src, dest)  # opens for (move/delete, move/add)
                else:
                    p4_integrate(src, dest)
                    if diff['src_sha1'] != diff['dst_sha1']:
                        p4_edit(dest)
                    else:
                        pureRenameCopy.add(dest)
                if isModeExecChanged(diff['src_mode'], diff['dst_mode']):
                    if not self.p4HasMoveCommand:
                        p4_edit(dest)   # with move: already open, writable
                    filesToChangeExecBit[dest] = diff['dst_mode']
                if not self.p4HasMoveCommand:
                    if self.isWindows:
                        os.chmod(dest, stat.S_IWRITE)
                    os.unlink(dest)
                    filesToDelete.add(src)
                editedFiles.add(dest)
            elif modifier == "T":
                filesToChangeType.add(path)
            else:
                die("unknown modifier %s for %s" % (modifier, path))

        diffcmd = "git diff-tree --full-index -p \"%s\"" % (id)
        patchcmd = diffcmd + " | git apply "
        tryPatchCmd = patchcmd + "--check -"
        applyPatchCmd = patchcmd + "--check --apply -"
        patch_succeeded = True

        if os.system(tryPatchCmd) != 0:
            fixed_rcs_keywords = False
            patch_succeeded = False
            print "Unfortunately applying the change failed!"

            # Patch failed, maybe it's just RCS keyword woes. Look through
            # the patch to see if that's possible.
            if gitConfigBool("git-p4.attemptRCSCleanup"):
                file = None
                pattern = None
                kwfiles = {}
                for file in editedFiles | filesToDelete:
                    # did this file's delta contain RCS keywords?
                    pattern = p4_keywords_regexp_for_file(file)

                    if pattern:
                        # this file is a possibility...look for RCS keywords.
                        regexp = re.compile(pattern, re.VERBOSE)
                        for line in read_pipe_lines(["git", "diff", "%s^..%s" % (id, id), file]):
                            if regexp.search(line):
                                if verbose:
                                    print "got keyword match on %s in %s in %s" % (pattern, line, file)
                                kwfiles[file] = pattern
                                break

                for file in kwfiles:
                    if verbose:
                        print "zapping %s with %s" % (line,pattern)
                    # File is being deleted, so not open in p4.  Must
                    # disable the read-only bit on windows.
                    if self.isWindows and file not in editedFiles:
                        os.chmod(file, stat.S_IWRITE)
                    self.patchRCSKeywords(file, kwfiles[file])
                    fixed_rcs_keywords = True

            if fixed_rcs_keywords:
                print "Retrying the patch with RCS keywords cleaned up"
                if os.system(tryPatchCmd) == 0:
                    patch_succeeded = True

        if not patch_succeeded:
            for f in editedFiles:
                p4_revert(f)
            return False

        #
        # Apply the patch for real, and do add/delete/+x handling.
        #
        system(applyPatchCmd)

        for f in filesToChangeType:
            p4_edit(f, "-t", "auto")
        for f in filesToAdd:
            p4_add(f)
        for f in filesToDelete:
            p4_revert(f)
            p4_delete(f)

        # Set/clear executable bits
        for f in filesToChangeExecBit.keys():
            mode = filesToChangeExecBit[f]
            setP4ExecBit(f, mode)

        #
        # Build p4 change description, starting with the contents
        # of the git commit message.
        #
        logMessage = extractLogMessageFromGitCommit(id)
        logMessage = logMessage.strip()
        (logMessage, jobs) = self.separate_jobs_from_description(logMessage)

        template = self.prepareSubmitTemplate()
        submitTemplate = self.prepareLogMessage(template, logMessage, jobs)

        if self.preserveUser:
           submitTemplate += "\n######## Actual user %s, modified after commit\n" % p4User

        if self.checkAuthorship and not self.p4UserIsMe(p4User):
            submitTemplate += "######## git author %s does not match your p4 account.\n" % gitEmail
            submitTemplate += "######## Use option --preserve-user to modify authorship.\n"
            submitTemplate += "######## Variable git-p4.skipUserNameCheck hides this message.\n"

        separatorLine = "######## everything below this line is just the diff #######\n"
        if not self.prepare_p4_only:
            submitTemplate += separatorLine
            submitTemplate += self.get_diff_description(editedFiles, filesToAdd)

        (handle, fileName) = tempfile.mkstemp()
        tmpFile = os.fdopen(handle, "w+b")
        if self.isWindows:
            submitTemplate = submitTemplate.replace("\n", "\r\n")
        tmpFile.write(submitTemplate)
        tmpFile.close()

        if self.prepare_p4_only:
            #
            # Leave the p4 tree prepared, and the submit template around
            # and let the user decide what to do next
            #
            print
            print "P4 workspace prepared for submission."
            print "To submit or revert, go to client workspace"
            print "  " + self.clientPath
            print
            print "To submit, use \"p4 submit\" to write a new description,"
            print "or \"p4 submit -i <%s\" to use the one prepared by" \
                  " \"git p4\"." % fileName
            print "You can delete the file \"%s\" when finished." % fileName

            if self.preserveUser and p4User and not self.p4UserIsMe(p4User):
                print "To preserve change ownership by user %s, you must\n" \
                      "do \"p4 change -f <change>\" after submitting and\n" \
                      "edit the User field."
            if pureRenameCopy:
                print "After submitting, renamed files must be re-synced."
                print "Invoke \"p4 sync -f\" on each of these files:"
                for f in pureRenameCopy:
                    print "  " + f

            print
            print "To revert the changes, use \"p4 revert ...\", and delete"
            print "the submit template file \"%s\"" % fileName
            if filesToAdd:
                print "Since the commit adds new files, they must be deleted:"
                for f in filesToAdd:
                    print "  " + f
            print
            return True

        #
        # Let the user edit the change description, then submit it.
        #
        submitted = False

        try:
            if self.edit_template(fileName):
                # read the edited message and submit
                tmpFile = open(fileName, "rb")
                message = tmpFile.read()
                tmpFile.close()
                if self.isWindows:
                    message = message.replace("\r\n", "\n")
                submitTemplate = message[:message.index(separatorLine)]
                p4_write_pipe(['submit', '-i'], submitTemplate)

                if self.preserveUser:
                    if p4User:
                        # Get last changelist number. Cannot easily get it from
                        # the submit command output as the output is
                        # unmarshalled.
                        changelist = self.lastP4Changelist()
                        self.modifyChangelistUser(changelist, p4User)

                # The rename/copy happened by applying a patch that created a
                # new file.  This leaves it writable, which confuses p4.
                for f in pureRenameCopy:
                    p4_sync(f, "-f")
                submitted = True

        finally:
            # skip this patch
            if not submitted:
                print "Submission cancelled, undoing p4 changes."
                for f in editedFiles:
                    p4_revert(f)
                for f in filesToAdd:
                    p4_revert(f)
                    os.remove(f)
                for f in filesToDelete:
                    p4_revert(f)

        os.remove(fileName)
        return submitted

    # Export git tags as p4 labels. Create a p4 label and then tag
    # with that.
    def exportGitTags(self, gitTags):
        validLabelRegexp = gitConfig("git-p4.labelExportRegexp")
        if len(validLabelRegexp) == 0:
            validLabelRegexp = defaultLabelRegexp
        m = re.compile(validLabelRegexp)

        for name in gitTags:

            if not m.match(name):
                if verbose:
                    print "tag %s does not match regexp %s" % (name, validLabelRegexp)
                continue

            # Get the p4 commit this corresponds to
            logMessage = extractLogMessageFromGitCommit(name)
            values = extractSettingsGitLog(logMessage)

            if not values.has_key('change'):
                # a tag pointing to something not sent to p4; ignore
                if verbose:
                    print "git tag %s does not give a p4 commit" % name
                continue
            else:
                changelist = values['change']

            # Get the tag details.
            inHeader = True
            isAnnotated = False
            body = []
            for l in read_pipe_lines(["git", "cat-file", "-p", name]):
                l = l.strip()
                if inHeader:
                    if re.match(r'tag\s+', l):
                        isAnnotated = True
                    elif re.match(r'\s*$', l):
                        inHeader = False
                        continue
                else:
                    body.append(l)

            if not isAnnotated:
                body = ["lightweight tag imported by git p4\n"]

            # Create the label - use the same view as the client spec we are using
            clientSpec = getClientSpec()

            labelTemplate  = "Label: %s\n" % name
            labelTemplate += "Description:\n"
            for b in body:
                labelTemplate += "\t" + b + "\n"
            labelTemplate += "View:\n"
            for depot_side in clientSpec.mappings:
                labelTemplate += "\t%s\n" % depot_side

            if self.dry_run:
                print "Would create p4 label %s for tag" % name
            elif self.prepare_p4_only:
                print "Not creating p4 label %s for tag due to option" \
                      " --prepare-p4-only" % name
            else:
                p4_write_pipe(["label", "-i"], labelTemplate)

                # Use the label
                p4_system(["tag", "-l", name] +
                          ["%s@%s" % (depot_side, changelist) for depot_side in clientSpec.mappings])

                if verbose:
                    print "created p4 label for tag %s" % name

    def run(self, args):
        if len(args) == 0:
            self.master = currentGitBranch()
        elif len(args) == 1:
            self.master = args[0]
            if not branchExists(self.master):
                die("Branch %s does not exist" % self.master)
        else:
            return False

        if self.master:
            allowSubmit = gitConfig("git-p4.allowSubmit")
            if len(allowSubmit) > 0 and not self.master in allowSubmit.split(","):
                die("%s is not in git-p4.allowSubmit" % self.master)

        [upstream, settings] = findUpstreamBranchPoint()
        self.depotPath = settings['depot-paths'][0]
        if len(self.origin) == 0:
            self.origin = upstream

        if self.preserveUser:
            if not self.canChangeChangelists():
                die("Cannot preserve user names without p4 super-user or admin permissions")

        # if not set from the command line, try the config file
        if self.conflict_behavior is None:
            val = gitConfig("git-p4.conflict")
            if val:
                if val not in self.conflict_behavior_choices:
                    die("Invalid value '%s' for config git-p4.conflict" % val)
            else:
                val = "ask"
            self.conflict_behavior = val

        if self.verbose:
            print "Origin branch is " + self.origin

        if len(self.depotPath) == 0:
            print "Internal error: cannot locate perforce depot path from existing branches"
            sys.exit(128)

        self.useClientSpec = False
        if gitConfigBool("git-p4.useclientspec"):
            self.useClientSpec = True
        if self.useClientSpec:
            self.clientSpecDirs = getClientSpec()

        # Check for the existance of P4 branches
        branchesDetected = (len(p4BranchesInGit().keys()) > 1)

        if self.useClientSpec and not branchesDetected:
            # all files are relative to the client spec
            self.clientPath = getClientRoot()
        else:
            self.clientPath = p4Where(self.depotPath)

        if self.clientPath == "":
            die("Error: Cannot locate perforce checkout of %s in client view" % self.depotPath)

        print "Perforce checkout for depot path %s located at %s" % (self.depotPath, self.clientPath)
        self.oldWorkingDirectory = os.getcwd()

        # ensure the clientPath exists
        new_client_dir = False
        if not os.path.exists(self.clientPath):
            new_client_dir = True
            os.makedirs(self.clientPath)

        chdir(self.clientPath, is_client_path=True)
        if self.dry_run:
            print "Would synchronize p4 checkout in %s" % self.clientPath
        else:
            print "Synchronizing p4 checkout..."
            if new_client_dir:
                # old one was destroyed, and maybe nobody told p4
                p4_sync("...", "-f")
            else:
                p4_sync("...")
        self.check()

        commits = []
        if self.master:
            commitish = self.master
        else:
            commitish = 'HEAD'

        for line in read_pipe_lines(["git", "rev-list", "--no-merges", "%s..%s" % (self.origin, commitish)]):
            commits.append(line.strip())
        commits.reverse()

        if self.preserveUser or gitConfigBool("git-p4.skipUserNameCheck"):
            self.checkAuthorship = False
        else:
            self.checkAuthorship = True

        if self.preserveUser:
            self.checkValidP4Users(commits)

        #
        # Build up a set of options to be passed to diff when
        # submitting each commit to p4.
        #
        if self.detectRenames:
            # command-line -M arg
            self.diffOpts = "-M"
        else:
            # If not explicitly set check the config variable
            detectRenames = gitConfig("git-p4.detectRenames")

            if detectRenames.lower() == "false" or detectRenames == "":
                self.diffOpts = ""
            elif detectRenames.lower() == "true":
                self.diffOpts = "-M"
            else:
                self.diffOpts = "-M%s" % detectRenames

        # no command-line arg for -C or --find-copies-harder, just
        # config variables
        detectCopies = gitConfig("git-p4.detectCopies")
        if detectCopies.lower() == "false" or detectCopies == "":
            pass
        elif detectCopies.lower() == "true":
            self.diffOpts += " -C"
        else:
            self.diffOpts += " -C%s" % detectCopies

        if gitConfigBool("git-p4.detectCopiesHarder"):
            self.diffOpts += " --find-copies-harder"

        #
        # Apply the commits, one at a time.  On failure, ask if should
        # continue to try the rest of the patches, or quit.
        #
        if self.dry_run:
            print "Would apply"
        applied = []
        last = len(commits) - 1
        for i, commit in enumerate(commits):
            if self.dry_run:
                print " ", read_pipe(["git", "show", "-s",
                                      "--format=format:%h %s", commit])
                ok = True
            else:
                ok = self.applyCommit(commit)
            if ok:
                applied.append(commit)
            else:
                if self.prepare_p4_only and i < last:
                    print "Processing only the first commit due to option" \
                          " --prepare-p4-only"
                    break
                if i < last:
                    quit = False
                    while True:
                        # prompt for what to do, or use the option/variable
                        if self.conflict_behavior == "ask":
                            print "What do you want to do?"
                            response = raw_input("[s]kip this commit but apply"
                                                 " the rest, or [q]uit? ")
                            if not response:
                                continue
                        elif self.conflict_behavior == "skip":
                            response = "s"
                        elif self.conflict_behavior == "quit":
                            response = "q"
                        else:
                            die("Unknown conflict_behavior '%s'" %
                                self.conflict_behavior)

                        if response[0] == "s":
                            print "Skipping this commit, but applying the rest"
                            break
                        if response[0] == "q":
                            print "Quitting"
                            quit = True
                            break
                    if quit:
                        break

        chdir(self.oldWorkingDirectory)

        if self.dry_run:
            pass
        elif self.prepare_p4_only:
            pass
        elif len(commits) == len(applied):
            print "All commits applied!"

            sync = P4Sync()
            if self.branch:
                sync.branch = self.branch
            sync.run([])

            rebase = P4Rebase()
            rebase.rebase()

        else:
            if len(applied) == 0:
                print "No commits applied."
            else:
                print "Applied only the commits marked with '*':"
                for c in commits:
                    if c in applied:
                        star = "*"
                    else:
                        star = " "
                    print star, read_pipe(["git", "show", "-s",
                                           "--format=format:%h %s",  c])
                print "You will have to do 'git p4 sync' and rebase."

        if gitConfigBool("git-p4.exportLabels"):
            self.exportLabels = True

        if self.exportLabels:
            p4Labels = getP4Labels(self.depotPath)
            gitTags = getGitTags()

            missingGitTags = gitTags - p4Labels
            self.exportGitTags(missingGitTags)

        # exit with error unless everything applied perfectly
        if len(commits) != len(applied):
                sys.exit(1)

        return True

class View(object):
    """Represent a p4 view ("p4 help views"), and map files in a
       repo according to the view."""

    def __init__(self, client_name):
        self.mappings = []
        self.client_prefix = "//%s/" % client_name
        # cache results of "p4 where" to lookup client file locations
        self.client_spec_path_cache = {}

    def append(self, view_line):
        """Parse a view line, splitting it into depot and client
           sides.  Append to self.mappings, preserving order.  This
           is only needed for tag creation."""

        # Split the view line into exactly two words.  P4 enforces
        # structure on these lines that simplifies this quite a bit.
        #
        # Either or both words may be double-quoted.
        # Single quotes do not matter.
        # Double-quote marks cannot occur inside the words.
        # A + or - prefix is also inside the quotes.
        # There are no quotes unless they contain a space.
        # The line is already white-space stripped.
        # The two words are separated by a single space.
        #
        if view_line[0] == '"':
            # First word is double quoted.  Find its end.
            close_quote_index = view_line.find('"', 1)
            if close_quote_index <= 0:
                die("No first-word closing quote found: %s" % view_line)
            depot_side = view_line[1:close_quote_index]
            # skip closing quote and space
            rhs_index = close_quote_index + 1 + 1
        else:
            space_index = view_line.find(" ")
            if space_index <= 0:
                die("No word-splitting space found: %s" % view_line)
            depot_side = view_line[0:space_index]
            rhs_index = space_index + 1

        # prefix + means overlay on previous mapping
        if depot_side.startswith("+"):
            depot_side = depot_side[1:]

        # prefix - means exclude this path, leave out of mappings
        exclude = False
        if depot_side.startswith("-"):
            exclude = True
            depot_side = depot_side[1:]

        if not exclude:
            self.mappings.append(depot_side)

    def convert_client_path(self, clientFile):
        # chop off //client/ part to make it relative
        if not clientFile.startswith(self.client_prefix):
            die("No prefix '%s' on clientFile '%s'" %
                (self.client_prefix, clientFile))
        return clientFile[len(self.client_prefix):]

    def update_client_spec_path_cache(self, files):
        """ Caching file paths by "p4 where" batch query """

        # List depot file paths exclude that already cached
        fileArgs = [f['path'] for f in files if f['path'] not in self.client_spec_path_cache]

        if len(fileArgs) == 0:
            return  # All files in cache

        where_result = p4CmdList(["-x", "-", "where"], stdin=fileArgs)
        for res in where_result:
            if "code" in res and res["code"] == "error":
                # assume error is "... file(s) not in client view"
                continue
            if "clientFile" not in res:
                die("No clientFile in 'p4 where' output")
            if "unmap" in res:
                # it will list all of them, but only one not unmap-ped
                continue
            if gitConfigBool("core.ignorecase"):
                res['depotFile'] = res['depotFile'].lower()
            self.client_spec_path_cache[res['depotFile']] = self.convert_client_path(res["clientFile"])

        # not found files or unmap files set to ""
        for depotFile in fileArgs:
            if gitConfigBool("core.ignorecase"):
                depotFile = depotFile.lower()
            if depotFile not in self.client_spec_path_cache:
                self.client_spec_path_cache[depotFile] = ""

    def map_in_client(self, depot_path):
        """Return the relative location in the client where this
           depot file should live.  Returns "" if the file should
           not be mapped in the client."""

        if gitConfigBool("core.ignorecase"):
            depot_path = depot_path.lower()

        if depot_path in self.client_spec_path_cache:
            return self.client_spec_path_cache[depot_path]

        die( "Error: %s is not found in client spec path" % depot_path )
        return ""

class P4Sync(Command, P4UserMap):
    delete_actions = ( "delete", "move/delete", "purge" )

    def __init__(self):
        Command.__init__(self)
        P4UserMap.__init__(self)
        self.options = [
                optparse.make_option("--branch", dest="branch"),
                optparse.make_option("--detect-branches", dest="detectBranches", action="store_true"),
                optparse.make_option("--changesfile", dest="changesFile"),
                optparse.make_option("--silent", dest="silent", action="store_true"),
                optparse.make_option("--detect-labels", dest="detectLabels", action="store_true"),
                optparse.make_option("--import-labels", dest="importLabels", action="store_true"),
                optparse.make_option("--import-local", dest="importIntoRemotes", action="store_false",
                                     help="Import into refs/heads/ , not refs/remotes"),
                optparse.make_option("--max-changes", dest="maxChanges",
                                     help="Maximum number of changes to import"),
                optparse.make_option("--changes-block-size", dest="changes_block_size", type="int",
                                     help="Internal block size to use when iteratively calling p4 changes"),
                optparse.make_option("--keep-path", dest="keepRepoPath", action='store_true',
                                     help="Keep entire BRANCH/DIR/SUBDIR prefix during import"),
                optparse.make_option("--use-client-spec", dest="useClientSpec", action='store_true',
                                     help="Only sync files that are included in the Perforce Client Spec"),
                optparse.make_option("-/", dest="cloneExclude",
                                     action="append", type="string",
                                     help="exclude depot path"),
        ]
        self.description = """Imports from Perforce into a git repository.\n
    example:
    //depot/my/project/ -- to import the current head
    //depot/my/project/@all -- to import everything
    //depot/my/project/@1,6 -- to import only from revision 1 to 6

    (a ... is not needed in the path p4 specification, it's added implicitly)"""

        self.usage += " //depot/path[@revRange]"
        self.silent = False
        self.createdBranches = set()
        self.committedChanges = set()
        self.branch = ""
        self.detectBranches = False
        self.detectLabels = False
        self.importLabels = False
        self.changesFile = ""
        self.syncWithOrigin = True
        self.importIntoRemotes = True
        self.maxChanges = ""
        self.changes_block_size = None
        self.keepRepoPath = False
        self.depotPaths = None
        self.p4BranchesInGit = []
        self.cloneExclude = []
        self.useClientSpec = False
        self.useClientSpec_from_options = False
        self.clientSpecDirs = None
        self.tempBranches = []
        self.tempBranchLocation = "git-p4-tmp"
        self.largeFileSystem = None

        if gitConfig('git-p4.largeFileSystem'):
            largeFileSystemConstructor = globals()[gitConfig('git-p4.largeFileSystem')]
            self.largeFileSystem = largeFileSystemConstructor(
                lambda git_mode, relPath, contents: self.writeToGitStream(git_mode, relPath, contents)
            )

        if gitConfig("git-p4.syncFromOrigin") == "false":
            self.syncWithOrigin = False

    # This is required for the "append" cloneExclude action
    def ensure_value(self, attr, value):
        if not hasattr(self, attr) or getattr(self, attr) is None:
            setattr(self, attr, value)
        return getattr(self, attr)

    # Force a checkpoint in fast-import and wait for it to finish
    def checkpoint(self):
        self.gitStream.write("checkpoint\n\n")
        self.gitStream.write("progress checkpoint\n\n")
        out = self.gitOutput.readline()
        if self.verbose:
            print "checkpoint finished: " + out

    def extractFilesFromCommit(self, commit):
        self.cloneExclude = [re.sub(r"\.\.\.$", "", path)
                             for path in self.cloneExclude]
        files = []
        fnum = 0
        while commit.has_key("depotFile%s" % fnum):
            path =  commit["depotFile%s" % fnum]

            if [p for p in self.cloneExclude
                if p4PathStartsWith(path, p)]:
                found = False
            else:
                found = [p for p in self.depotPaths
                         if p4PathStartsWith(path, p)]
            if not found:
                fnum = fnum + 1
                continue

            file = {}
            file["path"] = path
            file["rev"] = commit["rev%s" % fnum]
            file["action"] = commit["action%s" % fnum]
            file["type"] = commit["type%s" % fnum]
            files.append(file)
            fnum = fnum + 1
        return files

    def extractJobsFromCommit(self, commit):
        jobs = []
        jnum = 0
        while commit.has_key("job%s" % jnum):
            job = commit["job%s" % jnum]
            jobs.append(job)
            jnum = jnum + 1
        return jobs

    def stripRepoPath(self, path, prefixes):
        """When streaming files, this is called to map a p4 depot path
           to where it should go in git.  The prefixes are either
           self.depotPaths, or self.branchPrefixes in the case of
           branch detection."""

        if self.useClientSpec:
            # branch detection moves files up a level (the branch name)
            # from what client spec interpretation gives
            path = self.clientSpecDirs.map_in_client(path)
            if self.detectBranches:
                for b in self.knownBranches:
                    if path.startswith(b + "/"):
                        path = path[len(b)+1:]

        elif self.keepRepoPath:
            # Preserve everything in relative path name except leading
            # //depot/; just look at first prefix as they all should
            # be in the same depot.
            depot = re.sub("^(//[^/]+/).*", r'\1', prefixes[0])
            if p4PathStartsWith(path, depot):
                path = path[len(depot):]

        else:
            for p in prefixes:
                if p4PathStartsWith(path, p):
                    path = path[len(p):]
                    break

        path = wildcard_decode(path)
        return path

    def splitFilesIntoBranches(self, commit):
        """Look at each depotFile in the commit to figure out to what
           branch it belongs."""

        if self.clientSpecDirs:
            files = self.extractFilesFromCommit(commit)
            self.clientSpecDirs.update_client_spec_path_cache(files)

        branches = {}
        fnum = 0
        while commit.has_key("depotFile%s" % fnum):
            path =  commit["depotFile%s" % fnum]
            found = [p for p in self.depotPaths
                     if p4PathStartsWith(path, p)]
            if not found:
                fnum = fnum + 1
                continue

            file = {}
            file["path"] = path
            file["rev"] = commit["rev%s" % fnum]
            file["action"] = commit["action%s" % fnum]
            file["type"] = commit["type%s" % fnum]
            fnum = fnum + 1

            # start with the full relative path where this file would
            # go in a p4 client
            if self.useClientSpec:
                relPath = self.clientSpecDirs.map_in_client(path)
            else:
                relPath = self.stripRepoPath(path, self.depotPaths)

            for branch in self.knownBranches.keys():
                # add a trailing slash so that a commit into qt/4.2foo
                # doesn't end up in qt/4.2, e.g.
                if relPath.startswith(branch + "/"):
                    if branch not in branches:
                        branches[branch] = []
                    branches[branch].append(file)
                    break

        return branches

    def writeToGitStream(self, gitMode, relPath, contents):
        self.gitStream.write('M %s inline %s\n' % (gitMode, relPath))
        self.gitStream.write('data %d\n' % sum(len(d) for d in contents))
        for d in contents:
            self.gitStream.write(d)
        self.gitStream.write('\n')

    # output one file from the P4 stream
    # - helper for streamP4Files

    def streamOneP4File(self, file, contents):
        relPath = self.stripRepoPath(file['depotFile'], self.branchPrefixes)
        if verbose:
            if 'fileSize' in self.stream_file:
                size = int(self.stream_file['fileSize'])
            elif 'streamContentSize' in self.stream_file:
                size = int(self.stream_file['streamContentSize'])
            else:
                size = 0
            sys.stdout.write('\r%s --> %s (%i MB)\n' % (file['depotFile'], relPath, size/1024/1024))
            sys.stdout.flush()

        (type_base, type_mods) = split_p4_type(file["type"])

        git_mode = "100644"
        if "x" in type_mods:
            git_mode = "100755"
        if type_base == "symlink":
            git_mode = "120000"
            # p4 print on a symlink sometimes contains "target\n";
            # if it does, remove the newline
            data = ''.join(contents)
            if not data:
                # Some version of p4 allowed creating a symlink that pointed
                # to nothing.  This causes p4 errors when checking out such
                # a change, and errors here too.  Work around it by ignoring
                # the bad symlink; hopefully a future change fixes it.
                print "\nIgnoring empty symlink in %s" % file['depotFile']
                return
            elif data[-1] == '\n':
                contents = [data[:-1]]
            else:
                contents = [data]

        if type_base == "utf16":
            # p4 delivers different text in the python output to -G
            # than it does when using "print -o", or normal p4 client
            # operations.  utf16 is converted to ascii or utf8, perhaps.
            # But ascii text saved as -t utf16 is completely mangled.
            # Invoke print -o to get the real contents.
            #
            # On windows, the newlines will always be mangled by print, so put
            # them back too.  This is not needed to the cygwin windows version,
            # just the native "NT" type.
            #
            try:
                text = p4_read_pipe(['print', '-q', '-o', '-', '%s@%s' % (file['depotFile'], file['change'])])
            except Exception as e:
                if 'Translation of file content failed' in str(e):
                    type_base = 'binary'
                else:
                    raise e
            else:
                if p4_version_string().find('/NT') >= 0:
                    text = text.replace('\r\n', '\n')
                contents = [ text ]

        if type_base == "apple":
            # Apple filetype files will be streamed as a concatenation of
            # its appledouble header and the contents.  This is useless
            # on both macs and non-macs.  If using "print -q -o xx", it
            # will create "xx" with the data, and "%xx" with the header.
            # This is also not very useful.
            #
            # Ideally, someday, this script can learn how to generate
            # appledouble files directly and import those to git, but
            # non-mac machines can never find a use for apple filetype.
            print "\nIgnoring apple filetype file %s" % file['depotFile']
            return

        # Note that we do not try to de-mangle keywords on utf16 files,
        # even though in theory somebody may want that.
        pattern = p4_keywords_regexp_for_type(type_base, type_mods)
        if pattern:
            regexp = re.compile(pattern, re.VERBOSE)
            text = ''.join(contents)
            text = regexp.sub(r'$\1$', text)
            contents = [ text ]

        try:
            relPath.decode('ascii')
        except:
            encoding = 'utf8'
            if gitConfig('git-p4.pathEncoding'):
                encoding = gitConfig('git-p4.pathEncoding')
            relPath = relPath.decode(encoding, 'replace').encode('utf8', 'replace')
            if self.verbose:
                print 'Path with non-ASCII characters detected. Used %s to encode: %s ' % (encoding, relPath)

        if self.largeFileSystem:
            (git_mode, contents) = self.largeFileSystem.processContent(git_mode, relPath, contents)

        self.writeToGitStream(git_mode, relPath, contents)

    def streamOneP4Deletion(self, file):
        relPath = self.stripRepoPath(file['path'], self.branchPrefixes)
        if verbose:
            sys.stdout.write("delete %s\n" % relPath)
            sys.stdout.flush()
        self.gitStream.write("D %s\n" % relPath)

        if self.largeFileSystem and self.largeFileSystem.isLargeFile(relPath):
            self.largeFileSystem.removeLargeFile(relPath)

    # handle another chunk of streaming data
    def streamP4FilesCb(self, marshalled):

        # catch p4 errors and complain
        err = None
        if "code" in marshalled:
            if marshalled["code"] == "error":
                if "data" in marshalled:
                    err = marshalled["data"].rstrip()

        if not err and 'fileSize' in self.stream_file:
            required_bytes = int((4 * int(self.stream_file["fileSize"])) - calcDiskFree())
            if required_bytes > 0:
                err = 'Not enough space left on %s! Free at least %i MB.' % (
                    os.getcwd(), required_bytes/1024/1024
                )

        if err:
            f = None
            if self.stream_have_file_info:
                if "depotFile" in self.stream_file:
                    f = self.stream_file["depotFile"]
            # force a failure in fast-import, else an empty
            # commit will be made
            self.gitStream.write("\n")
            self.gitStream.write("die-now\n")
            self.gitStream.close()
            # ignore errors, but make sure it exits first
            self.importProcess.wait()
            if f:
                die("Error from p4 print for %s: %s" % (f, err))
            else:
                die("Error from p4 print: %s" % err)

        if marshalled.has_key('depotFile') and self.stream_have_file_info:
            # start of a new file - output the old one first
            self.streamOneP4File(self.stream_file, self.stream_contents)
            self.stream_file = {}
            self.stream_contents = []
            self.stream_have_file_info = False

        # pick up the new file information... for the
        # 'data' field we need to append to our array
        for k in marshalled.keys():
            if k == 'data':
                if 'streamContentSize' not in self.stream_file:
                    self.stream_file['streamContentSize'] = 0
                self.stream_file['streamContentSize'] += len(marshalled['data'])
                self.stream_contents.append(marshalled['data'])
            else:
                self.stream_file[k] = marshalled[k]

        if (verbose and
            'streamContentSize' in self.stream_file and
            'fileSize' in self.stream_file and
            'depotFile' in self.stream_file):
            size = int(self.stream_file["fileSize"])
            if size > 0:
                progress = 100*self.stream_file['streamContentSize']/size
                sys.stdout.write('\r%s %d%% (%i MB)' % (self.stream_file['depotFile'], progress, int(size/1024/1024)))
                sys.stdout.flush()

        self.stream_have_file_info = True

    # Stream directly from "p4 files" into "git fast-import"
    def streamP4Files(self, files):
        filesForCommit = []
        filesToRead = []
        filesToDelete = []

        for f in files:
            filesForCommit.append(f)
            if f['action'] in self.delete_actions:
                filesToDelete.append(f)
            else:
                filesToRead.append(f)

        # deleted files...
        for f in filesToDelete:
            self.streamOneP4Deletion(f)

        if len(filesToRead) > 0:
            self.stream_file = {}
            self.stream_contents = []
            self.stream_have_file_info = False

            # curry self argument
            def streamP4FilesCbSelf(entry):
                self.streamP4FilesCb(entry)

            fileArgs = ['%s#%s' % (f['path'], f['rev']) for f in filesToRead]

            p4CmdList(["-x", "-", "print"],
                      stdin=fileArgs,
                      cb=streamP4FilesCbSelf)

            # do the last chunk
            if self.stream_file.has_key('depotFile'):
                self.streamOneP4File(self.stream_file, self.stream_contents)

    def make_email(self, userid):
        if userid in self.users:
            return self.users[userid]
        else:
            return "%s <a@b>" % userid

    def streamTag(self, gitStream, labelName, labelDetails, commit, epoch):
        """ Stream a p4 tag.
        commit is either a git commit, or a fast-import mark, ":<p4commit>"
        """

        if verbose:
            print "writing tag %s for commit %s" % (labelName, commit)
        gitStream.write("tag %s\n" % labelName)
        gitStream.write("from %s\n" % commit)

        if labelDetails.has_key('Owner'):
            owner = labelDetails["Owner"]
        else:
            owner = None

        # Try to use the owner of the p4 label, or failing that,
        # the current p4 user id.
        if owner:
            email = self.make_email(owner)
        else:
            email = self.make_email(self.p4UserId())
        tagger = "%s %s %s" % (email, epoch, self.tz)

        gitStream.write("tagger %s\n" % tagger)

        print "labelDetails=",labelDetails
        if labelDetails.has_key('Description'):
            description = labelDetails['Description']
        else:
            description = 'Label from git p4'

        gitStream.write("data %d\n" % len(description))
        gitStream.write(description)
        gitStream.write("\n")

    def inClientSpec(self, path):
        if not self.clientSpecDirs:
            return True
        inClientSpec = self.clientSpecDirs.map_in_client(path)
        if not inClientSpec and self.verbose:
            print('Ignoring file outside of client spec: {0}'.format(path))
        return inClientSpec

    def hasBranchPrefix(self, path):
        if not self.branchPrefixes:
            return True
        hasPrefix = [p for p in self.branchPrefixes
                        if p4PathStartsWith(path, p)]
        if not hasPrefix and self.verbose:
            print('Ignoring file outside of prefix: {0}'.format(path))
        return hasPrefix

    def commit(self, details, files, branch, parent = ""):
        epoch = details["time"]
        author = details["user"]
        jobs = self.extractJobsFromCommit(details)

        if self.verbose:
            print('commit into {0}'.format(branch))

        if self.clientSpecDirs:
            self.clientSpecDirs.update_client_spec_path_cache(files)

        files = [f for f in files
            if self.inClientSpec(f['path']) and self.hasBranchPrefix(f['path'])]

        if not files and not gitConfigBool('git-p4.keepEmptyCommits'):
            print('Ignoring revision {0} as it would produce an empty commit.'
                .format(details['change']))
            return

        self.gitStream.write("commit %s\n" % branch)
        self.gitStream.write("mark :%s\n" % details["change"])
        self.committedChanges.add(int(details["change"]))
        committer = ""
        if author not in self.users:
            self.getUserMapFromPerforceServer()
        committer = "%s %s %s" % (self.make_email(author), epoch, self.tz)

        self.gitStream.write("committer %s\n" % committer)

        self.gitStream.write("data <<EOT\n")
        self.gitStream.write(details["desc"])
        if len(jobs) > 0:
            self.gitStream.write("\nJobs: %s" % (' '.join(jobs)))
        self.gitStream.write("\n[git-p4: depot-paths = \"%s\": change = %s" %
                             (','.join(self.branchPrefixes), details["change"]))
        if len(details['options']) > 0:
            self.gitStream.write(": options = %s" % details['options'])
        self.gitStream.write("]\nEOT\n\n")

        if len(parent) > 0:
            if self.verbose:
                print "parent %s" % parent
            self.gitStream.write("from %s\n" % parent)

        self.streamP4Files(files)
        self.gitStream.write("\n")

        change = int(details["change"])

        if self.labels.has_key(change):
            label = self.labels[change]
            labelDetails = label[0]
            labelRevisions = label[1]
            if self.verbose:
                print "Change %s is labelled %s" % (change, labelDetails)

            files = p4CmdList(["files"] + ["%s...@%s" % (p, change)
                                                for p in self.branchPrefixes])

            if len(files) == len(labelRevisions):

                cleanedFiles = {}
                for info in files:
                    if info["action"] in self.delete_actions:
                        continue
                    cleanedFiles[info["depotFile"]] = info["rev"]

                if cleanedFiles == labelRevisions:
                    self.streamTag(self.gitStream, 'tag_%s' % labelDetails['label'], labelDetails, branch, epoch)

                else:
                    if not self.silent:
                        print ("Tag %s does not match with change %s: files do not match."
                               % (labelDetails["label"], change))

            else:
                if not self.silent:
                    print ("Tag %s does not match with change %s: file count is different."
                           % (labelDetails["label"], change))

    # Build a dictionary of changelists and labels, for "detect-labels" option.
    def getLabels(self):
        self.labels = {}

        l = p4CmdList(["labels"] + ["%s..." % p for p in self.depotPaths])
        if len(l) > 0 and not self.silent:
            print "Finding files belonging to labels in %s" % `self.depotPaths`

        for output in l:
            label = output["label"]
            revisions = {}
            newestChange = 0
            if self.verbose:
                print "Querying files for label %s" % label
            for file in p4CmdList(["files"] +
                                      ["%s...@%s" % (p, label)
                                          for p in self.depotPaths]):
                revisions[file["depotFile"]] = file["rev"]
                change = int(file["change"])
                if change > newestChange:
                    newestChange = change

            self.labels[newestChange] = [output, revisions]

        if self.verbose:
            print "Label changes: %s" % self.labels.keys()

    # Import p4 labels as git tags. A direct mapping does not
    # exist, so assume that if all the files are at the same revision
    # then we can use that, or it's something more complicated we should
    # just ignore.
    def importP4Labels(self, stream, p4Labels):
        if verbose:
            print "import p4 labels: " + ' '.join(p4Labels)

        ignoredP4Labels = gitConfigList("git-p4.ignoredP4Labels")
        validLabelRegexp = gitConfig("git-p4.labelImportRegexp")
        if len(validLabelRegexp) == 0:
            validLabelRegexp = defaultLabelRegexp
        m = re.compile(validLabelRegexp)

        for name in p4Labels:
            commitFound = False

            if not m.match(name):
                if verbose:
                    print "label %s does not match regexp %s" % (name,validLabelRegexp)
                continue

            if name in ignoredP4Labels:
                continue

            labelDetails = p4CmdList(['label', "-o", name])[0]

            # get the most recent changelist for each file in this label
            change = p4Cmd(["changes", "-m", "1"] + ["%s...@%s" % (p, name)
                                for p in self.depotPaths])

            if change.has_key('change'):
                # find the corresponding git commit; take the oldest commit
                changelist = int(change['change'])
                if changelist in self.committedChanges:
                    gitCommit = ":%d" % changelist       # use a fast-import mark
                    commitFound = True
                else:
                    gitCommit = read_pipe(["git", "rev-list", "--max-count=1",
                        "--reverse", ":/\[git-p4:.*change = %d\]" % changelist], ignore_error=True)
                    if len(gitCommit) == 0:
                        print "importing label %s: could not find git commit for changelist %d" % (name, changelist)
                    else:
                        commitFound = True
                        gitCommit = gitCommit.strip()

                if commitFound:
                    # Convert from p4 time format
                    try:
                        tmwhen = time.strptime(labelDetails['Update'], "%Y/%m/%d %H:%M:%S")
                    except ValueError:
                        print "Could not convert label time %s" % labelDetails['Update']
                        tmwhen = 1

                    when = int(time.mktime(tmwhen))
                    self.streamTag(stream, name, labelDetails, gitCommit, when)
                    if verbose:
                        print "p4 label %s mapped to git commit %s" % (name, gitCommit)
            else:
                if verbose:
                    print "Label %s has no changelists - possibly deleted?" % name

            if not commitFound:
                # We can't import this label; don't try again as it will get very
                # expensive repeatedly fetching all the files for labels that will
                # never be imported. If the label is moved in the future, the
                # ignore will need to be removed manually.
                system(["git", "config", "--add", "git-p4.ignoredP4Labels", name])

    def guessProjectName(self):
        for p in self.depotPaths:
            if p.endswith("/"):
                p = p[:-1]
            p = p[p.strip().rfind("/") + 1:]
            if not p.endswith("/"):
               p += "/"
            return p

    def getBranchMapping(self):
        lostAndFoundBranches = set()

        user = gitConfig("git-p4.branchUser")
        if len(user) > 0:
            command = "branches -u %s" % user
        else:
            command = "branches"

        for info in p4CmdList(command):
            details = p4Cmd(["branch", "-o", info["branch"]])
            viewIdx = 0
            while details.has_key("View%s" % viewIdx):
                paths = details["View%s" % viewIdx].split(" ")
                viewIdx = viewIdx + 1
                # require standard //depot/foo/... //depot/bar/... mapping
                if len(paths) != 2 or not paths[0].endswith("/...") or not paths[1].endswith("/..."):
                    continue
                source = paths[0]
                destination = paths[1]
                ## HACK
                if p4PathStartsWith(source, self.depotPaths[0]) and p4PathStartsWith(destination, self.depotPaths[0]):
                    source = source[len(self.depotPaths[0]):-4]
                    destination = destination[len(self.depotPaths[0]):-4]

                    if destination in self.knownBranches:
                        if not self.silent:
                            print "p4 branch %s defines a mapping from %s to %s" % (info["branch"], source, destination)
                            print "but there exists another mapping from %s to %s already!" % (self.knownBranches[destination], destination)
                        continue

                    self.knownBranches[destination] = source

                    lostAndFoundBranches.discard(destination)

                    if source not in self.knownBranches:
                        lostAndFoundBranches.add(source)

        # Perforce does not strictly require branches to be defined, so we also
        # check git config for a branch list.
        #
        # Example of branch definition in git config file:
        # [git-p4]
        #   branchList=main:branchA
        #   branchList=main:branchB
        #   branchList=branchA:branchC
        configBranches = gitConfigList("git-p4.branchList")
        for branch in configBranches:
            if branch:
                (source, destination) = branch.split(":")
                self.knownBranches[destination] = source

                lostAndFoundBranches.discard(destination)

                if source not in self.knownBranches:
                    lostAndFoundBranches.add(source)


        for branch in lostAndFoundBranches:
            self.knownBranches[branch] = branch

    def getBranchMappingFromGitBranches(self):
        branches = p4BranchesInGit(self.importIntoRemotes)
        for branch in branches.keys():
            if branch == "master":
                branch = "main"
            else:
                branch = branch[len(self.projectName):]
            self.knownBranches[branch] = branch

    def updateOptionDict(self, d):
        option_keys = {}
        if self.keepRepoPath:
            option_keys['keepRepoPath'] = 1

        d["options"] = ' '.join(sorted(option_keys.keys()))

    def readOptions(self, d):
        self.keepRepoPath = (d.has_key('options')
                             and ('keepRepoPath' in d['options']))

    def gitRefForBranch(self, branch):
        if branch == "main":
            return self.refPrefix + "master"

        if len(branch) <= 0:
            return branch

        return self.refPrefix + self.projectName + branch

    def gitCommitByP4Change(self, ref, change):
        if self.verbose:
            print "looking in ref " + ref + " for change %s using bisect..." % change

        earliestCommit = ""
        latestCommit = parseRevision(ref)

        while True:
            if self.verbose:
                print "trying: earliest %s latest %s" % (earliestCommit, latestCommit)
            next = read_pipe("git rev-list --bisect %s %s" % (latestCommit, earliestCommit)).strip()
            if len(next) == 0:
                if self.verbose:
                    print "argh"
                return ""
            log = extractLogMessageFromGitCommit(next)
            settings = extractSettingsGitLog(log)
            currentChange = int(settings['change'])
            if self.verbose:
                print "current change %s" % currentChange

            if currentChange == change:
                if self.verbose:
                    print "found %s" % next
                return next

            if currentChange < change:
                earliestCommit = "^%s" % next
            else:
                latestCommit = "%s" % next

        return ""

    def importNewBranch(self, branch, maxChange):
        # make fast-import flush all changes to disk and update the refs using the checkpoint
        # command so that we can try to find the branch parent in the git history
        self.gitStream.write("checkpoint\n\n");
        self.gitStream.flush();
        branchPrefix = self.depotPaths[0] + branch + "/"
        range = "@1,%s" % maxChange
        #print "prefix" + branchPrefix
        changes = p4ChangesForPaths([branchPrefix], range, self.changes_block_size)
        if len(changes) <= 0:
            return False
        firstChange = changes[0]
        #print "first change in branch: %s" % firstChange
        sourceBranch = self.knownBranches[branch]
        sourceDepotPath = self.depotPaths[0] + sourceBranch
        sourceRef = self.gitRefForBranch(sourceBranch)
        #print "source " + sourceBranch

        branchParentChange = int(p4Cmd(["changes", "-m", "1", "%s...@1,%s" % (sourceDepotPath, firstChange)])["change"])
        #print "branch parent: %s" % branchParentChange
        gitParent = self.gitCommitByP4Change(sourceRef, branchParentChange)
        if len(gitParent) > 0:
            self.initialParents[self.gitRefForBranch(branch)] = gitParent
            #print "parent git commit: %s" % gitParent

        self.importChanges(changes)
        return True

    def searchParent(self, parent, branch, target):
        parentFound = False
        for blob in read_pipe_lines(["git", "rev-list", "--reverse",
                                     "--no-merges", parent]):
            blob = blob.strip()
            if len(read_pipe(["git", "diff-tree", blob, target])) == 0:
                parentFound = True
                if self.verbose:
                    print "Found parent of %s in commit %s" % (branch, blob)
                break
        if parentFound:
            return blob
        else:
            return None

    def importChanges(self, changes):
        cnt = 1
        for change in changes:
            description = p4_describe(change)
            self.updateOptionDict(description)

            if not self.silent:
                sys.stdout.write("\rImporting revision %s (%s%%)" % (change, cnt * 100 / len(changes)))
                sys.stdout.flush()
            cnt = cnt + 1

            try:
                if self.detectBranches:
                    branches = self.splitFilesIntoBranches(description)
                    for branch in branches.keys():
                        ## HACK  --hwn
                        branchPrefix = self.depotPaths[0] + branch + "/"
                        self.branchPrefixes = [ branchPrefix ]

                        parent = ""

                        filesForCommit = branches[branch]

                        if self.verbose:
                            print "branch is %s" % branch

                        self.updatedBranches.add(branch)

                        if branch not in self.createdBranches:
                            self.createdBranches.add(branch)
                            parent = self.knownBranches[branch]
                            if parent == branch:
                                parent = ""
                            else:
                                fullBranch = self.projectName + branch
                                if fullBranch not in self.p4BranchesInGit:
                                    if not self.silent:
                                        print("\n    Importing new branch %s" % fullBranch);
                                    if self.importNewBranch(branch, change - 1):
                                        parent = ""
                                        self.p4BranchesInGit.append(fullBranch)
                                    if not self.silent:
                                        print("\n    Resuming with change %s" % change);

                                if self.verbose:
                                    print "parent determined through known branches: %s" % parent

                        branch = self.gitRefForBranch(branch)
                        parent = self.gitRefForBranch(parent)

                        if self.verbose:
                            print "looking for initial parent for %s; current parent is %s" % (branch, parent)

                        if len(parent) == 0 and branch in self.initialParents:
                            parent = self.initialParents[branch]
                            del self.initialParents[branch]

                        blob = None
                        if len(parent) > 0:
                            tempBranch = "%s/%d" % (self.tempBranchLocation, change)
                            if self.verbose:
                                print "Creating temporary branch: " + tempBranch
                            self.commit(description, filesForCommit, tempBranch)
                            self.tempBranches.append(tempBranch)
                            self.checkpoint()
                            blob = self.searchParent(parent, branch, tempBranch)
                        if blob:
                            self.commit(description, filesForCommit, branch, blob)
                        else:
                            if self.verbose:
                                print "Parent of %s not found. Committing into head of %s" % (branch, parent)
                            self.commit(description, filesForCommit, branch, parent)
                else:
                    files = self.extractFilesFromCommit(description)
                    self.commit(description, files, self.branch,
                                self.initialParent)
                    # only needed once, to connect to the previous commit
                    self.initialParent = ""
            except IOError:
                print self.gitError.read()
                sys.exit(1)

    def importHeadRevision(self, revision):
        print "Doing initial import of %s from revision %s into %s" % (' '.join(self.depotPaths), revision, self.branch)

        details = {}
        details["user"] = "git perforce import user"
        details["desc"] = ("Initial import of %s from the state at revision %s\n"
                           % (' '.join(self.depotPaths), revision))
        details["change"] = revision
        newestRevision = 0

        fileCnt = 0
        fileArgs = ["%s...%s" % (p,revision) for p in self.depotPaths]

        for info in p4CmdList(["files"] + fileArgs):

            if 'code' in info and info['code'] == 'error':
                sys.stderr.write("p4 returned an error: %s\n"
                                 % info['data'])
                if info['data'].find("must refer to client") >= 0:
                    sys.stderr.write("This particular p4 error is misleading.\n")
                    sys.stderr.write("Perhaps the depot path was misspelled.\n");
                    sys.stderr.write("Depot path:  %s\n" % " ".join(self.depotPaths))
                sys.exit(1)
            if 'p4ExitCode' in info:
                sys.stderr.write("p4 exitcode: %s\n" % info['p4ExitCode'])
                sys.exit(1)


            change = int(info["change"])
            if change > newestRevision:
                newestRevision = change

            if info["action"] in self.delete_actions:
                # don't increase the file cnt, otherwise details["depotFile123"] will have gaps!
                #fileCnt = fileCnt + 1
                continue

            for prop in ["depotFile", "rev", "action", "type" ]:
                details["%s%s" % (prop, fileCnt)] = info[prop]

            fileCnt = fileCnt + 1

        details["change"] = newestRevision

        # Use time from top-most change so that all git p4 clones of
        # the same p4 repo have the same commit SHA1s.
        res = p4_describe(newestRevision)
        details["time"] = res["time"]

        self.updateOptionDict(details)
        try:
            self.commit(details, self.extractFilesFromCommit(details), self.branch)
        except IOError:
            print "IO error with git fast-import. Is your git version recent enough?"
            print self.gitError.read()


    def run(self, args):
        self.depotPaths = []
        self.changeRange = ""
        self.previousDepotPaths = []
        self.hasOrigin = False

        # map from branch depot path to parent branch
        self.knownBranches = {}
        self.initialParents = {}

        if self.importIntoRemotes:
            self.refPrefix = "refs/remotes/p4/"
        else:
            self.refPrefix = "refs/heads/p4/"

        if self.syncWithOrigin:
            self.hasOrigin = originP4BranchesExist()
            if self.hasOrigin:
                if not self.silent:
                    print 'Syncing with origin first, using "git fetch origin"'
                system("git fetch origin")

        branch_arg_given = bool(self.branch)
        if len(self.branch) == 0:
            self.branch = self.refPrefix + "master"
            if gitBranchExists("refs/heads/p4") and self.importIntoRemotes:
                system("git update-ref %s refs/heads/p4" % self.branch)
                system("git branch -D p4")

        # accept either the command-line option, or the configuration variable
        if self.useClientSpec:
            # will use this after clone to set the variable
            self.useClientSpec_from_options = True
        else:
            if gitConfigBool("git-p4.useclientspec"):
                self.useClientSpec = True
        if self.useClientSpec:
            self.clientSpecDirs = getClientSpec()

        # TODO: should always look at previous commits,
        # merge with previous imports, if possible.
        if args == []:
            if self.hasOrigin:
                createOrUpdateBranchesFromOrigin(self.refPrefix, self.silent)

            # branches holds mapping from branch name to sha1
            branches = p4BranchesInGit(self.importIntoRemotes)

            # restrict to just this one, disabling detect-branches
            if branch_arg_given:
                short = self.branch.split("/")[-1]
                if short in branches:
                    self.p4BranchesInGit = [ short ]
            else:
                self.p4BranchesInGit = branches.keys()

            if len(self.p4BranchesInGit) > 1:
                if not self.silent:
                    print "Importing from/into multiple branches"
                self.detectBranches = True
                for branch in branches.keys():
                    self.initialParents[self.refPrefix + branch] = \
                        branches[branch]

            if self.verbose:
                print "branches: %s" % self.p4BranchesInGit

            p4Change = 0
            for branch in self.p4BranchesInGit:
                logMsg =  extractLogMessageFromGitCommit(self.refPrefix + branch)

                settings = extractSettingsGitLog(logMsg)

                self.readOptions(settings)
                if (settings.has_key('depot-paths')
                    and settings.has_key ('change')):
                    change = int(settings['change']) + 1
                    p4Change = max(p4Change, change)

                    depotPaths = sorted(settings['depot-paths'])
                    if self.previousDepotPaths == []:
                        self.previousDepotPaths = depotPaths
                    else:
                        paths = []
                        for (prev, cur) in zip(self.previousDepotPaths, depotPaths):
                            prev_list = prev.split("/")
                            cur_list = cur.split("/")
                            for i in range(0, min(len(cur_list), len(prev_list))):
                                if cur_list[i] <> prev_list[i]:
                                    i = i - 1
                                    break

                            paths.append ("/".join(cur_list[:i + 1]))

                        self.previousDepotPaths = paths

            if p4Change > 0:
                self.depotPaths = sorted(self.previousDepotPaths)
                self.changeRange = "@%s,#head" % p4Change
                if not self.silent and not self.detectBranches:
                    print "Performing incremental import into %s git branch" % self.branch

        # accept multiple ref name abbreviations:
        #    refs/foo/bar/branch -> use it exactly
        #    p4/branch -> prepend refs/remotes/ or refs/heads/
        #    branch -> prepend refs/remotes/p4/ or refs/heads/p4/
        if not self.branch.startswith("refs/"):
            if self.importIntoRemotes:
                prepend = "refs/remotes/"
            else:
                prepend = "refs/heads/"
            if not self.branch.startswith("p4/"):
                prepend += "p4/"
            self.branch = prepend + self.branch

        if len(args) == 0 and self.depotPaths:
            if not self.silent:
                print "Depot paths: %s" % ' '.join(self.depotPaths)
        else:
            if self.depotPaths and self.depotPaths != args:
                print ("previous import used depot path %s and now %s was specified. "
                       "This doesn't work!" % (' '.join (self.depotPaths),
                                               ' '.join (args)))
                sys.exit(1)

            self.depotPaths = sorted(args)

        revision = ""
        self.users = {}

        # Make sure no revision specifiers are used when --changesfile
        # is specified.
        bad_changesfile = False
        if len(self.changesFile) > 0:
            for p in self.depotPaths:
                if p.find("@") >= 0 or p.find("#") >= 0:
                    bad_changesfile = True
                    break
        if bad_changesfile:
            die("Option --changesfile is incompatible with revision specifiers")

        newPaths = []
        for p in self.depotPaths:
            if p.find("@") != -1:
                atIdx = p.index("@")
                self.changeRange = p[atIdx:]
                if self.changeRange == "@all":
                    self.changeRange = ""
                elif ',' not in self.changeRange:
                    revision = self.changeRange
                    self.changeRange = ""
                p = p[:atIdx]
            elif p.find("#") != -1:
                hashIdx = p.index("#")
                revision = p[hashIdx:]
                p = p[:hashIdx]
            elif self.previousDepotPaths == []:
                # pay attention to changesfile, if given, else import
                # the entire p4 tree at the head revision
                if len(self.changesFile) == 0:
                    revision = "#head"

            p = re.sub ("\.\.\.$", "", p)
            if not p.endswith("/"):
                p += "/"

            newPaths.append(p)

        self.depotPaths = newPaths

        # --detect-branches may change this for each branch
        self.branchPrefixes = self.depotPaths

        self.loadUserMapFromCache()
        self.labels = {}
        if self.detectLabels:
            self.getLabels();

        if self.detectBranches:
            ## FIXME - what's a P4 projectName ?
            self.projectName = self.guessProjectName()

            if self.hasOrigin:
                self.getBranchMappingFromGitBranches()
            else:
                self.getBranchMapping()
            if self.verbose:
                print "p4-git branches: %s" % self.p4BranchesInGit
                print "initial parents: %s" % self.initialParents
            for b in self.p4BranchesInGit:
                if b != "master":

                    ## FIXME
                    b = b[len(self.projectName):]
                self.createdBranches.add(b)

        self.tz = "%+03d%02d" % (- time.timezone / 3600, ((- time.timezone % 3600) / 60))

        self.importProcess = subprocess.Popen(["git", "fast-import"],
                                              stdin=subprocess.PIPE,
                                              stdout=subprocess.PIPE,
                                              stderr=subprocess.PIPE);
        self.gitOutput = self.importProcess.stdout
        self.gitStream = self.importProcess.stdin
        self.gitError = self.importProcess.stderr

        if revision:
            self.importHeadRevision(revision)
        else:
            changes = []

            if len(self.changesFile) > 0:
                output = open(self.changesFile).readlines()
                changeSet = set()
                for line in output:
                    changeSet.add(int(line))

                for change in changeSet:
                    changes.append(change)

                changes.sort()
            else:
                # catch "git p4 sync" with no new branches, in a repo that
                # does not have any existing p4 branches
                if len(args) == 0:
                    if not self.p4BranchesInGit:
                        die("No remote p4 branches.  Perhaps you never did \"git p4 clone\" in here.")

                    # The default branch is master, unless --branch is used to
                    # specify something else.  Make sure it exists, or complain
                    # nicely about how to use --branch.
                    if not self.detectBranches:
                        if not branch_exists(self.branch):
                            if branch_arg_given:
                                die("Error: branch %s does not exist." % self.branch)
                            else:
                                die("Error: no branch %s; perhaps specify one with --branch." %
                                    self.branch)

                if self.verbose:
                    print "Getting p4 changes for %s...%s" % (', '.join(self.depotPaths),
                                                              self.changeRange)
                changes = p4ChangesForPaths(self.depotPaths, self.changeRange, self.changes_block_size)

                if len(self.maxChanges) > 0:
                    changes = changes[:min(int(self.maxChanges), len(changes))]

            if len(changes) == 0:
                if not self.silent:
                    print "No changes to import!"
            else:
                if not self.silent and not self.detectBranches:
                    print "Import destination: %s" % self.branch

                self.updatedBranches = set()

                if not self.detectBranches:
                    if args:
                        # start a new branch
                        self.initialParent = ""
                    else:
                        # build on a previous revision
                        self.initialParent = parseRevision(self.branch)

                self.importChanges(changes)

                if not self.silent:
                    print ""
                    if len(self.updatedBranches) > 0:
                        sys.stdout.write("Updated branches: ")
                        for b in self.updatedBranches:
                            sys.stdout.write("%s " % b)
                        sys.stdout.write("\n")

        if gitConfigBool("git-p4.importLabels"):
            self.importLabels = True

        if self.importLabels:
            p4Labels = getP4Labels(self.depotPaths)
            gitTags = getGitTags()

            missingP4Labels = p4Labels - gitTags
            self.importP4Labels(self.gitStream, missingP4Labels)

        self.gitStream.close()
        if self.importProcess.wait() != 0:
            die("fast-import failed: %s" % self.gitError.read())
        self.gitOutput.close()
        self.gitError.close()

        # Cleanup temporary branches created during import
        if self.tempBranches != []:
            for branch in self.tempBranches:
                read_pipe("git update-ref -d %s" % branch)
            os.rmdir(os.path.join(os.environ.get("GIT_DIR", ".git"), self.tempBranchLocation))

        # Create a symbolic ref p4/HEAD pointing to p4/<branch> to allow
        # a convenient shortcut refname "p4".
        if self.importIntoRemotes:
            head_ref = self.refPrefix + "HEAD"
            if not gitBranchExists(head_ref) and gitBranchExists(self.branch):
                system(["git", "symbolic-ref", head_ref, self.branch])

        return True

class P4Rebase(Command):
    def __init__(self):
        Command.__init__(self)
        self.options = [
                optparse.make_option("--import-labels", dest="importLabels", action="store_true"),
        ]
        self.importLabels = False
        self.description = ("Fetches the latest revision from perforce and "
                            + "rebases the current work (branch) against it")

    def run(self, args):
        sync = P4Sync()
        sync.importLabels = self.importLabels
        sync.run([])

        return self.rebase()

    def rebase(self):
        if os.system("git update-index --refresh") != 0:
            die("Some files in your working directory are modified and different than what is in your index. You can use git update-index <filename> to bring the index up-to-date or stash away all your changes with git stash.");
        if len(read_pipe("git diff-index HEAD --")) > 0:
            die("You have uncommitted changes. Please commit them before rebasing or stash them away with git stash.");

        [upstream, settings] = findUpstreamBranchPoint()
        if len(upstream) == 0:
            die("Cannot find upstream branchpoint for rebase")

        # the branchpoint may be p4/foo~3, so strip off the parent
        upstream = re.sub("~[0-9]+$", "", upstream)

        print "Rebasing the current branch onto %s" % upstream
        oldHead = read_pipe("git rev-parse HEAD").strip()
        system("git rebase %s" % upstream)
        system("git diff-tree --stat --summary -M %s HEAD --" % oldHead)
        return True

class P4Clone(P4Sync):
    def __init__(self):
        P4Sync.__init__(self)
        self.description = "Creates a new git repository and imports from Perforce into it"
        self.usage = "usage: %prog [options] //depot/path[@revRange]"
        self.options += [
            optparse.make_option("--destination", dest="cloneDestination",
                                 action='store', default=None,
                                 help="where to leave result of the clone"),
            optparse.make_option("--bare", dest="cloneBare",
                                 action="store_true", default=False),
        ]
        self.cloneDestination = None
        self.needsGit = False
        self.cloneBare = False

    def defaultDestination(self, args):
        ## TODO: use common prefix of args?
        depotPath = args[0]
        depotDir = re.sub("(@[^@]*)$", "", depotPath)
        depotDir = re.sub("(#[^#]*)$", "", depotDir)
        depotDir = re.sub(r"\.\.\.$", "", depotDir)
        depotDir = re.sub(r"/$", "", depotDir)
        return os.path.split(depotDir)[1]

    def run(self, args):
        if len(args) < 1:
            return False

        if self.keepRepoPath and not self.cloneDestination:
            sys.stderr.write("Must specify destination for --keep-path\n")
            sys.exit(1)

        depotPaths = args

        if not self.cloneDestination and len(depotPaths) > 1:
            self.cloneDestination = depotPaths[-1]
            depotPaths = depotPaths[:-1]

        self.cloneExclude = ["/"+p for p in self.cloneExclude]
        for p in depotPaths:
            if not p.startswith("//"):
                sys.stderr.write('Depot paths must start with "//": %s\n' % p)
                return False

        if not self.cloneDestination:
            self.cloneDestination = self.defaultDestination(args)

        print "Importing from %s into %s" % (', '.join(depotPaths), self.cloneDestination)

        if not os.path.exists(self.cloneDestination):
            os.makedirs(self.cloneDestination)
        chdir(self.cloneDestination)

        init_cmd = [ "git", "init" ]
        if self.cloneBare:
            init_cmd.append("--bare")
        retcode = subprocess.call(init_cmd)
        if retcode:
            raise CalledProcessError(retcode, init_cmd)

        if not P4Sync.run(self, depotPaths):
            return False

        # create a master branch and check out a work tree
        if gitBranchExists(self.branch):
            system([ "git", "branch", "master", self.branch ])
            if not self.cloneBare:
                system([ "git", "checkout", "-f" ])
        else:
            print 'Not checking out any branch, use ' \
                  '"git checkout -q -b master <branch>"'

        # auto-set this variable if invoked with --use-client-spec
        if self.useClientSpec_from_options:
            system("git config --bool git-p4.useclientspec true")

        return True

class P4Branches(Command):
    def __init__(self):
        Command.__init__(self)
        self.options = [ ]
        self.description = ("Shows the git branches that hold imports and their "
                            + "corresponding perforce depot paths")
        self.verbose = False

    def run(self, args):
        if originP4BranchesExist():
            createOrUpdateBranchesFromOrigin()

        cmdline = "git rev-parse --symbolic "
        cmdline += " --remotes"

        for line in read_pipe_lines(cmdline):
            line = line.strip()

            if not line.startswith('p4/') or line == "p4/HEAD":
                continue
            branch = line

            log = extractLogMessageFromGitCommit("refs/remotes/%s" % branch)
            settings = extractSettingsGitLog(log)

            print "%s <= %s (%s)" % (branch, ",".join(settings["depot-paths"]), settings["change"])
        return True

class HelpFormatter(optparse.IndentedHelpFormatter):
    def __init__(self):
        optparse.IndentedHelpFormatter.__init__(self)

    def format_description(self, description):
        if description:
            return description + "\n"
        else:
            return ""

def printUsage(commands):
    print "usage: %s <command> [options]" % sys.argv[0]
    print ""
    print "valid commands: %s" % ", ".join(commands)
    print ""
    print "Try %s <command> --help for command specific help." % sys.argv[0]
    print ""

commands = {
    "debug" : P4Debug,
    "submit" : P4Submit,
    "commit" : P4Submit,
    "sync" : P4Sync,
    "rebase" : P4Rebase,
    "clone" : P4Clone,
    "rollback" : P4RollBack,
    "branches" : P4Branches
}


def main():
    if len(sys.argv[1:]) == 0:
        printUsage(commands.keys())
        sys.exit(2)

    cmdName = sys.argv[1]
    try:
        klass = commands[cmdName]
        cmd = klass()
    except KeyError:
        print "unknown command %s" % cmdName
        print ""
        printUsage(commands.keys())
        sys.exit(2)

    options = cmd.options
    cmd.gitdir = os.environ.get("GIT_DIR", None)

    args = sys.argv[2:]

    options.append(optparse.make_option("--verbose", "-v", dest="verbose", action="store_true"))
    if cmd.needsGit:
        options.append(optparse.make_option("--git-dir", dest="gitdir"))

    parser = optparse.OptionParser(cmd.usage.replace("%prog", "%prog " + cmdName),
                                   options,
                                   description = cmd.description,
                                   formatter = HelpFormatter())

    (cmd, args) = parser.parse_args(sys.argv[2:], cmd);
    global verbose
    verbose = cmd.verbose
    if cmd.needsGit:
        if cmd.gitdir == None:
            cmd.gitdir = os.path.abspath(".git")
            if not isValidGitDir(cmd.gitdir):
                cmd.gitdir = read_pipe("git rev-parse --git-dir").strip()
                if os.path.exists(cmd.gitdir):
                    cdup = read_pipe("git rev-parse --show-cdup").strip()
                    if len(cdup) > 0:
                        chdir(cdup);

        if not isValidGitDir(cmd.gitdir):
            if isValidGitDir(cmd.gitdir + "/.git"):
                cmd.gitdir += "/.git"
            else:
                die("fatal: cannot locate git repository at %s" % cmd.gitdir)

        os.environ["GIT_DIR"] = cmd.gitdir

    if not cmd.run(args):
        parser.print_help()
        sys.exit(2)


if __name__ == '__main__':
    main()
