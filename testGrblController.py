"""Tests for GrblController's serial conversation, against a scripted fake port."""

import unittest
from unittest import mock

import serial

from GrblController import GrblController

STATUS_IDLE = b"<Idle|MPos:-619.000,-602.000,-3.000|Bf:15,127|FS:0,0>\r\n"


class FakePort:
    """Stands in for serial.Serial's I/O: readline() replays scripted lines, and a
    line given as None stands for a read that timed out (b"")."""

    def __init__(self, lines):
        self.lines = list(lines)
        self.written = []

    def readline(self, *args):
        return self.lines.pop(0) if self.lines else b""

    def write(self, data):
        self.written.append(data)
        return len(data)


def controller(port):
    grbl = GrblController.__new__(GrblController)
    serial.Serial.__init__(grbl)          # unopened: no device is touched
    grbl.timeout = 30
    grbl.homingPosition = GrblController.HomingPositions.bottomLeftZUp
    patches = [mock.patch.object(serial.Serial, "readline", side_effect=port.readline),
               mock.patch.object(serial.Serial, "write", side_effect=port.write)]
    for patch in patches:
        patch.start()
    return grbl, patches


class HomingTests(unittest.TestCase):
    def run_(self, lines):
        port = FakePort(lines)
        grbl, patches = controller(port)
        for patch in patches:
            self.addCleanup(patch.stop)
        return grbl, port

    def testStaysInStepWithGrbl(self):
        # $23 "ok", then the $H "ok" (only once homing has finished), then the
        # position query.  The next status query must read its own report: Idle,
        # not a stale "ok" (that was "state 'None'" in jog.py --home).
        grbl, port = self.run_([b"ok\r\n", b"ok\r\n", STATUS_IDLE, b"ok\r\n",
                                STATUS_IDLE, b"ok\r\n"])
        position = grbl.runHomingCycle()
        self.assertEqual((position.x, position.y, position.z), (-619.0, -602.0, -3.0))
        self.assertEqual(grbl.getGrblState(), "Idle")
        self.assertIn(b"$H\n", port.written)
        self.assertEqual(grbl.timeout, 30)  # restored afterwards

    def testWaitsLongEnoughForHoming(self):
        # The $H reply is read with a 120 s timeout, not the port's 30 s.
        grbl, port = self.run_([b"ok\r\n", b"ok\r\n", STATUS_IDLE, b"ok\r\n"])
        seen = []

        def readline(*args):
            seen.append(grbl.timeout)
            return port.readline()
        with mock.patch.object(serial.Serial, "readline", side_effect=readline):
            grbl.runHomingCycle()
        self.assertEqual(seen[1], 120)   # the read after "$H" was written

    def testAnAlarmRaises(self):
        grbl, port = self.run_([b"ok\r\n", b"ALARM:9\r\n", b"", b"ok\r\n"])
        with self.assertRaisesRegex(RuntimeError, r"Homing failed \(ALARM:9\)"):
            grbl.runHomingCycle()

    def testNoReplyRaises(self):
        grbl, port = self.run_([b"ok\r\n", b""])
        with self.assertRaisesRegex(RuntimeError, r"no reply within 120 s"):
            grbl.runHomingCycle()


if __name__ == "__main__":
    unittest.main()
