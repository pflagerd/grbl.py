#!/usr/env python
from grbl import *
import keyboard
import math

def main(args):
    initializeGrbl()

    doHomeCycle()

    center = type('', (), {})()
    center.x = 175
    center.y = 161

    x = center.x
    y = center.y
    z = -87
    depth = 6.6

    # Uncomment this to enter GCODE to find the correct numbers for x, y, and z above.
    # while True:
    #     s = input("enter gcode followed by  or Q to quit : ")
    #     if s in ["Q", "q"]:
    #         break
    #     sendToGrbl(s)
    # return

    # center hole.
    # drillHoleWithPecking(x, y, z, 9, 8)

    # nail head diameter is 3mm, so let's go with a radius of 3.5mm

    headRadius = 3.1  # head radius of 3 with a 0.1 tolerance
    radius = 3.5

    startSpindle(2000)  # start spindle
    for j in range(0, 14):  # 7, 11
        moveInAStraightLineRapidly(x, y, 0)
        pause(2)
        # how many holes may we fit into $2\pi \times 3.5 = 7\pi$ ?
        circumference = 2 * math.pi * j * radius
        holes = math.floor(circumference / headRadius)
        for i in range(0, holes):
            x, y = center.x + j * radius * math.cos(2 * math.pi * i / holes), center.y + j * radius * math.sin(2 * math.pi * i / holes)
            drillHoleWithPecking(x, y, z, depth, 4, 100)

    moveInAStraightLineRapidly(x, y, 0)
    stopSpindle()
    moveInAStraightLineRapidly(0, 0, 0)

# noinspection PyUnboundLocalVariable
if __name__ == '__main__':
    sys.exit(main(sys.argv))
