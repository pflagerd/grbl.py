#!/usr/bin/env python3
"""Hold-to-jog and tap-to-step from the keyboard (issue #13, steps 8 and 10).

    Left / Right          X- / X+
    Up / Down             Y+ / Y-
    Shift+Up / Shift+Down Z+ / Z-
    tap (release within HOLD_THRESHOLD, 250 ms)
                          one step: 0.1 mm, or 0.01 mm with Alt (Left or Right Alt / AltGr)
    hold (past HOLD_THRESHOLD)
                          continuous jog until the key comes up
    Ctrl (held)           a tenth of the speed, for creeping onto a surface
    Enter                 stop jogging, return "accepted" (queued tap steps finish first)
    Esc                   stop jogging at once, return "aborted" (queued tap steps are discarded)

Two arrows tapped together (both down before either is up) give one diagonal
step.  Nothing moves during the first HOLD_THRESHOLD after an arrow goes down:
only when the key comes up (a tap: one step) or the threshold passes with it
still held (a hold: the continuous jog) is anything sent.

A continuous jog stops when its key comes up, and a tap is only known when the
key comes up, so key releases matter.  A terminal cannot report them, so key
events come from the Linux input layer (/dev/input/event*) through the `evdev`
library.  While jogging, the keyboard
is grabbed: keystrokes go only to this program, whichever window has focus.

How a continuous jog works with Grbl 1.1h: one `$J=G91 ...` is sent whose
length runs to the edge of the machine's travel in that direction, and when
the key set changes, the real-time byte 0x85 (jog-cancel) makes Grbl
decelerate to a stop and discard the rest of the jog.  Grbl does the
acceleration and deceleration itself ($120-$122).

The parts are kept separate so the logic can be tested without evdev or a
machine:
    JogKeyState         pure: key events (and the time) in, step/jog/cancel commands out
    planJog()           pure: direction + position + bounds -> one $J line to the edge
    planStep()          pure: direction + step size + position + bounds -> one short $J line
    softLimitBounds()   pure: $130-$132, $23, $27 -> machine-coordinate box
    JogSession          drives a GrblController (send/write/getStatus)
    formatPositionBlock()  pure: position + bounds -> the lines of the live display
    PositionDisplay     redraws that block in place with VT100 codes
    runKeyboardJog()    the thin evdev part: find, grab, read, ungrab
    checkJogEnvironment()  finds a missing evdev / input-group access, offers fixes

Run directly to jog a connected machine:  python jog.py [--home]
"""

import argparse
import getpass
import glob
import grp
import importlib
import math
import os
import pwd
import select
import shlex
import shutil
import subprocess
import sys
import time

# Default jog speeds, mm/min.  Callers can override them (runKeyboardJog / JogKeyState).
XY_FEED = 1000
Z_FEED = 300
CREEP_DIVISOR = 10          # Ctrl held: feed / CREEP_DIVISOR

JOG_CANCEL = b'\x85'        # Grbl 1.1 real-time jog-cancel: no newline, no 'ok'
LIMIT_MARGIN = 0.01         # mm kept clear of each soft-limit bound (float rounding in Grbl)
MINIMUM_MOVE = 0.001        # mm; an axis with less room than this is treated as blocked
CANCEL_TIMEOUT = 10.0       # s to wait for Grbl to leave Jog after a cancel
POLL_INTERVAL = 0.2         # s between status polls while a jog runs
STEP_FINISH_TIMEOUT = 60.0  # s to wait for queued tap steps to finish after Enter
DISPLAY_INTERVAL = 0.1      # s between updates of the live position display

# Tap-to-step (step 10).  Callers can override these (runKeyboardJog / JogKeyState),
# and the JogKeyState defaults are read when it is created, so setting
# jog.HOLD_THRESHOLD before then works too.
HOLD_THRESHOLD = 0.25       # s; an arrow released sooner is a tap, held longer a continuous jog
TAP_STEP = 0.1              # mm per plain tap or Ctrl+tap
FINE_TAP_STEP = 0.01        # mm per Alt+tap

AXES = ('X', 'Y', 'Z')
TRAVEL_SETTINGS = {'X': '$130', 'Y': '$131', 'Z': '$132'}
AXIS_BITS = {'X': 1, 'Y': 2, 'Z': 4}

# Results of runKeyboardJog()
ACCEPTED = "accepted"
ABORTED = "aborted"

# Results of checkJogEnvironment()
ENVIRONMENT_OK = "ok"
UNAVAILABLE_UNTIL_RELOGIN = "jog unavailable until re-login"
UNAVAILABLE = "jog unavailable"

# evdev key codes -> the names JogKeyState understands.  Names, not numbers, so
# this module (and its tests) load without evdev.
EVDEV_KEY_NAMES = {
    'KEY_LEFT': 'LEFT', 'KEY_RIGHT': 'RIGHT', 'KEY_UP': 'UP', 'KEY_DOWN': 'DOWN',
    'KEY_LEFTSHIFT': 'LEFTSHIFT', 'KEY_RIGHTSHIFT': 'RIGHTSHIFT',
    'KEY_LEFTCTRL': 'LEFTCTRL', 'KEY_RIGHTCTRL': 'RIGHTCTRL',
    # AltGr is KEY_RIGHTALT at the evdev level (the layout maps it to ISO_Level3_Shift
    # later, in the desktop), so AltGr counts as Alt here.
    'KEY_LEFTALT': 'LEFTALT', 'KEY_RIGHTALT': 'RIGHTALT',
    'KEY_ENTER': 'ENTER', 'KEY_KPENTER': 'ENTER', 'KEY_ESC': 'ESC',
}
ARROW_KEYS = ('KEY_UP', 'KEY_DOWN', 'KEY_LEFT', 'KEY_RIGHT')


class JogRefused(RuntimeError):
    """The machine is not in a state where jogging is safe; nothing was sent."""


class JogError(RuntimeError):
    """Something went wrong while jogging (e.g. Grbl went into Alarm)."""


# ---------------------------------------------------------------------------
# Pure logic: key state -> commands
# ---------------------------------------------------------------------------

ARROW_NAMES = ('LEFT', 'RIGHT', 'UP', 'DOWN')
SHIFT_NAMES = ('LEFTSHIFT', 'RIGHTSHIFT')
CTRL_NAMES = ('LEFTCTRL', 'RIGHTCTRL')
ALT_NAMES = ('LEFTALT', 'RIGHTALT')


class JogKeyState:
    """Turns key presses/releases, and the passing of time, into commands.

    Knows nothing about Grbl.  handleKey() and tick() return a list of
    commands, each a tuple:
        ('cancel',)                         stop the running continuous jog
        ('jog', (dx, dy, dz), feed)         start a continuous jog; each d is -1, 0 or +1
        ('step', (dx, dy, dz), size, feed)  move each d axis by size mm, once
        ('accept',) / ('abort',)            Enter / Esc: end jog mode

    An arrow going down from rest starts a "gesture" and nothing is sent yet.
    If every arrow is up again within holdThreshold seconds, the gesture was a
    tap and gives one step (two arrows down together: one diagonal step).  If
    an arrow is still down when holdThreshold has passed, tick() starts the
    continuous jog, and from then on it works exactly as before step 10: a
    changed key set always gives a cancel before the new jog, so Grbl never
    has two continuous jogs queued.  Steps never give a cancel, so taps queue
    one after another in Grbl's planner.

    holdThreshold=0 turns tap-to-step off: every press is a hold at once.
    clock gives the time when handleKey()/tick() are not told it.
    """

    def __init__(self, xyFeed=XY_FEED, zFeed=Z_FEED, creepDivisor=CREEP_DIVISOR,
                 holdThreshold=None, tapStep=None, fineTapStep=None, clock=time.monotonic):
        self.xyFeed = xyFeed
        self.zFeed = zFeed
        self.creepDivisor = creepDivisor
        self.holdThreshold = HOLD_THRESHOLD if holdThreshold is None else holdThreshold
        self.tapStep = TAP_STEP if tapStep is None else tapStep
        self.fineTapStep = FINE_TAP_STEP if fineTapStep is None else fineTapStep
        self.clock = clock
        self.held = set()
        self.active = None  # the (direction, feed) of the continuous jog last commanded, or None
        self.gestureStart = None  # when the first arrow of an undecided tap/hold went down
        self.gestureKeys = set()  # every key that was down at some point during that gesture

    def vector(self, keys):
        # What a set of keys asks for: (direction, feed), or None for "stand still".
        shift = bool(keys & set(SHIFT_NAMES))
        ctrl = bool(keys & set(CTRL_NAMES))
        dx = ('RIGHT' in keys) - ('LEFT' in keys)   # both: they cancel out
        vertical = ('UP' in keys) - ('DOWN' in keys)
        dy, dz = (0, vertical) if shift else (vertical, 0)
        direction = (dx, dy, dz)
        if direction == (0, 0, 0):
            return None
        feed = self.zFeed if dz else self.xyFeed  # F applies to the whole vector path
        if ctrl:
            feed = feed / self.creepDivisor
        return direction, feed

    def desired(self):
        # What the held keys ask for right now.
        return self.vector(self.held)

    def nextDeadline(self):
        """When tick() must next be called to notice a hold, or None if no gesture is undecided."""
        if self.gestureStart is None:
            return None
        return self.gestureStart + self.holdThreshold

    def handleKey(self, name, pressed, now=None):
        now = self.clock() if now is None else now
        if pressed:
            self.held.add(name)
        else:
            self.held.discard(name)

        if pressed and name in ('ENTER', 'ESC'):
            commands = [('cancel',)] if self.active else []
            self.active = None
            self.gestureStart = None  # an undecided gesture is dropped: nothing had moved
            self.gestureKeys.clear()
            return commands + [('accept',) if name == 'ENTER' else ('abort',)]

        if self.gestureStart is not None:
            # Undecided: remember every key seen, so a modifier pressed or released
            # during a quick tap still counts (Alt down, tap Right, Alt up).
            if pressed:
                self.gestureKeys.add(name)
            if not self.held & set(ARROW_NAMES):
                return self.finishTap()
            return self.tick(now)  # an event can arrive after the threshold has passed

        if pressed and name in ARROW_NAMES and self.active is None \
                and self.held & set(ARROW_NAMES) == {name}:
            # The first arrow down from rest: wait to see whether it is a tap or a hold.
            self.gestureStart = now
            self.gestureKeys = set(self.held)
            return self.tick(now)  # holdThreshold=0: a hold at once

        return self.update()

    def finishTap(self):
        keys = self.gestureKeys
        self.gestureStart = None
        self.gestureKeys = set()
        wanted = self.vector(keys)
        if wanted is None:
            return []  # e.g. Left and Right tapped together
        size = self.fineTapStep if keys & set(ALT_NAMES) else self.tapStep
        return [('step', wanted[0], size, wanted[1])]

    def tick(self, now=None):
        """Call this regularly: once holdThreshold has passed with an arrow still down,
        returns the commands that start the continuous jog."""
        now = self.clock() if now is None else now
        if self.gestureStart is None or now - self.gestureStart < self.holdThreshold:
            return []
        self.gestureStart = None
        self.gestureKeys = set()
        return self.update()

    def update(self):
        # Continuous jog: make the running jog match the held keys.
        wanted = self.desired()
        if wanted == self.active:
            return []  # e.g. Ctrl pressed with no arrow held, or a key nobody maps
        commands = [('cancel',)] if self.active else []
        if wanted:
            commands.append(('jog', wanted[0], wanted[1]))
        self.active = wanted
        return commands


# ---------------------------------------------------------------------------
# Pure logic: soft-limit bounds and jog planning
# ---------------------------------------------------------------------------

def softLimitBounds(settings):
    """The box, in machine coordinates, that jogs must stay inside.

    Stock Grbl 1.1h (HOMING_FORCE_SET_ORIGIN off, the default in config.h)
    puts every axis's soft-limit range at [-$13x, 0], whichever way it homes:
    system_check_travel_limits() rejects target > 0 or target < -max_travel.
    Homing then sets the position to
        -$13x + $27   if the axis homes toward negative ($23 bit set)
        -$27          if it homes toward positive ($23 bit clear)
    i.e. the switch trigger point is the bound and the machine sits $27 inside
    it.  The home-end bound is moved inward by $27, so a jog stops at the homed
    position instead of driving back onto the home switch (which would trip a
    hard-limit alarm with $21=1).  The far bound is the soft limit itself.

    Returns {'X': (low, high), ...}.
    """
    homingMask = int(float(settings.get('$23', '0')))
    pullOff = float(settings.get('$27', '0'))
    bounds = {}
    for axis in AXES:
        travel = float(settings[TRAVEL_SETTINGS[axis]])
        low, high = -travel, 0.0
        if homingMask & AXIS_BITS[axis]:
            low += pullOff
        else:
            high -= pullOff
        bounds[axis] = (low, high)
    return bounds


def roomToBound(axis, sign, position, bounds, margin=LIMIT_MARGIN):
    low, high = bounds[axis]
    if sign > 0:
        return high - position[axis] - margin
    return position[axis] - low - margin


def planJog(direction, feed, position, bounds, margin=LIMIT_MARGIN):
    """One $J line that runs to the edge of the travel along `direction`.

    position is {'X': mpos, ...}.  Each moving axis gets the same distance (so
    a diagonal stays at 45 degrees) equal to the smallest room left among them:
    the point where the ray leaves the box.  An axis already at its bound is
    dropped, so the jog slides along that edge.  Returns (gcode, movingAxes),
    or None if no moving axis has room.
    """
    rooms = {}
    for axis, sign in zip(AXES, direction):
        if sign:
            room = roomToBound(axis, sign, position, bounds, margin)
            if room >= MINIMUM_MOVE:
                rooms[axis] = room
    if not rooms:
        return None
    # Round down to 0.001 mm, never past the bound (the 1e-6 absorbs float noise
    # such as 98.495 arriving as 98.49499999).
    distance = math.floor(min(rooms.values()) * 1000 + 1e-6) / 1000
    words = []
    for axis, sign in zip(AXES, direction):
        if axis in rooms:
            words.append(f"{axis}{sign * distance:.3f}")
    gcode = f"$J=G91 G21 {' '.join(words)} F{feed:g}"
    return gcode, tuple(sorted(rooms))


def planStep(direction, size, feed, position, bounds, margin=LIMIT_MARGIN):
    """One short $J line for a tap: each moving axis moves `size` mm.

    Unlike planJog, each axis is clamped on its own: an axis with less than
    `size` of room left moves only as far as its bound, an axis with no room
    is dropped, and the other axis of a diagonal still moves its full step.
    Returns (gcode, target) with target the machine position the step ends
    at, or None if no moving axis has room.
    """
    words = []
    target = dict(position)
    for axis, sign in zip(AXES, direction):
        if not sign:
            continue
        room = roomToBound(axis, sign, position, bounds, margin)
        distance = math.floor(min(size, room) * 1000 + 1e-6) / 1000  # 0.001 mm, never past
        if distance < MINIMUM_MOVE:
            continue
        words.append(f"{axis}{sign * distance:.3f}")
        target[axis] = position[axis] + sign * distance
    if not words:
        return None
    return f"$J=G91 G21 {' '.join(words)} F{feed:g}", target


def parseStatus(report):
    """'<Jog|MPos:1.000,2.000,3.000|...>' -> (state, mpos or None, wco or None, wpos or None)."""
    if not report or not report.startswith('<'):
        return None, None, None, None
    fields = report.strip().strip('<>').split('|')
    state = fields[0].split(':')[0]
    values = {}
    for field in fields[1:]:
        name, _, numbers = field.partition(':')
        if name in ('MPos', 'WPos', 'WCO'):
            values[name] = dict(zip(AXES, (float(n) for n in numbers.split(','))))
    return state, values.get('MPos'), values.get('WCO'), values.get('WPos')


def checkReadyToJog(grbl, settings, homed):
    """Raise JogRefused with a clear message unless jogging is safe.  Returns the bounds."""
    state = grbl.getGrblState()
    if state not in ('Idle', 'Jog'):
        raise JogRefused(f"Grbl is in state '{state}', not Idle or Jog. "
                         "Clear that (e.g. $X or $H after an alarm) before jogging.")
    if settings.get('$22') != '1':
        raise JogRefused("Homing is disabled ($22 is not 1), so machine coordinates mean "
                         "nothing and the travel limits cannot be applied.")
    if not homed:
        # Grbl 1.1h reports no "homed" flag, and GrblController sends $X on a power-up
        # alarm, so only the caller knows whether $H has run since power-up.
        raise JogRefused("The machine has not been homed. Home it ($H) first: the jog "
                         "length is clamped to the travel limits, which need homing.")
    bounds = softLimitBounds(settings)
    _, mpos, _, _ = parseStatus(grbl.getStatus())
    if mpos:
        for axis in AXES:
            travel = float(settings[TRAVEL_SETTINGS[axis]])
            if not -travel - 0.5 <= mpos[axis] <= 0.5:
                raise JogRefused(f"{axis} is at machine position {mpos[axis]:.3f}, outside the "
                                 f"travel range -{travel:g}..0. Not homed, or {TRAVEL_SETTINGS[axis]} "
                                 "is wrong; home the machine first.")
    return bounds


# ---------------------------------------------------------------------------
# The live position display
# ---------------------------------------------------------------------------

def formatMm(value):
    text = f"{value:.3f}"
    return "0.000" if text == "-0.000" else text


def formatPositionBlock(position, bounds, state=None, message="", margin=LIMIT_MARGIN,
                        width=78):
    """The lines of the live display: for each axis the machine position, the
    range jogs are kept inside, and how far it is to each end ("at limit" when
    there is no room left to move that way).  position is {'X': mpos, ...} or
    None (not read yet).  The message is cut to width, so it stays on one line
    (a wrapped line would throw off the redraw).  Pure, so it can be tested on
    its own."""
    lines = [f"     {'Position':>10}  {'Low limit':>10}  {'High limit':>10}  "
             f"{'To low':>9}  {'To high':>9}"]
    for axis in AXES:
        low, high = bounds[axis]
        if position is None:
            lines.append(f"  {axis}  {'-':>10}  {formatMm(low):>10}  {formatMm(high):>10}  "
                         f"{'-':>9}  {'-':>9}")
            continue
        here = position[axis]
        ends = []
        for distance in (here - low, high - here):
            ends.append("at limit" if distance - margin < MINIMUM_MOVE else formatMm(distance))
        lines.append(f"  {axis}  {formatMm(here):>10}  {formatMm(low):>10}  "
                     f"{formatMm(high):>10}  {ends[0]:>9}  {ends[1]:>9}")
    lines.append(f"  State: {state or '-'}")
    lines.append(f"  {message}"[:max(1, width - 1)])
    return lines


def terminalShowsDisplay(stream=None):
    """The display needs a real terminal that understands VT100 cursor codes."""
    stream = stream or sys.stdout
    try:
        tty = stream.isatty()
    except (AttributeError, ValueError):
        return False
    return tty and os.environ.get("TERM", "") not in ("", "dumb")


class PositionDisplay:
    """A fixed block of lines, redrawn in place while jogging.

    Only basic VT100 codes: cursor up (ESC[nA), erase line (ESC[2K), and hide /
    show the cursor (ESC[?25l / ESC[?25h).  Messages go on the block's last
    line instead of being printed, so they never scroll the block away.
    """

    UP = "\x1b[{}A"
    ERASE_LINE = "\x1b[2K"
    HIDE_CURSOR = "\x1b[?25l"
    SHOW_CURSOR = "\x1b[?25h"

    def __init__(self, bounds, write=None, flush=None):
        self.bounds = bounds
        self.write = write or sys.stdout.write
        self.flush = flush or sys.stdout.flush
        self.position = None
        self.state = None
        self.text = ""
        self.height = 0      # lines drawn so far (0: not drawn yet)
        self.shown = None    # the lines last drawn, to skip redrawing the same

    def start(self):
        self.write(self.HIDE_CURSOR)
        self.draw(force=True)

    def update(self, position, state):
        if position is not None:
            self.position = position
        self.state = state or self.state
        self.draw()

    def message(self, text):
        self.text = text
        self.draw()

    def draw(self, force=False):
        # The terminal's width, asked for each time, so a resized window is used in full.
        width = shutil.get_terminal_size((80, 24)).columns
        lines = formatPositionBlock(self.position, self.bounds, self.state, self.text,
                                    width=width)
        if lines == self.shown and not force:
            return
        if self.height:
            self.write(self.UP.format(self.height))
        for line in lines:
            self.write("\r" + self.ERASE_LINE + line + "\n")
        self.height = len(lines)
        self.shown = lines
        self.flush()

    def close(self):
        self.write(self.SHOW_CURSOR)
        self.flush()


# ---------------------------------------------------------------------------
# Driver: commands -> GrblController calls
# ---------------------------------------------------------------------------

class JogSession:
    """Carries out JogKeyState's commands on a GrblController.

    Uses only send(), write(), getGrblState() and getStatus(), so a fake that
    records those calls is enough to test it.
    """

    def __init__(self, grbl, bounds, out=print, sleep=time.sleep, clock=time.monotonic):
        self.grbl = grbl
        self.bounds = bounds
        self.out = out
        self.sleep = sleep
        self.clock = clock
        self.lastWco = None
        self.running = None  # (direction, feed, movingAxes) of the continuous jog Grbl is running
        self.stepTarget = None  # where the last queued tap step ends, while steps may be running

    def machinePosition(self):
        # MPos from a status report.  With $10 set to report WPos, MPos = WPos + WCO,
        # and WCO only appears every few reports, so remember the last one.
        for _ in range(30):
            state, mpos, wco, wpos = parseStatus(self.grbl.getStatus())
            if wco:
                self.lastWco = wco
            if mpos:
                return mpos
            if wpos and self.lastWco:
                return {axis: wpos[axis] + self.lastWco[axis] for axis in AXES}
        raise JogError("Could not read the machine position from Grbl's status report.")

    def status(self):
        """(state, machine position or None) from one status report, for the
        live display."""
        state, mpos, wco, wpos = parseStatus(self.grbl.getStatus())
        if wco:
            self.lastWco = wco
        if mpos is None and wpos and self.lastWco:
            mpos = {axis: wpos[axis] + self.lastWco[axis] for axis in AXES}
        return state, mpos

    def plannedPosition(self):
        # Where the next move starts from.  While tap steps are still running (Grbl
        # in Jog with no continuous jog), that is where the last queued step ends,
        # not the MPos the machine is passing through: a step queued behind it
        # starts there, and clamping from MPos could overrun the bound.
        if self.stepTarget is not None:
            state = self.grbl.getGrblState()
            if state == 'Alarm':
                self.stepTarget = None
                raise JogError("Grbl went into Alarm while stepping (limit switch?). "
                               "Clear it and re-home before continuing.")
            if state == 'Jog':
                return dict(self.stepTarget)
            self.stepTarget = None
        return self.machinePosition()

    def waitUntilStopped(self, timeout=CANCEL_TIMEOUT):
        # After 0x85 Grbl decelerates in state Jog, then goes Idle.  The next $J must
        # wait for that, or it would be computed from a position that is still moving.
        deadline = self.clock() + timeout
        while True:
            state = self.grbl.getGrblState()
            if state == 'Alarm':
                raise JogError("Grbl went into Alarm while jogging (limit switch?). "
                               "Clear it and re-home before continuing.")
            if state != 'Jog':
                return state
            if self.clock() > deadline:
                raise JogError(f"Grbl was still jogging {timeout:g}s after it should have stopped.")
            self.sleep(0.02)

    def cancel(self):
        # 0x85 discards everything queued, tap steps included.  Only a continuous jog
        # ending (or jog mode ending abnormally) sends it, never a key coming up.
        self.grbl.write(JOG_CANCEL)
        self.running = None
        self.stepTarget = None
        self.waitUntilStopped()

    def jog(self, direction, feed, quiet=False):
        planned = planJog(direction, feed, self.plannedPosition(), self.bounds)
        if planned is None:
            self.running = None
            if not quiet:
                self.out("At the travel limit in that direction; not moving.")
            return None
        gcode, movingAxes = planned
        self.grbl.send(gcode)  # queued behind any tap steps still running
        self.running = (direction, feed, movingAxes)
        self.stepTarget = None
        return gcode

    def step(self, direction, size, feed):
        # One tap.  No cancel and no wait: $J lines queue in Grbl's planner, so
        # rapid taps run one after another.  (When the planner is full, send()
        # waits for Grbl's 'ok', which comes as soon as a step finishes.)
        planned = planStep(direction, size, feed, self.plannedPosition(), self.bounds)
        if planned is None:
            self.out("At the travel limit in that direction; not moving.")
            return None
        gcode, target = planned
        self.grbl.send(gcode)
        self.stepTarget = target
        return gcode

    def finishSteps(self):
        # Enter: let queued tap steps finish, so the position read afterwards
        # is where they end.  (stop() would discard them.)
        if self.stepTarget is not None and not self.running:
            self.waitUntilStopped(STEP_FINISH_TIMEOUT)
        self.stepTarget = None

    def abortSteps(self):
        # Esc: stop at once.  If tap steps may still be queued, send 0x85 now
        # rather than waiting for them: in Grbl 1.1h a jog-cancel decelerates the
        # running $J and then flushes the planner (plan_reset() in
        # protocol_exec_rt_system), so every queued $J is discarded too.
        # cancel() then waits, bounded by CANCEL_TIMEOUT, for Grbl to leave Jog.
        if self.stepTarget is not None and not self.running:
            self.cancel()
        self.stepTarget = None

    def execute(self, command):
        if command[0] == 'cancel':
            self.cancel()
        elif command[0] == 'jog':
            self.jog(command[1], command[2])
        elif command[0] == 'step':
            self.step(command[1], command[2], command[3])
        else:
            raise ValueError(f"Not a motion command: {command}")

    def poll(self):
        # Called every POLL_INTERVAL while keys are held.  If a jog reached the edge
        # of the travel (Grbl went Idle by itself), re-plan it: the axis that hit
        # its bound is dropped and the rest carry on.  If that gives the same axes
        # again, nothing more can move, so stop re-planning until the keys change.
        if not self.running:
            return
        state = self.grbl.getGrblState()
        if state == 'Alarm':
            self.running = None
            raise JogError("Grbl went into Alarm while jogging (limit switch?).")
        if state != 'Idle':
            return
        direction, feed, previousAxes = self.running
        planned = planJog(direction, feed, self.machinePosition(), self.bounds)
        if planned is None or planned[1] == previousAxes:
            self.running = None
            return
        self.jog(direction, feed, quiet=True)

    def stop(self):
        # Always safe: 0x85 is ignored unless Grbl is jogging.
        self.cancel()


def runCommands(commands, session):
    """Carry out JogKeyState commands.  Returns ACCEPTED or ABORTED when Enter/Esc
    ends jog mode, else None."""
    for command in commands:
        if command[0] == 'accept':
            session.finishSteps()   # Enter: queued tap steps finish first
            return ACCEPTED
        if command[0] == 'abort':
            session.abortSteps()    # Esc: queued tap steps are cancelled at once
            return ABORTED
        session.execute(command)
    return None


def dispatchKey(keyState, session, name, pressed, now=None):
    """Feed one key event through the logic and the driver (see runCommands)."""
    return runCommands(keyState.handleKey(name, pressed, now), session)


def dispatchTick(keyState, session, now=None):
    """Let the logic notice a hold that has passed the threshold."""
    return runCommands(keyState.tick(now), session)


# ---------------------------------------------------------------------------
# The thin evdev part
# ---------------------------------------------------------------------------

def importEvdev():
    try:
        import evdev
    except ImportError as error:
        raise JogRefused("The evdev library is not installed; run checkJogEnvironment().") from error
    return evdev


def findKeyboards(evdev):
    """Every readable input device that reports all four arrow keys."""
    arrowCodes = [evdev.ecodes.ecodes[name] for name in ARROW_KEYS]
    keyboards = []
    for path in evdev.list_devices():
        try:
            device = evdev.InputDevice(path)
        except OSError:
            continue  # not readable by us
        keys = device.capabilities().get(evdev.ecodes.EV_KEY, [])
        if all(code in keys for code in arrowCodes):
            keyboards.append(device)
        else:
            device.close()
    return keyboards


def waitForKeysReleased(keyboards, timeout=3.0):
    # Grabbing while a key is down (e.g. the Enter that started this program) can
    # leave the desktop thinking that key is stuck.  Wait for all keys to be up.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any(device.active_keys() for device in keyboards):
            return
        time.sleep(0.05)


def runKeyboardJog(grbl, homed, xyFeed=XY_FEED, zFeed=Z_FEED, creepDivisor=CREEP_DIVISOR,
                   out=print, holdThreshold=None, showPosition=None):
    """Jog from the keyboard until Enter (returns ACCEPTED) or Esc (returns ABORTED).

    holdThreshold: seconds that tell a tap from a hold (default HOLD_THRESHOLD;
    0 turns tap-to-step off).

    showPosition: show the live position display (see PositionDisplay).  The
    default is to show it when printing to a real terminal.

    homed: whether $H has run since power-up; Grbl cannot tell us (see checkReadyToJog).
    Raises JogRefused, without moving anything, if jogging is not safe.
    """
    evdev = importEvdev()
    settings = grbl.getSettings()
    bounds = checkReadyToJog(grbl, settings, homed)
    keyboards = findKeyboards(evdev)
    if not keyboards:
        raise JogRefused("No readable keyboard with arrow keys under /dev/input/. "
                         "Run checkJogEnvironment() to check input-group access.")

    codeNames = {evdev.ecodes.ecodes[code]: name for code, name in EVDEV_KEY_NAMES.items()}
    keyState = JogKeyState(xyFeed, zFeed, creepDivisor, holdThreshold, clock=time.monotonic)
    if showPosition is None:
        showPosition = out is print and terminalShowsDisplay()
    display = PositionDisplay(bounds) if showPosition else None
    # With the display, jog messages go on its last line instead of scrolling it away.
    session = JogSession(grbl, bounds, display.message if display else out,
                         sleep=time.sleep, clock=time.monotonic)

    out(f"Jog mode: arrows X/Y, Shift+Up/Down Z. Tap to step {keyState.tapStep:g} mm "
        f"(Alt+tap {keyState.fineTapStep:g} mm), hold to jog; hold Ctrl to creep "
        f"(1/{creepDivisor:g} speed).")
    out("Enter accepts (after any queued steps finish), Esc aborts at once. "
        "The keyboard is captured until then.")
    waitForKeysReleased(keyboards)
    grabbed = []
    try:
        for device in keyboards:
            device.grab()
            grabbed.append(device)
        if grbl.getGrblState() == 'Jog':
            session.cancel()  # a jog left over from before: stop it first
        if display:
            display.start()
        devicesByFd = {device.fd: device for device in keyboards}
        lastPoll = lastDisplay = time.monotonic()
        while True:
            # Wake up in time to notice an arrow held past the hold threshold.
            timeout = DISPLAY_INTERVAL if display else POLL_INTERVAL
            deadline = keyState.nextDeadline()
            if deadline is not None:
                timeout = max(0.0, min(timeout, deadline - time.monotonic()))
            readable, _, _ = select.select(list(devicesByFd), [], [], timeout)
            for fd in readable:
                for event in devicesByFd[fd].read():
                    # value 1 = press, 0 = release, 2 = auto-repeat (ignored)
                    if event.type != evdev.ecodes.EV_KEY or event.value == 2:
                        continue
                    name = codeNames.get(event.code)
                    if name is None:
                        continue
                    result = dispatchKey(keyState, session, name, event.value == 1)
                    if result:
                        return result
            dispatchTick(keyState, session)
            # Auto-repeat keeps select() busy while a key is held, so poll by time.
            if time.monotonic() - lastPoll >= POLL_INTERVAL:
                session.poll()
                lastPoll = time.monotonic()
            if display and time.monotonic() - lastDisplay >= DISPLAY_INTERVAL:
                display.update(*reversed(session.status()))
                lastDisplay = time.monotonic()
    finally:
        # Stop the machine first, then give the keyboard back, whatever happened.
        stopError = None
        try:
            session.stop()
        except Exception as error:  # still ungrab; report rather than mask the original error
            stopError = error
        if display:
            # Leave the block showing where it stopped, and the cursor back on.
            try:
                display.update(*reversed(session.status()))
            except Exception:
                pass
            display.close()
        if stopError is not None:
            out(f"Warning: jog-cancel after jog mode failed: {stopError}")
        for device in grabbed:
            try:
                device.ungrab()
            except OSError:
                pass
        for device in keyboards:
            device.close()


# ---------------------------------------------------------------------------
# Environment check
# ---------------------------------------------------------------------------

def gatherJogEnvironmentFacts():
    """What checkJogEnvironment() decides from, as a dict.  No side effects."""
    importlib.invalidate_caches()
    try:
        import evdev  # noqa: F401
        evdevImportable = True
    except ImportError:
        evdevImportable = False

    user = getpass.getuser()
    try:
        inputGroup = grp.getgrnam('input')
    except KeyError:
        inputGroup = None
    inGroupNow = os.geteuid() == 0 or bool(inputGroup and inputGroup.gr_gid in os.getgroups())
    listedInGroup = False
    if inputGroup:
        listedInGroup = user in inputGroup.gr_mem
        try:
            listedInGroup = listedInGroup or pwd.getpwnam(user).pw_gid == inputGroup.gr_gid
        except KeyError:
            pass
    readableDevice = any(os.access(path, os.R_OK) for path in glob.glob('/dev/input/event*'))
    return {'evdevImportable': evdevImportable, 'user': user, 'inGroupNow': inGroupNow,
            'listedInGroup': listedInGroup, 'readableDevice': readableDevice}


def askYes(prompt):
    try:
        return input(prompt).strip().lower() in ('y', 'yes')
    except EOFError:
        return False


def runFix(command, out):
    # The terminal's stdin/stdout/stderr are inherited (nothing captured), so a sudo
    # password prompt reaches the user directly.  Returns True on exit status 0.
    out(f"Running: {shlex.join(command)}")
    try:
        completed = subprocess.run(command)
    except OSError as error:
        out(f"Could not run it: {error}. Please run it yourself.")
        return False
    out(f"Exit status {completed.returncode}.")
    if completed.returncode != 0:
        out("That failed. Please fix it by hand, then try again.")
        return False
    return True


def checkJogEnvironment(out=print):
    """Check that keyboard jog can work here; offer to fix what is missing.

    Returns ENVIRONMENT_OK (silently, when nothing is wrong),
    UNAVAILABLE_UNTIL_RELOGIN, or UNAVAILABLE.  Never runs a command without a
    yes at "Run it now? [y/N]".  The caller can carry on without jog on
    anything but ENVIRONMENT_OK.
    """
    facts = gatherJogEnvironmentFacts()
    results = []

    if not facts['evdevImportable']:
        command = [sys.executable, '-m', 'pip', 'install', 'evdev']
        out("Keyboard jog needs the evdev library, which this Python can't import. Fix:")
        out(f"    {shlex.join(command)}")
        if askYes("Run it now? [y/N] ") and runFix(command, out) \
                and gatherJogEnvironmentFacts()['evdevImportable']:
            results.append(ENVIRONMENT_OK)
        else:
            results.append(UNAVAILABLE)

    if facts['readableDevice']:
        pass  # this session can read input events (input group, or root)
    elif facts['listedInGroup'] and not facts['inGroupNow']:
        # usermod already done; this login session predates it.
        out("You are in the 'input' group, but only from your next login: log out and "
            "back in to use keyboard jog.")
        results.append(UNAVAILABLE_UNTIL_RELOGIN)
    elif not facts['inGroupNow']:
        out("Keyboard jog reads /dev/input/event*, which needs membership of the 'input' group. Fix:")
        out("    sudo usermod -aG input $USER")
        out("then log out and back in.")
        if askYes("Run it now? [y/N] ") and runFix(
                ['sudo', 'usermod', '-aG', 'input', facts['user']], out):
            out("Done. The group membership takes effect only after you log out and back "
                "in; keyboard jog is unavailable until then.")
            results.append(UNAVAILABLE_UNTIL_RELOGIN)
        else:
            results.append(UNAVAILABLE)
    else:
        out("You are in the 'input' group but no /dev/input/event* device is readable. "
            "Is a keyboard connected?")
        results.append(UNAVAILABLE)

    if UNAVAILABLE in results:
        return UNAVAILABLE
    if UNAVAILABLE_UNTIL_RELOGIN in results:
        return UNAVAILABLE_UNTIL_RELOGIN
    return ENVIRONMENT_OK


# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Hold-to-jog a Grbl machine from the keyboard")
    parser.add_argument("--port", default="/dev/ttyUSB0")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--home", action="store_true", help="run the homing cycle ($H) first")
    parser.add_argument("--xy-feed", type=float, default=XY_FEED, help="mm/min")
    parser.add_argument("--z-feed", type=float, default=Z_FEED, help="mm/min")
    parser.add_argument("--hold-threshold", type=float, default=HOLD_THRESHOLD,
                        help="seconds; an arrow released sooner is a tap (one step), 0 turns taps off")
    parser.add_argument("--no-position-display", action="store_true",
                        help="don't show the live X/Y/Z position and limits while jogging")
    arguments = parser.parse_args()

    environment = checkJogEnvironment()
    if environment != ENVIRONMENT_OK:
        sys.exit(f"Keyboard jog: {environment}.")

    from GrblController import GrblController
    grbl = GrblController(arguments.port, arguments.baud)
    state = grbl.getGrblState()
    if state != 'Idle':
        sys.exit(f"Grbl is in state '{state}', not Idle; not jogging.")
    if arguments.home:
        grbl.runHomingCycle()
        homed = True
    else:
        homed = askYes("Has the machine been homed ($H) since it was powered on? [y/N] ")

    try:
        result = runKeyboardJog(grbl, homed, xyFeed=arguments.xy_feed, zFeed=arguments.z_feed,
                                holdThreshold=arguments.hold_threshold,
                                showPosition=False if arguments.no_position_display else None)
    except JogRefused as refusal:
        sys.exit(f"Not jogging: {refusal}")
    position = grbl.getMachineCoordinates()
    print(f"Jog {result}. MPos: X{position.x:.3f} Y{position.y:.3f} Z{position.z:.3f}")


if __name__ == "__main__":
    main()
