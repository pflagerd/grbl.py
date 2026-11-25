#!/usr/bin/env python3
"""Manual QA for jog.py on the real machine (issue #13, step 13).

    python qa/qa_jog.py            # or: python3 qa/qa_jog.py

Run this in one console and jog.py in another, from the repository root with
the venv active.  The checks come from PR #31 (Step 8, hold-to-jog) and PR #33
(Step 10, tap-to-step, and Esc cancelling queued steps).  The checklist is
plain data; qaRunner.py does the asking and recording.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import qaRunner  # noqa: E402

JOG = 'python jog.py --home'
HOME = "X-619.000 Y-602.000 Z-3.000"

CHECKLIST = {
    "name": "jog",
    "title": "jog.py manual hardware checks",
    "machine": "6060 ProverXL V1",
    "intro": (
        "Run jog.py in the other console, from the repository root with the venv active.\n"
        "Opening the serial port resets this controller, so every jog.py run starts "
        "unhomed: always use --home (the machine homes at the start of every run).\n"
        "jog.py prints the machine position once, when you leave jog mode: "
        "\"Jog accepted. MPos: ...\" after Enter, \"Jog aborted. MPos: ...\" after Esc. "
        f"Homed, the 6060 is at {HOME} ($130=622, $131=605, $27=3).\n"
        "While jog mode is on, the keyboard is captured, so answer here after you leave it.\n"
        "Spindle off throughout. Keep the bit well clear of the spoilboard and a hand "
        "near reset."
    ),
    "sections": [
        {
            "title": "Setup",
            "intro": ("The start-up check for evdev and the 'input' group. Do these in "
                      "order. If evdev is already installed and you are already in the "
                      "'input' group, skip the section."),
            "items": [
                {
                    "id": "S1",
                    "title": "Starting point: no evdev, not in the input group",
                    "do": "Check that evdev is missing from the venv and that 'input' is "
                          "not among your groups.",
                    "command": 'python -c "import evdev"   and then   id -nG',
                    "expect": "ModuleNotFoundError for evdev, and no 'input' in the group "
                              "list. (If either is already in place, skip the items that "
                              "need it missing.)",
                },
                {
                    "id": "S2",
                    "title": "Declining both offers changes nothing",
                    "do": "Run jog.py and answer N (or just Enter) at both \"Run it now? "
                          "[y/N]\" prompts.",
                    "command": JOG,
                    "expect": "It says keyboard jog needs evdev and shows the exact "
                              "command (this venv's python -m pip install evdev), then "
                              "says it needs the 'input' group and shows "
                              "\"sudo usermod -aG input $USER\" and \"then log out and "
                              "back in.\". It exits with \"Keyboard jog: jog "
                              "unavailable.\" Nothing is installed, the serial port is "
                              "not opened, and nothing moves.",
                },
                {
                    "id": "S3",
                    "title": "Accepting the evdev install",
                    "do": "Run jog.py again. Answer y at the evdev offer, then N at the "
                          "input-group offer.",
                    "command": JOG,
                    "expect": "\"Running: .../python -m pip install evdev\", pip's own "
                              "output, then \"Exit status 0.\". It exits with \"Keyboard "
                              "jog: jog unavailable.\" (because of the group). Afterwards "
                              "python -c \"import evdev\" works. If pip fails (evdev "
                              "builds from source and needs a C compiler and kernel "
                              "headers), fail this item and paste the error.",
                },
                {
                    "id": "S4",
                    "title": "Accepting the input-group fix",
                    "do": "Run jog.py again and answer y at the input-group offer. Type "
                          "your password if sudo asks.",
                    "command": JOG,
                    "expect": "No evdev message this time. sudo's password prompt "
                              "appears in that console (jog.py never sees the password), "
                              "then \"Exit status 0.\" and \"Done. The group membership "
                              "takes effect only after you log out and back in; keyboard "
                              "jog is unavailable until then.\". It exits with "
                              "\"Keyboard jog: jog unavailable until re-login.\"",
                },
                {
                    "id": "S5",
                    "title": "The log-out reminder before logging out",
                    "do": "Without logging out, run jog.py once more.",
                    "command": JOG,
                    "expect": "One line: \"You are in the 'input' group, but only from your "
                              "next login: log out and back in to use keyboard jog.\" No "
                              "offer to run usermod again. It exits with \"Keyboard jog: "
                              "jog unavailable until re-login.\"",
                },
                {
                    "id": "S6",
                    "title": "Silent once everything is in place",
                    "do": "Log out and back in. (This session is saved: run qa/qa_jog.py "
                          "again and resume.) Run jog.py, wait for the jog-mode banner, "
                          "then press Esc.",
                    "command": JOG,
                    "safety": "The machine homes.",
                    "expect": "No setup messages and no questions: it connects, homes and "
                              f"shows the banner. Esc prints \"Jog aborted. MPos: {HOME}\".",
                },
            ],
        },
        {
            "title": "Hold-to-jog",
            "intro": f"Each item is one run of: {JOG}. Leave jog mode with Enter (or Esc) "
                     "and read the MPos line it prints.",
            "items": [
                {
                    "id": "H1",
                    "title": "The banner",
                    "do": "Run jog.py and read the two lines it prints before jog mode.",
                    "command": JOG,
                    "expect": "\"Jog mode: arrows X/Y, Shift+Up/Down Z. Tap to step 0.1 mm "
                              "(Alt+tap 0.01 mm), hold to jog; hold Ctrl to creep (1/10 "
                              "speed).\" and \"Enter accepts (after any queued steps "
                              "finish), Esc aborts at once. The keyboard is captured until "
                              "then.\"",
                },
                {
                    "id": "H2",
                    "title": "\"At the travel limit\" from home",
                    "do": "Straight after homing, hold Left for a second, then Down, then "
                          "Shift+Up (each one toward a home switch). Press Enter.",
                    "command": JOG,
                    "expect": "Nothing moves. Each prints \"At the travel limit in that "
                              "direction; not moving.\" No alarm. Enter prints "
                              f"\"Jog accepted. MPos: {HOME}\".",
                },
                {
                    "id": "H3",
                    "title": "Left and Right move X",
                    "do": "Hold Right for about 2 s and release. Then hold Left for about "
                          "1 s and release.",
                    "command": JOG,
                    "expect": "Right moves the tool right (+X) at 1000 mm/min, Left moves "
                              "it back left. Each time it decelerates to a stop when you "
                              "let go.",
                },
                {
                    "id": "H4",
                    "title": "Up and Down move Y",
                    "do": "Hold Up for about 2 s and release. Then hold Down for about 1 s "
                          "and release.",
                    "command": JOG,
                    "expect": "Up moves the tool away from you (+Y), Down toward you. Each "
                              "stops on release.",
                },
                {
                    "id": "H5",
                    "title": "Shift+Up and Shift+Down move Z",
                    "do": "Hold Shift+Down for about 2 s and release. Then hold Shift+Up "
                          "for about 1 s and release.",
                    "command": JOG,
                    "safety": "Z goes down at 300 mm/min. Release early if the bit gets "
                              "anywhere near the spoilboard.",
                    "expect": "Shift+Down lowers the spindle, Shift+Up raises it. Each "
                              "stops on release.",
                },
                {
                    "id": "H6",
                    "title": "Diagonal, then back to one axis",
                    "do": "Hold Right, then also hold Up. After a second, let go of Up. "
                          "Then let go of Right.",
                    "command": JOG,
                    "expect": "Right alone moves X. Adding Up gives one smooth 45-degree "
                              "diagonal. Letting go of Up goes back to X only. Letting go "
                              "of Right stops.",
                },
                {
                    "id": "H7",
                    "title": "Opposite keys cancel out",
                    "do": "Hold Right, then also hold Left. Then let go of Left.",
                    "command": JOG,
                    "expect": "With both held, X stops. Letting go of Left starts the "
                              "Right jog again.",
                },
                {
                    "id": "H8",
                    "title": "Ctrl creeps at a tenth of the speed",
                    "do": "Hold Ctrl+Right for a few seconds. Then, holding Right only, "
                          "press and release Ctrl.",
                    "command": JOG,
                    "expect": "Ctrl+Right creeps at 100 mm/min. Pressing Ctrl mid-jog "
                              "drops to the creep speed; releasing it goes back to full "
                              "speed.",
                },
                {
                    "id": "H9",
                    "title": "Coast distance after release",
                    "do": "Hold Right at full speed for about 3 s, then release. Watch how "
                          "far it keeps going after you let go.",
                    "command": JOG,
                    "expect": "It slows to a stop smoothly. Expected with $120=10 mm/s^2: "
                              "about 14 mm over about 1.7 s at 1000 mm/min (about 1.25 mm "
                              "for Z at 300). Record what you saw in the note, and whether "
                              "it feels too sluggish (Step 11 raises it to 30 mm/s^2).",
                },
                {
                    "id": "H10",
                    "title": "Stopping short at the far edge (X)",
                    "do": "From home, hold Right until it stops by itself (about 40 s). "
                          "Then press Enter.",
                    "command": JOG,
                    "safety": "It drives toward the right-hand switch. Hand near reset; "
                              "let go if it doesn't slow down before the switch.",
                    "expect": "It stops by itself short of the switch: no error:15, no "
                              "ALARM, no traceback. Enter prints X-0.010 (0.01 mm inside "
                              "the soft limit).",
                },
                {
                    "id": "H11",
                    "title": "A diagonal slides along an edge",
                    "do": "From home, hold Right and Up together until everything stops "
                          "(about a minute). Then press Enter.",
                    "command": JOG,
                    "safety": "It drives toward the back and right-hand switches. Hand "
                              "near reset.",
                    "expect": "A diagonal until Y reaches the back edge, then it carries on "
                              "along that edge in X only, then stops. No error:15, no "
                              "alarm. Enter prints X-0.010 Y-0.010.",
                },
            ],
        },
        {
            "title": "Taps",
            "intro": (f"Each item is one run of: {JOG}. A tap is an arrow released within "
                      "250 ms. Read the MPos line after Enter or Esc. With 160 steps/mm, "
                      "positions move in whole motor steps of 0.00625 mm."),
            "items": [
                {
                    "id": "T1",
                    "title": "One tap moves 0.100",
                    "do": "Tap Right once (a quick press and release), then press Enter.",
                    "command": JOG,
                    "expect": "A tiny move. \"Jog accepted. MPos: X-618.900 Y-602.000 "
                              "Z-3.000\".",
                },
                {
                    "id": "T2",
                    "title": "Taps on every axis and direction",
                    "do": "Tap Right twice, Left once, Up twice, Down once, Shift+Down "
                          "twice, Shift+Up once. Press Enter.",
                    "command": JOG,
                    "expect": "\"MPos: X-618.900 Y-601.900 Z-3.100\".",
                },
                {
                    "id": "T3",
                    "title": "Ctrl+tap is also 0.100",
                    "do": "Ctrl+tap Right once. Press Enter.",
                    "command": JOG,
                    "expect": "\"MPos: X-618.900 ...\".",
                },
                {
                    "id": "T4",
                    "title": "Ten Alt+taps total 0.100",
                    "do": "Hold the left Alt and tap Right ten times. Press Enter.",
                    "command": JOG,
                    "expect": "\"MPos: X-618.900 ...\" (each tap is 1 or 2 motor steps, "
                              "0.006 or 0.013 mm). The desktop doesn't react to Alt: no "
                              "menu opens.",
                },
                {
                    "id": "T5",
                    "title": "Ten AltGr+taps total 0.100",
                    "do": "Hold AltGr (the right Alt) and tap Right ten times. Press Enter.",
                    "command": JOG,
                    "expect": "\"MPos: X-618.900 ...\". No special characters or menus "
                              "appear anywhere.",
                },
                {
                    "id": "T6",
                    "title": "Ten Alt+Shift+Down taps lower Z by 0.100",
                    "do": "Hold Alt and Shift and tap Down ten times. Press Enter.",
                    "command": JOG,
                    "expect": "\"MPos: ... Z-3.100\". Alt+Shift doesn't switch the keyboard "
                              "layout.",
                },
                {
                    "id": "T7",
                    "title": "Two keys tapped together give one diagonal step",
                    "do": "Press Right and Up together and release both quickly. Press "
                          "Enter.",
                    "command": JOG,
                    "expect": "\"MPos: X-618.900 Y-601.900 ...\".",
                },
                {
                    "id": "T8",
                    "title": "The hold threshold pause",
                    "do": "Hold Right and watch when it starts. Release.",
                    "command": JOG,
                    "expect": "About a quarter of a second passes before the continuous "
                              "jog starts; a tap moves nothing until the key comes up.",
                },
                {
                    "id": "T9",
                    "title": "Trying other hold thresholds",
                    "do": "Try taps and holds with --hold-threshold 0.2 and then 0.3. "
                          "Note which feels right.",
                    "command": "python jog.py --home --hold-threshold 0.2   (then 0.3)",
                    "expect": "Shorter or longer pause before a hold starts; taps still "
                              "step 0.1. Put your preferred value in the note.",
                },
                {
                    "id": "T10",
                    "title": "Ten quick taps total 1.000",
                    "do": "Tap Right ten times as fast as you can, wait until it stops, "
                          "then press Enter.",
                    "command": JOG,
                    "expect": "Smooth, with no stutter. \"MPos: X-618.000 ...\".",
                },
                {
                    "id": "T11",
                    "title": "A step is shortened at a bound",
                    "do": "Tap Right once (X-618.900), then tap Left twice. Press Enter.",
                    "command": JOG,
                    "expect": "The first Left tap moves only about 0.09 mm, stopping just "
                              "inside the home end; X ends at about -618.99 (to within a "
                              "motor step), never -619.000 or beyond. The second Left tap "
                              "moves nothing visible. No alarm, and the home switch isn't "
                              "touched.",
                },
                {
                    "id": "T12",
                    "title": "Enter includes every queued tap",
                    "do": "Tap Right about ten times very fast and press Enter at once, "
                          "while it is still moving.",
                    "command": JOG,
                    "expect": "It finishes all the taps before leaving jog mode. The MPos "
                              "line includes every tap (X-618.000 for ten).",
                },
                {
                    "id": "T13",
                    "title": "Esc stops queued taps part-way",
                    "do": "Tap Right about ten times very fast and press Esc at once, "
                          "while it is still moving.",
                    "command": JOG,
                    "expect": "It stops within a fraction of a millimetre. \"Jog aborted. "
                              "MPos: X...\" shows less than 1.000 travelled (X between "
                              "-619.000 and -618.000).",
                },
            ],
        },
        {
            "title": "Keyboard capture",
            "intro": f"Each item is one run of: {JOG}.",
            "items": [
                {
                    "id": "K1",
                    "title": "No keys leak to other windows",
                    "do": "Put the mouse over another window with something scrollable "
                          "(this console, a browser) and click it. Jog with the arrows.",
                    "command": JOG,
                    "expect": "The machine jogs; the other window doesn't scroll and "
                              "nothing is typed into it.",
                },
                {
                    "id": "K2",
                    "title": "No stuck keys afterwards",
                    "do": "Leave jog mode with Enter, type something in a few windows, "
                          "and use the arrows and Alt there. Repeat with Esc.",
                    "command": JOG,
                    "expect": "The keyboard works normally everywhere. No key repeats by "
                              "itself, and Shift, Ctrl and Alt aren't stuck.",
                },
                {
                    "id": "K3",
                    "title": "Unplugging the keyboard mid-jog stops the machine",
                    "needs": "A USB keyboard (not a laptop's built-in one).",
                    "do": "Hold Right with Ctrl (a slow creep) and unplug the USB keyboard "
                          "while it moves. Plug it back in.",
                    "command": JOG,
                    "safety": "Do it in the middle of the table, well away from the edges.",
                    "expect": "The machine stops at once. jog.py exits with an error about "
                              "the device. After plugging the keyboard back in, it works "
                              "normally.",
                },
            ],
        },
    ],
}

if __name__ == "__main__":
    qaRunner.main(CHECKLIST)
