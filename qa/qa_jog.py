#!/usr/bin/env python3
"""Manual QA for jog.py on the real machine (issue #13, step 13).

    python qa/qa_jog.py            # or: python3 qa/qa_jog.py

Run this in one console and jog.py in another, from the repository root with
the venv active.  The checks come from PR #31 (Step 8, hold-to-jog) and PR #33
(Step 10, tap-to-step, and Esc cancelling queued steps).  The checklist is
plain data; qaRunner.py does the asking and recording, and evidence.py looks
for signs that a step is already done.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import evidence  # noqa: E402
import qaRunner  # noqa: E402

JOG = "python jog.py --home"
HOME = "X-619.000 Y-602.000 Z-3.000"
REPO = qaRunner.REPO_DIR
VENV_PYTHON = os.path.join(REPO, ".venv", "bin", "python")
GROUP = "input"


# -- evidence -----------------------------------------------------------------

def evdevInVenv():
    return evidence.canImport(VENV_PYTHON, "evdev")


def listed():
    """Listed in the 'input' group (takes effect from the next login)."""
    return evidence.listedInGroup(GROUP)


def inGroupNow():
    """This login has the 'input' group (logged in again since joining)."""
    return evidence.inGroupNow(GROUP)


def startingPoint():
    """S1: evdev missing and not in the group, or what is already in place."""
    evdev, group = evdevInVenv(), listed()
    if not evdev and not group:
        return qaRunner.PASS, ("evdev isn't installed in the venv, and you aren't in the "
                               "'input' group: a clean starting point.")
    found = []
    if evdev:
        found.append("evdev is already installed in the venv")
    if group:
        found.append("you're already listed in the 'input' group" +
                     ("" if inGroupNow() else " (from your next login)"))
    return qaRunner.SKIP, ("Not a clean starting point: " + " and ".join(found) + ". "
                           "I'll skip the tests that need them missing.")


def evdevInstalled():
    if evdevInVenv():
        return True, "evdev is now installed in the venv."
    return False, "evdev still isn't installed in the venv."


def nowListed():
    if listed():
        return True, "You're now listed in the 'input' group."
    return False, "You aren't listed in the 'input' group yet."


def loggedInAgain():
    if inGroupNow():
        return True, ("You're in the 'input' group in this login, so you've logged out and "
                      "back in.")
    return False, ("This login doesn't have the 'input' group yet: please log out and back "
                   "in now.")


# -- step helpers -------------------------------------------------------------

def jogStep(actions, expect, command=JOG):
    """One run of jog.py.  Everything to do in jog mode is listed up front,
    because jog mode captures the keyboard until Enter or Esc."""
    return {
        "do": "Please read all of this first, because jog mode captures the keyboard. "
              "In the second console, start jog.py:",
        "run": command,
        "actions": ["Wait for the jog-mode banner."] + actions +
                   ["Come back here once jog.py has finished."],
        "expect": expect,
    }


def startJog(expect):
    return {"do": "Please start jog.py in the second console:", "run": JOG, "expect": expect}


# -- the checklist ------------------------------------------------------------

CHECKLIST = {
    "name": "jog",
    "title": "jog.py manual hardware checks",
    "program": "qa_jog.py",
    "machine": "6060 ProverXL V1",
    "purpose": ("qa_jog.py guides you (the operator) through a manual test of jog.py. This "
                "is both to test the software, and to help familiarise you with its "
                "operation."),
    "intro": evidence.consoleSteps("jog.py", REPO, qaRunner.shortPath(REPO)) + [
        {"do": "Please make sure the spindle is off and the bit is well clear of the "
               "spoilboard, and keep a hand near reset throughout."},
        {"do": "Every jog.py run homes the machine first, because connecting resets this "
               "controller. That's why each command here uses --home."},
    ],
    "sections": [
        {
            "title": "Setup",
            "intro": ("These test how jog.py helps you set up keyboard jog. They need "
                      "evdev to be missing and you not to be in the 'input' group yet; "
                      "I'll check that for you and skip what doesn't apply."),
            "items": [
                {
                    "id": "S1",
                    "title": "Starting point: no evdev, not in the input group",
                    "auto": startingPoint,
                },
                {
                    "id": "S2",
                    "title": "Declining both offers changes nothing",
                    "skipIf": lambda: (
                        "evdev is already installed in the venv, so jog.py won't offer "
                        "to install it." if evdevInVenv() else
                        "You're already listed in the 'input' group, so jog.py won't "
                        "offer to add you." if listed() else None),
                    "steps": [
                        startJog("A message that keyboard jog needs evdev, the command "
                                 f"{VENV_PYTHON} -m pip install evdev, and \"Run it "
                                 "now? [y/N]\"."),
                        {"do": "Please answer N.",
                         "expect": "A message that keyboard jog needs the 'input' group, "
                                   "the command sudo usermod -aG input $USER, and \"Run it "
                                   "now? [y/N]\" again."},
                        {"do": "Please answer N again.",
                         "expect": "\"Keyboard jog: jog unavailable.\" Nothing is "
                                   "installed, and nothing moves."},
                    ],
                },
                {
                    "id": "S3",
                    "title": "Accepting the evdev install",
                    "skipIf": lambda: ("evdev is already installed in the venv."
                                       if evdevInVenv() else None),
                    "steps": [
                        startJog("The evdev message and \"Run it now? [y/N]\"."),
                        {"do": "Please answer y.",
                         "expect": "\"Running: ... -m pip install evdev\", pip's output, "
                                   "then \"Exit status 0.\". If pip fails, please fail "
                                   "this test and paste the error into the note."},
                        {"do": lambda: ("Please let jog.py finish." if listed() else
                                        "Please answer N at the 'input' group question."),
                         "expect": lambda: (
                             "A reminder to log out and back in, then \"Keyboard jog: "
                             "jog unavailable until re-login.\"" if listed() else
                             "\"Keyboard jog: jog unavailable.\" (because of the group)."),
                         "verify": evdevInstalled},
                    ],
                },
                {
                    "id": "S4",
                    "title": "Accepting the input-group fix",
                    "skipIf": lambda: ("You're already listed in the 'input' group."
                                       if listed() else None),
                    "steps": [
                        startJog("No evdev message this time. A message that keyboard "
                                 "jog needs the 'input' group, and \"Run it now? "
                                 "[y/N]\"."),
                        {"do": "Please answer y, and type your password if sudo asks "
                               "for it.",
                         "expect": "sudo's password prompt in that console (jog.py never "
                                   "sees your password), \"Exit status 0.\", a reminder "
                                   "to log out and back in, and \"Keyboard jog: jog "
                                   "unavailable until re-login.\"",
                         "verify": nowListed},
                    ],
                },
                {
                    "id": "S5",
                    "title": "The log-out reminder before logging out",
                    "skipIf": lambda: (
                        "You've already logged in again since joining the 'input' group."
                        if inGroupNow() else
                        "You aren't listed in the 'input' group, so there's nothing to "
                        "remind you about." if not listed() else None),
                    "steps": [
                        {"do": "Without logging out, please start jog.py once more:",
                         "run": JOG,
                         "expect": "One line: \"You are in the 'input' group, but only "
                                   "from your next login: log out and back in to use "
                                   "keyboard jog.\" No offer to run usermod again, then "
                                   "\"Keyboard jog: jog unavailable until re-login.\""},
                    ],
                },
                {
                    "id": "S6",
                    "title": "Silent once everything is in place",
                    "skipIf": lambda: (None if listed() else
                                       "You aren't listed in the 'input' group, so keyboard "
                                       "jog can't work yet."),
                    "safety": "The machine homes.",
                    "steps": [
                        {"do": "Please log out and back in now, so the 'input' group "
                               "takes effect. Your answers so far are saved. Afterwards, "
                               "please run qa_jog.py again from the repository root and "
                               "choose to resume:",
                         "run": "python3 qa/qa_jog.py",
                         "already": loggedInAgain, "verify": loggedInAgain},
                        jogStep(["Press Esc."],
                                "No setup messages and no questions: it connects, homes "
                                "and shows the banner. After Esc: \"Jog aborted. MPos: "
                                f"{HOME}\"."),
                    ],
                },
            ],
        },
        {
            "title": "Hold-to-jog",
            "intro": ("Holding a key jogs until you let go. Homed, the 6060 is at "
                      f"{HOME} ($130=622, $131=605, $27=3). jog.py prints the machine "
                      "position once, when you leave jog mode: \"Jog accepted. MPos: "
                      "...\" after Enter, \"Jog aborted. MPos: ...\" after Esc."),
            "items": [
                {
                    "id": "H1",
                    "title": "The banner",
                    "steps": [jogStep(
                        ["Read the two lines it prints as jog mode starts.", "Press Enter."],
                        "\"Jog mode: arrows X/Y, Shift+Up/Down Z. Tap to step 0.1 mm "
                        "(Alt+tap 0.01 mm), hold to jog; hold Ctrl to creep (1/10 "
                        "speed).\" and \"Enter accepts (after any queued steps finish), "
                        "Esc aborts at once. The keyboard is captured until then.\" "
                        f"Then \"Jog accepted. MPos: {HOME}\".")],
                },
                {
                    "id": "H2",
                    "title": "\"At the travel limit\" from home",
                    "steps": [jogStep(
                        ["Hold Left for a second.", "Hold Down for a second.",
                         "Hold Shift+Up for a second.", "Press Enter."],
                        "Nothing moves: each key is toward a home switch, and prints \"At "
                        "the travel limit in that direction; not moving.\" No alarm. "
                        f"Then \"Jog accepted. MPos: {HOME}\".")],
                },
                {
                    "id": "H3",
                    "title": "Left and Right move X",
                    "steps": [jogStep(
                        ["Hold Right for about 2 seconds, then let go.",
                         "Hold Left for about 1 second, then let go.", "Press Enter."],
                        "Right moves the tool right (+X) at 1000 mm/min and Left moves it "
                        "back; each time it slows to a stop when you let go. The MPos X "
                        "is a little above -619.")],
                },
                {
                    "id": "H4",
                    "title": "Up and Down move Y",
                    "steps": [jogStep(
                        ["Hold Up for about 2 seconds, then let go.",
                         "Hold Down for about 1 second, then let go.", "Press Enter."],
                        "Up moves the tool away from you (+Y) and Down toward you; each "
                        "stops when you let go. The MPos Y is a little above -602.")],
                },
                {
                    "id": "H5",
                    "title": "Shift+Up and Shift+Down move Z",
                    "safety": "Z goes down at 300 mm/min. Please let go early if the bit "
                              "gets anywhere near the spoilboard.",
                    "steps": [jogStep(
                        ["Hold Shift+Down for about 2 seconds, then let go.",
                         "Hold Shift+Up for about 1 second, then let go.", "Press Enter."],
                        "Shift+Down lowers the spindle and Shift+Up raises it; each stops "
                        "when you let go. The MPos Z is a little below -3.")],
                },
                {
                    "id": "H6",
                    "title": "Diagonal, then back to one axis",
                    "steps": [jogStep(
                        ["Hold Right.", "Keep holding Right and also hold Up.",
                         "After a second, let go of Up.", "Let go of Right.",
                         "Press Enter."],
                        "Right alone moves X; adding Up gives one smooth 45-degree "
                        "diagonal; letting go of Up goes back to X only; letting go of "
                        "Right stops.")],
                },
                {
                    "id": "H7",
                    "title": "Opposite keys cancel out",
                    "steps": [jogStep(
                        ["Hold Right.", "Keep holding Right and also hold Left.",
                         "Let go of Left.", "Let go of Right.", "Press Enter."],
                        "With both held, X stops. Letting go of Left starts the Right jog "
                        "again.")],
                },
                {
                    "id": "H8",
                    "title": "Ctrl creeps at a tenth of the speed",
                    "steps": [jogStep(
                        ["Hold Ctrl+Right for a few seconds, then let go of both.",
                         "Hold Right only, then press and hold Ctrl.",
                         "Let go of Ctrl, still holding Right.", "Let go of Right.",
                         "Press Enter."],
                        "Ctrl+Right creeps at 100 mm/min. Pressing Ctrl mid-jog drops to "
                        "the creep speed; letting go of it goes back to full speed.")],
                },
                {
                    "id": "H9",
                    "title": "Coast distance after release",
                    "steps": [jogStep(
                        ["Hold Right at full speed for about 3 seconds, then let go, "
                         "watching how far it keeps going.", "Press Enter."],
                        "It slows to a stop smoothly. Expected with $120=10 mm/s^2: about "
                        "14 mm over about 1.7 s at 1000 mm/min (about 1.25 mm for Z at "
                        "300). Please note what you saw, and whether it feels too "
                        "sluggish (Step 11 raises it to 30 mm/s^2).")],
                },
                {
                    "id": "H10",
                    "title": "Stopping short at the far edge (X)",
                    "safety": "It drives toward the right-hand switch. Keep a hand near "
                              "reset, and let go if it doesn't slow down before the switch.",
                    "steps": [jogStep(
                        ["Hold Right until it stops by itself (about 40 seconds).",
                         "Press Enter."],
                        "It stops by itself short of the switch: no error:15, no ALARM, "
                        "no traceback. The MPos X is -0.010 (0.01 mm inside the soft "
                        "limit).")],
                },
                {
                    "id": "H11",
                    "title": "A diagonal slides along an edge",
                    "safety": "It drives toward the back and right-hand switches. Keep a "
                              "hand near reset.",
                    "steps": [jogStep(
                        ["Hold Right and Up together until everything stops (about a "
                         "minute).", "Press Enter."],
                        "A diagonal until Y reaches the back edge, then along that edge "
                        "in X only, then it stops. No error:15, no alarm. The MPos is "
                        "X-0.010 Y-0.010.")],
                },
            ],
        },
        {
            "title": "Taps",
            "intro": ("A tap is an arrow released within 250 ms; it steps 0.1 mm. With 160 "
                      "steps/mm, positions move in whole motor steps of 0.00625 mm."),
            "items": [
                {
                    "id": "T1",
                    "title": "One tap moves 0.100",
                    "steps": [jogStep(
                        ["Tap Right once (a quick press and release).", "Press Enter."],
                        "A tiny move, then \"Jog accepted. MPos: X-618.900 Y-602.000 "
                        "Z-3.000\".")],
                },
                {
                    "id": "T2",
                    "title": "Taps on every axis and direction",
                    "steps": [jogStep(
                        ["Tap Right twice, then Left once.", "Tap Up twice, then Down once.",
                         "Tap Shift+Down twice, then Shift+Up once.", "Press Enter."],
                        "\"MPos: X-618.900 Y-601.900 Z-3.100\".")],
                },
                {
                    "id": "T3",
                    "title": "Ctrl+tap is also 0.100",
                    "steps": [jogStep(["Ctrl+tap Right once.", "Press Enter."],
                                      "\"MPos: X-618.900 ...\".")],
                },
                {
                    "id": "T4",
                    "title": "Ten Alt+taps total 0.100",
                    "steps": [jogStep(
                        ["Hold the left Alt and tap Right ten times.", "Press Enter."],
                        "Ten very small moves, and the desktop doesn't react to Alt (no "
                        "menu opens). \"MPos: X-618.900 ...\" (each tap is 1 or 2 motor "
                        "steps, 0.006 or 0.013 mm).")],
                },
                {
                    "id": "T5",
                    "title": "Ten AltGr+taps total 0.100",
                    "steps": [jogStep(
                        ["Hold AltGr (the right Alt) and tap Right ten times.",
                         "Press Enter."],
                        "No special characters or menus appear anywhere. \"MPos: "
                        "X-618.900 ...\".")],
                },
                {
                    "id": "T6",
                    "title": "Ten Alt+Shift+Down taps lower Z by 0.100",
                    "steps": [jogStep(
                        ["Hold Alt and Shift and tap Down ten times.", "Press Enter."],
                        "Alt+Shift doesn't switch the keyboard layout. \"MPos: ... "
                        "Z-3.100\".")],
                },
                {
                    "id": "T7",
                    "title": "Two keys tapped together give one diagonal step",
                    "steps": [jogStep(
                        ["Press Right and Up together and release both quickly.",
                         "Press Enter."],
                        "One tiny diagonal move. \"MPos: X-618.900 Y-601.900 ...\".")],
                },
                {
                    "id": "T8",
                    "title": "The hold threshold pause",
                    "steps": [jogStep(
                        ["Hold Right and watch when it starts, then let go.",
                         "Press Enter."],
                        "About a quarter of a second passes before the continuous jog "
                        "starts; a tap moves nothing until the key comes up.")],
                },
                {
                    "id": "T9",
                    "title": "Trying other hold thresholds",
                    "steps": [
                        jogStep(["Try a few taps and holds.", "Press Enter."],
                                "A shorter pause before a hold starts; taps still step "
                                "0.1.", command=f"{JOG} --hold-threshold 0.2"),
                        jogStep(["Try a few taps and holds.", "Press Enter."],
                                "A longer pause before a hold starts; taps still step 0.1. "
                                "Please put the value you prefer (0.2, 0.25 or 0.3) in the "
                                "note.", command=f"{JOG} --hold-threshold 0.3"),
                    ],
                },
                {
                    "id": "T10",
                    "title": "Ten quick taps total 1.000",
                    "steps": [jogStep(
                        ["Tap Right ten times as fast as you can.",
                         "Wait until it stops.", "Press Enter."],
                        "Smooth, with no stutter. \"MPos: X-618.000 ...\".")],
                },
                {
                    "id": "T11",
                    "title": "A step is shortened at a bound",
                    "steps": [jogStep(
                        ["Tap Right once (to X-618.900).", "Tap Left once.",
                         "Tap Left once more.", "Press Enter."],
                        "The first Left tap moves only about 0.09 mm, stopping just inside "
                        "the home end; the second moves nothing visible. No alarm, and "
                        "the home switch isn't touched. X ends at about -618.99 (to within "
                        "a motor step), never -619.000 or beyond.")],
                },
                {
                    "id": "T12",
                    "title": "Enter includes every queued tap",
                    "steps": [jogStep(
                        ["Tap Right about ten times very fast, and press Enter at once, "
                         "while it is still moving."],
                        "It finishes all the taps before leaving jog mode. The MPos "
                        "includes every tap (X-618.000 for ten).")],
                },
                {
                    "id": "T13",
                    "title": "Esc stops queued taps part-way",
                    "steps": [jogStep(
                        ["Tap Right about ten times very fast, and press Esc at once, "
                         "while it is still moving."],
                        "It stops within a fraction of a millimetre. \"Jog aborted. MPos: "
                        "X...\" shows less than 1.000 travelled (X between -619.000 and "
                        "-618.000).")],
                },
            ],
        },
        {
            "title": "Keyboard capture",
            "intro": "While jog mode is on, jog.py should have the keyboard to itself.",
            "items": [
                {
                    "id": "K1",
                    "title": "No keys leak to other windows",
                    "steps": [jogStep(
                        ["Click another window with something to scroll (a browser, say), "
                         "and leave the mouse over it.", "Jog with the arrows.",
                         "Press Enter."],
                        "The machine jogs; the other window doesn't scroll and nothing is "
                        "typed into it.")],
                },
                {
                    "id": "K2",
                    "title": "No stuck keys afterwards",
                    "steps": [
                        jogStep(["Jog a little.", "Press Enter."],
                                "\"Jog accepted. MPos: ...\"."),
                        {"do": "Please type something in a few windows, and try the arrows "
                               "and Alt there.",
                         "expect": "The keyboard works normally everywhere. No key repeats "
                                   "by itself, and Shift, Ctrl and Alt aren't stuck."},
                        jogStep(["Jog a little.", "Press Esc."],
                                "\"Jog aborted. MPos: ...\"."),
                        {"do": "Please type something in a few windows again, and try the "
                               "arrows and Alt there.",
                         "expect": "The keyboard works normally everywhere."},
                    ],
                },
                {
                    "id": "K3",
                    "title": "Unplugging the keyboard mid-jog stops the machine",
                    "needs": "A USB keyboard (not a laptop's built-in one).",
                    "safety": "Please do this in the middle of the table, well away from "
                              "the edges.",
                    "steps": [jogStep(
                        ["Jog to the middle of the table.",
                         "Hold Ctrl+Right (a slow creep).",
                         "While it moves, unplug the USB keyboard.",
                         "Plug the keyboard back in."],
                        "The machine stops at once, and jog.py exits with an error about "
                        "the device. Once plugged back in, the keyboard works normally.")],
                },
            ],
        },
    ],
}

if __name__ == "__main__":
    qaRunner.main(CHECKLIST)
