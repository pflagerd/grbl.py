from datetime import datetime
import re

from GrblController import *

workPieceLowerLeftOriginMachineCoordinates = GrblController.Vector(-405.000, -297.000, -81.0)


def parseGcodeLine(gcode_string):
    # Find all X, Y, Z coordinates with their values
    coord_pattern = r'([XYZIJF])(-?\d*\.?\d+)'
    coord_matches = re.findall(coord_pattern, gcode_string)

    # Extract values
    x_value = y_value = z_value = i_value = j_value = f_value = None

    for coord, value in coord_matches:
        if coord == 'X':
            x_value = float(value)
        elif coord == 'Y':
            y_value = float(value)
        elif coord == 'Z':
            z_value = float(value)
        elif coord == 'F':
            f_value = float(value)
        elif coord == 'I':
            i_value = float(value)
        elif coord == 'J':
            j_value = float(value)

    # Split the string by the coordinate patterns to get parts between them
    parts = re.split(coord_pattern, gcode_string)

    # The first part is the prefix (before any coordinates)
    prefix = parts[0]

    # The last part is the suffix (after all coordinates)
    suffix = parts[-1]

    return {
        'prefix': prefix,
        'X': x_value,
        'Y': y_value,
        'Z': z_value,
        'F': f_value,
        'I': i_value,
        'J': j_value,
        'suffix': suffix
    }


def transformShape(gcodeInputLines, XOffset=0, YOffset=0, ZFeed=50, XYFeed=400):
    gcodeOutputLines = ""

    for gcodeInputLine in gcodeInputLines.split('\n'):
        print("input:  " + gcodeInputLine)
        parsedGCodeLine = parseGcodeLine(gcodeInputLine)

        gcodeOutputLine = ''
        gcodeOutputLine += parsedGCodeLine['prefix'] if parsedGCodeLine['prefix'] else ""
        gcodeOutputLine += ("X" + str(parsedGCodeLine['X'] + XOffset)) if parsedGCodeLine['X'] else ""
        gcodeOutputLine += ("Y" + str(parsedGCodeLine['Y'] + YOffset)) if parsedGCodeLine['Y'] else ""
        gcodeOutputLine += ("Z" + str(parsedGCodeLine['Z'])) if parsedGCodeLine['Z'] else ""
        gcodeOutputLine += ("I" + str(parsedGCodeLine['I'] + XOffset)) if parsedGCodeLine['I'] else ""
        gcodeOutputLine += ("J" + str(parsedGCodeLine['J'] + YOffset)) if parsedGCodeLine['J'] else ""
        if parsedGCodeLine['F']:
            gcodeOutputLine += ("F" + str(ZFeed)) if parsedGCodeLine['Z'] else ("F" + str(XYFeed))
        print("output: " + gcodeOutputLine)
        gcodeOutputLines += gcodeOutputLine

    return gcodeOutputLines


if __name__ == "__main__":
    with open(
            "/home/oy753c/desktops/toadstool/carveco/Toolpaths/Toadstool Logo Scaled to 30 wide - sto.birch plywood.B/slow cut 5.6 ball nose 2mm sd0.1 fr200 pr100/conventional.outside.shape.gcode",
            "r") as gcodeFile:
        gcodeInputLines = gcodeFile.read()

    gcodeOutputLines = transformShape(gcodeInputLines)

    safeZAboveZOrigin = 5

    grblController = GrblController()
    print(grblController.runHomingCycle())
    grblController.setOrigin(workPieceLowerLeftOriginMachineCoordinates)
    # grblController.moveToMachineCoordinates(workPieceLowerLeftOriginMachineCoordinates.x, workPieceLowerLeftOriginMachineCoordinates.y)
    # grblController.moveToMachineCoordinates(z=workPieceLowerLeftOriginMachineCoordinates.z + safeZAboveZOrigin)
    # print(grblController.getMachineCoordinates())

    start = datetime.now()

    grblController.sendLines(gcodeOutputLines)
    finish = datetime.now()
    print(f'Elapsed time was {finish - start}')

    # grblController.moveToMachineCoordinates(workPieceLowerLeftOriginMachineCoordinates[0],
    #                                         workPieceLowerLeftOriginMachineCoordinates[1] + 280,
    #                                         -5)
    #grblController.moveToMachineCoordinates(z=safeZAboveZOrigin)
    #grblController.moveToMachineCoordinates(0, 0)
