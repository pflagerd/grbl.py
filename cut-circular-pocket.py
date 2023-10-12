#!/usr/env python
from grbl import *
from math import *
from datetime import *


def main(args):
    initializeGrbl()

    print("Move cutting head to center of pocket. Cutter blade should be just touching material surface.")
    input("Hit enter when ready. ")

    setOriginToCurrentLocation()  # makes (X, Y, Z) = (0, 0, 0).  Uses G54 coordinates. Absolute.

    printGrblStatus()

    dustShoeClearanceHeight = 30
    cuttingSpeed = 300
    plungeSpeed = 20
    plungeDepth = 1
    depth = 10
    radius = 30.5

    workPiece = type('', (), {})()
    workPiece.origin = type('', (), {})()
    workPiece.origin.x = 0
    workPiece.origin.y = 0
    workPiece.origin.z = 0
    workPiece.size = type('', (), {})()
    workPiece.size.x = 90
    workPiece.size.y = 90

    cutter = type('', (), {})()
    cutter.radius = 11.8  # 23.6 mm diameter
    cutter.overlapPercentage = 66  # percent of cutter.radius
    cutter.swatheWidth = cutter.radius - cutter.radius * cutter.overlapPercentage / 100  # width of cutter movement in y direction
    cutter.position = type('', (), {})()
    cutter.position.x = 0
    cutter.position.y = 0
    cutter.position.z = 0

    if radius < cutter.radius:
        print("Cannot cut a radius (" + str(radius) + ") smaller than the cutter radius (" + str(cutter.diameter / 2) + ")")

    moveInAStraightLineRapidly(0, 0, dustShoeClearanceHeight)  # raise the cutting tool 50mm for dust shoe.
    input("Put dust shoe on cutter.  Hit enter when ready. ")

    while True:
        startTime = datetime.now()
        startSpindle(10000)  # start spindle

        cutter.position.x = cutter.position.y = cutter.position.z = 0
        moveInAStraightLineRapidly(cutter.position.x, cutter.position.y, cutter.position.z + 1)

        firstCut = True
        while cutter.position.z > -depth:
            cutRadius = cutter.radius
            cutter.position.z = max(cutter.position.z - plungeDepth, -depth)
            # moveInAStraightLine(cutter.position.x, cutter.position.y, cutter.position.z, plungeSpeed)

            lastCutRadius = cutRadius
            while cutRadius < radius:
                cutRadius = min(cutRadius + cutter.swatheWidth, radius)

                cutter.position.x = (cutRadius - cutter.radius)
                moveInAnArcClockwise(cutter.position.x, cutter.position.y, cutter.position.z, (cutRadius - lastCutRadius) / 2, 0, plungeSpeed if firstCut else cuttingSpeed / 2)
                moveInAnArcClockwise(cutter.position.x, cutter.position.y, cutter.position.z, -(cutRadius - cutter.radius), 0, cuttingSpeed / 5 if firstCut else cuttingSpeed)

                firstCut = False
                lastCutRadius = cutRadius

            moveInAnArcClockwise(cutter.position.x, cutter.position.y, cutter.position.z, -(cutRadius - cutter.radius),
                                 0, cuttingSpeed)
            moveInAStraightLineRapidly(cutter.position.x, cutter.position.y, cutter.position.z + 1)
            cutter.position.x = cutter.position.y = 0
            moveInAStraightLineRapidly(cutter.position.x, cutter.position.y, cutter.position.z)

        moveInAStraightLineRapidly(0, 0, 0)  # raise the cutting tool to its starting position.
        moveInAStraightLineRapidly(0, 0, dustShoeClearanceHeight)  # raise the cutting tool to its starting position.
        stopSpindle()
        print("Elapsed time " + str(datetime.now() - startTime))

        response = input("Do another pass? y/N ")
        if response not in ["y", "Y"]:
            break

    return 0

# noinspection PyUnboundLocalVariable
if __name__ == '__main__':
    sys.exit(main(sys.argv))
