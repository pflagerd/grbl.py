#!/usr/bin/env python3
"""Manual QA for jog.py on the real machine (issue #13, step 13).

    python qa/qa_jog.py                       # or: python3 qa/qa_jog.py
    python qa/qa_jog.py --machine "NAME"      # skip the menu of machines

Run this in one console and jog.py in another, from the repository root with
the venv active.  The checks come from PR #31 (Step 8, hold-to-jog) and PR #33
(Step 10, tap-to-step, and Esc cancelling queued steps).  The checklist is
plain data; qaRunner.py does the asking and recording, and evidence.py looks
for signs that a step is already done.

Everything machine-specific (the home position, the tap targets, the travel)
comes from the chosen machine's profile, profiles/NAME.json, so the machine
must have been calibrated first.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import evidence  # noqa: E402
import qaRunner  # noqa: E402

JOG = "python jog.py --home"
TITLE = "jog.py manual hardware checks"
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
    """S4: what keyboard-jog setup is already in place, and so which tests to skip."""
    evdev, group = evdevInVenv(), listed()
    if not evdev and not group:
        return qaRunner.PASS, ("evdev isn't installed in the venv yet and you aren't in the "
                               "'input' group yet, so the next tests will check how jog.py "
                               "helps you set up keyboard jog.")
    done = []
    if evdev:
        done.append("evdev is already installed in the venv")
    if group:
        done.append("you're already listed in the 'input' group" +
                    ("" if inGroupNow() else " (from your next login)"))
    found = " and ".join(done)
    found = found[0].upper() + found[1:] if found.startswith("you") else found
    if evdev and group:
        return qaRunner.SKIP, (found + ", so keyboard jog is already set up. That's why I'll "
                               "skip the next tests, which check how jog.py helps you set it up.")
    return qaRunner.SKIP, (found + ", so that part of keyboard jog's setup is already done. "
                           "That's why I'll skip the next tests that check it.")


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

def jogStep(actions, expect, command=JOG, intro=None):
    """One run of jog.py.  Everything to do in jog mode is listed up front,
    because jog mode captures the keyboard until Enter or Esc.  intro, if
    given, is a paragraph shown first (what this group of tests is about)."""
    return {
        "do": (intro + "\n\n" if intro else "") +
              "Please read all of this first, because jog mode captures the keyboard. "
              "In the second console, start jog.py:",
        "run": command,
        "actions": [evidence.JOG_MODE_STARTS] + actions +
                   ["Come back here once jog.py has finished."],
        "expect": expect,
    }


def startJog(expect):
    return {"do": "Please start jog.py in the second console:", "run": JOG, "expect": expect}


# -- the machine --------------------------------------------------------------

XY_FEED = 1000        # jog.py's default XY jog feed, mm/min
LIMIT_MARGIN = 0.01   # jog.py stops this far inside each soft limit, mm
FINE_TAP = 0.01       # mm per Alt+tap
KEYS = {"X": {1: "Right", -1: "Left"}, "Y": {1: "Up", -1: "Down"},
        "Z": {1: "Shift+Up", -1: "Shift+Down"}}


def noProfile(machine):
    """Why qa_jog.py can't run for a machine without a profile."""
    return (f"There's no profile for \"{machine}\" (profiles/{machine}.json), and qa_jog.py "
            "takes the home position and travel from it. Please calibrate the machine first: "
            f"python calibrate.py \"{machine}\" (or python3 qa/qa_calibrate.py --machine "
            f"\"{machine}\").")


def duration(seconds):
    """A rough time to say: "about 40 seconds", "about a minute"."""
    tens = round(seconds / 10) * 10
    if tens < 50:
        return f"about {max(10, tens)} seconds"
    minutes = max(1, round(seconds / 60))
    return "about a minute" if minutes == 1 else f"about {minutes} minutes"


class Machine:
    """The numbers the checklist shows for one machine, from its profile."""

    def __init__(self, name, profile):
        self.name = name
        self.home = qaRunner.homeMPos(profile)
        self.away = qaRunner.awayFromHome(profile)   # +1 or -1 per axis, off the switch
        self.travel = {axis: float(profile[f"workArea{axis}"]) for axis in qaRunner.AXES}
        self.pullOff = float(profile.get("pullOff", 1.0))
        snapshot = profile.get("settingsSnapshot") or {}
        steps = [snapshot.get(key) for key in ("$100", "$101", "$102")]
        self.stepsPerMm = float(steps[0]) if steps[0] else None
        self.sameSteps = len(set(steps)) == 1

    def key(self, axis, toward=False):
        """The key that jogs axis away from its home switch (or toward it)."""
        return KEYS[axis][-self.away[axis] if toward else self.away[axis]]

    def at(self, axis, distance=0.0, places=3):
        """axis's MPos after moving distance away from home, as text: "-396.900" (home -397)."""
        return f"{self.home[axis] + self.away[axis] * distance:.{places}f}"

    def mpos(self, **moves):
        """The MPos after the given moves away from home: "X-396.900 Y-297.000 Z-3.000"."""
        return " ".join(f"{axis}{self.at(axis, moves.get(axis, 0.0))}" for axis in qaRunner.AXES)

    def homeText(self, axis):
        """The home coordinate as said in prose: "-397"."""
        return f"{self.home[axis]:g}"

    def side(self, axis):
        """"above" or "below" the home coordinate, after a move away from home."""
        return "above" if self.away[axis] > 0 else "below"

    def far(self, axis):
        """Where a jog away from home stops by itself: just inside the far soft limit."""
        bound = 0.0 if self.away[axis] > 0 else -self.travel[axis]
        return f"{bound - self.away[axis] * LIMIT_MARGIN:.3f}"

    def room(self, axis):
        """How far an axis can jog from home to the far edge."""
        return self.travel[axis] - self.pullOff - LIMIT_MARGIN

    def edgeToEdge(self):
        """H10: how long a full-speed X jog takes from home to the far edge."""
        return duration(self.room("X") / XY_FEED * 60)

    def diagonal(self):
        """H11: how long the X+Y diagonal and then the slide along an edge take."""
        first, last = sorted((self.room("X"), self.room("Y")))
        return duration(first / (XY_FEED / 2 ** 0.5) * 60 + (last - first) / XY_FEED * 60)

    def stepsSentence(self):
        """T1: the motor step size, from the profile's $100 (and $101, $102)."""
        if not self.stepsPerMm:
            return ""
        where = "" if self.sameSteps else " in X"
        return (f" With {self.stepsPerMm:g} steps/mm{where}, positions move in whole motor "
                f"steps of {1 / self.stepsPerMm:g} mm.")

    def fineTapSteps(self):
        """T4: the motor steps one Alt+tap of 0.01 mm makes in X."""
        if not self.stepsPerMm:
            return ""
        exact = FINE_TAP * self.stepsPerMm
        if abs(exact - round(exact)) < 1e-9:
            count = round(exact)
            return (f" (each tap is {count} motor step{'' if count == 1 else 's'}, "
                    f"{count / self.stepsPerMm:.3f} mm)")
        low, high = int(exact), int(exact) + 1
        return (f" (each tap is {low} or {high} motor steps, {low / self.stepsPerMm:.3f} or "
                f"{high / self.stepsPerMm:.3f} mm)")


def loadMachine(machine, profile=None):
    """The Machine for this name, from profile or profiles/<machine>.json.
    Exits with a message when there is no profile."""
    if profile is None:
        profile = qaRunner.readProfile(machine, os.path.join(REPO, "profiles"))
    if profile is None:
        sys.exit(noProfile(machine))
    return Machine(machine, profile)


# -- the checklist ------------------------------------------------------------

def makeChecklist(machine, profile=None):
    """The checklist for one machine.  profile is its profile as a dict (read
    from profiles/<machine>.json when not given); with none, this exits and
    says to calibrate first."""
    m = loadMachine(machine, profile)
    HOME = m.mpos()
    X, Y, Z = (m.key(axis) for axis in qaRunner.AXES)              # away from home
    toX, toY, toZ = (m.key(axis, toward=True) for axis in qaRunner.AXES)
    zArrow = Z.split("+")[1]                                          # Shift+Down: Down
    xWay = {1: "right (+X)", -1: "left (-X)"}[m.away["X"]]
    yWay, yBack = {1: ("away from you (+Y)", "toward you"),
                   -1: ("toward you (-Y)", "away from you")}[m.away["Y"]]
    zWay, zBack = {1: ("raises the spindle", "lowers it"),
                   -1: ("lowers the spindle", "raises it")}[m.away["Z"]]
    xSwitch = {1: "right-hand", -1: "left-hand"}[m.away["X"]]
    ySwitch = {1: "back", -1: "front"}[m.away["Y"]]
    # H11: which axis reaches its far edge first, and then slides along it.
    first, other = ("Y", "X") if m.room("Y") <= m.room("X") else ("X", "Y")
    edge = {"X": xSwitch, "Y": ySwitch}[first]
    return {
        "name": "jog",
        "title": TITLE,
        "program": "qa_jog.py",
        "machine": machine,
        "purpose": ("qa_jog.py guides you (the operator) through a manual test of jog.py. This "
                    "is both to test the software, and to help familiarise you with its "
                    "operation."),
        "intro": evidence.consoleSteps("jog.py", REPO, qaRunner.shortPath(REPO)) + [
            {"do": "Please make sure the spindle is off and the bit is well clear of the "
                   "spoilboard, and keep a hand near the E-stop button throughout.\n\n"
                   "Because connecting resets this controller, every jog.py must home the "
                   "machine first. That is why you will see jog.py --home in upcoming steps."},
        ],
        "sections": [
            {
                "title": "Setup",
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
                        "steps": [
                            {"do": "Please log out and back in now, so the 'input' group "
                                   "takes effect. Your answers so far are saved. Afterwards, "
                                   "please run qa_jog.py again from the repository root and "
                                   "choose to resume:",
                             "run": f'python3 qa/qa_jog.py --machine "{machine}"',
                             "already": loggedInAgain, "verify": loggedInAgain,
                             # Already logged in again: S5's screen has just said so.
                             "quietWhenDone": True},
                            jogStep(["Press Esc."],
                                    "No setup messages and no questions: it connects, homes "
                                    "and prints the two lines above. After Esc: \"Jog aborted. MPos: "
                                    f"{HOME}\"."),
                        ],
                    },
                ],
            },
            {
                "title": "Hold-to-jog",
                "items": [
                    {
                        "id": "H1",
                        "title": "The two lines that start jog mode",
                        "steps": [
                            {"do": f"Holding a key jogs until you let go. Homed, the {machine} is "
                                   f"at {HOME} ($130={m.travel['X']:g}, $131={m.travel['Y']:g}, "
                                   f"$27={m.pullOff:g}). While you jog, jog.py "
                                   "shows the position live, with the limits and how far you "
                                   "are from each. When you leave jog mode it prints \"Jog "
                                   "accepted. MPos: ...\" after Enter, or \"Jog aborted. MPos: "
                                   "...\" after Esc."},
                            jogStep(
                                ["Check that the two lines it prints are exactly as shown above.",
                                 "Under them, check the position block: X, Y and Z with their "
                                 "low and high limits and the distance to each, updating as the "
                                 "machine moves.",
                                 "Press Enter."],
                                "The two lines exactly as shown above. A block with X "
                                f"{HOME.split()[0][1:]}, Y {HOME.split()[1][1:]}, Z "
                                f"{HOME.split()[2][1:]}, \"at limit\" at the home end of each axis, "
                                "and State: Idle. After Enter: \"Jog accepted. MPos: "
                                f"{HOME}\"."),
                        ],
                    },
                    {
                        "id": "H2",
                        "title": "\"At the travel limit\" from home",
                        "steps": [jogStep(
                            [f"Hold {toX} for a second.", f"Hold {toY} for a second.",
                             f"Hold {toZ} for a second.", "Press Enter."],
                            "Nothing moves: each key is toward a home switch, and prints \"At "
                            "the travel limit in that direction; not moving.\" No alarm. "
                            f"Then \"Jog accepted. MPos: {HOME}\".")],
                    },
                    {
                        "id": "H3",
                        "title": "Left and Right move X",
                        "steps": [jogStep(
                            [f"Hold {X} for about 2 seconds, then let go.",
                             f"Hold {toX} for about 1 second, then let go.", "Press Enter."],
                            f"{X} moves the tool {xWay} at {XY_FEED} mm/min and {toX} moves "
                            "it back; each time it slows to a stop when you let go. The MPos X "
                            f"is a little {m.side('X')} {m.homeText('X')}.")],
                    },
                    {
                        "id": "H4",
                        "title": "Up and Down move Y",
                        "steps": [jogStep(
                            [f"Hold {Y} for about 2 seconds, then let go.",
                             f"Hold {toY} for about 1 second, then let go.", "Press Enter."],
                            f"{Y} moves the tool {yWay} and {toY} {yBack}; each stops when "
                            f"you let go. The MPos Y is a little {m.side('Y')} "
                            f"{m.homeText('Y')}.")],
                    },
                    {
                        "id": "H5",
                        "title": "Shift+Up and Shift+Down move Z",
                        "safety": "Z goes down at 300 mm/min. Please let go early if the bit "
                                  "gets anywhere near the spoilboard.",
                        "steps": [jogStep(
                            [f"Hold {Z} for about 2 seconds, then let go.",
                             f"Hold {toZ} for about 1 second, then let go.", "Press Enter."],
                            f"{Z} {zWay} and {toZ} {zBack}; each stops when you let go. The "
                            f"MPos Z is a little {m.side('Z')} {m.homeText('Z')}.")],
                    },
                    {
                        "id": "H6",
                        "title": "Diagonal, then back to one axis",
                        "steps": [jogStep(
                            [f"Hold {X}.", f"Keep holding {X} and also hold {Y}.",
                             f"After a second, let go of {Y}.", f"Let go of {X}.",
                             "Press Enter."],
                            f"{X} alone moves X; adding {Y} gives one smooth 45-degree "
                            f"diagonal; letting go of {Y} goes back to X only; letting go of "
                            f"{X} stops.")],
                    },
                    {
                        "id": "H7",
                        "title": "Opposite keys cancel out",
                        "steps": [jogStep(
                            [f"Hold {X}.", f"Keep holding {X} and also hold {toX}.",
                             f"Let go of {toX}.", f"Let go of {X}.", "Press Enter."],
                            f"With both held, X stops. Letting go of {toX} starts the {X} jog "
                            "again.")],
                    },
                    {
                        "id": "H8",
                        "title": "Ctrl creeps at a tenth of the speed",
                        "steps": [jogStep(
                            [f"Hold Ctrl+{X} for a few seconds, then let go of both.",
                             f"Hold {X} only, then press and hold Ctrl.",
                             f"Let go of Ctrl, still holding {X}.", f"Let go of {X}.",
                             "Press Enter."],
                            f"Ctrl+{X} creeps at {XY_FEED // 10} mm/min. Pressing Ctrl mid-jog drops to "
                            "the creep speed; letting go of it goes back to full speed.")],
                    },
                    {
                        "id": "H9",
                        "title": "Coast distance after release",
                        "steps": [jogStep(
                            [f"Hold {X} at full speed for about 3 seconds, then let go, "
                             "watching how far it keeps going.", "Press Enter."],
                            "It slows to a stop smoothly. Expected with $120=10 mm/s^2: about "
                            "14 mm over about 1.7 s at 1000 mm/min (about 1.25 mm for Z at "
                            "300). Please note what you saw, and whether it feels too "
                            "sluggish (Step 11 raises it to 30 mm/s^2).")],
                    },
                    {
                        "id": "H10",
                        "title": "Stopping short at the far edge (X)",
                        "safety": f"It drives toward the {xSwitch} switch. Keep a hand near "
                                  "the E-stop button, and let go if it doesn't slow down before "
                                  "the switch.",
                        "steps": [jogStep(
                            [f"Hold {X} until it stops by itself ({m.edgeToEdge()}).",
                             "Press Enter."],
                            "It stops by itself short of the switch: no error:15, no ALARM, "
                            f"no traceback. The MPos X is {m.far('X')} ({LIMIT_MARGIN} mm inside the "
                            "soft limit).")],
                    },
                    {
                        "id": "H11",
                        "title": "A diagonal slides along an edge",
                        "safety": f"It drives toward the {ySwitch} and {xSwitch} switches. Keep a "
                                  "hand near the E-stop button.",
                        "steps": [jogStep(
                            [f"Hold {X} and {Y} together until everything stops "
                             f"({m.diagonal()}).", "Press Enter."],
                            f"A diagonal until {first} reaches the {edge} edge, then along that "
                            f"edge in {other} only, then it stops. No error:15, no alarm. The "
                            f"MPos is X{m.far('X')} Y{m.far('Y')}.")],
                    },
                ],
            },
            {
                "title": "Taps",
                "items": [
                    {
                        "id": "T1",
                        "title": "One tap moves 0.100",
                        "steps": [jogStep(
                            [f"Tap {X} once (a quick press and release).", "Press Enter."],
                            f"A tiny move, then \"Jog accepted. MPos: {m.mpos(X=0.1)}\".",
                            intro="Now taps. A tap is an arrow released within 250 ms; it "
                                  "steps 0.1 mm." + m.stepsSentence())],
                    },
                    {
                        "id": "T2",
                        "title": "Taps on every axis and direction",
                        "steps": [jogStep(
                            [f"Tap {X} twice, then {toX} once.", f"Tap {Y} twice, then {toY} once.",
                             f"Tap {Z} twice, then {toZ} once.", "Press Enter."],
                            f"\"MPos: {m.mpos(X=0.1, Y=0.1, Z=0.1)}\".")],
                    },
                    {
                        "id": "T3",
                        "title": "Ctrl+tap is also 0.100",
                        "steps": [jogStep([f"Ctrl+tap {X} once.", "Press Enter."],
                                          f"\"MPos: X{m.at('X', 0.1)} ...\".")],
                    },
                    {
                        "id": "T4",
                        "title": "Ten Alt+taps total 0.100",
                        "steps": [jogStep(
                            [f"Hold the left Alt and tap {X} ten times.", "Press Enter."],
                            "Ten very small moves, and the desktop doesn't react to Alt (no "
                            f"menu opens). \"MPos: X{m.at('X', 0.1)} ...\"{m.fineTapSteps()}.")],
                    },
                    {
                        "id": "T5",
                        "title": "Ten AltGr+taps total 0.100",
                        "steps": [jogStep(
                            [f"Hold AltGr (the right Alt) and tap {X} ten times.",
                             "Press Enter."],
                            "No special characters or menus appear anywhere. \"MPos: "
                            f"X{m.at('X', 0.1)} ...\".")],
                    },
                    {
                        "id": "T6",
                        "title": f"Ten Alt+{Z} taps {zWay.split()[0][:-1]} Z by 0.100",
                        "steps": [jogStep(
                            [f"Hold Alt and Shift and tap {zArrow} ten times.", "Press Enter."],
                            "Alt+Shift doesn't switch the keyboard layout. \"MPos: ... "
                            f"Z{m.at('Z', 0.1)}\".")],
                    },
                    {
                        "id": "T7",
                        "title": "Two keys tapped together give one diagonal step",
                        "steps": [jogStep(
                            [f"Press {X} and {Y} together and release both quickly.",
                             "Press Enter."],
                            f"One tiny diagonal move. \"MPos: X{m.at('X', 0.1)} "
                            f"Y{m.at('Y', 0.1)} ...\".")],
                    },
                    {
                        "id": "T8",
                        "title": "The hold threshold pause",
                        "steps": [jogStep(
                            [f"Hold {X} and watch when it starts, then let go.",
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
                            [f"Tap {X} ten times as fast as you can.",
                             "Wait until it stops.", "Press Enter."],
                            f"Smooth, with no stutter. \"MPos: X{m.at('X', 1)} ...\".")],
                    },
                    {
                        "id": "T11",
                        "title": "A step is shortened at a bound",
                        "steps": [jogStep(
                            [f"Tap {X} once (to X{m.at('X', 0.1)}).", f"Tap {toX} once.",
                             f"Tap {toX} once more.", "Press Enter."],
                            f"The first {toX} tap moves only about 0.09 mm, stopping just inside "
                            "the home end; the second moves nothing visible. No alarm, and "
                            "the home switch isn't touched. X ends at about "
                            f"{m.at('X', LIMIT_MARGIN, places=2)} (to within a motor step), never "
                            f"{m.at('X')} or beyond.")],
                    },
                    {
                        "id": "T12",
                        "title": "Enter includes every queued tap",
                        "steps": [jogStep(
                            [f"Tap {X} about ten times very fast, and press Enter at once, "
                             "while it is still moving."],
                            "It finishes all the taps before leaving jog mode. The MPos "
                            f"includes every tap (X{m.at('X', 1)} for ten).")],
                    },
                    {
                        "id": "T13",
                        "title": "Esc stops queued taps part-way",
                        "steps": [jogStep(
                            [f"Tap {X} about ten times very fast, and press Esc at once, "
                             "while it is still moving."],
                            "It stops within a fraction of a millimetre. \"Jog aborted. MPos: "
                            f"X...\" shows less than 1.000 travelled (X between {m.at('X')} and "
                            f"{m.at('X', 1)}).")],
                    },
                ],
            },
            {
                "title": "Keyboard capture",
                "items": [
                    {
                        "id": "K1",
                        "title": "No keys leak to other windows",
                        "steps": [jogStep(
                            ["Click another window with something to scroll (a browser, say), "
                             "and leave the mouse over it.", "Jog with the arrows.",
                             "Press Enter."],
                            "The machine jogs; the other window doesn't scroll and nothing is "
                            "typed into it.",
                            intro="While jog mode is on, jog.py should have the keyboard to "
                                  "itself.")],
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
    qaRunner.main(makeChecklist, title=TITLE)
