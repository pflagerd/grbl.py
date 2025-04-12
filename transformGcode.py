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


def transformGcode(gcodeInputLines, XOffset=0.0, YOffset=0.0, ZDepth=-1.0, ZFeed=50.0, XYFeed=400.0):
    # if XOffset < 0 or YOffset < 0 or ZFeed < 0 or XYFeed < 0:
    #     raise ValueError("XOffset < 0 or YOffset < 0 or ZFeed < 0 or XYFeed < 0!")

    gcodeOutputLines = ""

    for gcodeInputLine in gcodeInputLines.split('\n'):
        # print("input:  " + gcodeInputLine)
        if not gcodeInputLine:
            continue
        parsedGCodeLine = parseGcodeLine(gcodeInputLine)

        gcodeOutputLine = ''
        gcodeOutputLine += parsedGCodeLine['prefix'] if parsedGCodeLine['prefix'] else ""
        gcodeOutputLine += f"X{parsedGCodeLine['X'] + XOffset:.4f}" if parsedGCodeLine['X'] else ""
        gcodeOutputLine += f"Y{parsedGCodeLine['Y'] + YOffset:.4f}" if parsedGCodeLine['Y'] else ""
        if parsedGCodeLine['Z'] == 0.0 and parsedGCodeLine['X'] is None and parsedGCodeLine['Y'] is None:  # G0Z0.0000 commands are not altered
            gcodeOutputLine += f"Z{0.0:.4f}"
        else:
            gcodeOutputLine += f"Z{ZDepth:.4f}" if parsedGCodeLine['Z'] else ""
        gcodeOutputLine += f"I{parsedGCodeLine['I']:.4f}" if parsedGCodeLine['I'] else ""
        gcodeOutputLine += f"J{parsedGCodeLine['J']:.4f}" if parsedGCodeLine['J'] else ""
        if parsedGCodeLine['F']:
            gcodeOutputLine += f"F{ZFeed:.4f}" if parsedGCodeLine['Z'] else f"F{XYFeed:.4f}"
        # print("output: " + gcodeOutputLine)
        gcodeOutputLines += gcodeOutputLine + '\n'

    return gcodeOutputLines


def main(args):
    pass


if __name__ == '__main__':
    sys.exit(main(sys.argv))

