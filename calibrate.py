#!/usr/bin/env python3
"""One-time per-machine capability test for grbl.py.

Attended calibration: verifies limit-switch wiring (press test), verifies axis
directions (test jogs + operator confirmation), measures usable travel per axis
(creep probe toward the far end, which the operator can stop with Enter), and
probes which homing corners work.
Writes the result as a MachineProfile JSON into profiles/<name>.json.

The machines this targets have TWO limit switches per axis (one at each end),
which is what makes the corner sweep and the X/Y creep probes safe.  The Z
probe is different: on most machines the baseboard, spoiler board or a clamped
workpiece sits above the bottom Z switch, so the tool would crash long before
that switch triggers.  The Z probe is therefore operator-terminated: you watch
the tool tip and press Enter when it nears the obstruction.  What gets measured
is the USABLE depth, which is what workAreaZ / $132 mean.

Run with the machine powered, the work area clear, and a hand near the E-stop
button.  Nothing moves without an Enter from you first.

Z limit (issue #13, step 9): sets Grbl's Z soft limit ($132) so the bit cannot
go deeper than a chosen depth into the spoilboard.  It runs as an optional last
step of the full calibration, or on its own:

    calibrate.py NAME --z-limit [--method spoilboard|workpiece|jog]
                                [--into-depth MM] [--thickness MM]
                                [--plate-thickness MM]
    calibrate.py NAME --reset-z-limit

$132 is stored in Grbl's EEPROM, so the limit stays in force after the job,
for any program, until --reset-z-limit puts back the profile's calibrated
$132.  See the "Z limit" section below for the arithmetic.
"""

import argparse
import datetime
import json
import math
import os
import select
import sys
import time

# These two need the venv (GrblController needs pyserial).  If the venv isn't
# active, say so politely instead of dying with a traceback.
try:
    import jog as jogModule  # calibrate.py already has a function called jog()
    from GrblController import GrblController, MachineProfile
except ModuleNotFoundError as missing:
    sys.exit(f"Please activate the venv first (this Python has no '{missing.name}' module).\n"
             f"From the repository root, run:\n"
             f"    source .venv/bin/activate")

JOG_FEED = 600          # mm/min for direction-test jogs
PROBE_FEED = 300        # mm/min for the creep probe (slow: we intend to touch a switch)
PROBE_STEP = 1.0        # mm per creep step
PROBE_MAX_TRAVEL = 1500  # mm; give up if no switch found by then (miswired/dead)
PROBE_BACK_OFF = 5      # mm to retreat after a probe stops, so no switch stays triggered

# Why a creep probe stopped.  Only the first two are measurements.
STOPPED_BY_OPERATOR = "operator"
STOPPED_BY_SWITCH = "switch"
STOPPED_BY_MAX_TRAVEL = "maxTravel"
DIRECTION_BITS = {'X': 1, 'Y': 2, 'Z': 4}

CORNERS = {
    GrblController.HomingPositions.topRightZUp: "top-right (Z up)",
    GrblController.HomingPositions.topLeftZUp: "top-left (Z up)",
    GrblController.HomingPositions.bottomRightZUp: "bottom-right (Z up)",
    GrblController.HomingPositions.bottomLeftZUp: "bottom-left (Z up)",
}


def readAnswer(prompt, inputStream=None):
    # input() replacement that can never be answered by type-ahead.  Anything
    # typed before the prompt appears (an extra Enter, a key pressed while the
    # machine was moving) is thrown away first, then one fresh line is read.
    # It reads the file descriptor directly (os.read), bypassing sys.stdin's
    # buffer, so no earlier line can be sitting hidden in Python's buffer.
    if inputStream is None:
        inputStream = sys.stdin
    discardPendingInput(inputStream)
    print(prompt, end="", flush=True)
    received = b""
    while b"\n" not in received:
        chunk = os.read(inputStream.fileno(), 4096)
        if chunk == b"":
            raise EOFError("operator input closed")
        received += chunk
    return received.split(b"\n", 1)[0].decode(errors="replace")


def confirm(prompt, inputStream=None):
    return readAnswer(prompt + " [y/n] ", inputStream).strip().lower().startswith('y')


def pause(prompt, inputStream=None):
    readAnswer(prompt + "  Press Enter when ready. ", inputStream)


def waitForPin(grbl, axisLetter, present, timeoutSeconds=30):
    # Poll the status report's Pn: field until axisLetter appears (present=True)
    # or clears (present=False).
    deadline = time.monotonic() + timeoutSeconds
    while time.monotonic() < deadline:
        if (axisLetter in grbl.getPinState()) == present:
            return True
        time.sleep(0.2)
    return False


def jog(grbl, axisWord):
    # One jog command, then wait until motion has actually finished so that
    # position reads and pin checks see a machine at rest.
    grbl.send(f'$J=G91 {axisWord}')
    while grbl.getGrblState() != 'Idle':
        time.sleep(0.05)


def axisCoordinate(grbl, axisLetter):
    position = grbl.getMachineCoordinates()
    return {'X': position.x, 'Y': position.y, 'Z': position.z}[axisLetter]


def pressTest(grbl, axes):
    # No motion.  Both switches of an axis share one Grbl input pin, so Grbl can
    # never count them; having the operator press each end is the only way to
    # prove both switches exist, work, and are wired to the right axis.
    print("\n=== Switch press test (no motion) ===")
    verified = {}
    for axis in axes:
        ends = ["MIN (left/front/bottom)", "MAX (right/back/top)"]
        axisOk = True
        for end in ends:
            pause(f"Press and HOLD the {axis}-axis switch at the {end} end.")
            if not waitForPin(grbl, axis, present=True, timeoutSeconds=15):
                print(f"  !! No {axis} pin trigger seen. Switch dead, unplugged, or wired to another axis.")
                axisOk = False
            else:
                print(f"  {axis} {end}: triggered, good.")
            pause(f"Release the switch.")
            if not waitForPin(grbl, axis, present=False, timeoutSeconds=15):
                print(f"  !! {axis} pin stayed triggered after release (stuck switch?).")
                axisOk = False
        verified[axis] = axisOk
    return verified


def directionTest(grbl, axes):
    # The operator's eyes are the only source of truth for which way is
    # physically "right"/"away"/"up".  Fix $3 until reality matches convention.
    print("\n=== Axis direction test ===")
    print("Homing to bottom-left (Z up) first so there is room to jog inward.")
    alarm = grbl.tryHomingCycle(GrblController.HomingPositions.bottomLeftZUp)
    if alarm is not None:
        sys.exit(f"Cannot home bottom-left (ALARM:{alarm}); fix that before calibrating directions.")

    questions = {'X': ("X10", "Did the tool move RIGHT?"),
                 'Y': ("Y10", "Did the tool move AWAY from you?"),
                 'Z': ("Z-10", "Did the tool move DOWN?")}
    flipped = False
    for axis in axes:
        axisWord, question = questions[axis]
        pause(f"About to jog {axis} by 10mm for the direction check.")
        jog(grbl, f"{axisWord} F{JOG_FEED}")
        if not confirm(question):
            current = int(grbl.getSettings().get('$3', '0'))
            grbl.send(f'$3={current ^ DIRECTION_BITS[axis]}')
            flipped = True
            print(f"  Flipped {axis} in $3. Re-homing to re-establish position, then re-checking.")
            alarm = grbl.tryHomingCycle(GrblController.HomingPositions.bottomLeftZUp)
            if alarm is not None:
                sys.exit(f"Homing failed after flipping $3 (ALARM:{alarm}).")
            jog(grbl, f"{axisWord} F{JOG_FEED}")
            if not confirm(question):
                sys.exit(f"{axis} direction still wrong after flipping $3 — check wiring/mechanics.")
    if flipped:
        print("Directions corrected. Re-homing for a clean starting position.")
        alarm = grbl.tryHomingCycle(GrblController.HomingPositions.bottomLeftZUp)
        if alarm is not None:
            sys.exit(f"Homing failed (ALARM:{alarm}).")
    return grbl.getSettings().get('$3', '0')


def operatorPressedEnter(inputStream):
    # Non-blocking: True if input (an Enter press) is waiting on inputStream.
    # Whatever is waiting is consumed so it can't also answer the next prompt.
    # It is read with os.read on the file descriptor, not readline(): Python's
    # buffered readline() can pull a second line into its own buffer, where
    # select() can no longer see it, and the next Enter would be missed.
    # End-of-file (stdin closed) also counts as a stop: if we can no longer
    # hear the operator, we must not keep moving.
    readable, _, _ = select.select([inputStream], [], [], 0)
    if not readable:
        return False
    os.read(inputStream.fileno(), 4096)
    return True


def discardPendingInput(inputStream):
    # An extra Enter typed at an earlier prompt would otherwise stop the probe
    # at 0mm.  Throw away anything already waiting before the probe starts.
    while select.select([inputStream], [], [], 0)[0]:
        if os.read(inputStream.fileno(), 4096) == b"":
            break  # EOF: nothing more will ever arrive


def creepProbe(grbl, axis, sign, inputStream=None):
    # Step `axis` by PROBE_STEP in direction `sign` until one of three things
    # happens, checked before every step:
    #   - the axis limit pin shows in Pn:          -> STOPPED_BY_SWITCH
    #   - the operator pressed Enter                -> STOPPED_BY_OPERATOR
    #   - PROBE_MAX_TRAVEL covered with neither     -> STOPPED_BY_MAX_TRAVEL
    # Returns (distance travelled in mm, terminator).  Leaves the machine where
    # it stopped; the caller does the back-off.
    if inputStream is None:
        inputStream = sys.stdin
    start = axisCoordinate(grbl, axis)
    steps = 0
    while True:
        if axis in grbl.getPinState():
            terminator = STOPPED_BY_SWITCH
            break
        if operatorPressedEnter(inputStream):
            terminator = STOPPED_BY_OPERATOR
            break
        if PROBE_STEP * steps >= PROBE_MAX_TRAVEL:
            terminator = STOPPED_BY_MAX_TRAVEL
            break
        jog(grbl, f"{axis}{sign * PROBE_STEP} F{PROBE_FEED}")
        steps += 1
    return abs(axisCoordinate(grbl, axis) - start), terminator


def describeStop(axis, measured, terminator):
    if terminator == STOPPED_BY_OPERATOR:
        where = "usable depth — obstruction below" if axis == 'Z' else "usable travel — obstruction in the path"
        return f"{axis}: stopped by operator at {measured:.3f}mm ({where})"
    return f"{axis}: stopped by {axis} limit switch at {measured:.3f}mm"


def measureTravel(grbl, axes, inputStream=None):
    # Creep probe: from a fresh bottom-left home, step toward the far end of each
    # axis.  Requires hard limits off (a switch touch must not alarm).
    #   X, Y: stop automatically on the far switch (the press test proved it
    #         exists); Enter is an emergency stop, e.g. for a clamp in the path.
    #   Z:    the bottom switch is usually BELOW the baseboard / spoiler board /
    #         workpiece, so the operator must stop it with Enter.  The switch
    #         still stops it if it happens to trigger first.
    # Returns ({axis: mm}, {axis: terminator}).
    if inputStream is None:
        inputStream = sys.stdin
    print("\n=== Travel measurement (creep probe) ===")
    travel = {}
    stoppedBy = {}
    probeSigns = {'X': +1, 'Y': +1, 'Z': -1}  # homed bottom-left Z-up: far end is +X, +Y, -Z
    for axis in axes:
        if axis == 'Z':
            print(f"\n  !! WARNING: the Z probe creeps the tool DOWNWARD in {PROBE_STEP:g}mm steps"
                  f" at {PROBE_FEED}mm/min.")
            print("  !! It will NOT stop by itself before the baseboard, spoiler board or workpiece.")
            print("  !! WATCH THE TOOL TIP and press Enter the moment it approaches whatever is below it.")
            print("  !! (It also stops on its own if the bottom Z switch triggers first.)")
            pause("Z probe: hand on the keyboard, eyes on the tool tip.", inputStream)
        else:
            pause(f"About to creep {axis} toward its far switch at {PROBE_FEED}mm/min"
                  " (press Enter at any time to stop it).", inputStream)
        alarm = grbl.tryHomingCycle(GrblController.HomingPositions.bottomLeftZUp)
        if alarm is not None:
            sys.exit(f"Re-homing before {axis} probe failed (ALARM:{alarm}).")
        discardPendingInput(inputStream)
        measured, terminator = creepProbe(grbl, axis, probeSigns[axis], inputStream)
        if terminator == STOPPED_BY_MAX_TRAVEL:
            sys.exit(f"No {axis} switch and no operator stop within {PROBE_MAX_TRAVEL}mm — aborting.")
        travel[axis] = measured
        stoppedBy[axis] = terminator
        print("  " + describeStop(axis, measured, terminator))
        # Back away so no switch stays triggered and the tool clears any obstruction
        jog(grbl, f"{axis}{-probeSigns[axis] * PROBE_BACK_OFF} F{JOG_FEED}")
    return travel, stoppedBy


def cornerSweep(grbl):
    print("\n=== Homing corner sweep ===")
    homeable = {}
    for value, description in CORNERS.items():
        pause(f"About to home toward {description} ($23={value}).")
        alarm = grbl.tryHomingCycle(value)
        homeable[value] = alarm is None
        print(f"  {description}: " + ("homed OK" if alarm is None else f"FAILED (ALARM:{alarm})"))
    return homeable


# ---------------------------------------------------------------------------
# Z limit ($132)
# ---------------------------------------------------------------------------
#
# Grbl's soft limit fences the SPINDLE position, not the bit tip.  In stock
# Grbl 1.1h (HOMING_FORCE_SET_ORIGIN off) every axis's allowed machine range is
# [-$13x, 0]; Z homes to the top, at MPos z = -$27.  A target below -$132 is
# refused: a jog with error:15, any other motion (G0/G1/G38.2) with ALARM:2,
# which also needs a reset.
#
# The bit tip is a fixed distance below the spindle while one bit is fitted, so
# "the spindle MPos z at which the tip touches a surface" is a usable stand-in
# for the surface height.  Call that the surface z.
#   probe: G38.2 reports [PRB:x,y,z:1], z = spindle MPos when the tip touched the
#          TOP of the plate.  The tip reaches the surface the plate sits on after
#          another plateThickness of travel, so surface z = z - plateThickness.
#   jog:   surface z = spindle MPos z when Enter is pressed.
# The lowest the spindle may go is
#   lowest = surface z - materialThickness - intoDepth   (materialThickness = 0
#                                                         when the surface is the
#                                                         spoilboard)
# and the soft limit that puts the fence exactly there is $132 = -lowest.
# $132 is rounded DOWN to 0.001 mm (Grbl's $$ shows 3 decimals): never deeper
# than asked for, at most 0.001 mm shallower.

ZLIMIT_SUFFIX = ".zlimit.json"   # sidecar next to profiles/<name>.json
METHOD_SPOILBOARD = "spoilboard"
METHOD_WORKPIECE = "workpiece"
METHOD_JOG = "jog"
METHODS = (METHOD_SPOILBOARD, METHOD_WORKPIECE, METHOD_JOG)
SURFACE_SPOILBOARD = "spoilboard"
SURFACE_WORKPIECE = "workpiece"

PROBE_PIN = 'P'             # the probe input's letter in the status report's Pn: field
PROBE_SEEK_FEED = 100       # mm/min, first touch
PROBE_FINE_FEED = 20        # mm/min, second touch, for the measurement
PROBE_RETRACT = 2.0         # mm up after each touch
PROBE_FINE_DISTANCE = 4.0   # mm, bound on the second touch (retract + 2 mm)
PROBE_LIMIT_MARGIN = 0.5    # mm kept clear of the current Z soft limit
PROBE_PIN_TIMEOUT = 15      # s for the operator to touch the plate to the bit

BIT_CHANGE_WARNING = (
    "!! This Z limit is only valid for the bit fitted now.  Grbl fences the spindle,\n"
    "!! not the tip: a longer bit would cut deeper.  After a bit change, set the\n"
    "!! limit again (see issue #30).")


class ZLimitRefused(ValueError):
    """The requested Z limit is not safe or not possible; nothing was written."""


class ProbeFailed(RuntimeError):
    """The probe did not give a measurement (no contact, alarm, wiring)."""


def floorTo3(value):
    # Round down to 0.001 mm; the 1e-6 absorbs float noise (52.5 arriving as 52.4999999).
    return math.floor(value * 1000 + 1e-6) / 1000


def computeZLimit(surfaceZ, intoDepth, fullTravel, materialThickness=0.0, pullOff=0.0):
    """Return (lowest spindle z, new $132) for a surface z, or raise ZLimitRefused.

    surfaceZ: spindle MPos z at which the tip touches the reference surface.
    fullTravel: the calibrated full Z travel (profile.workAreaZ).
    pullOff: $27; Z sits at -pullOff after homing, so the fence must be below that.
    """
    if intoDepth < 0:
        raise ZLimitRefused(f"The depth into the spoilboard must be 0 or more, not {intoDepth:g}.")
    if materialThickness < 0:
        raise ZLimitRefused(f"The material thickness must be 0 or more, not {materialThickness:g}.")
    lowest = surfaceZ - materialThickness - intoDepth
    new132 = floorTo3(-lowest)
    if new132 <= 0:
        raise ZLimitRefused(f"That puts the lowest spindle position at z={lowest:.3f}, at or above "
                            f"home (z=0), so $132 would be {new132:.3f}.  Check the measurements.")
    if new132 <= pullOff:
        raise ZLimitRefused(f"$132 would be {new132:.3f}, not below the homed position "
                            f"(z=-{pullOff:g}, the $27 pull-off), so the machine could not move "
                            "after homing.  Check the measurements.")
    if new132 > fullTravel + 1e-9:
        raise ZLimitRefused(f"$132 would be {new132:.3f}, deeper than the calibrated full Z travel "
                            f"({fullTravel:.3f}, workAreaZ).  Check the measurements.")
    return lowest, new132


def zLimitPath(profilePath):
    return os.path.splitext(profilePath)[0] + ZLIMIT_SUFFIX


def loadZLimitRecord(path):
    if not os.path.exists(path):
        return {"schemaVersion": 1}
    with open(path) as jsonFile:
        return json.load(jsonFile)


def saveZLimitRecord(path, record):
    with open(path, 'w') as jsonFile:
        json.dump(record, jsonFile, indent=2)
        jsonFile.write('\n')


def askNumber(ask, prompt, minimum=0.0):
    # A number >= minimum; asks again until it gets one.
    while True:
        text = ask(prompt).strip()
        try:
            value = float(text)
        except ValueError:
            print(f"  '{text}' is not a number.")
            continue
        if math.isnan(value) or value < minimum:
            print(f"  It must be {minimum:g} or more.")
            continue
        return value


def askChoice(ask, prompt, choices):
    # One of `choices`; accepts a unique prefix ("s" for spoilboard).
    while True:
        text = ask(prompt).strip().lower()
        matches = [choice for choice in choices if text and choice.startswith(text)]
        if len(matches) == 1:
            return matches[0]
        print(f"  Please answer one of: {', '.join(choices)}.")


def getPlateThickness(record, sidecarPath, ask, out=print):
    # Asked once, the first time a probe method is used, then saved in the sidecar.
    if record.get("plateThickness") is not None:
        out(f"  Probe plate thickness: {record['plateThickness']:g} mm (saved; change it with "
            "--plate-thickness).")
        return record["plateThickness"]
    value = askNumber(ask, "Probe plate thickness in mm (measured once, then saved): ")
    record["plateThickness"] = value
    saveZLimitRecord(sidecarPath, record)
    out(f"  Saved the plate thickness to {sidecarPath}.")
    return value


def probePinCheck(grbl, ask, out=print, timeoutSeconds=PROBE_PIN_TIMEOUT):
    # Like the switch press test: no motion.  Proves the clip and plate reach
    # Grbl's probe input before anything moves toward the plate.
    if PROBE_PIN in grbl.getPinState():
        raise ProbeFailed("The probe input is already triggered (P in Pn:) with nothing touching. "
                          "Is the plate touching the bit or clip, or is the wiring shorted?")
    ask("Probe check: clip on the bit; touch the plate to the bit and HOLD it there.  "
        "Press Enter when ready. ")
    if not waitForPin(grbl, PROBE_PIN, present=True, timeoutSeconds=timeoutSeconds):
        raise ProbeFailed("No probe trigger seen (no P in Pn:).  Check the clip, the plate lead "
                          "and the probe input wiring.")
    out("  Probe input triggered, good.")
    ask("Take the plate away from the bit.  Press Enter when ready. ")
    if not waitForPin(grbl, PROBE_PIN, present=False, timeoutSeconds=timeoutSeconds):
        raise ProbeFailed("The probe input stayed triggered after the plate was taken away.")


def parseProbeReport(line):
    # '[PRB:-10.000,-20.000,-55.250:1]' -> (-55.250, True)  (z, success)
    body = line.strip()[len('[PRB:'):-1]
    coordinates, _, flag = body.rpartition(':')
    return float(coordinates.split(',')[2]), flag == '1'


def sendProbe(grbl, distance, feed):
    """One G38.2 straight down by `distance` mm.  Returns the contact z (spindle MPos).

    GrblController.send() would throw the [PRB:...] line away, so this writes the
    line and reads the answers itself.  Raises ProbeFailed on no contact or alarm.
    """
    # G91 on the same line: the distance is relative to where the spindle is.  On
    # its own, 'G38.2 Z-50' in G90 would mean work-coordinate Z=-50.
    gcode = f"G91 G21 G38.2 Z-{distance:.3f} F{feed:g}"
    originalTimeout = grbl.timeout
    grbl.timeout = distance / feed * 60 + 10  # Grbl says nothing until the probe ends
    contactZ = success = alarm = None
    try:
        grbl.write((gcode + "\n").encode())
        while True:
            line = grbl.readline()
            if line == b"":
                if alarm is not None:
                    break  # alarms may or may not be followed by ok
                raise ProbeFailed(f"No answer from Grbl within {grbl.timeout:.0f}s of '{gcode}'.")
            text = line.decode(errors='replace').strip()
            if text.startswith('[PRB:'):
                contactZ, success = parseProbeReport(text)
            elif text.startswith('ALARM:'):
                alarm = text.partition(':')[2]
                grbl.timeout = 2
            elif text.startswith('error:'):
                raise ProbeFailed(f"Grbl refused '{gcode}': {text}.")
            elif text == 'ok':
                break
    finally:
        grbl.timeout = originalTimeout

    if alarm is not None:
        if alarm in ('4', '5'):
            # Probe-fail alarms: the motion ended normally, so the position is still
            # good; unlocking (no motion) is safe.  4 = probe already triggered at
            # the start, 5 = no contact within the distance.
            grbl.send("$X")
            grbl.send("G90")
            reason = ("the probe input was already triggered" if alarm == '4'
                      else f"no contact within {distance:.3f} mm")
            raise ProbeFailed(f"Probe failed (ALARM:{alarm}: {reason}).  Unlocked with $X; "
                              "the machine position is still valid.")
        raise ProbeFailed(f"Grbl went into ALARM:{alarm} during the probe.  Reset and re-home "
                          "before continuing.")
    grbl.send("G90")
    if contactZ is None or not success:
        raise ProbeFailed(f"The probe did not report a contact ({'no [PRB] line' if contactZ is None else '[PRB:...:0]'}).")
    return contactZ


def retract(grbl, contactZ, pullOff, distance=PROBE_RETRACT):
    # Up off the plate, without asking for more room than there is below home.
    up = floorTo3(min(distance, -contactZ - pullOff - 0.01))
    if up > 0:
        jog(grbl, f"Z{up:.3f} F{JOG_FEED}")


def probeSurface(grbl, settings, plateThickness, ask, out=print, pinTimeout=PROBE_PIN_TIMEOUT):
    """Pin check, then a two-touch G38.2 probe.  Returns (contact z, surface z)."""
    probePinCheck(grbl, ask, out, pinTimeout)
    ask("Put the plate on the surface, directly under the bit, and keep the clip on the bit.  "
        "Press Enter when ready. ")
    state = grbl.getGrblState()
    if state != 'Idle':
        raise ProbeFailed(f"Grbl is in state '{state}', not Idle; not probing.")
    startZ = grbl.getMachineCoordinates().z
    limitZ = -float(settings['$132'])
    pullOff = float(settings.get('$27', '0'))
    room = floorTo3(startZ - limitZ - PROBE_LIMIT_MARGIN)
    if room < 1:
        raise ProbeFailed(f"Only {room:.3f} mm left above the Z soft limit (z={limitZ:.3f}); "
                          "nothing to probe.  Raise Z, or reset $132 with --reset-z-limit.")
    out(f"  The probe goes down at most {room:.3f} mm (the current $132={settings['$132']} fences "
        f"z={limitZ:.3f}), at {PROBE_SEEK_FEED} mm/min.  If the surface is lower, run "
        "--reset-z-limit first.")
    ask("About to probe DOWN.  Hand on the E-stop button.  Press Enter when ready. ")
    try:
        seekZ = sendProbe(grbl, room, PROBE_SEEK_FEED)
    except ProbeFailed:
        # No contact leaves the bit at the bottom of the probe; take it back up to
        # where it started (a move toward home), so a retry has room.
        if grbl.getGrblState() == 'Idle':
            up = floorTo3(startZ - grbl.getMachineCoordinates().z)
            if up > 0:
                out(f"  Returning {up:.3f} mm up to the starting height.")
                jog(grbl, f"Z{up:.3f} F{JOG_FEED}")
        raise
    retract(grbl, seekZ, pullOff)
    fineDistance = floorTo3(min(PROBE_FINE_DISTANCE, grbl.getMachineCoordinates().z - limitZ
                                - PROBE_LIMIT_MARGIN))
    contactZ = sendProbe(grbl, fineDistance, PROBE_FINE_FEED)
    retract(grbl, contactZ, pullOff, PROBE_BACK_OFF)
    out(f"  Touches at z={seekZ:.3f} (seek) and z={contactZ:.3f} (fine).  Remove the plate.")
    return contactZ, contactZ - plateThickness


def jogToSurface(grbl, ask, out, runJog):
    # Returns the spindle z with the tip on the surface, or None if Esc was pressed.
    ask("Jog the bit tip down onto the surface: Shift+Down lowers Z, hold Ctrl to creep, "
        "tap Shift+Down to step 0.1 mm, Alt+Shift+Down 0.01 mm "
        "(a sheet of paper under the tip helps).  Enter accepts, Esc aborts.  "
        "Press Enter to start jogging. ")
    if runJog(grbl, True, out=out) != jogModule.ACCEPTED:
        return None
    return grbl.getMachineCoordinates().z


def writeZLimit(grbl, value, out=print):
    # $132 is an EEPROM write; Grbl takes $ settings only in Idle (or Alarm).
    state = grbl.getGrblState()
    if state != 'Idle':
        raise ZLimitRefused(f"Grbl is in state '{state}', not Idle; not writing $132.")
    text = f"{value:.3f}"
    grbl.send(f"$132={text}")  # raises on error:; but a timeout looks like ok, so read back
    readBack = grbl.getSettings().get('$132')
    if readBack != text:
        raise RuntimeError(f"Wrote $132={text} but $$ reads back $132={readBack}.")
    out(f"  $132={text} written and read back.")


STEP_TITLES = {'method': "method", 'surface': "surface measurement",
               'kind': "surface kind", 'thickness': "material thickness",
               'intoDepth': "depth into the spoilboard"}
STEP_ORDER = ('method', 'surface', 'kind', 'thickness', 'intoDepth')


def surfaceKind(choices):
    if choices.get('method') == METHOD_SPOILBOARD:
        return SURFACE_SPOILBOARD
    if choices.get('method') == METHOD_WORKPIECE:
        return SURFACE_WORKPIECE
    return choices.get('kind')


def stepApplies(name, choices):
    if name == 'kind':
        return choices.get('method') == METHOD_JOG
    if name == 'thickness':
        return surfaceKind(choices) == SURFACE_WORKPIECE
    return True


def describeChoice(name, choices):
    if name == 'method':
        return choices['method']
    if name == 'surface':
        if choices.get('measuredBy'):
            return f"{choices['measuredBy']} top at z={choices['surface']:.3f}"
        if 'contactZ' in choices:
            return (f"probe touched at z={choices['contactZ']:.3f}, plate {choices['plate']:g} mm "
                    f"-> surface z={choices['surface']:.3f}")
        return f"jogged to the surface at z={choices['surface']:.3f}"
    if name == 'kind':
        return choices['kind']
    return f"{choices[name]:g} mm"


def forgetChoice(choices, name):
    # Drop one answer, and whatever was measured or decided because of it.
    dependants = {'method': ('surface', 'contactZ', 'plate', 'kind', 'measuredBy'),
                  'surface': ('contactZ', 'plate', 'measuredBy')}
    choices.pop(name, None)
    for key in dependants.get(name, ()):
        choices.pop(key, None)


def askYesNo(ask, prompt):
    return ask(prompt + " [y/n] ").strip().lower().startswith('y')


def runZLimitStep(grbl, profile, profilePath, method=None, intoDepth=None, thickness=None,
                  ask=None, out=print, runJog=None, checkEnvironment=None,
                  pinTimeout=PROBE_PIN_TIMEOUT, today=None, surface=None, measuredBy=None,
                  recordExtra=None):
    """Interactive Z-limit step.  Returns the $132 written, or None if nothing was written.

    surface, measuredBy, recordExtra: for a caller that already knows the
    surface z (surface-spoiler-board.py: the surfaced top).  The surface step
    is then filled from `surface` instead of being measured, described as
    "<measuredBy> top", and the spoilboard record gets measuredBy plus
    recordExtra's fields.  Changing that step at the summary measures afresh.

    The machine must be homed.  Nothing is written to Grbl before the summary is
    confirmed.  The spoilboard result also becomes the profile's calibrated $132
    (profile.expectedSettings) and is recorded in the sidecar; a workpiece result
    changes only Grbl's $132.
    """
    ask = ask or readAnswer
    runJog = runJog or jogModule.runKeyboardJog
    checkEnvironment = checkEnvironment or jogModule.checkJogEnvironment
    today = today or datetime.date.today().isoformat()

    out("\n=== Z limit ($132) ===")
    jogAvailable = checkEnvironment(out=out) == jogModule.ENVIRONMENT_OK
    methods = METHODS if jogAvailable else (METHOD_SPOILBOARD, METHOD_WORKPIECE)
    if not jogAvailable:
        out("  Keyboard jog is unavailable here, so only the probe methods are offered.")
    if method == METHOD_JOG and not jogAvailable:
        out("  --method jog was given, but keyboard jog is unavailable; choose another method.")
        method = None

    sidecarPath = zLimitPath(profilePath)
    record = loadZLimitRecord(sidecarPath)
    settings = grbl.getSettings()
    old132 = settings.get('$132')
    pullOff = float(settings.get('$27', '0'))
    fullTravel = float(profile.workAreaZ)

    given = {'method': method, 'intoDepth': intoDepth, 'thickness': thickness, 'surface': surface}
    choices = {}

    def fill(name):
        # Ask for (or measure) one step.  Returns False if the measurement was
        # abandoned, so the method has to be chosen again.
        if given.get(name) is not None:
            choices[name] = given.pop(name)
            if name == 'surface':
                choices['measuredBy'] = measuredBy
            source = "given" if name == 'surface' else "from the command line"
            out(f"  {STEP_TITLES[name].capitalize()}: {describeChoice(name, choices)} ({source})")
            return True
        if name == 'method':
            choices['method'] = askChoice(ask, f"Find the reference surface by ({' / '.join(methods)}): ",
                                          methods)
        elif name == 'surface':
            if choices['method'] == METHOD_JOG:
                surfaceZ = jogToSurface(grbl, ask, out, runJog)
                if surfaceZ is None:
                    out("  Jog aborted (Esc).")
                    return False
                choices['surface'] = surfaceZ
            else:
                plate = getPlateThickness(record, sidecarPath, ask, out)
                if jogAvailable and askYesNo(ask, "Jog the bit over the plate spot (and closer "
                                                  "to it) first?"):
                    runJog(grbl, True, out=out)
                try:
                    contactZ, surfaceZ = probeSurface(grbl, settings, plate, ask, out, pinTimeout)
                except ProbeFailed as failure:
                    out(f"  !! {failure}")
                    return False
                choices.update(surface=surfaceZ, contactZ=contactZ, plate=plate)
        elif name == 'kind':
            choices['kind'] = askChoice(ask, "Is the surface under the bit the spoilboard or the "
                                             "workpiece? (spoilboard / workpiece): ",
                                        (SURFACE_SPOILBOARD, SURFACE_WORKPIECE))
        elif name == 'thickness':
            choices['thickness'] = askNumber(ask, "Material (workpiece) thickness in mm: ")
        elif name == 'intoDepth':
            choices['intoDepth'] = askNumber(ask, "Depth to cut into the spoilboard in mm (0 or more, "
                                                  "usually 1 or more): ")
        return True

    while True:
        # Fill the first step that applies and has no answer yet; repeat until none is missing.
        missing = [name for name in STEP_ORDER if stepApplies(name, choices) and name not in choices]
        if missing:
            if not fill(missing[0]) and not askYesNo(ask, "Try the measurement again?"):
                forgetChoice(choices, 'method')  # back to choosing the method
            continue

        steps = [name for name in STEP_ORDER if stepApplies(name, choices)]
        kind = surfaceKind(choices)
        materialThickness = choices['thickness'] if kind == SURFACE_WORKPIECE else 0.0
        refusal = None
        try:
            lowest, new132 = computeZLimit(choices['surface'], choices['intoDepth'], fullTravel,
                                           materialThickness, pullOff)
        except ZLimitRefused as error:
            refusal = error

        out("\n--- Z limit summary (nothing written yet) ---")
        for number, name in enumerate(steps, 1):
            out(f"  {number}. {STEP_TITLES[name].capitalize()}: {describeChoice(name, choices)}")
        if refusal:
            out(f"  REFUSED: {refusal}")
        else:
            formula = "surface z" + (" - thickness" if kind == SURFACE_WORKPIECE else "") + " - into-depth"
            out(f"  Lowest spindle z = {formula} = {lowest:.3f}")
            out(f"  New $132 = {new132:.3f} (rounded down to 0.001 mm); old $132 = {old132}")
            if kind == SURFACE_SPOILBOARD:
                out("  Kept until the spoilboard is replaced: becomes the profile's calibrated $132 "
                    "(what --reset-z-limit restores).")
            else:
                out("  For this job only: Grbl's $132 changes, the profile does not.")
            out("  $132 stays in Grbl's EEPROM after the job, until --reset-z-limit.")

        prompt = (("y to write it, " if not refusal else "")
                  + f"1-{len(steps)} to change that step, b to change the most recent, q to quit: ")
        answer = ask(prompt).strip().lower()
        if answer == 'q':
            out("  Nothing written.")
            return None
        if answer == 'y' and not refusal:
            break
        if answer == 'b':
            forgetChoice(choices, steps[-1])
        elif answer.isdigit() and 1 <= int(answer) <= len(steps):
            forgetChoice(choices, steps[int(answer) - 1])

    writeZLimit(grbl, new132, out)
    if kind == SURFACE_SPOILBOARD:
        spoilboard = {"surfaceZ": round(choices['surface'], 3), "intoDepth": choices['intoDepth'],
                      "zLimit": new132, "measuredOn": today,
                      "measuredBy": choices.get('measuredBy')
                      or ("probe" if 'contactZ' in choices else "jog")}
        if choices.get('measuredBy') and recordExtra:
            spoilboard.update(recordExtra)
        if 'contactZ' in choices:
            spoilboard["probeContactZ"] = round(choices['contactZ'], 3)
            spoilboard["plateThickness"] = choices['plate']
        record["spoilboard"] = spoilboard
        saveZLimitRecord(sidecarPath, record)
        profile.expectedSettings["$132"] = f"{new132:.3f}"
        profile.save(profilePath)
        out(f"  Profile {profilePath}: calibrated $132 is now {new132:.3f} "
            f"(workAreaZ stays {fullTravel:.3f}, the full travel).  Spoilboard recorded in {sidecarPath}.")
    out(BIT_CHANGE_WARNING)
    return new132


def resetZLimit(grbl, profile, out=print):
    """Put Grbl's $132 back to the profile's calibrated value.  Returns that value."""
    calibrated = profile.expectedSettings.get("$132")
    if calibrated is None:
        calibrated = f"{float(profile.workAreaZ):.3f}"
        out(f"  The profile has no calibrated $132; using workAreaZ ({calibrated}).")
    current = grbl.getSettings().get('$132')
    if current == calibrated:
        out(f"  $132 is already {calibrated}, the profile's calibrated value.")
        return float(calibrated)
    out(f"  Resetting $132 from {current} to the profile's calibrated {calibrated}.")
    writeZLimit(grbl, float(calibrated), out)
    return float(calibrated)


def setPlateThickness(profilePath, value, out=print):
    if value < 0:
        sys.exit(f"--plate-thickness must be 0 or more, not {value:g}.")
    sidecarPath = zLimitPath(profilePath)
    record = loadZLimitRecord(sidecarPath)
    record["plateThickness"] = value
    saveZLimitRecord(sidecarPath, record)
    out(f"Probe plate thickness set to {value:g} mm in {sidecarPath}.")


def zLimitMain(arguments):
    # --z-limit / --reset-z-limit / --plate-thickness on their own, for an
    # already-calibrated machine.
    path = os.path.join(arguments.profiles_dir, f"{arguments.name}.json")
    if not os.path.exists(path):
        sys.exit(f"No profile {path}; run the full calibration first.")
    profile = MachineProfile.load(path)
    if arguments.plate_thickness is not None:
        setPlateThickness(path, arguments.plate_thickness)
    if not (arguments.z_limit or arguments.reset_z_limit):
        return

    port = arguments.port or profile.portDeviceName
    baud = arguments.baud or profile.portBaudRate
    print(f"Connecting to {port} @ {baud}...")
    # Deliberately WITHOUT profile=: GrblController's connect-time check treats
    # $132 as critical and would refuse (or, with applySettings, overwrite) a
    # per-job $132 that differs from the profile.
    grbl = GrblController(port, baud)
    if profile.homingPosition != grbl.homingPosition:
        grbl.homingPosition = profile.homingPosition
        grbl.send('$23=' + profile.homingPosition)  # the constructor wrote its default

    if arguments.reset_z_limit:
        print("\n=== Reset Z limit ===")
        resetZLimit(grbl, profile)
        return

    print("\nKeep a hand near the E-stop button at all times.")
    if not confirm("Has the machine been homed ($H) since it was powered on?"):
        pause("About to home the machine.")
        alarm = grbl.tryHomingCycle(profile.homingPosition)
        if alarm is not None:
            sys.exit(f"Homing failed (ALARM:{alarm}); not setting a Z limit.")
    runZLimitStep(grbl, profile, path, method=arguments.method,
                  intoDepth=arguments.into_depth, thickness=arguments.thickness)


def main():
    parser = argparse.ArgumentParser(description="One-time Grbl machine capability test")
    parser.add_argument("name", help="machine name; profile saved as profiles/<name>.json")
    parser.add_argument("--port", help="default /dev/ttyUSB0, or the profile's port with --z-limit")
    parser.add_argument("--baud", type=int, help="default 115200, or the profile's rate with --z-limit")
    parser.add_argument("--axes", default="XYZ", help="axes to calibrate, e.g. XY for a laser")
    parser.add_argument("--laser", action="store_true", help="machine is a laser etcher")
    parser.add_argument("--profiles-dir", default="profiles")
    zLimit = parser.add_argument_group("Z limit ($132)")
    zLimit.add_argument("--z-limit", action="store_true",
                        help="only set the Z soft limit, on an already-calibrated machine")
    zLimit.add_argument("--reset-z-limit", action="store_true",
                        help="put $132 back to the profile's calibrated value")
    zLimit.add_argument("--method", choices=METHODS, help="how to find the reference surface")
    zLimit.add_argument("--into-depth", type=float, metavar="MM", help="depth to cut into the spoilboard")
    zLimit.add_argument("--thickness", type=float, metavar="MM", help="material thickness (workpiece)")
    zLimit.add_argument("--plate-thickness", type=float, metavar="MM",
                        help="set the saved probe plate thickness")
    arguments = parser.parse_args()
    if arguments.z_limit and arguments.reset_z_limit:
        parser.error("--z-limit and --reset-z-limit are separate runs; give one of them.")
    if arguments.z_limit or arguments.reset_z_limit or arguments.plate_thickness is not None:
        zLimitMain(arguments)
        return
    arguments.port = arguments.port or "/dev/ttyUSB0"
    arguments.baud = arguments.baud or 115200
    axes = [a for a in arguments.axes.upper() if a in "XYZ"]

    print(f"Connecting to {arguments.port} @ {arguments.baud}...")
    grbl = GrblController(arguments.port, arguments.baud)
    snapshot = grbl.getSettings()
    print(f"Connected. Grbl {grbl.grblVersion or 'version unknown'}, {len(snapshot)} settings read.")

    print("\nKeep a hand near the E-stop button at all times.")
    pause("Clear the work area of clamps, stock, and tools.")

    # Limits off during the test: a probed switch touch must not hard-alarm, and
    # unmeasured travel means soft limits can't be trusted yet.
    grbl.send('$21=0')
    grbl.send('$20=0')

    switchesVerified = pressTest(grbl, axes)
    if not all(switchesVerified.values()):
        if not confirm("Some switches failed the press test. Continue anyway (motion tests NOT recommended)?"):
            sys.exit("Aborted. Fix the switch wiring and rerun.")

    directionMask = directionTest(grbl, axes)
    travel, stoppedBy = measureTravel(grbl, axes)
    homeable = cornerSweep(grbl)

    # Leave the machine configured the way the profile promises: bottom-left
    # homing, measured travel, soft limits on, hard limits on (both ends exist).
    expected = {"$3": directionMask,
                "$23": GrblController.HomingPositions.bottomLeftZUp,
                "$20": "1", "$21": "1"}
    settingNumbers = {'X': "$130", 'Y': "$131", 'Z': "$132"}
    for axis in axes:
        expected[settingNumbers[axis]] = f"{travel[axis]:.3f}"
        grbl.send(f"{settingNumbers[axis]}={travel[axis]:.3f}")
    grbl.send('$23=' + GrblController.HomingPositions.bottomLeftZUp)
    grbl.send('$20=1')
    grbl.send('$21=1')

    profile = MachineProfile(
        name=arguments.name,
        portDeviceName=arguments.port,
        portBaudRate=arguments.baud,
        workAreaX=travel.get('X', 0.0),
        workAreaY=travel.get('Y', 0.0),
        workAreaZ=travel.get('Z', 0.0),
        homingPosition=GrblController.HomingPositions.bottomLeftZUp,
        isLaser=arguments.laser,
        expectedSettings=expected,
        firmwareVersion=grbl.grblVersion or "",
        calibratedOn=datetime.date.today().isoformat(),
        pullOff=float(snapshot.get('$27', 1.0)),
        settingsSnapshot=grbl.getSettings(),  # re-read: includes our changes
        homeableCorners=homeable,
        switchesVerified=switchesVerified,
    )

    # The profile has no field yet for HOW each travel was measured, so say it
    # here, before saving.  workAreaZ / $132 hold the usable value either way.
    print("\n=== Measured travel (saved as workArea* and $130-$132) ===")
    for axis in axes:
        print("  " + describeStop(axis, travel[axis], stoppedBy[axis]))

    os.makedirs(arguments.profiles_dir, exist_ok=True)
    path = os.path.join(arguments.profiles_dir, f"{arguments.name}.json")
    profile.save(path)
    print(f"\nProfile written to {path}.")
    print("Load it with: GrblController(profile=MachineProfile.load(" + repr(path) + "))")

    # Optional last step.  The corner sweep ends with a bottom-left home, so
    # machine coordinates are good if that home worked.
    homedNow = homeable.get(GrblController.HomingPositions.bottomLeftZUp)
    if 'Z' in axes and not arguments.laser and homedNow and confirm(
            "\nSet a Z soft limit for cutting now (spoilboard / workpiece / jog)?"):
        runZLimitStep(grbl, profile, path, method=arguments.method,
                      intoDepth=arguments.into_depth, thickness=arguments.thickness)


if __name__ == "__main__":
    main()
