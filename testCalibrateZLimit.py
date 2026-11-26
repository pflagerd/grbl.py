#!/usr/bin/env python3
"""Unit tests for calibrate.py's Z-limit step (issue #13, step 9).

No hardware, no evdev.  A FakeGrbl simulates just enough of Grbl 1.1h: $J
jogs move Z, G38.2 stops on a plate top (or fails with ALARM:4 / ALARM:5),
the probe input shows as P in Pn: while the fake operator holds the plate to
the bit, a motion target below -$132 is an ALARM:2 (the tests fail on it),
and $132= writes are read back by $$.  A scripted operator answers the
prompts in order, and a fake runKeyboardJog moves Z to a chosen height.

Run: python testCalibrateZLimit.py
"""

import io
import json
import os
import re
import shutil
import tempfile
import types
import unittest
from contextlib import redirect_stdout

import calibrate
import jog
from GrblController import MachineProfile

SETTINGS = {'$20': '1', '$22': '1', '$23': '3', '$27': '3.000',
            '$130': '622.000', '$131': '605.000', '$132': '109.000'}
HOME_Z = -3.0           # where $H leaves Z: -$27
FULL_TRAVEL = 109.0     # the 6060 profile's workAreaZ


class FakeGrbl:
    def __init__(self, settings=None, z=HOME_Z, plateTopZ=None, contactFailures=0, state='Idle',
                 stubborn=False):
        self.settings = dict(settings or SETTINGS)
        self.z = z
        self.plateTopZ = plateTopZ          # spindle z where the tip meets the plate top
        self.contactFailures = contactFailures  # first N probes find nothing (ALARM:5)
        self.state = state
        self.stubborn = stubborn            # accepts $132= but does not change it
        self.probeTouched = False           # operator holding the plate to the bit
        self.sent = []
        self.written = []
        self.replies = []
        self.timeout = 30
        self.alarm2 = False

    # --- GrblController surface used by calibrate.py ---
    def getSettings(self):
        return dict(self.settings)

    def send(self, gcode):
        self.sent.append(gcode)
        if gcode == '$X':
            if self.state == 'Alarm':
                self.state = 'Idle'
            return
        if self.state == 'Alarm':
            raise RuntimeError("Error during send(): b'error:9\\r\\n'")
        match = re.match(r'\$J=G91 Z(-?[\d.]+) F[\d.]+$', gcode)
        if match:
            self.moveTo(self.z + float(match.group(1)), jogging=True)
            return
        match = re.match(r'(\$\d+)=(.*)$', gcode)
        if match:
            if self.state != 'Idle':
                raise RuntimeError("Error during send(): b'error:8\\r\\n'")
            if not (self.stubborn and match.group(1) == '$132'):
                self.settings[match.group(1)] = f"{float(match.group(2)):.3f}"

    def moveTo(self, target, jogging=False):
        if target < -float(self.settings['$132']) or target > 0:
            if jogging:
                raise RuntimeError("Error during send(): b'error:15\\r\\n'")
            self.alarm2 = True
            raise AssertionError(f"motion target z={target} is outside the soft limits (ALARM:2)")
        self.z = round(target, 3)

    def getGrblState(self):
        return self.state

    def getMachineCoordinates(self):
        return types.SimpleNamespace(x=-619.0, y=-602.0, z=self.z)

    def getPinState(self):
        return calibrate.PROBE_PIN if self.probeTouched else ""

    def tryHomingCycle(self, homingPosition):
        self.z = HOME_Z
        return None

    # --- raw serial, used by sendProbe ---
    def write(self, data):
        self.written.append(data)
        match = re.match(rb'G91 G21 G38\.2 Z-([\d.]+) F([\d.]+)\n$', data)
        if not match:
            raise AssertionError(f"unexpected raw write {data!r}")
        distance = float(match.group(1))
        target = self.z - distance
        prb = lambda z, flag: f"[PRB:-619.000,-602.000,{z:.3f}:{flag}]\r\n".encode()
        if self.probeTouched:
            self.state = 'Alarm'
            self.replies += [b"ALARM:4\r\n", prb(self.z, 0), b"ok\r\n"]
            return
        start = self.z
        self.moveTo(target)  # soft-limit check on the whole target, as mc_line does
        if self.contactFailures == 0 and self.plateTopZ is not None and target <= self.plateTopZ < start:
            self.z = self.plateTopZ
            self.replies += [prb(self.z, 1), b"ok\r\n"]
        else:
            if self.contactFailures:
                self.contactFailures -= 1
            self.state = 'Alarm'
            self.replies += [b"ALARM:5\r\n", prb(self.z, 0), b"ok\r\n"]

    def readline(self):
        return self.replies.pop(0) if self.replies else b""

    def probes(self):
        return [data.decode().strip() for data in self.written if b'G38.2' in data]

    def settingWrites(self):
        return [gcode for gcode in self.sent if gcode.startswith('$132=')]


class Operator:
    """Answers prompts from a script of (expected substring, reply), in order."""

    def __init__(self, grbl, script):
        self.grbl = grbl
        self.script = list(script)
        self.prompts = []

    def __call__(self, prompt, inputStream=None):
        self.prompts.append(prompt)
        # The fake operator really touches / removes the plate when asked to.
        if 'HOLD it there' in prompt:
            self.grbl.probeTouched = True
        if 'Take the plate away' in prompt:
            self.grbl.probeTouched = False
        if not self.script:
            raise AssertionError(f"unscripted prompt: {prompt!r}")
        expected, reply = self.script.pop(0)
        if expected not in prompt:
            raise AssertionError(f"expected a prompt containing {expected!r}, got {prompt!r}")
        return reply


# Prompt sequences that recur
PIN_CHECK = [("HOLD it there", ""), ("Take the plate away", "")]
PROBE = PIN_CHECK + [("Put the plate on the surface", ""), ("About to probe DOWN", "")]


def fakeJog(grbl, surfaceZ, results=(jog.ACCEPTED,)):
    # Each call returns the next of `results` (the last one repeats); an
    # accepted jog leaves the spindle at surfaceZ.
    calls = []

    def runJog(grblArgument, homed, out=print):
        calls.append(homed)
        result = results[min(len(calls), len(results)) - 1]
        if result == jog.ACCEPTED:
            grbl.z = surfaceZ
        return result
    runJog.calls = calls
    return runJog


class ZLimitTestBase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory)
        self.profilePath = os.path.join(self.directory, "6060.json")
        self.profile = MachineProfile(name="6060", workAreaZ=FULL_TRAVEL,
                                      expectedSettings={"$23": "3", "$132": "109.000"})
        self.profile.save(self.profilePath)
        self.sidecarPath = calibrate.zLimitPath(self.profilePath)
        self.output = []
        self.environmentChecks = 0

    def environment(self, result):
        def check(out=print):
            self.environmentChecks += 1
            return result
        return check

    def run_step(self, grbl, script, jogResult=jog.ENVIRONMENT_OK, runJog=None, **arguments):
        operator = Operator(grbl, script)
        with redirect_stdout(io.StringIO()):  # askNumber/askChoice complaints
            result = calibrate.runZLimitStep(
                grbl, self.profile, self.profilePath, ask=operator, out=self.output.append,
                runJog=runJog or fakeJog(grbl, -100.0), checkEnvironment=self.environment(jogResult),
                pinTimeout=0.05, today="2026-11-24", **arguments)
        self.assertEqual(operator.script, [], "not every scripted answer was used")
        return result, operator

    def savedProfile(self):
        with open(self.profilePath) as jsonFile:
            return json.load(jsonFile)

    def sidecar(self):
        with open(self.sidecarPath) as jsonFile:
            return json.load(jsonFile)

    def text(self):
        return "\n".join(self.output)


class ArithmeticTests(unittest.TestCase):
    def testSpoilboard(self):
        # Surface z -100, 1 mm into the spoilboard: fence at -101, $132 = 101.
        self.assertEqual(calibrate.computeZLimit(-100.0, 1.0, FULL_TRAVEL), (-101.0, 101.0))

    def testWorkpiece(self):
        # Workpiece top at -80, 19 mm thick, 1 mm in: fence at -100.
        lowest, new132 = calibrate.computeZLimit(-80.0, 1.0, FULL_TRAVEL, materialThickness=19.0)
        self.assertAlmostEqual(lowest, -100.0)
        self.assertEqual(new132, 100.0)

    def testZeroIntoDepthIsAllowed(self):
        self.assertEqual(calibrate.computeZLimit(-100.0, 0.0, FULL_TRAVEL)[1], 100.0)

    def testRoundsDownNeverDeeper(self):
        self.assertEqual(calibrate.computeZLimit(-100.0004, 1.0, FULL_TRAVEL)[1], 101.0)
        self.assertEqual(calibrate.computeZLimit(-100.0009, 1.0, FULL_TRAVEL)[1], 101.0)
        # float noise: 52.5 must stay 52.5, not drop to 52.499
        self.assertEqual(calibrate.computeZLimit(-51.4, 1.1, FULL_TRAVEL)[1], 52.5)

    def testRefusals(self):
        refused = calibrate.ZLimitRefused
        with self.assertRaisesRegex(refused, "at or above"):
            calibrate.computeZLimit(0.5, 0.5, FULL_TRAVEL)     # fence at z=0
        with self.assertRaisesRegex(refused, "deeper than the calibrated full Z travel"):
            calibrate.computeZLimit(-108.0, 1.5, FULL_TRAVEL)  # 109.5 > 109
        with self.assertRaisesRegex(refused, "0 or more"):
            calibrate.computeZLimit(-50.0, -1.0, FULL_TRAVEL)
        with self.assertRaisesRegex(refused, "0 or more"):
            calibrate.computeZLimit(-50.0, 1.0, FULL_TRAVEL, materialThickness=-2.0)
        with self.assertRaisesRegex(refused, "pull-off"):
            calibrate.computeZLimit(-1.0, 1.0, FULL_TRAVEL, pullOff=3.0)  # 2 <= 3
        # Exactly the full travel is allowed.
        self.assertEqual(calibrate.computeZLimit(-108.0, 1.0, FULL_TRAVEL)[1], 109.0)

    def testParseProbeReport(self):
        self.assertEqual(calibrate.parseProbeReport("[PRB:-1.000,-2.000,-55.250:1]"), (-55.25, True))
        self.assertEqual(calibrate.parseProbeReport("[PRB:0.000,0.000,-3.000:0]\r\n"), (-3.0, False))


class SpoilboardProbeTests(ZLimitTestBase):
    def testSpoilboardProbeSetsLimitAndProfile(self):
        # Plate 10 mm thick; its top meets the tip at spindle z -90, so the
        # spoilboard is at -100.  1.5 mm in: fence -101.5, $132 = 101.5.
        grbl = FakeGrbl(plateTopZ=-90.0)
        result, operator = self.run_step(grbl, [
            ("Find the reference surface", "spoilboard"),
            ("Probe plate thickness", "10"),
            ("Jog the bit over the plate spot", "n"),
        ] + PROBE + [
            ("Depth to cut into the spoilboard", "1.5"),
            ("y to write it", "y"),
        ])
        self.assertEqual(result, 101.5)
        self.assertEqual(grbl.settings['$132'], "101.500")
        self.assertEqual(grbl.settingWrites(), ["$132=101.500"])
        # Probe bounded by the current fence: from z=-3 down to -109 + 0.5 margin.
        self.assertEqual(grbl.probes()[0], "G91 G21 G38.2 Z-105.500 F100")
        self.assertEqual(grbl.probes()[1], "G91 G21 G38.2 Z-4.000 F20")
        self.assertIn("G90", grbl.sent)
        self.assertFalse(grbl.alarm2)
        # Retracted after contact (back-off after the fine touch).
        self.assertGreater(grbl.z, -90.0)
        # Profile: calibrated $132 updated, full travel kept.
        saved = self.savedProfile()
        self.assertEqual(saved["expectedSettings"]["$132"], "101.500")
        self.assertEqual(saved["workAreaZ"], FULL_TRAVEL)
        # Sidecar: plate thickness and the spoilboard record.
        sidecar = self.sidecar()
        self.assertEqual(sidecar["plateThickness"], 10.0)
        self.assertEqual(sidecar["spoilboard"], {
            "surfaceZ": -100.0, "intoDepth": 1.5, "zLimit": 101.5, "measuredBy": "probe",
            "measuredOn": "2026-11-24", "probeContactZ": -90.0, "plateThickness": 10.0})
        self.assertIn(calibrate.BIT_CHANGE_WARNING, self.output)
        self.assertEqual(self.environmentChecks, 1)

    def testCommandLineValuesAreNotAskedFor(self):
        grbl = FakeGrbl(plateTopZ=-90.0)
        result, operator = self.run_step(grbl, [
            ("Probe plate thickness", "10"),
            ("Jog the bit over the plate spot", "n"),
        ] + PROBE + [("y to write it", "y")], method="spoilboard", intoDepth=2.0)
        self.assertEqual(result, 102.0)
        self.assertIn("(from the command line)", self.text())

    def testPlateThicknessAskedOnlyOnce(self):
        for _ in range(2):
            grbl = FakeGrbl(plateTopZ=-90.0)
            script = [("Find the reference surface", "spoilboard")]
            if not os.path.exists(self.sidecarPath):
                script.append(("Probe plate thickness", "10"))
            script += [("Jog the bit over the plate spot", "n")] + PROBE + [
                ("Depth to cut into the spoilboard", "1"), ("y to write it", "y")]
            _, operator = self.run_step(grbl, script)
        allPrompts = "\n".join(operator.prompts)
        self.assertNotIn("Probe plate thickness", allPrompts)  # second run: not asked
        self.assertIn("Probe plate thickness: 10 mm (saved", self.text())

    def testPlateThicknessOptionOverridesSavedValue(self):
        with redirect_stdout(io.StringIO()):
            calibrate.setPlateThickness(self.profilePath, 12.5)
        self.assertEqual(self.sidecar()["plateThickness"], 12.5)
        grbl = FakeGrbl(plateTopZ=-87.5)  # 12.5 mm plate on a spoilboard at -100
        result, _ = self.run_step(grbl, [("Jog the bit over the plate spot", "n")] + PROBE
                                  + [("y to write it", "y")], method="spoilboard", intoDepth=1.0)
        self.assertEqual(result, 101.0)


class WorkpieceProbeTests(ZLimitTestBase):
    def testWorkpieceProbeChangesOnlyGrbl(self):
        calibrate.saveZLimitRecord(self.sidecarPath, {"schemaVersion": 1, "plateThickness": 10.0})
        # Workpiece top at -70 (plate top at -60), 25 mm thick, 1 mm in: fence -96.
        grbl = FakeGrbl(plateTopZ=-60.0)
        result, operator = self.run_step(grbl, [
            ("Find the reference surface", "workpiece"),
            ("Jog the bit over the plate spot", "n"),
        ] + PROBE + [
            ("Material (workpiece) thickness", "25"),
            ("Depth to cut into the spoilboard", "1"),
            ("y to write it", "y"),
        ])
        self.assertEqual(result, 96.0)
        self.assertEqual(grbl.settings['$132'], "96.000")
        self.assertEqual(self.savedProfile()["expectedSettings"]["$132"], "109.000")
        self.assertNotIn("spoilboard", self.sidecar())
        self.assertIn("surface z - thickness - into-depth = -96.000", self.text())
        self.assertIn(calibrate.BIT_CHANGE_WARNING, self.output)


class JogMethodTests(ZLimitTestBase):
    def testJogSpoilboard(self):
        grbl = FakeGrbl()
        runJog = fakeJog(grbl, -100.25)
        result, _ = self.run_step(grbl, [
            ("Find the reference surface", "jog"),
            ("Press Enter to start jogging", ""),
            ("spoilboard or the workpiece", "spoilboard"),
            ("Depth to cut into the spoilboard", "1"),
            ("y to write it", "y"),
        ], runJog=runJog)
        self.assertEqual(result, 101.25)
        self.assertEqual(runJog.calls, [True])  # homed=True
        self.assertEqual(self.savedProfile()["expectedSettings"]["$132"], "101.250")
        self.assertEqual(self.sidecar()["spoilboard"]["measuredBy"], "jog")
        self.assertEqual(grbl.probes(), [])

    def testJogWorkpiece(self):
        grbl = FakeGrbl()
        result, _ = self.run_step(grbl, [
            ("Find the reference surface", "jog"),
            ("Press Enter to start jogging", ""),
            ("spoilboard or the workpiece", "w"),
            ("Material (workpiece) thickness", "18"),
            ("Depth to cut into the spoilboard", "0.5"),
            ("y to write it", "y"),
        ], runJog=fakeJog(grbl, -80.0))
        self.assertEqual(result, 98.5)
        self.assertEqual(self.savedProfile()["expectedSettings"]["$132"], "109.000")
        self.assertFalse(os.path.exists(self.sidecarPath))

    def testJogAbortRetryAndBackToMethod(self):
        # Esc, retry; Esc again, give up on jog and choose the method again; then a
        # jog that is accepted.
        grbl = FakeGrbl()
        runJog = fakeJog(grbl, -100.0, results=(jog.ABORTED, jog.ABORTED, jog.ACCEPTED))
        result, _ = self.run_step(grbl, [
            ("Find the reference surface", "jog"),
            ("Press Enter to start jogging", ""),
            ("Try the measurement again", "y"),
            ("Press Enter to start jogging", ""),
            ("Try the measurement again", "n"),
            ("Find the reference surface", "jog"),
            ("Press Enter to start jogging", ""),
            ("spoilboard or the workpiece", "spoilboard"),
            ("Depth to cut into the spoilboard", "1"),
            ("y to write it", "y"),
        ], runJog=runJog)
        self.assertEqual(result, 101.0)
        self.assertEqual(len(runJog.calls), 3)
        self.assertEqual(self.output.count("  Jog aborted (Esc)."), 2)

    def testJogUnavailableOffersOnlyProbeMethods(self):
        grbl = FakeGrbl(plateTopZ=-90.0)
        result, operator = self.run_step(grbl, [
            ("Find the reference surface", "jog"),        # not offered: asked again
            ("Find the reference surface", "spoilboard"),
            ("Probe plate thickness", "10"),
        ] + PROBE + [("Depth to cut into the spoilboard", "1"), ("y to write it", "y")],
            jogResult=jog.UNAVAILABLE, method="jog")
        self.assertEqual(result, 101.0)
        self.assertNotIn("jog", operator.prompts[0].split("(")[1])
        self.assertIn("--method jog was given, but keyboard jog is unavailable", self.text())
        # No offer to jog over the plate either.
        self.assertFalse(any("Jog the bit over" in prompt for prompt in operator.prompts))


class ProbeFailureTests(ZLimitTestBase):
    def testNoContactUnlocksAndRetries(self):
        grbl = FakeGrbl(plateTopZ=-90.0, contactFailures=1)
        result, _ = self.run_step(grbl, [
            ("Find the reference surface", "spoilboard"),
            ("Probe plate thickness", "10"),
            ("Jog the bit over the plate spot", "n"),
        ] + PROBE + [("Try the measurement again", "y"),
                     ("Jog the bit over the plate spot", "n")] + PROBE + [
            ("Depth to cut into the spoilboard", "1"),
            ("y to write it", "y"),
        ])
        self.assertEqual(result, 101.0)
        self.assertIn("ALARM:5", self.text())
        self.assertIn("$X", grbl.sent)
        self.assertFalse(grbl.alarm2)

    def testProbeAlreadyTriggeredIsAlarm4(self):
        grbl = FakeGrbl()
        grbl.probeTouched = True
        with self.assertRaisesRegex(calibrate.ProbeFailed, "ALARM:4"):
            calibrate.sendProbe(grbl, 10.0, 100)
        self.assertEqual(grbl.sent, ["$X", "G90"])
        self.assertEqual(grbl.state, 'Idle')

    def testPinCheckFailsBeforeAnyMotion(self):
        grbl = FakeGrbl(plateTopZ=-90.0)
        operator = Operator(grbl, [("HOLD it there", "")])
        grbl.getPinState = lambda: ""  # the clip or the wiring is broken
        with self.assertRaisesRegex(calibrate.ProbeFailed, "No probe trigger"):
            calibrate.probeSurface(grbl, SETTINGS, 10.0, operator, self.output.append, 0.05)
        self.assertEqual(grbl.written, [])

    def testPinAlreadyTriggeredRefusesBeforeAsking(self):
        grbl = FakeGrbl()
        grbl.probeTouched = True
        operator = Operator(grbl, [])
        with self.assertRaisesRegex(calibrate.ProbeFailed, "already triggered"):
            calibrate.probePinCheck(grbl, operator, self.output.append, 0.05)
        self.assertEqual(operator.prompts, [])

    def testFailedReportWithoutAlarm(self):
        grbl = FakeGrbl()
        grbl.write = lambda data: grbl.replies.extend([b"[PRB:0.000,0.000,-13.000:0]\r\n", b"ok\r\n"])
        with self.assertRaisesRegex(calibrate.ProbeFailed, r"\[PRB:\.\.\.:0\]"):
            calibrate.sendProbe(grbl, 10.0, 100)

    def testNoAnswer(self):
        grbl = FakeGrbl()
        grbl.write = lambda data: None
        with self.assertRaisesRegex(calibrate.ProbeFailed, "No answer"):
            calibrate.sendProbe(grbl, 10.0, 100)
        self.assertEqual(grbl.timeout, 30)  # restored

    def testProbeBoundedByShallowPerJobLimit(self):
        # A per-job $132 of 20 is in force; the spoilboard is far below it.  The
        # probe must stop at the fence (no ALARM:2) and say to reset first.
        settings = dict(SETTINGS, **{'$132': '20.000'})
        grbl = FakeGrbl(settings=settings, plateTopZ=-90.0)
        operator = Operator(grbl, PROBE)
        with self.assertRaisesRegex(calibrate.ProbeFailed, "ALARM:5"):
            calibrate.probeSurface(grbl, settings, 10.0, operator, self.output.append, 0.05)
        self.assertEqual(grbl.probes(), ["G91 G21 G38.2 Z-16.500 F100"])
        self.assertFalse(grbl.alarm2)
        self.assertIn("--reset-z-limit", self.text())

    def testNoRoomToProbe(self):
        settings = dict(SETTINGS, **{'$132': '3.200'})
        grbl = FakeGrbl(settings=settings)
        operator = Operator(grbl, PIN_CHECK + [("Put the plate on the surface", "")])
        with self.assertRaisesRegex(calibrate.ProbeFailed, "nothing to probe"):
            calibrate.probeSurface(grbl, settings, 10.0, operator, self.output.append, 0.05)
        self.assertEqual(grbl.written, [])


class ChangeYourMindTests(ZLimitTestBase):
    def testChangeMostRecentThenGoBackFurther(self):
        grbl = FakeGrbl()
        result, operator = self.run_step(grbl, [
            ("Find the reference surface", "jog"),
            ("Press Enter to start jogging", ""),
            ("spoilboard or the workpiece", "workpiece"),
            ("Material (workpiece) thickness", "20"),
            ("Depth to cut into the spoilboard", "1"),
            ("y to write it", "b"),                          # change the most recent: into-depth
            ("Depth to cut into the spoilboard", "2"),
            ("y to write it", "3"),                          # back further: surface kind
            ("spoilboard or the workpiece", "spoilboard"),   # thickness no longer applies
            ("y to write it", "y"),
        ], runJog=fakeJog(grbl, -60.0))
        # workpiece: -60 - 20 - 1 = -81; into 2: -82; spoilboard: -60 - 2 = -62
        summaries = [line.split()[3] for line in self.output if line.startswith("  New $132")]
        self.assertEqual(summaries, ["81.000", "82.000", "62.000"])
        self.assertEqual(result, 62.0)
        self.assertEqual(grbl.settingWrites(), ["$132=62.000"])  # nothing before the final y
        self.assertIn("  4. Material thickness: 20 mm", self.output)
        lastSummary = self.text().rsplit("--- Z limit summary", 1)[1]
        self.assertNotIn("thickness", lastSummary)
        self.assertIn("old $132 = 109.000", lastSummary)
        self.assertEqual(self.savedProfile()["expectedSettings"]["$132"], "62.000")

    def testRefusedSummaryCannotBeConfirmed(self):
        grbl = FakeGrbl()
        result, operator = self.run_step(grbl, [
            ("Find the reference surface", "jog"),
            ("Press Enter to start jogging", ""),
            ("spoilboard or the workpiece", "spoilboard"),
            ("Depth to cut into the spoilboard", "12"),      # 112 > 109: refused
            ("to change that step", "y"),                    # ignored
            ("to change that step", "b"),
            ("Depth to cut into the spoilboard", "5"),
            ("y to write it", "y"),
        ], runJog=fakeJog(grbl, -100.0))
        self.assertEqual(result, 105.0)
        self.assertIn("REFUSED", self.text())
        self.assertFalse(operator.prompts[4].startswith("y to write it"))
        self.assertEqual(grbl.settingWrites(), ["$132=105.000"])

    def testChangeMethodRemeasures(self):
        calibrate.saveZLimitRecord(self.sidecarPath, {"schemaVersion": 1, "plateThickness": 10.0})
        grbl = FakeGrbl(plateTopZ=-90.0)
        result, _ = self.run_step(grbl, [
            ("Find the reference surface", "jog"),
            ("Press Enter to start jogging", ""),
            ("spoilboard or the workpiece", "spoilboard"),
            ("Depth to cut into the spoilboard", "1"),
            ("y to write it", "1"),                          # change the method
            ("Find the reference surface", "spoilboard"),
            ("Jog the bit over the plate spot", "n"),
        ] + PROBE + [("y to write it", "y")], runJog=fakeJog(grbl, -85.0))
        self.assertEqual(result, 101.0)   # the probe's -100, not the jog's -85; into-depth kept
        self.assertEqual(self.sidecar()["spoilboard"]["measuredBy"], "probe")

    def testQuitWritesNothing(self):
        grbl = FakeGrbl()
        result, _ = self.run_step(grbl, [
            ("Find the reference surface", "jog"),
            ("Press Enter to start jogging", ""),
            ("spoilboard or the workpiece", "spoilboard"),
            ("Depth to cut into the spoilboard", "1"),
            ("q to quit", "q"),
        ])
        self.assertIsNone(result)
        self.assertEqual(grbl.settingWrites(), [])
        self.assertEqual(self.savedProfile()["expectedSettings"]["$132"], "109.000")
        self.assertNotIn(calibrate.BIT_CHANGE_WARNING, self.output)


class WriteAndResetTests(ZLimitTestBase):
    def testWriteRefusedUnlessIdle(self):
        grbl = FakeGrbl(state='Run')
        with self.assertRaisesRegex(calibrate.ZLimitRefused, "not Idle"):
            calibrate.writeZLimit(grbl, 100.0, self.output.append)
        self.assertEqual(grbl.sent, [])

    def testWriteVerifiedByReadBack(self):
        grbl = FakeGrbl(stubborn=True)  # 'ok' but the value did not change
        with self.assertRaisesRegex(RuntimeError, "reads back"):
            calibrate.writeZLimit(grbl, 100.0, self.output.append)

    def testResetRestoresProfileValue(self):
        self.profile.expectedSettings["$132"] = "101.500"   # after a spoilboard step
        grbl = FakeGrbl(settings=dict(SETTINGS, **{'$132': '60.000'}))  # a per-job limit
        self.assertEqual(calibrate.resetZLimit(grbl, self.profile, self.output.append), 101.5)
        self.assertEqual(grbl.settingWrites(), ["$132=101.500"])
        self.assertEqual(grbl.settings['$132'], "101.500")

    def testResetWhenAlreadyThere(self):
        grbl = FakeGrbl()
        calibrate.resetZLimit(grbl, self.profile, self.output.append)
        self.assertEqual(grbl.settingWrites(), [])
        self.assertIn("already 109.000", self.text())

    def testResetWithoutCalibratedValueUsesWorkAreaZ(self):
        del self.profile.expectedSettings["$132"]
        grbl = FakeGrbl(settings=dict(SETTINGS, **{'$132': '60.000'}))
        calibrate.resetZLimit(grbl, self.profile, self.output.append)
        self.assertEqual(grbl.settingWrites(), ["$132=109.000"])


class OperatorWordingTests(unittest.TestCase):
    """The operator is told to use the E-stop button, never reset/power.

    The Grbl soft reset (Ctrl-X) and --reset-z-limit are different things and
    keep their names.
    """

    BANNED = ("reset button", "reset/power", "power switch", "hand on reset")

    def testNoOperatorMessageSaysResetButton(self):
        # Every string literal in calibrate.py, f-string pieces and the
        # docstring included, with line breaks folded to single spaces so a
        # phrase wrapped across two lines is still caught.
        import ast
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "calibrate.py")) as source:
            tree = ast.parse(source.read())
        found = [(node.lineno, node.value) for node in ast.walk(tree)
                 if isinstance(node, ast.Constant) and isinstance(node.value, str)
                 and any(banned in " ".join(node.value.lower().split())
                         for banned in self.BANNED)]
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
