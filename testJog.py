#!/usr/bin/env python3
"""Unit tests for jog.py.  No evdev, no hardware, no sudo.

A FakeGrbl records send()/write() calls and simulates just enough of Grbl
1.1h: a $J moves toward its target, 0x85 stops it part-way after a few
status polls in state Jog, and (like soft limits with $20=1) a jog target
outside the travel box is rejected with error:15.  A fake evdev module with
real pipe file descriptors exercises runKeyboardJog's select/grab/ungrab.

Run: python testJog.py
"""

import io
import os
import re
import sys
import types
import unittest
from unittest import mock

import jog

# The 6060-style settings this repo's profile and GrblController assume:
# bottom-left (Z up) homing, 3 mm pull-off.
SETTINGS = {'$20': '1', '$22': '1', '$23': '3', '$27': '3.000',
            '$130': '420.000', '$131': '310.000', '$132': '110.000'}
HOME = {'X': -417.0, 'Y': -307.0, 'Z': -3.0}  # where $H leaves it: -$13x+$27 or -$27


class FakeGrbl:
    def __init__(self, settings=None, position=None, state='Idle', decelPolls=2, stopFraction=0.5):
        self.settings = dict(settings or SETTINGS)
        self.position = dict(position or HOME)
        self.state = state
        self.decelPolls = decelPolls      # status polls that still say Jog after a cancel
        self.stopFraction = stopFraction  # how far along the jog a cancel stops it
        self.sent = []
        self.written = []
        self.log = []
        self.start = None
        self.target = None
        self.pollsUntilIdle = 0
        self.cancelPending = False
        self.alarmOnCancel = False

    # --- the GrblController methods jog.py may use ---
    def getSettings(self):
        return dict(self.settings)

    def send(self, gcode):
        self.sent.append(gcode)
        self.log.append(('send', gcode))
        match = re.match(r'\$J=G91 G21 ((?:[XYZ]-?[\d.]+ )+)F[\d.]+$', gcode)
        if not match:
            return
        if self.cancelPending and self.state == 'Jog':
            raise AssertionError("$J sent before the cancelled jog had stopped")
        target = dict(self.position)
        for axis, value in re.findall(r'([XYZ])(-?[\d.]+)', match.group(1)):
            target[axis] += float(value)
        if self.settings.get('$20') == '1':
            for axis, setting in jog.TRAVEL_SETTINGS.items():
                if target[axis] > 0 or target[axis] < -float(self.settings[setting]):
                    raise RuntimeError("Error during send(): b'error:15\\r\\n'")
        self.start, self.target = dict(self.position), target
        self.state = 'Jog'

    def write(self, data):
        self.written.append(data)
        self.log.append(('write', data))
        if data == jog.JOG_CANCEL and self.state == 'Jog':
            self.cancelPending = True
            self.pollsUntilIdle = self.decelPolls
            for axis in jog.AXES:
                self.position[axis] = round(self.start[axis] + self.stopFraction
                                            * (self.target[axis] - self.start[axis]), 3)

    def getStatus(self, debug=False):
        if self.state == 'Jog' and self.cancelPending:
            if self.pollsUntilIdle <= 0:
                self.state = 'Alarm' if self.alarmOnCancel else 'Idle'
                self.cancelPending = False
            self.pollsUntilIdle -= 1
        self.log.append(('status', self.state))
        p = self.position
        return f"<{self.state}|MPos:{p['X']:.3f},{p['Y']:.3f},{p['Z']:.3f}|FS:0,0>\r\n"

    def getGrblState(self):
        return self.getStatus()[1:].split('|')[0].split(':')[0]

    # --- test helpers ---
    def finish(self):
        # The jog ran all the way to its target by itself.
        self.position = dict(self.target)
        self.state = 'Idle'

    def jogs(self):
        return [line for line in self.sent if line.startswith('$J=')]


def makeSession(grbl=None):
    grbl = grbl or FakeGrbl()
    messages = []
    session = jog.JogSession(grbl, jog.softLimitBounds(grbl.settings), out=messages.append,
                             sleep=lambda seconds: None)
    return grbl, session, messages


class JogKeyStateTests(unittest.TestCase):
    def setUp(self):
        self.keys = jog.JogKeyState()

    def press(self, *names):
        commands = []
        for name in names:
            commands += self.keys.handleKey(name, True)
        return commands

    def release(self, *names):
        commands = []
        for name in names:
            commands += self.keys.handleKey(name, False)
        return commands

    def testEachArrowStartsOneAxisJog(self):
        expected = {'RIGHT': (1, 0, 0), 'LEFT': (-1, 0, 0), 'UP': (0, 1, 0), 'DOWN': (0, -1, 0)}
        for key, direction in expected.items():
            keys = jog.JogKeyState()
            self.assertEqual(keys.handleKey(key, True), [('jog', direction, 1000)], key)
            self.assertEqual(keys.handleKey(key, False), [('cancel',)], key)

    def testShiftUpDownMoveZAtZFeed(self):
        self.assertEqual(self.press('LEFTSHIFT', 'UP'), [('jog', (0, 0, 1), 300)])
        self.assertEqual(self.release('UP'), [('cancel',)])
        self.assertEqual(self.press('DOWN'), [('jog', (0, 0, -1), 300)])

    def testSecondKeyGivesOneDiagonalAndReleaseGoesBack(self):
        self.assertEqual(self.press('RIGHT'), [('jog', (1, 0, 0), 1000)])
        self.assertEqual(self.press('UP'), [('cancel',), ('jog', (1, 1, 0), 1000)])
        self.assertEqual(self.release('UP'), [('cancel',), ('jog', (1, 0, 0), 1000)])
        self.assertEqual(self.release('RIGHT'), [('cancel',)])

    def testCtrlMidJogReissuesAtATenth(self):
        self.press('LEFT')
        self.assertEqual(self.press('LEFTCTRL'), [('cancel',), ('jog', (-1, 0, 0), 100)])
        self.assertEqual(self.release('LEFTCTRL'), [('cancel',), ('jog', (-1, 0, 0), 1000)])

    def testCtrlHeldBeforeArrowCreepsFromTheStart(self):
        self.assertEqual(self.press('RIGHTCTRL'), [])  # nothing to move yet
        self.assertEqual(self.press('RIGHTSHIFT', 'DOWN'), [('jog', (0, 0, -1), 30)])

    def testShiftPressedWhileUpHeldSwitchesYToZ(self):
        self.press('UP')
        self.assertEqual(self.press('LEFTSHIFT'), [('cancel',), ('jog', (0, 0, 1), 300)])
        self.assertEqual(self.release('LEFTSHIFT'), [('cancel',), ('jog', (0, 1, 0), 1000)])

    def testOppositeKeysStandStill(self):
        self.press('LEFT')
        self.assertEqual(self.press('RIGHT'), [('cancel',)])
        self.assertEqual(self.release('LEFT'), [('jog', (1, 0, 0), 1000)])

    def testDiagonalInvolvingZUsesZFeed(self):
        self.assertEqual(self.press('LEFTSHIFT', 'UP', 'RIGHT'),
                         [('jog', (0, 0, 1), 300), ('cancel',), ('jog', (1, 0, 1), 300)])

    def testEnterAndEscEndJogMode(self):
        self.press('RIGHT')
        self.assertEqual(self.press('ENTER'), [('cancel',), ('accept',)])
        self.assertEqual(jog.JogKeyState().handleKey('ESC', True), [('abort',)])

    def testSpeedsCanBeOverridden(self):
        keys = jog.JogKeyState(xyFeed=2000, zFeed=500, creepDivisor=4)
        self.assertEqual(keys.handleKey('UP', True), [('jog', (0, 1, 0), 2000)])
        self.assertEqual(keys.handleKey('LEFTCTRL', True), [('cancel',), ('jog', (0, 1, 0), 500)])
        keys.handleKey('LEFTSHIFT', True)
        self.assertEqual(keys.active, ((0, 0, 1), 125))

    def testDefaultsAreModuleConstants(self):
        self.assertEqual((jog.XY_FEED, jog.Z_FEED, jog.CREEP_DIVISOR), (1000, 300, 10))


class SoftLimitTests(unittest.TestCase):
    def testBoundsForBottomLeftZUpHoming(self):
        bounds = jog.softLimitBounds(SETTINGS)
        self.assertEqual(bounds['X'], (-417.0, 0.0))   # homes negative: home end inset by $27
        self.assertEqual(bounds['Y'], (-307.0, 0.0))
        self.assertEqual(bounds['Z'], (-110.0, -3.0))  # homes positive (up)

    def testBoundsForTopRightHoming(self):
        bounds = jog.softLimitBounds(dict(SETTINGS, **{'$23': '0'}))
        self.assertEqual(bounds['X'], (-420.0, -3.0))
        self.assertEqual(bounds['Y'], (-310.0, -3.0))

    def testHomedPositionIsTheHomeEndBound(self):
        # GrblController's own comment shows MPos -417,-307,-3 after a bottom-left home.
        bounds = jog.softLimitBounds(SETTINGS)
        self.assertEqual([bounds[a][0] for a in 'XY'] + [bounds['Z'][1]], [HOME['X'], HOME['Y'], HOME['Z']])

    def testPlanRunsToTheFarEdge(self):
        gcode, axes = jog.planJog((1, 0, 0), 1000, HOME, jog.softLimitBounds(SETTINGS))
        self.assertEqual(gcode, "$J=G91 G21 X416.990 F1000")
        self.assertEqual(axes, ('X',))

    def testPlanTowardHomeAtHomeIsNothing(self):
        self.assertIsNone(jog.planJog((-1, 0, 0), 1000, HOME, jog.softLimitBounds(SETTINGS)))

    def testPlanZDown(self):
        gcode, _ = jog.planJog((0, 0, -1), 30.0, HOME, jog.softLimitBounds(SETTINGS))
        self.assertEqual(gcode, "$J=G91 G21 Z-106.990 F30")

    def testDiagonalStopsWhereTheRayLeavesTheBox(self):
        gcode, axes = jog.planJog((1, 1, 0), 1000, HOME, jog.softLimitBounds(SETTINGS))
        self.assertEqual(gcode, "$J=G91 G21 X306.990 Y306.990 F1000")
        self.assertEqual(axes, ('X', 'Y'))

    def testAxisAtItsBoundIsDropped(self):
        position = dict(HOME, Y=-0.01)
        gcode, axes = jog.planJog((1, 1, 0), 1000, position, jog.softLimitBounds(SETTINGS))
        self.assertEqual(gcode, "$J=G91 G21 X416.990 F1000")
        self.assertEqual(axes, ('X',))

    def testPlannedTargetPassesGrblsOwnCheck(self):
        # Whatever planJog sends must not trip error:15 from an awkward position.
        grbl = FakeGrbl(position={'X': -0.512, 'Y': -309.987, 'Z': -55.5555})
        bounds = jog.softLimitBounds(SETTINGS)
        for direction in [(1, 0, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1), (1, -1, 0), (-1, 1, 1)]:
            planned = jog.planJog(direction, 1000, grbl.position, bounds)
            if planned:
                grbl.state = 'Idle'
                grbl.send(planned[0])  # raises on error:15

    def testParseStatus(self):
        self.assertEqual(jog.parseStatus("<Jog|MPos:1.000,-2.500,3.000|FS:0,0>\r\n")[:2],
                         ('Jog', {'X': 1.0, 'Y': -2.5, 'Z': 3.0}))
        state, mpos, wco, wpos = jog.parseStatus("<Hold:0|WPos:1,2,3|WCO:-10,-20,-30>")
        self.assertEqual((state, mpos, wco['Y'], wpos['Z']), ('Hold', None, -20.0, 3.0))
        self.assertEqual(jog.parseStatus(None), (None, None, None, None))


class ReadyToJogTests(unittest.TestCase):
    def testIdleAndHomedIsReady(self):
        self.assertEqual(jog.checkReadyToJog(FakeGrbl(), SETTINGS, homed=True),
                         jog.softLimitBounds(SETTINGS))

    def testRefusesAlarm(self):
        with self.assertRaisesRegex(jog.JogRefused, "state 'Alarm'"):
            jog.checkReadyToJog(FakeGrbl(state='Alarm'), SETTINGS, homed=True)

    def testRefusesNotHomed(self):
        with self.assertRaisesRegex(jog.JogRefused, "not been homed"):
            jog.checkReadyToJog(FakeGrbl(), SETTINGS, homed=False)

    def testRefusesHomingDisabled(self):
        with self.assertRaisesRegex(jog.JogRefused, r"\$22"):
            jog.checkReadyToJog(FakeGrbl(), dict(SETTINGS, **{'$22': '0'}), homed=True)

    def testRefusesPositionOutsideTravel(self):
        grbl = FakeGrbl(position={'X': 12.0, 'Y': -5.0, 'Z': -3.0})
        with self.assertRaisesRegex(jog.JogRefused, "outside the travel range"):
            jog.checkReadyToJog(grbl, SETTINGS, homed=True)


class JogSessionTests(unittest.TestCase):
    def drive(self, session, keys, *events):
        results = []
        for name, pressed in events:
            results.append(jog.dispatchKey(keys, session, name, pressed))
        return results

    def testPressAndReleaseSendsJogThenCancel(self):
        grbl, session, _ = makeSession()
        self.drive(session, jog.JogKeyState(), ('RIGHT', True), ('RIGHT', False))
        self.assertEqual(grbl.jogs(), ["$J=G91 G21 X416.990 F1000"])
        self.assertEqual(grbl.written, [jog.JOG_CANCEL])
        self.assertEqual(grbl.state, 'Idle')  # waited for the deceleration to finish

    def testNextJogWaitsForCancelledJogToStop(self):
        grbl, session, _ = makeSession(FakeGrbl(decelPolls=5))
        self.drive(session, jog.JogKeyState(), ('RIGHT', True), ('UP', True))
        # FakeGrbl raises if the diagonal arrives while still in Jog; check order too.
        cancelAt = grbl.log.index(('write', jog.JOG_CANCEL))
        diagonalAt = next(i for i, entry in enumerate(grbl.log)
                          if entry[0] == 'send' and 'Y' in entry[1])
        self.assertIn(('status', 'Idle'), grbl.log[cancelAt:diagonalAt])
        # Cancelled half-way along X, at X-208.505: the diagonal is planned from there,
        # and X's 208.495 mm of room is the smaller.
        self.assertEqual(grbl.jogs()[1], "$J=G91 G21 X208.495 Y208.495 F1000")

    def testReleasingOneOfTwoKeysGoesBackToOneAxis(self):
        grbl, session, _ = makeSession()
        self.drive(session, jog.JogKeyState(), ('RIGHT', True), ('UP', True), ('UP', False))
        self.assertTrue(re.fullmatch(r"\$J=G91 G21 X[\d.]+ F1000", grbl.jogs()[-1]))
        self.assertEqual(grbl.written, [jog.JOG_CANCEL, jog.JOG_CANCEL])

    def testCtrlReissuesAtCreepSpeed(self):
        grbl, session, _ = makeSession()
        self.drive(session, jog.JogKeyState(), ('LEFTSHIFT', True), ('DOWN', True), ('LEFTCTRL', True))
        self.assertEqual(grbl.jogs()[0], "$J=G91 G21 Z-106.990 F300")
        self.assertTrue(grbl.jogs()[1].endswith(" F30"))

    def testAtTheLimitNothingIsSent(self):
        grbl, session, messages = makeSession()
        self.drive(session, jog.JogKeyState(), ('LEFT', True))
        self.assertEqual(grbl.jogs(), [])
        self.assertIn("At the travel limit", messages[0])

    def testDiagonalSlidesAlongTheEdgeItReaches(self):
        grbl, session, _ = makeSession()
        self.drive(session, jog.JogKeyState(), ('RIGHT', True), ('UP', True))
        grbl.finish()   # X (cancelled half-way, so nearer its edge) reached its far edge
        session.poll()
        self.assertEqual(grbl.jogs()[-1], "$J=G91 G21 Y98.495 F1000")
        grbl.finish()   # reached Y's far edge too
        session.poll()
        session.poll()
        self.assertEqual(len(grbl.jogs()), 3)  # nothing more to re-plan
        self.assertIsNone(session.running)

    def testPollWhileStillJoggingDoesNothing(self):
        grbl, session, _ = makeSession()
        self.drive(session, jog.JogKeyState(), ('RIGHT', True))
        session.poll()
        self.assertEqual(len(grbl.jogs()), 1)

    def testEnterCancelsAndAccepts(self):
        grbl, session, _ = makeSession()
        results = self.drive(session, jog.JogKeyState(), ('DOWN', True), ('ENTER', True))
        self.assertEqual(results, [None, jog.ACCEPTED])
        self.assertEqual(grbl.written, [jog.JOG_CANCEL])

    def testEscAborts(self):
        _, session, _ = makeSession()
        self.assertEqual(self.drive(session, jog.JogKeyState(), ('ESC', True)), [jog.ABORTED])

    def testAlarmDuringCancelRaises(self):
        grbl, session, _ = makeSession()
        grbl.alarmOnCancel = True
        with self.assertRaises(jog.JogError):
            self.drive(session, jog.JogKeyState(), ('RIGHT', True), ('RIGHT', False))

    def testPositionFromWPosPlusWco(self):
        class WPosGrbl(FakeGrbl):
            reports = iter(["<Idle|WPos:1,2,3|FS:0,0>", "<Idle|WPos:1,2,3|WCO:-10,-20,-30>"])

            def getStatus(self, debug=False):
                return next(self.reports)
        _, session, _ = makeSession(WPosGrbl())
        self.assertEqual(session.machinePosition(), {'X': -9.0, 'Y': -18.0, 'Z': -27.0})


# --- a fake evdev with real pipe fds, so select() works ---

KEY_CODES = {name: code for code, name in enumerate(
    ['KEY_LEFT', 'KEY_RIGHT', 'KEY_UP', 'KEY_DOWN', 'KEY_LEFTSHIFT', 'KEY_RIGHTSHIFT',
     'KEY_LEFTCTRL', 'KEY_RIGHTCTRL', 'KEY_ENTER', 'KEY_KPENTER', 'KEY_ESC', 'KEY_A'], start=100)}
EV_KEY = 1


class FakeDevice:
    def __init__(self, keys, events=(), failOnRead=False):
        self.readFd, self.writeFd = os.pipe()
        self.fd = self.readFd
        self.keys = [KEY_CODES[k] for k in keys]
        self.events = [types.SimpleNamespace(type=EV_KEY, code=KEY_CODES[k], value=v) for k, v in events]
        self.failOnRead = failOnRead
        self.grabbed = self.ungrabbed = self.closed = False
        if events or failOnRead:
            os.write(self.writeFd, b"x")

    def capabilities(self):
        return {EV_KEY: self.keys}

    def active_keys(self):
        return []

    def grab(self):
        self.grabbed = True

    def ungrab(self):
        self.ungrabbed = True

    def read(self):
        os.read(self.readFd, 4096)
        if self.failOnRead:
            raise OSError("device unplugged")
        events, self.events = self.events, []
        return events

    def close(self):
        if not self.closed:
            os.close(self.readFd)
            os.close(self.writeFd)
        self.closed = True


def fakeEvdev(devices):
    return types.SimpleNamespace(
        ecodes=types.SimpleNamespace(ecodes=KEY_CODES, EV_KEY=EV_KEY),
        list_devices=lambda: list(devices),
        InputDevice=lambda path: devices[path])


ARROWS = ['KEY_LEFT', 'KEY_RIGHT', 'KEY_UP', 'KEY_DOWN', 'KEY_ENTER', 'KEY_ESC']


class RunKeyboardJogTests(unittest.TestCase):
    def run_(self, devices, grbl=None, homed=True):
        grbl = grbl or FakeGrbl()
        with mock.patch.object(jog, 'importEvdev', return_value=fakeEvdev(devices)):
            result = jog.runKeyboardJog(grbl, homed, out=lambda text: None)
        return grbl, result

    def testJogThenEnter(self):
        keyboard = FakeDevice(ARROWS, [('KEY_RIGHT', 1), ('KEY_RIGHT', 2), ('KEY_RIGHT', 0),
                                       ('KEY_A', 1), ('KEY_ENTER', 1)])
        mouse = FakeDevice(['KEY_A'])
        grbl, result = self.run_({'/dev/input/event0': keyboard, '/dev/input/event1': mouse})
        self.assertEqual(result, jog.ACCEPTED)
        self.assertEqual(grbl.jogs(), ["$J=G91 G21 X416.990 F1000"])  # repeat (value 2) ignored
        self.assertTrue(keyboard.grabbed and keyboard.ungrabbed and keyboard.closed)
        self.assertFalse(mouse.grabbed)   # no arrow keys: not watched
        self.assertTrue(mouse.closed)
        self.assertEqual(grbl.written[-1], jog.JOG_CANCEL)  # finally: cancel even when stopped

    def testEscReturnsAborted(self):
        keyboard = FakeDevice(ARROWS, [('KEY_ESC', 1)])
        _, result = self.run_({'/dev/input/event0': keyboard})
        self.assertEqual(result, jog.ABORTED)

    def testExceptionStillCancelsAndUngrabs(self):
        keyboard = FakeDevice(ARROWS, failOnRead=True)
        grbl = FakeGrbl()
        with self.assertRaises(OSError):
            self.run_({'/dev/input/event0': keyboard}, grbl)
        self.assertTrue(keyboard.ungrabbed)
        self.assertEqual(grbl.written, [jog.JOG_CANCEL])

    def testNoKeyboardRefuses(self):
        with self.assertRaisesRegex(jog.JogRefused, "No readable keyboard"):
            self.run_({'/dev/input/event1': FakeDevice(['KEY_A'])})

    def testNotHomedRefusesWithoutGrabbing(self):
        keyboard = FakeDevice(ARROWS)
        with self.assertRaises(jog.JogRefused):
            self.run_({'/dev/input/event0': keyboard}, homed=False)
        self.assertFalse(keyboard.grabbed)
        keyboard.close()

    def testMissingEvdevRefuses(self):
        with mock.patch.dict(sys.modules, {'evdev': None}):  # import evdev -> ImportError
            with self.assertRaisesRegex(jog.JogRefused, "evdev"):
                jog.runKeyboardJog(FakeGrbl(), True)


class EnvironmentCheckTests(unittest.TestCase):
    GOOD = {'evdevImportable': True, 'user': 'alice', 'inGroupNow': True,
            'listedInGroup': True, 'readableDevice': True}

    def check(self, facts, answers=(), returncode=0, runError=None):
        output = []
        factSequence = facts if isinstance(facts, list) else [facts]
        completed = types.SimpleNamespace(returncode=returncode)
        with mock.patch.object(jog, 'gatherJogEnvironmentFacts', side_effect=factSequence), \
                mock.patch('builtins.input', side_effect=list(answers)) as fakeInput, \
                mock.patch.object(jog.subprocess, 'run', return_value=completed,
                                  side_effect=runError) as fakeRun:
            result = jog.checkJogEnvironment(out=output.append)
        return result, output, fakeInput, fakeRun

    def testAllGoodIsSilent(self):
        result, output, fakeInput, fakeRun = self.check(dict(self.GOOD))
        self.assertEqual(result, jog.ENVIRONMENT_OK)
        self.assertEqual(output, [])
        fakeInput.assert_not_called()
        fakeRun.assert_not_called()

    def testMissingEvdevDeclined(self):
        result, output, fakeInput, fakeRun = self.check(dict(self.GOOD, evdevImportable=False), [""])
        self.assertEqual(result, jog.UNAVAILABLE)
        self.assertIn(f"{sys.executable} -m pip install evdev", "\n".join(output))
        fakeInput.assert_called_once_with("Run it now? [y/N] ")
        fakeRun.assert_not_called()

    def testMissingEvdevInstalledOnYes(self):
        facts = [dict(self.GOOD, evdevImportable=False), dict(self.GOOD)]
        result, output, _, fakeRun = self.check(facts, ["y"])
        self.assertEqual(result, jog.ENVIRONMENT_OK)
        # Inherited stdin/stdout/stderr: no capture or pipe arguments at all.
        fakeRun.assert_called_once_with([sys.executable, '-m', 'pip', 'install', 'evdev'])
        self.assertIn("Exit status 0.", output)

    def testFailedInstallIsReported(self):
        result, output, _, _ = self.check(dict(self.GOOD, evdevImportable=False), ["yes"], returncode=1)
        self.assertEqual(result, jog.UNAVAILABLE)
        self.assertIn("Exit status 1.", output)
        self.assertTrue(any("failed" in line for line in output))

    def testNotInInputGroupDeclined(self):
        facts = dict(self.GOOD, inGroupNow=False, listedInGroup=False, readableDevice=False)
        result, output, _, fakeRun = self.check(facts, ["n"])
        self.assertEqual(result, jog.UNAVAILABLE)
        self.assertIn("    sudo usermod -aG input $USER", output)
        fakeRun.assert_not_called()

    def testUsermodOnYesNeedsRelogin(self):
        facts = dict(self.GOOD, inGroupNow=False, listedInGroup=False, readableDevice=False)
        result, output, _, fakeRun = self.check(facts, ["y"])
        self.assertEqual(result, jog.UNAVAILABLE_UNTIL_RELOGIN)
        fakeRun.assert_called_once_with(['sudo', 'usermod', '-aG', 'input', 'alice'])
        self.assertTrue(any("log out and back in" in line and "unavailable" in line for line in output))

    def testAlreadyAddedButNotLoggedInAgain(self):
        facts = dict(self.GOOD, inGroupNow=False, listedInGroup=True, readableDevice=False)
        result, output, fakeInput, fakeRun = self.check(facts)
        self.assertEqual(result, jog.UNAVAILABLE_UNTIL_RELOGIN)
        self.assertEqual(len(output), 1)
        self.assertIn("log out", output[0])
        fakeInput.assert_not_called()
        fakeRun.assert_not_called()

    def testEndOfInputCountsAsNo(self):
        result, _, _, fakeRun = self.check(dict(self.GOOD, evdevImportable=False), [EOFError()])
        self.assertEqual(result, jog.UNAVAILABLE)
        fakeRun.assert_not_called()

    def testCommandThatCannotStartIsReported(self):
        facts = dict(self.GOOD, inGroupNow=False, listedInGroup=False, readableDevice=False)
        result, output, _, _ = self.check(facts, ["y"], runError=FileNotFoundError("sudo"))
        self.assertEqual(result, jog.UNAVAILABLE)
        self.assertTrue(any("Could not run it" in line for line in output))

    def testBothProblemsAskTwice(self):
        facts = [dict(self.GOOD, evdevImportable=False, inGroupNow=False, listedInGroup=False,
                      readableDevice=False), dict(self.GOOD)]
        result, _, fakeInput, fakeRun = self.check(facts, ["y", "y"])
        self.assertEqual(result, jog.UNAVAILABLE_UNTIL_RELOGIN)
        self.assertEqual(fakeInput.call_count, 2)
        self.assertEqual(fakeRun.call_count, 2)

    def testGatherFactsRunsHere(self):
        facts = jog.gatherJogEnvironmentFacts()  # real, read-only
        self.assertEqual(set(facts), set(self.GOOD))


if __name__ == "__main__":
    unittest.main(verbosity=2)
