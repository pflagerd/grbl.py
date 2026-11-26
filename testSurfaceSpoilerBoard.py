#!/usr/bin/env python3
"""Unit tests for surface-spoiler-board.py (issue #13, step 12).

No hardware, no evdev.  The script's file name has hyphens, so it cannot be
imported with a plain `import`; importlib loads it from its path instead.

A FakeGrbl answers each written G-code line with 'ok' (or an error, or a
Ctrl-C at a chosen line), answers a raw '?' with a status report, tracks the
G53 position, and keeps $132 writes so calibrate.resetZLimit() can read them
back.  A scripted operator answers the prompts in order, and a fake
runKeyboardJog moves the machine to the next of a list of positions.

Run: python testSurfaceSpoilerBoard.py
"""

import contextlib
import importlib.util
import io
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import calibrate
import jog
from GrblController import MachineProfile

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("surfaceSpoilerBoard",
                                               os.path.join(HERE, "surface-spoiler-board.py"))
ssb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ssb)

SETTINGS = {'$20': '1', '$22': '1', '$23': '3', '$27': '3.000', '$110': '2000.000',
            '$111': '2000.000', '$112': '2000.000', '$130': '622.000', '$131': '605.000',
            '$132': '109.000'}
BOUNDS = jog.softLimitBounds(SETTINGS)          # X -619..0, Y -602..0, Z -109..-3
BOARD = (-600.0, -580.0, -200.0, -280.0)        # 400 x 300 mm
TOUCH_Z = -80.0
CORNER1 = (-600.0, -580.0, -60.0)
CORNER2 = (-200.0, -280.0, -60.0)
TOUCH = (-400.0, -400.0, TOUCH_Z)


def plan(**overrides):
    arguments = dict(board=BOARD, diameter=35.0, stepover=14.0, overrun=17.5, touchZ=TOUCH_Z,
                     totalDepth=0.3, passDepth=0.3, clearance=5.0, pattern=ssb.ZIGZAG,
                     bounds=BOUNDS, feed=286, plungeFeed=50, spindle=10000)
    arguments.update(overrides)
    return ssb.planRaster(**arguments)


def words(line):
    return {letter: float(value) for letter, value in re.findall(r'([XYZF])(-?[\d.]+)', line)}


def cuttingXMoves(gcode):
    # (start x, end x) of every G1 move along X
    moves, x = [], None
    for line in gcode:
        w = words(line)
        if 'X' in w:
            if line.startswith('G53 G1') and x is not None:
                moves.append((x, w['X']))
            x = w['X']
    return moves


class FeedTests(unittest.TestCase):
    def testFormula(self):
        self.assertEqual(ssb.REFERENCE_FEED, 400)
        self.assertEqual(ssb.REFERENCE_DIAMETER, 25)
        self.assertEqual(ssb.defaultFeed(25), 400)
        self.assertEqual(ssb.defaultFeed(35), 286)   # 285.7 rounds up
        self.assertEqual(ssb.defaultFeed(50), 200)
        self.assertEqual(ssb.defaultFeed(30), 333)

    def testClamp(self):
        self.assertEqual(ssb.defaultFeed(5), 1000)    # 2000 -> 1000
        self.assertEqual(ssb.defaultFeed(10), 1000)   # exactly 1000
        self.assertEqual(ssb.defaultFeed(100), 100)   # exactly 100
        self.assertEqual(ssb.defaultFeed(200), 100)   # 50 -> 100

    def testPrecedence(self):
        self.assertEqual(ssb.resolveParameter(300, 250, 286), (300, 'command line'))
        self.assertEqual(ssb.resolveParameter(None, 250, 286), (250, 'saved'))
        self.assertEqual(ssb.resolveParameter(None, None, 286), (286, 'derived'))


class GeometryTests(unittest.TestCase):
    def testLineCountAndEvenStepover(self):
        # 300 mm in Y at <= 14 mm: 22 intervals, 23 lines, 13.636 mm apart.
        p = plan()
        self.assertEqual(len(p.lineYs), 23)
        self.assertAlmostEqual(p.stepover, 300 / 22)
        gaps = [b - a for a, b in zip(p.lineYs, p.lineYs[1:])]
        for gap in gaps:
            self.assertAlmostEqual(gap, 300 / 22, places=2)
            self.assertLessEqual(gap, 14.0 + 1e-3)

    def testExactMultiple(self):
        p = plan(stepover=15.0)   # 300 / 15 = 20 intervals exactly
        self.assertEqual(len(p.lineYs), 21)
        self.assertAlmostEqual(p.stepover, 15.0)

    def testEdgeCoverage(self):
        p = plan()
        x0, y0, x1, y1 = BOARD
        self.assertEqual(p.lineYs[0], y0)
        self.assertEqual(p.lineYs[-1], y1)
        self.assertLessEqual(p.xStart, x0 - 17.5)
        self.assertGreaterEqual(p.xEnd, x1 + 17.5)
        # Every cutting line spans the full X extent.
        for start, end in cuttingXMoves(p.gcode):
            self.assertEqual({start, end}, {p.xStart, p.xEnd})
        # Neighbouring lines overlap: the gap is less than the diameter.
        self.assertTrue(all(b - a < 35 for a, b in zip(p.lineYs, p.lineYs[1:])))

    def testLowToHighY(self):
        p = plan()
        ys = [words(line)['Y'] for line in p.gcode if 'Y' in words(line)]
        self.assertEqual(ys, sorted(ys))
        self.assertEqual(ys[0], BOARD[1])

    def testOverrunClampedToBounds(self):
        # Board 5 mm from the X low bound (-619 + 0.01 margin): overrun shrinks to 4.99.
        p = plan(board=(-614.0, -580.0, -5.0, -280.0))
        self.assertAlmostEqual(p.xStart, -618.99)
        self.assertAlmostEqual(p.xEnd, -0.01)
        text = " ".join(p.notes)
        self.assertIn("left (low X) edge is shrunk from 17.500 to 4.990", text)
        self.assertIn("right (high X) edge is shrunk from 17.500 to 4.990", text)
        for line in p.gcode:
            w = words(line)
            if 'X' in w:
                self.assertTrue(BOUNDS['X'][0] <= w['X'] <= BOUNDS['X'][1])

    def testNoNoteWhenOverrunFits(self):
        self.assertEqual(plan().notes, [])

    def testZigzagAlternates(self):
        p = plan()
        moves = cuttingXMoves(p.gcode)
        self.assertEqual(len(moves), 23)
        for index, (start, end) in enumerate(moves):
            self.assertEqual(end > start, index % 2 == 0, f"line {index}")
        # The Y step happens at the cutting feed, at depth, with no lift.
        self.assertFalse(any(line.startswith('G53 G0 Z') for line in p.gcode[6:-2]))

    def testOneWayAllPlusXWithLifts(self):
        p = plan(pattern=ssb.ONE_WAY)
        moves = cuttingXMoves(p.gcode)
        self.assertEqual(len(moves), 23)
        self.assertTrue(all(end > start for start, end in moves))
        lifts = [line for line in p.gcode if line == f"G53 G0 Z{p.clearZ:.3f}"]
        self.assertEqual(len(lifts), 1 + 22 + 1)   # start, one per return, end
        plunges = [line for line in p.gcode if line.startswith('G53 G1 Z')]
        self.assertEqual(len(plunges), 23)

    def testDepthsAndHeights(self):
        p = plan(totalDepth=0.6, passDepth=0.3)
        self.assertEqual(p.cutZ, -80.6)
        self.assertEqual(p.clearZ, -75.0)
        self.assertEqual(p.approachZ, -79.3)    # 1 mm above what the first pass left
        self.assertIn("G53 G1 Z-80.600 F50", p.gcode)

    def testClearanceClampedAtTopOfTravel(self):
        p = plan(touchZ=-6.0)       # -6 + 5 = -1, above the Z top -3 (+0.01 margin)
        self.assertEqual(p.clearZ, -3.01)
        self.assertIn("clearance is shrunk", " ".join(p.notes))
        with self.assertRaisesRegex(ssb.PlanRefused, "clearance"):
            plan(touchZ=-3.5)

    def testRefusals(self):
        with self.assertRaisesRegex(ssb.PlanRefused, "right of"):
            plan(board=(-200.0, -580.0, -600.0, -280.0))
        with self.assertRaisesRegex(ssb.PlanRefused, "right of"):
            plan(board=(-600.0, -280.0, -200.0, -580.0))
        with self.assertRaisesRegex(ssb.PlanRefused, "stepover"):
            plan(stepover=40.0)

    def testSpindleBeforePlungeAndStoppedAtEnd(self):
        p = plan()
        m3 = p.gcode.index("M3 S10000")
        firstPlunge = next(i for i, line in enumerate(p.gcode) if line.startswith('G53 G1 Z'))
        lift = p.gcode.index(f"G53 G0 Z{p.clearZ:.3f}")
        self.assertLess(lift, m3)               # off the surface before it spins
        self.assertEqual(p.gcode[m3 + 1], "G4 P3")
        self.assertLess(m3, firstPlunge)
        self.assertEqual(p.gcode[-1], "M5")
        self.assertEqual(p.gcode[-2], f"G53 G0 Z{p.clearZ:.3f}")

    def testWholeNumberSpindle(self):
        self.assertIn("M3 S10000", plan(spindle=10000.0).gcode)
        self.assertIn("M3 S12000", plan(spindle=11999.6).gcode)
        for line in plan(spindle=9000.4).gcode:
            self.assertNotRegex(line, r'S\d+\.')

    def testLinesFitGrbl(self):
        self.assertTrue(all(len(line) <= ssb.MAX_LINE_LENGTH for line in plan().gcode))

    def testEstimate(self):
        p = plan()
        # 23 lines of 435 mm plus 22 steps of 13.64 mm plus the 1.3 mm plunge, at feed.
        self.assertAlmostEqual(p.cutLength, 23 * 435 + 300 + 1.3, places=1)
        self.assertGreater(p.seconds, p.cutLength / 286 * 60)


class StartCornerTests(unittest.TestCase):
    """--start back-right: the front-left pass turned half a turn, nothing else changed."""

    BACK_RIGHT_X = BOARD[2] + 17.5     # xEnd: the right edge plus the overrun

    def testFrontLeftIsTheDefaultAndByteForByteUnchanged(self):
        # sha256 of the whole G-code from master, before --start existed.  If
        # this fails, the default (front-left) G-code has changed: on purpose
        # or not, the operator's usual cut is no longer what it was.
        golden = {ssb.ZIGZAG: "f92973c15389f09f5c10afb4b1424e5a8dbd8b8280ff59807be778ccf4283a52",
                  ssb.ONE_WAY: "3d7f6dd115feec4c39b24bf243710a42f4bd45db299e4704219ec80cc4140bf4"}
        for pattern, digest in golden.items():
            for p in (plan(pattern=pattern), plan(pattern=pattern, start=ssb.FRONT_LEFT)):
                self.assertEqual(hashlib.sha256("\n".join(p.gcode).encode()).hexdigest(), digest,
                                 pattern)

    def testFirstLineAtTheBackLastAtTheFront(self):
        p = plan(start=ssb.BACK_RIGHT)
        x0, y0, x1, y1 = BOARD
        self.assertEqual(p.lineYs[0], y1)
        self.assertEqual(p.lineYs[-1], y0)
        self.assertEqual(p.lineYs, sorted(p.lineYs, reverse=True))
        ys = [words(line)['Y'] for line in p.gcode if 'Y' in words(line)]
        self.assertEqual(ys, sorted(ys, reverse=True))

    def testSameLinesStepoverAndLengths(self):
        for pattern in ssb.PATTERNS:
            front = plan(pattern=pattern)
            back = plan(pattern=pattern, start=ssb.BACK_RIGHT)
            self.assertEqual(back.lineYs, list(reversed(front.lineYs)), pattern)
            self.assertEqual(back.stepover, front.stepover)
            self.assertEqual((back.xStart, back.xEnd), (front.xStart, front.xEnd))
            self.assertEqual(len(cuttingXMoves(back.gcode)), len(cuttingXMoves(front.gcode)))
            self.assertAlmostEqual(back.cutLength, front.cutLength, places=6)
            self.assertAlmostEqual(back.rapidLength, front.rapidLength, places=6)
            self.assertAlmostEqual(back.seconds, front.seconds, places=6)
            self.assertEqual(len(back.gcode), len(front.gcode))

    def testOneWayLinesRunRightToLeft(self):
        p = plan(pattern=ssb.ONE_WAY, start=ssb.BACK_RIGHT)
        moves = cuttingXMoves(p.gcode)
        self.assertEqual(len(moves), 23)
        self.assertTrue(all((start, end) == (p.xEnd, p.xStart) for start, end in moves))
        # Each return is a lifted rapid back to the right-hand end.
        returns = [line for line in p.gcode if line.startswith(f"G53 G0 X{p.xEnd:.3f} Y")]
        self.assertEqual(len(returns), 23)       # the start, then one per line after the first

    def testZigzagStartsRightToLeft(self):
        p = plan(start=ssb.BACK_RIGHT)
        moves = cuttingXMoves(p.gcode)
        self.assertEqual(len(moves), 23)
        for index, (start, end) in enumerate(moves):
            self.assertEqual(end < start, index % 2 == 0, f"line {index}")

    def testInitialRapidGoesToTheBackRight(self):
        p = plan(start=ssb.BACK_RIGHT)
        first = next(line for line in p.gcode if 'X' in words(line))
        self.assertEqual(first, f"G53 G0 X{self.BACK_RIGHT_X:.3f} Y{BOARD[3]:.3f}")
        self.assertEqual(p.xEnd, self.BACK_RIGHT_X)
        # The estimate starts over that corner: the rapid there counts nothing,
        # just as the front-left rapid counts nothing from the front-left.
        self.assertAlmostEqual(p.rapidLength, plan().rapidLength, places=6)

    def testClampingAndNotesUnchanged(self):
        board = (-614.0, -580.0, -5.0, -280.0)
        front = plan(board=board)
        back = plan(board=board, start=ssb.BACK_RIGHT)
        self.assertEqual((back.xStart, back.xEnd), (front.xStart, front.xEnd))
        self.assertEqual(back.notes, front.notes)
        for line in back.gcode:
            w = words(line)
            if 'X' in w:
                self.assertTrue(BOUNDS['X'][0] <= w['X'] <= BOUNDS['X'][1])
        first = next(line for line in back.gcode if 'X' in words(line))
        self.assertEqual(first, "G53 G0 X-0.010 Y-280.000")   # the clamped right end

    def testUnknownStartRefused(self):
        with self.assertRaisesRegex(ssb.PlanRefused, "start corner"):
            plan(start="top-left")


class DepthTests(unittest.TestCase):
    def testArithmetic(self):
        total = 0.0
        for expected in (0.3, 0.6, 0.9, 1.2):
            total = ssb.planPassDepth(total, 0.3, 3.0)
            self.assertEqual(total, expected)   # exactly, no float drift

    def testCap(self):
        total = 0.0
        for _ in range(10):
            total = ssb.planPassDepth(total, 0.3, 3.0)
        self.assertEqual(total, 3.0)            # exactly the cap is allowed
        with self.assertRaisesRegex(ssb.DepthRefused, "past the 3 mm cap"):
            ssb.planPassDepth(total, 0.3, 3.0)
        with self.assertRaisesRegex(ssb.DepthRefused, "0.300 mm is left"):
            ssb.planPassDepth(2.7, 0.5, 3.0)
        with self.assertRaisesRegex(ssb.DepthRefused, "more than 0"):
            ssb.planPassDepth(0.0, 0.0, 3.0)

    def testZRoom(self):
        self.assertEqual(ssb.checkZRoom(-80.0, 0.3, '109.000'), (True, -80.3, -109.0))
        ok, bottom, floor = ssb.checkZRoom(-80.0, 1.0, '80.950')   # -81 < -80.95 + 0.1
        self.assertFalse(ok)
        self.assertTrue(ssb.checkZRoom(-80.0, 1.0, '81.100')[0])   # exactly the margin
        self.assertFalse(ssb.checkZRoom(-80.0, 1.0, '81.099')[0])


class BitDirectoryTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory)
        self.path = os.path.join(self.directory, "bits.json")


class BitFileTests(BitDirectoryTest):
    def testMissingFileIsEmpty(self):
        self.assertEqual(ssb.loadBits(self.path), {})

    def testRoundTripAndFormat(self):
        bits = {"35mm-flat": {"diameter": 35.0, "flutes": 2, "note": "the big one"},
                "25mm": {"diameter": 25.0, "feed": 400}}
        ssb.saveBits(self.path, bits)
        self.assertEqual(ssb.loadBits(self.path), bits)
        with open(self.path) as jsonFile:
            text = jsonFile.read()
        self.assertEqual(text, json.dumps({"schemaVersion": 1, "bits": bits}, indent=2,
                                          sort_keys=True) + "\n")
        self.assertLess(text.index('"25mm"'), text.index('"35mm-flat"'))

    def testWriteKeepsTheFileReadable(self):
        # mkstemp's temp file is 0600; the saved file must not end up owner-only.
        umask = os.umask(0o022)
        try:
            ssb.saveBits(self.path, {"a": {"diameter": 20.0}})
            self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o644)
            os.chmod(self.path, 0o664)
            ssb.saveBits(self.path, {"b": {"diameter": 30.0}})
            self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o664)   # an existing mode is kept
        finally:
            os.umask(umask)

    def testShippedBits(self):
        # profiles/bits.json as committed: the owner's four bits, diameter only,
        # so each uses the diameter-derived feed.
        bits = ssb.loadBits(os.path.join(HERE, "profiles", "bits.json"))
        self.assertEqual(sorted(bits), ["25mm", "36mm", "38mm", "50mm"])
        for name, entry in bits.items():
            self.assertEqual(set(entry), {"diameter"}, f"{name}: only the diameter is known yet")
            self.assertEqual(name, f"{entry['diameter']:g}mm")
        feeds = {name: ssb.defaultFeed(entry['diameter']) for name, entry in bits.items()}
        self.assertEqual(feeds, {"25mm": 400, "36mm": 278, "38mm": 263, "50mm": 200})

    def testAtomicWrite(self):
        ssb.saveBits(self.path, {"old": {"diameter": 20.0}})
        with open(self.path) as jsonFile:
            before = jsonFile.read()

        def failingDump(data, jsonFile, **keywords):
            jsonFile.write('{"half": ')
            raise OSError("disk full")
        with mock.patch.object(ssb.json, 'dump', failingDump):
            with self.assertRaises(OSError):
                ssb.saveBits(self.path, {"new": {"diameter": 30.0}})
        with open(self.path) as jsonFile:
            self.assertEqual(jsonFile.read(), before)
        self.assertEqual(os.listdir(self.directory), ["bits.json"])   # no temp file left

    def testBitsPathNextToProfile(self):
        self.assertEqual(ssb.bitsPath(os.path.join(self.directory, "6060.json")), self.path)


class Operator:
    """Answers prompts from a script of (expected substring, reply), in order."""

    def __init__(self, script):
        self.script = list(script)
        self.prompts = []

    def __call__(self, prompt, inputStream=None):
        self.prompts.append(prompt)
        if not self.script:
            raise AssertionError(f"unscripted prompt: {prompt!r}")
        expected, reply = self.script.pop(0)
        if expected not in prompt:
            raise AssertionError(f"expected a prompt containing {expected!r}, got {prompt!r}")
        return reply


class ChooseBitTests(BitDirectoryTest):
    def choose(self, script, bitName=None, diameter=None, bits=None):
        bits = {} if bits is None else bits
        operator = Operator(script)
        output = []
        with redirect_stdout(io.StringIO()):
            result = ssb.chooseBit(bits, self.path, bitName, diameter, operator, output.append)
        self.assertEqual(operator.script, [], "not every scripted answer was used")
        return result, output

    def saved(self):
        return {"25mm": {"diameter": 25.0}, "35mm-flat": {"diameter": 35.0, "feed": 250}}

    def testBitByName(self):
        (name, entry), _ = self.choose([], bitName="35mm-flat", bits=self.saved())
        self.assertEqual((name, entry['feed']), ("35mm-flat", 250))

    def testUnknownBit(self):
        with self.assertRaisesRegex(ssb.PlanRefused, "No bit called 'nope'.*25mm, 35mm-flat"):
            self.choose([], bitName="nope", bits=self.saved())

    def testPickByNumber(self):
        (name, entry), output = self.choose([("Pick a bit (1-2)", "7"), ("Pick a bit", "2")],
                                            bits=self.saved())
        self.assertEqual(name, "35mm-flat")
        self.assertIn("    1. 25mm: 25 mm", output)
        self.assertIn("    2. 35mm-flat: 35 mm (feed 250)", output)

    def testMenuIsSmallestFirst(self):
        # By diameter, not by name: "100mm" would sort before "25mm" as text.
        (name, entry), output = self.choose([("Pick a bit (1-2)", "1")],
                                            bits={"100mm": {"diameter": 100.0},
                                                  "25mm": {"diameter": 25.0}})
        self.assertEqual(name, "25mm")
        self.assertIn("    2. 100mm: 100 mm", output)

    def testAddNewBitFromMenu(self):
        bits = self.saved()
        (name, entry), _ = self.choose([("Pick a bit", "a"), ("Name for the new bit", "25mm"),
                                        ("Name for the new bit", "50mm-flat"),
                                        ("Bit diameter", "50"), ("flutes", "")], bits=bits)
        self.assertEqual((name, entry), ("50mm-flat", {"diameter": 50.0}))
        self.assertEqual(ssb.loadBits(self.path)["50mm-flat"], {"diameter": 50.0})

    def testEmptyListGoesStraightToAdd(self):
        (name, entry), _ = self.choose([("Name for the new bit", "35mm-flat"),
                                        ("Bit diameter", "35"), ("flutes", "2")])
        self.assertEqual(entry, {"diameter": 35.0, "flutes": 2})
        self.assertEqual(ssb.loadBits(self.path), {"35mm-flat": {"diameter": 35.0, "flutes": 2}})

    def testDiameterOffersSave(self):
        (name, entry), _ = self.choose([("Save this 35 mm bit", "n")], diameter=35.0)
        self.assertEqual((name, entry), (None, {"diameter": 35.0}))
        self.assertFalse(os.path.exists(self.path))
        (name, entry), _ = self.choose([("Save this 35 mm bit", "y"), ("Name", "35mm-flat"),
                                        ("flutes", "")], diameter=35.0)
        self.assertEqual(name, "35mm-flat")
        self.assertEqual(ssb.loadBits(self.path), {"35mm-flat": {"diameter": 35.0}})


class FakeGrbl:
    def __init__(self, settings=None):
        self.settings = dict(settings or SETTINGS)
        self.position = {'X': -619.0, 'Y': -602.0, 'Z': -3.0}
        self.state = 'Idle'
        self.replies = []
        self.lines = []          # G-code lines streamed through write()
        self.sent = []           # GrblController.send() calls
        self.raw = []
        self.timeout = 30
        self.resets = 0
        self.interruptAt = None  # substring: raise KeyboardInterrupt when that line is written
        self.errorAt = None      # substring: answer error:15 to that line
        self.homed = 0
        self.events = []         # every send() and streamed line, in order

    # --- GrblController surface ---
    def getSettings(self):
        return dict(self.settings)

    def send(self, gcode):
        self.sent.append(gcode)
        self.events.append(gcode)
        match = re.match(r'(\$\d+)=(.*)$', gcode)
        if match:
            self.settings[match.group(1)] = f"{float(match.group(2)):.3f}"

    def getGrblState(self):
        return self.state

    def getMachineCoordinates(self):
        return types.SimpleNamespace(x=self.position['X'], y=self.position['Y'], z=self.position['Z'])

    def softReset(self):
        self.resets += 1
        self.state = 'Idle'

    def tryHomingCycle(self, homingPosition):
        self.homed += 1
        self.position = {'X': -619.0, 'Y': -602.0, 'Z': -3.0}
        return None

    # --- raw serial ---
    def status(self):
        state = 'Hold:0' if self.state == 'Hold' else self.state
        p = self.position
        return f"<{state}|MPos:{p['X']:.3f},{p['Y']:.3f},{p['Z']:.3f}|FS:0,0>\r\n".encode()

    def write(self, data):
        self.raw.append(data)
        if data == b'?':
            self.replies.append(self.status())
            return
        if data == b'!':
            if self.state == 'Run':
                self.state = 'Hold'
            return
        line = data.decode().strip()
        self.lines.append(line)
        self.events.append(line)
        if self.interruptAt and self.interruptAt in line:
            self.state = 'Run'
            raise KeyboardInterrupt()
        if self.errorAt and self.errorAt in line:
            self.replies.append(b"error:15\r\n")
            return
        if line.startswith('G53'):
            for axis, value in re.findall(r'([XYZ])(-?[\d.]+)', line):
                value = float(value)
                low, high = jog.softLimitBounds(self.settings)[axis]
                if not -float(self.settings[jog.TRAVEL_SETTINGS[axis]]) <= value <= 0:
                    raise AssertionError(f"{line}: outside the soft limits (ALARM:2)")
                self.position[axis] = value
        self.replies.append(b"ok\r\n")

    def readline(self):
        return self.replies.pop(0) if self.replies else b""


def fakeJog(grbl, positions):
    remaining = list(positions)

    def runJog(grblArgument, homed, out=print):
        assert homed
        x, y, z = remaining.pop(0)
        grbl.position.update(X=x, Y=y, Z=z)
        return jog.ACCEPTED
    runJog.remaining = remaining
    return runJog


# Prompt sequences that recur, for a run with --diameter 35 (not saved)
START = [("Save this 35 mm bit", "n"), ("Press Enter to home", ""),
         ("FRONT-LEFT", ""), ("BACK-RIGHT", ""), ("Save these corners", "n"),
         ("just touches", "")]
PARAMETERS = [("Stepover in mm [14,", ""), ("Depth per pass in mm [0.3,", ""),
              ("Feed in mm/min [286, from 35 mm bit]", ""), ("Plunge feed in mm/min [50,", ""),
              ("Spindle speed (S word) [10000,", ""), ("Clearance in mm [5,", "")]
FIRST_PASS = [("y to start", "y"), ("Pass 1: -0.3 mm (total -0.3)", "")]
PARK = [("Press Enter to park", "")]
END = [("Save this 35 mm bit and these settings", "n"),
       ("Set the spoilboard Z limit from the surfaced top? [Y/n]", "n")]


class FlowTestBase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory)
        self.profilePath = os.path.join(self.directory, "6060.json")
        self.profile = MachineProfile(name="6060", workAreaZ=109.0,
                                      expectedSettings={"$23": "3", "$132": "109.000"})
        self.profile.save(self.profilePath)
        self.bitsPath = os.path.join(self.directory, "bits.json")
        # The Grbl-switched spindle is stored, so these flows never ask for it.
        self.machinePath = os.path.join(self.directory, "6060.machine.json")
        ssb.writeJsonAtomically(self.machinePath, {"schemaVersion": 1, "spindleControl": "grbl"})
        self.output = []

    def run_flow(self, script, arguments=("--diameter", "35"), grbl=None, positions=None,
                 expectException=None):
        grbl = grbl or FakeGrbl()
        options = ssb.parseArguments(["--profile", self.profilePath, *arguments])
        operator = Operator(script)
        runJog = fakeJog(grbl, positions or [CORNER1, CORNER2, TOUCH])
        surfacing = ssb.Surfacing(grbl, self.profile, self.profilePath, options, ask=operator,
                                  out=self.output.append, runJog=runJog,
                                  checkEnvironment=lambda out=print: jog.ENVIRONMENT_OK,
                                  sleep=lambda seconds: None)
        homes = []
        with redirect_stdout(io.StringIO()):
            if expectException:
                with self.assertRaises(expectException):
                    surfacing.runSafely(lambda: homes.append(1) or grbl.tryHomingCycle('3'))
            else:
                surfacing.runSafely(lambda: homes.append(1) or grbl.tryHomingCycle('3'))
        self.assertEqual(operator.script, [], "not every scripted answer was used")
        self.homes = homes
        return grbl, surfacing, operator

    def text(self):
        return "\n".join(self.output)


class FlowTests(FlowTestBase):
    def testOnePassAndPark(self):
        grbl, surfacing, operator = self.run_flow(
            START + PARAMETERS + FIRST_PASS + [("Another pass at -0.3 mm (total -0.6)", "s")]
            + PARK + END)
        self.assertEqual(self.homes, [1])
        self.assertIn("Knob".lower(), self.text().lower())
        self.assertIn("G10 L20 P1 Z0", grbl.sent)
        self.assertIn("  3. Touch-off: plywood top at MPos z=-80.000 (work Z0)", self.output)
        self.assertIn("  6. Feed: 286 mm/min (from 35 mm bit; override with --feed)", self.output)
        lines = grbl.lines
        m3 = lines.index("M3 S10000")
        plunge = next(i for i, line in enumerate(lines) if line.startswith("G53 G1 Z"))
        self.assertLess(m3, plunge)
        self.assertIn("G53 G1 Z-80.300 F50", lines)
        self.assertEqual(lines[-4:], ["M5", "G53 G0 Z-75.000", "G53 G0 Z-3.000",
                                      "G53 G0 X-600.000 Y-580.000"])
        self.assertEqual(grbl.position, {'X': -600.0, 'Y': -580.0, 'Z': -3.0})
        self.assertEqual(surfacing.totalDepth, 0.3)
        self.assertIn("Total depth removed: 0.300 mm in 1 pass.", self.text())
        self.assertIn("hearing and eye protection", self.text())
        self.assertIn("SIDEWAYS", self.text())

    def testNothingMovesBeforeEnter(self):
        # Ctrl-C at the "Press Enter to home" prompt: no homing, no motion.
        grbl = FakeGrbl()
        ask = Operator([("Save this 35 mm bit", "n")])

        def stopAtHome(prompt, inputStream=None):
            if "Press Enter to home" in prompt:
                raise KeyboardInterrupt()
            return ask(prompt)
        options = ssb.parseArguments(["--profile", self.profilePath, "--diameter", "35"])
        surfacing = ssb.Surfacing(grbl, self.profile, self.profilePath, options, ask=stopAtHome,
                                  out=self.output.append, runJog=fakeJog(grbl, []),
                                  checkEnvironment=lambda out=print: jog.ENVIRONMENT_OK,
                                  sleep=lambda seconds: None)
        with self.assertRaises(KeyboardInterrupt):
            surfacing.runSafely(grbl.tryHomingCycle)
        self.assertEqual(grbl.homed, 0)
        self.assertEqual([line for line in grbl.lines if line.startswith("G53")], [])

    def testJogUnavailableStops(self):
        grbl = FakeGrbl()
        options = ssb.parseArguments(["--profile", self.profilePath, "--diameter", "35"])
        surfacing = ssb.Surfacing(grbl, self.profile, self.profilePath, options, ask=Operator([]),
                                  out=self.output.append, runJog=fakeJog(grbl, []),
                                  checkEnvironment=lambda out=print: jog.UNAVAILABLE,
                                  sleep=lambda seconds: None)
        with self.assertRaisesRegex(ssb.PlanRefused, "Keyboard jog is unavailable"):
            surfacing.runSafely(grbl.tryHomingCycle)
        self.assertEqual(grbl.homed, 0)

    def testSecondCornerMustBeUpAndRight(self):
        grbl, surfacing, operator = self.run_flow(
            START[:4] + [("FRONT-LEFT", ""), ("BACK-RIGHT", ""), ("Save these corners", "n"),
                         ("just touches", "")]
            + PARAMETERS + [("y to start", "q"), ("Press Enter to park", "")],
            positions=[CORNER2, CORNER1, CORNER1, CORNER2, TOUCH])
        self.assertIn("is not to the right of", self.text())
        self.assertIn("is not behind", self.text())
        self.assertEqual(surfacing.choices['board'], BOARD)

    def testTwoPassesThenCapStops(self):
        # Cap 0.5: 0.3, then a 0.3 is refused; change to 0.2; then the cap is reached.
        grbl, surfacing, operator = self.run_flow(
            START + PARAMETERS + FIRST_PASS
            + [("Change the depth, or stop", "c"), ("How much deeper for the next pass", "0.2")]
            + PARK + END,
            arguments=("--diameter", "35", "--max-total-depth", "0.5"))
        self.assertEqual(surfacing.totalDepth, 0.5)
        self.assertEqual(surfacing.passesDone, 2)
        self.assertIn("past the 0.5 mm cap", self.text())
        self.assertIn("The total-depth cap is reached.", self.text())
        self.assertIn("G53 G1 Z-80.500 F50", grbl.lines)
        self.assertFalse(any("Z-80.700" in line for line in grbl.lines))

    def testOneWayOption(self):
        grbl, surfacing, operator = self.run_flow(
            START + PARAMETERS + FIRST_PASS + [("Another pass", "stop")] + PARK + END,
            arguments=("--diameter", "35", "--one-way"))
        self.assertIn("  10. Pattern: one-way (every line +X, lift to return)", self.output)
        cuts = cuttingXMoves(grbl.lines)
        self.assertTrue(all(end > start for start, end in cuts[:23]))


class SummaryTests(FlowTestBase):
    def testChangeAStepByNumber(self):
        grbl, surfacing, operator = self.run_flow(
            START + PARAMETERS + [("y to start", "6"), ("Feed in mm/min [286", "300"),
                                  ("y to start", "y"), ("Pass 1", ""), ("Another pass", "s")]
            + PARK + END)
        self.assertIn("  6. Feed: 300 mm/min (typed; override with --feed)", self.output)
        self.assertTrue(any(line.endswith("F300") for line in grbl.lines))
        self.assertFalse(any(line.endswith("F286") for line in grbl.lines))

    def testBackChangesTheMostRecent(self):
        # The most recent step is the start corner (11).
        grbl, surfacing, operator = self.run_flow(
            START + PARAMETERS + [("y to start", "b"), ("Start corner (front-left / back-right)",
                                                         "back"),
                                  ("y to start", "y"), ("Pass 1", ""), ("Another pass", "s")]
            + PARK + END)
        self.assertEqual(surfacing.choices['start'], ssb.BACK_RIGHT)

    def testChangeThePattern(self):
        grbl, surfacing, operator = self.run_flow(
            START + PARAMETERS + [("y to start", "10"), ("Pattern (zigzag / one-way)", "one"),
                                  ("y to start", "y"), ("Pass 1", ""), ("Another pass", "s")]
            + PARK + END)
        self.assertEqual(surfacing.choices['pattern'], ssb.ONE_WAY)

    def testChangeTheBoardJogsAgain(self):
        grbl, surfacing, operator = self.run_flow(
            START + PARAMETERS + [("y to start", "2"), ("FRONT-LEFT", ""), ("BACK-RIGHT", ""),
                                  ("Save these corners", "n"),
                                  ("y to start", "q"), ("Press Enter to park", "")],
            positions=[CORNER1, CORNER2, TOUCH, (-500.0, -500.0, -60.0), (-300.0, -300.0, -60.0)])
        self.assertEqual(surfacing.choices['board'], (-500.0, -500.0, -300.0, -300.0))

    def testQuitCutsNothingAndAsksNoSave(self):
        grbl, surfacing, operator = self.run_flow(
            START + PARAMETERS + [("y to start", "q"), ("Press Enter to park", "")])
        self.assertFalse(any(line.startswith("M3") for line in grbl.lines))
        self.assertIn("Nothing cut.", self.text())
        self.assertEqual(surfacing.passesDone, 0)   # END prompts were never asked

    def testRefusedPlanHasNoYes(self):
        grbl, surfacing, operator = self.run_flow(
            START + [("Stepover in mm [14,", "40")] + PARAMETERS[1:]
            + [("1-11 to change", "y"), ("1-11 to change", "4"), ("Stepover in mm [14,", ""),
               ("y to start", "q"), ("Press Enter to park", "")])
        self.assertIn("REFUSED", self.text())
        self.assertFalse(operator.prompts[-3].startswith("y to start"))


# --corners and --board: the corners given, so no corner jogs
CORNERS = ("--corners", "-600", "-580", "-200", "-280")     # = BOARD
GIVEN_START = [("Save this 35 mm bit", "n"), ("Press Enter to home", ""),
               ("First jog the bit over the plywood (arrows move X/Y).  Jog the bit DOWN", "")]
QUIT = [("y to start", "q"), ("Press Enter to park", "")]
CORNER_PROMPTS = ("FRONT-LEFT", "BACK-RIGHT", "Save these corners")


class CornersOptionTests(FlowTestBase):
    def refusedBeforeHoming(self, options, pattern):
        # Stopped before the bit question and before homing: nothing written to Grbl.
        grbl = FakeGrbl()
        surfacing = ssb.Surfacing(grbl, self.profile, self.profilePath, options, ask=Operator([]),
                                  out=self.output.append, runJog=fakeJog(grbl, []),
                                  checkEnvironment=lambda out=print: jog.ENVIRONMENT_OK,
                                  sleep=lambda seconds: None)
        with self.assertRaisesRegex(ssb.PlanRefused, pattern) as caught:
            surfacing.runSafely(grbl.tryHomingCycle)
        self.assertEqual(grbl.homed, 0)
        self.assertEqual(grbl.lines, [])
        self.assertEqual(grbl.sent, [])
        return str(caught.exception)

    def testCornersSkipTheCornerJogs(self):
        grbl, surfacing, operator = self.run_flow(
            GIVEN_START + PARAMETERS + FIRST_PASS + [("Another pass", "s")] + PARK + END,
            arguments=("--diameter", "35", *CORNERS), positions=[TOUCH])
        self.assertFalse([p for p in operator.prompts if any(c in p for c in CORNER_PROMPTS)])
        self.assertEqual(surfacing.choices['board'], BOARD)
        self.assertIn("  Corners from --corners: front-left X-600.000 Y-580.000, back-right "
                      "X-200.000 Y-280.000; board 400.000 x 300.000 mm.", self.output)
        self.assertIn("  2. Board: front-left X-600.000 Y-580.000, back-right X-200.000 Y-280.000 "
                      "(400.000 x 300.000 mm) (from --corners)", self.output)
        # The plan uses the given corners: the first line starts at x0 - overrun on y0,
        # the lines run to x1 + overrun, and the last one is on y1.
        self.assertIn("G53 G0 X-617.500 Y-580.000", grbl.lines)
        self.assertIn("G53 G1 X-182.500 F286", grbl.lines)
        self.assertIn("G53 G1 Y-280.000 F286", grbl.lines)
        self.assertIn("G10 L20 P1 Z0", grbl.sent)          # the touch-off still happened
        self.assertFalse(os.path.exists(ssb.boardsPath(self.profilePath)))  # no save offer

    def testCornersOutOfOrderRefusedByTheParser(self):
        stderr = io.StringIO()
        with mock.patch('sys.stderr', stderr), self.assertRaises(SystemExit):
            ssb.parseArguments(["--corners", "-200", "-280", "-600", "-580"])
        self.assertIn("X-600.000 is not to the right of X-200.000", stderr.getvalue())
        self.assertIn("Y-580.000 is not behind Y-280.000", stderr.getvalue())

    def testCornersOutOfOrderRefusedBeforeHoming(self):
        # Surfacing used without parseArguments' check (say, from another script).
        options = ssb.parseArguments(["--profile", self.profilePath, "--diameter", "35"])
        options.corners = (-200.0, -580.0, -600.0, -280.0)
        message = self.refusedBeforeHoming(options, "out of order")
        self.assertIn("Please run this again with corrected --corners.", message)

    def testCornersOutsideSoftLimitsRefused(self):
        # X travel is -619..0 (minus the 0.01 mm margin); -700 is past it.
        options = ssb.parseArguments(["--profile", self.profilePath, "--diameter", "35",
                                      "--corners", "-700", "-580", "-200", "-280"])
        message = self.refusedBeforeHoming(options, "outside the soft-limit travel")
        self.assertIn("X must be from -618.990 to -0.010", message)

    def testPlanStillRefusesCornersOutsideSoftLimits(self):
        with self.assertRaisesRegex(ssb.PlanRefused, "outside the soft-limit travel"):
            plan(board=(-700.0, -580.0, -200.0, -280.0))

    def testJogUnavailableSaysOnlyTheTouchOffNeedsIt(self):
        grbl = FakeGrbl()
        options = ssb.parseArguments(["--profile", self.profilePath, "--diameter", "35", *CORNERS])
        surfacing = ssb.Surfacing(grbl, self.profile, self.profilePath, options, ask=Operator([]),
                                  out=self.output.append, runJog=fakeJog(grbl, []),
                                  checkEnvironment=lambda out=print: jog.UNAVAILABLE,
                                  sleep=lambda seconds: None)
        with self.assertRaisesRegex(ssb.PlanRefused, "Keyboard jog is unavailable, and the "
                                                     "touch-off needs it"):
            surfacing.runSafely(grbl.tryHomingCycle)

    def testChangingTheBoardJogsTheCorners(self):
        grbl, surfacing, operator = self.run_flow(
            GIVEN_START + PARAMETERS
            + [("y to start", "2"), ("FRONT-LEFT", ""), ("BACK-RIGHT", ""),
               ("Save these corners", "n")] + QUIT,
            arguments=("--diameter", "35", *CORNERS),
            positions=[TOUCH, (-500.0, -500.0, -60.0), (-300.0, -300.0, -60.0)])
        self.assertEqual(surfacing.choices['board'], (-500.0, -500.0, -300.0, -300.0))


class SavedBoardTests(FlowTestBase):
    # self.profile is named "6060", so boards are saved under that machine.
    def boards(self, machine="6060"):
        return ssb.loadBoards(ssb.boardsPath(self.profilePath)).get(machine, {})

    def saveOne(self, name="600x400", corners=BOARD, machine="6060"):
        x0, y0, x1, y1 = corners
        path = ssb.boardsPath(self.profilePath)
        machines = ssb.loadBoards(path)
        machines.setdefault(machine, {})[name] = {'x0': x0, 'y0': y0, 'x1': x1, 'y1': y1}
        ssb.saveBoards(path, machines)

    def refused(self, arguments, pattern):
        grbl = FakeGrbl()
        options = ssb.parseArguments(["--profile", self.profilePath, *arguments])
        surfacing = ssb.Surfacing(grbl, self.profile, self.profilePath, options, ask=Operator([]),
                                  out=self.output.append, runJog=fakeJog(grbl, []),
                                  checkEnvironment=lambda out=print: jog.ENVIRONMENT_OK,
                                  sleep=lambda seconds: None)
        with self.assertRaisesRegex(ssb.PlanRefused, pattern) as caught:
            surfacing.runSafely(grbl.tryHomingCycle)
        self.assertEqual(grbl.homed, 0)
        self.assertEqual(grbl.lines, [])
        return str(caught.exception)

    def testBoardsFileNextToProfile(self):
        self.assertEqual(ssb.boardsPath(self.profilePath),
                         os.path.join(self.directory, "boards.json"))

    def testSaveJoggedCorners(self):
        self.run_flow(START[:4] + [("Save these corners as a board for next time? [y/n]", "y"),
                                   ("Name for this board", "600x400"), ("just touches", "")]
                      + PARAMETERS + QUIT)
        self.assertEqual(self.boards(), {"600x400": {'x0': -600.0, 'y0': -580.0,
                                                     'x1': -200.0, 'y1': -280.0}})
        self.assertIn("next time use --board 600x400", self.text())

    def testReuseSavedBoard(self):
        self.saveOne()
        grbl, surfacing, operator = self.run_flow(
            GIVEN_START + PARAMETERS + FIRST_PASS + [("Another pass", "s")] + PARK + END,
            arguments=("--diameter", "35", "--board", "600x400"), positions=[TOUCH])
        self.assertFalse([p for p in operator.prompts if any(c in p for c in CORNER_PROMPTS)])
        self.assertEqual(surfacing.choices['board'], BOARD)
        self.assertIn("Corners from --board 600x400", self.text())
        self.assertIn("same place on the bed (against the same stops)", self.text())
        self.assertIn("homed", self.text().split("Reminder:", 1)[1])
        self.assertIn("(from --board 600x400)", self.text())
        self.assertIn("G53 G0 X-617.500 Y-580.000", grbl.lines)

    def testOverwriteAsksFirst(self):
        self.saveOne(corners=(-610.0, -590.0, -210.0, -290.0))
        self.run_flow(START[:4] + [("Save these corners", "y"), ("Name for this board", "600x400"),
                                   ("There is already a board 600x400: front-left X-610.000", "n"),
                                   ("Name for this board", "600x400"),
                                   ("Overwrite it? [y/n]", "y"), ("just touches", "")]
                      + PARAMETERS + QUIT)
        self.assertEqual(self.boards()["600x400"], {'x0': -600.0, 'y0': -580.0,
                                                    'x1': -200.0, 'y1': -280.0})

    def testDecliningOverwriteKeepsTheOldBoard(self):
        self.saveOne(corners=(-610.0, -590.0, -210.0, -290.0))
        self.run_flow(START[:4] + [("Save these corners", "y"), ("Name for this board", "600x400"),
                                   ("Overwrite it?", "n"), ("Name for this board", "second"),
                                   ("just touches", "")] + PARAMETERS + QUIT)
        self.assertEqual(self.boards()["600x400"]['x0'], -610.0)
        self.assertEqual(self.boards()["second"]['x0'], -600.0)

    def testUnknownBoardListsThisMachinesNames(self):
        self.saveOne("b")
        self.saveOne("a")
        self.saveOne("z", machine="4030")      # another machine's: not listed
        self.refused(["--board", "c"], r"No board called 'c' for 6060 .*\(a, b\)")

    def testSavedBoardOutsideSoftLimitsRefused(self):
        self.saveOne(corners=(-700.0, -580.0, -200.0, -280.0))
        self.refused(["--board", "600x400"], "saved board '600x400' are outside the "
                                             "soft-limit travel.*Measure it again")

    def testBoardMeasuredOnAnotherMachineRefused(self):
        # Inside this machine's soft limits, but measured on the 4030: refused anyway.
        self.saveOne(machine="4030 ProverXL V2")
        message = self.refused(["--board", "600x400"], "measured on 4030 ProverXL V2, not on 6060")
        self.assertIn("Please measure it on 6060: run this again without --board, jog the "
                      "corners, and save them.", message)

    def testSameNameOnTwoMachinesKeptApart(self):
        self.saveOne(machine="4030 ProverXL V2", corners=(-390.0, -290.0, -10.0, -10.0))
        self.saveOne()
        grbl, surfacing, operator = self.run_flow(
            GIVEN_START + PARAMETERS + QUIT,
            arguments=("--diameter", "35", "--board", "600x400"), positions=[TOUCH])
        self.assertEqual(surfacing.choices['board'], BOARD)    # the 6060's, not the 4030's

    def testSavingDoesNotDisturbAnotherMachinesBoards(self):
        self.saveOne(machine="4030 ProverXL V2", corners=(-390.0, -290.0, -10.0, -10.0))
        theirs = self.boards("4030 ProverXL V2")
        self.run_flow(START[:4] + [("Save these corners", "y"), ("Name for this board", "600x400"),
                                   ("just touches", "")] + PARAMETERS + QUIT)
        self.assertEqual(self.boards("4030 ProverXL V2"), theirs)   # no overwrite question
        self.assertEqual(self.boards()["600x400"]['x0'], -600.0)
        self.assertIn("Saved board '600x400' for 6060", self.text())

    def testMachineKeyIsTheProfileName(self):
        self.assertEqual(ssb.machineKey(self.profile, self.profilePath), "6060")
        unnamed = MachineProfile(name="", workAreaZ=109.0)
        self.assertEqual(ssb.machineKey(unnamed, "/somewhere/4030 ProverXL V2.json"),
                         "4030 ProverXL V2")

    def oldFlatFile(self):
        path = ssb.boardsPath(self.profilePath)
        old = {"schemaVersion": 1,
               "boards": {"600x400": {'x0': -600, 'y0': -580, 'x1': -200, 'y1': -280}}}
        ssb.writeJsonAtomically(path, old)
        with open(path) as jsonFile:
            return path, jsonFile.read()

    def testOldFlatFileIsWarnedAboutNotUsed(self):
        path, before = self.oldFlatFile()
        self.refused(["--board", "600x400"], "No board called '600x400' for 6060")
        self.assertIn("not in the per-machine format", self.text())
        with open(path) as jsonFile:
            self.assertEqual(jsonFile.read(), before)

    def testOldFlatFileIsNotRewrittenBySaving(self):
        path, before = self.oldFlatFile()
        self.run_flow(START[:4] + [("Save these corners", "y"), ("just touches", "")]
                      + PARAMETERS + QUIT)
        self.assertIn("Warning: could not read", self.text())
        with open(path) as jsonFile:
            self.assertEqual(jsonFile.read(), before)

    def testBoardAndCornersTogetherIsAnError(self):
        stderr = io.StringIO()
        with mock.patch('sys.stderr', stderr), self.assertRaises(SystemExit):
            ssb.parseArguments(["--board", "600x400", *CORNERS])
        self.assertIn("give --board or --corners, not both", stderr.getvalue())

    def testWriteFailureWarnsAndCarriesOn(self):
        with mock.patch.object(ssb, 'saveBoards', side_effect=OSError("disk full")):
            grbl, surfacing, operator = self.run_flow(
                START[:4] + [("Save these corners", "y"), ("Name for this board", "600x400"),
                             ("just touches", "")] + PARAMETERS + QUIT)
        self.assertIn("Warning: could not save the board", self.text())
        self.assertIn("disk full", self.text())
        self.assertEqual(surfacing.choices['board'], BOARD)
        self.assertIn("G10 L20 P1 Z0", grbl.sent)          # the run went on to the touch-off

    def testUnreadableBoardsFileWarnsAndCarriesOn(self):
        with open(ssb.boardsPath(self.profilePath), 'w') as broken:
            broken.write("{not json")
        grbl, surfacing, operator = self.run_flow(
            START[:4] + [("Save these corners", "y"), ("just touches", "")] + PARAMETERS + QUIT)
        self.assertIn("Warning: could not read", self.text())
        self.assertEqual(surfacing.choices['board'], BOARD)


class ZRoomFlowTests(FlowTestBase):
    def shallow(self, z132='80.200'):
        settings = dict(SETTINGS)
        settings['$132'] = z132          # fence z=-80.2: a 0.3 mm pass from -80 needs -80.3
        return FakeGrbl(settings)

    def savedProfile(self):
        with open(self.profilePath) as jsonFile:
            return json.load(jsonFile)

    def testResetGoesToWorkAreaZNotTheCalibratedValue(self):
        # The profile's calibrated $132 is a (shallow) spoilboard limit; the reset
        # must go to the full travel instead, and must not touch the profile.
        self.profile.expectedSettings["$132"] = "80.200"
        self.profile.save(self.profilePath)
        grbl, surfacing, operator = self.run_flow(
            START + PARAMETERS + [("y to start", "y"),
                                  ("Reset $132 to the full Z travel, 109.000", "y"),
                                  ("Pass 1", ""), ("Another pass", "s")] + PARK + END,
            grbl=self.shallow())
        self.assertIn("Z room: NOT enough", self.text())
        self.assertIn("Not enough Z room: this pass cuts down to z=-80.300", self.text())
        self.assertIn("$132=109.000", grbl.sent)
        self.assertEqual(grbl.settings['$132'], '109.000')
        self.assertEqual(surfacing.passesDone, 1)
        self.assertEqual(self.profile.expectedSettings["$132"], "80.200")
        self.assertEqual(self.savedProfile()["expectedSettings"]["$132"], "80.200")
        self.assertIn("not the profile's calibrated $132", " ".join(operator.prompts))

    def testResetDeclinedGoesBackToTheSummary(self):
        grbl, surfacing, operator = self.run_flow(
            START + PARAMETERS + [("y to start", "y"), ("Reset $132", "n"),
                                  ("y to start", "q")] + PARK, grbl=self.shallow())
        self.assertIn("Back to the summary", self.text())
        self.assertFalse(any(line.startswith("M3") for line in grbl.lines))
        self.assertEqual([gcode for gcode in grbl.sent if gcode.startswith("$132=")], [])

    def testResetNotEnough(self):
        self.profile.workAreaZ = 80.25   # even the "full travel" (-80.25) is above -80.3
        grbl, surfacing, operator = self.run_flow(
            START + PARAMETERS + [("y to start", "y"), ("Reset $132", "y"),
                                  ("y to start", "q")] + PARK, grbl=self.shallow())
        self.assertIn("Still not enough", self.text())
        self.assertEqual(surfacing.passesDone, 0)

    def testRoomCheckedBeforeEachPassForThatPassesBottom(self):
        # $132 80.5 fences z=-80.5 (-80.4 with the margin): pass 1 (to -80.3) fits
        # with no offer; pass 2 (to -80.6) gets the offer, after its "Another pass?".
        grbl, surfacing, operator = self.run_flow(
            START + PARAMETERS + FIRST_PASS + [("Another pass at -0.3 mm (total -0.6)", "y"),
                                               ("Reset $132", "y"), ("Another pass", "s")]
            + PARK + END, grbl=self.shallow('80.500'))
        self.assertIn("Z room: ok", self.text())
        self.assertIn("Not enough Z room: this pass cuts down to z=-80.600", self.text())
        prompts = operator.prompts
        firstAnother = next(i for i, p in enumerate(prompts) if p.startswith("Another pass"))
        reset = next(i for i, p in enumerate(prompts) if "Reset $132" in p)
        self.assertGreater(reset, firstAnother)
        self.assertEqual(surfacing.totalDepth, 0.6)

    def testDeclinedResetLaterOffersAShallowerPass(self):
        # After pass 1 the reset is declined: a shallower pass that fits, then stop.
        grbl, surfacing, operator = self.run_flow(
            START + PARAMETERS + FIRST_PASS + [("Another pass at -0.3 mm (total -0.6)", "y"),
                                               ("Reset $132", "n"), ("Change the depth, or stop", "c"),
                                               ("How much deeper for the next pass", "0.1"),
                                               ("Another pass", "s")] + PARK + END,
            grbl=self.shallow('80.500'))
        self.assertEqual(surfacing.totalDepth, 0.4)
        self.assertIn("G53 G1 Z-80.400 F50", grbl.lines)


class PassLoopTests(FlowTestBase):
    def runWith(self, ask, grbl):
        options = ssb.parseArguments(["--profile", self.profilePath, "--diameter", "35"])
        surfacing = ssb.Surfacing(grbl, self.profile, self.profilePath, options, ask=ask,
                                  out=self.output.append,
                                  runJog=fakeJog(grbl, [CORNER1, CORNER2, TOUCH]),
                                  checkEnvironment=lambda out=print: jog.ENVIRONMENT_OK,
                                  sleep=lambda seconds: None)
        with redirect_stdout(io.StringIO()):
            surfacing.runSafely(lambda: grbl.tryHomingCycle('3'))
        return surfacing

    def testSpindleOffAtEveryPromptAndRestartedForEveryPass(self):
        grbl = FakeGrbl()
        operator = Operator(START + PARAMETERS + FIRST_PASS + [
            ("Another pass at -0.3 mm (total -0.6)", "y"),
            ("Another pass at -0.3 mm (total -0.9)", ""),        # Enter is Y
            ("Another pass at -0.3 mm (total -1.2)", "stop")] + PARK + END)
        seen = []

        def ask(prompt, inputStream=None):
            seen.append((prompt, list(grbl.lines)))
            return operator(prompt)
        surfacing = self.runWith(ask, grbl)
        self.assertEqual(operator.script, [], "an unscripted prompt (an extra Enter?) or a missing one")
        asked = [sent for prompt, sent in seen if prompt.startswith("Another pass")]
        self.assertEqual(len(asked), 3)                           # one after every pass
        for sent in asked:
            self.assertEqual(sent[-2:], ["G53 G0 Z-75.000", "M5"], "lifted, then spindle off")
        self.assertEqual(self.output.count("  (spindle off; answering Y restarts it)"), 3)
        # Every pass, including the later ones, starts the spindle with the dwell
        # before its plunge.
        lines = grbl.lines
        starts = [i for i, line in enumerate(lines) if line == "M3 S10000"]
        self.assertEqual(len(starts), 3)
        for start, depth in zip(starts, ("80.300", "80.600", "80.900")):
            self.assertEqual(lines[start + 1], "G4 P3")
            self.assertLess(start, lines.index(f"G53 G1 Z-{depth} F50"))
        self.assertEqual(surfacing.totalDepth, 0.9)
        # Only the first pass asked for an Enter.
        self.assertEqual(sum("Press Enter to start." in prompt for prompt, _ in seen), 1)

    def pipe(self):
        reader, writer = os.pipe()
        readFile = os.fdopen(reader, 'rb', buffering=0)
        self.addCleanup(readFile.close)
        return readFile, writer

    def testWaitsThroughALongSilenceThenRunsTheNextPass(self):
        import threading
        grbl = FakeGrbl()
        readFile, writer = self.pipe()
        operator = Operator(START + PARAMETERS + FIRST_PASS + [
            ("Another pass at -0.3 mm (total -0.9)", "s")] + PARK + END)
        waited = {}

        def answerLater():
            time.sleep(1.0)                    # a long pause by test standards
            os.write(writer, b"y\n")
            os.close(writer)

        def ask(prompt, inputStream=None):
            if prompt.startswith("Another pass at -0.3 mm (total -0.6)"):
                linesBefore = len(grbl.raw)
                threading.Thread(target=answerLater, daemon=True).start()
                began = time.monotonic()
                with redirect_stdout(io.StringIO()):
                    answer = calibrate.readAnswer(prompt, readFile)   # the real, blocking reader
                waited['seconds'] = time.monotonic() - began
                waited['sentMeanwhile'] = len(grbl.raw) - linesBefore
                return answer
            return operator(prompt)
        surfacing = self.runWith(ask, grbl)
        self.assertGreaterEqual(waited['seconds'], 0.9)
        self.assertEqual(waited['sentMeanwhile'], 0)        # nothing polled, nothing moved
        self.assertEqual(surfacing.totalDepth, 0.6)          # the "y" ran pass 2
        self.assertEqual(operator.script, [])

    def testEndOfInputStops(self):
        grbl = FakeGrbl()
        readFile, writer = self.pipe()
        os.close(writer)                                     # EOF at once
        operator = Operator(START + PARAMETERS + FIRST_PASS + PARK + END)

        def ask(prompt, inputStream=None):
            if prompt.startswith("Another pass"):
                with redirect_stdout(io.StringIO()):
                    return calibrate.readAnswer(prompt, readFile)
            return operator(prompt)
        surfacing = self.runWith(ask, grbl)
        self.assertIn("  (input closed: stopping)", self.output)
        self.assertEqual(surfacing.passesDone, 1)
        self.assertEqual(operator.script, [])
        self.assertEqual(grbl.position, {'X': -600.0, 'Y': -580.0, 'Z': -3.0})   # parked

    def testM5BeforeTheZLimitWriteThenSpinUpAgain(self):
        settings = dict(SETTINGS)
        settings['$132'] = '80.500'
        grbl, surfacing, operator = self.run_flow(START + PARAMETERS + FIRST_PASS + [
            ("Another pass at -0.3 mm (total -0.6)", "y"), ("Reset $132", "y"),
            ("Another pass", "s")] + PARK + END, grbl=FakeGrbl(settings))
        events = grbl.events
        write = events.index("$132=109.000")
        self.assertEqual([e for e in events[:write] if e in ("M3 S10000", "M5")][-1], "M5",
                         "the spindle must be stopped before the $132 write")
        after = events[write:]
        m3 = after.index("M3 S10000")
        self.assertEqual(after[m3 + 1], "G4 P3")      # restarted with the dwell
        self.assertLess(m3, after.index("G53 G1 Z-80.600 F50"))

    def testResetStopsARunningSpindleFirst(self):
        # A backstop: every pass already ends with M5, but if the spindle were
        # running when the reset is accepted, M5 must still go before the write.
        settings = dict(SETTINGS)
        settings['$132'] = '80.200'
        grbl = FakeGrbl(settings)
        options = ssb.parseArguments(["--profile", self.profilePath, "--diameter", "35"])
        surfacing = ssb.Surfacing(grbl, self.profile, self.profilePath, options,
                                  ask=Operator([("Reset $132", "y")]), out=self.output.append,
                                  sleep=lambda seconds: None)
        surfacing.choices['touch'] = TOUCH_Z
        surfacing.spindleOn = True
        self.assertTrue(surfacing.ensureZRoom(0.3))
        self.assertEqual(grbl.events, ["M5", "$132=109.000"])
        self.assertFalse(surfacing.spindleOn)

    def testCtrlCInTheSecondPassStillStops(self):
        grbl = FakeGrbl()
        grbl.interruptAt = "G53 G1 Z-80.600"
        self.run_flow(START + PARAMETERS + FIRST_PASS
                      + [("Another pass at -0.3 mm (total -0.6)", "y")], grbl=grbl,
                      expectException=KeyboardInterrupt)
        self.assertIn(b'!', grbl.raw)
        self.assertEqual(grbl.resets, 1)


class SurfacedZLimitTests(FlowTestBase):
    TWO_PASSES = (START + PARAMETERS + FIRST_PASS
                  + [("Another pass at -0.3 mm (total -0.6)", "y"), ("Another pass", "s")]
                  + PARK + [("Save this 35 mm bit and these settings", "n")])

    def sidecarPath(self):
        return calibrate.zLimitPath(self.profilePath)

    def savedProfile(self):
        with open(self.profilePath) as jsonFile:
            return json.load(jsonFile)

    def testArithmeticAndRecordsOnConfirm(self):
        # Touch-off -80, two 0.3 mm passes: the new top is -80.6.  1 mm into it:
        # the fence at -81.6, so $132 = 81.6.
        grbl, surfacing, operator = self.run_flow(self.TWO_PASSES + [
            ("Set the spoilboard Z limit from the surfaced top? [Y/n]", ""),
            ("Depth to cut into the spoilboard", "1"), ("y to write it", "y")])
        self.assertEqual(surfacing.surfacedTop(), -80.6)
        self.assertIn("Surface measurement: surfaced top at z=-80.600 (given)", self.text())
        self.assertIn("Lowest spindle z = surface z - into-depth = -81.600", self.text())
        self.assertIn("$132=81.600", grbl.sent)
        self.assertEqual(grbl.settings['$132'], '81.600')
        self.assertEqual(self.savedProfile()["expectedSettings"]["$132"], "81.600")
        with open(self.sidecarPath()) as jsonFile:
            record = json.load(jsonFile)["spoilboard"]
        self.assertEqual(record["measuredBy"], "surfaced")
        self.assertEqual(record["bit"], "35 mm (not saved)")
        self.assertEqual((record["surfaceZ"], record["intoDepth"], record["zLimit"]),
                         (-80.6, 1.0, 81.6))
        self.assertEqual((record["touchOffZ"], record["surfacedDepth"]), (-80.0, 0.6))
        self.assertIn("only valid for the bit fitted now", self.text())
        # The spindle was off before the $132 write.
        events = grbl.events
        self.assertEqual([e for e in events[:events.index("$132=81.600")]
                          if e in ("M3 S10000", "M5")][-1], "M5")

    def testChangeYourMindThenWrite(self):
        grbl, surfacing, operator = self.run_flow(self.TWO_PASSES + [
            ("Set the spoilboard Z limit", "y"), ("Depth to cut into the spoilboard", "1"),
            ("1-3 to change", "3"), ("Depth to cut into the spoilboard", "2"),
            ("y to write it", "y")])
        self.assertIn("$132=82.600", grbl.sent)
        self.assertNotIn("$132=81.600", grbl.sent)

    def testNothingWrittenWithoutConfirm(self):
        before = self.savedProfile()
        grbl, surfacing, operator = self.run_flow(self.TWO_PASSES + [
            ("Set the spoilboard Z limit", ""), ("Depth to cut into the spoilboard", "1"),
            ("y to write it", "q")])
        self.assertEqual([gcode for gcode in grbl.sent if gcode.startswith("$132=")], [])
        self.assertEqual(self.savedProfile(), before)
        self.assertFalse(os.path.exists(self.sidecarPath()))

    def testDeclined(self):
        before = self.savedProfile()
        grbl, surfacing, operator = self.run_flow(self.TWO_PASSES + [
            ("Set the spoilboard Z limit", "n")])
        self.assertEqual(self.savedProfile(), before)
        self.assertFalse(os.path.exists(self.sidecarPath()))


class SpindleControlTests(BitDirectoryTest):
    def testAskedOnceThenSaved(self):
        path = os.path.join(self.directory, "m.machine.json")
        operator = Operator([("switched by Grbl (M3/M5) or by hand", "router"),   # not a choice
                             ("switched by Grbl (M3/M5) or by hand", "man")])
        output = []
        with redirect_stdout(io.StringIO()):
            self.assertEqual(ssb.resolveSpindleControl(None, path, operator, output.append),
                             ("manual", "asked"))
        with open(path) as jsonFile:
            self.assertEqual(json.load(jsonFile), {"schemaVersion": 1, "spindleControl": "manual"})
        # Read back without asking (an Operator with no script fails on any prompt).
        self.assertEqual(ssb.resolveSpindleControl(None, path, Operator([]), output.append),
                         ("manual", "saved"))

    def testCommandLineWinsAndIsNotSaved(self):
        path = os.path.join(self.directory, "m.machine.json")
        ssb.writeJsonAtomically(path, {"schemaVersion": 1, "spindleControl": "manual"})
        self.assertEqual(ssb.resolveSpindleControl("grbl", path, Operator([])),
                         ("grbl", "command line"))
        self.assertEqual(ssb.loadMachineRecord(path)["spindleControl"], "manual")
        self.assertEqual(ssb.parseArguments(["--spindle-control", "manual"]).spindle_control,
                         "manual")

    def testShippedMachineFile(self):
        record = ssb.loadMachineRecord(os.path.join(HERE, "profiles", "6060 ProverXL V1.machine.json"))
        self.assertEqual(record["spindleControl"], "manual")
        self.assertEqual(ssb.machinePath(os.path.join("profiles", "6060 ProverXL V1.json")),
                         os.path.join("profiles", "6060 ProverXL V1.machine.json"))

    def testNoDwellLineWithoutADwell(self):
        self.assertNotIn("G4 P0", plan(dwell=0).gcode)
        self.assertIn("M3 S10000", plan(dwell=0).gcode)     # still sent: harmless, standard


class ManualSpindleFlowTests(FlowTestBase):
    # Manual mode: no spindle-speed prompt, the router-on Enter before the first
    # plunge, and no dwell.
    PARAMETERS = [entry for entry in PARAMETERS if not entry[0].startswith("Spindle speed")]

    def setUp(self):
        super().setUp()
        ssb.writeJsonAtomically(self.machinePath, {"schemaVersion": 1, "spindleControl": "manual"})

    def recordedRun(self, script, grbl=None, expectException=None):
        grbl = grbl or FakeGrbl()
        operator = Operator(script)
        seen = []

        def ask(prompt, inputStream=None):
            seen.append((prompt, list(grbl.lines)))
            return operator(prompt)
        options = ssb.parseArguments(["--profile", self.profilePath, "--diameter", "35"])
        surfacing = ssb.Surfacing(grbl, self.profile, self.profilePath, options, ask=ask,
                                  out=self.output.append,
                                  runJog=fakeJog(grbl, [CORNER1, CORNER2, TOUCH]),
                                  checkEnvironment=lambda out=print: jog.ENVIRONMENT_OK,
                                  sleep=lambda seconds: None)
        with redirect_stdout(io.StringIO()):
            if expectException:
                with self.assertRaises(expectException):
                    surfacing.runSafely(lambda: grbl.tryHomingCycle('3'))
            else:
                surfacing.runSafely(lambda: grbl.tryHomingCycle('3'))
        self.assertEqual(operator.script, [], "not every scripted answer was used")
        return grbl, surfacing, seen

    def testRouterOnBeforeTheFirstPlungeAndNoDwell(self):
        grbl, surfacing, seen = self.recordedRun(
            START + self.PARAMETERS + FIRST_PASS + [("Switch the router ON now, then press Enter", ""),
                                                    ("Router ON? Another pass at -0.3 mm (total -0.6)", "y"),
                                                    ("Router ON? Another pass", "s"),
                                                    ("Switch the router OFF now", "")] + END)
        lines = grbl.lines
        self.assertNotIn("G4 P3", lines)
        self.assertFalse(any(line.startswith("G4") for line in lines))
        self.assertEqual(lines.count("M3 S10000"), 2)        # still sent, once per pass
        routerOn = [sent for prompt, sent in seen if prompt.startswith("Switch the router ON")]
        self.assertEqual(len(routerOn), 1)                   # only before the first pass
        sentBefore = routerOn[0]
        self.assertEqual(sentBefore[-2:], ["G53 G0 Z-75.000", "M3 S10000"])   # lifted first
        self.assertFalse(any(line.startswith("G53 G1") for line in sentBefore))
        self.assertIn("G53 G1 Z-80.300 F50", lines)
        self.assertIn("  (answer Y only with the router running)", self.output)
        asked = [prompt for prompt, sent in seen if "Another pass" in prompt]
        self.assertEqual(asked, ["Router ON? Another pass at -0.3 mm (total -0.6)? "
                                 "[Y / change depth / stop]: ",
                                 "Router ON? Another pass at -0.3 mm (total -0.9)? "
                                 "[Y / change depth / stop]: "])
        self.assertNotIn("  (spindle off; answering Y restarts it)", self.output)
        self.assertIn("router keeps", self.text())
        summary = next(line for line in self.output if line.startswith("  8. Spindle:"))
        self.assertIn("control: manual (saved in 6060.machine.json)", summary)
        self.assertEqual(surfacing.totalDepth, 0.6)

    def testChangedDepthIsConfirmedWithRouterOn(self):
        # "change depth" must not cut straight away in manual mode: the new
        # numbers come back as a "Router ON?" question first.
        grbl, surfacing, seen = self.recordedRun(
            START + self.PARAMETERS + FIRST_PASS + [
                ("Switch the router ON now", ""),
                ("Router ON? Another pass at -0.3 mm (total -0.6)", "c"),
                ("How much deeper for the next pass", "0.2"),
                ("Router ON? Another pass at -0.2 mm (total -0.5)", "y"),
                ("Router ON? Another pass at -0.2 mm (total -0.7)", "s"),
                ("Switch the router OFF now", "")] + END)
        confirm = next(sent for prompt, sent in seen
                       if prompt.startswith("Router ON? Another pass at -0.2 mm (total -0.5)"))
        self.assertNotIn("G53 G1 Z-80.500 F50", confirm)      # nothing cut before the Y
        self.assertIn("G53 G1 Z-80.500 F50", grbl.lines)
        self.assertEqual(surfacing.totalDepth, 0.5)

    def testChangedDepthWithAMinusSign(self):
        # The "Router ON? Another pass at -0.05 mm" question is the
        # confirmation here, so there is no separate "Next pass:" line.
        grbl, surfacing, seen = self.recordedRun(
            START + self.PARAMETERS + FIRST_PASS + [
                ("Switch the router ON now", ""),
                ("Router ON? Another pass at -0.3 mm (total -0.6)", "c"),
                ("How much deeper for the next pass", "-0.05"),
                ("Router ON? Another pass at -0.05 mm (total -0.35)", "y"),
                ("Router ON? Another pass at -0.05 mm (total -0.4)", "s"),
                ("Switch the router OFF now", "")] + END)
        self.assertEqual(surfacing.totalDepth, 0.35)
        self.assertFalse(any("Next pass:" in line for line in self.output))

    def testShallowerPassAfterADeclinedResetIsConfirmedToo(self):
        settings = dict(SETTINGS)
        settings['$132'] = '80.500'
        grbl, surfacing, seen = self.recordedRun(
            START + self.PARAMETERS + FIRST_PASS + [
                ("Switch the router ON now", ""),
                ("Router ON? Another pass at -0.3 mm (total -0.6)", "y"), ("Reset $132", "n"),
                ("Change the depth, or stop", "c"), ("How much deeper for the next pass", "0.1"),
                ("Router ON? Another pass at -0.1 mm (total -0.4)", "y"),
                ("Router ON? Another pass", "s"), ("Switch the router OFF now", "")] + END,
            grbl=FakeGrbl(settings))
        self.assertEqual(surfacing.totalDepth, 0.4)

    def testCtrlCSaysTheRouterIsStillRunning(self):
        grbl = FakeGrbl()
        grbl.interruptAt = "G53 G1 Y"
        grbl, surfacing, seen = self.recordedRun(
            START + self.PARAMETERS + FIRST_PASS + [("Switch the router ON now", "")],
            grbl=grbl, expectException=KeyboardInterrupt)
        self.assertIn(ssb.ROUTER_STILL_RUNNING, self.output)
        self.assertIn("soft reset (Ctrl-X)", self.text())
        self.assertNotIn("which also stops the spindle", self.text().split("!! Stopped")[-1])

    def testFirstRunAsksAndSaves(self):
        os.unlink(self.machinePath)
        grbl, surfacing, seen = self.recordedRun(
            [("Save this 35 mm bit", "n"), ("switched by Grbl (M3/M5) or by hand", "manual")]
            + START[1:] + self.PARAMETERS + [("y to start", "q"), ("Press Enter to park", "")])
        self.assertEqual(ssb.loadMachineRecord(self.machinePath)["spindleControl"], "manual")
        self.assertIn("asked once, now saved", self.text())


class GrblSpindleUnchangedTests(FlowTestBase):
    def testGrblModeKeepsDwellAndAsksNoRouterQuestions(self):
        grbl, surfacing, operator = self.run_flow(
            START + PARAMETERS + FIRST_PASS + [("Another pass", "y"), ("Another pass", "s")]
            + PARK + END)
        self.assertEqual(grbl.lines.count("G4 P3"), 2)
        self.assertFalse(any("router" in prompt.lower() for prompt in operator.prompts))
        summary = next(line for line in self.output if line.startswith("  8. Spindle:"))
        self.assertIn("control: grbl (saved in 6060.machine.json)", summary)

    def testGrblPromptHasNoRouterWordingAndAChangedDepthCutsAtOnce(self):
        grbl, surfacing, operator = self.run_flow(
            START + PARAMETERS + FIRST_PASS + [("Another pass at -0.3 mm (total -0.6)", "c"),
                                               ("How much deeper for the next pass", "0.2"),
                                               ("Another pass at -0.2 mm (total -0.7)", "s")]
            + PARK + END)
        another = [prompt for prompt in operator.prompts if "Another pass" in prompt]
        self.assertEqual(another[0], "Another pass at -0.3 mm (total -0.6)? "
                                     "[Y / change depth / stop]: ")
        self.assertFalse(any("Router ON?" in prompt for prompt in operator.prompts))
        self.assertIn("  (spindle off; answering Y restarts it)", self.output)
        self.assertEqual(surfacing.totalDepth, 0.5)

    def testCommandLineOverridesTheSavedMode(self):
        ssb.writeJsonAtomically(self.machinePath, {"schemaVersion": 1, "spindleControl": "manual"})
        grbl, surfacing, operator = self.run_flow(
            START + PARAMETERS + [("y to start", "q"), ("Press Enter to park", "")],
            arguments=("--diameter", "35", "--spindle-control", "grbl"))
        self.assertEqual(surfacing.spindleControl, "grbl")
        self.assertEqual(ssb.loadMachineRecord(self.machinePath)["spindleControl"], "manual")


class SafetyTests(FlowTestBase):
    def testCtrlCDuringPassHoldsResetsAndReports(self):
        grbl = FakeGrbl()
        grbl.interruptAt = "G53 G1 Y"   # the first Y step, mid-raster
        self.run_flow(START + PARAMETERS + FIRST_PASS, grbl=grbl,
                      expectException=KeyboardInterrupt)
        self.assertIn(b'!', grbl.raw)
        self.assertEqual(grbl.resets, 1)
        self.assertIn("feed hold (!)", self.text())
        self.assertIn("Machine state: Idle", self.text())
        self.assertIn("M3 S10000", grbl.lines)

    def testErrorWhileIdleSendsM5WithoutReset(self):
        grbl = FakeGrbl()
        grbl.errorAt = "M3"
        self.run_flow(START + PARAMETERS + FIRST_PASS, grbl=grbl, expectException=ssb.MachineError)
        self.assertNotIn(b'!', grbl.raw)
        self.assertEqual(grbl.resets, 0)
        self.assertIn(b'M5\n', grbl.raw)
        self.assertIn("M5 (spindle off)", self.text())

    def testAlarmIsReported(self):
        grbl = FakeGrbl()
        grbl.state = 'Alarm'
        self.assertEqual(ssb.safeStop(grbl, self.output.append, sleep=lambda s: None), 'Alarm')
        self.assertIn("Home ($H) before anything else", self.text())
        self.assertEqual(grbl.resets, 0)

    def testStreamDoesNotTakeSilenceAsOk(self):
        grbl = FakeGrbl()
        silent = []
        grbl.readline = lambda: silent.pop(0) if silent else b"ok\r\n"
        silent.extend([b"", b"", b""])
        ticks = iter(range(100))
        ssb.streamGcode(grbl, ["G53 G1 X-1.000 F100"], timeout=50, clock=lambda: next(ticks))
        self.assertEqual(silent, [])    # it kept waiting through the silent reads
        grbl.readline = lambda: b""
        with self.assertRaisesRegex(ssb.MachineError, "No 'ok'"):
            ssb.streamGcode(grbl, ["G53 G1 X-1.000 F100"], timeout=5, clock=lambda: next(ticks))


class OperatorWordingTests(FlowTestBase):
    """The operator is told to use the E-stop button, never "the reset button".

    The Grbl soft reset (Ctrl-X) is a different thing and keeps its name.
    """

    BANNED = ("reset button", "reset/power", "power switch")

    def testNoOperatorMessageSaysResetButton(self):
        # Every string literal in the script, f-string pieces included: the
        # prompts and messages the operator can see are all built from these.
        import ast
        with open(os.path.join(HERE, "surface-spoiler-board.py")) as source:
            tree = ast.parse(source.read())
        found = [(node.lineno, node.value) for node in ast.walk(tree)
                 if isinstance(node, ast.Constant) and isinstance(node.value, str)
                 and any(banned in node.value.lower() for banned in self.BANNED)]
        self.assertEqual(found, [])

    def testHomingPromptSaysEStopButton(self):
        grbl, surfacing, operator = self.run_flow(
            START + PARAMETERS + FIRST_PASS + [("Another pass at -0.3 mm (total -0.6)", "s")]
            + PARK + END)
        homing = next(p for p in operator.prompts if "Press Enter to home" in p)
        self.assertIn("Keep a hand near the E-stop button.", homing)

    def testFailedSafetyStopSaysEStopButton(self):
        grbl = FakeGrbl()
        grbl.state = 'Run'

        def brokenReset():
            raise OSError("port closed")
        grbl.softReset = brokenReset
        self.assertIsNone(ssb.safeStop(grbl, self.output.append, sleep=lambda s: None))
        self.assertIn("The safety stop itself failed: port closed.  Use the E-stop button.",
                      self.text())

    def testNoStatusReportSaysEStopButton(self):
        grbl = FakeGrbl()
        grbl.readline = lambda: b""      # Grbl says nothing at all
        ticks = iter(range(0, 1000, 5))
        self.assertIsNone(ssb.safeStop(grbl, self.output.append, sleep=lambda s: None,
                                       clock=lambda: next(ticks)))
        self.assertIn("use the E-stop button if it is moving.", self.text())
        self.assertIn("soft reset (Ctrl-X)", self.text())   # the Grbl reset keeps its name


class StartCornerFlowTests(FlowTestBase):
    """--start through the operator flow: the summary, every pass, the park."""

    def testDefaultIsFrontLeft(self):
        options = ssb.parseArguments(["--profile", self.profilePath, "--diameter", "35"])
        self.assertEqual(options.start, ssb.FRONT_LEFT)
        grbl, surfacing, operator = self.run_flow(
            START + PARAMETERS + FIRST_PASS + [("Another pass", "s")] + PARK + END)
        self.assertEqual(surfacing.choices['start'], ssb.FRONT_LEFT)
        self.assertIn("  11. Start corner: front-left (front to back, the first line left to right; "
                      "the first lines are nearest you)", self.output)
        self.assertIn("  10. Pattern: zigzag (alternate +X / -X)", self.output)
        self.assertIn("(front to back)", self.text())

    def testBackRightEveryPassAndParkStaysFrontLeft(self):
        grbl, surfacing, operator = self.run_flow(
            START + PARAMETERS + FIRST_PASS + [("Another pass at -0.3 mm (total -0.6)", "")]
            + [("Another pass", "s")] + PARK + END,
            arguments=("--diameter", "35", "--one-way", "--start", "back-right"))
        self.assertIn("  11. Start corner: back-right (back to front, the first line right to "
                      "left; ends at the front, nearer the park spot)", self.output)
        self.assertIn("  10. Pattern: one-way (every line -X, lift to return)", self.output)
        self.assertIn("Y-280.000 to Y-580.000 (back to front)", self.text())
        # Each pass starts with a rapid to the back-right corner (plus the overrun).
        starts = [i for i, line in enumerate(grbl.lines) if line.startswith("M3")]
        self.assertEqual(len(starts), 2)
        for m3 in starts:
            first = next(line for line in grbl.lines[m3:] if "X" in line)
            self.assertEqual(first, "G53 G0 X-182.500 Y-280.000")
        self.assertTrue(all(end < start for start, end in cuttingXMoves(grbl.lines)))
        # The park is unchanged: over the front-left corner.
        self.assertEqual(grbl.lines[-1], "G53 G0 X-600.000 Y-580.000")

    def testChangeTheStartCornerInTheSummary(self):
        grbl, surfacing, operator = self.run_flow(
            START + PARAMETERS + [("y to start", "11"), ("Start corner (front-left / back-right)",
                                                          "f"),
                                  ("y to start", "q"), ("Press Enter to park", "")],
            arguments=("--diameter", "35", "--start", "back-right"))
        self.assertEqual(surfacing.choices['start'], ssb.FRONT_LEFT)
        self.assertIn("  11. Start corner: back-right", "\n".join(self.output))

    def testRejectsAnUnknownCorner(self):
        with redirect_stdout(io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit):
                ssb.parseArguments(["--start", "top-left"])


class MinusSignDepthTests(FlowTestBase):
    """A depth may be typed with a minus sign: "-0.05" means 0.05 mm deeper.

    The script shows depths as negative ("Pass 1: -0.3 mm", "Another pass at
    -0.3 mm?"), so the owner typed "-0.05" at the next-pass prompt and was
    told "It must be more than 0."  A depth only goes down, so the minus sign
    is accepted and dropped.  Other values (feed, stepover, ...) still refuse it.
    """

    def askWith(self, function, answers, default=None):
        operator = Operator([("", answer) for answer in answers])
        printed = io.StringIO()
        with redirect_stdout(printed):
            value = function(operator, "? ", default=default)
        self.assertEqual(operator.script, [], "not every answer was asked for")
        return value, printed.getvalue()

    def testAskDepthTakesEitherSign(self):
        self.assertEqual(self.askWith(ssb.askDepth, ["-0.05"])[0], 0.05)
        self.assertEqual(self.askWith(ssb.askDepth, ["0.05"])[0], 0.05)
        self.assertEqual(self.askWith(ssb.askDepth, [""], default=0.3)[0], 0.3)

    def testAskDepthRefusesZeroAndNonsense(self):
        value, printed = self.askWith(ssb.askDepth, ["0", "-0", "abc", "nan", "-0.05"])
        self.assertEqual(value, 0.05)
        self.assertEqual(printed.count("It must be more than 0"), 3)   # 0, -0, nan
        self.assertIn("'abc' is not a number.", printed)

    def testAskPositiveStillRefusesAMinus(self):
        value, printed = self.askWith(ssb.askPositive, ["-300", "300"])
        self.assertEqual(value, 300)
        self.assertIn("It must be more than 0.", printed)

    def nextPass(self, typed):
        return self.run_flow(
            START + PARAMETERS + FIRST_PASS
            + [("Another pass at -0.3 mm (total -0.6)", "c"),
               ("How much deeper for the next pass, in mm (e.g. 0.05): ", typed),
               ("Another pass at -0.05 mm (total -0.4)", "s")] + PARK + END)

    def testNextPassWithAMinusSign(self):
        grbl, surfacing, operator = self.nextPass("-0.05")
        self.assertEqual(surfacing.totalDepth, 0.35)
        self.assertIn("  Next pass: 0.05 mm deeper (total -0.35 mm).", self.output)
        self.assertIn("G53 G1 Z-80.350 F50", grbl.lines)

    def testNextPassWithoutAMinusSign(self):
        grbl, surfacing, operator = self.nextPass("0.05")
        self.assertEqual(surfacing.totalDepth, 0.35)
        self.assertIn("  Next pass: 0.05 mm deeper (total -0.35 mm).", self.output)

    def testNextPassRefusesZeroAndNonsense(self):
        grbl, surfacing, operator = self.run_flow(
            START + PARAMETERS + FIRST_PASS
            + [("Another pass at -0.3 mm (total -0.6)", "c")]
            + [("How much deeper for the next pass", typed) for typed in ("0", "-0", "abc", "-0.05")]
            + [("Another pass at -0.05 mm (total -0.4)", "s")] + PARK + END)
        self.assertEqual(surfacing.totalDepth, 0.35)
        self.assertEqual(surfacing.passesDone, 2)

    def testSummaryDepthWithAMinusSign(self):
        parameters = [(expected, "-0.05" if expected.startswith("Depth per pass") else reply)
                      for expected, reply in PARAMETERS]
        grbl, surfacing, operator = self.run_flow(
            START + parameters + [("y to start", "y"), ("Pass 1: -0.05 mm (total -0.05)", ""),
                                  ("Another pass at -0.05 mm (total -0.1)", "s")] + PARK + END)
        self.assertEqual(surfacing.choices['depth'], 0.05)
        self.assertTrue(any(line.startswith("  5. Depth this pass: 0.05 mm (typed)")
                            for line in self.output))
        self.assertIn("G53 G1 Z-80.050 F50", grbl.lines)

    def testSummaryDepthChangedWithAMinusSign(self):
        grbl, surfacing, operator = self.run_flow(
            START + PARAMETERS + [("y to start", "5"), ("Depth per pass in mm [0.3", "-0.2"),
                                  ("y to start", "y"), ("Pass 1: -0.2 mm (total -0.2)", ""),
                                  ("Another pass", "s")] + PARK + END)
        self.assertEqual(surfacing.choices['depth'], 0.2)

    def testFeedStillRefusesAMinus(self):
        parameters = [(expected, "-300" if expected.startswith("Feed") else reply)
                      for expected, reply in PARAMETERS]
        feedAt = next(i for i, (expected, _) in enumerate(parameters) if expected.startswith("Feed"))
        parameters.insert(feedAt + 1, ("Feed in mm/min [286", ""))   # asked again; Enter: 286
        grbl, surfacing, operator = self.run_flow(
            START + parameters + [("y to start", "q"), ("Press Enter to park", "")])
        self.assertEqual(surfacing.choices['feed'], 286)

    def testDepthOptionTakesAMinusSign(self):
        options = ssb.parseArguments(["--profile", self.profilePath, "--depth", "-0.2"])
        self.assertEqual(options.depth, 0.2)
        for zero in ("0", "-0"):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                ssb.parseArguments(["--profile", self.profilePath, "--depth", zero])

    def testFeedOptionStillRefusesAMinus(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            ssb.parseArguments(["--profile", self.profilePath, "--feed", "-300"])


class DocstringTests(unittest.TestCase):
    def testPartsListNamesRealThings(self):
        # Each "name()" in the module docstring's parts list must exist.
        parts = ssb.__doc__.split("The parts, kept separate", 1)[1]
        names = re.findall(r'([A-Za-z_][\w.]*)\(\)', parts)
        self.assertIn("Surfacing.run", names)
        self.assertIn("Surfacing.runSafely", names)
        for name in names:
            target = ssb
            for piece in name.split("."):
                self.assertTrue(hasattr(target, piece), f"{name}() is not in surface-spoiler-board.py")
                target = getattr(target, piece)
            self.assertTrue(callable(target), name)

    def testUsageLineListsEveryOption(self):
        # The usage line at the top is hand-written, so nothing keeps it in
        # step with parseArguments(); this catches an option left out of it.
        usage = ssb.__doc__.split("The flow,", 1)[0]
        for option in ("--bit", "--diameter", "--corners", "--board", "--start",
                       "front-left", "back-right", "--one-way"):
            self.assertIn(option, usage)


class HelpTextTests(unittest.TestCase):
    def helpText(self):
        # argparse prints --help and exits; catch both and undo its line
        # wrapping so a phrase can be found wherever the wrap fell.
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed), self.assertRaises(SystemExit):
            ssb.parseArguments(["--help"])
        return " ".join(printed.getvalue().split())

    def testOneWayHelpNamesBothDirections(self):
        # --one-way cuts +X from the front-left start but -X from the
        # back-right one (--start), so its help must not promise +X only.
        text = self.helpText()
        oneWay = text.split("--one-way", 2)[2].split("--start", 1)[0]
        self.assertIn("+X from the front-left", oneWay)
        self.assertIn("-X from the back-right", oneWay)


class SaveSettingsTests(FlowTestBase):
    def testSavedBitDefaultsAndCommandLinePrecedence(self):
        ssb.saveBits(self.bitsPath, {"35mm-flat": {"diameter": 35.0, "feed": 250,
                                                   "stepoverPercent": 30.0}})
        script = [("Press Enter to home", ""), ("FRONT-LEFT", ""), ("BACK-RIGHT", ""),
                  ("Save these corners", "n"),
                  ("just touches", ""), ("Stepover in mm [10.5, saved for 35mm-flat]", ""),
                  ("Depth per pass in mm [0.3, default]", ""),
                  ("Plunge feed in mm/min [50", ""), ("Spindle speed", ""), ("Clearance", ""),
                  ("y to start", "y"), ("Pass 1", ""), ("Another pass", "s")] + PARK + [
                  ("Save these settings for 35mm-flat? [y/N]", "y"),
                  ("Set the spoilboard Z limit", "n")]
        grbl, surfacing, operator = self.run_flow(script, arguments=("--bit", "35mm-flat",
                                                                     "--feed", "320"))
        self.assertIn("  6. Feed: 320 mm/min (from --feed)", self.output)
        self.assertTrue(any(line.endswith("F320") for line in grbl.lines))
        saved = ssb.loadBits(self.bitsPath)["35mm-flat"]
        self.assertEqual(saved, {"diameter": 35.0, "feed": 320, "stepoverPercent": 30.0,
                                 "depthPerPass": 0.3, "plunge": 50})

    def testSavedFeedUsedWithoutCommandLine(self):
        ssb.saveBits(self.bitsPath, {"35mm-flat": {"diameter": 35.0, "feed": 250}})
        script = [("Pick a bit (1-1)", "1"), ("Press Enter to home", ""), ("FRONT-LEFT", ""),
                  ("BACK-RIGHT", ""), ("Save these corners", "n"), ("just touches", ""),
                  ("Stepover in mm [14,", ""),
                  ("Depth per pass", ""), ("Feed in mm/min [250, saved for 35mm-flat]", ""),
                  ("Plunge", ""), ("Spindle", ""), ("Clearance", ""),
                  ("y to start", "q"), ("Press Enter to park", "")]
        grbl, surfacing, operator = self.run_flow(script, arguments=())
        self.assertIn("  6. Feed: 250 mm/min (saved for 35mm-flat; override with --feed)",
                      self.output)

    def testNoSaveOfferWithoutACompletedPass(self):
        ssb.saveBits(self.bitsPath, {"35mm-flat": {"diameter": 35.0}})
        grbl = FakeGrbl()
        grbl.interruptAt = "G53 G1 Y"
        script = [("Press Enter to home", ""), ("FRONT-LEFT", ""), ("BACK-RIGHT", ""),
                  ("Save these corners", "n"),
                  ("just touches", "")] + PARAMETERS + FIRST_PASS
        self.run_flow(script, arguments=("--bit", "35mm-flat"), grbl=grbl,
                      expectException=KeyboardInterrupt)
        self.assertEqual(ssb.loadBits(self.bitsPath), {"35mm-flat": {"diameter": 35.0}})

    def testUnsavedBitOfferedAsNewAfterRun(self):
        script = START + PARAMETERS + FIRST_PASS + [("Another pass", "s")] + PARK + [
            ("Save this 35 mm bit and these settings as a new bit", "y"),
            ("Name for the new bit", "35mm-flat"), ("flutes", "2"), ("Set the spoilboard Z limit", "n")]
        self.run_flow(script)
        self.assertEqual(ssb.loadBits(self.bitsPath)["35mm-flat"],
                         {"diameter": 35.0, "flutes": 2, "feed": 286, "depthPerPass": 0.3,
                          "stepoverPercent": 40.0, "plunge": 50})


if __name__ == "__main__":
    unittest.main()
