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
"already" is asked before the step is shown (done: say so and skip it),
"verify" after Enter (not done: say so, offer to check again).  An item's
"skipIf" returns a reason to skip it, or None; "auto" returns (result, note)
to answer it without asking.  "expect" may be a function too.  The checklist's
"intro" is text or a list of steps; steps marked "again" are shown again on
resume (with their checks, so a console still open is not asked for twice).  In any text, "\n"
starts a new line, and a line that starts with spaces is shown as it is, so
commands can be listed one per line.  See qa_jog.py.
"""

import argparse
import datetime
import glob
import json
import os
import re
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

WIDTH = 78
ITEM_PROMPT = "How did it go? [p]ass / [f]ail / [s]kip / [n]ote / [b]ack / [q]uit or Esc: "
ITEM_HELP = ("p = pass, f = fail (asks what went wrong), s = skip (asks why), n = add a "
             "note first, b = back to the previous item, q or Esc = save and quit.")
# At the very first prompt there is nothing to go back to, so b isn't offered.
FIRST_ITEM_PROMPT = ITEM_PROMPT.replace("[b]ack / ", "")
FIRST_ITEM_HELP = ITEM_HELP.replace("b = back to the previous item, ", "")
SECTION_PROMPT = "Enter to start, [s]kip the whole section, [b]ack, [q]uit or Esc: "
SECTION_HELP = ("Press Enter to start the section, s to skip it, b to go back, q or Esc to "
                "quit.")
FIRST_SECTION_PROMPT = SECTION_PROMPT.replace("[b]ack, ", "")
FIRST_SECTION_HELP = SECTION_HELP.replace(" b to go back,", "")

CHECKLIST_KEYS = ("name", "title", "program", "purpose", "machine", "intro", "sections")
ITEM_KEYS = ("id", "title", "where", "steps", "do", "command", "expect", "needs", "safety",
             "skipIf", "auto")
STEP_KEYS = ("do", "run", "actions", "expect", "already", "verify", "again")
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
CONTINUE_PROMPT = BODY + "Press Enter when you're ready to continue (or q or Esc to quit). "
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
    for _, item in flatten(checklist):
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
# The interactive loop
# ---------------------------------------------------------------------------

class Runner:
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
        # Waits as long as it takes.  End of input and Ctrl-C mean quit.
        try:
            answer = (self.rawAsk or input)(prompt)
        except (EOFError, KeyboardInterrupt):
            self.out("")
            raise Quit()
        self.answered = True
        self.atTop = self.lastBlank = False   # the prompt and answer are on screen
        return answer

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
        input) it reads a line as usual."""
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
            for line in textwrap.wrap(paragraph, WIDTH - len(rest)):
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
                    self.out(textwrap.fill(line, WIDTH, initial_indent=start,
                                           subsequent_indent=indent) if line else "")

    # -- recording ----------------------------------------------------------

    def record(self, item, result, note):
        self.session["answers"][item["id"]] = {"result": result, "note": note.strip(),
                                               "answered": stamp(self.clock())}
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

    def sectionPrompt(self, index):
        """Returns the next index: index (start), past the section (skipped), or index-1 (back)."""
        self.freshScreen()
        section = self.items[index][0]
        number = self.checklist["sections"].index(section) + 1
        first, end = self.sectionRange(index)
        self.gap()
        self.out("=" * WIDTH)
        self.say(f"Section {number} of {len(self.checklist['sections'])}: {section['title']} "
                 f"({end - first} item{'' if end - first == 1 else 's'})")
        if section.get("intro"):
            self.say(self.resolve(section["intro"]), "  ")
        if section.get("needs"):
            self.field("Needs", self.resolve(section["needs"]))
        self.out("=" * WIDTH)
        prompt, help_ = (FIRST_SECTION_PROMPT, FIRST_SECTION_HELP) if index == 0 else \
            (SECTION_PROMPT, SECTION_HELP)
        while True:
            key, note = self.choose(prompt, SECTION_WORDS,
                                    ("s", "q") if index == 0 else ("s", "b", "q"))
            if key == "":
                return index
            if key == "q":
                raise Quit()
            if key == "b" and index > 0:
                return index - 1
            if key == "s":
                if not note:
                    default = section.get("needs", "")
                    hint = f" (Enter for \"section skipped: {default}\")" if default else \
                        " (Enter for none)"
                    note = self.ask(f"Why skip the section?{hint} ").strip()
                    note = note or (f"section skipped: {default}" if default else "section skipped")
                moment = stamp(self.clock())
                for _, item in self.items[first:end]:
                    if item["id"] not in self.session["answers"]:
                        self.session["answers"][item["id"]] = {"result": SKIP, "note": note,
                                                               "answered": moment}
                self.save()
                self.say(f"Skipped {section['title']}.")
                return end
            self.say(help_)

    def showItem(self, index):
        section, item = self.items[index]
        self.gap()
        self.out("-" * WIDTH)
        self.say(f"{item['title']}   ({section['title']})")
        self.field("Needs", self.resolve(item.get("needs")))
        self.field("Safety", self.resolve(item.get("safety")))
        if item.get("where"):
            self.say(item["where"], indent="  ")
        previous = self.session["answers"].get(item["id"])
        if previous:
            note = f" ({previous['note']})" if previous.get("note") else ""
            self.field("Answered", f"{previous['result']}{note}")

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
        for line in str(step.get("run") or "").split("\n"):
            if line:
                # A command is never wrapped, so it can be copied as it is.
                self.out(BODY + "    " + line)
        for number, action in enumerate(step.get("actions") or [], 1):
            self.out(textwrap.fill(self.resolve(action), WIDTH,
                                   initial_indent=f"{BODY}  {number:>2}. ",
                                   subsequent_indent=BODY + " " * 6))
        expect = self.resolve(step.get("expect"))
        if expect:
            self.say("You should see: " + expect, BODY)

    def runSteps(self, stepList, owner, enterAfterLast=False, only=None, header=None):
        """Show the steps one at a time, waiting for Enter between them.

        Every step starts with its number on a line of its own ("S12:", so it
        can be talked about), then an empty line.  owner is "intro" or the
        item's ID (see numberSteps).
        A step whose "already" check finds it done gets one line saying so, and
        is skipped.  After the last step there is no Enter (the answer prompt
        follows) unless it has a check or enterAfterLast is set.  only, if
        given, picks the steps to show (the rest keep their numbers).
        """
        total = len(stepList)
        for number, step in enumerate(stepList, 1):
            if only and not only(step):
                continue
            label = self.label(owner, number - 1)
            if step.get("already"):
                done, message = step["already"]()
                if done:
                    # It opens the next screen, so it stays in view.
                    if self.freshScreen() and header:
                        header()
                    self.gap()
                    self.say(f"{label} already done. {message}", HEAD, rest=BODY)
                    self.out("")   # a blank line after each report
                    continue
            if self.freshScreen() and header:
                header()   # the test's title and safety note, on every screen
            self.gap()
            self.say(label, HEAD, LABEL)
            self.out("")
            self.showStep(step)
            check = step.get("verify") or step.get("already")
            if number == total and not check and not enterAfterLast:
                break
            prompt = CONTINUE_PROMPT
            while True:
                answer = self.askKey(prompt, ("q", "c") if prompt == RECHECK_PROMPT else ("q",))
                if answer in ("q", "quit"):
                    raise Quit()
                if not check or (answer == "c" and prompt == RECHECK_PROMPT):
                    break
                done, message = check()
                self.say(message, BODY)
                if done:
                    break
                prompt = RECHECK_PROMPT

    def itemPrompt(self, index):
        """Ask until the item is answered.  Returns the next index."""
        _, item = self.items[index]
        self.freshScreen()
        self.showItem(index)
        if item["id"] not in self.session["answers"]:
            reason = item["skipIf"]() if item.get("skipIf") else None
            if reason:
                self.gap()
                self.say(f"{self.label(item['id'], 0)} not needed. {reason} I've recorded "
                         "this test as skipped.", HEAD, rest=BODY)
                self.record(item, SKIP, reason)
                return index + 1
            if item.get("auto"):
                result, note = item["auto"]()
                self.gap()
                done = {PASS: "passed", FAIL: "failed", SKIP: "skipped"}[result]
                self.say(f"{self.label(item['id'], 0)} checked for you. {note} I've recorded "
                         f"this test as {done}.", HEAD, rest=BODY)
                self.record(item, result, note)
                return index + 1
        self.runSteps(steps(item), item["id"], header=lambda: self.showItem(index))
        pending = ""
        prompt, help_ = (FIRST_ITEM_PROMPT, FIRST_ITEM_HELP) if index == 0 else \
            (ITEM_PROMPT, ITEM_HELP)
        while True:
            previous = self.session["answers"].get(item["id"])
            keep = f"(Enter keeps {previous['result']}) " if previous else ""
            key, inline = self.choose(BODY + keep + prompt, ITEM_WORDS,
                                      ("p", "f", "s", "n", "q") if index == 0 else None)
            if key == "":
                if previous:
                    return index + 1
                self.say(help_, BODY)
                continue
            if key == "q":
                raise Quit()
            if key == "b" and index > 0:
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

    def run(self):
        """Run the checklist.  Returns the session dict."""
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
            if index == 0 and not self.session["answers"]:
                program = self.checklist.get("program", "This program")
                self.out("")
                self.say(f"{program} will save your responses and any relevant data to "
                         "the file:", BODY)
                self.out("")
                self.out(f"{BODY}    {shortPath(self.basePath)}.md")
            if purpose:
                self.out("")
                self.say(STEP_NOTICE.format(label=self.label("opening", 0)[:-1]), BODY)
                self.out("")
                prompt = START_PROMPT if index == 0 and not self.session["answers"] \
                    else GO_ON_PROMPT
                if self.askKey(prompt) in ("q", "quit"):
                    raise Quit()
            intro = self.checklist.get("intro")
            if isinstance(intro, str):
                if index == 0:
                    self.say(intro, "  ")
            elif intro:
                # On resume, only the steps marked "again" (opening the second
                # console and so on); their checks skip what is still in place.
                self.runSteps(intro, "intro", enterAfterLast=True,
                              only=None if index == 0 else (lambda step: step.get("again")))
            entering = True  # moving forward: offer the section prompt at a section start
            while index < len(self.items):
                if entering and self.isSectionStart(index) \
                        and self.items[index][1]["id"] not in self.session["answers"]:
                    nextIndex = self.sectionPrompt(index)
                    if nextIndex != index:
                        entering = nextIndex > index
                        index = nextIndex
                        continue
                nextIndex = self.itemPrompt(index)
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


def main(checklist, argv=None):
    parser = argparse.ArgumentParser(description=f"Manual QA: {checklist['title']}")
    parser.add_argument("--machine", default=checklist.get("machine", ""),
                        help="machine profile name recorded in the results (default: %(default)s)")
    parser.add_argument("--results-dir", default=DEFAULT_RESULTS_DIR,
                        help="where results are saved (default: qa/results)")
    parser.add_argument("--new", action="store_true",
                        help="start a new session without offering to resume an unfinished one")
    arguments = parser.parse_args(argv)
    Runner(checklist, resultsDir=arguments.results_dir, machine=arguments.machine,
           offerResume=not arguments.new).run()
