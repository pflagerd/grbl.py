#!/usr/env python
from typing import Any

import serial
import sys

port = "/dev/ttyUSB0"
grblController = serial.Serial(port, baudrate=115200)


def pause(seconds):
    sendToGrbl("G4 P" + str(seconds))


def doHomeCycle():
    # 0 top right z-up
    # 1 top left z-up
    # 2 bottom right z-up
    # 3 bottom left z-up
    # 4 top right z-down
    sendToGrbl("$23=3")
    sendToGrbl("$H")  # Run Home Cycle
    sendToGrbl("G90")  # Set Absolute Coordinates
    sendToGrbl("G54")  # Coordinate System 1
    sendToGrbl("G10 L20 P1 X0 Y0 Z0")  # Reset origin


def drillHoleWithPecking(x, y, z, depth, pecks, peckSpeed):
    moveInAStraightLine(x, y, z, 500)

    depthPerPeck = depth / pecks

    z_retract = z
    for i in range(pecks):
        if i != 0:
            moveInAStraightLine(x, y, z_retract + 2, 500)
            pause(1)
        z = z_retract - depthPerPeck * (i + 1)
        if i != 0:
            moveInAStraightLine(x, y, z + depthPerPeck, 300)
        moveInAStraightLine(x, y, z, peckSpeed)

    moveInAStraightLine(x, y, z + 15, 500)


def initializeGrbl():
    line = grblController.readline()
    print(str(line))
    if line != b"\r\n":
        print("Unexpected response " + str(line))
        sys.exit(1)
    line = grblController.readline()
    print(str(line))
    if line != b"Grbl 1.1h ['$' for help]\r\n":
        print("Unexpected response " + str(line))
        sys.exit(1)
    line = grblController.readline()
    print(str(line))
    if line != b"[MSG:'$H'|'$X' to unlock]\r\n":
        print("Unexpected response " + str(line))
        sys.exit(1)
    sendToGrbl("$X")  # Unlock
    sendToGrbl("G90")  # (Set to absolute positioning)
    sendToGrbl("G54")  # (Select G54 as the active WCS)

def moveInAnArcClockwise(x, y, z, i, j, speed):
    sendToGrbl("F" + str(speed) + " G2 X" + str(x) + " Y" + str(y) + " Z" + str(z) + " I" + str(i) + " J" + str(j))


def moveInAStraightLine(x, y, z, speed):
    sendToGrbl("F" + str(speed) + " G1 X" + str(x) + " Y" + str(y) + " Z" + str(z))


def moveInAStraightLineRapidly(x, y, z):
    COORDINATED_MOVE_AT_RAPID_RATE = 'G0'  # A Rapid positioning move at the Rapid Feed Rate. In Laser mode Laser will be turned off.
    sendToGrbl("G0" + " X" + str(x) + " Y" + str(y) + " Z" + str(z))


def printGrblStatus():
    sendToGrbl("?")
    sendToGrbl("$$")
    sendToGrbl("$#")
    sendToGrbl("$G")
    sendToGrbl("$N")


def sendToGrbl(line):  # line is aka BLOCK in g-code context.
    print("sending " + line)  # Status report query.
    grblController.write(line.encode() + b'\n')
    while True:
        line = grblController.readline()
        if line == b"ok\r\n":
            break
        print(str(line))


def setOriginToCurrentLocation():
    sendToGrbl("G10 L20 P1 X0 Y0 Z0")  # (Set the current position as the zero point for G54)


def startSpindle(speed):
    sendToGrbl("M3 S" + str(speed))


def stopSpindle():
    sendToGrbl("M5")
