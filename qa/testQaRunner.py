#!/usr/bin/env python3
"""Unit tests for qaRunner.py and the two checklists.  Standard library only.

Input is a scripted list of answers (end of the list = end of input), output
is collected in a list, the clock is fake, and results go to a temporary
directory, so nothing touches qa/results/ or the machine.

Run: python qa/testQaRunner.py   (or python3 qa/testQaRunner.py)
"""

import datetime
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import qaRunner  # noqa: E402
import qa_calibrate  # noqa: E402
import qa_jog  # noqa: E402

CHECKLIST = {
    "name": "demo",
    "title": "Demo checklist",
    "machine": "Test Machine",
    "sections": [
        {"title": "First", "items": [
            {"id": "A1", "title": "Alpha", "do": "Do alpha.", "command": "python a.py",
             "expect": "Alpha happens."},
            {"id": "A2", "title": "Beta | pipe", "do": "Do beta.", "expect": "Beta happens."},
        ]},
        {"title": "Second", "needs": "probe plate wired", "items": [
            {"id": "B1", "title": "Gamma", "expect": "Gamma happens.", "needs": "plate",
             "safety": "Hand on reset."},
            {"id": "B2", "title": "Delta", "expect": "Delta happens."},
        ]},
        {"title": "Third", "items": [
            {"id": "C1", "title": "Epsilon", "expect": "Epsilon happens."},
        ]},
    ],
}


class Crash(Exception):
    pass


class Script:
    """input() stand-in: returns the scripted answers in order, then EOFError.

    An answer may be an exception instance, which is raised instead.  `onAsk`
    is called before each answer, e.g. to look at the files on disk.
    """

    def __init__(self, answers, onAsk=None):
        self.answers = list(answers)
        self.prompts = []
        self.onAsk = onAsk

    def __call__(self, prompt=""):
        self.prompts.append(prompt)
        if self.onAsk:
            self.onAsk(prompt)
        if not self.answers:
            raise EOFError()
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


class FakeClock:
    def __init__(self, start=datetime.datetime(2026, 11, 24, 9, 30, 0)):
        self.now = start

    def __call__(self):
        self.now += datetime.timedelta(seconds=1)
        return self.now


class RunnerTestCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="qaRunnerTest-")
        self.addCleanup(shutil.rmtree, self.directory)
        self.clock = FakeClock()

    def run_(self, answers, checklist=CHECKLIST, onAsk=None, **kwargs):
        self.script = Script(answers, onAsk)
        self.output = []
        runner = qaRunner.Runner(checklist, resultsDir=self.directory, commit="abc1234",
                                 ask=self.script, out=self.output.append, clock=self.clock,
                                 **kwargs)
        session = runner.run()
        self.runner = runner
        return session

    def answers(self, session):
        return {key: (value["result"], value["note"]) for key, value in session["answers"].items()}

    def loadJson(self):
        with open(self.runner.basePath + ".json", encoding="utf-8") as file:
            return json.load(file)

    def loadMarkdown(self):
        with open(self.runner.basePath + ".md", encoding="utf-8") as file:
            return file.read()

    def text(self):
        return "\n".join(self.output)


class AnswerTests(RunnerTestCase):
    def testPassFailSkipWithNotes(self):
        session = self.run_(["", "p", "f", "it broke",
                             "", "s", "no plate", "p looks good",
                             "", "pass"])
        self.assertEqual(self.answers(session), {
            "A1": ("pass", ""), "A2": ("fail", "it broke"),
            "B1": ("skip", "no plate"), "B2": ("pass", "looks good"), "C1": ("pass", "")})
        self.assertTrue(session["finished"])

    def testInlineNotesForFailAndSkip(self):
        session = self.run_(["", "f wrong axis", "skip not today", "", "s", "", "q"])
        self.assertEqual(session["answers"]["A1"]["note"], "wrong axis")
        self.assertEqual(session["answers"]["A2"]["note"], "not today")
        self.assertEqual(session["answers"]["B1"], {**session["answers"]["B1"],
                                                     "result": "skip", "note": ""})
        # The skip note was asked for (and left empty); a fail note never is with inline text.
        self.assertIn("Why skip? (Enter for none) ", self.script.prompts)

    def testFailInsistsOnANote(self):
        session = self.run_(["", "f", "", "   ", "the real note", "q"])
        self.assertEqual(session["answers"]["A1"]["note"], "the real note")
        self.assertEqual(self.script.prompts.count("What went wrong? (a short note) "), 3)

    def testNoteBeforeAnswering(self):
        session = self.run_(["", "n", "coasted 15 mm", "p", "n second note", "s", "q"])
        self.assertEqual(self.answers(session)["A1"], ("pass", "coasted 15 mm"))
        self.assertEqual(self.answers(session)["A2"], ("skip", "second note"))

    def testUnknownAnswerShowsHelpAndAsksAgain(self):
        session = self.run_(["", "x", "", "p", "q"])
        self.assertEqual(self.answers(session)["A1"], ("pass", ""))
        self.assertEqual(self.text().count("p = pass (\"p text\" adds a note)"), 2)

    def testItemDisplay(self):
        self.run_(["", "q"])
        text = self.text()
        for expected in ("[1/5] A1  Alpha", "Do:", "Do alpha.", "Run:", "python a.py",
                         "Expect:", "Alpha happens.", "Section 1 of 3: First (2 items)"):
            self.assertIn(expected, text)


class BackTests(RunnerTestCase):
    def testBackReanswersThePreviousItem(self):
        session = self.run_(["", "p", "b", "f actually wrong", "p", "", "q"])
        self.assertEqual(self.answers(session)["A1"], ("fail", "actually wrong"))
        self.assertEqual(self.answers(session)["A2"], ("pass", ""))
        self.assertIn("Answered: pass", self.text())

    def testEnterKeepsThePreviousAnswerAfterBack(self):
        session = self.run_(["", "p first", "b", "", "s", "", "q"])
        self.assertEqual(self.answers(session)["A1"], ("pass", "first"))
        self.assertIn("(Enter keeps pass) " + qaRunner.ITEM_PROMPT, self.script.prompts)

    def testBackAcrossASectionBoundary(self):
        session = self.run_(["", "p", "p", "", "b", "f redo", "", "q"])
        self.assertEqual(self.answers(session)["A2"], ("fail", "redo"))
        # Going forward again into B1 shows the section prompt once more (B1 unanswered).
        self.assertEqual(self.text().count("Section 2 of 3: Second"), 2)

    def testBackFromSectionPromptGoesToPreviousItem(self):
        session = self.run_(["", "p", "p", "b", "s skipped", "", "q"])
        self.assertEqual(self.answers(session)["A2"], ("skip", "skipped"))

    def testBackAtTheFirstItem(self):
        session = self.run_(["b", "", "b", "p", "q"])
        self.assertEqual(self.answers(session)["A1"], ("pass", ""))
        self.assertEqual(self.text().count("This is the first item"), 2)

    def testNoteOnAnAnsweredItemIsSavedAtOnce(self):
        self.run_(["", "p", "b", "n added later", "q"])
        self.assertEqual(self.loadJson()["answers"]["A1"]["note"], "added later")
        self.assertEqual(self.loadJson()["answers"]["A1"]["result"], "pass")


class SectionSkipTests(RunnerTestCase):
    def testSkipAWholeSectionWithTheDefaultNote(self):
        session = self.run_(["", "p", "p", "s", "", "", "p"])
        for itemId in ("B1", "B2"):
            self.assertEqual(self.answers(session)[itemId],
                             ("skip", "section skipped: probe plate wired"))
        self.assertEqual(self.answers(session)["C1"], ("pass", ""))
        self.assertTrue(session["finished"])
        self.assertNotIn("[3/5] B1", self.text())  # its items were never shown

    def testSkipAWholeSectionWithANote(self):
        session = self.run_(["s first section not today", "s", "", "", "p"])
        self.assertEqual(self.answers(session)["A1"], ("skip", "first section not today"))
        self.assertEqual(self.answers(session)["A2"], ("skip", "first section not today"))
        self.assertEqual(self.answers(session)["B1"][1], "section skipped: probe plate wired")
        self.assertTrue(session["finished"])

    def testSkippedSectionIsSavedAtOnce(self):
        self.run_(["", "p", "p", "s", ""])  # input ends right after the section skip
        saved = self.loadJson()
        self.assertEqual(saved["answers"]["B2"]["result"], "skip")
        self.assertFalse(saved["finished"])

    def testQuitFromSectionPrompt(self):
        session = self.run_(["q"])
        self.assertEqual(session["answers"], {})
        self.assertFalse(session["finished"])


class QuitResumeTests(RunnerTestCase):
    def testQuitSavesAndResumeContinues(self):
        first = self.run_(["", "p", "f bad", "q"])
        self.assertFalse(first["finished"])
        basePath = self.runner.basePath
        self.assertIn("Saved. Run it again to resume.", self.text())

        second = self.run_(["", "", "p", "p", "", "p"])  # Enter = yes, resume
        self.assertEqual(self.runner.basePath, basePath)
        self.assertIn("Resume it? [Y/n] ", self.script.prompts)
        self.assertIn("2 of 5 answered", " ".join(self.text().split()))
        self.assertEqual(self.answers(second)["A2"], ("fail", "bad"))
        self.assertEqual(self.answers(second)["C1"], ("pass", ""))
        self.assertTrue(second["finished"])
        self.assertEqual(len(os.listdir(self.directory)), 2)  # one .md, one .json

    def testResumeMidSectionStartsAtTheFirstUnansweredItem(self):
        self.run_(["", "p", "q"])
        self.run_(["y", "q"])
        self.assertIn("[2/5] A2", self.text())
        self.assertNotIn("Section 1 of 3", self.text())

    def testDecliningResumeStartsANewFile(self):
        self.run_(["", "p", "q"])
        firstPath = self.runner.basePath
        session = self.run_(["n", "", "s", "", "q"])
        self.assertNotEqual(self.runner.basePath, firstPath)
        self.assertEqual(self.answers(session), {"A1": ("skip", "")})
        self.assertEqual(len(os.listdir(self.directory)), 4)

    def testFinishedSessionsAreNotOffered(self):
        self.run_(["", "p", "p", "", "p", "p", "", "p"])
        self.run_(["q"])
        self.assertNotIn("Resume it? [Y/n] ", self.script.prompts)

    def testOtherChecklistsAreNotOffered(self):
        other = dict(CHECKLIST, name="other")
        self.run_(["", "p", "q"], checklist=other)
        self.run_(["q"])
        self.assertNotIn("Resume it? [Y/n] ", self.script.prompts)

    def testTheMostRecentUnfinishedSessionIsOffered(self):
        self.run_(["", "p", "q"])
        self.run_(["n", "", "p", "p", "q"])
        newest = self.runner.basePath
        self.run_(["y", "q"])
        self.assertEqual(self.runner.basePath, newest)

    def testNewFlagDoesNotOfferResume(self):
        self.run_(["", "p", "q"])
        self.run_(["q"], offerResume=False)
        self.assertNotIn("Resume it? [Y/n] ", self.script.prompts)

    def testEndOfInputSavesAndQuits(self):
        session = self.run_(["", "p", "f", "broken"])  # then EOF at A3's prompt
        self.assertFalse(session["finished"])
        saved = self.loadJson()
        self.assertEqual(saved["answers"]["A2"]["note"], "broken")
        self.assertFalse(saved["finished"])
        self.assertIn("Saved. Run it again to resume.", self.text())

    def testEndOfInputWhileAskingForANoteRecordsNothingForThatItem(self):
        session = self.run_(["", "p", "f"])
        self.assertNotIn("A2", session["answers"])

    def testEndOfInputAtTheResumeQuestion(self):
        self.run_(["", "p", "q"])
        self.assertIsNone(self.run_([]))

    def testCtrlCSavesAndQuits(self):
        session = self.run_(["", "p", KeyboardInterrupt()])
        self.assertEqual(self.loadJson()["answers"]["A1"]["result"], "pass")
        self.assertFalse(session["finished"])

    def testNamesInTheSameMinuteDoNotCollide(self):
        self.run_(["q"], offerResume=False)
        first = self.runner.basePath
        self.run_(["q"], offerResume=False)
        self.assertEqual(self.runner.basePath, first + "-2")
        self.assertTrue(os.path.basename(first).startswith("demo-2026-11-24-0930"))


class SaveTests(RunnerTestCase):
    def testSavedAfterEveryAnswer(self):
        seen = []

        def onAsk(prompt):
            # Before each prompt, the files on disk must hold every answer so far.
            path = self.directory
            jsonFiles = [name for name in os.listdir(path) if name.endswith(".json")]
            self.assertEqual(len(jsonFiles), 1)
            with open(os.path.join(path, jsonFiles[0]), encoding="utf-8") as file:
                seen.append(len(json.load(file)["answers"]))

        self.run_(["", "p", "f", "bad", "", "s", "", "p", "", "p"], onAsk=onAsk)
        # prompts: section, A1, A2, note, section, B1, skip-note, B2, section, C1
        self.assertEqual(seen, [0, 0, 1, 1, 2, 2, 2, 3, 4, 4])

    def testACrashMidRunLeavesValidFiles(self):
        with self.assertRaises(Crash):
            self.run_(["", "p", "f wrong", Crash()])
        jsonFiles = [name for name in os.listdir(self.directory) if name.endswith(".json")]
        self.assertEqual(len(jsonFiles), 1)
        with open(os.path.join(self.directory, jsonFiles[0]), encoding="utf-8") as file:
            saved = json.load(file)
        self.assertEqual({k: v["result"] for k, v in saved["answers"].items()},
                         {"A1": "pass", "A2": "fail"})
        with open(os.path.join(self.directory, jsonFiles[0][:-5] + ".md"), encoding="utf-8") as file:
            self.assertIn("| A2 | Beta \\| pipe | fail | wrong |", file.read())

    def testAFailedWriteKeepsTheOldFile(self):
        path = os.path.join(self.directory, "x.json")
        qaRunner.writeAtomically(path, "old\n")
        with mock.patch.object(qaRunner.os, "fsync", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                qaRunner.writeAtomically(path, "new\n")
        with open(path, encoding="utf-8") as file:
            self.assertEqual(file.read(), "old\n")
        self.assertEqual(os.listdir(self.directory), ["x.json"])  # no temporary left behind

    def testResultsDirectoryIsCreated(self):
        nested = os.path.join(self.directory, "a", "results")
        runner = qaRunner.Runner(CHECKLIST, resultsDir=nested, commit="x", ask=Script(["q"]),
                                 out=lambda *_: None, clock=self.clock)
        runner.run()
        self.assertTrue(os.path.exists(runner.basePath + ".md"))


class OutputTests(RunnerTestCase):
    def finishedSession(self):
        return self.run_(["", "p", "f the | pipe\\ note", "", "s", "no plate", "p", "", "f again"])

    def testSummaryPrinted(self):
        self.finishedSession()
        text = self.text()
        self.assertIn("All items answered.", text)
        self.assertIn("Pass 2, fail 2, skip 1.", text)
        self.assertIn("Failed:", text)
        self.assertIn("A2  Beta | pipe: the | pipe\\ note", text)
        self.assertIn("C1  Epsilon: again", text)

    def testSummaryAfterQuitCountsUnanswered(self):
        self.run_(["", "p", "q"])
        self.assertIn("Pass 1, fail 0, skip 0, not answered 4.", self.text())

    def testMarkdown(self):
        self.finishedSession()
        markdown = self.loadMarkdown()
        for expected in ("# QA: Demo checklist", "- Checklist: `demo`",
                         "- Machine profile: Test Machine", "- Git commit: `abc1234`",
                         "- Started: 2026-11-24 09:30:01", "- Status: finished",
                         "Pass 2, fail 2, skip 1, not answered 0.",
                         "### Failed", "- **C1** Epsilon: again",
                         "## First", "| ID | Check | Result | Note |",
                         "| A1 | Alpha | pass |  |",
                         "| A2 | Beta \\| pipe | fail | the \\| pipe\\\\ note |",
                         "## Second", "| B1 | Gamma | skip | no plate |", "## Third"):
            self.assertIn(expected, markdown)

    def testMarkdownWhileUnfinished(self):
        self.run_(["", "p", "q"])
        markdown = self.loadMarkdown()
        self.assertIn("- Status: unfinished (1 of 5 answered)", markdown)
        self.assertIn("| A2 | Beta \\| pipe | — |  |", markdown)
        self.assertNotIn("### Failed", markdown)

    def testJson(self):
        self.finishedSession()
        saved = self.loadJson()
        self.assertEqual(saved["checklist"], "demo")
        self.assertEqual(saved["machine"], "Test Machine")
        self.assertEqual(saved["commit"], "abc1234")
        self.assertTrue(saved["finished"])
        self.assertEqual([item["id"] for item in saved["items"]], ["A1", "A2", "B1", "B2", "C1"])
        self.assertEqual(saved["items"][1], {"id": "A2", "section": "First", "title": "Beta | pipe",
                                             "result": "fail", "note": "the | pipe\\ note",
                                             "answered": saved["answers"]["A2"]["answered"]})
        self.assertEqual(saved["items"][2]["section"], "Second")

    def testMachineOverride(self):
        self.run_(["q"], machine="4030 ProverXL V2")
        self.assertEqual(self.loadJson()["machine"], "4030 ProverXL V2")


class MiscTests(unittest.TestCase):
    def testParseAnswer(self):
        words = qaRunner.ITEM_WORDS
        self.assertEqual(qaRunner.parseAnswer("", words), ("", ""))
        self.assertEqual(qaRunner.parseAnswer("  P  ", words), ("p", ""))
        self.assertEqual(qaRunner.parseAnswer("pass it moved", words), ("p", "it moved"))
        self.assertEqual(qaRunner.parseAnswer("F  Z lagged ", words), ("f", "Z lagged"))
        self.assertEqual(qaRunner.parseAnswer("pz", words), ("?", ""))
        self.assertEqual(qaRunner.parseAnswer("p", qaRunner.SECTION_WORDS), ("?", ""))

    def testBadChecklistsAreRejected(self):
        duplicate = {"name": "d", "title": "D", "sections": [
            {"title": "S", "items": [{"id": "X", "title": "x", "expect": "e"},
                                     {"id": "X", "title": "y", "expect": "e"}]}]}
        self.assertIn("duplicate id X", qaRunner.checklistProblems(duplicate))
        with self.assertRaises(ValueError):
            qaRunner.Runner(duplicate)
        missing = {"name": "m", "title": "M", "sections": [
            {"title": "S", "items": [{"id": "Y", "title": " ", "expect": "", "typo": 1}]}]}
        problems = qaRunner.checklistProblems(missing)
        self.assertIn("Y has no title", problems)
        self.assertIn("Y has no expect", problems)
        self.assertIn("Y has unknown keys ['typo']", problems)

    def testGitCommit(self):
        commit = qaRunner.gitCommit()
        self.assertRegex(commit, r"^([0-9a-f]{4,}|unknown)$")
        self.assertEqual(qaRunner.gitCommit(os.path.join(tempfile.gettempdir(), "no-such-dir-qa")),
                         "unknown")

    def testMainRunsWithArguments(self):
        directory = tempfile.mkdtemp(prefix="qaRunnerMain-")
        self.addCleanup(shutil.rmtree, directory)
        with mock.patch("builtins.input", side_effect=["", "p", EOFError()]), \
                redirect_stdout(io.StringIO()) as output:
            qaRunner.main(CHECKLIST, ["--results-dir", directory, "--new", "--machine", "M"])
        jsonFiles = [name for name in os.listdir(directory) if name.endswith(".json")]
        with open(os.path.join(directory, jsonFiles[0]), encoding="utf-8") as file:
            saved = json.load(file)
        self.assertEqual(saved["machine"], "M")
        self.assertEqual(saved["answers"]["A1"]["result"], "pass")
        self.assertIn("Saved. Run it again to resume.", output.getvalue())


class ChecklistSanityTests(unittest.TestCase):
    CHECKLISTS = (qa_jog.CHECKLIST, qa_calibrate.CHECKLIST)

    def testBothChecklistsAreWellFormed(self):
        for checklist in self.CHECKLISTS:
            with self.subTest(checklist=checklist["name"]):
                self.assertEqual(qaRunner.checklistProblems(checklist), [])
                ids = [item["id"] for _, item in qaRunner.flatten(checklist)]
                self.assertEqual(len(ids), len(set(ids)))
                for _, item in qaRunner.flatten(checklist):
                    self.assertTrue(item["title"].strip(), item["id"])
                    self.assertTrue(item["expect"].strip(), item["id"])
                self.assertEqual(checklist["machine"], "6060 ProverXL V1")

    def testNamesAreDistinct(self):
        self.assertNotEqual(qa_jog.CHECKLIST["name"], qa_calibrate.CHECKLIST["name"])

    def testSections(self):
        self.assertEqual([s["title"] for s in qa_jog.CHECKLIST["sections"]],
                         ["Setup", "Hold-to-jog", "Taps", "Keyboard capture"])
        self.assertEqual([s["title"] for s in qa_calibrate.CHECKLIST["sections"]],
                         ["Before you start", "Full calibration", "Z limit (jog)",
                          "Z limit: power cycle and reset", "Z limit (jog, on the spoilboard)",
                          "Z limit (probe)"])

    def testProbeItemsAreMarked(self):
        probe = [s for s in qa_calibrate.CHECKLIST["sections"] if s["title"] == "Z limit (probe)"][0]
        self.assertIn("probe plate", probe["needs"].lower())
        for item in probe["items"]:
            self.assertIn("probe plate", item.get("needs", "").lower(), item["id"])

    def testCommandsUseRealOptions(self):
        # Every option named in a command exists in the program's argparse setup.
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        sources = {}
        for program in ("jog.py", "calibrate.py"):
            with open(os.path.join(root, program), encoding="utf-8") as file:
                sources[program] = file.read()
        for checklist in self.CHECKLISTS:
            for _, item in qaRunner.flatten(checklist):
                command = item.get("command", "")
                for program in sources:
                    if f"python {program}" not in command:
                        continue
                    for part in command.split():
                        if part.startswith("--"):
                            with self.subTest(item=item["id"], option=part):
                                self.assertIn(f'"{part}"', sources[program])


if __name__ == "__main__":
    unittest.main()
