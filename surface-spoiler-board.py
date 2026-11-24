#!/usr/bin/env python3
"""Surface (level) the plywood spoilboard with a wide flat bit (issue #13, step 12).

    python surface-spoiler-board.py [--bit NAME | --diameter MM] [--one-way] ...

The flow, with nothing moving before an Enter from you:
  1. Pick a saved surfacing bit from profiles/bits.json, or add one.
  2. You may turn the motor knob NOW, for a rough move, before homing.  After
     homing, every move is by keyboard jog (see "Why the knob" below).  Home.
  3. Jog the bit centre to the board's front-left corner (low X, low Y), Enter;
     then to the back-right corner (high X, high Y), Enter.
  4. Jog the bit down until it just touches the plywood top, Enter.  That
     becomes work Z0 (G54, G10 L20 P1 Z0); its machine z is recorded.
  5. Stepover, depth per pass, feed, plunge, spindle and clearance: a prompt
     for each one not given on the command line, then a summary you can change.
  6. Before each pass: the Z soft-limit room check ($132) and the total-depth
     cap (--max-total-depth, default 3 mm).  Then one raster pass, and
     "Another pass ...? [Y / change depth / stop]".
  7. Spindle off, lift, park; offers calibrate.py's spoilboard Z limit,
     because the spoilboard top has moved.

Why the knob loses Grbl's position: $1=25 makes Grbl release the stepper
motors 25 ms after each move, so the knob turns freely.  Grbl counts the steps
it sends; it cannot see a turned knob.  After homing, a knob turn moves the
machine without moving Grbl's idea of where it is, and the soft-limit fence
moves with that error.

All motion is in machine coordinates (G53), so the program does not depend on
the G54 X/Y offset and can be checked directly against the soft limits.

The parts, kept separate so they can be tested without a machine:
    defaultFeed(), resolveParameter()   pure: where each default comes from
    planPassDepth(), checkZRoom()       pure: depth arithmetic, the $132 check
    planRaster()                        pure: board + bit + bounds -> G-code
    loadBits(), saveBits(), chooseBit() the bit list (profiles/bits.json)
    streamGcode(), waitForIdle(), safeStop()   talk to Grbl
    runSurfacing()                      the operator flow
"""

import argparse
import json
import math
import os
import sys
import tempfile
import time
from dataclasses import dataclass, field

import calibrate
import jog as jogModule

# --- Defaults -------------------------------------------------------------
# Feed: the old plane-surface.py cut with a 25 mm bit at 400 mm/min.  The
# stepover is a fixed share of the diameter, so at a fixed depth the material
# removed per minute is feed x stepover x depth, i.e. proportional to
# feed x diameter.  Keeping that constant gives feed = 400 x 25 / diameter.
REFERENCE_FEED = 400.0       # mm/min ...
REFERENCE_DIAMETER = 25.0    # ... with this bit diameter (mm)
FEED_MIN = 100.0             # mm/min; clamp for the derived feed
FEED_MAX = 1000.0

STEPOVER_PERCENT = 40.0      # of the bit diameter
DEPTH_PER_PASS = 0.3         # mm
PLUNGE_FEED = 50.0           # mm/min
SPINDLE_SPEED = 10000        # S word (RPM); always sent as a whole number
CLEARANCE = 5.0              # mm above the touch-off height
MAX_TOTAL_DEPTH = 3.0        # mm; a sanity cap (the plywood is held with tape)

SPIN_UP_SECONDS = 3          # G4 P3 after M3
APPROACH_HEIGHT = 1.0        # mm above the remaining surface; rapid down to here, then plunge
MINIMUM_CLEARANCE = 1.0      # mm; refuse a plan whose clearance had to shrink below this
ROOM_MARGIN = 0.1            # mm kept above the Z soft-limit floor (-$132)
MINIMUM_BOARD = 1.0          # mm; the corners must be at least this far apart in X and Y
MAX_LINE_LENGTH = 70         # characters; Grbl's line buffer is 80
DEFAULT_RAPID = 2000.0       # mm/min if $110-$112 are missing (time estimate only)

ZIGZAG = "zigzag"
ONE_WAY = "one-way"
PATTERNS = (ZIGZAG, ONE_WAY)

SCRIPT_DIRECTORY = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PROFILE = os.path.join(SCRIPT_DIRECTORY, "profiles", "6060 ProverXL V1.json")
BITS_FILE = "bits.json"      # next to the machine profiles; shared across machines


class PlanRefused(ValueError):
    """The requested cut is not possible or not safe; nothing was sent."""


class DepthRefused(ValueError):
    """The pass depth is not allowed (zero, negative, or past the total-depth cap)."""


class MachineError(RuntimeError):
    """Grbl refused a line, went into Alarm, or stopped answering."""


class Quit(Exception):
    """The operator chose to stop before any cutting."""


# ---------------------------------------------------------------------------
# Pure: defaults and their precedence
# ---------------------------------------------------------------------------

def defaultFeed(diameter):
    """Feed for a bit diameter: 400 mm/min at 25 mm, scaled by 1/diameter, clamped."""
    feed = round(REFERENCE_FEED * REFERENCE_DIAMETER / diameter)
    return int(min(FEED_MAX, max(FEED_MIN, feed)))


def resolveParameter(commandLine, saved, derived):
    """(value, source) by precedence: the command line, then the saved bit, then derived.

    source is 'command line', 'saved' or 'derived'.  A None means "not given".
    """
    if commandLine is not None:
        return commandLine, 'command line'
    if saved is not None:
        return saved, 'saved'
    return derived, 'derived'


# ---------------------------------------------------------------------------
# Pure: depth arithmetic and the Z room check
# ---------------------------------------------------------------------------

def planPassDepth(totalSoFar, passDepth, maxTotal):
    """The total depth after one more pass of passDepth, or raise DepthRefused.

    Rounded to 0.001 mm so repeated 0.3 mm passes add up to 0.6, 0.9, ... exactly.
    """
    if not passDepth > 0:
        raise DepthRefused(f"The depth per pass must be more than 0, not {passDepth:g}.")
    newTotal = round(totalSoFar + passDepth, 3)
    if newTotal > maxTotal + 1e-9:
        left = round(maxTotal - totalSoFar, 3)
        raise DepthRefused(f"A {passDepth:g} mm pass would make the total {newTotal:.3f} mm, past the "
                           f"{maxTotal:g} mm cap (--max-total-depth); {max(left, 0):.3f} mm is left.")
    return newTotal


def checkZRoom(touchZ, totalDepth, z132, margin=ROOM_MARGIN):
    """Is the bottom of the planned cut above Grbl's Z soft-limit floor?

    Grbl fences the spindle at machine z = -$132.  The cut bottoms out at
    touchZ - totalDepth.  Returns (ok, bottom, floor).
    """
    bottom = round(touchZ - totalDepth, 3)
    floor = -float(z132)
    return bottom >= floor + margin - 1e-9, bottom, floor


# ---------------------------------------------------------------------------
# Pure: the raster
# ---------------------------------------------------------------------------

@dataclass
class RasterPlan:
    gcode: list
    lineYs: list
    xStart: float
    xEnd: float
    stepover: float          # the even stepover actually used (<= requested)
    cutZ: float
    clearZ: float
    approachZ: float
    cutLength: float = 0.0   # mm at feed (G1, including plunges)
    rapidLength: float = 0.0  # mm at rapid (G0)
    seconds: float = 0.0     # estimate, ignoring acceleration
    notes: list = field(default_factory=list)


def axisCode(value):
    return f"{value:.3f}"


def planRaster(board, diameter, stepover, overrun, touchZ, totalDepth, passDepth, clearance,
               pattern, bounds, feed, plungeFeed, spindle, rapidRates=None,
               dwell=SPIN_UP_SECONDS, margin=jogModule.LIMIT_MARGIN):
    """One surfacing pass as a list of G-code lines (all G53, machine coordinates).

    board: (x0, y0, x1, y1), the bit-centre positions of the front-left and
           back-right corners.
    Lines run along X, from low Y to high Y.  Each line runs past both X edges
    by `overrun` (default: the bit radius), clamped to the soft-limit travel.
    The first line is centred on y0 and the last on y1, so the cutter covers
    the Y edges with half its width to spare; the lines between are spaced
    evenly at the largest stepover not above the one requested.
    totalDepth is the depth after this pass, below touchZ; passDepth is this
    pass's share of it (the material that is left starts passDepth higher).
    bounds: jog.softLimitBounds() of the current settings.
    """
    x0, y0, x1, y1 = board
    if pattern not in PATTERNS:
        raise PlanRefused(f"Unknown pattern '{pattern}'.")
    if not diameter > 0:
        raise PlanRefused("The bit diameter must be more than 0.")
    if not 0 < stepover <= diameter:
        raise PlanRefused(f"The stepover must be more than 0 and at most the bit diameter "
                          f"({diameter:g} mm), not {stepover:g}; a larger one leaves ridges.")
    if x1 - x0 < MINIMUM_BOARD or y1 - y0 < MINIMUM_BOARD:
        raise PlanRefused("The back-right corner must be to the right of (higher X) and behind "
                          "(higher Y) the front-left corner.")
    if overrun < 0:
        raise PlanRefused("The overrun must be 0 or more.")

    xLow, xHigh = bounds['X'][0] + margin, bounds['X'][1] - margin
    yLow, yHigh = bounds['Y'][0] + margin, bounds['Y'][1] - margin
    zLow, zHigh = bounds['Z'][0] + margin, bounds['Z'][1] - margin
    if x0 < xLow - 1e-9 or x1 > xHigh + 1e-9 or y0 < yLow - 1e-9 or y1 > yHigh + 1e-9:
        raise PlanRefused("The board corners are outside the soft-limit travel.")
    notes = []

    xStart, xEnd = x0 - overrun, x1 + overrun
    if xStart < xLow:
        notes.append(f"The overrun past the left (low X) edge is shrunk from {overrun:.3f} to "
                     f"{x0 - xLow:.3f} mm to stay inside the soft-limit travel.")
        xStart = xLow
    if xEnd > xHigh:
        notes.append(f"The overrun past the right (high X) edge is shrunk from {overrun:.3f} to "
                     f"{xHigh - x1:.3f} mm to stay inside the soft-limit travel.")
        xEnd = xHigh
    radius = diameter / 2
    if min(x0 - xStart, xEnd - x1) < radius - 1e-9:
        notes.append("With less overrun than the bit radius, the plunge and the Y steps happen "
                     "partly over the board instead of in the air.")

    span = y1 - y0
    intervals = max(1, math.ceil(span / stepover - 1e-9))
    actualStepover = span / intervals
    lineYs = [round(y0 + i * actualStepover, 3) for i in range(intervals)] + [round(y1, 3)]

    cutZ = round(touchZ - totalDepth, 3)
    if cutZ < zLow - 1e-9:
        raise PlanRefused(f"The cut bottom z={cutZ:.3f} is below the Z soft limit z={zLow - margin:.3f}.")
    clearZ = round(touchZ + clearance, 3)
    if clearZ > zHigh:
        notes.append(f"The clearance is shrunk from {clearance:g} to {zHigh - touchZ:.3f} mm: the top "
                     "of the Z travel is in the way.")
        clearZ = round(zHigh, 3)
    if clearZ - touchZ < MINIMUM_CLEARANCE:
        raise PlanRefused(f"Only {clearZ - touchZ:.3f} mm of clearance fits above the touch-off "
                          f"(at least {MINIMUM_CLEARANCE:g} needed).")
    approachZ = round(min(clearZ, touchZ - (totalDepth - passDepth) + APPROACH_HEIGHT), 3)

    rates = rapidRates or {'X': DEFAULT_RAPID, 'Y': DEFAULT_RAPID, 'Z': DEFAULT_RAPID}
    feedWord = f"F{feed:g}"
    plungeWord = f"F{plungeFeed:g}"
    gcode = ["G90 G21 G94"]
    plan = RasterPlan(gcode, lineYs, round(xStart, 3), round(xEnd, 3), actualStepover,
                      cutZ, clearZ, approachZ, notes=notes)
    position = {'X': xStart, 'Y': y0, 'Z': None}  # the estimate starts over the start point

    def move(kind, rate=None, **target):
        words = " ".join(f"{axis}{axisCode(value)}" for axis, value in target.items())
        line = f"G53 {kind} {words}" + (f" F{rate:g}" if kind == 'G1' else "")
        gcode.append(line)
        deltas = {axis: abs(value - position[axis]) if position[axis] is not None else 0.0
                  for axis, value in target.items()}
        length = math.sqrt(sum(d * d for d in deltas.values()))
        if kind == 'G1':
            plan.cutLength += length
            plan.seconds += length / rate * 60
        else:
            plan.rapidLength += length
            plan.seconds += max((d / rates[axis] * 60 for axis, d in deltas.items()), default=0.0)
        position.update(target)

    def plunge():
        move('G0', Z=approachZ)
        move('G1', plungeFeed, Z=cutZ)

    move('G0', Z=clearZ)                       # off the surface before the spindle starts
    gcode.append(f"M3 S{int(round(spindle))}")
    gcode.append(f"G4 P{dwell:g}")
    plan.seconds += dwell
    move('G0', X=xStart, Y=lineYs[0])
    plunge()
    for index, y in enumerate(lineYs):
        if pattern == ZIGZAG:
            if index:
                move('G1', feed, Y=y)          # the step to the next line, at the line end
            target = xEnd if index % 2 == 0 else xStart
            move('G1', feed, X=target)
        else:
            if index:
                move('G0', Z=clearZ)           # lift for the return
                move('G0', X=xStart, Y=y)
                plunge()
            move('G1', feed, X=xEnd)
    move('G0', Z=clearZ)
    gcode.append("M5")

    for line in gcode:
        if len(line) > MAX_LINE_LENGTH:
            raise PlanRefused(f"G-code line too long for Grbl: {line}")
    return plan


def formatDuration(seconds):
    minutes, seconds = divmod(int(round(seconds)), 60)
    return f"{minutes} min {seconds:02d} s"


# ---------------------------------------------------------------------------
# The bit list: profiles/bits.json
# ---------------------------------------------------------------------------
#
# {"bits": {"35mm-flat": {"diameter": 35.0, "flutes": 2, "feed": 286,
#                         "depthPerPass": 0.3, "stepoverPercent": 40.0,
#                         "plunge": 50, "note": "..."}}, "schemaVersion": 1}
# Only "diameter" is required.

BIT_SETTINGS = ('feed', 'depthPerPass', 'stepoverPercent', 'plunge')


def bitsPath(profilePath):
    return os.path.join(os.path.dirname(os.path.abspath(profilePath)), BITS_FILE)


def loadBits(path):
    """{name: entry}; an empty dict if the file does not exist yet."""
    if not os.path.exists(path):
        return {}
    with open(path) as jsonFile:
        return json.load(jsonFile).get("bits", {})


def saveBits(path, bits):
    """Write atomically: a temp file in the same directory, then rename over the old file.

    A failure part-way leaves the old file as it was.  Indented, sorted keys, so
    it stays easy to edit by hand.
    """
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".bits-", suffix=".json", dir=directory)
    try:
        with os.fdopen(handle, 'w') as jsonFile:
            json.dump({"schemaVersion": 1, "bits": bits}, jsonFile, indent=2, sort_keys=True)
            jsonFile.write('\n')
            jsonFile.flush()
            os.fsync(jsonFile.fileno())
        os.replace(temporary, path)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


def describeBit(name, entry):
    text = f"{name}: {entry['diameter']:g} mm"
    if entry.get('flutes'):
        text += f", {entry['flutes']} flutes"
    saved = [f"{key} {entry[key]:g}" for key in BIT_SETTINGS if entry.get(key) is not None]
    if saved:
        text += " (" + ", ".join(saved) + ")"
    if entry.get('note'):
        text += f" - {entry['note']}"
    return text


def askOptional(ask, prompt, convert):
    # Enter skips (returns None); asks again on a bad answer.
    while True:
        text = ask(prompt).strip()
        if not text:
            return None
        try:
            return convert(text)
        except ValueError:
            print(f"  '{text}' is not valid; press Enter to skip.")


def askPositive(ask, prompt, default=None):
    # A number > 0; Enter takes the default when there is one.
    while True:
        text = ask(prompt).strip()
        if not text and default is not None:
            return default
        try:
            value = float(text)
        except ValueError:
            print(f"  '{text}' is not a number.")
            continue
        if math.isnan(value) or value <= 0:
            print("  It must be more than 0.")
            continue
        return value


def askNewBit(ask, out, bits, path, diameter=None):
    """Ask for a name (and the diameter, unless known), plus flutes; save.  Returns the name."""
    while True:
        name = ask("Name for the new bit (e.g. 35mm-flat): ").strip()
        if not name:
            out("  The name can't be empty.")
        elif name in bits:
            out(f"  There is already a bit called '{name}'.")
        else:
            break
    if diameter is None:
        diameter = askPositive(ask, "Bit diameter in mm: ")
    entry = {'diameter': diameter}
    flutes = askOptional(ask, "Number of flutes (Enter to skip): ", int)
    if flutes is not None:
        entry['flutes'] = flutes
    bits[name] = entry
    saveBits(path, bits)
    out(f"  Saved bit '{name}' in {path}.")
    return name


def chooseBit(bits, path, bitName, diameter, ask, out):
    """Returns (name or None, entry).  See the module docstring for the rules."""
    if bitName is not None:
        if bitName not in bits:
            known = ", ".join(sorted(bits)) or "none saved yet"
            raise PlanRefused(f"No bit called '{bitName}' in {path} ({known}).")
        return bitName, dict(bits[bitName])
    if diameter is not None:
        if calibrate.askYesNo(ask, f"Save this {diameter:g} mm bit in {path} for next time?"):
            name = askNewBit(ask, out, bits, path, diameter)
            return name, dict(bits[name])
        return None, {'diameter': diameter}
    names = sorted(bits)
    if not names:
        out(f"  No surfacing bits saved yet in {path}; add one.")
        name = askNewBit(ask, out, bits, path)
        return name, dict(bits[name])
    out("  Saved surfacing bits:")
    for number, name in enumerate(names, 1):
        out(f"    {number}. {describeBit(name, bits[name])}")
    while True:
        answer = ask(f"Pick a bit (1-{len(names)}), or a to add a new one: ").strip().lower()
        if answer == 'a':
            name = askNewBit(ask, out, bits, path)
            return name, dict(bits[name])
        if answer.isdigit() and 1 <= int(answer) <= len(names):
            name = names[int(answer) - 1]
            return name, dict(bits[name])
        out(f"  Please answer a number from 1 to {len(names)}, or a.")


def offerSaveBitSettings(ask, out, bits, path, name, diameter, used):
    """After a run with at least one completed pass.  used: feed, depthPerPass,
    stepoverPercent, plunge.  Returns the name saved under, or None."""
    if name is None:
        if not calibrate.askYesNo(ask, f"Save this {diameter:g} mm bit and these settings as a new bit?"):
            return None
        name = askNewBit(ask, out, bits, path, diameter)
    elif ask(f"Save these settings for {name}? [y/N] ").strip().lower() not in ('y', 'yes'):
        return None
    bits = loadBits(path) or bits   # re-read: keep hand edits made during the run
    entry = bits.setdefault(name, {'diameter': diameter})
    entry.update(used)
    saveBits(path, bits)
    out(f"  Saved {', '.join(f'{key} {value:g}' for key, value in used.items())} for '{name}'.")
    return name


# ---------------------------------------------------------------------------
# Talking to Grbl
# ---------------------------------------------------------------------------

def readStatus(grbl, attempts=3):
    """A raw status report: '?' without a newline, so no 'ok' comes back.

    GrblController.getStatus() sends '?\\n' and stops at the first 'ok'; after
    an interrupted stream an old 'ok' can still be waiting, and it would
    return nothing.  This skips any 'ok' lines and waits for the '<...>' line.
    """
    original = grbl.timeout
    grbl.timeout = 1
    try:
        for _ in range(attempts):
            grbl.write(b'?')
            for _ in range(50):
                line = grbl.readline()
                if line == b'':
                    break
                text = line.decode(errors='replace').strip()
                if text.startswith('<'):
                    return text
    finally:
        grbl.timeout = original
    return None


def stateOf(report):
    """'<Hold:0|...>' -> ('Hold', '0');  None -> (None, '')."""
    if not report:
        return None, ''
    state, _, substate = report[1:].split('|')[0].partition(':')
    return state, substate


def streamGcode(grbl, lines, timeout, out=print, clock=time.monotonic):
    """Send each line and wait for its 'ok' before the next (send-response).

    Grbl answers 'ok' as soon as a line is parsed into its planner buffer (16
    motions), not when the motion is done, so the planner stays full while
    only one line is ever in Grbl's 128-byte serial buffer.  Unlike
    GrblController.send(), a silent read is NOT taken as an 'ok': a long line
    can sit behind a full planner for a minute or more.  timeout bounds the
    wait for one 'ok' (a sync command such as M5 waits for the whole queue).
    """
    original = grbl.timeout
    grbl.timeout = 1
    try:
        for index, line in enumerate(lines, 1):
            grbl.write((line + '\n').encode())
            deadline = clock() + timeout
            while True:
                reply = grbl.readline()
                if reply == b'':
                    if clock() > deadline:
                        raise MachineError(f"No 'ok' from Grbl within {timeout:.0f} s of line "
                                           f"{index}: {line}")
                    continue
                text = reply.decode(errors='replace').strip()
                if text == 'ok':
                    break
                if text.startswith('error:'):
                    raise MachineError(f"Grbl refused line {index} ({line}): {text}")
                if text.startswith('ALARM:'):
                    raise MachineError(f"Grbl went into {text} at line {index} ({line}).")
                if text.startswith('[MSG:'):
                    out(f"  Grbl: {text}")
    finally:
        grbl.timeout = original


def waitForIdle(grbl, timeout, sleep=time.sleep, clock=time.monotonic):
    deadline = clock() + timeout
    while True:
        state, _ = stateOf(readStatus(grbl))
        if state == 'Idle':
            return
        if state == 'Alarm':
            raise MachineError("Grbl is in Alarm (limit switch or soft limit).")
        if clock() > deadline:
            raise MachineError(f"Grbl did not reach Idle within {timeout:.0f} s (state {state}).")
        sleep(0.5)


def safeStop(grbl, out=print, sleep=time.sleep, clock=time.monotonic):
    """After Ctrl-C or any error: stop motion and the spindle, and say what is left.

    Running (or held, or jogging): feed hold '!' decelerates to a stop without
    losing position.  Once Grbl reports Hold:0 (hold complete), a soft reset
    (Ctrl-X) empties the queue and stops the spindle, still without losing
    position.  A plain M5 would wait in the queue behind the held motion.
    Idle: nothing is queued, so M5 is enough.  Alarm: Grbl already stopped.
    """
    actions = []
    try:
        state, _ = stateOf(readStatus(grbl))
    except Exception:
        state = None
    try:
        if state in ('Run', 'Hold', 'Jog', None):
            grbl.write(b'!')
            actions.append("feed hold (!)")
            deadline = clock() + 15
            while True:
                state, substate = stateOf(readStatus(grbl))
                if (state == 'Hold' and substate == '0') or state in ('Idle', 'Alarm'):
                    break
                if clock() > deadline:
                    break
                sleep(0.1)
            if state != 'Alarm':
                grbl.softReset()
                actions.append("soft reset (Ctrl-X), which also stops the spindle")
        elif state == 'Idle':
            grbl.write(b'M5\n')
            actions.append("M5 (spindle off)")
        report = readStatus(grbl)
    except Exception as error:
        out(f"!! The safety stop itself failed: {error}.  Use the reset button or power switch.")
        return None
    state, _ = stateOf(report)
    _, mpos, _, _ = jogModule.parseStatus(report) if report else (None, None, None, None)
    where = f" at MPos X{mpos['X']:.3f} Y{mpos['Y']:.3f} Z{mpos['Z']:.3f}" if mpos else ""
    out(f"!! Stopped: {', '.join(actions) or 'nothing to send'}.  Machine state: {state}{where}.")
    if state == 'Alarm':
        out("!! Grbl is in Alarm: the position may be lost.  Home ($H) before anything else.")
    elif state is None:
        out("!! No status report from Grbl.  Check the machine; use the reset button if it is moving.")
    else:
        out("!! The position is kept; the bit may still be in the wood, so lift it by jog.")
    return state


def rapidRates(settings):
    rates = {}
    for axis, key in (('X', '$110'), ('Y', '$111'), ('Z', '$112')):
        try:
            rates[axis] = float(settings[key])
        except (KeyError, ValueError):
            rates[axis] = DEFAULT_RAPID
    return rates


# ---------------------------------------------------------------------------
# The operator flow
# ---------------------------------------------------------------------------

STEP_TITLES = {'bit': "bit", 'board': "board", 'touch': "touch-off", 'stepover': "stepover",
               'depth': "depth this pass", 'feed': "feed", 'plunge': "plunge",
               'spindle': "spindle", 'clearance': "clearance", 'pattern': "pattern"}
STEP_ORDER = ('bit', 'board', 'touch', 'stepover', 'depth', 'feed', 'plunge', 'spindle',
              'clearance', 'pattern')
BIT_DEPENDENT = ('stepover', 'depth', 'feed', 'plunge')  # their defaults come from the bit

SAFETY_REMINDER = (
    "Before cutting:\n"
    "  - hearing and eye protection on; a wide bit throws chips a long way\n"
    "  - dust: extraction or a mask; plywood dust is fine and full of glue\n"
    "  - the double-sided tape must hold the plywood against SIDEWAYS force,\n"
    "    not just downward; press the corners down and check none lifts\n"
    "  - Ctrl-C stops it: feed hold, then reset, which also stops the spindle")


class Surfacing:
    """The operator flow.  Everything that talks to people or the machine is injected."""

    def __init__(self, grbl, profile, profilePath, options, ask=None, out=print, runJog=None,
                 checkEnvironment=None, sleep=time.sleep, clock=time.monotonic):
        self.grbl = grbl
        self.profile = profile
        self.profilePath = profilePath
        self.options = options
        self.ask = ask or calibrate.readAnswer
        self.out = out
        self.runJog = runJog or jogModule.runKeyboardJog
        self.checkEnvironment = checkEnvironment or jogModule.checkJogEnvironment
        self.sleep = sleep
        self.clock = clock
        self.bitsPath = bitsPath(profilePath)
        self.bits = loadBits(self.bitsPath)
        self.choices = {}
        self.sources = {}
        self.totalDepth = 0.0
        self.passesDone = 0
        self.lastPlan = None

    # --- parameters ---------------------------------------------------------

    def bitEntry(self):
        return self.choices['bit'][1]

    def diameter(self):
        return float(self.bitEntry()['diameter'])

    def derived(self, name):
        diameter = self.diameter()
        entry = self.bitEntry()
        saved = {'stepover': (entry['stepoverPercent'] * diameter / 100
                              if entry.get('stepoverPercent') is not None else None),
                 'depth': entry.get('depthPerPass'), 'feed': entry.get('feed'),
                 'plunge': entry.get('plunge')}.get(name)
        derived = {'stepover': STEPOVER_PERCENT * diameter / 100, 'depth': DEPTH_PER_PASS,
                   'feed': defaultFeed(diameter), 'plunge': PLUNGE_FEED,
                   'spindle': SPINDLE_SPEED, 'clearance': CLEARANCE}[name]
        commandLine = getattr(self.options, name, None)
        return resolveParameter(commandLine, saved, derived)

    def defaultReason(self, name, source):
        bitName = self.choices['bit'][0]
        if source == 'command line':
            return f"from --{name}"
        if source == 'saved':
            return f"saved for {bitName}"
        if source == 'typed':
            return "typed"
        diameter = self.diameter()
        return {'stepover': f"{STEPOVER_PERCENT:g}% of the {diameter:g} mm diameter",
                'depth': "default", 'feed': f"from {diameter:g} mm bit",
                'plunge': "default", 'spindle': "default", 'clearance': "default"}[name]

    def describe(self, name):
        value = self.choices[name]
        source = self.sources.get(name)
        if name == 'bit':
            bitName, entry = value
            return describeBit(bitName, entry) if bitName else f"{entry['diameter']:g} mm (not saved)"
        if name == 'board':
            x0, y0, x1, y1 = value
            return (f"front-left X{x0:.3f} Y{y0:.3f}, back-right X{x1:.3f} Y{y1:.3f} "
                    f"({x1 - x0:.3f} x {y1 - y0:.3f} mm)")
        if name == 'touch':
            return f"plywood top at MPos z={value:.3f} (work Z0)"
        if name == 'pattern':
            return value + (" (every line +X, lift to return)" if value == ONE_WAY
                            else " (alternate +X / -X)")
        reason = self.defaultReason(name, source)
        if name == 'feed':
            return f"{value:g} mm/min ({reason}; override with --feed)"
        if name == 'plunge':
            return f"{value:g} mm/min ({reason})"
        if name == 'spindle':
            return f"S{int(round(value))} ({reason})"
        if name == 'depth':
            after = round(self.totalDepth + value, 3)
            return (f"{value:g} mm ({reason}); total after this pass {after:.3f} mm "
                    f"(so far {self.totalDepth:.3f}; cap {self.options.max_total_depth:g})")
        return f"{value:g} mm ({reason})"

    def fill(self, name):
        if name == 'bit':
            self.choices['bit'] = chooseBit(self.bits, self.bitsPath, self.options.bit,
                                            self.options.diameter, self.ask, self.out)
            self.options.bit = self.options.diameter = None  # used once; a change asks again
            self.bits = loadBits(self.bitsPath) or self.bits
        elif name == 'board':
            self.choices['board'] = self.measureBoard()
        elif name == 'touch':
            self.choices['touch'] = self.touchOff()
        elif name == 'pattern':
            if 'pattern' in self.sources:  # asked to change it
                self.choices['pattern'] = calibrate.askChoice(
                    self.ask, f"Pattern ({' / '.join(PATTERNS)}): ", PATTERNS)
            else:
                self.choices['pattern'] = ONE_WAY if self.options.one_way else ZIGZAG
            self.sources['pattern'] = 'given'
        else:
            value, source = self.derived(name)
            if source != 'command line':
                unit = {'feed': "mm/min", 'plunge': "mm/min", 'spindle': "(S word)"}.get(name, "mm")
                label = STEP_TITLES[name].replace(" this pass", " per pass").capitalize()
                typed = askPositive(self.ask, f"{label} in {unit} [{value:g}, "
                                              f"{self.defaultReason(name, source)}]: ", default=value)
                if typed != value:
                    value, source = typed, 'typed'
            if name == 'spindle':
                value = int(round(value))
            self.choices[name], self.sources[name] = value, source

    def forget(self, name):
        # The operator asked to change `name`: it is asked again, even if it came
        # from the command line.  A new bit also re-asks the values whose defaults
        # came from the old bit, but keeps those given on the command line.
        self.choices.pop(name, None)
        if name in vars(self.options) and name != 'bit':
            setattr(self.options, name, None)
        if name == 'bit':
            for dependent in BIT_DEPENDENT:
                if self.sources.get(dependent) != 'command line':
                    self.choices.pop(dependent, None)
                    self.sources.pop(dependent, None)
        elif name != 'pattern':
            self.sources.pop(name, None)

    # --- measurements -------------------------------------------------------

    def jogTo(self, prompt):
        while True:
            self.ask(prompt + "  Enter accepts, Esc aborts.  Press Enter to start jogging. ")
            if self.runJog(self.grbl, True, out=self.out) == jogModule.ACCEPTED:
                return self.grbl.getMachineCoordinates()
            if not calibrate.askYesNo(self.ask, "Jog aborted (Esc).  Try again?"):
                raise Quit()

    def measureBoard(self):
        while True:
            first = self.jogTo("Jog the bit CENTRE over the board's FRONT-LEFT corner (low X, low Y)."
                               "  Arrows move X/Y; raise Z first (Shift+Up) if it is low.")
            second = self.jogTo("Jog the bit CENTRE over the board's BACK-RIGHT corner "
                                "(high X, high Y).")
            x0, y0, x1, y1 = first.x, first.y, second.x, second.y
            problems = []
            if x1 - x0 < MINIMUM_BOARD:
                problems.append(f"X{x1:.3f} is not to the right of X{x0:.3f}")
            if y1 - y0 < MINIMUM_BOARD:
                problems.append(f"Y{y1:.3f} is not behind Y{y0:.3f}")
            if not problems:
                self.out(f"  Board: {x1 - x0:.3f} x {y1 - y0:.3f} mm.")
                return (x0, y0, x1, y1)
            self.out(f"  The second corner must be up and to the right of the first: "
                     f"{'; '.join(problems)}.  Measure both corners again.")

    def touchOff(self):
        position = self.jogTo("Jog the bit DOWN until it just touches the plywood top: Shift+Down, "
                              "hold Ctrl to creep (a sheet of paper under the bit helps).  If it "
                              "stops with 'At the travel limit', $132 is in the way: Esc, then "
                              "calibrate.py --reset-z-limit.")
        self.grbl.send("G10 L20 P1 Z0")
        self.out(f"  Work Z0 (G54) set at the plywood top, MPos z={position.z:.3f}.")
        return position.z

    # --- planning -----------------------------------------------------------

    def plan(self, passDepth):
        settings = self.grbl.getSettings()
        bounds = jogModule.softLimitBounds(settings)
        diameter = self.diameter()
        overrun = self.options.overrun if self.options.overrun is not None else diameter / 2
        totalAfter = round(self.totalDepth + passDepth, 3)
        return planRaster(self.choices['board'], diameter, self.choices['stepover'], overrun,
                          self.choices['touch'], totalAfter, passDepth, self.choices['clearance'],
                          self.choices['pattern'], bounds, self.choices['feed'],
                          self.choices['plunge'], self.choices['spindle'], rapidRates(settings))

    def summary(self):
        """The change-your-mind loop.  Returns once the operator answers y."""
        while True:
            missing = [name for name in STEP_ORDER if name not in self.choices]
            if missing:
                self.fill(missing[0])
                continue
            refusal = plan = None
            try:
                planPassDepth(self.totalDepth, self.choices['depth'], self.options.max_total_depth)
                plan = self.plan(self.choices['depth'])
            except (PlanRefused, DepthRefused) as error:
                refusal = error
            self.out("\n--- Surfacing summary (nothing moves yet) ---")
            for number, name in enumerate(STEP_ORDER, 1):
                self.out(f"  {number}. {STEP_TITLES[name].capitalize()}: {self.describe(name)}")
            if refusal:
                self.out(f"  REFUSED: {refusal}")
            else:
                self.describePlan(plan)
                ok, bottom, floor = checkZRoom(self.choices['touch'], self.totalDepth
                                               + self.choices['depth'],
                                               self.grbl.getSettings()['$132'])
                self.out("  Z room: " + (f"ok (cut bottom z={bottom:.3f}, $132 floor z={floor:.3f})"
                                         if ok else f"NOT enough: cut bottom z={bottom:.3f} is below "
                                         f"the $132 floor z={floor:.3f}; a reset is offered next"))
            prompt = (("y to start, " if not refusal else "")
                      + f"1-{len(STEP_ORDER)} to change that step, b to change the most recent, "
                        "q to quit: ")
            answer = self.ask(prompt).strip().lower()
            if answer == 'q':
                raise Quit()
            if answer == 'y' and not refusal:
                return
            if answer == 'b':
                self.forget(STEP_ORDER[-1])
            elif answer.isdigit() and 1 <= int(answer) <= len(STEP_ORDER):
                self.forget(STEP_ORDER[int(answer) - 1])

    def describePlan(self, plan):
        self.out(f"  Lines: {len(plan.lineYs)} along X, X{plan.xStart:.3f} to X{plan.xEnd:.3f}, "
                 f"Y{plan.lineYs[0]:.3f} to Y{plan.lineYs[-1]:.3f} (front to back), "
                 f"stepover {plan.stepover:.3f} mm")
        self.out(f"  Path: {plan.cutLength:.0f} mm cutting + {plan.rapidLength:.0f} mm rapid; "
                 f"about {formatDuration(plan.seconds)} per pass (acceleration not counted)")
        self.out(f"  Cut at z={plan.cutZ:.3f}, clearance z={plan.clearZ:.3f}")
        for note in plan.notes:
            self.out(f"  Note: {note}")

    # --- checks before a pass -----------------------------------------------

    def ensureZRoom(self, totalAfter):
        settings = self.grbl.getSettings()
        ok, bottom, floor = checkZRoom(self.choices['touch'], totalAfter, settings['$132'])
        if ok:
            return True
        self.out(f"  Not enough Z room: this pass cuts down to z={bottom:.3f}, but $132="
                 f"{settings['$132']} fences the spindle at z={floor:.3f} (with a {ROOM_MARGIN:g} mm "
                 "margin).  A cut below the fence is an ALARM:2, which stops everything and needs "
                 "re-homing.  The fence was probably set by calibrate.py --z-limit, a little into "
                 "the old spoilboard top; surfacing goes below that top.")
        if not calibrate.askYesNo(self.ask, "Reset $132 to the profile's calibrated value now "
                                            "(what calibrate.py --reset-z-limit does)?"):
            return False
        calibrate.resetZLimit(self.grbl, self.profile, self.out)
        settings = self.grbl.getSettings()
        ok, bottom, floor = checkZRoom(self.choices['touch'], totalAfter, settings['$132'])
        if not ok:
            self.out(f"  Still not enough: the profile's calibrated $132={settings['$132']} fences "
                     f"z={floor:.3f}.  It was probably itself set by a spoilboard Z limit; fix "
                     "the profile's $132 before cutting deeper.")
        return ok

    def askPassDepth(self, passDepth, allowSame):
        # The between-pass question.  Returns a depth, or None to stop.
        while True:
            if allowSame:
                after = round(self.totalDepth + passDepth, 3)
                prompt = (f"Another pass at -{passDepth:g} mm (total -{after:g})? "
                          "[Y / change depth / stop]: ")
            else:
                prompt = "Change the depth, or stop? [c / s]: "
            answer = self.ask(prompt).strip().lower()
            if allowSame and answer in ('', 'y', 'yes'):
                return passDepth
            if answer.startswith('c'):
                return askPositive(self.ask, "Depth for the next pass in mm: ")
            if answer.startswith('s') or answer in ('n', 'no', 'q'):
                return None

    # --- cutting ------------------------------------------------------------

    def runPass(self, plan):
        timeout = plan.seconds * 2 + 120
        self.out(f"  Cutting: {len(plan.gcode)} lines, about {formatDuration(plan.seconds)}.  "
                 "Ctrl-C stops it.")
        streamGcode(self.grbl, plan.gcode, timeout, self.out, self.clock)
        waitForIdle(self.grbl, timeout, self.sleep, self.clock)

    def cutPasses(self):
        passDepth = self.choices['depth']
        allowSame = True
        while True:
            try:
                totalAfter = planPassDepth(self.totalDepth, passDepth, self.options.max_total_depth)
            except DepthRefused as refusal:
                self.out(f"  {refusal}")
                if self.options.max_total_depth - self.totalDepth < 0.001:
                    self.out("  The total-depth cap is reached.")
                    return
                passDepth = self.askPassDepth(passDepth, allowSame=False)
                if passDepth is None:
                    return
                continue
            if not self.ensureZRoom(totalAfter):
                passDepth = self.askPassDepth(passDepth, allowSame=False)
                if passDepth is None:
                    return
                continue
            plan = self.plan(passDepth)
            number = self.passesDone + 1
            if number == 1:
                self.out(SAFETY_REMINDER)
            self.ask(f"Pass {number}: -{passDepth:g} mm (total -{totalAfter:g}), "
                     f"{len(plan.lineYs)} lines, about {formatDuration(plan.seconds)}.  The bit "
                     "lifts, the spindle starts, then it cuts.  Press Enter to start. ")
            self.runPass(plan)
            self.totalDepth = totalAfter
            self.passesDone += 1
            self.lastPlan = plan
            self.choices['depth'] = passDepth
            self.out(f"  Pass {number} done: {self.totalDepth:.3f} mm removed in total.")
            passDepth = self.askPassDepth(passDepth, allowSame=True)
            if passDepth is None:
                return

    def park(self):
        x0, y0, _, _ = self.choices['board']
        settings = self.grbl.getSettings()
        bounds = jogModule.softLimitBounds(settings)
        top = round(bounds['Z'][1], 3)
        clearZ = min(round(self.choices['touch'] + self.choices['clearance'], 3), top)
        self.ask("Press Enter to park: spindle off, Z up to the top, then over the front-left "
                 "corner. ")
        lines = ["M5", f"G53 G0 Z{clearZ:.3f}", f"G53 G0 Z{top:.3f}",
                 f"G53 G0 X{x0:.3f} Y{y0:.3f}"]
        streamGcode(self.grbl, lines, 300, self.out, self.clock)
        waitForIdle(self.grbl, 300, self.sleep, self.clock)

    def finish(self):
        self.park()
        self.out(f"\nTotal depth removed: {self.totalDepth:.3f} mm in {self.passesDone} "
                 f"pass{'es' if self.passesDone != 1 else ''}.")
        if not self.passesDone:
            return
        bitName, entry = self.choices['bit']
        diameter = self.diameter()
        used = {'feed': self.choices['feed'], 'depthPerPass': self.choices['depth'],
                'stepoverPercent': round(self.choices['stepover'] / diameter * 100, 1),
                'plunge': self.choices['plunge']}
        offerSaveBitSettings(self.ask, self.out, self.bits, self.bitsPath, bitName, diameter, used)
        self.out("The spoilboard top has moved, so a spoilboard Z limit set before is now wrong.")
        if calibrate.askYesNo(self.ask, "Set it again now (calibrate.py --z-limit --method "
                                        "spoilboard; needs the probe plate)?"):
            calibrate.runZLimitStep(self.grbl, self.profile, self.profilePath,
                                    method=calibrate.METHOD_SPOILBOARD, ask=self.ask, out=self.out,
                                    runJog=self.runJog, checkEnvironment=self.checkEnvironment)

    # --- the whole run ------------------------------------------------------

    def run(self, home):
        """home(): runs $H, returns an alarm code or None.  Returns the total depth removed."""
        self.out("=== Surface the spoilboard ===")
        if self.checkEnvironment(out=self.out) != jogModule.ENVIRONMENT_OK:
            raise PlanRefused("Keyboard jog is unavailable, and the corners and the touch-off need "
                              "it.  Fix that (see above) and run this again.")
        try:
            self.fill('bit')
            self.out("\nThe motors are released between moves ($1=25), so the knob turns them, but "
                     "Grbl can't see it.  Use the knob NOW, before homing, for a rough move if you "
                     "like.  After homing, move only by jog: a knob turn would make Grbl's position, "
                     "and the soft limits, wrong.")
            self.ask("Keep a hand near the reset button.  Press Enter to home the machine. ")
            alarm = home()
            if alarm is not None:
                raise MachineError(f"Homing failed (ALARM:{alarm}).")
            current132 = self.grbl.getSettings().get('$132')
            calibrated = self.profile.expectedSettings.get('$132')
            if calibrated is not None and current132 != calibrated:
                self.out(f"  Note: $132 is {current132}, not the profile's {calibrated}: a Z limit "
                         "from calibrate.py --z-limit is in force.  The room under it is checked "
                         "before each pass.")
            self.summary()
        except Quit:
            self.out("  Nothing cut.")
            if 'touch' in self.choices:   # the bit is resting on the plywood: lift and park
                self.finish()
            return self.totalDepth
        self.cutPasses()
        self.finish()
        return self.totalDepth


def runWithSafeStop(surfacing, home):
    """Run, and on Ctrl-C or any exception do safeStop() before re-raising."""
    try:
        return surfacing.run(home)
    except BaseException as error:
        surfacing.out(f"\n!! {type(error).__name__}: {error}" if str(error)
                      else f"\n!! {type(error).__name__}")
        safeStop(surfacing.grbl, surfacing.out, surfacing.sleep, surfacing.clock)
        raise


# ---------------------------------------------------------------------------

def parseArguments(argv=None):
    parser = argparse.ArgumentParser(description="Surface the plywood spoilboard with a wide flat bit")
    parser.add_argument("--profile", default=DEFAULT_PROFILE, help="machine profile JSON")
    parser.add_argument("--port", help="default: the profile's port")
    parser.add_argument("--baud", type=int, help="default: the profile's rate")
    parser.add_argument("--bit", help="a saved bit from profiles/bits.json")
    parser.add_argument("--diameter", type=float, metavar="MM", help="bit diameter (a bit not saved)")
    parser.add_argument("--stepover", type=float, metavar="MM",
                        help=f"default {STEPOVER_PERCENT:g}%% of the diameter")
    parser.add_argument("--depth", type=float, metavar="MM", help=f"per pass, default {DEPTH_PER_PASS:g}")
    parser.add_argument("--feed", type=float, metavar="MM/MIN",
                        help=f"default {REFERENCE_FEED:g} x {REFERENCE_DIAMETER:g} / diameter, "
                             f"clamped to {FEED_MIN:g}-{FEED_MAX:g}")
    parser.add_argument("--plunge", type=float, metavar="MM/MIN", help=f"default {PLUNGE_FEED:g}")
    parser.add_argument("--spindle", type=float, metavar="RPM", help=f"default {SPINDLE_SPEED}")
    parser.add_argument("--clearance", type=float, metavar="MM", help=f"default {CLEARANCE:g}")
    parser.add_argument("--overrun", type=float, metavar="MM",
                        help="past each X edge; default the bit radius")
    parser.add_argument("--max-total-depth", type=float, default=MAX_TOTAL_DEPTH, metavar="MM")
    parser.add_argument("--one-way", action="store_true",
                        help="cut every line in +X, lifting to clearance for the return")
    arguments = parser.parse_args(argv)
    if arguments.bit and arguments.diameter is not None:
        parser.error("give --bit or --diameter, not both")
    for name in ('diameter', 'stepover', 'depth', 'feed', 'plunge', 'spindle', 'clearance',
                 'max_total_depth'):
        value = getattr(arguments, name)
        if value is not None and not value > 0:
            parser.error(f"--{name.replace('_', '-')} must be more than 0")
    if arguments.overrun is not None and arguments.overrun < 0:
        parser.error("--overrun must be 0 or more")
    return arguments


def main(argv=None):
    arguments = parseArguments(argv)
    from GrblController import GrblController, MachineProfile
    profile = MachineProfile.load(arguments.profile)
    port = arguments.port or profile.portDeviceName
    baud = arguments.baud or profile.portBaudRate
    print(f"Connecting to {port} @ {baud}...")
    # Without profile=, as calibrate.py --z-limit does: the connect-time check
    # treats $132 as critical and would refuse a per-job Z limit.
    grbl = GrblController(port, baud)
    if profile.homingPosition != grbl.homingPosition:
        grbl.homingPosition = profile.homingPosition
        grbl.send('$23=' + profile.homingPosition)
    surfacing = Surfacing(grbl, profile, arguments.profile, arguments)
    try:
        runWithSafeStop(surfacing, lambda: grbl.tryHomingCycle(profile.homingPosition))
    except KeyboardInterrupt:
        sys.exit(1)
    except (PlanRefused, MachineError) as error:
        sys.exit(f"Stopped: {error}")


if __name__ == "__main__":
    main()
