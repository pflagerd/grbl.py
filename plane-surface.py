#!/usr/env python
from grbl import *
from math import *

def main(args):
    initializeGrbl()

    print("Move cutting head to lower left corner of work-piece.  Center the shaft over the corner, and move the z axis to just slightly press down a 0.1mm gauge.")
    input("Hit enter when ready. ")

    setOriginToCurrentLocation()  # makes (X, Y, Z) = (0, 0, 0).  Uses G54 coordinates. Absolute.

    printGrblStatus()

    dustShoeClearanceHeight = 50

    moveInAStraightLineRapidly(0, 0, dustShoeClearanceHeight)  # raise the cutting tool 50mm for dust shoe.
    input("Put dust shoe on cutter.  Hit enter when ready. ")

    clearanceHeight = 10  # 10 mm.  The height to safely move the head over the workpiece.

    moveInAStraightLineRapidly(0, 0, clearanceHeight)  # move the cutting tool to 10mms

    workPieceSize = type('', (), {})()
    workPieceSize.x = 225
    workPieceSize.y = 142

    cutter = type('', (), {})()
    cutter.diameter = 25  # 25 mm diameter
    cutter.overlapPercentage = 33  # 25 percent of diameter
    cutter.swatheWidth = cutter.diameter - cutter.diameter * cutter.overlapPercentage / 100  # width of cutter movement in y direction
    cutter.x = 0
    cutter.y = 0
    cutter.z = 0

    cuttingSpeed = 300

    pattern = 'climb-cut'

    # Cut left-to-right, then right-to-left, and so on until done
    numberOfPasses = int(ceil(workPieceSize.y / cutter.swatheWidth + 1))  # The + 1 indicates that we are making an inclusive number of cuts (lines rather than spaces between the lines)
    cutter.swatheWidth *= (numberOfPasses - 1) / numberOfPasses
    # except that we want to always end on a standard cut (not a climb cut), which means we want an odd number of passes
    if pattern == 'bidirectional' and numberOfPasses % 2 == 0:
        cutter.swatheWidth *= numberOfPasses / (numberOfPasses + 1)
        numberOfPasses += 1

    facingDepth = 0.2  # 0.5mm cutting depth
    cutter.z = -facingDepth
    while True:
        startSpindle(10000)  # start spindle

        if pattern == "climb-cut":
            cutter.x = workPieceSize.x
            moveInAStraightLineRapidly(cutter.x, cutter.y, clearanceHeight)

        moveInAStraightLine(cutter.x, cutter.y, cutter.z + 1, 500)  # fairly quick move to 1 mm above workpiece

        moveInAStraightLine(cutter.x, cutter.y, cutter.z, 50)  # cut slowly down into workpiece

        for passNumber in range(numberOfPasses):
            if pattern == "bidirectional":
                if passNumber % 2 == 0:  # passNumber counts from zero, so even passes are left-to-right
                    cutter.x = workPieceSize.x
                else:
                    cutter.x = 0
                moveInAStraightLine(cutter.x, cutter.y, cutter.z, cuttingSpeed)
                if passNumber != numberOfPasses - 1:
                    cutter.y += cutter.swatheWidth
                    moveInAStraightLine(cutter.x, cutter.y, cutter.z, cuttingSpeed / 2)  # move slower in Y direction
            else:  # pattern = "climb-cut".  Cuts are from right to left
                moveInAStraightLine(0, cutter.y, cutter.z, cuttingSpeed)
                moveInAStraightLineRapidly(0, cutter.y, clearanceHeight)
                if passNumber != numberOfPasses - 1:
                    cutter.y += cutter.swatheWidth
                    moveInAStraightLineRapidly(cutter.x, cutter.y, clearanceHeight)

                    moveInAStraightLine(cutter.x, cutter.y, cutter.z + 1, 500)  # fairly quick move to 1 mm above workpiece

                    moveInAStraightLine(cutter.x, cutter.y, cutter.z, 100)  # cut slowly down into workpiece

        cutter.z = dustShoeClearanceHeight    # raise the cutting tool to clearanceHeight above the current z-height, in case you want to do another (finishing) pass.
        if pattern == 'climb-cut:':
            moveInAStraightLineRapidly(0, cutter.y, cutter.z)
        else:
            moveInAStraightLineRapidly(cutter.x, cutter.y, cutter.z)
        stopSpindle()
        cutter.x, cutter.y = 0, 0
        moveInAStraightLineRapidly(cutter.x, cutter.y, cutter.z)
        response = input("Do another pass? y/n ")
        if response not in ["y", "Y"]:
            break
        cutter.z = -facingDepth

    cutter.z = dustShoeClearanceHeight
    moveInAStraightLineRapidly(cutter.x, cutter.y, cutter.z)

    return 0

# noinspection PyUnboundLocalVariable
if __name__ == '__main__':
    sys.exit(main(sys.argv))