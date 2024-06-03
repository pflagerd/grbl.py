#!/usr/env python
import serial
import sys

port = "/dev/ttyUSB0"

if __name__ == '__main__':
    grblController = serial.Serial(port, baudrate=115200)

    looping = True
    while looping:
        line = grblController.readline()
        print(str(line))

    grblController.close()
