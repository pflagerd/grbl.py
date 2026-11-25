import sys

from GrblController import *

usage = """usage: python move_to_machine_coordinates.py X Y Z

Runs a homing cycle, then moves rapidly to the given machine coordinates (mm).
On this machine, homed to the bottom left with Z up, machine coordinates are
zero or negative, e.g.

    python move_to_machine_coordinates.py -405 -10 -10
"""

if __name__ == '__main__':
    if len(sys.argv) != 4:
        print(usage, file=sys.stderr)
        sys.exit(2)
    try:
        x, y, z = (float(arg) for arg in sys.argv[1:])
    except ValueError:
        print(usage, file=sys.stderr)
        sys.exit(2)

    grblController = GrblController()
    # Machine coordinates mean nothing until the machine has been homed.
    print('machine coordinates at home == ' + str(grblController.runHomingCycle()))
    print(grblController.moveToMachineCoordinates(x, y, z))
