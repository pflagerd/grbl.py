"""Tests for the GrblController startup handshake. No CNC machine or serial port is needed.

Run with:    python3 -m unittest testGrblControllerStartup -v

The cases come from https://github.com/pflagerd/grbl.py/issues/13. The controller's side of each
conversation is scripted from what the Grbl 1.1h firmware source says it sends.
"""
import inspect
import unittest
import warnings
from unittest import mock

import serial

from GrblController import (GrblController, GrblLimitSwitchError, GrblSettingsError, GrblStartupError,
                            GrblStateError, GrblVersionError, MachineProfile, classifyLine, limitPins,
                            parseStatusReport)

blank = b"\r\n"
welcome = b"Grbl 1.1h ['$' for help]\r\n"
unlockHint = b"[MSG:'$H'|'$X' to unlock]\r\n"
checkLimits = b"[MSG:Check Limits]\r\n"
alarmStatus = b"<Alarm|MPos:0.000,0.000,0.000|Bf:15,127|FS:0,0|WCO:-405.000,-299.000,-84.000>\r\n"
idleStatus = b"<Idle|MPos:-417.000,-307.000,-3.000|Bf:15,127|FS:0,0>\r\n"
unlockReply = [b"[MSG:Caution: Unlocked]\r\n", b"ok\r\n"]
buildInfoReply = [b"[VER:1.1h.20190830:]\r\n", b"[OPT:V,15,128]\r\n", b"ok\r\n"]
readTimeout = None  # In a greeting: one read that times out with nothing received.


class ScriptedGrblController(GrblController):
    """A GrblController that talks to a script instead of a serial port.

    greeting: what the controller sends unprompted, in order. An entry of None is one read timeout.
    replies:  {bytes written by the host: [reply, reply, ...]} where each reply is a list of lines.
              The first write gets the first reply, the second write the second, and the last reply repeats.
    Time is faked: every read that times out advances the clock by the current read timeout.

    If the greeting contains a welcome message, the controller is taken to be resetting until that line
    has been read: a '?' written before then is lost, as it is on a real board, and gets no reply.
    """
    is_open = False  # So that serial.Serial.close(), called from __del__, does nothing.
    timeout = 30     # Plain attribute replacing serial.Serial's property, which needs an open port.

    def __init__(self, greeting=(), replies=None, answerOk=False):
        # Deliberately does not call GrblController.__init__(): no serial port is opened.
        # answerOk: any line written that has no scripted reply is answered with 'ok'.
        self.portDeviceName = "scripted-port"
        self.answerOk = answerOk
        self.portClosed = False
        self.incoming = list(greeting)
        self.replies = {written: list(replyList) for written, replyList in (replies or {}).items()}
        self.written = []
        self.writtenAt = []  # (clock, bytes) for each write
        self.clock = 0.0
        self.resetting = any(line is not readTimeout and b"for help]" in line for line in self.incoming)

    def _now(self):
        return self.clock

    def write(self, data):
        self.written.append(data)
        self.writtenAt.append((self.clock, data))
        if self.resetting and data == b"?":
            return len(data)  # lost: the controller has not finished resetting
        replyList = self.replies.get(data)
        if replyList:
            reply = replyList.pop(0) if len(replyList) > 1 else replyList[0]
            self.incoming.extend(reply)
        elif self.answerOk and data.endswith(b"\n"):
            self.incoming.append(b"ok\r\n")
        return len(data)

    def close(self):
        self.portClosed = True

    def readline(self):
        if self.incoming:
            line = self.incoming.pop(0)
            if line is not readTimeout:
                if b"for help]" in line:
                    self.resetting = False
                return line
        self.clock += self.timeout
        return b""


def scripted(greeting, status=None, extraReplies=None, answerOk=False):
    """A scripted controller that answers $X, and answers '?' with the given status report (if any)."""
    replies = {b"$X\n": [unlockReply]}
    if status is not None:
        replies[b"?"] = [[status]]
    replies.update(extraReplies or {})
    return ScriptedGrblController(greeting, replies, answerOk)


def settingsReply(settings):
    """What Grbl sends in answer to $$: one '$n=value' line per setting, then 'ok'."""
    return [f"{key}={value}\r\n".encode() for key, value in settings.items()] + [b"ok\r\n"]


def scriptedSerialPort():
    """Patches pyserial so that the real GrblController.__init__() can run on a ScriptedGrblController:
    opening the port does nothing, and super().write()/super().readline() reach the script."""
    patches = [mock.patch.object(serial.Serial, '__init__', lambda self, *args, **kwargs: None),
               mock.patch.object(serial.Serial, 'write', ScriptedGrblController.write),
               mock.patch.object(serial.Serial, 'readline', ScriptedGrblController.readline)]
    for patch in patches:
        patch.start()
    return patches


def stopPatches(patches):
    for patch in patches:
        patch.stop()


class StartupHandshakeTest(unittest.TestCase):

    # --- The eleven cases listed in issue 13 ---

    def testNormalPowerUpUnlocks(self):
        controller = scripted([blank, welcome, unlockHint], alarmStatus)
        controller._handshake()
        # The first '?' is lost in the reset; the one sent when the welcome arrives is answered.
        self.assertEqual(controller.written, [b"?", b"?", b"$X\n"])
        self.assertEqual(controller.grblVersion, "1.1h")
        self.assertEqual(controller.startupState, "Alarm")
        self.assertEqual(controller.startupMessages, ["'$H'|'$X' to unlock"])

    def testLimitSwitchEngagedRaisesAndDoesNotUnlock(self):
        controller = scripted([blank, welcome, checkLimits, unlockHint], alarmStatus)
        with self.assertRaises(GrblLimitSwitchError) as raised:
            controller._handshake()
        self.assertNotIn(b"$X\n", controller.written)
        self.assertIn("Check Limits", str(raised.exception))
        self.assertIn("scripted-port", str(raised.exception))

    def testSoftResetWhileIdleNeedsNoUnlock(self):
        controller = scripted([blank, welcome], idleStatus)
        controller._handshake()
        self.assertEqual(controller.written, [b"?", b"?"])
        self.assertEqual(controller.startupState, "Idle")

    def testStoredStartupLineIsRecorded(self):
        controller = scripted([blank, welcome, b">G54:ok\r\n"], idleStatus)
        with warnings.catch_warnings():
            warnings.simplefilter("error")  # a successful startup line must not warn
            controller._handshake()
        self.assertEqual(controller.startupLineResults, [("G54", "ok")])

    def testFailedStartupLineWarnsButSucceeds(self):
        controller = scripted([blank, welcome, b">G99:error:20\r\n"], idleStatus)
        with self.assertWarns(UserWarning) as warned:
            controller._handshake()
        self.assertEqual(controller.startupLineResults, [("G99", "error:20")])
        self.assertIn("error:20", str(warned.warning))
        self.assertEqual(controller.startupState, "Idle")

    def testNoResetOnOpenStillConnects(self):
        controller = scripted([], idleStatus, {b"$I\n": [buildInfoReply]})
        controller._handshake()
        self.assertEqual(controller.written, [b"?", b"$I\n"])
        self.assertEqual(controller.grblVersion, "1.1h")
        self.assertEqual(controller.startupState, "Idle")
        self.assertLess(controller.clock, 1.0)  # the first '?' is answered: no waiting for a welcome message

    def testTotalSilenceFailsWithinTheDeadline(self):
        controller = scripted([])
        with self.assertRaises(GrblStartupError) as raised:
            controller._handshake()
        self.assertLessEqual(controller.clock, 10.5)  # not 30 seconds per line
        self.assertIn("scripted-port", str(raised.exception))
        self.assertIn("Received nothing at all", str(raised.exception))

    def testOtherGrbl11LetterIsAccepted(self):
        controller = scripted([blank, b"Grbl 1.1f ['$' for help]\r\n", unlockHint], alarmStatus)
        controller._handshake()
        self.assertEqual(controller.grblVersion, "1.1f")

    def testWrongMajorVersionRaisesQuotingTheLine(self):
        controller = scripted([blank, b"Grbl 0.9j ['$' for help]\r\n"])
        with self.assertRaises(GrblVersionError) as raised:
            controller._handshake()
        self.assertIn("Grbl 0.9j ['$' for help]", str(raised.exception))
        self.assertLess(controller.clock, 1.0)  # raised as soon as the line is seen, without waiting
        self.assertEqual(controller.written, [b"?"])  # only the first probe, lost in the reset; no $X

    def testStrayBytesBeforeTheGreetingAreTolerated(self):
        for greeting in ([b"\xff\x00\r\n", welcome, unlockHint],
                         [b"\xff\x00Grbl 1.1h ['$' for help]\r\n", unlockHint]):
            with self.subTest(greeting=greeting):
                controller = scripted(greeting, alarmStatus)
                controller._handshake()
                self.assertEqual(controller.grblVersion, "1.1h")
                self.assertIn(b"$X\n", controller.written)

    def testLimitPinInTheStatusReportRefusesToUnlock(self):
        # Not reset by opening the port, so no [MSG:Check Limits]; but Pn: shows the X limit pin active.
        controller = scripted([], b"<Alarm|MPos:0.000,0.000,0.000|Bf:15,128|FS:0,0|Pn:X>\r\n",
                              {b"$I\n": [buildInfoReply]})
        with self.assertRaises(GrblLimitSwitchError) as raised:
            controller._handshake()
        self.assertNotIn(b"$X\n", controller.written)
        self.assertIn("limit pin X", str(raised.exception))

    def testProbePinIsNotALimitSwitch(self):
        controller = scripted([blank, welcome, unlockHint], b"<Alarm|MPos:0.000,0.000,0.000|FS:0,0|Pn:P>\r\n")
        controller._handshake()
        self.assertIn(b"$X\n", controller.written)

    def testDoorOpenRaisesNamingTheState(self):
        controller = scripted([blank, welcome], b"<Door:0|MPos:0.000,0.000,0.000|FS:0,0>\r\n")
        with self.assertRaises(GrblStateError) as raised:
            controller._handshake()
        self.assertIn("Door", str(raised.exception))
        self.assertNotIn(b"$X\n", controller.written)

    # --- Further cases ---

    def testProbeIsRetriedEverySecondUntilTheWelcomeArrives(self):
        # The board takes 6 s to reset. Each '?' sent before then is lost, so one is sent every second,
        # and another as soon as the welcome message arrives.
        greeting = [readTimeout] * 12 + [blank, welcome, unlockHint]
        controller = scripted(greeting, alarmStatus)
        controller._handshake()
        probeTimes = [clock for clock, data in controller.writtenAt if data == b"?"]
        self.assertEqual(probeTimes, [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 6.0])
        self.assertEqual(controller.written[-1], b"$X\n")
        self.assertEqual(controller.startupState, "Alarm")

    def testSilentControllerIsProbedOncePerSecondUntilTheDeadline(self):
        controller = scripted([])
        with self.assertRaises(GrblStartupError):
            controller._handshake()
        probeTimes = [clock for clock, data in controller.writtenAt if data == b"?"]
        self.assertEqual(probeTimes, [float(second) for second in range(10)])

    def testNoResetAndBusyRaisesStateError(self):
        controller = scripted([], b"<Run|MPos:1.000,2.000,3.000|FS:400,1000>\r\n")
        with self.assertRaises(GrblStateError) as raised:
            controller._handshake()
        self.assertIn("Run", str(raised.exception))
        self.assertNotIn(b"$I\n", controller.written)  # $I is only accepted in Idle and Alarm

    def testNoResetAndNoVersionReplyRaisesVersionError(self):
        controller = scripted([], idleStatus)  # nothing answers $I
        with self.assertRaises(GrblVersionError):
            controller._handshake()
        self.assertLessEqual(controller.clock, 10.5)

    def testResetOnConnectSendsCtrlXFirst(self):
        controller = scripted([blank, welcome, unlockHint], alarmStatus)
        controller._handshake(resetOnConnect=True)
        self.assertEqual(controller.written, [b"\x18", b"?", b"?", b"$X\n"])

    def testCtrlXIsNotSentByDefault(self):
        controller = scripted([])
        with self.assertRaises(GrblStartupError):
            controller._handshake()
        self.assertNotIn(b"\x18", controller.written)

    def testLineSplitAcrossTwoReadsIsReassembled(self):
        controller = scripted([b"Grbl 1.1h ['$' ", b"for help]\r\n", unlockHint], alarmStatus)
        controller._handshake()
        self.assertEqual(controller.grblVersion, "1.1h")
        self.assertEqual(controller.startupTranscript[0], "Grbl 1.1h ['$' for help]")

    def testReadTimeoutIsRestored(self):
        controller = scripted([blank, welcome, unlockHint], alarmStatus)
        controller._handshake()
        self.assertEqual(controller.timeout, 30)

        controller = scripted([])
        with self.assertRaises(GrblStartupError):
            controller._handshake()
        self.assertEqual(controller.timeout, 30)

    def testShorterDeadlineIsHonoured(self):
        controller = scripted([])
        with self.assertRaises(GrblStartupError):
            controller._handshake(deadlineSeconds=2.0)
        self.assertLessEqual(controller.clock, 2.5)

    def testConstructorNoLongerCallsSysExit(self):
        self.assertNotIn("sys.exit", inspect.getsource(GrblController.__init__))
        self.assertNotIn("sys.exit", inspect.getsource(GrblController._handshake))


class ConstructorTest(unittest.TestCase):
    """The real GrblController.__init__(), run against a script: what happens after the handshake."""

    def setUp(self):
        self.addCleanup(stopPatches, scriptedSerialPort())

    def profile(self, **expectedSettings):
        return MachineProfile(name="test-machine", portDeviceName="profile-port",
                              expectedSettings=expectedSettings or {"$3": "0", "$23": "3"})

    def construct(self, machineSettings, **kwargs):
        settingsReplies = [settingsReply(settings) for settings in machineSettings]
        self.controller = scripted([blank, welcome, unlockHint], alarmStatus,
                                   {b"$$\n": settingsReplies} if settingsReplies else {}, answerOk=True)
        GrblController.__init__(self.controller, **kwargs)
        return self.controller

    def testSettingsAreVerifiedAfterTheHandshakeAndBeforeHoming(self):
        controller = self.construct([{"$3": "0", "$23": "3"}], profile=self.profile(), homeOnConnect=True)
        written = controller.written
        self.assertLess(written.index(b"$X\n"), written.index(b"$$\n"))
        self.assertLess(written.index(b"$$\n"), written.index(b"$H\n"))
        self.assertEqual(controller.portDeviceName, "profile-port")
        self.assertEqual(controller.grblVersion, "1.1h")

    def testUnsafeSettingRaisesBeforeAnythingIsConfiguredOrMoves(self):
        with self.assertRaises(GrblSettingsError) as raised:
            self.construct([{"$3": "0", "$23": "0"}], profile=self.profile(), homeOnConnect=True)
        self.assertIn("$23", str(raised.exception))
        written = self.controller.written
        self.assertNotIn(b"$H\n", written)
        self.assertFalse([data for data in written if data.startswith(b"$23=")])  # nothing overwrote it
        self.assertEqual(written[-1], b"$$\n")  # the settings check was the last thing done
        self.assertTrue(self.controller.portClosed)

    def testNonCriticalMismatchWarnsAndConnects(self):
        with self.assertWarns(UserWarning):
            self.construct([{"$110": "999.000"}], profile=self.profile(**{"$110": "500.000"}))
        self.assertFalse(self.controller.portClosed)

    def testApplySettingsCorrectsThenHomes(self):
        controller = self.construct([{"$3": "0", "$23": "0"}, {"$3": "0", "$23": "3"}],
                                    profile=self.profile(), applySettings=True, homeOnConnect=True)
        written = controller.written
        self.assertLess(written.index(b"$23=3\n"), written.index(b"$H\n"))
        self.assertEqual(written.count(b"$$\n"), 2)  # re-read after applying

    def testLimitSwitchStopsTheConstructorBeforeSettingsAreRead(self):
        controller = scripted([blank, welcome, checkLimits, unlockHint], alarmStatus, answerOk=True)
        with self.assertRaises(GrblLimitSwitchError):
            GrblController.__init__(controller, profile=self.profile(), homeOnConnect=True)
        self.assertNotIn(b"$$\n", controller.written)
        self.assertNotIn(b"$X\n", controller.written)
        self.assertTrue(controller.portClosed)

    def testWithoutAProfileNoSettingsAreRead(self):
        controller = self.construct([])
        self.assertNotIn(b"$$\n", controller.written)
        self.assertEqual(controller.portDeviceName, "/dev/ttyUSB0")
        self.assertIsNone(controller.profile)

    def testResetOnConnectGoesThroughSoftResetWithoutDraining(self):
        controller = scripted([blank, welcome, unlockHint], alarmStatus, answerOk=True)
        with mock.patch.object(GrblController, 'softReset', autospec=True,
                               side_effect=lambda self, drain=True: self.write(b"\x18")) as softReset:
            GrblController.__init__(controller, resetOnConnect=True)
        softReset.assert_called_once_with(controller, drain=False)
        self.assertEqual(controller.written[0], b"\x18")
        self.assertEqual(controller.startupTranscript[0], "Grbl 1.1h ['$' for help]")  # the banner was not drained


class AfterConnectTest(unittest.TestCase):
    """Methods that use super().write()/super().readline(), run against a script."""

    def setUp(self):
        self.addCleanup(stopPatches, scriptedSerialPort())

    def testGetMachineCoordinatesUpdatesOnlyWhenIdle(self):
        controller = ScriptedGrblController(replies={b"?\n": [[idleStatus, b"ok\r\n"], [alarmStatus, b"ok\r\n"]]})
        controller.machineCoordinates = None
        position = controller.getMachineCoordinates()
        self.assertEqual(str(position), "(-417.0, -307.0, -3.0)")
        self.assertIs(controller.getMachineCoordinates(), position)  # an Alarm report does not replace it

    def testTryHomingCycleReturnsTheAlarmAndUnlocksWhenNoSwitchIsEngaged(self):
        controller = ScriptedGrblController(replies={
            b"$H\n": [[b"ALARM:9\r\n"]],
            b"\x18": [[blank, welcome, unlockHint]],
            b"?\n": [[alarmStatus, b"ok\r\n"]],
            b"$X\n": [unlockReply]}, answerOk=True)
        self.assertEqual(controller.tryHomingCycle("3"), "9")
        self.assertIn(b"$X\n", controller.written)

    def testTryHomingCycleRefusesToUnlockOnAnEngagedSwitch(self):
        for afterReset, status in (([blank, welcome, checkLimits, unlockHint], alarmStatus),
                                   ([blank, welcome, unlockHint], b"<Alarm|MPos:0.000,0.000,0.000|Pn:Y>\r\n")):
            with self.subTest(status=status):
                controller = ScriptedGrblController(replies={
                    b"$H\n": [[b"ALARM:9\r\n"]],
                    b"\x18": [afterReset],
                    b"?\n": [[status, b"ok\r\n"]]}, answerOk=True)
                with self.assertRaises(GrblLimitSwitchError):
                    controller.tryHomingCycle("3")
                self.assertNotIn(b"$X\n", controller.written)


class LineParsingTest(unittest.TestCase):

    def testClassifyLine(self):
        cases = [
            (b"\r\n", "blank"),
            (b"Grbl 1.1h ['$' for help]\r\n", "welcome"),
            (b"[MSG:'$H'|'$X' to unlock]\r\n", "message"),
            (b"[MSG:Check Limits]\r\n", "message"),
            (b"[VER:1.1h.20190830:]\r\n", "buildInfo"),
            (b">G54:ok\r\n", "startupLine"),
            (b"ALARM:1\r\n", "alarm"),
            (b"<Idle|MPos:0.000,0.000,0.000|FS:0,0>\r\n", "status"),
            (b"ok\r\n", "ok"),
            (b"error:9\r\n", "error"),
            (b"[OPT:V,15,128]\r\n", "unknown"),
            (b"\xff\xfe garbage\r\n", "unknown"),
            ("Grbl 1.1h ['$' for help]", "welcome"),  # str is accepted as well as bytes
        ]
        for rawLine, expectedKind in cases:
            with self.subTest(rawLine=rawLine):
                self.assertEqual(classifyLine(rawLine)[0], expectedKind)

    def testClassifyLineReturnsStrippedText(self):
        self.assertEqual(classifyLine(b"  ok\r\n"), ("ok", "ok"))

    def testParseStatusReport(self):
        report = parseStatusReport("<Idle|MPos:-417.000,-307.000,-3.000|Bf:15,127|FS:0,0|WCO:-417.000,-307.000,-3.000>")
        self.assertEqual(report["state"], "Idle")
        self.assertIsNone(report["subState"])
        self.assertEqual(report["fields"]["MPos"], "-417.000,-307.000,-3.000")
        self.assertEqual(report["fields"]["Bf"], "15,127")
        self.assertEqual(report["fields"]["WCO"], "-417.000,-307.000,-3.000")

    def testParseStatusReportWithSubState(self):
        self.assertEqual(parseStatusReport("<Hold:0|MPos:0.000,0.000,0.000>")["state"], "Hold")
        self.assertEqual(parseStatusReport("<Hold:0|MPos:0.000,0.000,0.000>")["subState"], "0")
        self.assertEqual(parseStatusReport("<Door:3|MPos:0.000,0.000,0.000>")["state"], "Door")

    def testLimitPins(self):
        self.assertEqual(limitPins("XZP"), "XZ")
        self.assertEqual(limitPins("PD"), "")
        self.assertEqual(limitPins(None), "")

    def testParseStatusReportRejectsOtherText(self):
        self.assertIsNone(parseStatusReport("ok"))
        self.assertIsNone(parseStatusReport("[MSG:Check Limits]"))


if __name__ == "__main__":
    unittest.main()
