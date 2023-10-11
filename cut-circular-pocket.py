#!/usr/env python
from grbl import *
from math import *

def main(args):
    initializeGrbl()

    print("Move cutting head to center of pocket. Cutter blade should be just touching material surface.")
    input("Hit enter when ready. ")

    setOriginToCurrentLocation()  # makes (X, Y, Z) = (0, 0, 0).  Uses G54 coordinates. Absolute.

    printGrblStatus()

    cuttingSpeed = 200
    plungeSpeed = 50
    plungeDepth = 1
    depth = 8
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
    cutter.radius = 12.5  # 25 mm diameter
    cutter.overlapPercentage = 33  # percent of cutter.radius
    cutter.swatheWidth = cutter.radius - cutter.radius * cutter.overlapPercentage / 100  # width of cutter movement in y direction
    cutter.position = type('', (), {})()
    cutter.position.x = 0
    cutter.position.y = 0
    cutter.position.z = 0

    if radius < cutter.diameter / 2:
        print("Cannot cut a radius (" + str(radius) + ") smaller than the cutter radius (" + str(cutter.diameter / 2) + ")")

    moveInAStraightLineRapidly(0, 0, dustShoeClearanceHeight)  # raise the cutting tool 50mm for dust shoe.
    input("Put dust shoe on cutter.  Hit enter when ready. ")

    while True:
        startSpindle(10000)  # start spindle

        cutter.position.x = cutter.position.y = cutter.position.z = 0
        moveInAStraightLineRapidly(cutter.position.x, cutter.position.y, cutter.position.z + 1)

        while cutter.position.z > -depth:
            cutRadius = cutter.radius
            cutter.position.z = max(cutter.position.z - plungeDepth, -depth)
            moveInAStraightLine(cutter.position.x, cutter.position.y, cutter.position.z, plungeSpeed)

            while cutRadius < radius:
                cutRadius = min(cutRadius + cutter.swatheWidth, radius)

                cutter.position.x += (cutRadius - cutter.radius)
                moveInAnArcClockwise(cutter.position.x, cutter.position.y, cutter.position.z, (cutRadius - cutter.radius) / 2, 0, cuttingSpeed)
                moveInAnArcClockwise(cutter.position.x, cutter.position.y, cutter.position.z, -(cutRadius - cutter.radius), 0, cuttingSpeed)

            moveInAStraightLineRapidly(cutter.position.x, cutter.position.y, cutter.position.z + 1)
            cutter.position.x = cutter.position.y = 0
            moveInAStraightLineRapidly(cutter.position.x, cutter.position.y, cutter.position.z + 1)

        stopSpindle()

        response = input("Do another pass? y/N ")
        if response not in ["y", "Y"]:
            break

    moveInAStraightLineRapidly(0, 0, 0)  # raise the cutting tool to its starting position.
    return 0

# noinspection PyUnboundLocalVariable
if __name__ == '__main__':
    sys.exit(main(sys.argv))
