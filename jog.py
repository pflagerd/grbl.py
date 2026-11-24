#!/usr/bin/env python3
"""Hold-to-jog from the keyboard (issue #13, step 8).

    Left / Right          X- / X+
    Up / Down             Y+ / Y-
    Shift+Up / Shift+Down Z+ / Z-
    Ctrl (held)           a tenth of the speed, for creeping onto a surface
    Enter                 stop jogging, return "accepted"
    Esc                   stop jogging, return "aborted"

Motion starts when a key goes down and stops when it comes up.  A terminal
cannot report key releases, so key events come from the Linux input layer
(/dev/input/event*) through the `evdev` library.  While jogging, the keyboard
is grabbed: keystrokes go only to this program, whichever window has focus.

How a continuous jog works with Grbl 1.1h: one `$J=G91 ...` is sent whose
length runs to the edge of the machine's travel in that direction, and when
the key set changes, the real-time byte 0x85 (jog-cancel) makes Grbl
decelerate to a stop and discard the rest of the jog.  Grbl does the
acceleration and deceleration itself ($120-$122).

The parts are kept separate so the logic can be tested without evdev or a
machine:
    JogKeyState         pure: key events in, jog/cancel commands out
    planJog()           pure: direction + position + bounds -> one $J line
    softLimitBounds()   pure: $130-$132, $23, $27 -> machine-coordinate box
    JogSession          drives a GrblController (send/write/getStatus)
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

class JogKeyState:
    """Turns key presses/releases into commands.  Knows nothing about Grbl.

    handleKey() returns a list of commands, each a tuple:
        ('cancel',)                     stop the running jog
        ('jog', (dx, dy, dz), feed)     start a jog; each d is -1, 0 or +1
        ('accept',) / ('abort',)        Enter / Esc: end jog mode
    A changed key set always gives a cancel (if a jog is running) before the
    new jog, so Grbl never has two jogs queued.
    """

    def __init__(self, xyFeed=XY_FEED, zFeed=Z_FEED, creepDivisor=CREEP_DIVISOR):
        self.xyFeed = xyFeed
        self.zFeed = zFeed
        self.creepDivisor = creepDivisor
        self.held = set()
        self.active = None  # the (direction, feed) last commanded, or None when stopped

    def desired(self):
        # What the held keys ask for right now: (direction, feed), or None for "stand still".
        shift = bool(self.held & {'LEFTSHIFT', 'RIGHTSHIFT'})
        ctrl = bool(self.held & {'LEFTCTRL', 'RIGHTCTRL'})
        dx = ('RIGHT' in self.held) - ('LEFT' in self.held)   # both held: they cancel out
        vertical = ('UP' in self.held) - ('DOWN' in self.held)
        dy, dz = (0, vertical) if shift else (vertical, 0)
        direction = (dx, dy, dz)
        if direction == (0, 0, 0):
            return None
        feed = self.zFeed if dz else self.xyFeed  # F applies to the whole vector path
        if ctrl:
            feed = feed / self.creepDivisor
        return direction, feed

    def handleKey(self, name, pressed):
        if pressed:
            self.held.add(name)
        else:
            self.held.discard(name)

        if pressed and name in ('ENTER', 'ESC'):
            commands = [('cancel',)] if self.active else []
            self.active = None
            return commands + [('accept',) if name == 'ENTER' else ('abort',)]

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
        self.running = None  # (direction, feed, movingAxes) of the jog Grbl is running

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

    def waitUntilStopped(self):
        # After 0x85 Grbl decelerates in state Jog, then goes Idle.  The next $J must
        # wait for that, or it would be computed from a position that is still moving.
        deadline = self.clock() + CANCEL_TIMEOUT
        while True:
            state = self.grbl.getGrblState()
            if state == 'Alarm':
                raise JogError("Grbl went into Alarm while jogging (limit switch?). "
                               "Clear it and re-home before continuing.")
            if state != 'Jog':
                return state
            if self.clock() > deadline:
                raise JogError(f"Grbl was still jogging {CANCEL_TIMEOUT:g}s after jog-cancel.")
            self.sleep(0.02)

    def cancel(self):
        self.grbl.write(JOG_CANCEL)
        self.running = None
        self.waitUntilStopped()

    def jog(self, direction, feed, quiet=False):
        planned = planJog(direction, feed, self.machinePosition(), self.bounds)
        if planned is None:
            self.running = None
            if not quiet:
                self.out("At the travel limit in that direction; not moving.")
            return None
        gcode, movingAxes = planned
        self.grbl.send(gcode)
        self.running = (direction, feed, movingAxes)
        return gcode

    def execute(self, command):
        if command[0] == 'cancel':
            self.cancel()
        elif command[0] == 'jog':
            self.jog(command[1], command[2])
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


def dispatchKey(keyState, session, name, pressed):
    """Feed one key event through the logic and the driver.

    Returns ACCEPTED or ABORTED when Enter/Esc ends jog mode, else None.
    """
    for command in keyState.handleKey(name, pressed):
        if command[0] == 'accept':
            return ACCEPTED
        if command[0] == 'abort':
            return ABORTED
        session.execute(command)
    return None


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
                   out=print):
    """Jog from the keyboard until Enter (returns ACCEPTED) or Esc (returns ABORTED).

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
    keyState = JogKeyState(xyFeed, zFeed, creepDivisor)
    session = JogSession(grbl, bounds, out)

    out(f"Jog mode: arrows X/Y, Shift+Up/Down Z, hold Ctrl to creep (1/{creepDivisor:g} speed).")
    out("Enter accepts, Esc aborts. The keyboard is captured until then.")
    waitForKeysReleased(keyboards)
    grabbed = []
    try:
        for device in keyboards:
            device.grab()
            grabbed.append(device)
        if grbl.getGrblState() == 'Jog':
            session.cancel()  # a jog left over from before: stop it first
        devicesByFd = {device.fd: device for device in keyboards}
        lastPoll = time.monotonic()
        while True:
            readable, _, _ = select.select(list(devicesByFd), [], [], POLL_INTERVAL)
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
            # Auto-repeat keeps select() busy while a key is held, so poll by time.
            if time.monotonic() - lastPoll >= POLL_INTERVAL:
                session.poll()
                lastPoll = time.monotonic()
    finally:
        # Stop the machine first, then give the keyboard back, whatever happened.
        try:
            session.stop()
        except Exception as error:  # still ungrab; report rather than mask the original error
            out(f"Warning: jog-cancel after jog mode failed: {error}")
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
        result = runKeyboardJog(grbl, homed, xyFeed=arguments.xy_feed, zFeed=arguments.z_feed)
    except JogRefused as refusal:
        sys.exit(f"Not jogging: {refusal}")
    position = grbl.getMachineCoordinates()
    print(f"Jog {result}. MPos: X{position.x:.3f} Y{position.y:.3f} Z{position.z:.3f}")


if __name__ == "__main__":
    main()
