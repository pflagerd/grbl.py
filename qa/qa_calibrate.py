#!/usr/bin/env python3
"""Manual QA for calibrate.py on the real machine (issue #13, step 13).

    python qa/qa_calibrate.py            # or: python3 qa/qa_calibrate.py

Run this in one console and calibrate.py in another, from the repository root
with the venv active.  The checks cover the full calibration (press test,
direction test, travel creep probe including the operator-stopped Z probe
from PR #27, corner sweep, profile written) and the Z-limit step from PR #32
(Step 9).  The probe-method checks need a probe plate wired to the
controller's probe input; skip that section as a whole until it is.  The
checklist is plain data; qaRunner.py does the asking and recording.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import qaRunner  # noqa: E402

MACHINE = "6060 ProverXL V1"
PROFILE = f"profiles/{MACHINE}.json"
SIDECAR = f"profiles/{MACHINE}.zlimit.json"
CALIBRATE = f'python calibrate.py "{MACHINE}"'
Z_LIMIT_JOG = f"{CALIBRATE} --z-limit --method jog"
RESET = f"{CALIBRATE} --reset-z-limit"
# Reads the settings that matter here.  Like every program in this repo it
# connects through GrblController, which also writes $23=3 and $120-$122.
READ_SETTINGS = ('python -c \'from GrblController import GrblController; '
                 's = GrblController().getSettings(); '
                 'print({k: s[k] for k in ("$20", "$21", "$130", "$131", "$132")})\'')
PLATE_NEEDED = "Probe plate wired to the controller's probe input."
HOMED_ANSWER = ("At \"Has the machine been homed ($H) since it was powered on?\" answer n "
                "and press Enter at \"About to home the machine.\": connecting resets this "
                "controller, so it is never homed at that point.")

CHECKLIST = {
    "name": "calibrate",
    "title": "calibrate.py manual hardware checks",
    "machine": MACHINE,
    "intro": (
        "Run calibrate.py in the other console, from the repository root with the venv "
        "active.\n"
        f"The full calibration rewrites {PROFILE}. It is tracked by git, so "
        "git diff profiles/ shows what a run changed.\n"
        "Spindle off throughout, no clamps or stock on the table, and a hand near reset "
        "or power at all times. The Z checks need the bit you will cut with, or one of "
        "the same stick-out."
    ),
    "sections": [
        {
            "title": "Before you start",
            "items": [
                {
                    "id": "P1",
                    "title": "Clean profile and starting settings",
                    "do": "Check that git shows no changes under profiles/, then read the "
                          "current settings.",
                    "command": f"git status --short profiles/   and then   {READ_SETTINGS}",
                    "expect": "git prints nothing. The settings read {'$20': '1', '$21': "
                              "'1', '$130': '622.000', '$131': '605.000', '$132': "
                              "'109.000'}, matching the profile. Note anything different.",
                },
            ],
        },
        {
            "title": "Full calibration",
            "intro": ("One run of the full calibration; the items follow its prompts in "
                      f"order. Start it with: {CALIBRATE}"),
            "items": [
                {
                    "id": "C1",
                    "title": "Connect",
                    "do": "Start the calibration and read what it prints.",
                    "command": CALIBRATE,
                    "expect": "\"Connecting to /dev/ttyUSB0 @ 115200...\", \"Connected. Grbl "
                              "1.1h, N settings read.\", \"Keep a hand near reset/power at "
                              "all times.\" and a prompt to clear the work area. Press "
                              "Enter there.",
                },
                {
                    "id": "C2",
                    "title": "Press test: X switches",
                    "do": "When asked, press and hold the X switch at the MIN end and "
                          "press Enter; release it and press Enter. Then the same at the "
                          "MAX end.",
                    "expect": "\"=== Switch press test (no motion) ===\". For each end, "
                              "\"X MIN (left/front/bottom): triggered, good.\" (or MAX ...), "
                              "and no \"stayed triggered\" after release. Nothing moves.",
                },
                {
                    "id": "C3",
                    "title": "Press test: Y switches",
                    "do": "The same for both Y switches.",
                    "expect": "\"Y MIN ...: triggered, good.\" and \"Y MAX ...: triggered, "
                              "good.\" Nothing moves.",
                },
                {
                    "id": "C4",
                    "title": "Press test: Z switches",
                    "do": "The same for both Z switches.",
                    "expect": "\"Z MIN ...: triggered, good.\" and \"Z MAX ...: triggered, "
                              "good.\" No question about continuing despite failures.",
                },
                {
                    "id": "C5",
                    "title": "Direction test",
                    "do": "It homes bottom-left, then jogs each axis 10 mm. Answer each "
                          "question by what you see.",
                    "safety": "The machine homes, then moves Z down 10 mm.",
                    "expect": "X moves RIGHT, Y moves AWAY from you, Z moves DOWN, so every "
                              "answer is y and no \"Flipped ... in $3\" message appears.",
                },
                {
                    "id": "C6",
                    "title": "Travel: X creeps to the far switch",
                    "do": "Press Enter at \"About to creep X toward its far switch\". Let it "
                          "run (several minutes, in 1 mm steps).",
                    "safety": "Press Enter to stop it at once if anything is in the way.",
                    "expect": "It re-homes, creeps right and stops by itself at the switch: "
                              "\"X: stopped by X limit switch at N mm\", with N close to the "
                              "profile's 622. It then backs off 5 mm.",
                },
                {
                    "id": "C7",
                    "title": "Travel: Y creeps to the far switch",
                    "do": "Press Enter at the Y prompt and let it run.",
                    "safety": "Press Enter to stop it at once if anything is in the way.",
                    "expect": "\"Y: stopped by Y limit switch at N mm\", N close to 605. It "
                              "backs off 5 mm.",
                },
                {
                    "id": "C8",
                    "title": "Z probe warning",
                    "do": "Read the warning before the Z probe. Don't press Enter yet.",
                    "expect": "Four \"!!\" lines: it creeps DOWNWARD in 1mm steps at "
                              "300mm/min, it will NOT stop by itself before the spoilboard, "
                              "watch the tool tip and press Enter, and it also stops if the "
                              "bottom Z switch triggers first. Then \"Z probe: hand on the "
                              "keyboard, eyes on the tool tip.\"",
                },
                {
                    "id": "C9",
                    "title": "Typed-ahead Enter is ignored",
                    "do": "At the Z probe prompt, press Enter twice in quick succession.",
                    "expect": "It re-homes and then starts creeping down anyway. It does "
                              "NOT stop at once with \"stopped by operator at 0.000mm\".",
                },
                {
                    "id": "C10",
                    "title": "Operator-stopped Z probe",
                    "do": "Watch the tip. Press Enter when it is a few millimetres above "
                          "the spoilboard.",
                    "safety": "It will not stop by itself before the spoilboard. Eyes on "
                              "the tip, hand on Enter and near reset.",
                    "expect": "It stops within about 1 mm after Enter: \"Z: stopped by "
                              "operator at N mm (usable depth — obstruction below)\". Then "
                              "it lifts 5 mm. (This depth becomes workAreaZ and $132: no "
                              "later Z limit can go below the point where you stopped.)",
                },
                {
                    "id": "C11",
                    "title": "Corner sweep",
                    "do": "Press Enter at each \"About to home toward ...\" prompt.",
                    "safety": "It homes to all four corners in turn. Keep the whole table "
                              "clear.",
                    "expect": "\"top-right (Z up): homed OK\", then top-left, bottom-right "
                              "and bottom-left, each \"homed OK\". No ALARM.",
                },
                {
                    "id": "C12",
                    "title": "Measured travel and profile written",
                    "do": "Read the summary, then look at what changed in the profile.",
                    "command": "git diff profiles/",
                    "expect": "\"=== Measured travel (saved as workArea* and $130-$132) ===\" "
                              "with one line per axis (X and Y \"stopped by ... limit "
                              "switch\", Z \"stopped by operator\"), then \"Profile written "
                              f"to {PROFILE}.\". The diff shows today's calibratedOn and "
                              "workArea/$13x values close to the old ones.",
                },
                {
                    "id": "C13",
                    "title": "Limits back on afterwards",
                    "do": "Answer n at \"Set a Z soft limit for cutting now?\" (the next "
                          "sections test it). Then read the settings.",
                    "command": READ_SETTINGS,
                    "expect": "The run ends. $20 and $21 are both '1' again, and $130-$132 "
                              "are the values just measured.",
                },
            ],
        },
        {
            "title": "Z limit (jog)",
            "intro": ("The jog method of the Z-limit step. To test the fence safely, the "
                      "\"surface\" here is a point about 20 mm above the spoilboard, in "
                      "the air, with thickness 0 and depth 0, so the fence sits where the "
                      "tip was and nothing can touch."),
            "items": [
                {
                    "id": "ZJ1",
                    "title": "Reset first: already at the calibrated value",
                    "do": "Run the reset.",
                    "command": RESET,
                    "expect": "\"=== Reset Z limit ===\" and \"$132 is already N, the "
                              "profile's calibrated value.\" (N is the profile's "
                              "expectedSettings $132.) Nothing moves.",
                },
                {
                    "id": "ZJ2",
                    "title": "Start the jog method",
                    "do": f"Start it. {HOMED_ANSWER}",
                    "command": Z_LIMIT_JOG,
                    "safety": "The machine homes.",
                    "expect": "\"=== Z limit ($132) ===\" with no jog-setup messages, "
                              "\"Method: jog (from the command line)\", and the prompt "
                              "\"Jog the bit tip down onto the surface: ... tap Shift+Down "
                              "to step 0.1 mm, Alt+Shift+Down 0.01 mm ... Press Enter to "
                              "start jogging.\"",
                },
                {
                    "id": "ZJ3",
                    "title": "Esc, then try again",
                    "do": "Press Enter to start jogging, then Esc straight away. Answer y "
                          "at \"Try the measurement again?\".",
                    "expect": "\"Jog aborted (Esc).\", then the question, then the same jog "
                              "prompt again. Nothing was written.",
                },
                {
                    "id": "ZJ4",
                    "title": "Accept a surface in the air",
                    "do": "Start jogging and hold Shift+Down until the tip is about 20 mm "
                          "above the spoilboard. Press Enter. Answer workpiece, thickness "
                          "0 and depth 0.",
                    "safety": "Release Shift+Down early; 20 mm of air is the point.",
                    "expect": "The summary lists 1 method jog, 2 \"jogged to the surface at "
                              "z=...\", 3 workpiece, 4 0 mm, 5 0 mm; \"Lowest spindle z = "
                              "surface z - thickness - into-depth = ...\"; \"New $132 = ... "
                              "(rounded down to 0.001 mm); old $132 = ...\"; \"For this job "
                              "only: Grbl's $132 changes, the profile does not.\"",
                },
                {
                    "id": "ZJ5",
                    "title": "Change your mind: b and a step number",
                    "do": "Type b and give the depth (0) again. Then type 3 and answer "
                          "workpiece again.",
                    "expect": "b asks again for step 5 only; 3 asks again for the surface "
                              "kind only. Every other answer is kept, and the summary is "
                              "shown again.",
                },
                {
                    "id": "ZJ6",
                    "title": "A refused limit can't be written",
                    "do": "Type 5 and give a depth of 200. Then try y. Then type 5 and give "
                          "0 again.",
                    "expect": "\"REFUSED: $132 would be ..., deeper than the calibrated full "
                              "Z travel ...\". The prompt has no \"y to write it\", and y "
                              "only shows the summary again. With 0 it is offered again.",
                },
                {
                    "id": "ZJ7",
                    "title": "Write the limit",
                    "do": "Type y. Then check the profile.",
                    "command": "git status --short profiles/",
                    "expect": "\"$132=N written and read back.\" and the three \"!!\" lines "
                              "about the limit being valid only for the bit fitted now. A "
                              "workpiece limit leaves the profile alone (no new change "
                              "under profiles/ from this run).",
                },
                {
                    "id": "ZJ8",
                    "title": "The fence holds in jog.py",
                    "do": "Run jog.py, hold Shift+Down until it stops by itself, then press "
                          "Enter.",
                    "command": "python jog.py --home",
                    "safety": "If Z doesn't stop at the height you accepted, let go at "
                              "once.",
                    "expect": "Z stops by itself in the air at the height accepted in ZJ4. "
                              "No error:15, no alarm. The MPos Z is the new $132 as a "
                              "negative number plus 0.010.",
                },
            ],
        },
        {
            "title": "Z limit: power cycle and reset",
            "intro": "Still with the in-the-air limit from the previous section in force.",
            "items": [
                {
                    "id": "ZR1",
                    "title": "$132 persists across a power cycle",
                    "do": "Switch the controller off, wait a few seconds, switch it on. "
                          "Read the settings.",
                    "command": READ_SETTINGS,
                    "expect": "$132 is still the value written in ZJ7.",
                },
                {
                    "id": "ZR2",
                    "title": "Homing works under the shallow limit",
                    "do": "Run jog.py and let it home, then press Esc.",
                    "command": "python jog.py --home",
                    "expect": "It homes normally (no ALARM:9). Esc prints \"Jog aborted. "
                              "MPos: X-619.000 Y-602.000 Z-3.000\".",
                },
                {
                    "id": "ZR3",
                    "title": "--reset-z-limit puts the calibrated value back",
                    "do": "Run the reset.",
                    "command": RESET,
                    "expect": "\"Resetting $132 from <the in-air value> to the profile's "
                              "calibrated N.\" then \"$132=N written and read back.\"",
                },
                {
                    "id": "ZR4",
                    "title": "Resetting again does nothing",
                    "do": "Run the reset a second time, then read the settings.",
                    "command": f"{RESET}   and then   {READ_SETTINGS}",
                    "expect": "\"$132 is already N, the profile's calibrated value.\" The "
                              "settings show $132 = N.",
                },
                {
                    "id": "ZR5",
                    "title": "--plate-thickness saves without connecting",
                    "do": "Set the saved plate thickness (use your plate's real thickness "
                          "if you know it).",
                    "command": f"{CALIBRATE} --plate-thickness 10",
                    "expect": f"\"Probe plate thickness set to 10 mm in {SIDECAR}.\" It "
                              "doesn't connect to the machine.",
                },
            ],
        },
        {
            "title": "Z limit (jog, on the spoilboard)",
            "intro": ("Touching the real spoilboard with the jog method. The fence is now "
                      "the full travel, so nothing but you stops the bit: creep with Ctrl "
                      "and tap the last bit."),
            "items": [
                {
                    "id": "ZS1",
                    "title": "Touch off on paper, then quit with nothing written",
                    "do": f"Start the jog method ({HOMED_ANSWER}) Put a sheet of paper on "
                          "the spoilboard under the bit. Jog down with Ctrl+Shift+Down, "
                          "then Shift+Down and Alt+Shift+Down taps, until the paper just "
                          "drags. Press Enter. Answer workpiece, 0, 0. At the summary type "
                          "q.",
                    "command": Z_LIMIT_JOG,
                    "safety": "Spindle off. Creep the last few millimetres.",
                    "expect": "The taps move Z by 0.1 and 0.01 mm. The summary shows the "
                              "surface z. q prints \"Nothing written.\" and $132 is "
                              "unchanged. If Z stops by itself (\"At the travel limit\") "
                              "before the paper drags, the calibrated Z travel from C10 ends "
                              "above the spoilboard: fail this with the gap in the note, and "
                              "expect ZS2 to be refused.",
                },
                {
                    "id": "ZS2",
                    "title": "Spoilboard limit by jog (changes the profile)",
                    "do": "Run the jog method again, touch off on paper as before, answer "
                          "spoilboard and a depth of 1, and type y. Then look at the "
                          "profile.",
                    "command": f"{Z_LIMIT_JOG}   and then   git diff profiles/",
                    "safety": "From now on the bit can go 1 mm into the spoilboard.",
                    "expect": "The summary says \"Kept until the spoilboard is replaced: "
                              "becomes the profile's calibrated $132\". After y: \"Profile "
                              "...: calibrated $132 is now N\". The diff shows "
                              f"expectedSettings $132 = N, and {SIDECAR} has a spoilboard "
                              "record with measuredBy jog. A reset now says $132 is already "
                              "N. (If ZS1 failed because the tip couldn't reach the paper, "
                              "this is REFUSED as deeper than the calibrated full Z travel.) "
                              "To undo: restore the profile with git and run the reset.",
                },
            ],
        },
        {
            "title": "Z limit (probe)",
            "needs": PLATE_NEEDED,
            "intro": ("The two probe methods. They were tested only with fakes. Skip this "
                      "whole section until a probe plate is wired."),
            "items": [
                {
                    "id": "ZP1",
                    "title": "Already-triggered probe input is refused",
                    "needs": PLATE_NEEDED,
                    "do": f"Start the spoilboard method ({HOMED_ANSWER}) Give the plate "
                          "thickness if asked. Hold the plate against the bit while you "
                          "answer n at \"Jog the bit over the plate spot ... first?\".",
                    "command": f"{CALIBRATE} --z-limit --method spoilboard",
                    "expect": "\"!! The probe input is already triggered (P in Pn:) with "
                              "nothing touching ...\" and \"Try the measurement again?\". "
                              "Nothing moves. Answer y and take the plate away.",
                },
                {
                    "id": "ZP2",
                    "title": "Pin check with the plate",
                    "needs": PLATE_NEEDED,
                    "do": "Answer y to the jog offer and put the bit over the plate spot, "
                          "about 10 mm above it; Enter. Then follow \"Probe check\": touch "
                          "the plate to the bit and hold it, Enter; take it away, Enter.",
                    "expect": "\"Probe input triggered, good.\" and no complaint after you "
                              "take it away. Nothing moves during the check.",
                },
                {
                    "id": "ZP3",
                    "title": "Two-touch probe of the spoilboard",
                    "needs": PLATE_NEEDED,
                    "do": "Put the plate under the bit, clip on the bit; Enter. Read the "
                          "\"goes down at most\" line, then Enter at \"About to probe "
                          "DOWN\".",
                    "safety": "If the bit touches the plate and doesn't stop, hit reset at "
                              "once.",
                    "expect": "Down at 100 mm/min to the plate, up 2 mm, down again slowly "
                              "(20 mm/min), then up 5 mm. \"Touches at z=... (seek) and "
                              "z=... (fine). Remove the plate.\" The two z values are close; "
                              "the fine one is the measurement.",
                },
                {
                    "id": "ZP4",
                    "title": "Spoilboard limit from the probe",
                    "needs": PLATE_NEEDED,
                    "do": "Give a depth of 1, check the summary, type y. Look at the "
                          "profile and sidecar.",
                    "command": "git diff profiles/",
                    "expect": "Summary step 2 reads \"probe touched at z=..., plate T mm -> "
                              "surface z=...\" with surface z = touch z - T. After y the "
                              f"profile's expectedSettings $132 changes and {SIDECAR} has a "
                              "spoilboard record with measuredBy probe, probeContactZ and "
                              "plateThickness.",
                },
                {
                    "id": "ZP5",
                    "title": "Plate thickness isn't asked twice; workpiece method",
                    "needs": PLATE_NEEDED + " A workpiece of known thickness on the table.",
                    "do": f"Run the workpiece method ({HOMED_ANSWER}) Probe the workpiece "
                          "top as before, give a depth of 0.5 and type y.",
                    "command": f"{CALIBRATE} --z-limit --method workpiece --thickness T",
                    "expect": "\"Probe plate thickness: T mm (saved; change it with "
                              "--plate-thickness).\" instead of a question. The summary "
                              "says \"surface z - thickness - into-depth\" and \"For this "
                              "job only\". y writes $132; the profile is unchanged by this "
                              "run.",
                },
                {
                    "id": "ZP6",
                    "title": "No contact under a shallow limit",
                    "needs": PLATE_NEEDED + " An in-the-air limit in force (repeat ZJ2-ZJ7).",
                    "do": "With the in-the-air limit in force, run the spoilboard method "
                          "and probe with the plate on the spoilboard, below the fence.",
                    "command": f"{CALIBRATE} --z-limit --method spoilboard",
                    "expect": "The probe goes down only to just above the fence and finds "
                              "nothing: \"Probe failed (ALARM:5: no contact within ...). "
                              "Unlocked with $X ...\", \"Returning N mm up to the starting "
                              "height.\" and \"Try the measurement again?\". Answer n: the "
                              "method question comes back, and it has no quit choice, so "
                              "leave with Ctrl-C. Then run the reset.",
                },
            ],
        },
    ],
}

if __name__ == "__main__":
    qaRunner.main(CHECKLIST)
