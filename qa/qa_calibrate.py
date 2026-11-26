#!/usr/bin/env python3
"""Manual QA for calibrate.py on the real machine (issue #13, step 13).

    python qa/qa_calibrate.py            # or: python3 qa/qa_calibrate.py

Run this in one console and calibrate.py in another, from the repository root
with the venv active.  The checks cover the full calibration (press test,
direction test, travel creep probe including the operator-stopped Z probe
from PR #27, corner sweep, profile written) and the Z-limit step from PR #32
(Step 9).  The probe-method checks need a probe plate wired to the
controller's probe input; skip that section as a whole until it is.  The
checklist is plain data; qaRunner.py does the asking and recording, and
evidence.py looks for signs that a step is already done.
"""

import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import evidence  # noqa: E402
import qaRunner  # noqa: E402

MACHINE = "6060 ProverXL V1"
REPO = qaRunner.REPO_DIR
PROFILE = f"profiles/{MACHINE}.json"
SIDECAR = f"profiles/{MACHINE}.zlimit.json"
CALIBRATE = f'python calibrate.py "{MACHINE}"'
Z_LIMIT_JOG = f"{CALIBRATE} --z-limit --method jog"
RESET = f"{CALIBRATE} --reset-z-limit"
JOG = "python jog.py --home"
HOME = "X-619.000 Y-602.000 Z-3.000"
PLATE = 10
# Reads the settings that matter here.  Like every program in this repo it
# connects through GrblController, which also writes $23=3 and $120-$122.
READ_SETTINGS = ('python -c \'from GrblController import GrblController; '
                 's = GrblController().getSettings(); '
                 'print({k: s[k] for k in ("$20", "$21", "$130", "$131", "$132")})\'')
PLATE_NEEDED = "Probe plate wired to the controller's probe input."
HOMED = ("Please answer n: connecting resets this controller, so it is never homed at "
         "that point.")


# -- evidence -----------------------------------------------------------------

def readJson(relative):
    try:
        with open(os.path.join(REPO, relative), encoding="utf-8") as file:
            return json.load(file)
    except (OSError, ValueError):
        return {}


def today():
    return datetime.date.today().isoformat()


def port():
    return readJson(PROFILE).get("portDeviceName") or "/dev/ttyUSB0"


def controllerConnected():
    if os.path.exists(port()):
        return True, f"I can see the controller's port, {port()}."
    return False, f"I can't see the controller's port, {port()}, yet."


def profileUnchanged():
    """P1: git shows no changes under profiles/."""
    clean, changes = evidence.gitClean(REPO, "profiles/")
    if clean:
        return qaRunner.PASS, "git shows no changes under profiles/: a clean starting point."
    return qaRunner.FAIL, ("git shows changes under profiles/ (" + changes.replace("\n", "; ") +
                           "). Please restore them with git before calibrating, or note "
                           "why they're there.")


def calibratedToday():
    calibratedOn = readJson(PROFILE).get("calibratedOn")
    if calibratedOn == today():
        return True, f"The profile was written today (calibratedOn {calibratedOn})."
    return False, (f"The profile's calibratedOn is still {calibratedOn}: it hasn't been "
                   "written yet.")


def plateThicknessSaved():
    thickness = readJson(SIDECAR).get("plateThickness")
    if thickness == PLATE:
        return True, f"{SIDECAR} now has plateThickness {PLATE}."
    return False, f"{SIDECAR} doesn't have plateThickness {PLATE} yet."


def spoilboardRecorded(measuredBy):
    def check():
        record = readJson(SIDECAR).get("spoilboard") or {}
        calibrated = readJson(PROFILE).get("expectedSettings", {}).get("$132")
        if record.get("measuredBy") == measuredBy and record.get("measuredOn") == today():
            return True, (f"{SIDECAR} has today's spoilboard record (measuredBy "
                          f"{measuredBy}, zLimit {record.get('zLimit')}), and the profile's "
                          f"calibrated $132 is {calibrated}.")
        return False, (f"{SIDECAR} has no spoilboard record from today measured by "
                       f"{measuredBy} yet.")
    return check


# -- step helpers -------------------------------------------------------------

def there(text):
    """A step done in the console where calibrate.py is running."""
    return f"In the calibrate.py console, please {text}"


def start(command, expect, what="calibrate.py", intro=None):
    """A step that starts a program in the second console.  intro, if given, is
    a paragraph shown first (what this group of tests is about)."""
    return {"do": (intro + "\n\n" if intro else "") +
                  f"Please start {what} in the second console:",
            "run": command, "expect": expect}


def jogStep(actions, expect):
    """One run of jog.py; jog mode captures the keyboard, so it's all up front."""
    return {
        "do": "Please read all of this first, because jog mode captures the keyboard. "
              "In the second console, start jog.py:",
        "run": JOG,
        "actions": [evidence.JOG_MODE_STARTS] + actions +
                   ["Come back here once jog.py has finished."],
        "expect": expect,
    }


# -- the checklist ------------------------------------------------------------

CHECKLIST = {
    "name": "calibrate",
    "title": "calibrate.py manual hardware checks",
    "program": "qa_calibrate.py",
    "machine": MACHINE,
    "purpose": ("qa_calibrate.py guides you (the operator) through a manual test of "
                "calibrate.py. This is both to test the software, and to help familiarise "
                "you with its operation."),
    "intro": evidence.consoleSteps("calibrate.py", REPO, qaRunner.shortPath(REPO)) + [
        {"do": "Please plug in the controller's USB cable and switch the controller on.",
         "already": controllerConnected, "verify": controllerConnected, "again": True},
        {"do": "Please make sure the spindle is off and there are no clamps or stock on "
               "the table, and keep a hand near the E-stop button throughout."},
        {"do": "The Z checks need the bit you will cut with, or one with the same "
               "stick-out. Please fit it now if it isn't fitted already."},
        {"do": f"The full calibration rewrites {PROFILE}. It's tracked by git, so git "
               "diff profiles/ shows what a run changed."},
    ],
    "sections": [
        {
            "title": "Before you start",
            "items": [
                {
                    "id": "P1",
                    "title": "Clean profile",
                    "auto": profileUnchanged,
                },
                {
                    "id": "P2",
                    "title": "Starting settings",
                    "steps": [
                        {"do": "In the second console, please read the current settings:",
                         "run": READ_SETTINGS,
                         "expect": "{'$20': '1', '$21': '1', '$130': '622.000', '$131': "
                                   "'605.000', '$132': '109.000'}, matching the profile. "
                                   "Please note anything different."},
                    ],
                },
            ],
        },
        {
            "title": "Full calibration",
            "items": [
                {
                    "id": "C1",
                    "title": "Connect",
                    "steps": [
                        start(CALIBRATE, "\"Connecting to /dev/ttyUSB0 @ 115200...\", "
                              "\"Connected. Grbl 1.1h, N settings read.\", \"Keep a hand "
                              "near reset/power at all times.\" and a prompt to clear the "
                              "work area.",
                              intro="Now one run of the full calibration. This step starts "
                                    "it, and the steps after it follow its prompts in "
                                    "order: please do each one in the calibrate.py "
                                    "console, then come back here."),
                        {"do": there("press Enter."),
                         "expect": "\"=== Switch press test (no motion) ===\"."},
                    ],
                },
                {
                    "id": "C2",
                    "title": "Press test: X switches",
                    "steps": [
                        {"do": there("press and hold the X switch at the MIN end, and "
                                     "press Enter."),
                         "expect": "\"X MIN (left/front/bottom): triggered, good.\" "
                                   "Nothing moves."},
                        {"do": there("let go of the switch and press Enter."),
                         "expect": "No \"stayed triggered\"."},
                        {"do": there("press and hold the X switch at the MAX end, and "
                                     "press Enter."),
                         "expect": "\"X MAX ...: triggered, good.\""},
                        {"do": there("let go of the switch and press Enter."),
                         "expect": "No \"stayed triggered\"."},
                    ],
                },
                {
                    "id": "C3",
                    "title": "Press test: Y switches",
                    "steps": [
                        {"do": there("press and hold the Y MIN switch and press Enter, "
                                     "then let go and press Enter."),
                         "expect": "\"Y MIN ...: triggered, good.\", and no \"stayed "
                                   "triggered\". Nothing moves."},
                        {"do": there("do the same with the Y MAX switch."),
                         "expect": "\"Y MAX ...: triggered, good.\", and no \"stayed "
                                   "triggered\"."},
                    ],
                },
                {
                    "id": "C4",
                    "title": "Press test: Z switches",
                    "steps": [
                        {"do": there("press and hold the Z MIN switch and press Enter, "
                                     "then let go and press Enter."),
                         "expect": "\"Z MIN ...: triggered, good.\", and no \"stayed "
                                   "triggered\". Nothing moves."},
                        {"do": there("do the same with the Z MAX switch."),
                         "expect": "\"Z MAX ...: triggered, good.\" No question about "
                                   "continuing despite failures."},
                    ],
                },
                {
                    "id": "C5",
                    "title": "Direction test",
                    "safety": "The machine homes, then moves Z down 10 mm.",
                    "steps": [
                        {"do": "Please watch the machine home to the bottom-left and jog "
                               "X by 10 mm, then answer its question by what you saw.",
                         "expect": "X moves RIGHT, so the answer is y."},
                        {"do": "Please watch it jog Y by 10 mm, and answer by what you saw.",
                         "expect": "Y moves AWAY from you, so the answer is y."},
                        {"do": "Please watch it jog Z by 10 mm, and answer by what you saw.",
                         "expect": "Z moves DOWN, so the answer is y. No \"Flipped ... in "
                                   "$3\" message."},
                    ],
                },
                {
                    "id": "C6",
                    "title": "Travel: X creeps to the far switch",
                    "safety": "Press Enter there to stop it at once if anything is in the "
                              "way.",
                    "steps": [
                        {"do": there("press Enter at \"About to creep X toward its far "
                                     "switch\", and let it run. It takes several minutes, "
                                     "in 1 mm steps."),
                         "expect": "It re-homes, creeps right and stops by itself at the "
                                   "switch: \"X: stopped by X limit switch at N mm\", with "
                                   "N close to the profile's 622. Then it backs off 5 mm."},
                    ],
                },
                {
                    "id": "C7",
                    "title": "Travel: Y creeps to the far switch",
                    "safety": "Press Enter there to stop it at once if anything is in the "
                              "way.",
                    "steps": [
                        {"do": there("press Enter at the Y prompt, and let it run."),
                         "expect": "\"Y: stopped by Y limit switch at N mm\", with N close "
                                   "to 605. Then it backs off 5 mm."},
                    ],
                },
                {
                    "id": "C8",
                    "title": "Z probe warning",
                    "steps": [
                        {"do": there("read the warning before the Z probe, but don't "
                                     "press Enter yet."),
                         "expect": "Four \"!!\" lines: it creeps DOWNWARD in 1mm steps at "
                                   "300mm/min, it will NOT stop by itself before the "
                                   "spoilboard, watch the tool tip and press Enter, and it "
                                   "also stops if the bottom Z switch triggers first. Then "
                                   "\"Z probe: hand on the keyboard, eyes on the tool "
                                   "tip.\""},
                    ],
                },
                {
                    "id": "C9",
                    "title": "Typed-ahead Enter is ignored",
                    "steps": [
                        {"do": there("press Enter twice, quickly, at the Z probe prompt."),
                         "expect": "It re-homes and then starts creeping down anyway. It "
                                   "does NOT stop at once with \"stopped by operator at "
                                   "0.000mm\"."},
                    ],
                },
                {
                    "id": "C10",
                    "title": "Operator-stopped Z probe",
                    "safety": "It will not stop by itself before the spoilboard. Eyes on "
                              "the tip, hand on Enter and near the E-stop button.",
                    "steps": [
                        {"do": "Please watch the tip, and press Enter in the calibrate.py "
                               "console when it is a few millimetres above the "
                               "spoilboard.",
                         "expect": "It stops within about 1 mm: \"Z: stopped by operator "
                                   "at N mm (usable depth — obstruction below)\", then "
                                   "lifts 5 mm. (This depth becomes workAreaZ and $132: no "
                                   "later Z limit can go below where you stopped.)"},
                    ],
                },
                {
                    "id": "C11",
                    "title": "Corner sweep",
                    "safety": "It homes to all four corners in turn. Please keep the whole "
                              "table clear.",
                    "steps": [
                        {"do": there("press Enter at \"About to home toward top-right\"."),
                         "expect": "\"top-right (Z up): homed OK\". No ALARM."},
                        {"do": there("press Enter at the top-left prompt."),
                         "expect": "\"top-left ...: homed OK\". No ALARM."},
                        {"do": there("press Enter at the bottom-right prompt."),
                         "expect": "\"bottom-right ...: homed OK\". No ALARM."},
                        {"do": there("press Enter at the bottom-left prompt."),
                         "expect": "\"bottom-left ...: homed OK\". No ALARM."},
                    ],
                },
                {
                    "id": "C12",
                    "title": "Measured travel and profile written",
                    "steps": [
                        {"do": there("read the summary."),
                         "expect": "\"=== Measured travel (saved as workArea* and "
                                   "$130-$132) ===\" with one line per axis (X and Y "
                                   "\"stopped by ... limit switch\", Z \"stopped by "
                                   f"operator\"), then \"Profile written to {PROFILE}.\".",
                         "verify": calibratedToday},
                        {"do": "Please look at what changed in the profile. calibrate.py "
                               "is still waiting, so please use a third console, from the "
                               "repository root:",
                         "run": "git diff profiles/",
                         "expect": "Today's calibratedOn, and workArea/$13x values close to "
                                   "the old ones."},
                    ],
                },
                {
                    "id": "C13",
                    "title": "Limits back on afterwards",
                    "steps": [
                        {"do": there("answer n at \"Set a Z soft limit for cutting now?\". "
                                     "The next sections test it."),
                         "expect": "The run ends."},
                        {"do": "In the second console, please read the settings:",
                         "run": READ_SETTINGS,
                         "expect": "$20 and $21 are both '1' again, and $130-$132 are the "
                                   "values just measured."},
                    ],
                },
            ],
        },
        {
            "title": "Z limit (jog)",
            "items": [
                {
                    "id": "ZJ1",
                    "title": "Reset first: already at the calibrated value",
                    "steps": [
                        start(RESET, "\"=== Reset Z limit ===\" and \"$132 is already N, "
                              "the profile's calibrated value.\" (N is the profile's "
                              "expectedSettings $132.) Nothing moves.", "the reset",
                              intro="Now the jog method of the Z-limit step. To test the "
                                    "fence safely, the \"surface\" here is a point about "
                                    "20 mm above the spoilboard, in the air, with "
                                    "thickness 0 and depth 0, so the fence sits where the "
                                    "tip was and nothing can touch."),
                    ],
                },
                {
                    "id": "ZJ2",
                    "title": "Start the jog method",
                    "steps": [
                        start(Z_LIMIT_JOG, "\"=== Z limit ($132) ===\" with no jog-setup "
                              "messages, \"Method: jog (from the command line)\", and "
                              "\"Has the machine been homed ($H) since it was powered "
                              "on?\".", "the jog method"),
                        {"do": HOMED, "expect": "\"About to home the machine.\""},
                        {"do": there("press Enter."),
                         "expect": "It homes, then the prompt \"Jog the bit tip down onto "
                                   "the surface: ... tap Shift+Down to step 0.1 mm, "
                                   "Alt+Shift+Down 0.01 mm ... Press Enter to start "
                                   "jogging.\""},
                    ],
                },
                {
                    "id": "ZJ3",
                    "title": "Esc, then try again",
                    "steps": [
                        {"do": there("press Enter to start jogging, then Esc straight "
                                     "away."),
                         "expect": "\"Jog aborted (Esc).\", then \"Try the measurement "
                                   "again?\". Nothing was written."},
                        {"do": there("answer y."),
                         "expect": "The same jog prompt again."},
                    ],
                },
                {
                    "id": "ZJ4",
                    "title": "Accept a surface in the air",
                    "safety": "Let go of Shift+Down early: 20 mm of air is the point.",
                    "steps": [
                        {"do": "Please read this first, because jog mode captures the "
                               "keyboard. In the calibrate.py console:",
                         "actions": ["Press Enter to start jogging.",
                                     "Hold Shift+Down until the tip is about 20 mm above "
                                     "the spoilboard.",
                                     "Press Enter.",
                                     "Come back here."],
                         "expect": "Z stops where you let go, and it asks what the surface "
                                   "is."},
                        {"do": there("answer workpiece, thickness 0 and depth 0."),
                         "expect": "The summary lists 1 method jog, 2 \"jogged to the "
                                   "surface at z=...\", 3 workpiece, 4 0 mm, 5 0 mm; "
                                   "\"Lowest spindle z = surface z - thickness - "
                                   "into-depth = ...\"; \"New $132 = ... (rounded down to "
                                   "0.001 mm); old $132 = ...\"; and \"For this job only: "
                                   "Grbl's $132 changes, the profile does not.\""},
                    ],
                },
                {
                    "id": "ZJ5",
                    "title": "Change your mind: b and a step number",
                    "steps": [
                        {"do": there("type b, and give the depth (0) again."),
                         "expect": "b asks again for step 5 only. Every other answer is "
                                   "kept, and the summary is shown again."},
                        {"do": there("type 3, and answer workpiece again."),
                         "expect": "3 asks again for the surface kind only. Every other "
                                   "answer is kept, and the summary is shown again."},
                    ],
                },
                {
                    "id": "ZJ6",
                    "title": "A refused limit can't be written",
                    "steps": [
                        {"do": there("type 5, and give a depth of 200."),
                         "expect": "\"REFUSED: $132 would be ..., deeper than the "
                                   "calibrated full Z travel ...\". The prompt has no \"y "
                                   "to write it\"."},
                        {"do": there("type y."),
                         "expect": "It only shows the summary again."},
                        {"do": there("type 5, and give 0 again."),
                         "expect": "Writing is offered again."},
                    ],
                },
                {
                    "id": "ZJ7",
                    "title": "Write the limit",
                    "steps": [
                        {"do": there("type y."),
                         "expect": "\"$132=N written and read back.\" and the three \"!!\" "
                                   "lines about the limit being valid only for the bit "
                                   "fitted now."},
                        {"do": "In the second console, please check the profile:",
                         "run": "git status --short profiles/",
                         "expect": "No new change under profiles/ from this run: a "
                                   "workpiece limit leaves the profile alone."},
                    ],
                },
                {
                    "id": "ZJ8",
                    "title": "The fence holds in jog.py",
                    "safety": "If Z doesn't stop at the height you accepted, let go at "
                              "once.",
                    "steps": [jogStep(
                        ["Hold Shift+Down until it stops by itself.", "Press Enter."],
                        "Z stops by itself in the air, at the height accepted in [[ZJ4]]. No "
                        "error:15, no alarm. The MPos Z is the new $132 as a negative "
                        "number, plus 0.010.")],
                },
            ],
        },
        {
            "title": "Z limit: power cycle and reset",
            "items": [
                {
                    "id": "ZR1",
                    "title": "$132 persists across a power cycle",
                    "steps": [
                        {"do": "The in-the-air limit from [[ZJ7]] is still in force for "
                               "the next few tests.\n\nPlease switch the controller off, "
                               "wait a few seconds, and switch it on again.",
                         "verify": controllerConnected},
                        {"do": "In the second console, please read the settings:",
                         "run": READ_SETTINGS,
                         "expect": "$132 is still the value written in [[ZJ7]]."},
                    ],
                },
                {
                    "id": "ZR2",
                    "title": "Homing works under the shallow limit",
                    "steps": [jogStep(
                        ["Press Esc."],
                        "It homes normally (no ALARM:9). After Esc: \"Jog aborted. MPos: "
                        f"{HOME}\".")],
                },
                {
                    "id": "ZR3",
                    "title": "--reset-z-limit puts the calibrated value back",
                    "steps": [
                        start(RESET, "\"Resetting $132 from <the in-air value> to the "
                              "profile's calibrated N.\" then \"$132=N written and read "
                              "back.\"", "the reset"),
                    ],
                },
                {
                    "id": "ZR4",
                    "title": "Resetting again does nothing",
                    "steps": [
                        start(RESET, "\"$132 is already N, the profile's calibrated "
                              "value.\"", "the reset again"),
                        {"do": "In the second console, please read the settings:",
                         "run": READ_SETTINGS,
                         "expect": "$132 = N."},
                    ],
                },
                {
                    "id": "ZR5",
                    "title": "--plate-thickness saves without connecting",
                    "steps": [
                        {"do": "In the second console, please save a plate thickness:",
                         "run": f"{CALIBRATE} --plate-thickness {PLATE}",
                         "expect": f"\"Probe plate thickness set to {PLATE} mm in "
                                   f"{SIDECAR}.\" It doesn't connect to the machine. (If "
                                   "you know your plate's real thickness, you can set it "
                                   "the same way afterwards.)",
                         "verify": plateThicknessSaved},
                    ],
                },
            ],
        },
        {
            "title": "Z limit (jog, on the spoilboard)",
            "items": [
                {
                    "id": "ZS1",
                    "title": "Touch off on paper, then quit with nothing written",
                    "safety": "Spindle off. Creep the last few millimetres.",
                    "steps": [
                        {"do": "Now touching the real spoilboard with the jog method. The "
                               "fence is now the full travel, so nothing but you stops the "
                               "bit: please creep with Ctrl and tap the last bit.\n\n"
                               "Please put a sheet of paper on the spoilboard, under the "
                               "bit."},
                        start(Z_LIMIT_JOG, "\"Has the machine been homed ($H) since it "
                              "was powered on?\"", "the jog method"),
                        {"do": HOMED + " Then press Enter.",
                         "expect": "It homes, then the jog prompt."},
                        {"do": "Please read this first, because jog mode captures the "
                               "keyboard. In the calibrate.py console:",
                         "actions": ["Press Enter to start jogging.",
                                     "Jog down with Ctrl+Shift+Down.",
                                     "Then tap Shift+Down, and Alt+Shift+Down, until the "
                                     "paper just drags.",
                                     "Press Enter.",
                                     "Come back here."],
                         "expect": "The taps move Z by 0.1 and 0.01 mm. If Z stops by "
                                   "itself (\"At the travel limit\") before the paper "
                                   "drags, the calibrated Z travel from [[C10]] ends above the "
                                   "spoilboard: please fail this with the gap in the note, "
                                   "and expect [[ZS2]] to be refused."},
                        {"do": there("answer workpiece, 0 and 0."),
                         "expect": "The summary shows the surface z."},
                        {"do": there("type q."),
                         "expect": "\"Nothing written.\" $132 is unchanged."},
                    ],
                },
                {
                    "id": "ZS2",
                    "title": "Spoilboard limit by jog (changes the profile)",
                    "safety": "From now on the bit can go 1 mm into the spoilboard.",
                    "steps": [
                        start(Z_LIMIT_JOG, "\"Has the machine been homed ($H) since it "
                              "was powered on?\"", "the jog method again"),
                        {"do": HOMED + " Then press Enter, and touch off on paper as in "
                               "[[ZS1]].",
                         "expect": "It asks what the surface is."},
                        {"do": there("answer spoilboard, and a depth of 1."),
                         "expect": "The summary says \"Kept until the spoilboard is "
                                   "replaced: becomes the profile's calibrated $132\". (If "
                                   "[[ZS1]] failed because the tip couldn't reach the paper, "
                                   "this is REFUSED as deeper than the calibrated full Z "
                                   "travel.)"},
                        {"do": there("type y."),
                         "expect": "\"Profile ...: calibrated $132 is now N\".",
                         "verify": spoilboardRecorded("jog")},
                        start(RESET, "\"$132 is already N, the profile's calibrated "
                              "value.\" (To undo [[ZS2]] later, please restore the profile "
                              "with git and run the reset.)", "the reset"),
                    ],
                },
            ],
        },
        {
            "title": "Z limit (probe)",
            "needs": PLATE_NEEDED,
            "intro": ("Now the two probe methods. They have been tested only with fakes "
                      "so far. Please skip them until a probe plate is wired."),
            "items": [
                {
                    "id": "ZP1",
                    "title": "Already-triggered probe input is refused",
                    "needs": PLATE_NEEDED,
                    "steps": [
                        start(f"{CALIBRATE} --z-limit --method spoilboard",
                              "\"Has the machine been homed ($H) since it was powered "
                              "on?\"", "the spoilboard method"),
                        {"do": HOMED + " Then press Enter, and give the plate thickness "
                               "if it asks.",
                         "expect": "It homes, then \"Jog the bit over the plate spot ... "
                                   "first?\"."},
                        {"do": "Please hold the plate against the bit, and answer n in "
                               "the calibrate.py console.",
                         "expect": "\"!! The probe input is already triggered (P in Pn:) "
                                   "with nothing touching ...\" and \"Try the measurement "
                                   "again?\". Nothing moves."},
                        {"do": "Please take the plate away, and answer y.",
                         "expect": "\"Jog the bit over the plate spot ... first?\" again."},
                    ],
                },
                {
                    "id": "ZP2",
                    "title": "Pin check with the plate",
                    "needs": PLATE_NEEDED,
                    "steps": [
                        {"do": there("answer y to the jog offer, put the bit over the "
                                     "plate spot about 10 mm above it, and press Enter."),
                         "expect": "\"Probe check\"."},
                        {"do": "Please touch the plate to the bit and hold it there, and "
                               "press Enter in the calibrate.py console.",
                         "expect": "\"Probe input triggered, good.\" Nothing moves."},
                        {"do": "Please take the plate away, and press Enter there.",
                         "expect": "No complaint. Nothing moves."},
                    ],
                },
                {
                    "id": "ZP3",
                    "title": "Two-touch probe of the spoilboard",
                    "needs": PLATE_NEEDED,
                    "safety": "If the bit touches the plate and doesn't stop, hit the E-stop "
                              "button at once.",
                    "steps": [
                        {"do": "Please put the plate under the bit, clip on to the bit, "
                               "and press Enter in the calibrate.py console.",
                         "expect": "A \"goes down at most\" line and \"About to probe "
                                   "DOWN\"."},
                        {"do": there("press Enter."),
                         "expect": "Down at 100 mm/min to the plate, up 2 mm, down again "
                                   "slowly (20 mm/min), then up 5 mm. \"Touches at z=... "
                                   "(seek) and z=... (fine). Remove the plate.\" The two z "
                                   "values are close; the fine one is the measurement."},
                    ],
                },
                {
                    "id": "ZP4",
                    "title": "Spoilboard limit from the probe",
                    "needs": PLATE_NEEDED,
                    "steps": [
                        {"do": there("give a depth of 1, and read the summary."),
                         "expect": "Step 2 reads \"probe touched at z=..., plate T mm -> "
                                   "surface z=...\", with surface z = touch z - T."},
                        {"do": there("type y."),
                         "expect": "\"Profile ...: calibrated $132 is now N\".",
                         "verify": spoilboardRecorded("probe")},
                        {"do": "In the second console, please look at the profile and "
                               "sidecar:",
                         "run": "git diff profiles/",
                         "expect": "The profile's expectedSettings $132 changes, and "
                                   f"{SIDECAR} has a spoilboard record with probeContactZ "
                                   "and plateThickness."},
                    ],
                },
                {
                    "id": "ZP5",
                    "title": "Plate thickness isn't asked twice; workpiece method",
                    "needs": PLATE_NEEDED + " A workpiece of known thickness on the table.",
                    "steps": [
                        start(f"{CALIBRATE} --z-limit --method workpiece --thickness T",
                              "\"Has the machine been homed ($H) since it was powered "
                              "on?\" (T is the workpiece thickness.)", "the workpiece method"),
                        {"do": HOMED + " Then press Enter.",
                         "expect": "It homes, then \"Probe plate thickness: T mm (saved; "
                                   "change it with --plate-thickness).\" instead of a "
                                   "question."},
                        {"do": "Please probe the workpiece top as in [[ZP2]] and [[ZP3]], and give "
                               "a depth of 0.5.",
                         "expect": "The summary says \"surface z - thickness - "
                                   "into-depth\" and \"For this job only\"."},
                        {"do": there("type y."),
                         "expect": "$132 is written, and the profile is unchanged by this "
                                   "run."},
                    ],
                },
                {
                    "id": "ZP6",
                    "title": "No contact under a shallow limit",
                    "needs": PLATE_NEEDED + " An in-the-air limit in force (repeat [[ZJ2]] to [[ZJ7]]).",
                    "steps": [
                        {"do": "With the in-the-air limit in force, please start the "
                               "spoilboard method in the second console, and probe with "
                               "the plate on the spoilboard, below the fence:",
                         "run": f"{CALIBRATE} --z-limit --method spoilboard",
                         "expect": "The probe goes down only to just above the fence and "
                                   "finds nothing: \"Probe failed (ALARM:5: no contact "
                                   "within ...). Unlocked with $X ...\", \"Returning N mm "
                                   "up to the starting height.\" and \"Try the "
                                   "measurement again?\"."},
                        {"do": there("answer n."),
                         "expect": "The method question comes back, with no quit choice."},
                        {"do": there("press Ctrl-C to leave, then run the reset:"),
                         "run": RESET,
                         "expect": "calibrate.py exits, and the reset puts the calibrated "
                                   "$132 back."},
                    ],
                },
            ],
        },
    ],
}

if __name__ == "__main__":
    qaRunner.main(CHECKLIST)
