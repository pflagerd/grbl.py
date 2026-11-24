#!/usr/bin/env python3
"""Unit tests for calibrate.py's creep-probe termination logic.

No hardware: a fake Grbl tracks position and raises the limit pin at a chosen
distance, and a real OS pipe stands in for the operator's keyboard, so the
select()-based Enter check is exercised for real.

Run: python testCalibrateProbe.py
"""

import os
import re
import threading
import time
import types
import unittest

import calibrate


class FakeGrbl:
    # Just enough of GrblController for creepProbe/measureTravel: jogs move a
    # simulated position, the axis pin shows in Pn: once the probe has travelled
    # `pinAt` mm, and after `enterAt` mm the fake "operator" presses Enter by
    # writing to the pipe.
    def __init__(self, axis, pinAt=None, enterAt=None, enterWriteFd=None):
        self.axis = axis
        self.pinAt = pinAt
        self.enterAt = enterAt
        self.enterWriteFd = enterWriteFd
        self.position = {'X': 0.0, 'Y': 0.0, 'Z': 0.0}
        self.sent = []

    def travelled(self):
        return abs(self.position[self.axis])

    def send(self, gcode):
        self.sent.append(gcode)
        match = re.match(r'\$J=G91 ([XYZ])(-?[\d.]+)', gcode)
        if match:
            self.position[match.group(1)] += float(match.group(2))
            if self.enterAt is not None and self.travelled() >= self.enterAt:
                os.write(self.enterWriteFd, b"\n")
                self.enterAt = None  # one press

    def getGrblState(self):
        return 'Idle'

    def getPinState(self):
        if self.pinAt is not None and self.travelled() >= self.pinAt:
            return self.axis
        return ""

    def getMachineCoordinates(self):
        return types.SimpleNamespace(x=self.position['X'], y=self.position['Y'], z=self.position['Z'])

    def tryHomingCycle(self, homingPosition):
        self.position = {'X': 0.0, 'Y': 0.0, 'Z': 0.0}
        return None


class CreepProbeTests(unittest.TestCase):
    def setUp(self):
        readFd, self.writeFd = os.pipe()
        self.keyboard = os.fdopen(readFd, 'r')
        self.originalPause = calibrate.pause
        calibrate.pause = lambda prompt, inputStream=None: None

    def tearDown(self):
        calibrate.pause = self.originalPause
        self.keyboard.close()
        try:
            os.close(self.writeFd)
        except OSError:
            pass

    def probe(self, axis, sign, **fakeArguments):
        grbl = FakeGrbl(axis, enterWriteFd=self.writeFd, **fakeArguments)
        return calibrate.creepProbe(grbl, axis, sign, self.keyboard)

    def testZStoppedByOperatorBeforeSwitch(self):
        # The 6060 case: baseboard at 7mm, bottom switch far below at 100mm.
        measured, terminator = self.probe('Z', -1, pinAt=100, enterAt=7)
        self.assertEqual(terminator, calibrate.STOPPED_BY_OPERATOR)
        self.assertAlmostEqual(measured, 7.0)

    def testZStoppedBySwitchWhenTravelIsClear(self):
        measured, terminator = self.probe('Z', -1, pinAt=12)
        self.assertEqual(terminator, calibrate.STOPPED_BY_SWITCH)
        self.assertAlmostEqual(measured, 12.0)

    def testXEmergencyStopByOperator(self):
        # A clamp in the X path: Enter stops X even though it is switch-terminated.
        measured, terminator = self.probe('X', +1, pinAt=600, enterAt=250)
        self.assertEqual(terminator, calibrate.STOPPED_BY_OPERATOR)
        self.assertAlmostEqual(measured, 250.0)

    def testXStoppedBySwitch(self):
        measured, terminator = self.probe('X', +1, pinAt=600)
        self.assertEqual(terminator, calibrate.STOPPED_BY_SWITCH)
        self.assertAlmostEqual(measured, 600.0)

    def testNeitherStopGivesMaxTravel(self):
        measured, terminator = self.probe('Y', +1)
        self.assertEqual(terminator, calibrate.STOPPED_BY_MAX_TRAVEL)
        self.assertAlmostEqual(measured, calibrate.PROBE_MAX_TRAVEL)

    def testStdinClosedCountsAsStop(self):
        os.close(self.writeFd)  # EOF: the operator can no longer be heard
        measured, terminator = self.probe('Z', -1, pinAt=100)
        self.assertEqual(terminator, calibrate.STOPPED_BY_OPERATOR)
        self.assertAlmostEqual(measured, 0.0)

    def testEnterIsConsumed(self):
        # The press that stopped the probe must not linger for the next prompt.
        self.probe('Z', -1, pinAt=100, enterAt=3)
        self.assertFalse(calibrate.operatorPressedEnter(self.keyboard))

    def testMeasureTravelReportsTerminatorsAndBacksOff(self):
        grbl = FakeGrbl('Z', pinAt=100, enterAt=40, enterWriteFd=self.writeFd)
        travel, stoppedBy = calibrate.measureTravel(grbl, ['Z'], self.keyboard)
        self.assertAlmostEqual(travel['Z'], 40.0)
        self.assertEqual(stoppedBy['Z'], calibrate.STOPPED_BY_OPERATOR)
        self.assertEqual(grbl.sent[-1], f"$J=G91 Z{calibrate.PROBE_BACK_OFF} F{calibrate.JOG_FEED}")

    def testStaleEnterBeforeProbeIsDiscarded(self):
        # An extra Enter typed at the warning prompt must not end the probe at 0mm.
        os.write(self.writeFd, b"\n\n")
        grbl = FakeGrbl('Z', pinAt=20, enterWriteFd=self.writeFd)
        travel, stoppedBy = calibrate.measureTravel(grbl, ['Z'], self.keyboard)
        self.assertEqual(stoppedBy['Z'], calibrate.STOPPED_BY_SWITCH)
        self.assertAlmostEqual(travel['Z'], 20.0)

    def testMeasureTravelAbortsOnMaxTravel(self):
        grbl = FakeGrbl('X')
        with self.assertRaises(SystemExit):
            calibrate.measureTravel(grbl, ['X'], self.keyboard)

    def testDescribeStop(self):
        self.assertIn("stopped by operator at 7.000mm (usable depth — obstruction below)",
                      calibrate.describeStop('Z', 7.0, calibrate.STOPPED_BY_OPERATOR))
        self.assertEqual("Z: stopped by Z limit switch at 12.000mm",
                         calibrate.describeStop('Z', 12.0, calibrate.STOPPED_BY_SWITCH))
        self.assertEqual("X: stopped by X limit switch at 600.000mm",
                         calibrate.describeStop('X', 600.0, calibrate.STOPPED_BY_SWITCH))


class PromptTypeAheadTests(unittest.TestCase):
    # pause()/confirm() must never be answered by something typed BEFORE the
    # prompt appeared.  The real answer arrives from a thread after a delay,
    # the way an operator answers after reading the prompt.
    ANSWER_DELAY = 0.2

    def setUp(self):
        readFd, self.writeFd = os.pipe()
        self.keyboard = os.fdopen(readFd, 'r')

    def tearDown(self):
        self.keyboard.close()
        os.close(self.writeFd)

    def answerLater(self, text):
        timer = threading.Timer(self.ANSWER_DELAY, os.write, (self.writeFd, text))
        timer.start()
        self.addCleanup(timer.join)

    def testConfirmIgnoresTypeAhead(self):
        os.write(self.writeFd, b"y\n")  # typed ahead while the machine was moving
        self.answerLater(b"n\n")         # the operator's real answer
        self.assertFalse(calibrate.confirm("Did the tool move RIGHT?", self.keyboard))

    def testPauseWaitsForAFreshEnter(self):
        os.write(self.writeFd, b"\n\n")  # extra Enters from an earlier prompt
        self.answerLater(b"\n")
        started = time.monotonic()
        calibrate.pause("About to jog.", self.keyboard)
        self.assertGreaterEqual(time.monotonic() - started, self.ANSWER_DELAY * 0.9)

    def testConfirmReadsAnswer(self):
        self.answerLater(b"Yes\n")
        self.assertTrue(calibrate.confirm("Continue?", self.keyboard))

    def testClosedInputRaisesEOFError(self):
        os.close(self.writeFd)
        self.writeFd = os.open(os.devnull, os.O_WRONLY)  # so tearDown has something to close
        with self.assertRaises(EOFError):
            calibrate.pause("About to jog.", self.keyboard)


if __name__ == "__main__":
    unittest.main(verbosity=2)
