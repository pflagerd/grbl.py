import sys

from transformGcode import transformGcode


def makeGcodeToadstoolStoLowerLeftCutout():
    lowerLeftStoConventionalInsideGcodeFilename = "/home/oy753c/desktops/toadstool/carveco/Toolpaths/Toadstool Logo Scaled to 30 wide - sto.birch plywood.B/lower left sto conventional inside.gcode"
    with open(lowerLeftStoConventionalInsideGcodeFilename, "r") as gcodeFile:
        lowerLeftStoConventionalInsideGcode = gcodeFile.read()

    lowerLeftStoClimbInsideGcodeFilename = "/home/oy753c/desktops/toadstool/carveco/Toolpaths/Toadstool Logo Scaled to 30 wide - sto.birch plywood.B/lower left sto climb inside.gcode"
    with open(lowerLeftStoClimbInsideGcodeFilename, "r") as gcodeFile:
        lowerLeftStoClimbInsideGcode = gcodeFile.read()

    lowerLeftStoConventionalOutsideGcodeFilename = "/home/oy753c/desktops/toadstool/carveco/Toolpaths/Toadstool Logo Scaled to 30 wide - sto.birch plywood.B/lower left sto conventional outside.gcode"
    with open(lowerLeftStoConventionalOutsideGcodeFilename, "r") as gcodeFile:
        lowerLeftStoConventionalOutsideGcode = gcodeFile.read()

    lowerLeftStoClimbOutsideGcodeFilename = "/home/oy753c/desktops/toadstool/carveco/Toolpaths/Toadstool Logo Scaled to 30 wide - sto.birch plywood.B/lower left sto climb outside.gcode"
    with open(lowerLeftStoClimbOutsideGcodeFilename, "r") as gcodeFile:
        lowerLeftStoClimbOutsideGcode = gcodeFile.read()

    gcodeOutputLines = "T1\n"           # Tool 1
    gcodeOutputLines += "G17\n"         # Draw Arcs in the XY plane, default.
    gcodeOutputLines += "G21\n"         # All distances and positions are in mm
    gcodeOutputLines += "G90\n"         # All distances and positions are Absolute values from the current origin.
    gcodeOutputLines += "G0Z5.0000\n"   # A Rapid positioning move at the Rapid Feed Rate to Z5.0
    gcodeOutputLines += "S10000M3\n"    # Set Spindle speed in RPM. 10,000 RPM.  Then turn the motor on.

    startingZ = -5.8

    gcodeOutputLines += "; Inside\n"
    for depth in range(0, 9):
        gcodeOutputLines += transformGcode(lowerLeftStoConventionalInsideGcode, 0.0, 0.0, startingZ - depth, 100.0, 200.0)

    #  Do these two in the opposite direction to raise grain and then trim it off.
    gcodeOutputLines += transformGcode(lowerLeftStoClimbInsideGcode, 0.0, 0.0, -13.8, 100.0, 400.0)
    gcodeOutputLines += transformGcode(lowerLeftStoConventionalInsideGcode, 0.0, 0.0, -13.8, 100.0, 400.0)

    gcodeOutputLines += "G0Z5.0000\n"   # A Rapid positioning move at the Rapid Feed Rate to Z5.0

    gcodeOutputLines += "; Outside\n"
    for depth in range(0, 9):
        gcodeOutputLines += transformGcode(lowerLeftStoConventionalOutsideGcode, 0.0, 0.0, startingZ - depth, 100.0, 200.0)
    #  Do these two in the opposite direction to raise grain and then trim it off.
    gcodeOutputLines += transformGcode(lowerLeftStoClimbOutsideGcode, 0.0, 0.0, -13.8, 100.0, 400.0)
    gcodeOutputLines += transformGcode(lowerLeftStoConventionalOutsideGcode, 0.0, 0.0, -13.8, 100.0, 400.0)

    gcodeOutputLines += "M5\n"                  # Turn spindle motor off
    gcodeOutputLines += "G0Z5.0000\n"           # Rapidly raise the cutting head above the workpiece
    gcodeOutputLines += "G0X0.0000Y290.0000\n"  # Rapidly move the cutting head to top left out-of-the-way position
    gcodeOutputLines += "M2\n"                  # End program.

    with open("/home/oy753c/desktops/toadstool/carveco/Toolpaths/Toadstool Logo Scaled to 30 wide - sto.birch plywood.B/toadstoolStoCutout.gcode", "w") as gcodeFile:
        gcodeFile.write(gcodeOutputLines)


if __name__ == '__main__':
    sys.exit(makeGcodeToadstoolStoLowerLeftCutout())

