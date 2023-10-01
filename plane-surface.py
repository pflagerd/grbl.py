#!/usr/env python
from grbl import *

def main(args):
    initializeGrbl()

    print("Move cutting head to lower left corner of work-piece.  Center the shaft over the corner, and move the z axis to just slightly press down a 0.1mm gauge.")

    setOriginToCurrentLocation()  # makes (X, Y, Z) = (0, 0, 0).  Uses G54 coordinates. Absolute.

    return 0

    clearanceHeight = 10  # 10 mm.  The height to safely move the head over the workpiece.

    moveInAStraightLineRapidly(0, 0, clearanceHeight)  # raise the cutting tool 10mm
    startSpindle(2000)  # start spindle

    workPieceSize = type('', (), {})()
    workPieceSize.x = 100
    workPieceSize.y = 100

    cutter = type('', (), {})()
    cutter.diameter = 25  # 25 mm diameter
    cutter.overlapPercentage = 25  # 25 percent of diameter
    cutter.swatheWidth = cutter.diameter - cutter.diameter * cutter.overlapPercentage  # width of cutter movement in y direction
    cutter.x = 0
    cutter.y = 0
    cutter.z = 0

    # Cut left-to-right, then right-to-left, and so on until done
    numberOfPasses = workPieceSize.y // cutter.swatheWidth
    # except that we want to always end on a standard cut (not a climb cut), which means we want an odd number of passes
    if numberOfPasses % 2 == 0:
        numberOfPasses += 1
    cutter.swatheWidth = workPieceSize.y / numberOfPasses  # adjust to match odd number of passes

    moveInAStraightLine(cutter.x, cutter.y, cutter.z + 1, 500)  # fairly quick move to 1 mm above workpiece

    facingDepth = 0.5  # 0.5mm cutting depth
    cutter.z -= facingDepth
    moveInAStraightLine(cutter.x, cutter.y, cutter.z, 100)  # cut slowly down into workpiece

    for passNumber in range(numberOfPasses):
        if passNumber % 2 == 0:  # passNumber counts from zero, so even passes are left-to-right
            cutter.x = workPieceSize.x
        else:
            cutter.x = 0
        moveInAStraightLine(cutter.x, cutter.y, cutter.z, 750)
        if passNumber != numberOfPasses - 1:
            cutter.y += cutter.swatheWidth
            moveInAStraightLine(cutter.x, cutter.y, cutter.z, 375)  # move slower in Y direction

    cutter.z = clearanceHeight    # raise the cutting tool to 10mm above the workpiece's original size
    moveInAStraightLineRapidly(cutter.x, cutter.y, cutter.z)
    stopSpindle()
    cutter.x, cutter.y = 0, 0
    moveInAStraightLineRapidly(cutter.x, cutter.y, cutter.z)

    return 0

# noinspection PyUnboundLocalVariable
if __name__ == '__main__':
    sys.exit(main(sys.argv))
