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


def transformShapeString(gcodeInputLines, XOffset=0.0, YOffset=0.0, ZDepth=-1.0, ZFeed=50.0, XYFeed=400.0):
    # if XOffset < 0 or YOffset < 0 or ZFeed < 0 or XYFeed < 0:
    #     raise ValueError("XOffset < 0 or YOffset < 0 or ZFeed < 0 or XYFeed < 0!")

    gcodeOutputLines = ""

    for gcodeInputLine in gcodeInputLines.split('\n'):
        print("input:  " + gcodeInputLine)
        parsedGCodeLine = parseGcodeLine(gcodeInputLine)

        gcodeOutputLine = ''
        gcodeOutputLine += parsedGCodeLine['prefix'] if parsedGCodeLine['prefix'] else ""
        gcodeOutputLine += f"X{parsedGCodeLine['X'] + XOffset:.4f}" if parsedGCodeLine['X'] else ""
        gcodeOutputLine += f"Y{parsedGCodeLine['Y'] + YOffset:.4f}" if parsedGCodeLine['Y'] else ""
        gcodeOutputLine += f"Z{ZDepth:.4f}" if parsedGCodeLine['Z'] else ""
        gcodeOutputLine += f"I{parsedGCodeLine['I']:.4f}" if parsedGCodeLine['I'] else ""
        gcodeOutputLine += f"J{parsedGCodeLine['J']:.4f}" if parsedGCodeLine['J'] else ""
        if parsedGCodeLine['F']:
            gcodeOutputLine += f"F{ZFeed:.4f}" if parsedGCodeLine['Z'] else f"F{XYFeed:.4f}"
        print("output: " + gcodeOutputLine)
        gcodeOutputLines += gcodeOutputLine + '\n'

    return gcodeOutputLines


def main(args):
    inputFileName = "/home/oy753c/desktops/toadstool/carveco/Toolpaths/Toadstool Logo Scaled to 30 wide - sto.birch plywood.B/lower left sto climb.gcode"
    with open(inputFileName, "r") as gcodeFile:
        gcodeInputLines = gcodeFile.read()

    #gcodeOutputLines = transformShape(gcodeInputLines)
    #gcodeOutputLines = transformShape(gcodeInputLines, -0.2046, 160.6439)
    gcodeOutputLines = transformShapeString(gcodeInputLines, 179.862, 90.6404)

    outputFileName = inputFileName.replace(".gcode", ".transformed.gcode")
    with open(outputFileName, "w") as gcodeFile:
        gcodeFile.write(gcodeOutputLines)


if __name__ == '__main__':
    sys.exit(main(sys.argv))

