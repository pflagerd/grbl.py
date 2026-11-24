#!/usr/bin/env python3
"""One-time per-machine capability test for grbl.py.

Attended calibration: verifies limit-switch wiring (press test), verifies axis
directions (test jogs + operator confirmation), measures physical travel per axis
(creep probe toward the far switch), and probes which homing corners work.
Writes the result as a MachineProfile JSON into profiles/<name>.json.

The machines this targets have TWO limit switches per axis (one at each end),
which is what makes the corner sweep and the creep probe safe.

Run with the machine powered, the work area clear, and a hand near the reset
button.  Nothing moves without an Enter from you first.
"""

import argparse
import datetime
import os
import sys
import time

from GrblController import GrblController, MachineProfile

JOG_FEED = 600          # mm/min for direction-test jogs
PROBE_FEED = 300        # mm/min for the creep probe (slow: we intend to touch a switch)
PROBE_STEP = 1.0        # mm per creep step
PROBE_MAX_TRAVEL = 1500  # mm; give up if no switch found by then (miswired/dead)
DIRECTION_BITS = {'X': 1, 'Y': 2, 'Z': 4}

CORNERS = {
    GrblController.HomingPositions.topRightZUp: "top-right (Z up)",
    GrblController.HomingPositions.topLeftZUp: "top-left (Z up)",
    GrblController.HomingPositions.bottomRightZUp: "bottom-right (Z up)",
    GrblController.HomingPositions.bottomLeftZUp: "bottom-left (Z up)",
}


def confirm(prompt):
    return input(prompt + " [y/n] ").strip().lower().startswith('y')


def pause(prompt):
    input(prompt + "  Press Enter when ready. ")


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


def measureTravel(grbl, axes):
    # Creep probe: from a fresh bottom-left home, step toward the far switch while
    # watching Pn.  Requires hard limits off (a touch must not alarm) and the far
    # switch to exist — which the press test just proved.
    print("\n=== Travel measurement (creep probe) ===")
    travel = {}
    probeWords = {'X': 'X', 'Y': 'Y', 'Z': 'Z'}
    probeSigns = {'X': +1, 'Y': +1, 'Z': -1}  # homed bottom-left Z-up: far switch is +X, +Y, -Z
    for axis in axes:
        pause(f"About to creep {axis} toward its far switch at {PROBE_FEED}mm/min.")
        alarm = grbl.tryHomingCycle(GrblController.HomingPositions.bottomLeftZUp)
        if alarm is not None:
            sys.exit(f"Re-homing before {axis} probe failed (ALARM:{alarm}).")
        start = axisCoordinate(grbl, axis)
        steps = 0
        while PROBE_STEP * steps < PROBE_MAX_TRAVEL:
            if axis in grbl.getPinState():
                break
            jog(grbl, f"{probeWords[axis]}{probeSigns[axis] * PROBE_STEP} F{PROBE_FEED}")
            steps += 1
        else:
            sys.exit(f"No {axis} switch found within {PROBE_MAX_TRAVEL}mm — aborting.")
        measured = abs(axisCoordinate(grbl, axis) - start)
        travel[axis] = measured
        print(f"  {axis}: {measured:.3f}mm switch-to-switch (minus pull-off).")
        # Back away so the switch isn't left triggered
        jog(grbl, f"{probeWords[axis]}{-probeSigns[axis] * 5} F{JOG_FEED}")
    return travel


def cornerSweep(grbl):
    print("\n=== Homing corner sweep ===")
    homeable = {}
    for value, description in CORNERS.items():
        pause(f"About to home toward {description} ($23={value}).")
        alarm = grbl.tryHomingCycle(value)
        homeable[value] = alarm is None
        print(f"  {description}: " + ("homed OK" if alarm is None else f"FAILED (ALARM:{alarm})"))
    return homeable


def main():
    parser = argparse.ArgumentParser(description="One-time Grbl machine capability test")
    parser.add_argument("name", help="machine name; profile saved as profiles/<name>.json")
    parser.add_argument("--port", default="/dev/ttyUSB0")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--axes", default="XYZ", help="axes to calibrate, e.g. XY for a laser")
    parser.add_argument("--laser", action="store_true", help="machine is a laser etcher")
    parser.add_argument("--profiles-dir", default="profiles")
    arguments = parser.parse_args()
    axes = [a for a in arguments.axes.upper() if a in "XYZ"]

    print(f"Connecting to {arguments.port} @ {arguments.baud}...")
    grbl = GrblController(arguments.port, arguments.baud)
    snapshot = grbl.getSettings()
    print(f"Connected. Grbl {grbl.grblVersion or 'version unknown'}, {len(snapshot)} settings read.")

    print("\nKeep a hand near reset/power at all times.")
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
    travel = measureTravel(grbl, axes)
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

    os.makedirs(arguments.profiles_dir, exist_ok=True)
    path = os.path.join(arguments.profiles_dir, f"{arguments.name}.json")
    profile.save(path)
    print(f"\nProfile written to {path}.")
    print("Load it with: GrblController(profile=MachineProfile.load(" + repr(path) + "))")


if __name__ == "__main__":
    main()
