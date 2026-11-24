#!/usr/bin/env python3
"""Unit tests for jog.py.  No evdev, no hardware, no sudo.

A FakeGrbl records send()/write() calls and simulates just enough of Grbl
1.1h: a $J moves toward its target, a $J sent while one runs queues behind
it, 0x85 stops it part-way after a few status polls in state Jog, and (like
soft limits with $20=1) a jog target outside the travel box is rejected with
error:15.  A fake evdev module with real pipe file descriptors exercises
runKeyboardJog's select/grab/ungrab, and a scripted Timeline stands in for
select() and the clock, so holds and taps are timed without real sleeps.

The step-8 tests of continuous jogging use holdThreshold=0 (every press is a
hold at once); the step-10 tests use the real 250 ms threshold with `now`
passed in.

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
    def __init__(self, settings=None, position=None, state='Idle', decelPolls=2, stopFraction=0.5,
                 autoFinishPolls=None):
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
        self.autoFinishPolls = autoFinishPolls  # None: a jog runs until finish() or a cancel
        self.pollsUntilDone = None
        self.clock = None       # set to record when each line was sent
        self.sendTimes = []

    # --- the GrblController methods jog.py may use ---
    def getSettings(self):
        return dict(self.settings)

    def send(self, gcode):
        self.sent.append(gcode)
        self.log.append(('send', gcode))
        if self.clock:
            self.sendTimes.append(self.clock())
        match = re.match(r'\$J=G91 G21 ((?:[XYZ]-?[\d.]+ )+)F[\d.]+$', gcode)
        if not match:
            return
        if self.cancelPending and self.state == 'Jog':
            raise AssertionError("$J sent before the cancelled jog had stopped")
        queued = self.state == 'Jog'   # Grbl plans it behind the jog(s) already running
        target = dict(self.target if queued else self.position)
        for axis, value in re.findall(r'([XYZ])(-?[\d.]+)', match.group(1)):
            target[axis] += float(value)
        if self.settings.get('$20') == '1':
            for axis, setting in jog.TRAVEL_SETTINGS.items():
                if target[axis] > 0 or target[axis] < -float(self.settings[setting]):
                    raise RuntimeError("Error during send(): b'error:15\\r\\n'")
        if not queued:
            self.start = dict(self.position)
        self.target = target
        self.state = 'Jog'
        self.pollsUntilDone = self.autoFinishPolls

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
        elif self.state == 'Jog' and self.pollsUntilDone is not None:
            if self.pollsUntilDone <= 0:
                self.finish()
            else:
                self.pollsUntilDone -= 1
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


def fakeClock(tick=0.01):
    # Advances on every call, so a wait that never ends hits its timeout quickly.
    state = {'now': 0.0}

    def clock():
        state['now'] += tick
        return state['now']
    return clock


def makeSession(grbl=None):
    grbl = grbl or FakeGrbl()
    messages = []
    session = jog.JogSession(grbl, jog.softLimitBounds(grbl.settings), out=messages.append,
                             sleep=lambda seconds: None, clock=fakeClock())
    return grbl, session, messages


def holdKeys(**overrides):
    # Step-8 behaviour: every press is a hold at once.
    return jog.JogKeyState(holdThreshold=0, **overrides)


class JogKeyStateTests(unittest.TestCase):
    def setUp(self):
        self.keys = holdKeys()

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
            keys = holdKeys()
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
        keys = holdKeys(xyFeed=2000, zFeed=500, creepDivisor=4)
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
        self.drive(session, holdKeys(), ('RIGHT', True), ('RIGHT', False))
        self.assertEqual(grbl.jogs(), ["$J=G91 G21 X416.990 F1000"])
        self.assertEqual(grbl.written, [jog.JOG_CANCEL])
        self.assertEqual(grbl.state, 'Idle')  # waited for the deceleration to finish

    def testNextJogWaitsForCancelledJogToStop(self):
        grbl, session, _ = makeSession(FakeGrbl(decelPolls=5))
        self.drive(session, holdKeys(), ('RIGHT', True), ('UP', True))
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
        self.drive(session, holdKeys(), ('RIGHT', True), ('UP', True), ('UP', False))
        self.assertTrue(re.fullmatch(r"\$J=G91 G21 X[\d.]+ F1000", grbl.jogs()[-1]))
        self.assertEqual(grbl.written, [jog.JOG_CANCEL, jog.JOG_CANCEL])

    def testCtrlReissuesAtCreepSpeed(self):
        grbl, session, _ = makeSession()
        self.drive(session, holdKeys(), ('LEFTSHIFT', True), ('DOWN', True), ('LEFTCTRL', True))
        self.assertEqual(grbl.jogs()[0], "$J=G91 G21 Z-106.990 F300")
        self.assertTrue(grbl.jogs()[1].endswith(" F30"))

    def testAtTheLimitNothingIsSent(self):
        grbl, session, messages = makeSession()
        self.drive(session, holdKeys(), ('LEFT', True))
        self.assertEqual(grbl.jogs(), [])
        self.assertIn("At the travel limit", messages[0])

    def testDiagonalSlidesAlongTheEdgeItReaches(self):
        grbl, session, _ = makeSession()
        self.drive(session, holdKeys(), ('RIGHT', True), ('UP', True))
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
        self.drive(session, holdKeys(), ('RIGHT', True))
        session.poll()
        self.assertEqual(len(grbl.jogs()), 1)

    def testEnterCancelsAndAccepts(self):
        grbl, session, _ = makeSession()
        results = self.drive(session, holdKeys(), ('DOWN', True), ('ENTER', True))
        self.assertEqual(results, [None, jog.ACCEPTED])
        self.assertEqual(grbl.written, [jog.JOG_CANCEL])

    def testEscAborts(self):
        _, session, _ = makeSession()
        self.assertEqual(self.drive(session, holdKeys(), ('ESC', True)), [jog.ABORTED])

    def testAlarmDuringCancelRaises(self):
        grbl, session, _ = makeSession()
        grbl.alarmOnCancel = True
        with self.assertRaises(jog.JogError):
            self.drive(session, holdKeys(), ('RIGHT', True), ('RIGHT', False))

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
     'KEY_LEFTCTRL', 'KEY_RIGHTCTRL', 'KEY_LEFTALT', 'KEY_RIGHTALT', 'KEY_ENTER', 'KEY_KPENTER',
     'KEY_ESC', 'KEY_A', 'KEY_KP8'], start=100)}
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

    def queue(self, events):
        self.events += [types.SimpleNamespace(type=EV_KEY, code=KEY_CODES[k], value=v) for k, v in events]
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
        # All five events arrive in one read, so Right is down for no time at all:
        # since step 10 that is a tap, one 0.1 mm step.
        keyboard = FakeDevice(ARROWS, [('KEY_RIGHT', 1), ('KEY_RIGHT', 2), ('KEY_RIGHT', 0),
                                       ('KEY_A', 1), ('KEY_ENTER', 1)])
        mouse = FakeDevice(['KEY_A'])
        grbl, result = self.run_({'/dev/input/event0': keyboard, '/dev/input/event1': mouse},
                                 FakeGrbl(autoFinishPolls=2))
        self.assertEqual(result, jog.ACCEPTED)
        self.assertEqual(grbl.jogs(), ["$J=G91 G21 X0.100 F1000"])  # repeat (value 2) ignored
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


# ---------------------------------------------------------------------------
# Step 10: tap-to-step
# ---------------------------------------------------------------------------

MIDDLE = {'X': -200.0, 'Y': -150.0, 'Z': -50.0}  # room in every direction

# (modifiers held, arrow) -> (direction, size, feed)
TAPS = [
    ((), 'RIGHT', (1, 0, 0), 0.1, 1000), ((), 'LEFT', (-1, 0, 0), 0.1, 1000),
    ((), 'UP', (0, 1, 0), 0.1, 1000), ((), 'DOWN', (0, -1, 0), 0.1, 1000),
    (('LEFTSHIFT',), 'UP', (0, 0, 1), 0.1, 300), (('RIGHTSHIFT',), 'DOWN', (0, 0, -1), 0.1, 300),
    (('LEFTCTRL',), 'RIGHT', (1, 0, 0), 0.1, 100), (('RIGHTCTRL',), 'UP', (0, 1, 0), 0.1, 100),
    (('LEFTCTRL', 'LEFTSHIFT'), 'DOWN', (0, 0, -1), 0.1, 30),
    (('LEFTALT',), 'LEFT', (-1, 0, 0), 0.01, 1000), (('LEFTALT',), 'DOWN', (0, -1, 0), 0.01, 1000),
    (('RIGHTALT',), 'RIGHT', (1, 0, 0), 0.01, 1000),          # AltGr counts as Alt
    (('LEFTALT', 'LEFTSHIFT'), 'UP', (0, 0, 1), 0.01, 300),
    (('RIGHTALT', 'RIGHTSHIFT'), 'DOWN', (0, 0, -1), 0.01, 300),
]


def gcodeFor(direction, size, feed):
    words = [f"{axis}{sign * size:.3f}" for axis, sign in zip(jog.AXES, direction) if sign]
    return f"$J=G91 G21 {' '.join(words)} F{feed:g}"


class TapKeyStateTests(unittest.TestCase):
    def tap(self, keys, modifiers, arrow, down=0.0, up=0.1):
        commands = []
        for name in modifiers:
            commands += keys.handleKey(name, True, now=down)
        commands += keys.handleKey(arrow, True, now=down)
        self.assertEqual(commands, [], "nothing may move before the key comes up")
        commands = keys.handleKey(arrow, False, now=up)
        for name in modifiers:
            commands += keys.handleKey(name, False, now=up)
        commands += keys.tick(now=up + 10)
        return commands

    def testEachTapIsExactlyOneStepOfTheRightSizeAndSign(self):
        for modifiers, arrow, direction, size, feed in TAPS:
            with self.subTest(modifiers=modifiers, arrow=arrow):
                commands = self.tap(jog.JogKeyState(), modifiers, arrow)
                self.assertEqual(commands, [('step', direction, size, feed)])

    def testNothingMovesDuringTheThreshold(self):
        keys = jog.JogKeyState()
        self.assertEqual(keys.handleKey('RIGHT', True, now=5.0), [])
        self.assertEqual(keys.nextDeadline(), 5.25)
        self.assertEqual(keys.tick(now=5.2499), [])
        self.assertEqual(keys.handleKey('RIGHT', False, now=5.2499), [('step', (1, 0, 0), 0.1, 1000)])
        self.assertIsNone(keys.nextDeadline())

    def testHoldPastTheThresholdIsTheContinuousJog(self):
        keys = jog.JogKeyState()
        keys.handleKey('LEFTSHIFT', True, now=0.0)
        keys.handleKey('DOWN', True, now=0.0)
        self.assertEqual(keys.tick(now=0.25), [('jog', (0, 0, -1), 300)])
        self.assertEqual(keys.tick(now=0.5), [])
        self.assertEqual(keys.handleKey('LEFTCTRL', True, now=0.7), [('cancel',), ('jog', (0, 0, -1), 30)])
        self.assertEqual(keys.handleKey('DOWN', False, now=2.0), [('cancel',)])  # no step

    def testEventAfterTheThresholdAlsoStartsTheHold(self):
        # A second arrow pressed after 250 ms, before any tick(): already a hold.
        keys = jog.JogKeyState()
        keys.handleKey('RIGHT', True, now=0.0)
        self.assertEqual(keys.handleKey('UP', True, now=0.3), [('jog', (1, 1, 0), 1000)])

    def testTwoKeysTappedTogetherGiveOneDiagonalStep(self):
        keys = jog.JogKeyState()
        self.assertEqual(keys.handleKey('RIGHT', True, now=0.0), [])
        self.assertEqual(keys.handleKey('UP', True, now=0.05), [])
        self.assertEqual(keys.handleKey('RIGHT', False, now=0.1), [])  # Up still down
        self.assertEqual(keys.handleKey('UP', False, now=0.15), [('step', (1, 1, 0), 0.1, 1000)])

    def testAltShiftDiagonalTapInXAndZ(self):
        keys = jog.JogKeyState()
        for name in ('LEFTALT', 'LEFTSHIFT', 'LEFT', 'UP'):
            keys.handleKey(name, True, now=0.0)
        keys.handleKey('LEFT', False, now=0.1)
        self.assertEqual(keys.handleKey('UP', False, now=0.1), [('step', (-1, 0, 1), 0.01, 300)])

    def testModifierLetGoDuringTheTapStillCounts(self):
        keys = jog.JogKeyState()
        keys.handleKey('LEFTALT', True, now=0.0)
        keys.handleKey('RIGHT', True, now=0.02)
        keys.handleKey('LEFTALT', False, now=0.05)
        self.assertEqual(keys.handleKey('RIGHT', False, now=0.08), [('step', (1, 0, 0), 0.01, 1000)])

    def testOppositeArrowsTappedTogetherDoNothing(self):
        keys = jog.JogKeyState()
        keys.handleKey('LEFT', True, now=0.0)
        keys.handleKey('RIGHT', True, now=0.0)
        keys.handleKey('LEFT', False, now=0.1)
        self.assertEqual(keys.handleKey('RIGHT', False, now=0.1), [])

    def testRapidTapsGiveOneStepEachAndNeverACancel(self):
        keys = jog.JogKeyState()
        commands = []
        for n in range(5):
            commands += keys.handleKey('UP', True, now=n * 0.1)
            commands += keys.handleKey('UP', False, now=n * 0.1 + 0.04)
            commands += keys.tick(now=n * 0.1 + 0.06)
        self.assertEqual(commands, [('step', (0, 1, 0), 0.1, 1000)] * 5)

    def testTapAfterAHoldIsATapAgain(self):
        keys = jog.JogKeyState()
        keys.handleKey('RIGHT', True, now=0.0)
        keys.tick(now=1.0)
        self.assertEqual(keys.handleKey('RIGHT', False, now=2.0), [('cancel',)])
        keys.handleKey('RIGHT', True, now=3.0)
        self.assertEqual(keys.handleKey('RIGHT', False, now=3.1), [('step', (1, 0, 0), 0.1, 1000)])

    def testEnterDuringAnUndecidedTapMovesNothing(self):
        keys = jog.JogKeyState()
        keys.handleKey('RIGHT', True, now=0.0)
        self.assertEqual(keys.handleKey('ENTER', True, now=0.1), [('accept',)])
        self.assertEqual(keys.handleKey('RIGHT', False, now=0.15), [])
        self.assertEqual(keys.tick(now=1.0), [])

    def testThresholdIsAModuleConstantThatCanBeOverridden(self):
        self.assertEqual((jog.HOLD_THRESHOLD, jog.TAP_STEP, jog.FINE_TAP_STEP), (0.25, 0.1, 0.01))
        with mock.patch.object(jog, 'HOLD_THRESHOLD', 0.5):
            keys = jog.JogKeyState()
        keys.handleKey('RIGHT', True, now=0.0)
        self.assertEqual(keys.tick(now=0.4), [])
        self.assertEqual(keys.handleKey('RIGHT', False, now=0.45), [('step', (1, 0, 0), 0.1, 1000)])
        keys = jog.JogKeyState(holdThreshold=0.1)
        keys.handleKey('RIGHT', True, now=0.0)
        self.assertEqual(keys.tick(now=0.1), [('jog', (1, 0, 0), 1000)])

    def testUsesItsClockWhenNotToldTheTime(self):
        now = [10.0]
        keys = jog.JogKeyState(clock=lambda: now[0])
        keys.handleKey('DOWN', True)
        now[0] = 10.3
        self.assertEqual(keys.tick(), [('jog', (0, -1, 0), 1000)])


class PlanStepTests(unittest.TestCase):
    bounds = jog.softLimitBounds(SETTINGS)

    def testFullStepWithRoom(self):
        gcode, target = jog.planStep((0, 0, -1), 0.01, 300, MIDDLE, self.bounds)
        self.assertEqual(gcode, "$J=G91 G21 Z-0.010 F300")
        self.assertAlmostEqual(target['Z'], -50.01)

    def testShortenedAtTheBound(self):
        gcode, target = jog.planStep((1, 0, 0), 0.1, 1000, dict(MIDDLE, X=-0.06), self.bounds)
        self.assertEqual(gcode, "$J=G91 G21 X0.050 F1000")  # 0.06 - the 0.01 margin
        self.assertAlmostEqual(target['X'], -0.01)

    def testRefusedAtTheBound(self):
        self.assertIsNone(jog.planStep((-1, 0, 0), 0.1, 1000, HOME, self.bounds))  # home-end inset
        self.assertIsNone(jog.planStep((0, 0, 1), 0.01, 300, HOME, self.bounds))

    def testDiagonalAxesAreClampedSeparately(self):
        gcode, _ = jog.planStep((1, 1, 0), 0.1, 1000, dict(MIDDLE, X=-0.06), self.bounds)
        self.assertEqual(gcode, "$J=G91 G21 X0.050 Y0.100 F1000")
        gcode, _ = jog.planStep((-1, 1, 0), 0.1, 1000, dict(MIDDLE, X=HOME['X']), self.bounds)
        self.assertEqual(gcode, "$J=G91 G21 Y0.100 F1000")  # X dropped, Y still steps


class TapSessionTests(unittest.TestCase):
    def tap(self, keys, session, arrows, modifiers=(), at=0.0):
        results = []
        for name in tuple(modifiers) + tuple(arrows):
            results.append(jog.dispatchKey(keys, session, name, True, now=at))
        for name in tuple(arrows) + tuple(modifiers):
            results.append(jog.dispatchKey(keys, session, name, False, now=at + 0.05))
        results.append(jog.dispatchTick(keys, session, now=at + 0.06))
        return results

    def testEachTapSendsOneStepAndNoCancel(self):
        for modifiers, arrow, direction, size, feed in TAPS:
            with self.subTest(modifiers=modifiers, arrow=arrow):
                grbl, session, _ = makeSession(FakeGrbl(position=MIDDLE))
                self.tap(jog.JogKeyState(), session, [arrow], modifiers)
                self.assertEqual(grbl.jogs(), [gcodeFor(direction, size, feed)])
                self.assertEqual(grbl.written, [])

    def testTwoKeyTapSendsOneDiagonalStep(self):
        grbl, session, _ = makeSession(FakeGrbl(position=MIDDLE))
        self.tap(jog.JogKeyState(), session, ['LEFT', 'DOWN'], ['LEFTALT'])
        self.assertEqual(grbl.jogs(), ["$J=G91 G21 X-0.010 Y-0.010 F1000"])

    def testRapidTapsQueueWithoutACancel(self):
        # The fake stays in Jog until finish(): every step after the first is sent
        # while the earlier ones are still running, and must queue behind them.
        grbl, session, _ = makeSession(FakeGrbl(position=MIDDLE))
        keys = jog.JogKeyState()
        for n in range(5):
            self.tap(keys, session, ['RIGHT'], at=n * 0.1)
        self.assertEqual(grbl.jogs(), ["$J=G91 G21 X0.100 F1000"] * 5)
        self.assertEqual(grbl.written, [])
        self.assertEqual(grbl.state, 'Jog')
        self.assertAlmostEqual(grbl.target['X'], -199.5)

    def testQueuedStepsAreClampedFromWhereTheQueueEnds(self):
        # MPos still reads X-0.25 while the steps run; clamping from it would
        # send a third full step past the bound (error:15 from the fake).
        grbl, session, messages = makeSession(FakeGrbl(position=dict(MIDDLE, X=-0.25)))
        keys = jog.JogKeyState()
        for n in range(4):
            self.tap(keys, session, ['RIGHT'], at=n * 0.1)
        self.assertEqual(grbl.jogs(), ["$J=G91 G21 X0.100 F1000", "$J=G91 G21 X0.100 F1000",
                                       "$J=G91 G21 X0.040 F1000"])
        self.assertEqual(messages, ["At the travel limit in that direction; not moving."])
        self.assertAlmostEqual(grbl.target['X'], -0.01)

    def testStepTowardHomeAtHomeIsRefused(self):
        grbl, session, messages = makeSession()
        self.tap(jog.JogKeyState(), session, ['UP'], ['LEFTSHIFT'])
        self.assertEqual(grbl.jogs(), [])
        self.assertIn("At the travel limit", messages[0])

    def testHoldAfterQueuedStepsPlansFromTheirEnd(self):
        grbl, session, _ = makeSession(FakeGrbl(position=dict(MIDDLE, X=-0.5)))
        keys = jog.JogKeyState()
        self.tap(keys, session, ['RIGHT'], at=0.0)
        self.tap(keys, session, ['RIGHT'], at=0.1)
        jog.dispatchKey(keys, session, 'RIGHT', True, now=0.2)
        jog.dispatchTick(keys, session, now=0.45)
        self.assertEqual(grbl.jogs()[-1], "$J=G91 G21 X0.290 F1000")  # from X-0.3, not -0.5
        self.assertEqual(grbl.written, [])
        jog.dispatchKey(keys, session, 'RIGHT', False, now=1.0)
        self.assertEqual(grbl.written, [jog.JOG_CANCEL])  # only the hold is cancelled

    def testHoldThroughTheSessionIsTheContinuousJog(self):
        grbl, session, _ = makeSession()
        keys = jog.JogKeyState()
        jog.dispatchKey(keys, session, 'RIGHT', True, now=0.0)
        jog.dispatchTick(keys, session, now=0.2)
        self.assertEqual(grbl.jogs(), [])
        jog.dispatchTick(keys, session, now=0.25)
        self.assertEqual(grbl.jogs(), ["$J=G91 G21 X416.990 F1000"])
        jog.dispatchKey(keys, session, 'RIGHT', False, now=0.8)
        self.assertEqual(grbl.written, [jog.JOG_CANCEL])

    def testEnterLetsQueuedStepsFinish(self):
        grbl, session, _ = makeSession(FakeGrbl(position=MIDDLE, autoFinishPolls=3))
        keys = jog.JogKeyState()
        self.tap(keys, session, ['DOWN'], ['LEFTSHIFT'], at=0.0)
        self.tap(keys, session, ['DOWN'], ['LEFTSHIFT'], at=0.1)
        self.assertEqual(jog.dispatchKey(keys, session, 'ENTER', True, now=0.2), jog.ACCEPTED)
        self.assertEqual(grbl.written, [])   # not cancelled
        self.assertEqual(grbl.state, 'Idle')  # waited for them
        self.assertAlmostEqual(grbl.position['Z'], -50.2)

    def testEnterWaitsForQueuedStepsBeforeAnyCancel(self):
        # The steps finish by themselves after a few polls; Enter must poll until
        # then and never send 0x85, so the accepted position includes every tap.
        grbl, session, _ = makeSession(FakeGrbl(position=MIDDLE, autoFinishPolls=5))
        keys = jog.JogKeyState()
        for n in range(3):
            self.tap(keys, session, ['RIGHT'], at=n * 0.1)
        self.assertEqual(grbl.state, 'Jog')
        before = len(grbl.log)
        self.assertEqual(jog.dispatchKey(keys, session, 'ENTER', True, now=0.4), jog.ACCEPTED)
        self.assertNotIn(('write', jog.JOG_CANCEL), grbl.log)
        self.assertGreaterEqual(grbl.log[before:].count(('status', 'Jog')), 5)
        self.assertEqual(grbl.state, 'Idle')
        self.assertAlmostEqual(grbl.position['X'], -199.7)
        self.assertIsNone(session.stepTarget)

    def testEscCancelsQueuedStepsAtOnce(self):
        # The fake never finishes the steps by itself (no autoFinishPolls), so a
        # wait for them would run into STEP_FINISH_TIMEOUT and raise JogError.
        # Esc must instead send 0x85 first, then wait only for Grbl to leave Jog.
        clock = fakeClock()
        grbl = FakeGrbl(position=dict(MIDDLE, X=-0.25))
        session = jog.JogSession(grbl, jog.softLimitBounds(grbl.settings), out=[].append,
                                 sleep=lambda seconds: None, clock=clock)
        keys = jog.JogKeyState()
        self.tap(keys, session, ['RIGHT'], at=0.0)
        self.tap(keys, session, ['RIGHT'], at=0.1)
        self.assertEqual(grbl.state, 'Jog')
        self.assertAlmostEqual(session.stepTarget['X'], -0.05)
        before = len(grbl.log)
        start = clock()
        self.assertEqual(jog.dispatchKey(keys, session, 'ESC', True, now=0.2), jog.ABORTED)
        self.assertEqual(grbl.log[before], ('write', jog.JOG_CANCEL))  # before any wait
        self.assertEqual(grbl.written, [jog.JOG_CANCEL])
        self.assertEqual(grbl.state, 'Idle')
        self.assertAlmostEqual(grbl.position['X'], -0.15)  # stopped part-way: steps discarded
        self.assertLess(clock() - start, jog.CANCEL_TIMEOUT)
        self.assertIsNone(session.stepTarget)
        self.assertIsNone(keys.nextDeadline())
        # Nothing carries over: the next tap plans from MPos (-0.15), not from -0.05.
        self.tap(keys, session, ['RIGHT'], at=1.0)
        self.assertEqual(grbl.jogs()[-1], "$J=G91 G21 X0.100 F1000")
        self.assertAlmostEqual(grbl.target['X'], -0.05)

    def testEscWithNothingQueuedSendsNothing(self):
        grbl, session, _ = makeSession()
        keys = jog.JogKeyState()
        self.assertEqual(jog.dispatchKey(keys, session, 'ESC', True, now=0.0), jog.ABORTED)
        self.assertEqual((grbl.sent, grbl.written, grbl.state), ([], [], 'Idle'))

    def testEscAfterStepsHaveFinishedStillAborts(self):
        grbl, session, _ = makeSession(FakeGrbl(position=MIDDLE))
        keys = jog.JogKeyState()
        self.tap(keys, session, ['LEFT'], at=0.0)
        grbl.finish()
        self.assertEqual(jog.dispatchKey(keys, session, 'ESC', True, now=0.5), jog.ABORTED)
        self.assertEqual(grbl.state, 'Idle')
        self.assertAlmostEqual(grbl.position['X'], -200.1)  # the finished step is kept
        self.assertIsNone(session.stepTarget)

    def testEnterDuringAHoldStillCancels(self):
        grbl, session, _ = makeSession()
        keys = jog.JogKeyState()
        jog.dispatchKey(keys, session, 'RIGHT', True, now=0.0)
        jog.dispatchTick(keys, session, now=0.3)
        self.assertEqual(jog.dispatchKey(keys, session, 'ENTER', True, now=0.5), jog.ACCEPTED)
        self.assertEqual(grbl.written, [jog.JOG_CANCEL])


class Timeline:
    """Stands in for select.select() and the clock in runKeyboardJog.

    script is [(time, key, value), ...].  select() jumps the clock to the next
    scripted event if it falls within the timeout (and delivers every event
    due by then), else to the end of the timeout.
    """

    def __init__(self, device, script):
        self.device = device
        self.script = list(script)
        self.now = 0.0
        self.timeouts = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def select(self, readers, writers, errors, timeout):
        self.timeouts.append((self.now, timeout))
        if len(self.timeouts) > 10000:
            raise AssertionError("runKeyboardJog is spinning")
        if not self.script:
            raise AssertionError("the script ran out before Enter/Esc")
        if self.script[0][0] <= self.now + timeout:
            self.now = max(self.now, self.script[0][0])
            due = []
            while self.script and self.script[0][0] <= self.now:
                due.append(self.script.pop(0)[1:])
            self.device.queue(due)
            return [self.device.fd], [], []
        self.now += timeout
        return [], [], []


class TapThroughKeyboardLoopTests(unittest.TestCase):
    def run_(self, script, grbl=None):
        keyboard = FakeDevice(ARROWS + ['KEY_LEFTALT', 'KEY_RIGHTALT', 'KEY_LEFTSHIFT'])
        timeline = Timeline(keyboard, script)
        grbl = grbl or FakeGrbl(position=MIDDLE, autoFinishPolls=2)
        grbl.clock = timeline.monotonic
        messages = []
        fakeTime = types.SimpleNamespace(monotonic=timeline.monotonic, sleep=timeline.sleep)
        with mock.patch.object(jog, 'importEvdev', return_value=fakeEvdev({'/dev/input/event0': keyboard})), \
                mock.patch.object(jog, 'select', types.SimpleNamespace(select=timeline.select)), \
                mock.patch.object(jog, 'time', fakeTime):
            result = jog.runKeyboardJog(grbl, True, out=messages.append)
        return grbl, result, timeline, keyboard, messages

    def testHoldStartsTheJogAtTheThreshold(self):
        grbl, result, timeline, _, _ = self.run_(
            [(0.0, 'KEY_RIGHT', 1), (1.0, 'KEY_RIGHT', 0), (1.5, 'KEY_ENTER', 1)], FakeGrbl())
        self.assertEqual(result, jog.ACCEPTED)
        self.assertEqual(grbl.jogs(), ["$J=G91 G21 X416.990 F1000"])
        self.assertAlmostEqual(grbl.sendTimes[0], 0.25)  # select woke up for the threshold
        self.assertIn((0.0, 0.2), timeline.timeouts)      # first wait: the poll interval
        self.assertTrue(any(abs(timeout - 0.05) < 1e-9 for _, timeout in timeline.timeouts))

    def testTapsWithAltAndAltGrStepAndEnterKeepsThem(self):
        grbl, result, _, keyboard, messages = self.run_([
            (0.0, 'KEY_RIGHT', 1), (0.1, 'KEY_RIGHT', 0),
            (0.5, 'KEY_LEFTALT', 1), (0.55, 'KEY_UP', 1), (0.6, 'KEY_UP', 0), (0.65, 'KEY_LEFTALT', 0),
            (0.7, 'KEY_RIGHTALT', 1), (0.72, 'KEY_LEFTSHIFT', 1), (0.75, 'KEY_DOWN', 1),
            (0.8, 'KEY_DOWN', 0), (0.85, 'KEY_LEFTSHIFT', 0), (0.86, 'KEY_RIGHTALT', 0),
            (1.0, 'KEY_ENTER', 1)])
        self.assertEqual(result, jog.ACCEPTED)
        self.assertEqual(grbl.jogs(), ["$J=G91 G21 X0.100 F1000", "$J=G91 G21 Y0.010 F1000",
                                       "$J=G91 G21 Z-0.010 F300"])
        self.assertEqual([round(t, 3) for t in grbl.sendTimes], [0.1, 0.6, 0.8])  # on release
        self.assertEqual(grbl.state, 'Idle')
        self.assertEqual((round(grbl.position['X'], 3), round(grbl.position['Y'], 3),
                          round(grbl.position['Z'], 3)), (-199.9, -149.99, -50.01))
        self.assertTrue(keyboard.grabbed and keyboard.ungrabbed)
        self.assertIn("Tap to step 0.1 mm (Alt+tap 0.01 mm)", messages[0])

    def testEscAfterTapsCancelsAtOnceThroughTheLoop(self):
        # Steps that never finish by themselves: Esc must not wait for them.
        grbl, result, timeline, keyboard, messages = self.run_([
            (0.0, 'KEY_RIGHT', 1), (0.05, 'KEY_RIGHT', 0),
            (0.1, 'KEY_RIGHT', 1), (0.15, 'KEY_RIGHT', 0),
            (0.2, 'KEY_ESC', 1)], FakeGrbl(position=MIDDLE))
        self.assertEqual(result, jog.ABORTED)
        self.assertEqual(len(grbl.jogs()), 2)
        self.assertEqual(grbl.written[0], jog.JOG_CANCEL)
        self.assertEqual(grbl.state, 'Idle')
        self.assertLess(grbl.position['X'], -199.8 + 1e-9)  # not where both taps would end
        self.assertLess(timeline.now, jog.CANCEL_TIMEOUT)
        self.assertTrue(keyboard.ungrabbed)
        self.assertIn("Esc aborts at once", messages[1])

    def testKeypadArrowsAreStillIgnored(self):
        grbl, result, _, _, _ = self.run_([(0.0, 'KEY_KP8', 1), (0.1, 'KEY_KP8', 0), (0.2, 'KEY_ESC', 1)])
        self.assertEqual(result, jog.ABORTED)
        self.assertEqual(grbl.jogs(), [])

    def testAltKeysAreMapped(self):
        self.assertEqual((jog.EVDEV_KEY_NAMES['KEY_LEFTALT'], jog.EVDEV_KEY_NAMES['KEY_RIGHTALT']),
                         ('LEFTALT', 'RIGHTALT'))
        self.assertNotIn('KEY_KP8', jog.EVDEV_KEY_NAMES)


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
