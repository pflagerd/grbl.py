"""Evidence that the operator has done a QA step, gathered from this computer.

Linux only (it reads /proc and /etc/group).  Every check returns a pair
(done, message): done is True when there is evidence, and message says what
was found, in a sentence the runner can show the operator as it is.

Standard library only, so it runs on the system python3 as well as the venv.
"""

import grp
import os
import pwd
import subprocess

SHELLS = {"bash", "zsh", "fish", "sh", "dash", "ksh", "tcsh"}


def _statFields(pid, proc):
    """(comm, fields after the comm) from /proc/<pid>/stat, or None."""
    try:
        with open(os.path.join(proc, str(pid), "stat"), encoding="utf-8") as file:
            text = file.read()
    except OSError:
        return None
    return text[text.index("(") + 1:text.rindex(")")], text.rsplit(")", 1)[1].split()


def ownTty(proc="/proc"):
    """This process's controlling terminal (tty_nr), 0 when it has none."""
    found = _statFields("self", proc)
    return int(found[1][4]) if found else 0


def otherShells(proc="/proc", uid=None, tty=None):
    """Interactive shells of this user on a terminal other than this one.

    [{"pid", "comm", "cwd"}, ...].  A shell's cwd is where its console is.
    """
    uid = os.getuid() if uid is None else uid
    tty = ownTty(proc) if tty is None else tty
    shells = []
    for name in os.listdir(proc):
        if not name.isdigit():
            continue
        try:
            if os.stat(os.path.join(proc, name)).st_uid != uid:
                continue
            found = _statFields(name, proc)
            if not found:
                continue
            comm, fields = found
            terminal = int(fields[4])
            if comm not in SHELLS or terminal == 0 or terminal == tty:
                continue
            cwd = os.readlink(os.path.join(proc, name, "cwd"))
        except (OSError, ValueError, IndexError):
            continue
        shells.append({"pid": int(name), "comm": comm, "cwd": cwd})
    return shells


def secondConsole(proc="/proc"):
    if otherShells(proc):
        return True, "I can see a second console open."
    return False, "I can't see a second console yet."


def secondConsoleIn(directory, proc="/proc"):
    directory = os.path.realpath(directory)
    if any(os.path.realpath(shell["cwd"]) == directory for shell in otherShells(proc)):
        return True, f"I can see the second console in {directory}."
    return False, f"I can't see a second console in {directory} yet."


def userName():
    return pwd.getpwuid(os.getuid()).pw_name


def listedInGroup(group, user=None):
    """Is the user listed in /etc/group for group (takes effect at next login)?"""
    user = user or userName()
    try:
        entry = grp.getgrnam(group)
    except KeyError:
        return False
    return user in entry.gr_mem or pwd.getpwnam(user).pw_gid == entry.gr_gid


def inGroupNow(group):
    """Does this login session have the group (i.e. logged in since being added)?"""
    try:
        return grp.getgrnam(group).gr_gid in os.getgroups()
    except KeyError:
        return False


def canImport(python, module):
    """Can that python import the module?  False if the python isn't there."""
    try:
        return subprocess.run([python, "-c", f"import {module}"], capture_output=True,
                              timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


# What jog.py prints once it has connected and (with --home) homed: nothing
# before these two lines, which mean jog mode has started.
JOG_MODE_STARTS = (
    "Wait while it connects and homes the machine; it prints nothing meanwhile. Jog "
    "mode has started when it prints these two lines:\n"
    "    Jog mode: arrows X/Y, Shift+Up/Down Z. Tap to step 0.1 mm (Alt+tap 0.01 mm), "
    "hold to jog; hold Ctrl to creep (1/10 speed).\n"
    "    Enter accepts (after any queued steps finish), Esc aborts at once. The "
    "keyboard is captured until then.")


def consoleSteps(program, repo, shown=None):
    """The opening step both checklists share: a second console, in the
    repository root, with the venv active.  One step with both commands.  Its
    check can see the console and where it is, but not the venv, so the text
    asks for the venv either way.  Marked "again": on resume it is asked
    again, and its check says when the console is still in place."""
    def inRepo():
        return secondConsoleIn(repo)

    return [
        {"do": "If you haven't done so already, please open a second console window so "
               f"we can test {program} there, go to the repository root, and activate "
               "the venv (I can't check the venv from here, so please activate it if you "
               "haven't already):",
         "run": f"cd {shown or repo}\nsource .venv/bin/activate",
         "expect": "The prompt starts with (.venv).",
         "already": inRepo, "verify": inRepo, "again": True},
    ]


def gitClean(repo, path):
    """(clean, the changes git reports) for a path inside the repository."""
    try:
        completed = subprocess.run(["git", "status", "--short", "--", path], cwd=repo,
                                   capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False, "git couldn't be run."
    changes = completed.stdout.strip()
    return completed.returncode == 0 and not changes, changes
