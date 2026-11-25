#!/usr/bin/env python3
"""A guided manual-QA checklist for the console (issue #13, step 13).

Run a checklist (for example `python qa/qa_jog.py`) in one console while the
program under test runs in another.  For each item it shows what to do, the
command to run in the other console, and what you should see, then asks:

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
id, a title, what to do, optionally the command to run, what to expect, and
optionally what it needs and a safety note.  See qa_jog.py.
"""

import argparse
import datetime
import glob
import json
import os
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
ITEM_PROMPT = "[p]ass / [f]ail / [s]kip / [n]ote / [b]ack / [q]uit: "
ITEM_HELP = ("p = pass (\"p text\" adds a note), f = fail, s = skip, n = add a note, "
             "b = back to the previous item, q = save and quit.")

ITEM_KEYS = ("id", "title", "do", "command", "expect", "needs", "safety")


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
            for key in ("title", "expect"):
                if not str(item.get(key) or "").strip():
                    problems.append(f"{itemId} has no {key}")
            unknown = set(item) - set(ITEM_KEYS)
            if unknown:
                problems.append(f"{itemId} has unknown keys {sorted(unknown)}")
    return problems


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
    failed = [(item, session["answers"][item["id"]]) for _, item in flatten(checklist)
              if session["answers"].get(item["id"], {}).get("result") == FAIL]
    if failed:
        lines += ["### Failed", ""]
        lines += [f"- **{item['id']}** {item['title']}: {markdownCell(answer.get('note'))}"
                  for item, answer in failed]
        lines.append("")
    for section in checklist["sections"]:
        lines += [f"## {section['title']}", "", "| ID | Check | Result | Note |", "|---|---|---|---|"]
        for item in section["items"]:
            answer = session["answers"].get(item["id"], {})
            lines.append(f"| {item['id']} | {markdownCell(item['title'])} | "
                         f"{answer.get('result') or '—'} | {markdownCell(answer.get('note'))} |")
        lines.append("")
    return "\n".join(lines)


def toJson(checklist, session):
    data = dict(session)
    data["items"] = [{"id": item["id"], "section": section["title"], "title": item["title"],
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
            lines.append(f"  {item['id']}  {item['title']}: {note}")
    return lines


# ---------------------------------------------------------------------------
# The interactive loop
# ---------------------------------------------------------------------------

class Runner:
    def __init__(self, checklist, resultsDir=DEFAULT_RESULTS_DIR, machine=None, commit=None,
                 ask=None, out=print, clock=datetime.datetime.now, offerResume=True):
        problems = checklistProblems(checklist)
        if problems:
            raise ValueError("Bad checklist: " + "; ".join(problems))
        self.checklist = checklist
        self.items = flatten(checklist)
        self.resultsDir = resultsDir
        self.machine = machine or checklist.get("machine", "")
        self.commit = commit
        self.rawAsk = ask
        self.out = out
        self.clock = clock
        self.offerResume = offerResume
        self.session = None
        self.basePath = None

    # -- input ------------------------------------------------------------

    def ask(self, prompt):
        # Waits as long as it takes.  End of input and Ctrl-C mean quit.
        try:
            return (self.rawAsk or input)(prompt)
        except (EOFError, KeyboardInterrupt):
            self.out("")
            raise Quit()

    def say(self, text="", indent=""):
        if not text:
            self.out("")
            return
        for paragraph in str(text).split("\n"):
            self.out(textwrap.fill(paragraph, WIDTH, initial_indent=indent,
                                   subsequent_indent=indent) if paragraph else "")

    def field(self, label, text):
        if text:
            prefix = f"  {label + ':':<10}"
            self.out(textwrap.fill(str(text), WIDTH, initial_indent=prefix,
                                   subsequent_indent=" " * len(prefix)))

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
                self.say(f"An unfinished '{self.checklist['name']}' session was found: "
                         f"{os.path.basename(basePath)}.md, {total - tally[None]} of {total} "
                         f"answered, last saved {session.get('updated')}.")
                if not self.ask("Resume it? [Y/n] ").strip().lower().startswith("n"):
                    session.setdefault("answers", {})
                    self.session, self.basePath = session, basePath
                    self.say("Resuming. Results:")
                    self.out(f"  {self.basePath}.md (and .json)")
                    unanswered = [i for i, (_, item) in enumerate(self.items)
                                  if item["id"] not in session["answers"]]
                    return unanswered[0] if unanswered else len(self.items)
        moment = self.clock()
        commit = self.commit if self.commit is not None else gitCommit()
        self.session = newSession(self.checklist, self.machine, commit, moment)
        self.basePath = newBasePath(self.resultsDir, self.checklist["name"], moment)
        self.save()
        self.say("Results are saved after every answer to:")
        self.out(f"  {self.basePath}.md (and .json)")
        return 0

    def isSectionStart(self, index):
        return index == 0 or self.items[index][0] is not self.items[index - 1][0]

    def sectionRange(self, index):
        section = self.items[index][0]
        indexes = [i for i, (s, _) in enumerate(self.items) if s is section]
        return indexes[0], indexes[-1] + 1

    def sectionPrompt(self, index):
        """Returns the next index: index (start), past the section (skipped), or index-1 (back)."""
        section = self.items[index][0]
        number = self.checklist["sections"].index(section) + 1
        first, end = self.sectionRange(index)
        self.out("")
        self.out("=" * WIDTH)
        self.say(f"Section {number} of {len(self.checklist['sections'])}: {section['title']} "
                 f"({end - first} item{'' if end - first == 1 else 's'})")
        if section.get("intro"):
            self.say(section["intro"], "  ")
        if section.get("needs"):
            self.field("Needs", section["needs"])
        self.out("=" * WIDTH)
        while True:
            key, note = parseAnswer(
                self.ask("Enter to start, [s]kip the whole section, [b]ack, [q]uit: "), SECTION_WORDS)
            if key == "":
                return index
            if key == "q":
                raise Quit()
            if key == "b":
                if index == 0:
                    self.say("This is the first item; there is nothing before it.")
                    continue
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
            self.say("Press Enter to start the section, s to skip it, b to go back, q to quit.")

    def showItem(self, index):
        section, item = self.items[index]
        self.out("")
        self.out("-" * WIDTH)
        self.say(f"[{index + 1}/{len(self.items)}] {item['id']}  {item['title']}   "
                 f"({section['title']})")
        self.field("Needs", item.get("needs"))
        self.field("Safety", item.get("safety"))
        self.field("Do", item.get("do"))
        self.field("Run", item.get("command"))
        self.field("Expect", item.get("expect"))
        previous = self.session["answers"].get(item["id"])
        if previous:
            note = f" ({previous['note']})" if previous.get("note") else ""
            self.field("Answered", f"{previous['result']}{note}")

    def itemPrompt(self, index):
        """Ask until the item is answered.  Returns the next index."""
        _, item = self.items[index]
        self.showItem(index)
        pending = ""
        while True:
            previous = self.session["answers"].get(item["id"])
            keep = f"(Enter keeps {previous['result']}) " if previous else ""
            answer = self.ask(keep + ITEM_PROMPT)
            key, inline = parseAnswer(answer, ITEM_WORDS)
            if key == "":
                if previous:
                    return index + 1
                self.say(ITEM_HELP)
                continue
            if key == "q":
                raise Quit()
            if key == "b":
                if index == 0:
                    self.say("This is the first item; there is nothing before it.")
                    continue
                return index - 1
            if key == "n":
                note = inline or self.ask("Note: ").strip()
                if previous:
                    previous["note"] = note
                    self.save()
                    self.say("Note saved.")
                else:
                    pending = note
                    self.say("Note kept; now answer p, f or s.")
                continue
            if key == "p":
                self.record(item, PASS, inline or pending)
                return index + 1
            if key == "f":
                note = inline or pending
                while not note:
                    note = self.ask("What went wrong? (a short note) ").strip()
                self.record(item, FAIL, note)
                return index + 1
            if key == "s":
                note = inline or pending or self.ask("Why skip? (Enter for none) ").strip()
                self.record(item, SKIP, note)
                return index + 1
            self.say(ITEM_HELP)

    def run(self):
        """Run the checklist.  Returns the session dict."""
        try:
            index = self.start()
            self.say(f"{self.checklist['title']}. Machine: {self.session['machine']}. "
                     f"Commit: {self.session['commit']}.")
            if self.checklist.get("intro") and index == 0:
                self.say(self.checklist["intro"], "  ")
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
