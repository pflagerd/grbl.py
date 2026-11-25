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
import textwrap
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import evidence  # noqa: E402
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
        self.assertIn(qaRunner.BODY + "Why skip? (Enter for none) ", self.script.prompts)

    def testFailInsistsOnANote(self):
        session = self.run_(["", "f", "", "   ", "the real note", "q"])
        self.assertEqual(session["answers"]["A1"]["note"], "the real note")
        self.assertEqual(self.script.prompts.count(qaRunner.BODY + "What went wrong? (a short note) "), 3)

    def testNoteBeforeAnswering(self):
        session = self.run_(["", "n", "coasted 15 mm", "p", "n second note", "s", "q"])
        self.assertEqual(self.answers(session)["A1"], ("pass", "coasted 15 mm"))
        self.assertEqual(self.answers(session)["A2"], ("skip", "second note"))

    def testUnknownAnswerShowsHelpAndAsksAgain(self):
        session = self.run_(["x", "", "p", "q"])
        self.assertEqual(self.answers(session)["A1"], ("pass", ""))
        self.assertEqual(" ".join(self.text().split()).count(qaRunner.FIRST_ITEM_HELP), 2)

    def testOpeningStatesThePurposeThenTheFile(self):
        checklist = dict(CHECKLIST, program="demo.py", purpose="demo.py guides you.")
        self.run_(["", "q"], checklist=checklist)
        lines = self.text().splitlines()
        self.assertEqual(lines[:3], ["(demo.py) S1:", "", "    demo.py guides you."])
        save = lines.index("    demo.py will save your responses and any relevant data to the "
                           "file:")
        self.assertTrue(lines[save + 2].strip().endswith(".md"))
        notice = " ".join(" ".join(lines).split())
        self.assertIn(qaRunner.STEP_NOTICE.format(label="(demo.py) S1"), notice)
        self.assertIn("Notice that this step is labelled (demo.py) S1. S stands for step.", notice)
        self.assertLess(save, next(i for i, line in enumerate(lines) if "Notice that" in line))
        self.assertEqual(self.script.prompts[0], qaRunner.START_PROMPT)
        self.assertEqual(self.script.prompts[1], qaRunner.BODY + qaRunner.ITEM_PROMPT)

    def testLabelNamesTheProgram(self):
        plain = qaRunner.Runner(CHECKLIST, resultsDir=self.directory, out=lambda *a: None)
        self.assertEqual(plain.label("A1", 0), "S1:")
        named = qaRunner.Runner(dict(CHECKLIST, program="demo.py"), resultsDir=self.directory,
                                out=lambda *a: None)
        self.assertEqual(named.label("A1", 0), "(demo.py) S1:")
        self.assertEqual(named.label("A2", 0), "(demo.py) S2:")

    def testShortPath(self):
        home = os.path.expanduser("~")
        self.assertEqual(qaRunner.shortPath(os.path.join(home, "a", "b.md")),
                         os.path.join("~", "a", "b.md"))
        self.assertEqual(qaRunner.shortPath("/elsewhere/b.md"), "/elsewhere/b.md")

    def stepChecklist(self, steps, **item):
        return {"name": "st", "title": "Steps", "sections": [{"title": "S", "items": [
            dict({"id": "S1", "title": "s", "steps": steps}, **item)]}]}

    def testStepsComeOneAtATime(self):
        checklist = self.stepChecklist([
            {"do": "Please start beta.", "run": "python b.py", "expect": "Beta starts."},
            {"do": "Please stop beta.", "actions": ["Hold X.", "Press Enter."],
             "expect": "Beta stops."}])
        outputs = []
        self.run_(["", "p", "q"], checklist=checklist,
                  onAsk=lambda prompt: outputs.append(len(self.output)))
        lines = self.text().splitlines()
        first = lines.index("S1:")
        self.assertEqual(lines[first + 1:first + 3], ["", "    Please start beta."])
        self.assertIn("S2:", lines)   # every step has its own number
        self.assertIn("        python b.py", lines)
        self.assertIn("    You should see: Beta starts.", lines)
        self.assertIn("       1. Hold X.", lines)
        self.assertIn("       2. Press Enter.", lines)
        # Step 2 is shown only after Enter at step 1; no Enter after the last step.
        self.assertEqual(self.script.prompts[0], qaRunner.FIRST_CONTINUE_PROMPT)
        shownBefore = "\n".join(self.output[:outputs[0]])
        self.assertNotIn("Please stop beta.", shownBefore)
        self.assertTrue(self.script.prompts[1].startswith(qaRunner.BODY + "How did it go?"))
        self.assertEqual(self.answers(self.loadJson())["S1"], ("pass", ""))

    def testAStepAlreadyDoneGetsItsOwnScreen(self):
        calls = []

        def already():
            calls.append(1)
            return True, "It's open."
        checklist = self.stepChecklist([
            {"do": "Please open a console.", "already": already},
            {"do": "Please run it.", "expect": "It runs."}])
        self.run_(["", "p", "q"], checklist=checklist)
        lines = self.text().splitlines()
        first = lines.index("S1:")
        # The heading, the note, then the step's own text, and the usual prompt.
        self.assertEqual(lines[first + 1:first + 5],
                         ["", "    Already done: It's open.", "", "    Please open a console."])
        self.assertEqual(self.script.prompts[0], qaRunner.FIRST_CONTINUE_PROMPT)
        self.assertEqual(len(calls), 1)            # Enter moves on without checking again
        self.assertNotIn(qaRunner.RECHECK_PROMPT, self.script.prompts)

    def testVerifyAsksAgainUntilDoneOrCarryOn(self):
        results = iter([(False, "Not yet."), (True, "Found it.")])
        checklist = self.stepChecklist([
            {"do": "Please install it.", "verify": lambda: next(results)}])
        self.run_(["", "", "p", "q"], checklist=checklist)
        lines = self.text().splitlines()
        self.assertIn("    Not yet.", lines)
        self.assertIn("    Found it.", lines)
        self.assertEqual(self.script.prompts[0:2], [qaRunner.FIRST_CONTINUE_PROMPT,
                                                    qaRunner.RECHECK_PROMPT])
        never = self.stepChecklist([{"do": "Please do it.",
                                     "verify": lambda: (False, "Not yet.")}])
        self.run_(["", "c", "p", "q"], checklist=never)
        self.assertEqual(self.answers(self.loadJson())["S1"], ("pass", ""))

    def testSkipIfAndAutoAnswerWithoutAsking(self):
        checklist = {"name": "sk", "title": "Skips", "sections": [{"title": "S", "items": [
            {"id": "K1", "title": "k", "auto": lambda: ("pass", "All in place.")},
            {"id": "K2", "title": "k", "skipIf": lambda: "It's installed already.",
             "steps": [{"do": "Please install it."}]},
            {"id": "K3", "title": "k", "skipIf": lambda: None,
             "steps": [{"do": "Please look."}]}]}]}
        # Each answered-for-you test has a screen of its own, and waits for Enter.
        self.run_(["", "", "p", "q"], checklist=checklist)
        answers = self.answers(self.loadJson())
        self.assertEqual(answers["K1"], ("pass", "All in place."))
        self.assertEqual(answers["K2"], ("skip", "It's installed already."))
        self.assertEqual(answers["K3"], ("pass", ""))
        text = self.text()
        self.assertIn("S1:\n\n    Checked for you: All in place.\n\n"
                      "    I've recorded this test as passed.", text)
        self.assertIn("S2:\n\n    Not needed: It's installed already.\n\n"
                      "    I've recorded this test as skipped.", text)
        self.assertIn("S3:\n\n    Please look.", text)
        self.assertNotIn("Please install it.", text)
        self.assertEqual(self.script.prompts[:2], [qaRunner.FIRST_CONTINUE_PROMPT,
                                                   qaRunner.CONTINUE_PROMPT])
        self.assertEqual(self.loadJson()["answers"]["K2"]["by"], "check")

    def testBackToACheckedTestShowsItsScreenAgain(self):
        checklist = {"name": "sk", "title": "Skips", "sections": [{"title": "S", "items": [
            {"id": "K1", "title": "k", "skipIf": lambda: "Not here.",
             "steps": [{"do": "Never shown."}]},
            {"id": "K2", "title": "k", "steps": [{"do": "Please look."}]}]}]}
        self.run_(["", "b", "q"], checklist=checklist)   # K1's screen, K2, b -> K1 again
        self.assertEqual(self.text().count("Not needed: Not here."), 2)
        self.assertNotIn("Never shown.", self.text())

    def testEachDoneStepWaitsForEnter(self):
        checklist = dict(CHECKLIST, intro=[
            {"do": "Please open a console.", "already": lambda: (True, "It's open.")},
            {"do": "Please go there.", "already": lambda: (True, "It's there.")},
            {"do": "Please read this."}])
        self.run_(["", "", "q"], checklist=checklist)
        text = self.text()
        self.assertIn("S1:\n\n    Already done: It's open.\n\n    Please open a console.", text)
        self.assertIn("S2:\n\n    Already done: It's there.\n\n    Please go there.", text)
        self.assertIn("S3:\n\n    Please read this.", text)   # after an Enter at each
        self.assertEqual(self.script.prompts, [qaRunner.FIRST_CONTINUE_PROMPT,
                                               qaRunner.CONTINUE_PROMPT,
                                               qaRunner.CONTINUE_PROMPT])

    def testNoBlankLineAtTheTopOfAScreen(self):
        self.output = []
        runner = qaRunner.Runner(CHECKLIST, resultsDir=self.directory, out=self.output.append,
                                 styled=True)
        runner.clearScreen()
        runner.gap()
        runner.out("text")
        runner.gap()
        runner.gap()
        self.assertEqual(self.output, [qaRunner.CLEAR, "text", ""])

    def testIntroStepsAndAgainOnResume(self):
        checklist = dict(CHECKLIST, intro=[
            {"do": "Please open a second console.", "again": True},
            {"do": "Please read the safety notes."}])
        self.run_(["", "", "", "p", "q"], checklist=checklist)
        # Nothing before the first step (no purpose here), so only the second offers b.
        self.assertEqual(self.script.prompts[:2], [qaRunner.FIRST_CONTINUE_PROMPT,
                                                   qaRunner.CONTINUE_PROMPT])
        self.run_(["", "", "", "q"], checklist=checklist)  # resume
        text = self.text()
        self.assertIn("S1:\n\n    Please open a second console.", text)
        self.assertNotIn("Please read the safety notes.", text)

    def testCommandsHaveEmptyLinesAroundThem(self):
        checklist = {"name": "c", "title": "C", "sections": [{"title": "S", "items": [
            {"id": "C1", "title": "c", "steps": [
                {"do": "Please run these:", "run": "cd x\nsource y", "expect": "It works."}]}]}]}
        self.run_(["", "q"], checklist=checklist)
        lines = self.text().splitlines()
        at = lines.index("    Please run these:")
        self.assertEqual(lines[at + 1:at + 6], ["", "        cd x", "        source y", "",
                                                "    You should see: It works."])

    def testQuotedOutputInAnAction(self):
        checklist = {"name": "q", "title": "Q", "sections": [{"title": "S", "items": [
            {"id": "Q1", "title": "q", "steps": [
                {"do": "Please do this:", "actions": ["Wait until it prints:\n    Ready.",
                                                      "Press Enter."]}]}]}]}
        self.run_(["", "q"], checklist=checklist)
        lines = self.text().splitlines()
        at = lines.index("       1. Wait until it prints:")
        self.assertEqual(lines[at + 1:at + 5], ["", "              Ready.", "",
                                                "       2. Press Enter."])

    def testJogModeLinesMatchJogPy(self):
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        import jog
        quoted = " ".join(evidence.JOG_MODE_STARTS.split())
        self.assertIn(f"Tap to step {jog.TAP_STEP:g} mm (Alt+tap {jog.FINE_TAP_STEP:g} mm), "
                      f"hold to jog; hold Ctrl to creep (1/{jog.CREEP_DIVISOR:g} speed).", quoted)
        with open(jog.__file__, encoding="utf-8") as file:
            source = file.read()
        self.assertIn('"Enter accepts (after any queued steps finish), Esc aborts at once. "',
                      source)
        self.assertIn('"The keyboard is captured until then."', source)

    def testCommandsAreNotWrapped(self):
        command = "python " + "x" * 100
        checklist = {"name": "w", "title": "W", "sections": [{"title": "S", "items": [
            {"id": "W1", "title": "w", "steps": [{"do": "d", "run": command, "expect": "e"}]}]}]}
        self.run_(["", "q"], checklist=checklist)
        self.assertIn(qaRunner.BODY + "    " + command, self.text().splitlines())

    def testChecklistsUseSteps(self):
        for checklist in (qa_jog.CHECKLIST, qa_calibrate.CHECKLIST):
            for _, item in qaRunner.flatten(checklist):
                self.assertTrue("steps" in item or "auto" in item, item["id"])

    def testReferencesBecomeStepNumbers(self):
        checklist = {"name": "r", "title": "R", "sections": [{"title": "S", "items": [
            {"id": "X1", "title": "x", "steps": [{"do": "a"}, {"do": "b"}]},
            {"id": "X2", "title": "y", "steps": [{"do": "As in [[X1]]."}]}]}]}
        self.run_(["", "", "p", "q"], checklist=checklist)
        self.assertIn("    As in S1-S2.", self.text().splitlines())
        bad = {"name": "b", "title": "B", "sections": [{"title": "S", "items": [
            {"id": "Y1", "title": "y", "steps": [{"do": "As in [[NOPE]]."}]}]}]}
        self.assertIn("Y1 refers to an unknown item [[NOPE]]", qaRunner.checklistProblems(bad))

    def testStepNumbersAreStable(self):
        numbers = qaRunner.numberSteps(qa_jog.CHECKLIST)
        self.assertEqual(numbers[("opening", 0)], 1)
        self.assertEqual(numbers[("intro", 0)], 2)
        self.assertEqual(sorted(numbers.values()), list(range(1, len(numbers) + 1)))

    def testItemDisplay(self):
        self.run_(["", "q"])
        text = self.text()
        for expected in ("\nAlpha\n", "S1:", "  Do alpha.", "    python a.py",
                         "  You should see: Alpha happens."):
            self.assertIn(expected, text)
        self.assertNotIn("Section", text)   # sections aren't shown
        self.assertNotIn("(First)", text)


class StepBackTests(RunnerTestCase):
    """b at a step's prompt goes back one step, across tests, and into the opening."""

    def checklist(self):
        return {"name": "sb", "title": "Step back", "program": "sb.py", "purpose": "Purpose.",
                "intro": [{"do": "Intro one."}, {"do": "Intro two.",
                                                 "already": lambda: (True, "Done.")}],
                "sections": [{"title": "S", "items": [
                    {"id": "A", "title": "a", "steps": [{"do": "A one."}, {"do": "A two."}]},
                    {"id": "B", "title": "b", "steps": [{"do": "B one."}, {"do": "B two."}]}]}]}

    def shown(self):
        # The steps shown, in order, by their text.
        return [line.strip() for line in self.text().splitlines()
                if line.strip() in ("Intro one.", "Intro two.", "A one.", "A two.", "B one.",
                                    "B two.", "Purpose.")]

    def testBackWithinATest(self):
        # S1, intro one, intro two, A one, A two -> back from A two's answer -> A one.
        self.run_(["", "", "", "", "b", "", "p", "q"], checklist=self.checklist())
        self.assertEqual(self.shown(), ["Purpose.", "Intro one.", "Intro two.", "A one.",
                                        "A two.", "A one.", "A two.", "B one."])
        self.assertEqual(self.answers(self.loadJson())["A"], ("pass", ""))

    def testBackFromAStepToTheOneBefore(self):
        self.run_(["", "", "", "", "p", "b", "q"], checklist=self.checklist())
        # B one, then b at B one: back to A's last step (A two), not A one.
        self.assertEqual(self.shown()[-2:], ["B one.", "A two."])

    def testBackIntoTheOpening(self):
        # S1, Intro one, Intro two (done, its own screen), A one, b -> Intro two;
        # b -> Intro one; b -> S1 again.
        self.run_(["", "", "", "b", "b", "b", "q"], checklist=self.checklist())
        self.assertEqual(self.shown(), ["Purpose.", "Intro one.", "Intro two.", "A one.",
                                        "Intro two.", "Intro one.", "Purpose."])
        self.assertEqual(self.text().count("Already done: Done."), 2)

    def testBackReachesStepsThatWereDone(self):
        checklist = self.checklist()
        checklist["intro"] = [{"do": "Intro one.", "already": lambda: (True, "Done.")},
                              {"do": "Intro two."}]
        # S1, Intro one (done), Intro two, b -> Intro one, b -> S1.
        self.run_(["", "", "b", "b", "q"], checklist=checklist)
        self.assertEqual(self.shown(), ["Purpose.", "Intro one.", "Intro two.", "Intro one.",
                                        "Purpose."])

    def testS1HasNoBack(self):
        self.run_(["b", "q"], checklist=self.checklist())
        self.assertNotIn("b to go back", self.script.prompts[0])
        self.assertEqual(self.shown(), ["Purpose.", "Intro one."])      # b acted as Enter
        self.assertEqual(self.script.prompts[1], qaRunner.CONTINUE_PROMPT)


class BackTests(RunnerTestCase):
    def testBackReanswersThePreviousItem(self):
        session = self.run_(["", "p", "b", "f actually wrong", "p", "", "q"])
        self.assertEqual(self.answers(session)["A1"], ("fail", "actually wrong"))
        self.assertEqual(self.answers(session)["A2"], ("pass", ""))
        self.assertIn("Answered: pass", self.text())

    def testEnterKeepsThePreviousAnswerAfterBack(self):
        session = self.run_(["", "p first", "b", "", "s", "", "q"])
        self.assertEqual(self.answers(session)["A1"], ("pass", "first"))
        self.assertIn(qaRunner.BODY + "(Enter keeps pass) " + qaRunner.FIRST_ITEM_PROMPT,
                      self.script.prompts)

    def testBackAcrossASectionBoundary(self):
        session = self.run_(["", "p", "p", "", "b", "f redo", "", "q"])
        self.assertEqual(self.answers(session)["A2"], ("fail", "redo"))
        # Going forward again shows the "needs" step once more (B1 unanswered).
        self.assertEqual(self.text().count("The next 2 tests need: probe plate wired"), 2)

    def testBackFromSectionPromptGoesToPreviousItem(self):
        session = self.run_(["", "p", "p", "b", "s skipped", "", "q"])
        self.assertEqual(self.answers(session)["A2"], ("skip", "skipped"))

    def testBackAtTheFirstItem(self):
        # The very first prompt doesn't offer [b]ack; typing b there just shows the help.
        session = self.run_(["b", "p", "q"])
        self.assertEqual(self.answers(session)["A1"], ("pass", ""))
        self.assertEqual(self.script.prompts[0], qaRunner.BODY + qaRunner.FIRST_ITEM_PROMPT)
        self.assertNotIn("[b]ack", self.script.prompts[0])
        self.assertIn(qaRunner.FIRST_ITEM_HELP, " ".join(self.text().split()))
        self.assertNotIn("back", qaRunner.FIRST_ITEM_HELP)
        # Later prompts still offer it.
        self.assertIn("[b]ack", self.script.prompts[2])

    def testNoteOnAnAnsweredItemIsSavedAtOnce(self):
        self.run_(["", "p", "b", "n added later", "q"])
        self.assertEqual(self.loadJson()["answers"]["A1"]["note"], "added later")
        self.assertEqual(self.loadJson()["answers"]["A1"]["result"], "pass")


class SectionSkipTests(RunnerTestCase):
    def testSkipAWholeSectionWithTheDefaultNote(self):
        session = self.run_(["", "p", "p", "s", "", "", "p"])
        for itemId in ("B1", "B2"):
            self.assertEqual(self.answers(session)[itemId],
                             ("skip", "skipped: needs probe plate wired"))
        self.assertEqual(self.answers(session)["C1"], ("pass", ""))
        self.assertTrue(session["finished"])
        self.assertNotIn("Gamma", self.text())  # its tests were never shown

    def testSkipAWholeSectionWithANote(self):
        session = self.run_(["p", "p", "s", "no plate today", "p"])
        self.assertEqual(self.answers(session)["B1"], ("skip", "no plate today"))
        self.assertEqual(self.answers(session)["B2"], ("skip", "no plate today"))
        self.assertEqual(self.answers(session)["C1"], ("pass", ""))
        self.assertTrue(session["finished"])
        # Only a section with "needs" gets such a step; it has a step number of its own.
        self.assertIn("S3:", self.text())
        self.assertIn(qaRunner.BODY + qaRunner.NEEDS_PROMPT, self.script.prompts)

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
        self.assertIn(qaRunner.BODY + "Resume it? [Y/n] ", self.script.prompts)
        self.assertIn("2 of 5 answered", " ".join(self.text().split()))
        self.assertEqual(self.answers(second)["A2"], ("fail", "bad"))
        self.assertEqual(self.answers(second)["C1"], ("pass", ""))
        self.assertTrue(second["finished"])
        self.assertEqual(len(os.listdir(self.directory)), 2)  # one .md, one .json

    def testResumeMidSectionStartsAtTheFirstUnansweredItem(self):
        self.run_(["", "p", "q"])
        self.run_(["y", "q"])
        self.assertIn("Beta | pipe\n\nS2:", self.text())
        self.assertNotIn("Section", self.text())

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
        self.assertNotIn(qaRunner.BODY + "Resume it? [Y/n] ", self.script.prompts)

    def testOtherChecklistsAreNotOffered(self):
        other = dict(CHECKLIST, name="other")
        self.run_(["", "p", "q"], checklist=other)
        self.run_(["q"])
        self.assertNotIn(qaRunner.BODY + "Resume it? [Y/n] ", self.script.prompts)

    def testTheMostRecentUnfinishedSessionIsOffered(self):
        self.run_(["", "p", "q"])
        self.run_(["n", "", "p", "p", "q"])
        newest = self.runner.basePath
        self.run_(["y", "q"])
        self.assertEqual(self.runner.basePath, newest)

    def testNewFlagDoesNotOfferResume(self):
        self.run_(["", "p", "q"])
        self.run_(["q"], offerResume=False)
        self.assertNotIn(qaRunner.BODY + "Resume it? [Y/n] ", self.script.prompts)

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
        self.assertIn("S2  Beta | pipe: the | pipe\\ note", text)
        self.assertIn("S6  Epsilon: again", text)

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
                         "### Failed", "- **S6** (C1) Epsilon: again",
                         "## First", "| Steps | ID | Check | Result | Note |",
                         "| S1 | A1 | Alpha | pass |  |",
                         "| S2 | A2 | Beta \\| pipe | fail | the \\| pipe\\\\ note |",
                         "## Second", "| S4 | B1 | Gamma | skip | no plate |", "## Third"):
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
        self.assertEqual(saved["items"][1], {"id": "A2", "steps": "S2", "section": "First", "title": "Beta | pipe",
                                             "result": "fail", "note": "the | pipe\\ note",
                                             "answered": saved["answers"]["A2"]["answered"]})
        self.assertEqual(saved["items"][2]["section"], "Second")

    def testMachineOverride(self):
        self.run_(["q"], machine="4030 ProverXL V2")
        self.assertEqual(self.loadJson()["machine"], "4030 ProverXL V2")


class EvidenceTests(unittest.TestCase):
    """evidence.py against a fake /proc."""

    def setUp(self):
        self.proc = tempfile.mkdtemp(prefix="qaProc-")
        self.addCleanup(shutil.rmtree, self.proc)
        self.repo = tempfile.mkdtemp(prefix="qaRepo-")
        self.addCleanup(shutil.rmtree, self.repo)

    def process(self, pid, comm, tty, cwd):
        directory = os.path.join(self.proc, str(pid))
        os.makedirs(directory)
        with open(os.path.join(directory, "stat"), "w", encoding="utf-8") as file:
            file.write(f"{pid} ({comm}) S 1 {pid} {pid} {tty} {pid} 0 0\n")
        os.symlink(cwd, os.path.join(directory, "cwd"))

    def testOtherShells(self):
        self.process(100, "bash", 34816, "/")          # this console
        self.process(101, "bash", 34817, self.repo)    # the second console
        self.process(102, "python3", 34818, self.repo)  # not a shell
        self.process(103, "bash", 0, self.repo)        # no terminal
        shells = evidence.otherShells(self.proc, tty=34816)
        self.assertEqual([shell["pid"] for shell in shells], [101])

    def testSecondConsoleChecks(self):
        self.process(101, "zsh", 34817, "/")
        tty = 34816
        original = evidence.ownTty
        evidence.ownTty = lambda proc="/proc": tty
        self.addCleanup(setattr, evidence, "ownTty", original)
        self.assertTrue(evidence.secondConsole(self.proc)[0])
        self.assertFalse(evidence.secondConsoleIn(self.repo, self.proc)[0])
        os.makedirs(os.path.join(self.proc, "201"))
        self.process(202, "bash", 34818, self.repo)
        done, message = evidence.secondConsoleIn(self.repo, self.proc)
        self.assertTrue(done)
        self.assertIn(os.path.realpath(self.repo), message)

    def testNoSecondConsole(self):
        self.process(100, "bash", 34816, "/")
        self.assertEqual(evidence.otherShells(self.proc, tty=34816), [])

    def testCanImport(self):
        self.assertTrue(evidence.canImport(sys.executable, "json"))
        self.assertFalse(evidence.canImport(sys.executable, "no_such_module_qa"))
        self.assertFalse(evidence.canImport("/no/such/python", "json"))

    def testGroups(self):
        self.assertFalse(evidence.listedInGroup("no-such-group-qa"))
        self.assertFalse(evidence.inGroupNow("no-such-group-qa"))

    def testConsoleStepsIsOneStep(self):
        # One step: open the console, go to the repository root, activate the venv.
        steps = evidence.consoleSteps("demo.py", self.repo, "~/repo")
        self.assertEqual(len(steps), 1)
        step = steps[0]
        self.assertEqual(step["run"].split("\n"), ["cd ~/repo", "source .venv/bin/activate"])
        self.assertIn("demo.py", step["do"])
        self.assertIn("activate the venv", step["do"])   # asked even when the check passes
        self.assertEqual(step["expect"], "The prompt starts with (.venv).")
        self.assertTrue(step["again"])
        with mock.patch.object(evidence, "secondConsoleIn",
                               return_value=(True, "Seen.")) as check:
            self.assertEqual(step["already"](), (True, "Seen."))
            self.assertEqual(step["verify"](), (True, "Seen."))
        self.assertEqual([call.args for call in check.call_args_list], [(self.repo,)] * 2)
        self.assertEqual(evidence.consoleSteps("d", self.repo)[0]["run"].split("\n")[0],
                         f"cd {self.repo}")   # the real path when no shorter one is given


class StyleTests(RunnerTestCase):
    def checklist(self):
        return dict(CHECKLIST, program="demo.py", purpose="demo.py guides you.", sections=[
            {"title": "S", "items": [{"id": "S1", "title": "s", "steps": [
                {"do": "Please start it.", "expect": "It starts."},
                {"do": "Please stop it."}]}]}])

    def testPlainUnlessATerminal(self):
        self.run_(["", "", "", "p", "q"], checklist=self.checklist())
        everything = self.text() + "".join(self.script.prompts)
        self.assertNotIn("\033", everything)

    def testStyledOnATerminal(self):
        checklist = self.checklist()
        checklist["intro"] = [{"do": "Please open it.", "already": lambda: (True, "It's open.")}]
        checklist["sections"][0]["items"][0]["safety"] = "Hand near reset."
        with mock.patch.dict(sys.modules, {"readline": None}):
            del sys.modules["readline"]
            self.run_(["", "", "", "", "p", "q"], checklist=checklist, styled=True)
        out = self.output
        self.assertEqual(out[0], qaRunner.CLEAR)               # a fresh screen to start
        self.assertIn("(demo.py) S3:", out)                  # headings and steps are plain
        self.assertIn("    Please start it.", out)
        self.assertIn("    You should see: It starts.", out)

        def screenWith(text):
            starts = [i for i, line in enumerate(out) if line == qaRunner.CLEAR]
            at = out.index(text)
            return out[max(i for i in starts if i < at):]
        # A done step has a screen of its own.
        doneScreen = screenWith("(demo.py) S2:")
        self.assertIn("    Already done: It's open.", doneScreen)
        self.assertNotIn("(demo.py) S2:", screenWith("(demo.py) S3:"))
        # Every step starts a fresh screen once the one before was answered, and
        # a later step of a test repeats the test's title and safety note.
        second = screenWith("(demo.py) S4:")
        self.assertNotIn("(demo.py) S3:", second)
        self.assertIn("s", second)          # the test's title
        self.assertIn("  Safety: Hand near reset.", second)
        # Nothing else is styled, prompts included.
        everything = "".join(line for line in out if line != qaRunner.CLEAR) + \
            "".join(self.script.prompts)
        self.assertNotIn("\033", everything)
        self.assertIn(qaRunner.CONTINUE_PROMPT, self.script.prompts)
        self.assertIn(qaRunner.START_PROMPT, self.script.prompts)

    def testWrapsToTheTerminalsFullWidth(self):
        checklist = self.checklist()
        checklist["purpose"] = " ".join(["word"] * 60)
        size = os.terminal_size((120, 40))
        with mock.patch.object(qaRunner.shutil, "get_terminal_size", return_value=size):
            self.run_(["q"], checklist=checklist, styled=True)
        purpose = [line for line in self.output if "word" in line]
        self.assertTrue(all(len(line) <= 120 for line in purpose))
        self.assertGreater(max(len(line) for line in purpose), 110)   # uses the width
        self.output = []
        self.run_(["q"], checklist=checklist)   # not a terminal: the fixed WIDTH
        purpose = [line for line in self.output if "word" in line]
        self.assertTrue(all(len(line) <= qaRunner.WIDTH for line in purpose))

    def testPromptsArePlain(self):
        runner = qaRunner.Runner(CHECKLIST, resultsDir=self.directory, out=lambda *a: None,
                                 styled=True)
        self.assertEqual(runner.promptStyle("  Press Enter. "), "  Press Enter. ")

    def testUseStyle(self):
        class Stream:
            def __init__(self, tty):
                self.tty = tty

            def isatty(self):
                return self.tty
        with mock.patch.dict(os.environ, {"TERM": "xterm"}, clear=True):
            self.assertTrue(qaRunner.useStyle(Stream(True)))
            self.assertFalse(qaRunner.useStyle(Stream(False)))
        with mock.patch.dict(os.environ, {"TERM": "dumb"}, clear=True):
            self.assertFalse(qaRunner.useStyle(Stream(True)))
        with mock.patch.dict(os.environ, {"TERM": "xterm", "NO_COLOR": ""}, clear=True):
            self.assertFalse(qaRunner.useStyle(Stream(True)))


class KeyTests(unittest.TestCase):
    """The "Press Enter ..." prompts react to single keys on a terminal."""

    def runner(self):
        directory = tempfile.mkdtemp(prefix="qaKeys-")
        self.addCleanup(shutil.rmtree, directory)
        return qaRunner.Runner(CHECKLIST, resultsDir=directory, out=lambda *a: None,
                               styled=False)

    def keys(self, *pressed):
        return mock.patch.multiple(qaRunner, keyboardIsTerminal=lambda: True,
                                   readKey=mock.Mock(side_effect=list(pressed)))

    def testQQuitsWithoutEnter(self):
        with self.keys("q"), redirect_stdout(io.StringIO()) as shown:
            with self.assertRaises(qaRunner.Quit):
                answer = self.runner().askKey(qaRunner.START_PROMPT)
                if answer == "q":
                    raise qaRunner.Quit()
        self.assertTrue(shown.getvalue().startswith(qaRunner.START_PROMPT))

    def testEnterAndIgnoredKeys(self):
        with self.keys("x", qaRunner.KEY_SEQUENCE, "\r"), redirect_stdout(io.StringIO()):
            self.assertEqual(self.runner().askKey(qaRunner.CONTINUE_PROMPT), "")
        with self.keys("x", "C"), redirect_stdout(io.StringIO()):
            self.assertEqual(self.runner().askKey(qaRunner.RECHECK_PROMPT, ("q", "c")), "c")

    def testEscQuitsButArrowsDont(self):
        with self.keys(qaRunner.ESC), redirect_stdout(io.StringIO()):
            self.assertEqual(self.runner().askKey(qaRunner.CONTINUE_PROMPT), "q")
        with self.keys(qaRunner.KEY_SEQUENCE, "\r"), redirect_stdout(io.StringIO()):
            self.assertEqual(self.runner().askKey(qaRunner.CONTINUE_PROMPT), "")

    def testCtrlCAndCtrlDQuit(self):
        for key in ("\x03", "\x04"):
            with self.keys(key), redirect_stdout(io.StringIO()):
                with self.assertRaises(qaRunner.Quit):
                    self.runner().askKey(qaRunner.CONTINUE_PROMPT)

    def testAnswerPromptTakesSingleKeys(self):
        with mock.patch.multiple(qaRunner, keyboardIsTerminal=lambda: True,
                                 readKey=mock.Mock(side_effect=["x", "p"])), \
                redirect_stdout(io.StringIO()) as shown:
            runner = self.runner()
            self.assertEqual(runner.choose(qaRunner.ITEM_PROMPT, qaRunner.ITEM_WORDS),
                             ("p", ""))
        self.assertNotIn("\033", shown.getvalue())   # answer prompts aren't highlighted
        with mock.patch.multiple(qaRunner, keyboardIsTerminal=lambda: True,
                                 readKey=mock.Mock(side_effect=["b", qaRunner.ESC])), \
                redirect_stdout(io.StringIO()):
            # b isn't allowed at the first item; Esc quits.
            self.assertEqual(self.runner().choose(qaRunner.FIRST_ITEM_PROMPT,
                                                  qaRunner.ITEM_WORDS, ("p", "q")), ("q", ""))

    def testLinesWhenNotATerminal(self):
        with mock.patch.object(qaRunner, "keyboardIsTerminal", lambda: False), \
                mock.patch("builtins.input", lambda prompt: " Q "):
            self.assertEqual(self.runner().askKey(qaRunner.CONTINUE_PROMPT), "q")


class BlankLineBeforePressEnterTests(RunnerTestCase):
    """Every "Press Enter ..." prompt has exactly one empty line above it."""

    def testEveryPressEnterPromptHasOneEmptyLineAboveIt(self):
        results = iter([(False, "Not yet."), (True, "Found it.")])
        checklist = {"name": "bl", "title": "Blank lines", "program": "bl.py",
                     "purpose": "Purpose.", "sections": [
                         {"title": "A", "items": [
                             {"id": "A1", "title": "a", "steps": [
                                 {"do": "Please look.", "expect": "A light."},
                                 {"do": "Please install it.",
                                  "verify": lambda: next(results)}]}]},
                         {"title": "B", "needs": "probe plate", "items": [
                             {"id": "B1", "title": "b", "skipIf": lambda: "Not here.",
                              "steps": [{"do": "Never shown."}]},
                             {"id": "B2", "title": "b",
                              "auto": lambda: ("pass", "All in place.")}]}]}
        seen = []   # (prompt, the lines shown before it)
        self.run_(["", "", "", "", "p", "", "", ""], checklist=checklist,
                  onAsk=lambda prompt: seen.append((prompt, list(self.output))))
        pressEnter = [(prompt, lines) for prompt, lines in seen if "Press Enter" in prompt]
        for prompt, lines in pressEnter:
            self.assertEqual(lines[-1], "", prompt)      # an empty line just above ...
            self.assertNotEqual(lines[-2], "", prompt)   # ... and only one
        above = [(prompt, lines[-2]) for prompt, lines in pressEnter]
        notice = qaRunner.STEP_NOTICE.format(label="(bl.py) S1")
        self.assertEqual(above, [
            # The S1 opening: the last line of the step-number notice.
            (qaRunner.START_PROMPT,
             "    " + textwrap.wrap(notice, qaRunner.WIDTH - 4)[-1]),
            (qaRunner.CONTINUE_PROMPT, "    You should see: A light."),   # after an expect
            (qaRunner.CONTINUE_PROMPT, "    Please install it."),
            (qaRunner.RECHECK_PROMPT, "    Not yet."),                    # a verify's recheck
            (qaRunner.BODY + qaRunner.NEEDS_PROMPT, "    The next 2 tests need: probe plate"),
            (qaRunner.CONTINUE_PROMPT, "    I've recorded this test as skipped."),  # Not needed
            (qaRunner.CONTINUE_PROMPT, "    I've recorded this test as passed."),   # Checked
        ])
        self.assertIn("    Not needed: Not here.", self.output)
        self.assertIn("    Checked for you: All in place.", self.output)
        # The "How did it go?" answer prompt is left as it was.
        answerPrompt = [lines for prompt, lines in seen if "How did it go?" in prompt][0]
        self.assertEqual(answerPrompt[-1], "    Found it.")

    def testKeyModePrintsTheEmptyLineThroughOut(self):
        output = []
        runner = qaRunner.Runner(CHECKLIST, resultsDir=self.directory, out=output.append,
                                 styled=False)
        keys = mock.patch.multiple(qaRunner, keyboardIsTerminal=lambda: True,
                                   readKey=mock.Mock(side_effect=["\r", "\r", "p"]))
        with keys, redirect_stdout(io.StringIO()) as shown:
            runner.out("You should see: it.")
            self.assertEqual(runner.askKey(qaRunner.CONTINUE_PROMPT), "")
            self.assertEqual(output, ["You should see: it.", ""])
            self.assertFalse(runner.lastBlank)   # the prompt is on screen now
            runner.out("")                       # already an empty line: no second one
            runner.askKey(qaRunner.CONTINUE_PROMPT)
            self.assertEqual(output, ["You should see: it.", "", ""])
            runner.out("Done.")                  # answer prompts get no empty line
            runner.choose(qaRunner.ITEM_PROMPT, qaRunner.ITEM_WORDS)
            self.assertEqual(output[-1], "Done.")
        self.assertTrue(shown.getvalue().startswith(qaRunner.CONTINUE_PROMPT))


class CalibrateEvidenceTests(unittest.TestCase):
    """qa_calibrate's checks against a temporary profiles folder."""

    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix="qaCal-")
        self.addCleanup(shutil.rmtree, self.repo)
        os.makedirs(os.path.join(self.repo, "profiles"))
        patcher = mock.patch.object(qa_calibrate, "REPO", self.repo)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, relative, data):
        with open(os.path.join(self.repo, relative), "w", encoding="utf-8") as file:
            json.dump(data, file)

    def testCalibratedToday(self):
        self.write(qa_calibrate.PROFILE, {"calibratedOn": "2020-01-01"})
        self.assertFalse(qa_calibrate.calibratedToday()[0])
        self.write(qa_calibrate.PROFILE, {"calibratedOn": qa_calibrate.today()})
        self.assertTrue(qa_calibrate.calibratedToday()[0])

    def testPlateThicknessSaved(self):
        self.assertFalse(qa_calibrate.plateThicknessSaved()[0])   # no sidecar yet
        self.write(qa_calibrate.SIDECAR, {"plateThickness": qa_calibrate.PLATE})
        self.assertTrue(qa_calibrate.plateThicknessSaved()[0])

    def testSpoilboardRecorded(self):
        check = qa_calibrate.spoilboardRecorded("jog")
        self.write(qa_calibrate.SIDECAR, {"spoilboard": {"measuredBy": "probe",
                                                         "measuredOn": qa_calibrate.today()}})
        self.assertFalse(check()[0])
        self.write(qa_calibrate.SIDECAR, {"spoilboard": {"measuredBy": "jog", "zLimit": 108.5,
                                                         "measuredOn": qa_calibrate.today()}})
        self.write(qa_calibrate.PROFILE, {"expectedSettings": {"$132": "108.500"}})
        done, message = check()
        self.assertTrue(done)
        self.assertIn("108.500", message)

    def testControllerPort(self):
        self.write(qa_calibrate.PROFILE, {"portDeviceName": os.path.join(self.repo, "tty")})
        self.assertFalse(qa_calibrate.controllerConnected()[0])
        open(os.path.join(self.repo, "tty"), "w").close()
        self.assertTrue(qa_calibrate.controllerConnected()[0])

    def testProfileUnchanged(self):
        with mock.patch.object(evidence, "gitClean", return_value=(True, "")):
            self.assertEqual(qa_calibrate.profileUnchanged()[0], qaRunner.PASS)
        with mock.patch.object(evidence, "gitClean", return_value=(False, " M profiles/x")):
            result, note = qa_calibrate.profileUnchanged()
            self.assertEqual(result, qaRunner.FAIL)
            self.assertIn("M profiles/x", note)

    def testGitClean(self):
        self.assertFalse(evidence.gitClean(self.repo, "profiles/")[0])  # not a repository
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.assertIsInstance(evidence.gitClean(root, "no-such-path-qa/")[0], bool)


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
        badSteps = {"name": "b", "title": "B", "sections": [
            {"title": "S", "items": [
                {"id": "Z", "title": "z", "expect": "e", "steps": [{"run": "r", "go": 1}]},
                {"id": "W", "title": "w", "steps": []}]}]}
        problems = qaRunner.checklistProblems(badSteps)
        self.assertIn("Z has both steps and do/command/expect", problems)
        self.assertIn("Z step 1 has no do", problems)
        self.assertIn("Z step 1 has unknown keys ['go']", problems)
        self.assertIn("W has no steps", problems)

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
                    for step in qaRunner.steps(item):
                        self.assertTrue(qaRunner.text(step["do"]).strip(), item["id"])
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

    def testJogOpeningSteps(self):
        # The intro: the console setup, then the two notes as one step.
        intro = qa_jog.CHECKLIST["intro"]
        self.assertEqual(len(intro), 2)
        self.assertEqual(intro[0]["do"],
                         evidence.consoleSteps("jog.py", qa_jog.REPO)[0]["do"])
        self.assertIn("source .venv/bin/activate", intro[0]["run"])
        safety, homes = intro[1]["do"].split("\n\n")
        self.assertIn("E-stop button", safety)
        self.assertTrue(homes.startswith("Every jog.py run homes the machine first"))
        self.assertIn("--home", homes)
        self.assertEqual(set(intro[1]), {"do"})

    def testJogH1IntroductionIsItsOwnStep(self):
        h1 = [item for _, item in qaRunner.flatten(qa_jog.CHECKLIST) if item["id"] == "H1"][0]
        first, second = h1["steps"]
        self.assertEqual(set(first), {"do"})   # the introduction only
        self.assertTrue(first["do"].startswith("Holding a key jogs until you let go."))
        self.assertIn("Jog aborted. MPos:", first["do"])
        self.assertTrue(second["do"].startswith("Please read all of this first"))
        self.assertNotIn("Holding a key", second["do"])
        self.assertEqual(second["run"], qa_jog.JOG)

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
                command = " ".join(step.get("run") or "" for step in qaRunner.steps(item))
                for program in sources:
                    if f"python {program}" not in command:
                        continue
                    for part in command.split():
                        if part.startswith("--"):
                            with self.subTest(item=item["id"], option=part):
                                self.assertIn(f'"{part}"', sources[program])


if __name__ == "__main__":
    unittest.main()
