#!/usr/env python
from grbl import *
from math import *


def main(args):
    initializeGrbl()
    doHomeCycle()  # -417.0, -307.0, -3.0
    setOriginToCurrentLocation()  # makes (X, Y, Z) = (0, 0, 0).  Uses G54 coordinates. Absolute.

    moveInAStraightLineRapidly(10, 7, -86) # machine coordinates -407.000, -300.000, -89.0
    moveInAStraightLineRapidly(10, 7, -86) # machine coordinates -407.000, -300.000, -89.0

    setOriginToCurrentLocation()  # makes (X, Y, Z) = (0, 0, 0).  Uses G54 coordinates. Absolute.

    printGrblStatus()

    clearanceHeight = 5  # mm.  The height to safely move the head over the workpiece.

    moveInAStraightLineRapidly(0, 0, clearanceHeight)  # move the cutting tool to 10mms

    workPiece = type('', (), {})()
    workPiece.origin = type('', (), {})()
    workPiece.origin.x = 0
    workPiece.origin.y = 0
    workPiece.origin.z = 0
    workPiece.size = type('', (), {})()
    workPiece.size.x = 400  # mm
    workPiece.size.y = 300  # mm

    facingDepth = 0.5  # mm cutting depth

    cutter = type('', (), {})()
    cutter.diameter = 25.0
    cutter.overlapPercentage = 33.0  # percent of diameter
    cutter.swatheWidth = cutter.diameter - cutter.diameter * cutter.overlapPercentage / 100.0  # width of cutter movement in y direction
    cutter.position = type('', (), {})()
    cutter.position.x = 0
    cutter.position.y = 0
    cutter.position.z = 0

    cuttingSpeed = 400.0  # mm/m

    cuttingPlane = type('', (), {})()
    cuttingPlane.origin = type('', (), {})()
    cuttingPlane.origin.x = 0
    cuttingPlane.origin.y = 0
    cuttingPlane.origin.z = -facingDepth if facingDepth != 0.0 else 0
    cuttingPlane.size = type('', (), {})()
    cuttingPlane.size.x = workPiece.size.x
    cuttingPlane.size.y = workPiece.size.y

    pattern = 'climb-cut'  # 'bidirectional`

    # if bidirectional, cut left-to-right, then right-to-left, and so on until done
    numberOfPasses = int(ceil(workPiece.size.y / cutter.swatheWidth + 1))  # The + 1 indicates that we are making an inclusive number of cuts (lines rather than spaces between the lines)
    cutter.swatheWidth *= (numberOfPasses - 1) / numberOfPasses
    # except that we want to always end on a standard cut (not a climb cut), which means we want an odd number of passes
    if pattern == 'bidirectional' and numberOfPasses % 2 == 0:
        cutter.swatheWidth *= numberOfPasses / (numberOfPasses + 1)
        numberOfPasses += 1

    plungeSpeed = 5  # mm/m

    while True:
        startSpindle(10000)  # start spindle

        if pattern == "bidirectional":
            cutter.position.x = cuttingPlane.origin.x
        else:  # climb-cut
            cutter.position.x = cuttingPlane.origin.x + cuttingPlane.size.x
        cutter.position.y = cuttingPlane.origin.y
        cutter.position.z = cuttingPlane.origin.z + clearanceHeight
        moveInAStraightLineRapidly(cutter.position.x, cutter.position.y, cutter.position.z)

        cutter.position.z = cuttingPlane.origin.z + 1
        moveInAStraightLineRapidly(cutter.position.x, cutter.position.y, cutter.position.z)

        cutter.position.z = cuttingPlane.origin.z
        moveInAStraightLine(cutter.position.x, cutter.position.y, cutter.position.z, plungeSpeed)  # cut slowly down

        for passNumber in range(numberOfPasses):
            if pattern == "bidirectional":
                if passNumber % 2 == 0:  # passNumber counts from zero, so even passes are left-to-right
                    cutter.position.x = cuttingPlane.origin.x + cuttingPlane.size.x
                else:
                    cutter.position.x = cuttingPlane.origin.x
                moveInAStraightLine(cutter.position.x, cutter.position.y, cutter.position.z, cuttingSpeed)
                if passNumber != numberOfPasses - 1:
                    cutter.position.y += cutter.swatheWidth
                    moveInAStraightLine(cutter.position.x, cutter.position.y, cutter.position.z, cuttingSpeed / 2)  # move slower in Y direction
            else:  # pattern = "climb-cut".  Cuts are from right to left
                cutter.position.x = cuttingPlane.origin.x
                moveInAStraightLine(cutter.position.x, cutter.position.y, cutter.position.z, cuttingSpeed)

                cutter.position.z = cuttingPlane.origin.z + clearanceHeight
                moveInAStraightLineRapidly(cutter.position.x, cutter.position.y, cutter.position.z)
                if passNumber != numberOfPasses - 1:
                    cutter.position.y += cutter.swatheWidth
                    cutter.position.x = cuttingPlane.origin.x + cuttingPlane.size.x
                    moveInAStraightLineRapidly(cutter.position.x, cutter.position.y, cutter.position.z)

                    cutter.position.z = cuttingPlane.origin.z + 1
                    moveInAStraightLine(cutter.position.x, cutter.position.y, cutter.position.z, 500)  # fairly quick move to 1 mm above workpiece

                    cutter.position.z = cuttingPlane.origin.z
                    moveInAStraightLine(cutter.position.x, cutter.position.y, cutter.position.z, plungeSpeed)  # cut slowly down into workpiece

        cutter.position.z = clearanceHeight    # raise the cutting tool to clearanceHeight above the current z-height, in case you want to do another (finishing) pass.
        if pattern == 'climb-cut':
            moveInAStraightLineRapidly(cutter.position.x, cutter.position.y, cutter.position.z)
        else:
            moveInAStraightLineRapidly(cutter.position.x, cutter.position.y, cutter.position.z)
        stopSpindle()
        cutter.position.x = workPiece.origin.x
        cutter.position.y = workPiece.origin.y
        moveInAStraightLineRapidly(cutter.position.x, cutter.position.y, cutter.position.z)
        response = input("Do another pass? y/n ")
        if response not in ["y", "Y"]:
            break

    cutter.position.z = clearanceHeight
    moveInAStraightLineRapidly(cutter.position.x, cutter.position.y, cutter.position.z)

    return 0

# noinspection PyUnboundLocalVariable
if __name__ == '__main__':
    sys.exit(main(sys.argv))
