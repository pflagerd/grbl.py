#!/usr/bin/env bash
# Builds grbl-sim, the Grbl simulator, against the Grbl 1.1h source.
# Everything goes into simulator/build/, which is ignored by git. See simulator/README.md.
#
# Needs: git, gcc, make  (sudo apt install build-essential git)
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
buildDir="$here/build"

# Pinned so the patch keeps applying. Grbl 1.1h build 20190830, and grbl-sim as of 2018-09-06.
grblCommit=bfb67f0c7963fe3ce4aaf8a97f9009ea5a8db36e
grblSimCommit=9bdd1654c7eb6cacf03e35340b38c962bcebe013

rm -rf "$buildDir"
mkdir -p "$buildDir"

git clone https://github.com/gnea/grbl.git "$buildDir/grbl"
git -C "$buildDir/grbl" -c advice.detachedHead=false checkout "$grblCommit"

# grbl-sim expects to live inside Grbl's source directory, as <grbl repo>/grbl/grbl-sim.
simDir="$buildDir/grbl/grbl/grbl-sim"
git clone https://github.com/grbl/grbl-sim.git "$simDir"
git -C "$simDir" -c advice.detachedHead=false checkout "$grblSimCommit"

git -C "$simDir" apply --verbose "$here/grblSim.patch"

make -C "$simDir" new

echo
echo "Built $simDir/grbl_sim.exe"
echo "Start it with: $here/runGrblSim.sh"
