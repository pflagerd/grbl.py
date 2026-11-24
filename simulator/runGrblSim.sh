#!/usr/bin/env bash
# Runs the simulated Grbl 1.1h behind a fake serial port. Stop it with Ctrl-C.
# Usage: runGrblSim.sh [port]      port defaults to /tmp/ttyFAKE
# Build it first with buildGrblSim.sh. See simulator/README.md.
#
# Needs: socat  (sudo apt install socat)
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
simDir="$here/build/grbl/grbl/grbl-sim"
port="${1:-/tmp/ttyFAKE}"

if [ ! -x "$simDir/grbl_sim.exe" ]; then
    echo "The simulator is not built yet. Run $here/buildGrblSim.sh first." >&2
    exit 1
fi

# A link left behind by a simulator that was killed would stop socat creating a new one.
if [ -L "$port" ]; then
    rm -f "$port"
fi

# The simulator keeps its settings in EEPROM.DAT in its working directory.
cd "$simDir"

echo "Simulated Grbl 1.1h is on $port. Press Ctrl-C to stop it."
echo "Its saved settings are in $simDir/EEPROM.DAT; delete that file to get Grbl's defaults back."

# socat arguments:
#   PTY                 create a pseudo-terminal, which behaves like a serial port
#   raw                 pass bytes through untouched
#   link=$port          make a symlink with this name pointing at the pseudo-terminal
#   echo=0              do not echo what the host sends back to it
#   EXEC:'...'          run the simulator and connect it to the other end
#   pty,raw,echo=0      give the simulator its own raw pseudo-terminal
# grbl_sim.exe arguments:
#   -n                  no comment character in front of Grbl's responses
#   -s FILE             where to log steps (the default is stderr)
#   -b FILE             where to log planner blocks (the default is stdout, which would mix
#                       them into the serial output)
exec socat "PTY,raw,link=$port,echo=0" "EXEC:'./grbl_sim.exe -n -s /dev/null -b /dev/null',pty,raw,echo=0"
