#!/usr/bin/env python3
"""Unit tests for the QA checklists on any machine (issue #13, Step 13 follow-up).

qa_jog.py and qa_calibrate.py build their checklists from the chosen
machine's profile.  These tests use profiles written to a temporary folder
(or passed in directly), so they need neither the machine nor the profiles
committed in profiles/.  Standard library only.

Run: python qa/testAnyMachine.py   (or python3 qa/testAnyMachine.py)
"""

import copy
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


def profile(x, y, z, homing="3", pullOff=3.0, steps="160.000", calibratedOn="2026-11-23"):
    """A profile as calibrate.py writes it (the fields the checklists use)."""
    travel = {"$130": f"{x:.3f}", "$131": f"{y:.3f}", "$132": f"{z:.3f}"}
    return {
        "name": "test", "portDeviceName": "/dev/ttyUSB0", "portBaudRate": 115200,
        "workAreaX": float(x), "workAreaY": float(y), "workAreaZ": float(z),
        "homingPosition": homing, "pullOff": pullOff, "calibratedOn": calibratedOn,
        "expectedSettings": dict({"$3": "0", "$23": homing, "$20": "1", "$21": "1"}, **travel),
        "settingsSnapshot": dict({"$23": homing, "$27": f"{pullOff:.3f}", "$100": steps,
                                  "$101": steps, "$102": steps}, **travel),
    }


# The 6060 ProverXL V1 as calibrated on 2026-11-23 (profiles/6060 ProverXL V1.json).
P6060 = profile(622, 605, 109)
# A 4030-like machine: 400 x 300 x 110, $23=3, pull-off 3.
P4030 = profile(400, 300, 110)
# The digits that were hard-coded for the 6060; none may show up for another machine.
SIX_THOUSAND_SIXTY = ("619", "618", "602", "601", "622", "605", "109", "6060")

TEXT_KEYS = ("title", "do", "run", "expect", "actions", "safety", "needs", "intro", "whenDone",
             "purpose")


def texts(value):
    """Every piece of text the operator can see in a checklist (functions are
    called; checks such as "auto" and "verify" are left out)."""
    if callable(value):
        value = value()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in TEXT_KEYS or key in ("sections", "items", "steps"):
                yield from texts(item)
    elif isinstance(value, list):
        for item in value:
            yield from texts(item)
    elif isinstance(value, str):
        yield value


def allText(checklist):
    return "\n".join(texts(checklist))


def item(checklist, itemId):
    return [found for _, found in qaRunner.flatten(checklist) if found["id"] == itemId][0]


def stepText(checklist, itemId, key="expect", number=0):
    value = qaRunner.steps(item(checklist, itemId))[number][key]
    return " ".join(qaRunner.text(value)) if isinstance(value, list) else qaRunner.text(value)


class TempRepo(unittest.TestCase):
    """A temporary repository root with a profiles/ folder, used by both checklists."""

    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix="qaAnyMachine-")
        self.addCleanup(shutil.rmtree, self.repo)
        self.profiles = os.path.join(self.repo, "profiles")
        os.makedirs(self.profiles)
        for module in (qa_jog, qa_calibrate):
            patcher = mock.patch.object(module, "REPO", self.repo)
            patcher.start()
            self.addCleanup(patcher.stop)

    def write(self, filename, data):
        with open(os.path.join(self.profiles, filename), "w", encoding="utf-8") as file:
            json.dump(data, file)


class HomeMPosTests(unittest.TestCase):
    def testThe6060(self):
        self.assertEqual(qaRunner.mposText(qaRunner.homeMPos(P6060)),
                         "X-619.000 Y-602.000 Z-3.000")

    def testThe4030(self):
        self.assertEqual(qaRunner.mposText(qaRunner.homeMPos(P4030)),
                         "X-397.000 Y-297.000 Z-3.000")

    def testEachDirectionBit(self):
        # A set bit homes that axis toward negative: -travel + pull-off; a clear
        # bit homes it toward positive: -pull-off.
        cases = {"0": "X-3.000 Y-3.000 Z-3.000", "1": "X-397.000 Y-3.000 Z-3.000",
                 "2": "X-3.000 Y-297.000 Z-3.000", "3": "X-397.000 Y-297.000 Z-3.000",
                 "7": "X-397.000 Y-297.000 Z-107.000"}
        for homing, expected in cases.items():
            with self.subTest(homing=homing):
                self.assertEqual(qaRunner.mposText(qaRunner.homeMPos(
                    profile(400, 300, 110, homing=homing))), expected)
                away = qaRunner.awayFromHome(profile(400, 300, 110, homing=homing))
                mask = int(homing)
                self.assertEqual(away, {"X": 1 if mask & 1 else -1, "Y": 1 if mask & 2 else -1,
                                        "Z": 1 if mask & 4 else -1})

    def testPullOff(self):
        self.assertEqual(qaRunner.mposText(qaRunner.homeMPos(profile(400, 300, 110,
                                                                     pullOff=1.5))),
                         "X-398.500 Y-298.500 Z-1.500")


class JogChecklistTests(unittest.TestCase):
    def build(self, data, machine="Test Machine"):
        checklist = qa_jog.makeChecklist(machine, copy.deepcopy(data))
        self.assertEqual(qaRunner.checklistProblems(checklist), [])
        self.assertEqual(checklist["machine"], machine)
        return checklist

    def test6060ValuesAreTheOldLiterals(self):
        checklist = self.build(P6060, "6060 ProverXL V1")
        home = "X-619.000 Y-602.000 Z-3.000"
        self.assertIn(f"Homed, the 6060 ProverXL V1 is at {home} ($130=622, $131=605, $27=3).",
                      stepText(checklist, "H1", "do"))
        self.assertIn(f"Jog accepted. MPos: {home}", stepText(checklist, "H1", "expect", 1))
        self.assertIn("A block with X -619.000, Y -602.000, Z -3.000",
                      stepText(checklist, "H1", "expect", 1))
        self.assertIn("Hold Left for a second. Hold Down for a second. Hold Shift+Up for a "
                      "second.", stepText(checklist, "H2", "actions"))
        self.assertIn("Right moves the tool right (+X) at 1000 mm/min and Left moves it back",
                      stepText(checklist, "H3"))
        self.assertIn("The MPos X is a little above -619.", stepText(checklist, "H3"))
        self.assertIn("Up moves the tool away from you (+Y) and Down toward you",
                      stepText(checklist, "H4"))
        self.assertIn("The MPos Y is a little above -602.", stepText(checklist, "H4"))
        self.assertIn("Shift+Down lowers the spindle and Shift+Up raises it",
                      stepText(checklist, "H5"))
        self.assertIn("The MPos Z is a little below -3.", stepText(checklist, "H5"))
        self.assertIn("Ctrl+Right creeps at 100 mm/min.", stepText(checklist, "H8"))
        self.assertIn("right-hand switch", item(checklist, "H10")["safety"])
        self.assertIn("(about 40 seconds)", stepText(checklist, "H10", "actions"))
        self.assertIn("The MPos X is -0.010 (0.01 mm inside the soft limit).",
                      stepText(checklist, "H10"))
        self.assertIn("back and right-hand switches", item(checklist, "H11")["safety"])
        self.assertIn("(about a minute)", stepText(checklist, "H11", "actions"))
        self.assertIn("A diagonal until Y reaches the back edge, then along that edge in X only",
                      stepText(checklist, "H11"))
        self.assertIn("The MPos is X-0.010 Y-0.010.", stepText(checklist, "H11"))
        self.assertIn("\"Jog accepted. MPos: X-618.900 Y-602.000 Z-3.000\"",
                      stepText(checklist, "T1"))
        self.assertIn("With 160 steps/mm, positions move in whole motor steps of 0.00625 mm.",
                      stepText(checklist, "T1", "do"))
        self.assertEqual(stepText(checklist, "T2"), "\"MPos: X-618.900 Y-601.900 Z-3.100\".")
        self.assertEqual(stepText(checklist, "T3"), "\"MPos: X-618.900 ...\".")
        self.assertIn("\"MPos: X-618.900 ...\" (each tap is 1 or 2 motor steps, 0.006 or "
                      "0.013 mm).", stepText(checklist, "T4"))
        self.assertIn("X-618.900 ...", stepText(checklist, "T5"))
        self.assertEqual(item(checklist, "T6")["title"], "Ten Alt+Shift+Down taps lower Z by 0.100")
        self.assertIn("tap Down ten times", stepText(checklist, "T6", "actions"))
        self.assertIn("Z-3.100", stepText(checklist, "T6"))
        self.assertIn("\"MPos: X-618.900 Y-601.900 ...\"", stepText(checklist, "T7"))
        self.assertIn("\"MPos: X-618.000 ...\"", stepText(checklist, "T10"))
        self.assertIn("Tap Right once (to X-618.900). Tap Left once. Tap Left once more.",
                      stepText(checklist, "T11", "actions"))
        self.assertIn("X ends at about -618.99 (to within a motor step), never -619.000 or "
                      "beyond.", stepText(checklist, "T11"))
        self.assertIn("(X-618.000 for ten)", stepText(checklist, "T12"))
        self.assertIn("(X between -619.000 and -618.000)", stepText(checklist, "T13"))
        self.assertIn(f"\"Jog aborted. MPos: {home}\"", stepText(checklist, "S6", "expect", 1))
        self.assertEqual(stepText(checklist, "S6", "run"),
                         'python3 qa/qa_jog.py --machine "6060 ProverXL V1"')

    def test4030Values(self):
        checklist = self.build(P4030, "4030 ProverXL V2")
        home = "X-397.000 Y-297.000 Z-3.000"
        self.assertIn(f"Homed, the 4030 ProverXL V2 is at {home} ($130=400, $131=300, $27=3).",
                      stepText(checklist, "H1", "do"))
        self.assertIn("The MPos X is a little above -397.", stepText(checklist, "H3"))
        self.assertIn("The MPos Y is a little above -297.", stepText(checklist, "H4"))
        self.assertIn("(about 20 seconds)", stepText(checklist, "H10", "actions"))
        self.assertIn("(about 30 seconds)", stepText(checklist, "H11", "actions"))
        self.assertIn("\"Jog accepted. MPos: X-396.900 Y-297.000 Z-3.000\"",
                      stepText(checklist, "T1"))
        self.assertEqual(stepText(checklist, "T2"), "\"MPos: X-396.900 Y-296.900 Z-3.100\".")
        self.assertIn("\"MPos: X-396.000 ...\"", stepText(checklist, "T10"))
        self.assertIn("about -396.99", stepText(checklist, "T11"))
        self.assertIn("(X between -397.000 and -396.000)", stepText(checklist, "T13"))
        self.assertIn(f"\"Jog aborted. MPos: {home}\"", stepText(checklist, "S6", "expect", 1))
        self.assertEqual(stepText(checklist, "S6", "run"),
                         'python3 qa/qa_jog.py --machine "4030 ProverXL V2"')

    def testNo6060NumbersForAnotherMachine(self):
        text = allText(self.build(P4030, "4030 ProverXL V2"))
        for number in SIX_THOUSAND_SIXTY:
            self.assertNotIn(number, text)

    def testXAndYHomingTowardPositive(self):
        # $23=0: X and Y home at their high ends, so "away" is Left and Down.
        checklist = self.build(profile(400, 300, 110, homing="0"))
        self.assertIn("Hold Right for a second. Hold Up for a second. Hold Shift+Up",
                      stepText(checklist, "H2", "actions"))
        self.assertIn("Left moves the tool left (-X) at 1000 mm/min and Right moves it back",
                      stepText(checklist, "H3"))
        self.assertIn("The MPos X is a little below -3.", stepText(checklist, "H3"))
        self.assertIn("Down moves the tool toward you (-Y) and Up away from you",
                      stepText(checklist, "H4"))
        self.assertIn("left-hand switch", item(checklist, "H10")["safety"])
        self.assertIn("The MPos X is -399.990", stepText(checklist, "H10"))
        self.assertIn("front and left-hand switches", item(checklist, "H11")["safety"])
        self.assertIn("The MPos is X-399.990 Y-299.990.", stepText(checklist, "H11"))
        self.assertIn("X-3.100 Y-3.000 Z-3.000", stepText(checklist, "T1"))
        self.assertEqual(stepText(checklist, "T2"), "\"MPos: X-3.100 Y-3.100 Z-3.100\".")
        self.assertIn("Tap Left once (to X-3.100). Tap Right once.",
                      stepText(checklist, "T11", "actions"))
        self.assertIn("about -3.01", stepText(checklist, "T11"))

    def testZHomingDown(self):
        # $23=7: Z homes to the bottom, so stepping away from home raises it.
        checklist = self.build(profile(400, 300, 110, homing="7"))
        self.assertIn("Hold Shift+Down for a second.", stepText(checklist, "H2", "actions"))
        self.assertIn("Shift+Up raises the spindle and Shift+Down lowers it",
                      stepText(checklist, "H5"))
        self.assertIn("a little above -107.", stepText(checklist, "H5"))
        self.assertEqual(item(checklist, "T6")["title"], "Ten Alt+Shift+Up taps raise Z by 0.100")
        self.assertIn("Z-106.900", stepText(checklist, "T6"))

    def testMotorStepsFollowTheProfile(self):
        checklist = self.build(profile(400, 300, 110, steps="100.000"))
        self.assertIn("With 100 steps/mm, positions move in whole motor steps of 0.01 mm.",
                      stepText(checklist, "T1", "do"))
        self.assertIn("(each tap is 1 motor step, 0.010 mm)", stepText(checklist, "T4"))
        bare = profile(400, 300, 110)
        del bare["settingsSnapshot"]
        checklist = self.build(bare)
        self.assertIn("it steps 0.1 mm.\n\nPlease read", stepText(checklist, "T1", "do"))
        self.assertTrue(stepText(checklist, "T4").endswith("\"MPos: X-396.900 ...\"."))


class JogNoProfileTests(TempRepo):
    def testRefusesWithoutAProfile(self):
        with self.assertRaises(SystemExit) as raised:
            qa_jog.makeChecklist("4030 ProverXL V2")
        message = str(raised.exception.code)
        self.assertIn("There's no profile for \"4030 ProverXL V2\"", message)
        self.assertIn('python calibrate.py "4030 ProverXL V2"', message)
        self.assertIn("qa/qa_calibrate.py", message)

    def testReadsTheProfileFromProfiles(self):
        self.write("4030 ProverXL V2.json", P4030)
        checklist = qa_jog.makeChecklist("4030 ProverXL V2")
        self.assertIn("X-397.000 Y-297.000 Z-3.000", stepText(checklist, "H1", "do"))

    def testMainRefusesAnUnknownMachine(self):
        with self.assertRaises(SystemExit) as raised, redirect_stdout(io.StringIO()):
            qaRunner.main(qa_jog.makeChecklist, ["--machine", "Nope", "--new"],
                          title=qa_jog.TITLE, profilesDir=self.profiles)
        self.assertIn('python calibrate.py "Nope"', str(raised.exception.code))

    def testMainWithNoProfilesAtAll(self):
        ask = mock.Mock(side_effect=AssertionError("no menu without profiles"))
        with self.assertRaises(SystemExit) as raised:
            qaRunner.main(qa_jog.makeChecklist, ["--new"], title=qa_jog.TITLE,
                          ask=ask, out=lambda *a: None, profilesDir=self.profiles)
        self.assertIn("No machine has a profile", str(raised.exception.code))
        self.assertIn('python calibrate.py "NAME"', str(raised.exception.code))


class MenuTests(TempRepo):
    def setUp(self):
        super().setUp()
        self.write("B machine.json", P4030)
        self.write("A machine.json", P6060)
        self.write("A machine.machine.json", {"schemaVersion": 1, "spindleControl": "manual"})
        self.write("A machine.zlimit.json", {"plateThickness": 10})
        self.write("bits.json", {"bits": {}, "schemaVersion": 1})

    def choose(self, answers, names=None, offerNew=False):
        self.prompts, self.lines = [], []

        def ask(prompt):
            self.prompts.append(prompt)
            if not answers:
                raise EOFError()
            answer = answers.pop(0)
            if isinstance(answer, BaseException):
                raise answer
            return answer
        names = qaRunner.profileNames(self.profiles) if names is None else names
        return qaRunner.chooseMachine(names, offerNew=offerNew, ask=ask, out=self.lines.append)

    def testProfileNamesLeaveOutSidecarsAndOtherFiles(self):
        self.assertEqual(qaRunner.profileNames(self.profiles), ["A machine", "B machine"])

    def testTheRealProfilesFolder(self):
        names = qaRunner.profileNames()
        self.assertIn("6060 ProverXL V1", names)
        for name in names:
            self.assertFalse(name.endswith((".machine", ".zlimit")), name)
        self.assertNotIn("bits", names)

    def testPickByNumber(self):
        self.assertEqual(self.choose(["2"]), "B machine")
        self.assertIn("    1  A machine", self.lines)
        self.assertIn("    2  B machine", self.lines)
        self.assertNotIn(f"    3  {qaRunner.NEW_MACHINE}", self.lines)

    def testBadAnswersAskAgain(self):
        self.assertEqual(self.choose(["", "x", "0", "3", " 1 "]), "A machine")
        self.assertEqual(len(self.prompts), 5)
        self.assertIn("Please type a number from 1 to 2.", self.lines)

    def testQuitting(self):
        self.assertIsNone(self.choose(["q"]))
        self.assertIsNone(self.choose([]))                       # end of input
        self.assertIsNone(self.choose([KeyboardInterrupt()]))     # Ctrl-C
        self.assertIsNone(self.choose(["x"], names=[]))          # nothing to choose

    def testNewMachine(self):
        name = self.choose(["3", "", "a/b", ".hidden", "  4030 ProverXL V2  "], offerNew=True)
        self.assertEqual(name, "4030 ProverXL V2")
        self.assertIn(f"    3  {qaRunner.NEW_MACHINE}", self.lines)
        self.assertEqual(self.lines.count("Please type a name, without a / and not starting "
                                          "with a dot."), 3)

    def testNewMachineWithNoProfilesYet(self):
        self.assertEqual(self.choose(["1", "First"], names=[], offerNew=True), "First")
        self.assertIn("Which machine is this for? No machine has a profile in profiles/ yet.",
                      self.lines)

    def runMain(self, build, argv, answers, offerNew=False):
        results = tempfile.mkdtemp(prefix="results-", dir=self.repo)
        prompts = []

        def ask(prompt):
            prompts.append(prompt)
            return answers.pop(0)
        with mock.patch("builtins.input", side_effect=["q"]), \
                redirect_stdout(io.StringIO()):
            qaRunner.main(build, argv + ["--results-dir", results, "--new"], title="T",
                          offerNew=offerNew, ask=ask, out=lambda *a: None,
                          profilesDir=self.profiles)
        jsonFile = [name for name in os.listdir(results) if name.endswith(".json")][0]
        with open(os.path.join(results, jsonFile), encoding="utf-8") as file:
            return json.load(file), prompts

    def demo(self, built):
        def build(machine):
            built.append(machine)
            return {"name": "demo", "title": "Demo", "machine": machine, "sections": [
                {"title": "S", "items": [{"id": "A1", "title": "a", "do": "Do a.",
                                          "expect": "A happens."}]}]}
        return build

    def testMainAsksWithoutMachine(self):
        built = []
        saved, prompts = self.runMain(self.demo(built), [], ["2"])
        self.assertEqual(built, ["B machine"])
        self.assertEqual(saved["machine"], "B machine")
        self.assertEqual(len(prompts), 1)

    def testMachineArgumentSkipsTheMenu(self):
        built = []
        saved, prompts = self.runMain(self.demo(built), ["--machine", "A machine"], [])
        self.assertEqual(built, ["A machine"])
        self.assertEqual(saved["machine"], "A machine")
        self.assertEqual(prompts, [])

    def testMainWithANewMachine(self):
        built = []
        saved, _ = self.runMain(self.demo(built), [], ["3", "C machine"], offerNew=True)
        self.assertEqual(saved["machine"], "C machine")

    def testMainQuitAtTheMenu(self):
        with self.assertRaises(SystemExit) as raised:
            qaRunner.main(self.demo([]), ["--new"], title="T", ask=lambda prompt: "q",
                          out=lambda *a: None, profilesDir=self.profiles)
        self.assertIn("No machine chosen", str(raised.exception.code))


class CalibrateChecklistTests(TempRepo):
    def build(self, machine):
        checklist = qa_calibrate.makeChecklist(machine)
        self.assertEqual(qaRunner.checklistProblems(checklist), [])
        self.assertEqual(checklist["machine"], machine)
        return checklist

    def test6060ValuesAreTheOldLiterals(self):
        self.write("6060 ProverXL V1.json", P6060)
        checklist = self.build("6060 ProverXL V1")
        self.assertEqual(stepText(checklist, "P2"),
                         "{'$20': '1', '$21': '1', '$130': '622.000', '$131': '605.000', "
                         "'$132': '109.000'}, matching the profile. Please note anything "
                         "different.")
        self.assertIn("with N close to the profile's 622. Then", stepText(checklist, "C6"))
        self.assertIn("with N close to 605. Then", stepText(checklist, "C7"))
        self.assertIn("Jog aborted. MPos: X-619.000 Y-602.000 Z-3.000",
                      stepText(checklist, "ZR2"))
        self.assertIn('python calibrate.py "6060 ProverXL V1" --reset-z-limit',
                      stepText(checklist, "ZR3", "run"))
        self.assertIn("profiles/6060 ProverXL V1.zlimit.json", stepText(checklist, "ZR5"))
        self.assertIn("rewrites profiles/6060 ProverXL V1.json", allText(checklist))
        self.assertEqual(stepText(checklist, "C12", "run", 1), "git diff profiles/")

    def test4030Recalibration(self):
        self.write("4030 ProverXL V2.json", P4030)
        checklist = self.build("4030 ProverXL V2")
        self.assertIn("'$130': '400.000', '$131': '300.000', '$132': '110.000'",
                      stepText(checklist, "P2"))
        self.assertIn("close to the profile's 400", stepText(checklist, "C6"))
        self.assertIn("close to 300", stepText(checklist, "C7"))
        self.assertIn("X-397.000 Y-297.000 Z-3.000", stepText(checklist, "ZR2"))
        self.assertIn('python calibrate.py "4030 ProverXL V2" --z-limit --method jog',
                      stepText(checklist, "ZJ2", "run"))
        text = allText(checklist)
        for number in SIX_THOUSAND_SIXTY:
            self.assertNotIn(number, text)

    def testFirstCalibration(self):
        machine = "4030 ProverXL V2"
        checklist = self.build(machine)
        text = allText(checklist)
        for number in SIX_THOUSAND_SIXTY:
            self.assertNotIn(number, text)
        # P1 passes as "no profile yet", without asking git.
        with mock.patch.object(qa_calibrate.evidence, "gitClean",
                               side_effect=AssertionError("git isn't needed")):
            result, note = item(checklist, "P1")["auto"]()
        self.assertEqual(result, qaRunner.PASS)
        self.assertIn("There's no profile yet (profiles/4030 ProverXL V2.json)", note)
        # P2 records Grbl's settings instead of comparing.
        p2 = stepText(checklist, "P2")
        self.assertNotIn("matching the profile", p2)
        self.assertIn("There's no profile yet to compare them with, so please record Grbl's "
                      "$130-$132 in this test's note", p2)
        # It says how: n for the note, then p.
        self.assertIn("press n and you will see a prompt for Note:, then enter the three "
                      "values as shown ($130=... $131=... $132=...), then press p to "
                      "continue.", p2)
        self.assertIn("the measured X travel (there's no profile yet", stepText(checklist, "C6"))
        self.assertIn("the measured Y travel. Then", stepText(checklist, "C7"))
        self.assertIn("This is the machine's first calibration: it writes "
                      "profiles/4030 ProverXL V2.json, a new file.", text)
        self.assertEqual(qa_calibrate.port(machine), "/dev/ttyUSB0")   # the default, by name
        done, message = qaRunner.steps(item(checklist, "C12"))[0]["verify"]()
        self.assertFalse(done)
        self.assertIn("There's no profile at profiles/4030 ProverXL V2.json yet", message)
        self.assertIn("no usable profile", stepText(checklist, "ZR2"))
        self.assertEqual(stepText(checklist, "C12", "run", 1), "git status --short profiles/")

    def testFirstCalibrationReadsTheNewProfileWhenItRuns(self):
        machine = "4030 ProverXL V2"
        checklist = self.build(machine)
        # calibrate.py writes the profile during the run (after the checklist is built).
        self.write(f"{machine}.json", profile(400, 300, 110, calibratedOn=qa_calibrate.today()))
        done, _ = qaRunner.steps(item(checklist, "C12"))[0]["verify"]()
        self.assertTrue(done)
        self.assertIn("Jog aborted. MPos: X-397.000 Y-297.000 Z-3.000",
                      stepText(checklist, "ZR2"))
        self.assertEqual(qa_calibrate.port(machine), "/dev/ttyUSB0")
        runner = mock.Mock(session={"answers": {"P2": {"note": "$130=200.000 $131=200.000 "
                                                               "$132=200.000"}}})
        with mock.patch.object(qaRunner.Runner, "current", runner):
            c12 = stepText(checklist, "C12", "expect", 1)
        self.assertIn("Measured travel (the new profile's): $130=400.000, $131=300.000, "
                      "$132=110.000.", c12)
        self.assertIn("Grbl's settings before calibrating ([[P2]]): $130=200.000 "
                      "$131=200.000 $132=200.000.", c12)
        with mock.patch.object(qaRunner.Runner, "current", None):
            self.assertIn("([[P2]]): nothing noted.", stepText(checklist, "C12", "expect", 1))

    def testFirstCalibrationRunShowsP2NextToTheMeasuredTravel(self):
        machine = "4030 ProverXL V2"
        checklist = self.build(machine)
        output, answers = [], []

        def ask(prompt=""):
            output.append(prompt)
            if "How did it go?" not in prompt:
                return "c" if "c to carry on" in prompt else ""
            answers.append(prompt)
            if len(answers) == 1:
                # P2 answered; calibrate.py then writes the profile in the other console.
                self.write(f"{machine}.json",
                           profile(400, 300, 110, calibratedOn=qa_calibrate.today()))
                return "p $130=200.000 $131=200.000 $132=200.000"
            return "q" if any("Measured travel" in line for line in output) else "p"
        results = tempfile.mkdtemp(prefix="results-", dir=self.repo)
        with mock.patch.object(qa_calibrate.evidence, "secondConsoleIn",
                               lambda directory: (True, "Seen.")):
            session = qaRunner.Runner(checklist, resultsDir=results, machine=machine,
                                      commit="abc1234", ask=ask, out=output.append).run()
        self.assertEqual(session["answers"]["P1"]["result"], qaRunner.PASS)
        self.assertEqual(session["answers"]["P2"]["note"],
                         "$130=200.000 $131=200.000 $132=200.000")
        shown = " ".join(" ".join(line.split()) for line in output)
        self.assertIn("Measured travel (the new profile's): $130=400.000, $131=300.000, "
                      "$132=110.000. Grbl's settings before calibrating (S", shown)
        self.assertIn("): $130=200.000 $131=200.000 $132=200.000.", shown)
        self.assertEqual(session["machine"], machine)

    def testFirstCalibrationNoteWithNThenPShowsInC12(self):
        """P2 as its text says: n, the three values at Note:, then p."""
        machine = "4030 ProverXL V2"
        checklist = self.build(machine)
        output, answers = [], []

        def ask(prompt=""):
            output.append(prompt)
            if prompt.endswith("Note: "):
                return "$130=200.000 $131=200.000 $132=200.000"
            if "How did it go?" not in prompt:
                return "c" if "c to carry on" in prompt else ""
            answers.append(prompt)
            if len(answers) == 1:
                return "n"
            if len(answers) == 2:
                # P2 answered; calibrate.py then writes the profile in the other console.
                self.write(f"{machine}.json",
                           profile(400, 300, 110, calibratedOn=qa_calibrate.today()))
                return "p"
            return "q" if any("Measured travel" in line for line in output) else "p"
        results = tempfile.mkdtemp(prefix="results-", dir=self.repo)
        with mock.patch.object(qa_calibrate.evidence, "secondConsoleIn",
                               lambda directory: (True, "Seen.")):
            session = qaRunner.Runner(checklist, resultsDir=results, machine=machine,
                                      commit="abc1234", ask=ask, out=output.append).run()
        shown = " ".join(" ".join(line.split()) for line in output)
        self.assertIn("Note kept; now answer p, f or s.", shown)
        self.assertEqual(session["answers"]["P2"]["result"], qaRunner.PASS)
        self.assertEqual(session["answers"]["P2"]["note"],
                         "$130=200.000 $131=200.000 $132=200.000")
        self.assertIn("Measured travel (the new profile's): $130=400.000, $131=300.000, "
                      "$132=110.000. Grbl's settings before calibrating (S", shown)
        self.assertIn("): $130=200.000 $131=200.000 $132=200.000.", shown)


if __name__ == "__main__":
    unittest.main()
