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

import importlib.util
import io
import json
import os
import re
import shutil
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from unittest import mock

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

    # --- GrblController surface ---
    def getSettings(self):
        return dict(self.settings)

    def send(self, gcode):
        self.sent.append(gcode)
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
         ("FRONT-LEFT", ""), ("BACK-RIGHT", ""), ("just touches", "")]
PARAMETERS = [("Stepover in mm [14,", ""), ("Depth per pass in mm [0.3,", ""),
              ("Feed in mm/min [286, from 35 mm bit]", ""), ("Plunge feed in mm/min [50,", ""),
              ("Spindle speed (S word) [10000,", ""), ("Clearance in mm [5,", "")]
FIRST_PASS = [("y to start", "y"), ("Pass 1: -0.3 mm (total -0.3)", "")]
PARK = [("Press Enter to park", "")]
END = [("Save this 35 mm bit and these settings", "n"), ("Set it again now", "n")]


class FlowTestBase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory)
        self.profilePath = os.path.join(self.directory, "6060.json")
        self.profile = MachineProfile(name="6060", workAreaZ=109.0,
                                      expectedSettings={"$23": "3", "$132": "109.000"})
        self.profile.save(self.profilePath)
        self.bitsPath = os.path.join(self.directory, "bits.json")
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
                    ssb.runWithSafeStop(surfacing, lambda: homes.append(1) or grbl.tryHomingCycle('3'))
            else:
                ssb.runWithSafeStop(surfacing, lambda: homes.append(1) or grbl.tryHomingCycle('3'))
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
            ssb.runWithSafeStop(surfacing, grbl.tryHomingCycle)
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
            ssb.runWithSafeStop(surfacing, grbl.tryHomingCycle)
        self.assertEqual(grbl.homed, 0)

    def testSecondCornerMustBeUpAndRight(self):
        grbl, surfacing, operator = self.run_flow(
            START[:4] + [("FRONT-LEFT", ""), ("BACK-RIGHT", ""), ("just touches", "")]
            + PARAMETERS + [("y to start", "q"), ("Press Enter to park", "")],
            positions=[CORNER2, CORNER1, CORNER1, CORNER2, TOUCH])
        self.assertIn("is not to the right of", self.text())
        self.assertIn("is not behind", self.text())
        self.assertEqual(surfacing.choices['board'], BOARD)

    def testTwoPassesThenCapStops(self):
        # Cap 0.5: 0.3, then a 0.3 is refused; change to 0.2; then the cap is reached.
        grbl, surfacing, operator = self.run_flow(
            START + PARAMETERS + FIRST_PASS
            + [("Another pass at -0.3 mm (total -0.6)", ""), ("Change the depth, or stop", "c"),
               ("Depth for the next pass", "0.2"), ("Pass 2: -0.2 mm (total -0.5)", ""),
               ("Another pass at -0.2 mm (total -0.7)", "y")] + PARK + END,
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
        grbl, surfacing, operator = self.run_flow(
            START + PARAMETERS + [("y to start", "b"), ("Pattern (zigzag / one-way)", "one"),
                                  ("y to start", "y"), ("Pass 1", ""), ("Another pass", "s")]
            + PARK + END)
        self.assertEqual(surfacing.choices['pattern'], ssb.ONE_WAY)

    def testChangeTheBoardJogsAgain(self):
        grbl, surfacing, operator = self.run_flow(
            START + PARAMETERS + [("y to start", "2"), ("FRONT-LEFT", ""), ("BACK-RIGHT", ""),
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
            + [("1-10 to change", "y"), ("1-10 to change", "4"), ("Stepover in mm [14,", ""),
               ("y to start", "q"), ("Press Enter to park", "")])
        self.assertIn("REFUSED", self.text())
        self.assertFalse(operator.prompts[-3].startswith("y to start"))


class ZRoomFlowTests(FlowTestBase):
    def shallow(self):
        settings = dict(SETTINGS)
        settings['$132'] = '80.200'      # fence z=-80.2: a 0.3 mm pass from -80 needs -80.3
        return FakeGrbl(settings)

    def testResetOfferedAndAccepted(self):
        grbl, surfacing, operator = self.run_flow(
            START + PARAMETERS + [("y to start", "y"), ("Reset $132", "y"), ("Pass 1", ""),
                                  ("Another pass", "s")] + PARK + END, grbl=self.shallow())
        self.assertIn("Z room: NOT enough", self.text())
        self.assertIn("Not enough Z room: this pass cuts down to z=-80.300", self.text())
        self.assertIn("$132=109.000", grbl.sent)
        self.assertEqual(surfacing.passesDone, 1)

    def testResetDeclined(self):
        grbl, surfacing, operator = self.run_flow(
            START + PARAMETERS + [("y to start", "y"), ("Reset $132", "n"),
                                  ("Change the depth, or stop", "s")] + PARK, grbl=self.shallow())
        self.assertFalse(any(line.startswith("M3") for line in grbl.lines))
        self.assertNotIn("$132=109.000", grbl.sent)

    def testResetNotEnough(self):
        self.profile.expectedSettings["$132"] = "80.200"   # the profile itself is shallow
        grbl, surfacing, operator = self.run_flow(
            START + PARAMETERS + [("y to start", "y"), ("Reset $132", "y"),
                                  ("Change the depth, or stop", "s")] + PARK, grbl=self.shallow())
        self.assertIn("Still not enough", self.text())
        self.assertEqual(surfacing.passesDone, 0)


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


class SaveSettingsTests(FlowTestBase):
    def testSavedBitDefaultsAndCommandLinePrecedence(self):
        ssb.saveBits(self.bitsPath, {"35mm-flat": {"diameter": 35.0, "feed": 250,
                                                   "stepoverPercent": 30.0}})
        script = [("Press Enter to home", ""), ("FRONT-LEFT", ""), ("BACK-RIGHT", ""),
                  ("just touches", ""), ("Stepover in mm [10.5, saved for 35mm-flat]", ""),
                  ("Depth per pass in mm [0.3, default]", ""),
                  ("Plunge feed in mm/min [50", ""), ("Spindle speed", ""), ("Clearance", ""),
                  ("y to start", "y"), ("Pass 1", ""), ("Another pass", "s")] + PARK + [
                  ("Save these settings for 35mm-flat? [y/N]", "y"), ("Set it again now", "n")]
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
                  ("BACK-RIGHT", ""), ("just touches", ""), ("Stepover in mm [14,", ""),
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
                  ("just touches", "")] + PARAMETERS + FIRST_PASS
        self.run_flow(script, arguments=("--bit", "35mm-flat"), grbl=grbl,
                      expectException=KeyboardInterrupt)
        self.assertEqual(ssb.loadBits(self.bitsPath), {"35mm-flat": {"diameter": 35.0}})

    def testUnsavedBitOfferedAsNewAfterRun(self):
        script = START + PARAMETERS + FIRST_PASS + [("Another pass", "s")] + PARK + [
            ("Save this 35 mm bit and these settings as a new bit", "y"),
            ("Name for the new bit", "35mm-flat"), ("flutes", "2"), ("Set it again now", "n")]
        self.run_flow(script)
        self.assertEqual(ssb.loadBits(self.bitsPath)["35mm-flat"],
                         {"diameter": 35.0, "flutes": 2, "feed": 286, "depthPerPass": 0.3,
                          "stepoverPercent": 40.0, "plunge": 50})


if __name__ == "__main__":
    unittest.main()
