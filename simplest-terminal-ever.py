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
        line = input("Enter something: ")
        if len(line) != 0:
            print(str(line))
            line += "\n"
            grblController.write(line.encode('utf-8'))

    grblController.close()
