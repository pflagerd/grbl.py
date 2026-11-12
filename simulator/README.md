# A simulated Grbl 1.1h to test against

This directory builds [grbl-sim](https://github.com/grbl/grbl-sim) against the
[Grbl 1.1h source](https://github.com/gnea/grbl) and runs it behind a fake serial port.
grbl-sim compiles the real Grbl firmware source into a program for the PC, so the welcome
message, replies, error numbers and status reports are the firmware's own.

It lets code that talks to Grbl be tried without the CNC machine. It does **not** simulate
limit switches, so homing cannot succeed (see [Limitations](#limitations)).

## Quick start

Install the tools:

```bash
sudo apt install build-essential git socat
```

Build the simulator (about 5 seconds; downloads two repositories into `simulator/build/`):

```bash
./simulator/buildGrblSim.sh
```

Run it, and leave it running in its own terminal:

```bash
./simulator/runGrblSim.sh
```

The fake serial port is `/tmp/ttyFAKE`. Pass a different path as the first argument to change it.
Stop the simulator with Ctrl-C.

## Talking to it

With plain pyserial:

```python
import serial

port = serial.Serial("/tmp/ttyFAKE", 115200, timeout=1)
port.write(b"?")
print(port.readline())   # b'<Idle|MPos:0.000,0.000,0.000|FS:0,0|Pn:PXYZRHS|WCO:0.000,0.000,0.000>\r\n'
port.write(b"$I\n")
print(port.readline())   # b'[VER:1.1h.20190830:]\r\n'
```

With `GrblController`:

```python
from GrblController import *

grblController = GrblController("/tmp/ttyFAKE", resetOnConnect=True)
print(grblController.moveToMachineCoordinates(3, 2, 1))   # (3.0, 2.0, 1.0)
```

This needs the tolerant startup handshake from https://github.com/pflagerd/grbl.py/pull/15.
The older constructor waits 30 seconds and then exits without a message, because it never
sees a welcome message (next section).

## Why `resetOnConnect=True`

The simulator prints its welcome message when it starts, before any program opens the port,
and pyserial discards waiting input when it opens a port. So a program never sees the welcome
message unless it asks for a new one.

- With `resetOnConnect=True`, `GrblController` sends ctrl-x first. The simulator resets, prints
  the welcome message again, and the connection takes no measurable time.
- Without it, `GrblController` waits 5 seconds for a welcome message, then connects through
  its `?` and `$I` fallback.

## Limitations

- **No limit switches, so no homing.** Every input pin always reads as active: status reports
  show `Pn:PXYZRHS`. The grbl-sim source has "set limit pins based on position" as a TODO.
  - With the default `$5=0`, `$H` fails with `ALARM:8`.
  - With `$5=1` (limit pins inverted, so they read as never active), `$H` did not finish in
    the 12 seconds it was given.
  - The simulator starts with homing off (`$22=0`), where `$H` returns `error:5`.
  - So scripts that call `runHomingCycle()` cannot run to completion against it.
- **Time is approximate.** grbl-sim's own README says it runs in "approximate realtime".
- **It starts in Idle, not Alarm**, because Grbl's default settings have homing off.
- **It is experimental.** grbl-sim's README calls itself a proof of concept, and its last
  commit is from 2018. It needed the three fixes below to be usable here.

## Things it is good for

- **The startup handshake.** Each of these was tried with the handshake from pull request 15:

  | Simulator settings | What Grbl sends after a reset | Result |
  |---|---|---|
  | Defaults | Welcome message only; state Idle | Connects, no unlock needed |
  | `$22=1` (homing on) | Welcome, `[MSG:'$H'\|'$X' to unlock]`; state Alarm | Connects, unlocks with `$X` |
  | `$21=1` (hard limits on) | Welcome, `[MSG:Check Limits]`, unlock hint; state Alarm | `GrblLimitSwitchError` |

  The last row works because the pins always read as active, which Grbl takes for an engaged
  limit switch.
- **Streaming g-code.** 3,000 `G1` lines sent one at a time each got a single `ok`, and the
  final reported position matched the last line to within one step.
- **Settings that persist.** The simulator keeps its settings in
  `simulator/build/grbl/grbl/grbl-sim/EEPROM.DAT`, as the real controller keeps them in EEPROM.
  Delete that file to return to Grbl's defaults.

## What the patch changes

`grblSim.patch` is applied to grbl-sim by the build script. Without it:

1. **It does not link.** GCC 10 and later default to `-fno-common`
   ([GCC 10 porting notes](https://gcc.gnu.org/gcc-10/porting_to.html)), and grbl-sim's headers
   rely on the old behaviour. The patch adds `-fcommon`.
2. **Commands stall.** A command such as `$I` followed by a newline got no reply until another
   byte arrived. grbl-sim asks `select()` whether input is waiting but reads it with `getchar()`,
   whose buffer swallows the rest of the line. The patch makes standard input unbuffered.
3. **Replies are occasionally corrupted.** About 1 reply in 1,500 had one wrong character, for
   example `MPos:P.000` for `MPos:3.000`. Grbl's serial buffers are shared between two threads
   with no locking, and the optimized build stores a buffer's head index before the data byte.
   The patch compiles Grbl's `serial.c` unoptimized and makes the transmit loop check the buffer
   itself. After it, 18,000 replies in a row were correct.

## Versions used

| | |
|---|---|
| Grbl | [gnea/grbl](https://github.com/gnea/grbl) commit `bfb67f0` (1.1h, build 20190830) |
| grbl-sim | [grbl/grbl-sim](https://github.com/grbl/grbl-sim) commit `9bdd165` (2018-09-06) |
| Tried on | Ubuntu 24.04, GCC 13.3, socat 1.8.0.0, Python 3.13, pyserial 3.5 |

Both commits are pinned in `buildGrblSim.sh` so that the patch keeps applying.
