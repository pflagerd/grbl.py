# Which files use `GrblController.py`, and which don't

A survey for [issue 9](https://github.com/pflagerd/grbl.py/issues/9), taken at `master` commit `49d1468`.

The repository has 33 Python files. One is `GrblController.py` itself. Of the other 32:

- **18 use `GrblController.py` for their implementation**: they drive the machine through a `GrblController` object.
- **14 do not.** They are listed first, because that list is what was asked for.

"Use" here means the file talks to the controller through `GrblController`, directly or through another script. A file that only imports it is counted separately, in the second table below.

## Files that do not use `GrblController.py`

### No connection to it at all (11)

None of these imports `GrblController.py`, directly or through another file.

| File | What it uses instead |
|---|---|
| `grbl.py` | The older library. Opens `/dev/ttyUSB0` with pyserial itself, as soon as it is imported. |
| `cut-circular-pocket.py` | `grbl.py` (`from grbl import *`). Uses its work-coordinate moves and clockwise arcs. |
| `plane-surface.py` | `grbl.py` (`from grbl import *`). Uses its homing and work-coordinate moves. |
| `find_home_and_set_origin.py` | pyserial directly. Has its own copy of the fixed three-line startup check. |
| `probe_machine_coordinates_of_all_limits.py` | `find_home_and_set_origin.py`, so pyserial indirectly. |
| `get_status.py` | pyserial directly. |
| `test_coordinate_systems.py` | pyserial directly. |
| `simplest-terminal-ever.py` | pyserial directly. |
| `mock-serial-port-terminal.py` | pyserial directly, on two hard-coded pseudo-terminals. Does not talk to the machine. |
| `pseudo-serial-port-test.py` | pyserial directly, on two hard-coded pseudo-terminals. Does not talk to the machine. |
| `move_to_machine_coordinates.py` | Nothing. The file is empty (0 bytes). |

### Import it, but do not use it for their implementation (3)

These load `GrblController.py` when they run, but never create a `GrblController` or talk to the machine. They only process g-code text.

| File | How it reaches `GrblController.py` | What it actually needs from it |
|---|---|---|
| `transformGcode.py` | `from GrblController import *` | `GrblController.Vector`, for one module-level constant that nothing else reads; and `sys`, which arrives through the star import. |
| `makeGcodeToadstoolStoCutout.py` | Imports `transformGcode`, which imports it | Nothing. |
| `makeGcodeToadstoolStoRoundover.py` | Imports `transformGcode`, which imports it | Nothing. |

One consequence: importing any of these three requires pyserial to be installed, although none of them uses a serial port.

## Files that do use `GrblController.py` (18)

### Directly: they create a `GrblController` (16)

| File | Methods it calls |
|---|---|
| `cut_horizontal_line.py` | `runHomingCycle`, `moveToMachineCoordinates`, `cutToMachineCoordinates` |
| `cut_horizontal_lines.py` | `runHomingCycle`, `moveToMachineCoordinates`, `cutToMachineCoordinates` |
| `cut_vertical_lines.py` | `runHomingCycle`, `moveToMachineCoordinates`, `cutToMachineCoordinates` |
| `cut_vertical_lines_constant_first_depth.py` | `runHomingCycle`, `moveToMachineCoordinates`, `cutToMachineCoordinates` |
| `cut_vertical_lines_increasing_depth.py` | `runHomingCycle`, `moveToMachineCoordinates`, `cutToMachineCoordinates` |
| `cut_vertical_lines_single_pass.py` | `runHomingCycle`, `moveToMachineCoordinates`, `cutToMachineCoordinates` |
| `cut_workpiece_perimeter_index_lines.py` | `runHomingCycle`, `moveToMachineCoordinates`, `cutToMachineCoordinates`, `stopSpindleMotor` |
| `findWorkPieceLowerLeftOriginMachineCoordinates.py` | `runHomingCycle`, `moveToMachineCoordinates` |
| `liveTestGetStatus.py` | `getStatus` |
| `moveToTopLeft.py` | `runHomingCycle`, `moveToMachineCoordinates` |
| `run_homing_cycle.py` | `runHomingCycle` |
| `run_homing_cycle_and_move_to_default_lower_left_origin.py` | `runHomingCycle`, `moveToMachineCoordinates` |
| `runHomingCycleSetOriginSendGCodeFile.py` | `runHomingCycle`, `moveToMachineCoordinates`, `setOrigin`, `sendFile` |
| `send_gcode_file.py` | `runHomingCycle`, `moveToMachineCoordinates`, `setOrigin`, `sendFile` |
| `runHomingCycleSetOriginMakeGCodeSendGcodeToadstoolStoInsideRoundover.Experiment.py` | `runHomingCycle`, `moveToMachineCoordinates`, `setOrigin`, `sendLines` |
| `runHomingCycleSetOriginMakeGCodeSendGcodeToadstoolStoOutsideRoundover.Experiment.py` | `runHomingCycle`, `moveToMachineCoordinates`, `setOrigin`, `sendLines` |

### Through another script (2)

| File | Through |
|---|---|
| `runHomingCycleSetOriginSendGCodeFileToadstoolStoCutout.py` | `runHomingCycleSetOriginSendGCodeFile.py` |
| `runHomingCycleSetOriginSendGCodeFileToadstoolStoRoundover.py` | `runHomingCycleSetOriginSendGCodeFile.py` |

## Files that are not Python (14)

None of these is code, so the question does not apply: `.gitignore`, `README.md`, `requirements.txt`, `how to set up venv`, `linuxcnc.org`, `grbl settings.txt`, `gcode parameter settings.txt`, `toadstoolStoOrigins.txt`, `test_coordinate_systems.py.log`, and the five files in `doc/`.

## What the survey shows about issue 9

Issue 9 is titled "Reimplement grbl.py as a superclass of GrblController.py." Its body says `GrblController.py` contains the "impl" of `grbl.py`, and `grbl.py` provides the interface. These are observations from the code, not a proposal.

1. **`grbl.py` is not a class.** It is 12 module-level functions plus a serial port opened at import (`grbl.py` line 8). It cannot be a superclass of anything without first being rewritten as a class.
2. **`GrblController` already has a superclass**, `serial.Serial`.
3. **The README says the opposite direction.** Its row for `GrblController.py` reads "Maybe it will become the super-class for grbl.py", where the issue makes `grbl.py` the superclass.
4. **`GrblController.py` does not contain the implementation of `grbl.py` today.** Matching each `grbl.py` function to its nearest counterpart:

   | `grbl.py` function | Nearest in `GrblController` | Same behaviour? |
   |---|---|---|
   | `initializeGrbl()` | `__init__` | Roughly: both check the greeting and unlock. |
   | `sendToGrbl(line)` | `send(gcode)` | Roughly. |
   | `setOriginToCurrentLocation()` | `setOrigin()` with no arguments | Yes: both send `G10 P1 L20 X0 Y0 Z0`. |
   | `doHomeCycle()` | `runHomingCycle()` | No: `doHomeCycle` also sends `G90`, `G54` and resets the G54 origin. |
   | `startSpindle(speed)` | `startSpindleMotor(speed)` | No: `startSpindle` sends `M3 S…`; `startSpindleMotor` sends only `S…`, with no `M3`. |
   | `stopSpindle()` | `stopSpindleMotor()` | Roughly: `M5` against `S0 M5`. |
   | `moveInAStraightLine(x, y, z, speed)` | `cut()` | **Not implemented** (raises). `cutToMachineCoordinates` exists but moves in machine coordinates (`G53`), not work coordinates. |
   | `moveInAStraightLineRapidly(x, y, z)` | `move()` | **Not implemented** (raises). `moveToMachineCoordinates` is the machine-coordinate version. |
   | `moveInAnArcClockwise(...)` | `cutClockwiseArc()` | **Not implemented** (raises). |
   | `pause(seconds)` | none | Missing. |
   | `drillHoleWithPecking(...)` | none | Missing. No script in the repo calls it. |
   | `printGrblStatus()` | `getStatus()` | Partly: `getStatus()` covers `?`. `printGrblStatus` also sends `$$`, `$#`, `$G` and `$N`; the related getters (`getGrblState`, `getGCodeParameters`, `getStartupGCode`) are empty stubs. |

5. **In practice `GrblController.py` is already the main library.** 18 scripts drive the machine through it and 2 through `grbl.py`.
6. **The two `grbl.py` scripts cannot be ported yet.** `cut-circular-pocket.py` needs clockwise arcs and both need work-coordinate moves, which are the unimplemented `cut()`, `move()` and `cutClockwiseArc()` above.
7. **Four more scripts bypass both libraries** and talk to the machine with pyserial directly or through `find_home_and_set_origin.py`: `find_home_and_set_origin.py`, `probe_machine_coordinates_of_all_limits.py`, `get_status.py` and `test_coordinate_systems.py`. `simplest-terminal-ever.py` does too, but as a raw terminal it has no use for either library.

## How this was derived

From each file's `import` statements, followed through local modules, and from which `GrblController` names each file refers to. No script was run against the machine.
