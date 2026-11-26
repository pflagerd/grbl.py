#!/usr/bin/env python3
"""A guided manual-QA checklist for the console (issue #13, step 13).

Run a checklist (for example `python qa/qa_jog.py`) in one console while the
program under test runs in another.  For each item it shows one short step at
a time (what to do, the command to run in the other console, what you should
see) and waits for Enter before the next.  Where it can, it looks for evidence
that a step is done (a second console open, a module installed, a group
joined): a step that is already done is skipped, and an item that isn't
needed is recorded as skipped, with the reason.  After the last step it asks:

    [p]ass / [f]ail / [s]kip / [n]ote / [b]ack / [q]uit

    p           pass; "p some text" passes with a note
    f           fail; asks what went wrong ("f some text" gives it at once)
    s           skip; asks why (Enter for no note; "s some text" works too)
    n           add a note to this item without answering it yet
    b           back to the previous item, to answer it again
    q           save and quit; run the checklist again later to resume
    Enter       on an item answered before (after b): keep that answer

At the start of each section you can skip the whole section, for example the
probe checks when no probe plate is wired.

Every answer is saved at once to qa/results/<checklist>-<YYYY-MM-DD-HHMM>.md
(for people) and .json (for tools), so nothing is lost if the window closes.
Starting the same checklist again offers to resume an unfinished session.
End of input (Ctrl-D) and Ctrl-C save and quit too.  Nothing here talks to the
machine: it only asks and records.

Standard library only, so it runs on the system python3 as well as the venv.

A checklist is plain data: a dict with a name, a title, a default machine and
a list of sections, each with a title and a list of items.  Each item has an
id, a title, optionally where to work ("In the other console, ..."), a list of
steps, and optionally what it needs and a safety note.  Each step has what to
do, and optionally the command to run, a list of actions to do in one go (for
jog mode, where the keyboard is captured), and what to expect.  (An item may
instead give do, command and expect itself: that is one step.)

Checks are functions.  A step's "already" and "verify" return (done, message):
"already" is asked before the step is shown (done: say "Checked for you: ..."
and need no check; "whenDone" text instead, or no screen with "quietWhenDone"),
"verify" after Enter (not done: say so, offer to check again).  An item's
"skipIf" returns a reason to skip it, or None; "auto" returns (result, note)
to answer it without asking.  "expect" may be a function too.  The checklist's
"intro" is text or a list of steps; steps marked "again" are shown again on
resume (with their checks, so a console still open is not asked for twice).  In any text, "\n"
starts a new line, and a line that starts with spaces is shown as it is, so
commands can be listed one per line.  See qa_jog.py.

A checklist for the real machine is built for one machine: qa_jog.py and
qa_calibrate.py each have makeChecklist(machine), which takes the numbers
from that machine's profile, profiles/<machine>.json.  main() takes that
function, and --machine NAME picks the machine; without it, a menu of the
profiles asks (see chooseMachine).
"""

import argparse
import datetime
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap

PASS = "pass"
FAIL = "fail"
SKIP = "skip"
RESULTS = (PASS, FAIL, SKIP)

QA_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(QA_DIR)
DEFAULT_RESULTS_DIR = os.path.join(QA_DIR, "results")

WIDTH = 78   # used only when not on a real terminal (tests, piped output)
ITEM_PROMPT = "How did it go? [p]ass / [f]ail / [s]kip / [n]ote / [b]ack / [q]uit or Esc: "
ITEM_HELP = ("p = pass, f = fail (asks what went wrong), s = skip (asks why), n = add a "
             "note first, b = back to the previous item, q or Esc = save and quit.")
# At the very first prompt there is nothing to go back to, so b isn't offered.
FIRST_ITEM_PROMPT = ITEM_PROMPT.replace("[b]ack / ", "")
FIRST_ITEM_HELP = ITEM_HELP.replace("b = back to the previous item, ", "")
# Sections aren't shown to the operator.  A section with "needs" (a probe plate,
# say) gets one step of its own, so its tests can be skipped all at once.
NEEDS_PROMPT = ("Press Enter to do these tests, s to skip them all (b to go back, q or Esc "
                "to quit). ")
FIRST_NEEDS_PROMPT = "Press Enter to do these tests, s to skip them all (or q or Esc to quit). "

CHECKLIST_KEYS = ("name", "title", "program", "purpose", "machine", "intro", "sections")
ITEM_KEYS = ("id", "title", "where", "steps", "do", "command", "expect", "needs", "safety",
             "skipIf", "auto")
STEP_KEYS = ("do", "run", "actions", "expect", "already", "verify", "again", "whenDone",
             "quietWhenDone")
# A step's "whenDone": fields that replace its own on the screen when its
# "already" check finds it done (it then says so itself, so no "Checked for you").
WHEN_DONE_KEYS = ("do", "run", "actions", "expect")
# A step's "quietWhenDone": when its "already" check finds it done, the step
# is passed over with no screen at all (the test before has just said so).

# What every automatic check's result starts with on screen: a step's
# "already", an item's "skipIf" and an item's "auto".
CHECKED = "Checked for you: "
# Terminal styling: the VT100/ANSI subset every Linux terminal supports, and
# only on a real terminal (see useStyle), so piped output and tests stay plain.
CLEAR = "\033[H\033[2J"      # home and clear the screen (scrollback is kept)
# Steps and their numbers are plain text: some terminals (Konsole, with some
# colour schemes) draw even plain bold in a colour of their own.
STEP = ""                    # the step to do now
LABEL = ""                   # its number ("S12:")
PROMPT = ""                  # "Press Enter ..." prompts: plain too
RESET = "\033[0m"

HEAD = ""                    # where an "S12:" heading starts: the left edge
BODY = HEAD + "    "         # everything under it: 4 further in
CONTINUE_PROMPT = BODY + ("Press Enter when you're ready to continue (b to go back, q or Esc "
                          "to quit). ")
# Where there is nothing to go back to, b isn't offered.
FIRST_CONTINUE_PROMPT = BODY + "Press Enter when you're ready to continue (or q or Esc to quit). "
BACK = "back"   # runSteps / itemPrompt: the operator went back past the first step
RECHECK_PROMPT = BODY + "Press Enter to check again, c to carry on anyway, or q or Esc to quit. "
START_PROMPT = BODY + "Press Enter when you're ready to start (or q or Esc to quit). "
GO_ON_PROMPT = BODY + "Press Enter when you're ready to go on (or q or Esc to quit). "
ESC = "\x1b"            # readKey: Esc on its own
KEY_SEQUENCE = "\x00"   # readKey: an arrow or other multi-byte key (ignored)


ITEM_WORDS = {"p": "p", "pass": "p", "f": "f", "fail": "f", "s": "s", "skip": "s",
              "n": "n", "note": "n", "b": "b", "back": "b", "q": "q", "quit": "q"}
SECTION_WORDS = {"s": "s", "skip": "s", "b": "b", "back": "b", "q": "q", "quit": "q"}


def parseAnswer(answer, words):
    """'f the Z axis lagged' -> ('f', 'the Z axis lagged').  '' -> ('', '').

    The first word picks the action (a letter or the whole word, any case); the
    rest is a note.  An unknown first word gives ('?', '').
    """
    parts = answer.strip().split(None, 1)
    if not parts:
        return "", ""
    key = words.get(parts[0].lower())
    if key is None:
        return "?", ""
    return key, parts[1].strip() if len(parts) > 1 else ""


class Quit(Exception):
    """The operator asked to stop (q, end of input or Ctrl-C)."""


# ---------------------------------------------------------------------------
# Checklist data
# ---------------------------------------------------------------------------

def checklistProblems(checklist):
    """Everything wrong with a checklist's shape, as a list of strings (empty if fine)."""
    problems = []
    for key in ("name", "title", "sections"):
        if not checklist.get(key):
            problems.append(f"checklist has no {key}")
    seen = set()
    allIds = {item.get("id") for section in checklist.get("sections") or []
              for item in section.get("items") or []}
    for sectionNumber, section in enumerate(checklist.get("sections") or [], 1):
        where = f"section {sectionNumber}"
        if not section.get("title"):
            problems.append(f"{where} has no title")
        if not section.get("items"):
            problems.append(f"{where} has no items")
        if section.get("intro") and not section.get("needs"):
            problems.append(f"{where} has an intro, which isn't shown (sections aren't): "
                            "put it in its first step")
        for item in section.get("items") or []:
            itemId = item.get("id")
            if not itemId:
                problems.append(f"an item in {where} has no id")
                continue
            if itemId in seen:
                problems.append(f"duplicate id {itemId}")
            seen.add(itemId)
            if not str(item.get("title") or "").strip():
                problems.append(f"{itemId} has no title")
            if "steps" in item:
                if any(key in item for key in ("do", "command", "expect")):
                    problems.append(f"{itemId} has both steps and do/command/expect")
                if not item["steps"]:
                    problems.append(f"{itemId} has no steps")
                for number, step in enumerate(item["steps"] or [], 1):
                    if not str(step.get("do") or "").strip():
                        problems.append(f"{itemId} step {number} has no do")
                    unknownStep = set(step) - set(STEP_KEYS)
                    if unknownStep:
                        problems.append(f"{itemId} step {number} has unknown keys "
                                        f"{sorted(unknownStep)}")
                    if step.get("quietWhenDone") and not step.get("already"):
                        problems.append(f"{itemId} step {number} has quietWhenDone but no "
                                        f"already")
                    if "whenDone" in step:
                        whenDone = step["whenDone"]
                        if not isinstance(whenDone, dict):
                            problems.append(f"{itemId} step {number} whenDone isn't a dict")
                        elif not step.get("already"):
                            problems.append(f"{itemId} step {number} has whenDone but no "
                                            f"already")
                        elif set(whenDone) - set(WHEN_DONE_KEYS):
                            extra = sorted(set(whenDone) - set(WHEN_DONE_KEYS))
                            problems.append(f"{itemId} step {number} whenDone has unknown "
                                            f"keys {extra}")
            elif not item.get("expect") and not item.get("auto") and not item.get("skipIf"):
                problems.append(f"{itemId} has no expect")
            for value in [item.get("safety"), item.get("needs")] + [
                    step.get(key) for step in (item.get("steps") or [])
                    for key in ("do", "expect")] + [
                    action for step in (item.get("steps") or [])
                    for action in (step.get("actions") or [])]:
                if isinstance(value, str):
                    for match in REFERENCE.finditer(value):
                        if match.group(1) not in allIds:
                            problems.append(f"{itemId} refers to an unknown item "
                                            f"[[{match.group(1)}]]")
            unknown = set(item) - set(ITEM_KEYS)
            if unknown:
                problems.append(f"{itemId} has unknown keys {sorted(unknown)}")
    return problems


def steps(item):
    """The item's steps, each {"do", "run", "expect"}: its own list, or the one
    step made of its do, command and expect."""
    if "steps" in item:
        return item["steps"]
    if not item.get("do") and not item.get("expect"):
        return []   # answered by its "auto" check
    return [{"do": item.get("do"), "run": item.get("command"), "expect": item.get("expect")}]


def passedOver(step):
    """Is this a step with "quietWhenDone" whose check finds it done?"""
    return bool(step.get("quietWhenDone") and step["already"]()[0])


def keyboardIsTerminal():
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def readKey():
    """One key from the terminal, without waiting for Enter.  Keys pressed
    before the prompt (an extra Enter, say) are thrown away first.  Esc on its
    own gives ESC; an arrow or other key that sends Esc and more bytes at once
    gives KEY_SEQUENCE."""
    import select
    import termios
    import tty
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        termios.tcflush(fd, termios.TCIFLUSH)
        tty.setcbreak(fd)
        key = os.read(fd, 1).decode(errors="replace")
        if key == ESC and select.select([fd], [], [], 0.05)[0]:
            key = KEY_SEQUENCE   # more bytes straight after Esc: not Esc itself
        termios.tcflush(fd, termios.TCIFLUSH)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
    return key


def useStyle(stream=None):
    """Style output only on a real terminal that isn't "dumb", unless NO_COLOR is set."""
    stream = stream or sys.stdout
    try:
        tty = stream.isatty()
    except (AttributeError, ValueError):
        tty = False
    return tty and os.environ.get("TERM", "") not in ("", "dumb") and "NO_COLOR" not in os.environ


def shortPath(path):
    """The path with the home directory written as ~."""
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path == home or path.startswith(home + os.sep) else path


def text(value):
    """A text field's value: as it is, or called when it is a function."""
    return value() if callable(value) else value


# {label} is the opening step's label without its colon, e.g. "(qa_jog.py) S1".
STEP_NOTICE = ("Notice that this step is labelled {label}. S stands for step. Each step has a "
               "unique number. Please consider using this number when reporting issues.")
REFERENCE = re.compile(r"\[\[(\w+)\]\]")


def numberSteps(checklist):
    """Every step the operator sees gets a unique, stable number: S1 is the
    opening (when there is a purpose), then the intro steps, then each item's
    steps in order.  An item answered by its "auto" check counts as one step.
    Returns {(owner, index): number}, owner being "opening", "intro" or an
    item's ID.  Skipped steps keep their numbers, so they never shift."""
    numbers = {}
    count = 0
    if checklist.get("purpose"):
        count += 1
        numbers[("opening", 0)] = count
    intro = checklist.get("intro")
    if isinstance(intro, list):
        for index in range(len(intro)):
            count += 1
            numbers[("intro", index)] = count
    for sectionIndex, section in enumerate(checklist["sections"]):
        if section.get("needs"):
            count += 1                       # its "do these, or skip them all?" step
            numbers[("needs", sectionIndex)] = count
        for item in section["items"]:
            for index in range(max(1, len(steps(item)))):
                count += 1
                numbers[(item["id"], index)] = count
    return numbers


def stepRange(numbers, itemId):
    """An item's steps as the operator knows them: "S12" or "S12-S14"."""
    found = sorted(number for (owner, _), number in numbers.items() if owner == itemId)
    if not found:
        return itemId
    return f"S{found[0]}" if len(found) == 1 else f"S{found[0]}-S{found[-1]}"


def resolve(numbers, value):
    """Text with each [[ITEM]] replaced by that item's step numbers."""
    if not isinstance(value, str):
        return value
    return REFERENCE.sub(lambda match: stepRange(numbers, match.group(1)), value)


def flatten(checklist):
    """[(section, item), ...] in order."""
    return [(section, item) for section in checklist["sections"] for item in section["items"]]


# ---------------------------------------------------------------------------
# Session state and files
# ---------------------------------------------------------------------------

def gitCommit(repoDir=REPO_DIR):
    try:
        completed = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=repoDir,
                                   capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return completed.stdout.strip() if completed.returncode == 0 and completed.stdout.strip() \
        else "unknown"


def stamp(moment):
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def newSession(checklist, machine, commit, moment):
    return {
        "checklist": checklist["name"],
        "title": checklist["title"],
        "machine": machine,
        "commit": commit,
        "started": stamp(moment),
        "updated": stamp(moment),
        "finished": False,
        "answers": {},   # id -> {"result": ..., "note": ..., "answered": ...}
    }


def newBasePath(resultsDir, checklistName, moment):
    base = os.path.join(resultsDir, f"{checklistName}-{moment:%Y-%m-%d-%H%M}")
    path, number = base, 2
    while os.path.exists(path + ".json") or os.path.exists(path + ".md"):
        path = f"{base}-{number}"
        number += 1
    return path


def findUnfinished(resultsDir, checklistName):
    """(basePath, session) of the most recently saved unfinished session, or None."""
    latest = None
    for path in glob.glob(os.path.join(resultsDir, f"{checklistName}-*.json")):
        try:
            with open(path, encoding="utf-8") as file:
                session = json.load(file)
        except (OSError, ValueError):
            continue
        if session.get("checklist") != checklistName or session.get("finished"):
            continue
        key = (session.get("updated", ""), path)
        if latest is None or key > latest[0]:
            latest = (key, path[:-len(".json")], session)
    return None if latest is None else (latest[1], latest[2])


def writeAtomically(path, text):
    # Write a temporary file next to the target, then rename it over the target:
    # a crash leaves either the old file or the new one, never half of one.
    directory = os.path.dirname(path) or "."
    descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".part")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


def counts(checklist, session):
    tally = {PASS: 0, FAIL: 0, SKIP: 0, None: 0}
    for _, item in flatten(checklist):
        tally[session["answers"].get(item["id"], {}).get("result")] += 1
    return tally


def markdownCell(text):
    return str(text or "").replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").strip()


def toMarkdown(checklist, session):
    tally = counts(checklist, session)
    total = sum(tally.values())
    status = "finished" if session["finished"] else \
        f"unfinished ({total - tally[None]} of {total} answered)"
    lines = [
        f"# QA: {session['title']}",
        "",
        f"- Checklist: `{session['checklist']}`",
        f"- Machine profile: {session['machine']}",
        f"- Git commit: `{session['commit']}`",
        f"- Started: {session['started']}",
        f"- Last saved: {session['updated']}",
        f"- Status: {status}",
        "",
        "## Summary",
        "",
        f"Pass {tally[PASS]}, fail {tally[FAIL]}, skip {tally[SKIP]}, not answered {tally[None]}.",
        "",
    ]
    numbers = numberSteps(checklist)
    failed = [(item, session["answers"][item["id"]]) for _, item in flatten(checklist)
              if session["answers"].get(item["id"], {}).get("result") == FAIL]
    if failed:
        lines += ["### Failed", ""]
        lines += [f"- **{stepRange(numbers, item['id'])}** ({item['id']}) {item['title']}: "
                  f"{markdownCell(answer.get('note'))}" for item, answer in failed]
        lines.append("")
    for section in checklist["sections"]:
        lines += [f"## {section['title']}", "", "| Steps | ID | Check | Result | Note |",
                  "|---|---|---|---|---|"]
        for item in section["items"]:
            answer = session["answers"].get(item["id"], {})
            lines.append(f"| {stepRange(numbers, item['id'])} | {item['id']} | "
                         f"{markdownCell(item['title'])} | "
                         f"{answer.get('result') or '—'} | {markdownCell(answer.get('note'))} |")
        lines.append("")
    return "\n".join(lines)


def toJson(checklist, session):
    data = dict(session)
    numbers = numberSteps(checklist)
    data["items"] = [{"id": item["id"], "steps": stepRange(numbers, item["id"]),
                      "section": section["title"], "title": item["title"],
                      "result": session["answers"].get(item["id"], {}).get("result"),
                      "note": session["answers"].get(item["id"], {}).get("note", ""),
                      "answered": session["answers"].get(item["id"], {}).get("answered")}
                     for section, item in flatten(checklist)]
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def save(checklist, session, basePath, moment):
    session["updated"] = stamp(moment)
    os.makedirs(os.path.dirname(basePath) or ".", exist_ok=True)
    writeAtomically(basePath + ".json", toJson(checklist, session))
    writeAtomically(basePath + ".md", toMarkdown(checklist, session))


def summaryLines(checklist, session):
    tally = counts(checklist, session)
    lines = [f"Pass {tally[PASS]}, fail {tally[FAIL]}, skip {tally[SKIP]}"
             + (f", not answered {tally[None]}" if tally[None] else "") + "."]
    failed = [item for _, item in flatten(checklist)
              if session["answers"].get(item["id"], {}).get("result") == FAIL]
    if failed:
        lines.append("Failed:")
        for item in failed:
            note = session["answers"][item["id"]].get("note") or "(no note)"
            lines.append(f"  {stepRange(numberSteps(checklist), item['id'])}  "
                         f"{item['title']}: {note}")
    return lines


# ---------------------------------------------------------------------------
# Machines: which profile a checklist is for
# ---------------------------------------------------------------------------

PROFILES_DIR = os.path.join(REPO_DIR, "profiles")
# Files in profiles/ that sit beside a profile but aren't one.
SIDECAR_SUFFIXES = (".machine.json", ".zlimit.json")
AXES = ("X", "Y", "Z")
AXIS_BITS = {"X": 1, "Y": 2, "Z": 4}   # $23: a set bit homes that axis toward negative
NEW_MACHINE = "New machine (no profile yet)"


def readProfile(machine, profilesDir=PROFILES_DIR):
    """profiles/<machine>.json as a dict, or None if there is no such profile."""
    try:
        with open(os.path.join(profilesDir, f"{machine}.json"), encoding="utf-8") as file:
            profile = json.load(file)
    except (OSError, ValueError):
        return None
    return profile if isinstance(profile, dict) else None


def profileNames(profilesDir=PROFILES_DIR):
    """The machines that have a profile in profiles/, sorted.  A profile is
    <name>.json written by calibrate.py; sidecars (<name>.machine.json,
    <name>.zlimit.json) and other files there (bits.json) are left out."""
    names = []
    for path in glob.glob(os.path.join(profilesDir, "*.json")):
        filename = os.path.basename(path)
        if filename.endswith(SIDECAR_SUFFIXES):
            continue
        name = filename[:-len(".json")]
        profile = readProfile(name, profilesDir)
        if profile is not None and "workAreaX" in profile:
            names.append(name)
    return sorted(names)


def homeMPos(profile):
    """Where the machine sits after homing, {"X": mm, ...}, from the profile.

    Stock Grbl 1.1h (HOMING_FORCE_SET_ORIGIN off) ends homing at
        -max travel + pull-off   on an axis that homes toward negative ($23 bit set)
        -pull-off                on an axis that homes toward positive ($23 bit clear)
    (jog.py's softLimitBounds has the same rule).  Max travel is workArea*
    ($130-$132), the pull-off is pullOff ($27) and $23 is homingPosition."""
    mask = int(float(profile.get("homingPosition", "3")))
    pullOff = float(profile.get("pullOff", 1.0))
    return {axis: (-float(profile[f"workArea{axis}"]) + pullOff
                   if mask & AXIS_BITS[axis] else -pullOff) for axis in AXES}


def awayFromHome(profile):
    """{"X": +1 or -1, ...}: the direction that moves an axis away from its
    home switch (+1 on an axis that homes toward negative)."""
    mask = int(float(profile.get("homingPosition", "3")))
    return {axis: 1 if mask & AXIS_BITS[axis] else -1 for axis in AXES}


def mposText(position):
    """{"X": -397, "Y": -297, "Z": -3} as Grbl-style "X-397.000 Y-297.000 Z-3.000"."""
    return " ".join(f"{axis}{position[axis]:.3f}" for axis in AXES)


def chooseMachine(names, offerNew=False, ask=input, out=print):
    """Ask which machine this run is for: a numbered menu of the profiles, plus
    a new machine when offerNew.  Returns its name, or None if the operator
    quits (q, Esc, end of input or Ctrl-C) or there is nothing to choose."""
    choices = list(names) + ([NEW_MACHINE] if offerNew else [])
    if not choices:
        return None
    out("")
    out("Which machine is this for? " + ("These have a profile in profiles/:" if names else
                                         "No machine has a profile in profiles/ yet."))
    out("")
    for number, choice in enumerate(choices, 1):
        out(f"    {number}  {choice}")
    try:
        while True:
            out("")
            numbers = "1" if len(choices) == 1 else f"1-{len(choices)}"
            answer = ask(f"Type its number ({numbers}), or q to quit: ").strip()
            if answer.lower() in ("q", "quit", ESC):
                return None
            if not (answer.isdigit() and 1 <= int(answer) <= len(choices)):
                out(f"Please type a number from 1 to {len(choices)}.")
                continue
            choice = choices[int(answer) - 1]
            if choice != NEW_MACHINE:
                return choice
            while True:
                out("")
                name = ask("The new machine's name (calibrate.py saves its profile as "
                           "profiles/<name>.json): ").strip()
                if name.lower() in ("q", "quit", ESC):
                    return None
                if name and os.sep not in name and not name.startswith("."):
                    return name
                out("Please type a name, without a / and not starting with a dot.")
    except (EOFError, KeyboardInterrupt):
        out("")
        return None


def recordedNote(itemId):
    """The note recorded so far for item itemId in the session being run, or
    "" (for checklist text that shows an earlier answer, such as qa_calibrate's
    C12 showing the settings noted in P2)."""
    runner = Runner.current
    if runner is None or runner.session is None:
        return ""
    return (runner.session.get("answers", {}).get(itemId) or {}).get("note") or ""


# ---------------------------------------------------------------------------
# The interactive loop
# ---------------------------------------------------------------------------

class Runner:
    current = None   # the Runner whose run() is in progress (see recordedNote)

    def __init__(self, checklist, resultsDir=DEFAULT_RESULTS_DIR, machine=None, commit=None,
                 ask=None, out=print, clock=datetime.datetime.now, offerResume=True,
                 styled=None):
        problems = checklistProblems(checklist)
        if problems:
            raise ValueError("Bad checklist: " + "; ".join(problems))
        self.checklist = checklist
        self.items = flatten(checklist)
        self.numbers = numberSteps(checklist)
        self.resultsDir = resultsDir
        self.machine = machine or checklist.get("machine", "")
        self.commit = commit
        self.rawAsk = ask
        self.rawOut = out
        self.atTop = True       # nothing shown since the screen was cleared
        self.lastBlank = False  # the last line shown was empty
        # Styled only when printing to a real terminal (or when a test says so).
        self.styled = (out is print and useStyle()) if styled is None else styled
        # Each step gets a fresh screen, but only once the operator has
        # answered something on the current one (so "already done" lines and
        # skipped tests stay in view on the next step's screen).
        self.answered = False
        self.clock = clock
        self.offerResume = offerResume
        self.session = None
        self.basePath = None

    # -- input ------------------------------------------------------------

    def ask(self, prompt):
        # Waits as long as it takes.  End of input and Ctrl-C mean quit.  Every
        # prompt has one empty line above it (gap() never doubles it).
        self.gap()
        try:
            answer = (self.rawAsk or input)(prompt)
        except (EOFError, KeyboardInterrupt):
            self.out("")
            raise Quit()
        self.answered = True
        self.atTop = self.lastBlank = False   # the prompt and answer are on screen
        return answer

    def width(self):
        """Columns to wrap to: the terminal's full width, asked for each time (so
        resizing the window takes effect), or WIDTH when not on a terminal."""
        if not self.styled:
            return WIDTH
        return max(40, shutil.get_terminal_size((WIDTH, 24)).columns)

    def out(self, line=""):
        self.rawOut(line)
        self.atTop = False
        self.lastBlank = line == ""

    def gap(self):
        """A blank line between blocks: never at the top of a fresh screen, and
        never two in a row."""
        if not (self.atTop or self.lastBlank):
            self.out("")

    def clearScreen(self):
        if self.styled:
            if self.rawOut is print:
                sys.stdout.write(CLEAR)
                sys.stdout.flush()
            else:
                self.rawOut(CLEAR)
            self.atTop = True

    def freshScreen(self):
        """Start a new screen if the operator has answered something on this
        one.  Returns True if the screen was cleared."""
        if not (self.styled and self.answered):
            return False
        self.clearScreen()
        self.answered = False
        return True

    def style(self, text, code):
        """text in that style on a terminal; as it is otherwise."""
        return f"{code}{text}{RESET}" if self.styled and text and code else text

    def choose(self, prompt, words, allowed=None):
        """An answer prompt: (key, inline note).  On a real terminal it is one
        key (no Enter; Esc is q) from allowed (default: every key in words),
        and notes are asked for separately.  Elsewhere a typed line, so
        "f the Z axis lagged" still works."""
        if self.rawAsk is None and keyboardIsTerminal():
            keys = tuple(allowed or sorted(set(words.values())))
            return self.askKey(prompt, keys, highlight=False), ""
        return parseAnswer(self.ask(prompt), words)

    def askKey(self, prompt, keys=("q",), highlight=True):
        """A "Press Enter ..." prompt.  On a real terminal it reacts to one key,
        no Enter needed: Enter gives "", and any of keys gives that key (other
        keys are ignored).  Esc gives "q"; Ctrl-C and Ctrl-D quit.  Elsewhere (tests, piped
        input) it reads a line as usual.

        Like every prompt, it has an empty line above it; gap() adds one only
        if there isn't one already."""
        self.gap()
        shown = self.promptStyle(prompt) if highlight else prompt
        if self.rawAsk is not None or not keyboardIsTerminal():
            return self.ask(shown).strip().lower()
        sys.stdout.write(shown.replace("\001", "").replace("\002", ""))
        sys.stdout.flush()
        self.answered = True
        self.atTop = self.lastBlank = False
        while True:
            try:
                key = readKey()
            except KeyboardInterrupt:
                key = "\x03"
            if key in ("\x03", "\x04", ""):
                sys.stdout.write("\n")
                raise Quit()
            if key in ("\r", "\n"):
                sys.stdout.write("\n")
                return ""
            if key == ESC:
                sys.stdout.write("Esc\n")
                return "q"
            if key.lower() in keys:
                sys.stdout.write(key + "\n")
                return key.lower()

    def promptStyle(self, prompt):
        """A styled input() prompt.  The leading indent stays plain, and with
        readline loaded the codes are wrapped in \\001...\\002 so it can still
        count the prompt's width."""
        if not self.styled or not PROMPT:
            return prompt
        body = prompt.lstrip(" ")
        indent = prompt[:len(prompt) - len(body)]
        start, end = (f"\001{PROMPT}\002", f"\001{RESET}\002") if "readline" in sys.modules \
            else (PROMPT, RESET)
        return f"{indent}{start}{body.rstrip()}{end} "

    def say(self, text="", indent="", code=None, rest=None):
        """Wrapped text at indent.  rest, if given, indents every line after the
        first (an "S12: ..." line keeps its wrapped lines under the body)."""
        if not text:
            self.out("")
            return
        rest = indent if rest is None else rest
        lead = indent
        for paragraph in str(text).split("\n"):
            if not paragraph:
                self.out("")
                continue
            # Style each wrapped line after wrapping: textwrap counts escape
            # codes as characters.  The indent stays unstyled.
            for line in textwrap.wrap(paragraph, self.width() - len(rest)):
                self.out(lead + (self.style(line, code) if code else line))
                lead = rest

    def field(self, label, text, margin="  ", wrap=True):
        """One labelled field.  Each line of text ("\\n") starts a new line under
        the label; a line that starts with spaces is shown as it is (a command)."""
        if text:
            prefix = f"{margin}{label + ':':<7} "
            indent = " " * len(prefix)
            for number, line in enumerate(str(text).split("\n")):
                start = indent if number else prefix
                if line[:1].isspace() or not wrap:
                    self.out(start + line)
                else:
                    self.out(textwrap.fill(line, self.width(), initial_indent=start,
                                           subsequent_indent=indent) if line else "")

    # -- recording ----------------------------------------------------------

    def record(self, item, result, note, by=None):
        """by="check": answered by the item's skipIf / auto check, not the operator."""
        answer = {"result": result, "note": note.strip(), "answered": stamp(self.clock())}
        if by:
            answer["by"] = by
        self.session["answers"][item["id"]] = answer
        self.save()

    def save(self):
        save(self.checklist, self.session, self.basePath, self.clock())

    # -- flow -------------------------------------------------------------

    def start(self):
        """Pick up an unfinished session or begin a new one.  Returns the starting index."""
        if self.offerResume:
            found = findUnfinished(self.resultsDir, self.checklist["name"])
            if found:
                basePath, session = found
                tally = counts(self.checklist, session)
                total = sum(tally.values())
                self.out("")
                self.say(f"An unfinished '{self.checklist['name']}' session was found: "
                         f"{os.path.basename(basePath)}.md, {total - tally[None]} of {total} "
                         f"answered, last saved {session.get('updated')}.", BODY)
                if not self.ask(BODY + "Resume it? [Y/n] ").strip().lower().startswith("n"):
                    session.setdefault("answers", {})
                    self.session, self.basePath = session, basePath
                    self.say("Resuming. Your responses are saved to the file:", BODY)
                    self.out("")
                    self.out(f"{BODY}    {shortPath(self.basePath)}.md")
                    unanswered = [i for i, (_, item) in enumerate(self.items)
                                  if item["id"] not in session["answers"]]
                    return unanswered[0] if unanswered else len(self.items)
        moment = self.clock()
        commit = self.commit if self.commit is not None else gitCommit()
        self.session = newSession(self.checklist, self.machine, commit, moment)
        self.basePath = newBasePath(self.resultsDir, self.checklist["name"], moment)
        self.save()
        return 0

    def isSectionStart(self, index):
        return index == 0 or self.items[index][0] is not self.items[index - 1][0]

    def sectionRange(self, index):
        section = self.items[index][0]
        indexes = [i for i, (s, _) in enumerate(self.items) if s is section]
        return indexes[0], indexes[-1] + 1

    def needsPrompt(self, index):
        """The step before a section whose tests need something (a probe plate):
        do them, or skip them all.  Returns index (do them), the index past the
        section (skipped), or index - 1 (back)."""
        self.freshScreen()
        section = self.items[index][0]
        sectionIndex = self.checklist["sections"].index(section)
        first, end = self.sectionRange(index)
        self.gap()
        self.say(self.label("needs", sectionIndex), HEAD, LABEL)
        self.out("")
        if section.get("intro"):
            self.say(self.resolve(section["intro"]), BODY)
            self.out("")
        count = end - first
        self.say(f"The next {count} test{'' if count == 1 else 's'} need"
                 f"{'s' if count == 1 else ''}: {self.resolve(section['needs'])}", BODY)
        self.out("")
        back = index > 0 or self.hasOpening()
        while True:
            key = self.askKey(BODY + (NEEDS_PROMPT if back else FIRST_NEEDS_PROMPT),
                              ("s", "b", "q") if back else ("s", "q"))
            if key == "":
                return index
            if key in ("q", "quit"):
                raise Quit()
            if key in ("b", "back") and back:
                return index - 1
            if key in ("s", "skip"):
                default = f"skipped: needs {section['needs']}"
                note = self.ask(BODY + f"Why skip them? (Enter for \"{default}\") ").strip()
                note = note or default
                moment = stamp(self.clock())
                for _, item in self.items[first:end]:
                    if item["id"] not in self.session["answers"]:
                        self.session["answers"][item["id"]] = {"result": SKIP, "note": note,
                                                               "answered": moment}
                self.save()
                self.say(f"Skipped those {count} tests.", BODY)
                return end

    def showItem(self, index):
        """A test's notes, on every screen of it: Needs, Safety, where, and what
        it was answered last time.  They go under the step's "S12:" label and its
        empty line, at BODY like the step text (nothing is ever above the
        label), and the caller leaves a gap after them.  No separator line and
        no title: the operator goes by the S number, and the title is kept for
        the results files."""
        _, item = self.items[index]
        self.field("Needs", self.resolve(item.get("needs")), margin=BODY)
        self.field("Safety", self.resolve(item.get("safety")), margin=BODY)
        if item.get("where"):
            self.say(item["where"], indent=BODY)
        previous = self.session["answers"].get(item["id"])
        # What the operator answered last time (not a check's own answer: its
        # screen says what it found).
        if previous and previous.get("by") != "check":
            note = f" ({previous['note']})" if previous.get("note") else ""
            self.field("Answered", f"{previous['result']}{note}", margin=BODY)

    def resolve(self, value):
        return resolve(self.numbers, text(value))

    def label(self, owner, index):
        """A step's on-screen label, e.g. "(qa_jog.py) S12:", so the operator can
        tell one checklist's steps from another's; plain "S12:" with no program."""
        step = f"S{self.numbers[(owner, index)]}:"
        program = self.checklist.get("program")
        return f"({program}) {step}" if program else step

    def showStep(self, step, label=""):
        if step.get("do"):
            self.say(label + self.resolve(step["do"]), BODY, STEP)
        commands = [line for line in str(step.get("run") or "").split("\n") if line]
        if commands:
            # Rule: an empty line before and after every shell command snippet.
            # A command is never wrapped, so it can be copied as it is.
            self.gap()
            for line in commands:
                self.out(BODY + "    " + line)
            self.out("")
        for number, action in enumerate(step.get("actions") or [], 1):
            # An action's first line is wrapped after its number; lines that
            # start with spaces are quoted program output, shown as a block of
            # their own, further in, with an empty line before and after.
            first, *quoted = self.resolve(action).split("\n")
            self.out(textwrap.fill(first, self.width(), initial_indent=f"{BODY}  {number:>2}. ",
                                   subsequent_indent=BODY + " " * 6))
            if quoted:
                self.gap()
                for line in quoted:
                    # A hanging indent, so a wrapped line still reads as one.
                    self.out(textwrap.fill(line.strip(), self.width(),
                                           initial_indent=BODY + " " * 10,
                                           subsequent_indent=BODY + " " * 12))
                self.out("")
        expect = self.resolve(step.get("expect"))
        if expect:
            self.say("You should see: " + expect, BODY)

    def runSteps(self, stepList, owner, enterAfterLast=False, only=None, header=None,
                 start=0, history=(), canGoBack=True):
        """Show the steps one at a time, waiting for Enter between them.

        Every step starts with its number on a line of its own ("S12:", so it
        can be talked about), then an empty line.  owner is "intro" or the
        item's ID (see numberSteps).
        A step whose "already" check finds it done is still shown on a screen
        of its own, with "Checked for you: ..." above its text, and Enter moves
        on without checking again.  If the step has "whenDone", its fields
        replace the step's own on that screen instead, with no "Checked for
        you" line.  If it has "quietWhenDone", it gets no screen at all: it is
        passed over, going forward or back, and never asks for Enter.
        After the last step there is no Enter (the answer prompt follows)
        unless it has a check or enterAfterLast is set.
        only, if given, picks the steps to show (the rest keep their numbers).

        b at a step's prompt goes back to the step shown before it.  start and
        history pick up part-way
        (history: the steps shown before start, in order).  From the first
        step, b returns BACK if canGoBack, so the caller can go further back.
        Otherwise returns the indexes of the steps shown, in order; the last
        is the one on screen.
        """
        total = len(stepList)
        history = list(history)
        index = start
        first = True   # the first screen of this call shows header even if not fresh
        goingBack = False
        while index < total:
            step = stepList[index]
            if only and not only(step):
                index += 1
                continue
            label = self.label(owner, index)
            done, message = step["already"]() if step.get("already") else (False, "")
            if done and step.get("quietWhenDone"):   # see passedOver
                # Passed over: on b, on to the step before it instead.
                if goingBack and history:
                    index = history.pop()
                elif goingBack and canGoBack:
                    return BACK
                else:
                    index += 1
                continue
            goingBack = False
            fresh = self.freshScreen()
            self.gap()
            self.say(label, HEAD, LABEL)
            self.out("")
            if header and (fresh or first):
                header()   # the test's Needs and Safety notes, under the label
                self.gap()
            first = False
            if done and step.get("whenDone"):
                self.showStep({**step, **step["whenDone"]})
            else:
                if done:
                    self.say(CHECKED + message, BODY)
                    self.out("")
                self.showStep(step)
            # A step already done needs no check after Enter.
            check = None if done else (step.get("verify") or step.get("already"))
            if index == total - 1 and not check and not enterAfterLast:
                history.append(index)
                return history
            back = bool(history) or canGoBack
            prompt = CONTINUE_PROMPT if back else FIRST_CONTINUE_PROMPT
            wentBack = False
            while True:
                if prompt == RECHECK_PROMPT:
                    keys = ("q", "c")
                else:
                    keys = ("q", "b") if back else ("q",)
                answer = self.askKey(prompt, keys)
                if answer in ("q", "quit"):
                    raise Quit()
                if answer in ("b", "back") and back and prompt != RECHECK_PROMPT:
                    if not history:
                        return BACK
                    index = history.pop()
                    wentBack = goingBack = True
                    break
                if not check or (answer == "c" and prompt == RECHECK_PROMPT):
                    break
                done, message = check()
                self.say(message, BODY)
                if done:
                    break
                prompt = RECHECK_PROMPT
            if wentBack:
                continue
            history.append(index)
            index += 1
        return history

    def hasOpening(self):
        """Is there anything before the first test to go back to?"""
        return bool(self.checklist.get("purpose") or isinstance(self.checklist.get("intro"), list))

    def itemPrompt(self, index, fromEnd=False):
        """Ask until the item is answered.  Returns the next index (index - 1
        to go back; -1 means back into the opening steps).  fromEnd: coming
        back from the next test, so start at this test's last step."""
        _, item = self.items[index]
        self.freshScreen()
        previous = self.session["answers"].get(item["id"])
        # A test the operator hasn't answered (or one its own check answered
        # before) is checked again; if the check settles it, it gets a screen of
        # its own saying so, and Enter moves on.
        if previous is None or previous.get("by") == "check":
            checked = None
            reason = item["skipIf"]() if item.get("skipIf") else None
            if reason:
                checked = (SKIP, reason)
            elif item.get("auto"):
                checked = item["auto"]()
            if checked:
                result, note = checked
                done = {PASS: "passed", FAIL: "failed", SKIP: "skipped"}[result]
                self.gap()
                self.say(self.label(item["id"], 0), HEAD, LABEL)
                self.out("")
                self.showItem(index)
                self.gap()
                self.say(CHECKED + note, BODY)
                self.out("")
                self.say(f"I've recorded this test as {done}.", BODY)
                self.out("")
                self.record(item, result, note, by="check")
                back = index > 0 or self.hasOpening()
                key = self.askKey(CONTINUE_PROMPT if back else FIRST_CONTINUE_PROMPT,
                                  ("q", "b") if back else ("q",))
                if key in ("q", "quit"):
                    raise Quit()
                if key in ("b", "back") and back:
                    return index - 1
                return index + 1
        stepList = steps(item)
        canGoBack = index > 0 or self.hasOpening()

        def show(start=0, history=()):
            return self.runSteps(stepList, item["id"], header=lambda: self.showItem(index),
                                 start=start, history=history, canGoBack=canGoBack)
        if fromEnd and stepList:
            # Steps passed over (quietWhenDone) were never shown, so b skips them.
            shown = show(len(stepList) - 1, [i for i in range(len(stepList) - 1)
                                             if not passedOver(stepList[i])])
        elif stepList:
            shown = show()
        else:
            # No steps (one answered by its check before): the label and notes.
            self.gap()
            self.say(self.label(item["id"], 0), HEAD, LABEL)
            self.out("")
            self.showItem(index)
            self.gap()
            shown = []
        if shown == BACK:
            return index - 1
        pending = ""
        while True:
            # b goes back a step within this test, or to the test before.
            backOK = len(shown) >= 2 or canGoBack
            prompt, help_ = (ITEM_PROMPT, ITEM_HELP) if backOK else \
                (FIRST_ITEM_PROMPT, FIRST_ITEM_HELP)
            previous = self.session["answers"].get(item["id"])
            keep = f"(Enter keeps {previous['result']}) " if previous else ""
            key, inline = self.choose(BODY + keep + prompt, ITEM_WORDS,
                                      None if backOK else ("p", "f", "s", "n", "q"))
            if key == "":
                if previous:
                    return index + 1
                self.say(help_, BODY)
                continue
            if key == "q":
                raise Quit()
            if key == "b" and backOK:
                if len(shown) >= 2:
                    shown = show(shown[-2], shown[:-2])
                    if shown == BACK:
                        return index - 1
                    continue
                return index - 1
            if key == "n":
                note = inline or self.ask(BODY + "Note: ").strip()
                if previous:
                    previous["note"] = note
                    self.save()
                    self.say("Note saved.", BODY)
                else:
                    pending = note
                    self.say("Note kept; now answer p, f or s.", BODY)
                continue
            if key == "p":
                self.record(item, PASS, inline or pending)
                return index + 1
            if key == "f":
                note = inline or pending
                while not note:
                    note = self.ask(BODY + "What went wrong? (a short note) ").strip()
                self.record(item, FAIL, note)
                return index + 1
            if key == "s":
                note = inline or pending or self.ask(BODY + "Why skip? (Enter for none) ").strip()
                self.record(item, SKIP, note)
                return index + 1
            self.say(help_, BODY)

    def openingRest(self):
        """The rest of S1, after the purpose: the results file (new sessions),
        the note about step numbers, and the prompt.  S1 has no back."""
        if self.isNew:
            program = self.checklist.get("program", "This program")
            self.out("")
            self.say(f"{program} will save your responses and any relevant data to "
                     "the file:", BODY)
            self.out("")
            self.out(f"{BODY}    {shortPath(self.basePath)}.md")
        if self.checklist.get("purpose"):
            self.out("")
            self.say(STEP_NOTICE.format(label=self.label("opening", 0)[:-1]), BODY)
            self.out("")
            if self.askKey(START_PROMPT if self.isNew else GO_ON_PROMPT) in ("q", "quit"):
                raise Quit()

    def introSteps(self, fromEnd=False):
        """The intro steps after S1.  On resume, only those marked "again" (the
        second console and so on; their checks skip what is still in place).
        Returns BACK if the operator went back to S1."""
        intro = self.checklist.get("intro")
        if isinstance(intro, str):
            if self.firstIndex == 0 and not fromEnd:
                self.say(intro, "  ")
            return BACK if fromEnd and self.checklist.get("purpose") else []
        if not intro:
            return BACK if fromEnd and self.checklist.get("purpose") else []
        only = None if self.firstIndex == 0 else (lambda step: step.get("again"))
        shown = [i for i, step in enumerate(intro) if not only or only(step)]
        if not shown:
            return BACK if fromEnd and self.checklist.get("purpose") else []
        start, history = (shown[-1], shown[:-1]) if fromEnd else (0, ())
        return self.runSteps(intro, "intro", enterAfterLast=True, only=only, start=start,
                             history=history, canGoBack=bool(self.checklist.get("purpose")))

    def introAndBack(self, fromEnd=False):
        """The intro steps; back from the first of them shows S1 again."""
        while self.introSteps(fromEnd) == BACK:
            self.freshScreen()
            self.say(self.label("opening", 0), HEAD, LABEL)
            self.out("")
            self.say(self.checklist["purpose"], BODY)
            self.openingRest()
            fromEnd = False

    def run(self):
        """Run the checklist.  Returns the session dict."""
        Runner.current = self
        try:
            # A fresh screen to start with; after that, each step clears the
            # screen once the operator has answered the one before (see
            # freshScreen).
            self.clearScreen()
            # S1, the very first text: what this program is for.
            purpose = self.checklist.get("purpose")
            if purpose:
                self.say(self.label("opening", 0), HEAD, LABEL)
                self.out("")
                self.say(purpose, BODY)
            index = self.start()
            self.isNew = index == 0 and not self.session["answers"]
            self.firstIndex = index
            self.openingRest()
            self.introAndBack()
            entering = True  # moving forward: offer the section prompt at a section start
            while index < len(self.items):
                if index < 0:
                    # Back from the first test: into the opening steps.
                    self.introAndBack(fromEnd=True)
                    index, entering = 0, True
                    continue
                if entering and self.isSectionStart(index) \
                        and self.items[index][0].get("needs") \
                        and self.items[index][1]["id"] not in self.session["answers"]:
                    nextIndex = self.needsPrompt(index)
                    if nextIndex != index:
                        entering = nextIndex > index
                        index = nextIndex
                        continue
                nextIndex = self.itemPrompt(index, fromEnd=not entering)
                entering = nextIndex > index
                index = nextIndex
            self.session["finished"] = True
            self.save()
            self.out("")
            self.say("All items answered.")
        except Quit:
            if self.session is None:
                return None
            self.save()
            self.say("Saved. Run it again to resume.")
        self.out("")
        for line in summaryLines(self.checklist, self.session):
            self.say(line)
        self.say("Results:")
        self.out(f"  {self.basePath}.md (and .json)")
        return self.session


def main(checklist, argv=None, title=None, offerNew=False, ask=input, out=print,
         profilesDir=PROFILES_DIR):
    """Run a checklist from the command line.

    checklist is a checklist dict, or a function that builds one for a machine
    name.  With a function, --machine NAME picks the machine, and without it a
    menu of the profiles in profiles/ asks (offerNew adds a new machine to the
    menu); title is then the checklist's title, for --help.  The function may
    refuse a machine with sys.exit("why")."""
    build = None if isinstance(checklist, dict) else checklist
    title = checklist["title"] if build is None else title or "checklist"
    parser = argparse.ArgumentParser(description=f"Manual QA: {title}")
    if build is None:
        parser.add_argument("--machine", default=checklist.get("machine", ""),
                            help="machine profile name recorded in the results "
                                 "(default: %(default)s)")
    else:
        parser.add_argument("--machine", metavar="NAME",
                            help="the machine, as in profiles/NAME.json (default: choose "
                                 "from a menu of the profiles)")
    parser.add_argument("--results-dir", default=DEFAULT_RESULTS_DIR,
                        help="where results are saved (default: qa/results)")
    parser.add_argument("--new", action="store_true",
                        help="start a new session without offering to resume an unfinished one")
    arguments = parser.parse_args(argv)
    if build is not None:
        machine = arguments.machine
        if not machine:
            names = profileNames(profilesDir)
            if not names and not offerNew:
                sys.exit(f"No machine has a profile in {shortPath(profilesDir)} yet. Please "
                         "calibrate it first: python calibrate.py \"NAME\" (or python3 "
                         "qa/qa_calibrate.py).")
            machine = chooseMachine(names, offerNew=offerNew, ask=ask, out=out)
            if machine is None:
                sys.exit("No machine chosen; nothing was started.")
        checklist = build(machine)
        arguments.machine = machine
    Runner(checklist, resultsDir=arguments.results_dir, machine=arguments.machine,
           offerResume=not arguments.new).run()
