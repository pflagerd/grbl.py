"""Tests for the GrblController startup handshake. No CNC machine or serial port is needed.

Run with:    python3 -m unittest testGrblControllerStartup -v

The cases come from https://github.com/pflagerd/grbl.py/issues/13. The controller's side of each
conversation is scripted from what the Grbl 1.1h firmware source says it sends.
"""
import inspect
import unittest
import warnings

from GrblController import (GrblController, GrblLimitSwitchError, GrblStartupError, GrblStateError,
                            GrblVersionError, classifyLine, parseStatusReport)

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
    """
    is_open = False  # So that serial.Serial.close(), called from __del__, does nothing.
    timeout = 30     # Plain attribute replacing serial.Serial's property, which needs an open port.

    def __init__(self, greeting=(), replies=None):
        # Deliberately does not call GrblController.__init__(): no serial port is opened.
        self.portDeviceName = "scripted-port"
        self.incoming = list(greeting)
        self.replies = {written: list(replyList) for written, replyList in (replies or {}).items()}
        self.written = []
        self.clock = 0.0

    def _now(self):
        return self.clock

    def write(self, data):
        self.written.append(data)
        replyList = self.replies.get(data)
        if replyList:
            reply = replyList.pop(0) if len(replyList) > 1 else replyList[0]
            self.incoming.extend(reply)
        return len(data)

    def readline(self):
        if self.incoming:
            line = self.incoming.pop(0)
            if line is not readTimeout:
                return line
        self.clock += self.timeout
        return b""


def scripted(greeting, status=None, extraReplies=None):
    """A scripted controller that answers $X, and answers '?' with the given status report (if any)."""
    replies = {b"$X\n": [unlockReply]}
    if status is not None:
        replies[b"?"] = [[status]]
    replies.update(extraReplies or {})
    return ScriptedGrblController(greeting, replies)


class StartupHandshakeTest(unittest.TestCase):

    # --- The eleven cases listed in issue 13 ---

    def testNormalPowerUpUnlocks(self):
        controller = scripted([blank, welcome, unlockHint], alarmStatus)
        controller._handshake()
        self.assertEqual(controller.written, [b"?", b"$X\n"])
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
        self.assertEqual(controller.written, [b"?"])
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
        self.assertLess(controller.clock, 10.0)

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
        self.assertEqual(controller.written, [])

    def testStrayBytesBeforeTheGreetingAreTolerated(self):
        for greeting in ([b"\xff\x00\r\n", welcome, unlockHint],
                         [b"\xff\x00Grbl 1.1h ['$' for help]\r\n", unlockHint]):
            with self.subTest(greeting=greeting):
                controller = scripted(greeting, alarmStatus)
                controller._handshake()
                self.assertEqual(controller.grblVersion, "1.1h")
                self.assertIn(b"$X\n", controller.written)

    def testDoorOpenRaisesNamingTheState(self):
        controller = scripted([blank, welcome], b"<Door:0|MPos:0.000,0.000,0.000|FS:0,0>\r\n")
        with self.assertRaises(GrblStateError) as raised:
            controller._handshake()
        self.assertIn("Door", str(raised.exception))
        self.assertNotIn(b"$X\n", controller.written)

    # --- Further cases ---

    def testWelcomeArrivingLateGetsASecondStatusRequest(self):
        # The board takes 6 s to reset. The '?' sent at 5 s is lost in the reset, so another must follow the welcome.
        greeting = [readTimeout] * 12 + [blank, welcome, unlockHint]
        controller = scripted(greeting, extraReplies={b"?": [[], [alarmStatus]]})
        controller._handshake()
        self.assertEqual(controller.written, [b"?", b"?", b"$X\n"])
        self.assertEqual(controller.startupState, "Alarm")

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
        self.assertEqual(controller.written, [b"\x18", b"?", b"$X\n"])

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

    def testStaleStatusReportBeforeTheWelcomeIsNotTrusted(self):
        controller = scripted([idleStatus, blank, welcome, unlockHint], alarmStatus)
        controller._handshake()
        self.assertEqual(controller.startupState, "Alarm")

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

    def testParseStatusReportRejectsOtherText(self):
        self.assertIsNone(parseStatusReport("ok"))
        self.assertIsNone(parseStatusReport("[MSG:Check Limits]"))


if __name__ == "__main__":
    unittest.main()
