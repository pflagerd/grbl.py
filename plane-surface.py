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

    clearanceHeight = 5  # 10 mm.  The height to safely move the head over the workpiece.

    moveInAStraightLineRapidly(0, 0, clearanceHeight)  # move the cutting tool to 10mms

    workPiece = type('', (), {})()
    workPiece.origin = type('', (), {})()
    workPiece.origin.x = 0
    workPiece.origin.y = 0
    workPiece.origin.z = 0
    workPiece.size = type('', (), {})()
    workPiece.size.x = 410
    workPiece.size.y = 290

    facingDepth = 0.0  # mm cutting depth

    cutter = type('', (), {})()
    cutter.diameter = 25  # mm diameter
    cutter.overlapPercentage = 33  # percent of diameter
    cutter.swatheWidth = cutter.diameter - cutter.diameter * cutter.overlapPercentage / 100  # width of cutter movement in y direction
    cutter.position = type('', (), {})()
    cutter.position.x = 0
    cutter.position.y = 0
    cutter.position.z = 0

    cuttingSpeed = 600

    cuttingPlane = type('', (), {})()
    cuttingPlane.origin = type('', (), {})()
    cuttingPlane.origin.x = -cutter.diameter / 2
    cuttingPlane.origin.y = 0
    cuttingPlane.origin.z = -facingDepth
    cuttingPlane.size = type('', (), {})()
    cuttingPlane.size.x = workPiece.size.x + cutter.diameter
    cuttingPlane.size.y = workPiece.size.y

    pattern = 'climb-cut' # 'bidirectional`

    numberOfPasses = int(ceil(workPiece.size.y / cutter.swatheWidth + 1))  # The + 1 indicates that we are making an inclusive number of cuts (lines rather than spaces between the lines)
    cutter.swatheWidth *= (numberOfPasses - 1) / numberOfPasses

    # except that if cutting `bidirectional` we want to always end on a standard cut (not a climb cut), which means we want an odd number of passes
    if pattern == 'bidirectional' and numberOfPasses % 2 == 0:
        cutter.swatheWidth *= numberOfPasses / (numberOfPasses + 1)
        numberOfPasses += 1

    plungeSpeed = 20

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
        moveInAStraightLine(cutter.position.x, cutter.position.y, cutter.position.z, 500)  # fairly quick move to 1 mm above workpiece

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
                    moveInAStraightLine(cutter.position.x, cutter.position.y, cutter.position.z, 100)  # cut slowly down into workpiece

        cutter.position.z = dustShoeClearanceHeight    # raise the cutting tool to clearanceHeight above the current z-height, in case you want to do another (finishing) pass.
        if pattern == 'climb-cut:':
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

    cutter.position.z = dustShoeClearanceHeight
    moveInAStraightLineRapidly(cutter.position.x, cutter.position.y, cutter.position.z)

    return 0

# noinspection PyUnboundLocalVariable
if __name__ == '__main__':
    sys.exit(main(sys.argv))
