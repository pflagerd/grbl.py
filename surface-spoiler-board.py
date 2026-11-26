#!/usr/bin/env python3
"""Surface (level) the plywood spoilboard with a wide flat bit (issue #13, step 12).

    python surface-spoiler-board.py [--bit NAME | --diameter MM]
                                    [--corners X0 Y0 X1 Y1 | --board NAME]
                                    [--start {front-left,back-right}] [--one-way]
                                    [--touch-z MM] ...

The flow, with nothing moving before an Enter from you:
  1. Pick a saved surfacing bit from profiles/bits.json, or add one.
  2. You may turn the motor knob NOW, for a rough move, before homing.  After
     homing, every move is by keyboard jog (see "Why the knob" below).  Home.
  3. Jog the bit centre to the board's front-left corner (low X, low Y), Enter;
     then to the back-right corner (high X, high Y), Enter.  Then an offer to
     save the two corners as a named board in profiles/boards.json, under
     this machine (the profile's name).
     --corners X0 Y0 X1 Y1 skips both jogs: it gives those two bit-centre
     positions in machine coordinates (MPos).  --board NAME skips them too,
     using a board saved on this machine (one measured on another machine is
     refused); they are only right if the new board sits where the measured
     one did (against the same stops).  Either way the
     corners are checked for order and against the soft limits before homing.
  4. With --corners or --board, an Enter first moves the bit over the
     board (Z up to the top, then X/Y to the front-left corner, one bit
     radius in); with jogged corners it is already there.  Then jog the bit
     down until it just touches the plywood top, Enter.  That becomes work
     Z0 (G54, G10 L20 P1 Z0); its machine z is recorded.
     --touch-z MM skips this jog: it gives that machine z (MPos, as the
     touch-off and the summary print it), and work Z0 is set there with
     G10 L2 P1 Z<MM>.  To repeat a surfacing with another stepover, give the
     same --board, --touch-z and --depth: the cut bottoms out at the same
     plane as before (a clean-up pass); a deeper cut needs a bigger --depth.
     Or give that cut's bottom as --touch-z with --depth 0: the same plane.
  5. Stepover, depth per pass, feed, plunge, spindle and clearance: a prompt
     for each one not given on the command line, then a summary you can change.
  6. Before each pass: the Z soft-limit room check ($132) and the total-depth
     cap (--max-total-depth, default 3 mm).  Then one raster pass, and
     "Another pass ...? [Y / change depth / stop]".  A depth (here, in the
     summary or as --depth) may be typed with or without a minus sign:
     -0.05 and 0.05 both mean 0.05 mm deeper.  0 is allowed too: no deeper.
     A first pass of 0 cuts at the touch-off plane (with --touch-z MM, at
     that z); a later pass of 0 repeats the plane just cut (a spring pass).  A pass cuts lines along X
     from the front-left corner, front to back (--one-way: every line left to
     right); with --start back-right, from the back-right corner, back to
     front (--one-way: right to left).  The cutting (climb) is the same.
     Each pass is timed from when its G-code starts (in manual mode, after
     your "router ON" Enter) until its motion is done: a live "Cutting pass
     N: ... elapsed" line in a terminal, then "Pass N done in ...".  After
     the last pass, the total cutting time and the time from the first cut
     to the last.  A stopped pass says how long it had cut.
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
    cornerProblems(), checkGivenCorners()   pure: are the two corners usable?
    checkGivenTouchZ()                  pure: is the --touch-z height usable?
    overTheBoard()                      pure: where the bit goes before a touch-off
    planRaster()                        pure: board + bit + bounds -> G-code
    loadBits(), saveBits(), chooseBit() the bit list (profiles/bits.json)
    loadBoards(), saveBoards(), chooseBoard()   saved corners (profiles/boards.json)
    streamGcode(), waitForIdle(), safeStop()   talk to Grbl
    LiveLine.show(), LiveLine.finish()  the updating "Cutting pass N" line
    Surfacing.run()                     the operator flow
    Surfacing.runSafely()               the same, with safeStop() on Ctrl-C or any error
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

# How the spindle is switched, per machine (profiles/<name>.machine.json)
SPINDLE_GRBL = "grbl"        # Grbl's M3/M5/S drive it (the 4030 ProverXL V2)
SPINDLE_MANUAL = "manual"    # a router switched by hand; M3/M5/S do nothing (the 6060)
SPINDLE_MODES = (SPINDLE_GRBL, SPINDLE_MANUAL)
MACHINE_SUFFIX = ".machine.json"

ZIGZAG = "zigzag"
ONE_WAY = "one-way"
PATTERNS = (ZIGZAG, ONE_WAY)

# Where each pass starts (--start).  front-left: the first lines are the
# nearest to the operator, the easiest place to check the depth.  back-right:
# usually nearer where the bit is after the touch-off (the back-right corner
# is the last one jogged), and the pass ends nearer the front-left park spot.
FRONT_LEFT = "front-left"
BACK_RIGHT = "back-right"
STARTS = (FRONT_LEFT, BACK_RIGHT)

SCRIPT_DIRECTORY = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PROFILE = os.path.join(SCRIPT_DIRECTORY, "profiles", "6060 ProverXL V1.json")
BITS_FILE = "bits.json"      # next to the machine profiles; shared across machines
BOARDS_FILE = "boards.json"  # saved board corners, next to bits.json


class PlanRefused(ValueError):
    """The requested cut is not possible or not safe; nothing was sent."""


class DepthRefused(ValueError):
    """The pass depth is not allowed (negative, not a number, or past the total-depth cap)."""


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
    A passDepth of 0 is allowed: the total stays the same, so the pass cuts
    the same plane again (at the touch-off plane if nothing is cut yet).
    Why allow it: with --touch-z set to the bottom of an earlier cut, a 0
    depth re-cuts exactly that plane with another stepover (a clean-up or
    spring pass), taking off only what the bit left behind.
    """
    # "not ... >= 0" also refuses NaN (every comparison with NaN is False);
    # isfinite() refuses inf, which would otherwise only meet the cap.
    if not (math.isfinite(passDepth) and passDepth >= 0):
        raise DepthRefused(f"The depth per pass must be 0 or more, not {passDepth:g}.")
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
               dwell=SPIN_UP_SECONDS, margin=jogModule.LIMIT_MARGIN, start=FRONT_LEFT):
    """One surfacing pass as a list of G-code lines (all G53, machine coordinates).

    board: (x0, y0, x1, y1), the bit-centre positions of the front-left and
           back-right corners.
    Lines run along X.  start=FRONT_LEFT: from low Y to high Y, the first line
    (and every one-way line) cutting in +X.  start=BACK_RIGHT: the same lines
    in the opposite order, from high Y to low Y, the first line (and every
    one-way line) cutting in -X.  Each line runs past both X edges
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
    if start not in STARTS:
        raise PlanRefused(f"Unknown start corner '{start}'.")
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

    # firstX: where each line starts (zigzag: where the first line starts);
    # lastX: where it ends.  From the back-right, the pass is the front-left
    # pass turned half a turn (180 degrees) about the middle of the board:
    # the same lines, cut back to front and right to left.
    #
    # Why that cuts the same way: the router spins clockwise seen from above.
    # From the front-left, a line runs +X and the next line is behind it (+Y),
    # so the uncut wood is on the bit's left (its back side), where a
    # clockwise edge moves +X, the same way as the feed: climb cutting.  From
    # the back-right, a line runs -X and the next line is in front of it (-Y):
    # the uncut wood is again on the bit's left (now its front side), where a
    # clockwise edge moves -X, again the same way as the feed: climb again.
    # Turning a plan round keeps clockwise clockwise; only a mirror image
    # (reversing just X, or just Y) would swap climb and conventional.  So
    # every line, one-way or zigzag, cuts exactly as it does from the
    # front-left; only the order changes.
    if start == BACK_RIGHT:
        lineYs.reverse()
        firstX, lastX = xEnd, xStart
    else:
        firstX, lastX = xStart, xEnd

    # The Z floor (-$132) is checkZRoom()'s job, before each pass, because it
    # comes with an offer to reset $132; refusing here would hide that offer.
    cutZ = round(touchZ - totalDepth, 3)
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
    gcode = ["G90 G21 G94"]
    plan = RasterPlan(gcode, lineYs, round(xStart, 3), round(xEnd, 3), actualStepover,
                      cutZ, clearZ, approachZ, notes=notes)
    position = {'X': firstX, 'Y': lineYs[0], 'Z': None}  # the estimate starts over the start point

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
    if dwell:                                  # 0: a hand-switched router, already running
        gcode.append(f"G4 P{dwell:g}")
        plan.seconds += dwell
    move('G0', X=firstX, Y=lineYs[0])
    plunge()
    for index, y in enumerate(lineYs):
        if pattern == ZIGZAG:
            if index:
                move('G1', feed, Y=y)          # the step to the next line, at the line end
            target = lastX if index % 2 == 0 else firstX
            move('G1', feed, X=target)
        else:
            if index:
                move('G0', Z=clearZ)           # lift for the return
                move('G0', X=firstX, Y=y)
                plunge()
            move('G1', feed, X=lastX)
    move('G0', Z=clearZ)
    gcode.append("M5")                         # off while the operator answers

    for line in gcode:
        if len(line) > MAX_LINE_LENGTH:
            raise PlanRefused(f"G-code line too long for Grbl: {line}")
    return plan


def cornerProblems(x0, y0, x1, y1):
    """What is wrong with the order of two corners, as a list of phrases (empty: fine).

    Shared by the jog path (measureBoard()) and --corners, so both refuse the
    same boards: the back-right corner must be at least MINIMUM_BOARD further
    in X and in Y than the front-left one.  planRaster() checks it again,
    because a plan must never trust its caller.
    """
    problems = []
    if x1 - x0 < MINIMUM_BOARD:
        problems.append(f"X{x1:.3f} is not to the right of X{x0:.3f}")
    if y1 - y0 < MINIMUM_BOARD:
        problems.append(f"Y{y1:.3f} is not behind Y{y0:.3f}")
    return problems


def checkGivenCorners(corners, bounds, margin=jogModule.LIMIT_MARGIN, given="--corners",
                      fix="Please run this again with corrected --corners."):
    """Raise PlanRefused if --corners can't be used; nothing is sent either way.

    Called before homing.  A jogged corner is inside the soft limits by
    construction (the jog stops at them), but typed numbers can be anything,
    so they are checked against the same box planRaster() uses.  There is no
    "try again" here: the numbers came from the command line, so the operator
    fixes them there and runs the script again.  given/fix: how to name the
    corners and what to do, so a saved board (--board) gets its own advice.
    """
    x0, y0, x1, y1 = corners
    if not all(math.isfinite(value) for value in corners):
        raise PlanRefused(f"The corners from {given} must be ordinary numbers.  {fix}")
    problems = cornerProblems(x0, y0, x1, y1)
    if problems:
        raise PlanRefused(f"The corners from {given} are out of order: {'; '.join(problems)}.  The "
                          "second corner (X1 Y1, back-right) must be at least "
                          f"{MINIMUM_BOARD:g} mm to the right of and behind the first (X0 Y0, "
                          f"front-left).  {fix}")
    xLow, xHigh = bounds['X'][0] + margin, bounds['X'][1] - margin
    yLow, yHigh = bounds['Y'][0] + margin, bounds['Y'][1] - margin
    if x0 < xLow - 1e-9 or x1 > xHigh + 1e-9 or y0 < yLow - 1e-9 or y1 > yHigh + 1e-9:
        raise PlanRefused(f"The corners from {given} are outside the soft-limit travel: X must be "
                          f"from {xLow:.3f} to {xHigh:.3f} and Y from {yLow:.3f} to {yHigh:.3f} "
                          "(machine coordinates, MPos, as the position display shows after "
                          f"homing).  {fix}")


def checkGivenTouchZ(touchZ, bounds, margin=jogModule.LIMIT_MARGIN):
    """Raise PlanRefused if --touch-z can't be used; nothing is sent either way.

    Called before homing, like checkGivenCorners(): a jogged touch-off is
    inside the Z travel by construction, a typed number can be anything.
    Two things are checked here, the rest later as usual:
      - it is inside the Z soft-limit travel (from -$132 to -$27), because
        a touch-off jog could not have found the plywood anywhere else;
      - there is room for at least MINIMUM_CLEARANCE above it, because
        planRaster() refuses a plan without that.
    The room for the cut BELOW it is left to the check before each pass
    (checkZRoom(), via ensureZRoom()): that one comes with the offer to reset
    $132, and refusing here would hide that offer.
    """
    zLow, zHigh = bounds['Z'][0] + margin, bounds['Z'][1] - margin
    fix = "Please run this again with a corrected --touch-z, or without it to touch off."
    if not math.isfinite(touchZ):
        raise PlanRefused(f"--touch-z must be an ordinary number.  {fix}")
    if not zLow - 1e-9 <= touchZ <= zHigh + 1e-9:
        raise PlanRefused(f"--touch-z {touchZ:.3f} is outside the Z soft-limit travel: it must be "
                          f"from {zLow:.3f} to {zHigh:.3f} (machine coordinates, MPos, as the "
                          "touch-off prints it: \"MPos z=...\").  If $132 is a spoilboard limit "
                          f"set too high, calibrate.py --reset-z-limit resets it.  {fix}")
    if touchZ + MINIMUM_CLEARANCE > zHigh + 1e-9:
        raise PlanRefused(f"--touch-z {touchZ:.3f} leaves less than {MINIMUM_CLEARANCE:g} mm of "
                          f"clearance under the top of the Z travel (z={zHigh:.3f}).  {fix}")


def overTheBoard(board, diameter):
    """(x, y, clamped): where the bit centre goes before a touch-off with given corners.

    The corners are bit-CENTRE positions, so with the centre exactly on the
    front-left corner, three quarters of the bit hang off the board, and the
    touch-off could find the bed (or nothing) instead of the plywood.  Moved
    one bit radius in, in X and in Y, the whole bit is over the board.  A
    board narrower (or shallower) than the bit has no such spot: the bit goes
    to the middle in that direction instead (clamped=True), as far over the
    board as it can be.  Always between the corners, so inside the soft
    limits whenever they are (checkGivenCorners()).
    """
    x0, y0, x1, y1 = board
    radius = diameter / 2
    x, y = x0 + radius, y0 + radius
    clamped = False
    if x > (x0 + x1) / 2:
        x, clamped = (x0 + x1) / 2, True
    if y > (y0 + y1) / 2:
        y, clamped = (y0 + y1) / 2, True
    return round(x, 3), round(y, 3), clamped


def formatDuration(seconds):
    minutes, seconds = divmod(int(round(seconds)), 60)
    return f"{minutes} min {seconds:02d} s"


class LiveLine:
    """One line that rewrites itself in place, for a terminal only.

    "\r" (carriage return) moves the cursor back to the start of the line,
    so the next write covers the old text; nothing scrolls.  That only works
    on a terminal: in a log file or a pipe every update would pile up, so
    when the stream is not a terminal (isatty() false, or no stream at all,
    as in the tests) show() does nothing.

    The line stays open (no newline) between updates, so anything else
    printed then would land on the end of it.  finish() closes it with a
    newline; Surfacing routes every message through finish() first.
    """

    def __init__(self, stream=None):
        try:
            self.enabled = stream is not None and stream.isatty()
        except (AttributeError, ValueError):   # no isatty(), or a closed stream
            self.enabled = False
        self.stream = stream
        self.width = 0          # length of the text now on the line; 0: no line open
        self.text = None

    def show(self, text):
        # Called after every read from Grbl, often many times a second; the
        # text changes once a second (whole seconds), so write only then.
        if not self.enabled or (self.width and text == self.text):
            return
        # Pad with spaces to cover a longer previous text.
        self.stream.write("\r" + text.ljust(self.width))
        self.stream.flush()
        self.width, self.text = len(text), text

    def finish(self):
        if self.width:
            self.stream.write("\n")
            self.stream.flush()
            self.width = 0


def describePassDepth(passDepth, totalAfter):
    """A pass's depth as the questions show it: "-0.3 mm (total -0.6)".

    Depths are shown below the touch-off, so with a minus sign; a 0 would
    come out as "-0", which reads like a typo, so a 0 pass says what it
    does instead: cut at the touch-off plane (nothing cut yet), or the
    plane just cut again (a spring pass).
    """
    if passDepth > 0:
        return f"-{passDepth:g} mm (total -{totalAfter:g})"
    if totalAfter > 0:
        return f"0 mm deeper (total -{totalAfter:g}, the same plane again)"
    return "0 mm (at the touch-off plane)"


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
    writeJsonAtomically(path, {"schemaVersion": 1, "bits": bits})


def writeJsonAtomically(path, data):
    """Write atomically: a temp file in the same directory, then rename over the old file.

    A failure part-way leaves the old file as it was.  Indented, sorted keys, so
    it stays easy to edit by hand.
    """
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    # mkstemp makes the temp file owner-only (0600); give the result the old
    # file's mode, or the usual umask mode for a new file, so it stays readable.
    if os.path.exists(path):
        mode = os.stat(path).st_mode & 0o777
    else:
        umask = os.umask(0)
        os.umask(umask)
        mode = 0o666 & ~umask
    handle, temporary = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=directory)
    try:
        os.chmod(temporary, mode)
        with os.fdopen(handle, 'w') as jsonFile:
            json.dump(data, jsonFile, indent=2, sort_keys=True)
            jsonFile.write('\n')
            jsonFile.flush()
            os.fsync(jsonFile.fileno())
        os.replace(temporary, path)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


# ---------------------------------------------------------------------------
# Saved boards: profiles/boards.json
# ---------------------------------------------------------------------------
#
# {"schemaVersion": 2,
#  "machines": {"6060 ProverXL V1": {"600x400": {"x0": -600.0, "y0": -580.0,
#                                                "x1": -200.0, "y1": -280.0,
#                                                "note": "..."}}}}
# Machine coordinates (MPos) of the bit centre, as measured after homing, so a
# stack of similar spoilboards can be surfaced one after another without
# measuring each.  The same atomic write as bits.json.
#
# Kept per machine, unlike bits.json: a bit is the same bit in any spindle,
# but MPos numbers only mean something on the machine that measured them (its
# travel, its home switches, its stops on the bed).  A board measured on the
# 6060 could fit inside the 4030's soft limits and still be in the wrong place,
# so --board only looks under the machine in use, and each machine can have
# its own "600x400".  (schemaVersion 1, a flat {"boards": {...}} with no
# machine, was this PR's first draft; such a file is warned about and left
# alone, never rewritten.)

BOARDS_SCHEMA = 2


class BoardsFileError(ValueError):
    """boards.json is not in the per-machine shape (schemaVersion 2)."""


def machineKey(profile, profilePath):
    """The name boards are saved under for this machine.

    The profile's "name" field ("6060 ProverXL V1"), because it is chosen by
    a person to name the machine and stays the same if the profile file is
    copied or renamed.  The file name (without .json) only if the profile has
    no name.
    """
    name = (getattr(profile, 'name', None) or '').strip()
    return name or os.path.splitext(os.path.basename(profilePath))[0]


def boardsPath(profilePath):
    return os.path.join(os.path.dirname(os.path.abspath(profilePath)), BOARDS_FILE)


def loadBoards(path):
    """{machine: {board name: entry}}; an empty dict if the file does not exist yet.

    Raises BoardsFileError for a file in another shape (e.g. the old flat
    one), OSError or ValueError for one that can't be read or parsed.
    """
    if not os.path.exists(path):
        return {}
    with open(path) as jsonFile:
        data = json.load(jsonFile)
    machines = data.get("machines") if isinstance(data, dict) else None
    if (data.get("schemaVersion") if isinstance(data, dict) else None) != BOARDS_SCHEMA \
            or not isinstance(machines, dict) \
            or not all(isinstance(boards, dict) for boards in machines.values()):
        raise BoardsFileError(f"{path} is not in the per-machine format (schemaVersion "
                              f"{BOARDS_SCHEMA}, boards listed under \"machines\")")
    return machines


def saveBoards(path, machines):
    writeJsonAtomically(path, {"schemaVersion": BOARDS_SCHEMA, "machines": machines})


def boardCorners(entry):
    return (float(entry['x0']), float(entry['y0']), float(entry['x1']), float(entry['y1']))


def describeBoard(name, entry):
    x0, y0, x1, y1 = boardCorners(entry)
    text = (f"{name}: front-left X{x0:.3f} Y{y0:.3f}, back-right X{x1:.3f} Y{y1:.3f} "
            f"({x1 - x0:.3f} x {y1 - y0:.3f} mm)")
    if entry.get('note'):
        text += f" - {entry['note']}"
    return text


def chooseBoard(path, machine, name, out=print):
    """The corners of this machine's saved board `name`, or PlanRefused saying why not."""
    try:
        machines = loadBoards(path)
    except BoardsFileError as error:
        # Not ours to fix or rewrite: warn, and carry on as if nothing is saved.
        out(f"  Warning: {error}; it is treated as having no boards for {machine}.")
        machines = {}
    except (OSError, ValueError) as error:
        raise PlanRefused(f"Could not read {path}: {error}.") from None
    boards = machines.get(machine, {})
    if name not in boards:
        elsewhere = sorted(other for other, theirs in machines.items()
                           if other != machine and name in theirs)
        if elsewhere:
            raise PlanRefused(f"The board '{name}' in {path} was measured on "
                              f"{' and '.join(elsewhere)}, not on {machine}.  Its corners are "
                              "machine coordinates, so they are only right on the machine that "
                              f"measured them.  Please measure it on {machine}: run this again "
                              "without --board, jog the corners, and save them.")
        known = ", ".join(sorted(boards)) or "none saved yet"
        raise PlanRefused(f"No board called '{name}' for {machine} in {path} ({known}).  Please "
                          "run this again with one of those names, or without --board to jog "
                          "the corners.")
    try:
        return boardCorners(boards[name])
    except (KeyError, TypeError, ValueError):
        raise PlanRefused(f"The board '{name}' in {path} needs numbers x0, y0, x1 and "
                          "y1.") from None


def offerSaveBoard(ask, out, path, machine, corners):
    """After jogged corners: offer to save them under a name, for this machine only.

    Returns the name, or None.  Saving is a convenience, so it must never stop
    the cut: a file that can't be read or written gives a warning, and the run
    carries on.  Other machines' boards are written back exactly as read.
    """
    if not calibrate.askYesNo(ask, "Save these corners as a board for next time?"):
        return None
    try:
        machines = loadBoards(path)
    except (OSError, ValueError) as error:   # BoardsFileError is a ValueError
        out(f"  Warning: could not read {path} ({error}), so the corners were not saved.  "
            "The cut can go ahead.")
        return None
    boards = machines.setdefault(machine, {})
    while True:
        name = ask("Name for this board (e.g. 600x400): ").strip()
        if not name:
            out("  The name can't be empty.")
            continue
        if name in boards and not calibrate.askYesNo(
                ask, f"There is already a board {describeBoard(name, boards[name])}.  "
                     "Overwrite it?"):
            continue   # keep the old one; ask for another name
        break
    x0, y0, x1, y1 = corners
    boards[name] = {'x0': round(x0, 3), 'y0': round(y0, 3), 'x1': round(x1, 3),
                    'y1': round(y1, 3)}
    try:
        saveBoards(path, machines)
    except OSError as error:
        out(f"  Warning: could not save the board in {path} ({error}).  The cut can go ahead; "
            "you will need to measure the corners again next time.")
        return None
    out(f"  Saved board '{name}' for {machine} in {path}; next time use --board {name}.")
    return name


# ---------------------------------------------------------------------------
# Per-machine facts: profiles/<name>.machine.json
# ---------------------------------------------------------------------------
#
# {"schemaVersion": 1, "spindleControl": "manual"}
# A sidecar because MachineProfile drops keys it doesn't know, and
# GrblController.py (where MachineProfile lives) belongs to another PR.  Not
# the .zlimit.json sidecar: that one is calibrate.py's Z-limit record.

def machinePath(profilePath):
    return os.path.splitext(profilePath)[0] + MACHINE_SUFFIX


def loadMachineRecord(path):
    if not os.path.exists(path):
        return {}
    with open(path) as jsonFile:
        return json.load(jsonFile)


def resolveSpindleControl(commandLine, path, ask, out=print):
    """(mode, source).  --spindle-control wins (for this run only, not saved);
    then the saved value; otherwise ask once and save the answer."""
    if commandLine is not None:
        return commandLine, 'command line'
    record = loadMachineRecord(path)
    if record.get('spindleControl') in SPINDLE_MODES:
        return record['spindleControl'], 'saved'
    answer = calibrate.askChoice(ask, "Is the spindle switched by Grbl (M3/M5) or by hand (e.g. a "
                                      "router)? (grbl / manual): ", SPINDLE_MODES)
    record.setdefault('schemaVersion', 1)
    record['spindleControl'] = answer
    writeJsonAtomically(path, record)
    out(f"  Saved spindleControl={answer} in {path}; you won't be asked again.")
    return answer, 'asked'


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


def askDepth(ask, prompt, default=None):
    # How much deeper to cut, in mm: like askPositive(), but a minus sign is
    # allowed and dropped, so "-0.05" and "0.05" both mean 0.05 mm deeper.
    # Why: the script itself shows depths as negative ("Pass 1: -0.3 mm",
    # "Another pass at -0.3 mm?"), so typing a minus is natural, and a depth
    # only ever goes down: there is nothing else "-0.05" could mean.  0 (and
    # "-0", which float() reads as -0.0; abs() makes it 0.0) means no deeper:
    # cut at the touch-off plane, or again at the plane just cut (see
    # planPassDepth()).  NaN and inf are refused.  Other values (feed,
    # stepover, ...) keep askPositive(): there a minus sign is a mistake, and
    # 0 makes no sense.
    while True:
        text = ask(prompt).strip()
        if not text and default is not None:
            return default
        try:
            value = float(text)
        except ValueError:
            print(f"  '{text}' is not a number.")
            continue
        if not math.isfinite(value):
            print("  It must be an ordinary number, 0 or more (a minus sign is fine: -0.05 "
                  "means 0.05 mm deeper).")
            continue
        return abs(value)


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
    names = sorted(bits, key=lambda name: (float(bits[name]['diameter']), name))  # smallest first
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


def streamGcode(grbl, lines, timeout, out=print, clock=time.monotonic, tick=None):
    """Send each line and wait for its 'ok' before the next (send-response).

    Grbl answers 'ok' as soon as a line is parsed into its planner buffer (16
    motions), not when the motion is done, so the planner stays full while
    only one line is ever in Grbl's 128-byte serial buffer.  Unlike
    GrblController.send(), a silent read is NOT taken as an 'ok': a long line
    can sit behind a full planner for a minute or more.  timeout bounds the
    wait for one 'ok' (a sync command such as M5 waits for the whole queue).

    tick(), if given, is called after every read: each 'ok', or each second
    of silence (the read timeout is 1 s), so a live display can refresh
    about once a second without a thread of its own.
    """
    original = grbl.timeout
    grbl.timeout = 1
    try:
        for index, line in enumerate(lines, 1):
            grbl.write((line + '\n').encode())
            deadline = clock() + timeout
            while True:
                reply = grbl.readline()
                if tick:
                    tick()
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


def waitForIdle(grbl, timeout, sleep=time.sleep, clock=time.monotonic, tick=None):
    deadline = clock() + timeout
    while True:
        state, _ = stateOf(readStatus(grbl))
        if tick:
            tick()    # as in streamGcode(): a live display refreshes between polls
        if state == 'Idle':
            return
        if state == 'Alarm':
            raise MachineError("Grbl is in Alarm (limit switch or soft limit).")
        if clock() > deadline:
            raise MachineError(f"Grbl did not reach Idle within {timeout:.0f} s (state {state}).")
        sleep(0.5)


def safeStop(grbl, out=print, sleep=time.sleep, clock=time.monotonic, manualSpindle=False):
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
                actions.append("soft reset (Ctrl-X)" if manualSpindle
                               else "soft reset (Ctrl-X), which also stops the spindle")
        elif state == 'Idle':
            grbl.write(b'M5\n')
            actions.append("M5 (Grbl's spindle output off)" if manualSpindle else "M5 (spindle off)")
        report = readStatus(grbl)
    except Exception as error:
        out(f"!! The safety stop itself failed: {error}.  Use the E-stop button.")
        return None
    state, _ = stateOf(report)
    _, mpos, _, _ = jogModule.parseStatus(report) if report else (None, None, None, None)
    where = f" at MPos X{mpos['X']:.3f} Y{mpos['Y']:.3f} Z{mpos['Z']:.3f}" if mpos else ""
    out(f"!! Stopped: {', '.join(actions) or 'nothing to send'}.  Machine state: {state}{where}.")
    if state == 'Alarm':
        out("!! Grbl is in Alarm: the position may be lost.  Home ($H) before anything else.")
    elif state is None:
        out("!! No status report from Grbl.  Check the machine; use the E-stop button if it is moving.")
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
               'spindle': "spindle", 'clearance': "clearance", 'pattern': "pattern",
               'start': "start corner"}
STEP_ORDER = ('bit', 'board', 'touch', 'stepover', 'depth', 'feed', 'plunge', 'spindle',
              'clearance', 'pattern', 'start')
BIT_DEPENDENT = ('stepover', 'depth', 'feed', 'plunge')  # their defaults come from the bit

SAFETY_REMINDER = (
    "Before cutting:\n"
    "  - hearing and eye protection on; a wide bit throws chips a long way\n"
    "  - dust: extraction or a mask; plywood dust is fine and full of glue\n"
    "  - the double-sided tape must hold the plywood against SIDEWAYS force,\n"
    "    not just downward; press the corners down and check none lifts\n"
    "  - Ctrl-C stops it: feed hold, then reset, which also stops the spindle")
SAFETY_REMINDER_MANUAL = SAFETY_REMINDER.replace(
    "  - Ctrl-C stops it: feed hold, then reset, which also stops the spindle",
    "  - Ctrl-C stops the MOTION (feed hold, then reset); the router keeps\n"
    "    running until you switch it off by hand")
ROUTER_STILL_RUNNING = (
    "!! ================================================================\n"
    "!! THE ROUTER IS STILL RUNNING.  Switch it OFF by hand now.\n"
    "!! Grbl does not control it, so this script cannot stop it.\n"
    "!! ================================================================")


class Surfacing:
    """The operator flow.  Everything that talks to people or the machine is injected."""

    def __init__(self, grbl, profile, profilePath, options, ask=None, out=print, runJog=None,
                 checkEnvironment=None, sleep=time.sleep, clock=time.monotonic,
                 liveStream=None):
        self.grbl = grbl
        self.profile = profile
        self.profilePath = profilePath
        self.options = options
        self.ask = ask or calibrate.readAnswer
        # Every message goes through say(), which first closes a live
        # "Cutting pass N" line (see LiveLine) so it can't land on the end of it.
        # liveStream: where that line goes; main() gives sys.stdout.
        self.live = LiveLine(liveStream)
        self.rawOut = out
        self.out = self.say
        self.runJog = runJog or jogModule.runKeyboardJog
        self.checkEnvironment = checkEnvironment or jogModule.checkJogEnvironment
        self.sleep = sleep
        self.clock = clock
        self.bitsPath = bitsPath(profilePath)
        self.bits = loadBits(self.bitsPath)
        self.boardsPath = boardsPath(profilePath)
        self.machineName = machineKey(profile, profilePath)   # boards are saved per machine
        self.boardName = None    # the saved board (--board) whose corners are in use
        self.choices = {}
        self.sources = {}
        self.totalDepth = 0.0
        self.passesDone = 0
        self.lastDepth = None
        self.spindleOn = False   # True from a pass's M3 until its M5
        self.machinePath = machinePath(profilePath)
        self.spindleControl, self.spindleSource = SPINDLE_GRBL, None
        self.routerOn = False    # manual mode: the operator said the router is on
        self.homingStarted = False   # run() sets it just before $H
        # Cutting time, per pass: (number, start, end, stopped), clock() seconds.
        self.passTimes = []
        self.cutting = None          # (number, start, estimate) while a pass cuts

    def say(self, text):
        self.live.finish()
        self.rawOut(text)

    # --- parameters ---------------------------------------------------------

    @property
    def manual(self):
        return self.spindleControl == SPINDLE_MANUAL

    def describeSpindleControl(self):
        source = {'command line': "from --spindle-control",
                  'saved': f"saved in {os.path.basename(self.machinePath)}",
                  'asked': "asked once, now saved"}.get(self.spindleSource, "")
        if self.manual:
            return (f"manual ({source}): a router switched by hand.  M3/M5/S are still sent but "
                    "do nothing; no spin-up dwell; you switch it on before the first plunge")
        return (f"grbl ({source}): M3 S + a {SPIN_UP_SECONDS:g} s spin-up before each pass, "
                "M5 after it")

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
                    f"({x1 - x0:.3f} x {y1 - y0:.3f} mm)"
                    + ((f" (from --board {self.boardName})" if self.boardName
                        else " (from --corners)") if source == 'command line' else ""))
        if name == 'touch':
            return (f"plywood top at MPos z={value:.3f} (work Z0)"
                    + (" (from --touch-z)" if source == 'command line' else ""))
        if name == 'pattern':
            forward, back = ("-X", "+X") if self.choices.get('start') == BACK_RIGHT else ("+X", "-X")
            return value + (f" (every line {forward}, lift to return)" if value == ONE_WAY
                            else f" (alternate {forward} / {back})")
        if name == 'start':
            if value == BACK_RIGHT:
                return (f"{value} (back to front, the first line right to left; ends at the "
                        "front, nearer the park spot)")
            return (f"{value} (front to back, the first line left to right; the first lines are "
                    "nearest you)")
        reason = self.defaultReason(name, source)
        if name == 'feed':
            hint = "" if source == 'command line' else "; override with --feed"
            return f"{value:g} mm/min ({reason}{hint})"
        if name == 'plunge':
            return f"{value:g} mm/min ({reason})"
        if name == 'spindle':
            return f"S{int(round(value))} ({reason}); control: {self.describeSpindleControl()}"
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
            if getattr(self.options, 'touch_z', None) is not None:
                self.choices['touch'] = self.givenTouchZ()
                self.sources['touch'] = 'command line'
            else:
                self.choices['touch'] = self.touchOff()
        elif name == 'pattern':
            if 'pattern' in self.sources:  # asked to change it
                self.choices['pattern'] = calibrate.askChoice(
                    self.ask, f"Pattern ({' / '.join(PATTERNS)}): ", PATTERNS)
            else:
                self.choices['pattern'] = ONE_WAY if self.options.one_way else ZIGZAG
            self.sources['pattern'] = 'given'
        elif name == 'start':   # the same as the pattern: asked only when changed
            if 'start' in self.sources:
                self.choices['start'] = calibrate.askChoice(
                    self.ask, f"Start corner ({' / '.join(STARTS)}): ", STARTS)
            else:
                self.choices['start'] = self.options.start or FRONT_LEFT
            self.sources['start'] = 'given'
        else:
            value, source = self.derived(name)
            if source != 'command line' and not (name == 'spindle' and self.manual):
                # (manual mode: the S word is sent but the router ignores it; don't ask)
                label = {'depth': "Depth per pass in mm", 'feed': "Feed in mm/min",
                         'plunge': "Plunge feed in mm/min", 'spindle': "Spindle speed (S word)",
                         'stepover': "Stepover in mm", 'clearance': "Clearance in mm"}[name]
                # The depth takes a minus sign too (see askDepth()).
                askNumber = askDepth if name == 'depth' else askPositive
                typed = askNumber(self.ask, f"{label} [{value:g}, "
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
        if name == 'board' and getattr(self.options, 'corners', None) is not None:
            self.options.corners = None   # changing the board means jogging the corners
            self.boardName = None
        if name == 'touch':
            self.options.touch_z = None   # changing the touch-off means jogging it
        if name == 'bit':
            for dependent in BIT_DEPENDENT:
                if self.sources.get(dependent) != 'command line':
                    self.choices.pop(dependent, None)
                    self.sources.pop(dependent, None)
        elif name not in ('pattern', 'start'):
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
        corners = getattr(self.options, 'corners', None)
        if corners is not None:
            # Typed on the command line (or read from boards.json by run() for
            # --board): no jogging.  run() has already checked them
            # (checkGivenCorners) before homing, so they are used as given.
            x0, y0, x1, y1 = corners
            self.sources['board'] = 'command line'
            given = f"--board {self.boardName}" if self.boardName else "--corners"
            self.out(f"  Corners from {given}: front-left X{x0:.3f} Y{y0:.3f}, back-right "
                     f"X{x1:.3f} Y{y1:.3f}; board {x1 - x0:.3f} x {y1 - y0:.3f} mm.")
            if self.boardName:
                # Saved numbers are positions on the bed, not on the board: they
                # fit a new board only if it was placed exactly like the old one.
                self.out("  Reminder: saved corners are only right if this board sits in the "
                         "same place on the bed (against the same stops) as the one measured, "
                         "and the machine was homed first (it just was).")
            return (x0, y0, x1, y1)
        while True:
            first = self.jogTo("Jog the bit CENTRE over the board's FRONT-LEFT corner (low X, low Y)."
                               "  Arrows move X/Y; raise Z first (Shift+Up) if it is low.")
            second = self.jogTo("Jog the bit CENTRE over the board's BACK-RIGHT corner "
                                "(high X, high Y).")
            x0, y0, x1, y1 = first.x, first.y, second.x, second.y
            problems = cornerProblems(x0, y0, x1, y1)
            if not problems:
                self.out(f"  Board: {x1 - x0:.3f} x {y1 - y0:.3f} mm.")
                offerSaveBoard(self.ask, self.out, self.boardsPath, self.machineName,
                               (x0, y0, x1, y1))
                return (x0, y0, x1, y1)
            self.out(f"  The second corner must be up and to the right of the first: "
                     f"{'; '.join(problems)}.  Measure both corners again.")

    def moveOverBoard(self):
        """Given corners: Enter, then Z up to the top, then X/Y over the board.

        With --corners or --board there was no corner jog, so the bit is still
        where homing left it, maybe not over the plywood at all.  Z goes up
        first, on its own line, so the X/Y move can't drag the bit through a
        clamp, a stop or the board's edge; Grbl runs the lines in order, so
        X/Y starts only once Z is up.  The top is the soft-limit top, as in
        park().  Both targets are inside the soft limits (see overTheBoard()),
        and a Ctrl-C (or an error) here reaches runSafely(), which stops the
        motion with safeStop() as for any other move.
        """
        x, y, clamped = overTheBoard(self.choices['board'], self.diameter())
        top = round(jogModule.softLimitBounds(self.grbl.getSettings())['Z'][1], 3)
        where = ("over the middle of the board (it is smaller than the bit, so there is no spot "
                 "one bit radius in from the corner)" if clamped
                 else "over the board's front-left corner, one bit radius in (so the whole bit is "
                      "over the plywood)")
        self.ask(f"Keep a hand near the E-stop button.  Press Enter to move the bit {where}, "
                 f"X{x:.3f} Y{y:.3f}: Z up to the top first, then X/Y. ")
        streamGcode(self.grbl, [f"G53 G0 Z{top:.3f}", f"G53 G0 X{x:.3f} Y{y:.3f}"], 300,
                    self.out, self.clock)
        waitForIdle(self.grbl, 300, self.sleep, self.clock)

    def givenTouchZ(self):
        """--touch-z: work Z0 at that machine z, without a touch-off jog.

        G10 L20 P1 Z0 (the touch-off) means "make the position where the bit
        is NOW work Z0"; here the bit is not at the plywood, so that would be
        wrong.  G10 L2 P1 Z<z> sets the G54 Z offset itself: work Z = machine
        Z - offset, so work Z0 is at machine z, wherever the bit is.  The cut
        doesn't depend on it (planRaster() works in G53 machine coordinates,
        from this same z); it keeps the position display the same as after a
        touch-off.  With given corners the bit still goes over the board
        first, at the top of Z: the first pass comes down to clearance
        wherever the bit is, and after homing that may be over a clamp or a
        stop; over the board, the clearance is above the plywood.
        """
        touchZ = round(float(self.options.touch_z), 3)
        if getattr(self.options, 'corners', None) is not None:
            self.moveOverBoard()
        self.grbl.send(f"G10 L2 P1 Z{touchZ:.3f}")
        self.out(f"  Work Z0 (G54) set at MPos z={touchZ:.3f}, from --touch-z (no touch-off).")
        self.out("  Reminder: a --touch-z is only right if the machine was homed first (it just "
                 "was) and the board has not been moved or changed since that z was measured.  "
                 "Surfacing since is fine: the same --touch-z and --depth cut down to the same "
                 "plane as before (a clean-up pass); a bigger --depth goes deeper.")
        return touchZ

    def touchOff(self):
        # With --corners or --board there was no corner jog: move the bit
        # over the board first (moveOverBoard()).  With jogged corners, the
        # bit is already over the board (at the back-right corner).
        if getattr(self.options, 'corners', None) is not None:
            self.moveOverBoard()
            over = "The bit is over the board.  "
        else:
            over = ""
        position = self.jogTo(over + "Jog the bit DOWN until it just touches the plywood top: "
                              "Shift+Down, hold Ctrl to creep, tap Shift+Down to step 0.1 mm, "
                              "Alt+Shift+Down 0.01 mm (a sheet of paper under the bit helps).  If it "
                              "stops with 'At the travel limit', $132 is in the way: Esc, then "
                              "calibrate.py --reset-z-limit.")
        self.grbl.send("G10 L20 P1 Z0")
        self.out(f"  Work Z0 (G54) set at the plywood top, MPos z={position.z:.3f}.")
        return position.z

    # --- planning -----------------------------------------------------------

    def plan(self, passDepth):
        """One pass of passDepth below the current total.

        Every pass is self-contained: it starts the spindle (M3 + spin-up dwell)
        and ends lifted to clearance with M5, so the spindle is off while the
        operator answers "Another pass?".
        """
        settings = self.grbl.getSettings()
        diameter = self.diameter()
        overrun = self.options.overrun if self.options.overrun is not None else diameter / 2
        totalAfter = round(self.totalDepth + passDepth, 3)
        return planRaster(self.choices['board'], diameter, self.choices['stepover'], overrun,
                          self.choices['touch'], totalAfter, passDepth, self.choices['clearance'],
                          self.choices['pattern'], jogModule.softLimitBounds(settings),
                          self.choices['feed'], self.choices['plunge'], self.choices['spindle'],
                          rapidRates(settings), dwell=0 if self.manual else SPIN_UP_SECONDS,
                          start=self.choices['start'])

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
                ok, bottom, floor = checkZRoom(self.choices['touch'],
                                               self.totalDepth + self.choices['depth'],
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
                 f"Y{plan.lineYs[0]:.3f} to Y{plan.lineYs[-1]:.3f} "
                 f"({'back to front' if plan.lineYs[0] > plan.lineYs[-1] else 'front to back'}), "
                 f"stepover {plan.stepover:.3f} mm")
        self.out(f"  Path: {plan.cutLength:.0f} mm cutting + {plan.rapidLength:.0f} mm rapid; "
                 f"about {formatDuration(plan.seconds)} per pass (acceleration not counted)")
        self.out(f"  Cut at z={plan.cutZ:.3f}, clearance z={plan.clearZ:.3f}")
        for note in plan.notes:
            self.out(f"  Note: {note}")
        if (self.choices['depth'] == 0 and self.totalDepth == 0
                and self.sources.get('touch') != 'command line'):
            # A fresh touch-off is where the bit met the highest spot under
            # it, so a cut at that plane takes off only what stands above it.
            # With --touch-z the plane is whatever z was given (for example
            # an earlier cut's bottom), so no note.
            self.out("  Note: a 0 mm pass cuts at the touch-off height, so it only skims the "
                     "high spots.")

    # --- checks before a pass -----------------------------------------------

    def ensureZRoom(self, totalAfter):
        """The room check for one pass's bottom; offers the reset to workAreaZ."""
        settings = self.grbl.getSettings()
        ok, bottom, floor = checkZRoom(self.choices['touch'], totalAfter, settings['$132'])
        if ok:
            return True
        full = float(self.profile.workAreaZ)
        self.out(f"  Not enough Z room: this pass cuts down to z={bottom:.3f}, but $132="
                 f"{settings['$132']} fences the spindle at z={floor:.3f} (with a {ROOM_MARGIN:g} mm "
                 "margin).  A cut below the fence is an ALARM:2, which stops everything and needs "
                 "re-homing.  The fence was probably set by calibrate.py --z-limit, a little into "
                 "the old spoilboard top; surfacing goes below that top.")
        if not calibrate.askYesNo(self.ask, f"Reset $132 to the full Z travel, {full:.3f} (the "
                                            "profile's workAreaZ)?  Surfacing moves the spoilboard "
                                            "top down, so the old spoilboard limit is out of date "
                                            "anyway.  Only Grbl's $132 changes now, not the "
                                            "profile's calibrated $132; at the end you can set a "
                                            "new spoilboard limit from the surfaced top."):
            return False
        self.stopSpindle()   # a $ setting is an EEPROM write: only with the spindle stopped
        calibrate.writeZLimit(self.grbl, full, self.out)   # writes, then reads back
        settings = self.grbl.getSettings()
        ok, bottom, floor = checkZRoom(self.choices['touch'], totalAfter, settings['$132'])
        if not ok:
            self.out(f"  Still not enough: even the full travel ($132={settings['$132']}) fences "
                     f"z={floor:.3f}, above z={bottom:.3f}.  Check the touch-off.")
        return ok

    def askPassDepth(self, passDepth, allowSame):
        """The after-pass question.  Returns a depth, or None to stop.

        It waits for as long as it takes: readAnswer() blocks on the terminal
        with no timeout, nothing polls Grbl meanwhile, and an empty line is the
        operator's Enter (Y), never silence.  End of input (EOF) means stop.
        """
        while True:
            if allowSame:
                after = round(self.totalDepth + passDepth, 3)
                # Manual mode: the Y also confirms the router is on, because Y
                # plunges at once and Grbl can't start a hand-switched router.
                prompt = (("Router ON? " if self.manual else "")
                          + f"Another pass at {describePassDepth(passDepth, after)}? "
                          "[Y / change depth / stop]: ")
            else:
                prompt = "Change the depth, or stop? [c / s]: "
            try:
                answer = self.ask(prompt).strip().lower()
                if answer.startswith('c'):
                    return askDepth(self.ask, "How much deeper for the next pass, in mm (e.g. 0.05, "
                                              "or 0 for the same plane again): ")
            except EOFError:
                self.out("  (input closed: stopping)")
                return None
            if allowSame and answer in ('', 'y', 'yes'):
                return passDepth
            if answer.startswith('s') or answer in ('n', 'no', 'q'):
                return None

    # --- cutting ------------------------------------------------------------

    def stopSpindle(self):
        # M5, and wait for it.  The next pass then starts the spindle again,
        # with the spin-up dwell.
        if not self.spindleOn:
            return
        self.out("  Stopping the spindle first.")
        streamGcode(self.grbl, ["M5"], 60, self.out, self.clock)
        waitForIdle(self.grbl, 60, self.sleep, self.clock)
        self.spindleOn = False

    # --- cutting time -------------------------------------------------------

    def startCutting(self, number, estimate):
        self.cutting = (number, self.clock(), estimate)

    def showProgress(self):
        """Called by streamGcode()/waitForIdle() after each read or status poll."""
        if not self.cutting or not self.live.enabled:
            return
        number, start, estimate = self.cutting
        self.live.show(f"  Cutting pass {number}: {formatDuration(self.clock() - start)} "
                       f"elapsed of about {formatDuration(estimate)}")

    def stopCutting(self, stopped=False):
        """Ends the timing of the pass that is cutting; returns its seconds."""
        number, start, _ = self.cutting
        end = self.clock()
        self.passTimes.append((number, start, end, stopped))
        self.cutting = None
        self.live.finish()
        return end - start

    def describeCuttingTime(self):
        """The total line: the passes' own times, and first cut to last."""
        cut = sum(end - start for _, start, end, _ in self.passTimes)
        count = len(self.passTimes)
        if count == 1:
            return f"  Cutting time: {formatDuration(cut)} in 1 pass."
        wall = self.passTimes[-1][2] - self.passTimes[0][1]
        return (f"  Cutting time: {formatDuration(cut)} over {count} passes "
                f"({formatDuration(wall)} from the first cut to the last, including the pauses "
                "between passes).")

    def runPass(self, plan, number=None):
        """Cuts one pass; returns the seconds it took, timed from its first line.

        In manual mode the wait for "Switch the router ON" is not counted:
        the clock starts when the cutting G-code begins streaming.
        """
        number = number or self.passesDone + 1
        timeout = plan.seconds * 2 + 120
        self.out(f"  Cutting: {len(plan.gcode)} lines, about {formatDuration(plan.seconds)}.  "
                 "Ctrl-C stops it.")
        self.spindleOn = True   # from the M3; the pass's own M5 turns it off
        lines = plan.gcode
        if self.manual and not self.routerOn:
            # Lift off the plywood first (and send the harmless M3), then wait for
            # the operator: cutting must not start until the router is running.
            split = lines.index(next(line for line in lines if line.startswith("M3"))) + 1
            streamGcode(self.grbl, lines[:split], timeout, self.out, self.clock)
            waitForIdle(self.grbl, timeout, self.sleep, self.clock)
            self.ask("Switch the router ON now, then press Enter. ")
            self.routerOn = True
            lines = lines[split:]
        self.startCutting(number, plan.seconds)
        try:
            streamGcode(self.grbl, lines, timeout, self.out, self.clock, tick=self.showProgress)
            # Idle: the M5 has run, so the pass's motion is finished.
            waitForIdle(self.grbl, timeout, self.sleep, self.clock, tick=self.showProgress)
        except BaseException:
            # Ctrl-C, an alarm, an error: keep the time cut so far for the
            # stop message in runSafely(), and for the total.
            self.stopCutting(stopped=True)
            raise
        self.spindleOn = False
        return self.stopCutting()

    def askExtraPass(self, depth, mustChange=False):
        """After a pass.  Returns the next pass's depth, or None to stop.

        A depth past the cap is refused here.  mustChange: `depth` can't be cut
        (no room), so only "change the depth, or stop" is offered.  In manual
        mode a changed depth comes back to the "Router ON? Another pass ...?"
        question with the new numbers, so nothing cuts without that Y.
        """
        cap = self.options.max_total_depth
        while True:
            try:
                planPassDepth(self.totalDepth, depth, cap)
                fits = True
            except DepthRefused as refusal:
                fits = False
                self.out(f"  {refusal}")
                if cap - self.totalDepth < 0.001:
                    self.out("  The total-depth cap is reached.")
                    return None
            allowSame = fits and not mustChange
            if allowSame:
                self.out("  (answer Y only with the router running)" if self.manual
                         else "  (spindle off; answering Y restarts it)")
            answer = self.askPassDepth(depth, allowSame=allowSame)
            if answer is None:
                return None
            if allowSame and answer == depth:
                return depth
            try:
                planPassDepth(self.totalDepth, answer, cap)
            except DepthRefused as refusal:
                self.out(f"  {refusal}")
                depth = answer
                continue
            if self.manual:
                # The "Router ON? Another pass at -X mm (total -Y)?" question
                # that comes next already shows the new numbers, so no
                # "Next pass:" line here: it would say the same thing twice.
                depth, mustChange = answer, False
                continue
            # Say what was understood (a typed "-0.05" became 0.05 deeper)
            # before it cuts.
            after = round(self.totalDepth + answer, 3)
            if answer > 0:
                self.out(f"  Next pass: {answer:g} mm deeper (total -{after:g} mm).")
            else:
                self.out(f"  Next pass: {describePassDepth(answer, after)}.")
            return answer

    def cutPasses(self):
        """One pass at a time, a question after each.

        Before every pass: the cap and the room check for that pass's bottom.
        The first pass needs an Enter; after that, the answer to "Another
        pass?" is the go-ahead: the next pass restarts the spindle with the
        spin-up dwell and cuts.  Raises Quit if the operator quits at the summary.
        """
        depth = self.choices['depth']
        while True:
            totalAfter = round(self.totalDepth + depth, 3)
            if not self.ensureZRoom(totalAfter):
                if not self.passesDone:
                    self.out("  Back to the summary: change the depth, or q to quit.")
                    self.summary()
                    depth = self.choices['depth']
                else:   # declined the reset: a shallower pass, or stop
                    depth = self.askExtraPass(depth, mustChange=True)
                    if depth is None:
                        return
                continue
            plan = self.plan(depth)
            number = self.passesDone + 1
            if number == 1:
                self.out(SAFETY_REMINDER_MANUAL if self.manual else SAFETY_REMINDER)
                then = ("you switch the router on, then it cuts" if self.manual
                        else "the spindle starts, then it cuts")
                self.ask(f"Pass 1: {describePassDepth(depth, totalAfter)}, {len(plan.lineYs)} lines, "
                         f"about {formatDuration(plan.seconds)}.  The bit lifts, {then}.  "
                         "Press Enter to start. ")
            else:
                self.out(f"  Pass {number}: {describePassDepth(depth, totalAfter)}, about "
                         f"{formatDuration(plan.seconds)}.")
            seconds = self.runPass(plan, number)
            self.totalDepth = totalAfter
            self.passesDone += 1
            if depth > 0:
                # The depth offered for saving with the bit: a 0 spring pass
                # is not a depth per pass to start from next time.
                self.lastDepth = depth
            self.out(f"  Pass {number} done in {formatDuration(seconds)} (estimated about "
                     f"{formatDuration(plan.seconds)}): {self.totalDepth:.3f} mm removed in total.  "
                     "The bit is "
                     "up at clearance" + (" (the router is still running)." if self.manual
                                          else " and the spindle is stopped."))
            depth = self.askExtraPass(depth)
            if depth is None:
                return

    def park(self):
        x0, y0, _, _ = self.choices['board']
        settings = self.grbl.getSettings()
        bounds = jogModule.softLimitBounds(settings)
        top = round(bounds['Z'][1], 3)
        clearZ = min(round(self.choices['touch'] + self.choices['clearance'], 3), top)
        if self.manual and self.routerOn:
            self.ask("Switch the router OFF now.  Then press Enter to park: Z up to the top, then "
                     "over the front-left corner. ")
            self.routerOn = False
        else:
            self.ask("Press Enter to park: spindle off, Z up to the top, then over the front-left "
                     "corner. ")
        lines = ["M5", f"G53 G0 Z{clearZ:.3f}", f"G53 G0 Z{top:.3f}",
                 f"G53 G0 X{x0:.3f} Y{y0:.3f}"]
        streamGcode(self.grbl, lines, 300, self.out, self.clock)
        self.spindleOn = False
        waitForIdle(self.grbl, 300, self.sleep, self.clock)

    def finish(self):
        if self.passTimes:
            self.out(self.describeCuttingTime())
        self.park()
        self.out(f"\nTotal depth removed: {self.totalDepth:.3f} mm in {self.passesDone} "
                 f"pass{'es' if self.passesDone != 1 else ''}.")
        if not self.passesDone:
            return
        bitName, entry = self.choices['bit']
        diameter = self.diameter()
        used = {'feed': self.choices['feed'], 'depthPerPass': self.lastDepth,
                'stepoverPercent': round(self.choices['stepover'] / diameter * 100, 1),
                'plunge': self.choices['plunge']}
        if self.lastDepth is None:   # every pass was 0: keep the bit's saved depth
            del used['depthPerPass']
        offerSaveBitSettings(self.ask, self.out, self.bits, self.bitsPath, bitName, diameter, used)
        self.offerSurfacedZLimit(bitName, diameter)

    def surfacedTop(self):
        # The spindle z at which this bit's tip meets the new top: the touch-off
        # met the old top, and the passes took totalDepth off it.
        return round(self.choices['touch'] - self.totalDepth, 3)

    def offerSurfacedZLimit(self, bitName, diameter):
        self.stopSpindle()   # park() has already sent M5; this makes sure
        top = self.surfacedTop()
        if self.totalDepth > 0:
            self.out(f"The spoilboard top has moved down {self.totalDepth:.3f} mm, to z={top:.3f} for "
                     "this bit, so a spoilboard Z limit set before is out of date.")
        else:
            # Only 0 mm passes: the top is the touch-off plane (with --touch-z,
            # the plane given, which may be an earlier cut's bottom).
            self.out(f"The passes cut at the touch-off plane, z={top:.3f} for this bit, no deeper.")
        answer = self.ask("Set the spoilboard Z limit from the surfaced top? [Y/n] ").strip().lower()
        if answer in ('n', 'no'):
            return None
        return calibrate.runZLimitStep(
            self.grbl, self.profile, self.profilePath, method=calibrate.METHOD_SPOILBOARD,
            ask=self.ask, out=self.out, runJog=self.runJog,
            checkEnvironment=self.checkEnvironment, surface=top, measuredBy="surfaced",
            recordExtra={"bit": bitName or f"{diameter:g} mm (not saved)",
                         "touchOffZ": round(self.choices['touch'], 3),
                         "surfacedDepth": self.totalDepth})

    # --- the whole run ------------------------------------------------------

    def run(self, home):
        """home(): runs $H, returns an alarm code or None.  Returns the total depth removed."""
        self.out("=== Surface the spoilboard ===")
        corners = getattr(self.options, 'corners', None)
        boardName = getattr(self.options, 'board', None)
        givenCorners = corners is not None or boardName is not None
        touchZ = getattr(self.options, 'touch_z', None)
        if self.checkEnvironment(out=self.out) != jogModule.ENVIRONMENT_OK:
            # --corners skips only the corner jogs; the touch-off is a jog
            # unless --touch-z gives its result.  With both, nothing is jogged
            # unless a step is changed at the summary, which jogs it again.
            if givenCorners and touchZ is not None:
                needs = "changing the board or the touch-off at the summary needs it"
            elif givenCorners:
                needs = "the touch-off needs it"
            elif touchZ is not None:
                needs = "the corners need it"
            else:
                needs = "the corners and the touch-off need it"
            raise PlanRefused(f"Keyboard jog is unavailable, and {needs}.  Fix that (see above) "
                              "and run this again.")
        if boardName is not None:
            # A saved board is just --corners read from a file: from here on
            # the two take the same path.
            self.options.corners = corners = chooseBoard(self.boardsPath, self.machineName,
                                                               boardName, self.out)
            self.boardName = boardName
            self.options.board = None   # used once; changing step 2 jogs instead
        if corners is not None:
            # Before homing, so bad numbers stop the run before anything moves.
            # The soft limits come from $130-$132, which Grbl reports unhomed.
            if self.boardName:
                checkGivenCorners(corners, jogModule.softLimitBounds(self.grbl.getSettings()),
                                  given=f"the saved board '{self.boardName}'",
                                  fix=f"Measure it again (run without --board) or correct it in "
                                      f"{self.boardsPath}.")
            else:
                checkGivenCorners(corners, jogModule.softLimitBounds(self.grbl.getSettings()))
        if touchZ is not None:
            # The same: before homing, so a bad number stops the run first.
            checkGivenTouchZ(touchZ, jogModule.softLimitBounds(self.grbl.getSettings()))
        try:
            self.fill('bit')
            self.spindleControl, self.spindleSource = resolveSpindleControl(
                self.options.spindle_control, self.machinePath, self.ask, self.out)
            self.out("\nThe motors are released between moves ($1=25), so the knob turns them, but "
                     "Grbl can't see it.  Use the knob NOW, before homing, for a rough move if you "
                     "like.  After homing, move only by jog: a knob turn would make Grbl's position, "
                     "and the soft limits, wrong.")
            self.ask("Keep a hand near the E-stop button.  Press Enter to home the machine. ")
            self.homingStarted = True   # from here on the machine may move
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
            self.cutPasses()
        except Quit:
            self.out("  Nothing cut.")
            if 'touch' in self.choices:   # the bit is resting on the plywood: lift and park
                self.finish()
            return self.totalDepth
        self.finish()
        return self.totalDepth

    def runSafely(self, home):
        """run(home); on Ctrl-C or any exception once homing has started, safeStop() and re-raise."""
        try:
            return self.run(home)
        except BaseException as error:
            if not self.homingStarted:
                raise   # stopped before anything could move; nothing to stop
            self.out(f"\n!! {type(error).__name__}: {error}" if str(error)
                     else f"\n!! {type(error).__name__}")
            if self.passTimes and self.passTimes[-1][3]:
                number, start, end, _ = self.passTimes[-1]
                self.out(f"!! Pass {number} was stopped after {formatDuration(end - start)} "
                         "of cutting.")
                if len(self.passTimes) > 1:
                    self.out("!!" + self.describeCuttingTime()[1:])
            safeStop(self.grbl, self.out, self.sleep, self.clock, manualSpindle=self.manual)
            if self.manual and self.routerOn:
                self.out(ROUTER_STILL_RUNNING)
            elif self.manual:
                self.out("!! If you switched the router on, switch it OFF by hand: Grbl can't.")
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
    parser.add_argument("--depth", type=float, metavar="MM",
                        help=f"per pass, default {DEPTH_PER_PASS:g}; a minus sign is fine (-0.2 means "
                             "0.2 deeper); 0 cuts at the touch-off plane (with --touch-z, at that z)")
    parser.add_argument("--feed", type=float, metavar="MM/MIN",
                        help=f"default {REFERENCE_FEED:g} x {REFERENCE_DIAMETER:g} / diameter, "
                             f"clamped to {FEED_MIN:g}-{FEED_MAX:g}")
    parser.add_argument("--plunge", type=float, metavar="MM/MIN", help=f"default {PLUNGE_FEED:g}")
    parser.add_argument("--spindle", type=float, metavar="RPM", help=f"default {SPINDLE_SPEED}")
    parser.add_argument("--clearance", type=float, metavar="MM", help=f"default {CLEARANCE:g}")
    parser.add_argument("--overrun", type=float, metavar="MM",
                        help="past each X edge; default the bit radius")
    parser.add_argument("--max-total-depth", type=float, default=MAX_TOTAL_DEPTH, metavar="MM")
    parser.add_argument("--spindle-control", choices=SPINDLE_MODES,
                        help="grbl: M3/M5 switch the spindle; manual: a router switched by hand "
                             "(default: saved per machine, asked once)")
    parser.add_argument("--corners", nargs=4, type=float, metavar=("X0", "Y0", "X1", "Y1"),
                        help="skip the two corner jogs: the bit-centre positions of the board's "
                             "front-left (lower-left) corner X0 Y0 and back-right (upper-right) "
                             "corner X1 Y1, in machine coordinates (MPos, as the position display "
                             "shows after homing).  The touch-off is still a jog (see "
                             "--touch-z); an Enter first moves the bit over the board")
    parser.add_argument("--board", metavar="NAME",
                        help="skip the two corner jogs: use the corners of a board saved in "
                             "profiles/boards.json for this machine (the profile's name); "
                             "saving is offered after the corners are jogged")
    parser.add_argument("--one-way", action="store_true",
                        # The direction follows --start (see planRaster()), so the
                        # help names both rather than promising +X.
                        help="every line cuts in the same direction (+X from the front-left "
                             "start, -X from the back-right), lifting to clearance for the "
                             "return; default: zigzag")
    parser.add_argument("--start", choices=STARTS, default=FRONT_LEFT,
                        help="where each pass starts (default front-left: the first lines are "
                             "nearest you, to check the depth).  back-right: usually starts nearer "
                             "the touch-off and ends nearer the front-left park spot (shorter "
                             "rapids); --one-way lines then cut in -X.  The cutting (climb) is the "
                             "same either way")
    parser.add_argument("--touch-z", type=float, metavar="MM",
                        help="skip the touch-off jog: the machine Z (MPos) of the plywood top, as "
                             "a touch-off prints it (\"MPos z=...\") and the summary shows it.  "
                             "Only right if the board has not moved or been changed since.  To "
                             "repeat a surfacing with another --stepover, give the same --board, "
                             "--touch-z and --depth: the cut bottoms out at the same plane as "
                             "before (a clean-up pass); a deeper cut needs a bigger --depth.  Or "
                             "give that cut's bottom as --touch-z with --depth 0")
    arguments = parser.parse_args(argv)
    if arguments.bit and arguments.diameter is not None:
        parser.error("give --bit or --diameter, not both")
    if arguments.depth is not None:
        # As at the prompts (askDepth()): --depth -0.2 means 0.2 mm deeper,
        # and 0 (or -0) means no deeper: cut at the touch-off plane.
        if not math.isfinite(arguments.depth):
            parser.error("--depth must be an ordinary number, 0 or more (a minus sign is fine: "
                         "-0.2 means 0.2 mm deeper)")
        arguments.depth = abs(arguments.depth)
    # The depth is the only one of these that may be 0 (see just above).
    for name in ('diameter', 'stepover', 'feed', 'plunge', 'spindle', 'clearance',
                 'max_total_depth'):
        value = getattr(arguments, name)
        if value is not None and not value > 0:
            parser.error(f"--{name.replace('_', '-')} must be more than 0")
    if arguments.overrun is not None and arguments.overrun < 0:
        parser.error("--overrun must be 0 or more")
    if arguments.touch_z is not None and not math.isfinite(arguments.touch_z):
        parser.error("--touch-z must be an ordinary number")
    if arguments.board is not None and arguments.corners is not None:
        parser.error("give --board or --corners, not both")
    if arguments.corners is not None:
        # The order check needs no machine, so it is done here, before
        # connecting.  The soft-limit check needs Grbl's settings: run() does it.
        if not all(math.isfinite(value) for value in arguments.corners):
            parser.error("--corners must be ordinary numbers")
        problems = cornerProblems(*arguments.corners)
        if problems:
            parser.error(f"--corners X0 Y0 X1 Y1: {'; '.join(problems)}.  The back-right "
                         f"corner (X1 Y1) must be at least {MINIMUM_BOARD:g} mm to the right of "
                         "and behind the front-left corner (X0 Y0)")
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
    surfacing = Surfacing(grbl, profile, arguments.profile, arguments, liveStream=sys.stdout)
    try:
        surfacing.runSafely(lambda: grbl.tryHomingCycle(profile.homingPosition))
    except KeyboardInterrupt:
        sys.exit(1)
    except (PlanRefused, MachineError) as error:
        sys.exit(f"Stopped: {error}")


if __name__ == "__main__":
    main()
