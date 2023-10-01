#!/usr/env python
import serial
import sys

port = "/dev/ttyUSB0"

if __name__ == '__main__':
    grblController = serial.Serial(port, baudrate=115200)
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

    print("sending $$\\n")  # Display Grbl Settings.
    grblController.write(b'$$\n')
    while True:
        line = grblController.readline()
        print(str(line))
        if line == b"ok\r\n":
            break

    print("sending $#\\n")  # View GCode Parameters.
    grblController.write(b'$#\n')
    while True:
        line = grblController.readline()
        print(str(line))
        if line == b"ok\r\n":
            break

    VIEW_GCODE_PARSER_STATE = b'$G'
    print('sending ' + str(VIEW_GCODE_PARSER_STATE) + '\\n')  # Status report query.
    grblController.write(VIEW_GCODE_PARSER_STATE + b'\n')
    while True:
        line = grblController.readline()
        print(str(line))
        if line == b"ok\r\n":
            break

    print("sending ?\\n")  # Status report query.
    grblController.write(b'?\n')
    while True:
        line = grblController.readline()
        print(str(line))
        if line == b"ok\r\n":
            break


    # 0 top right z-up
    # 1 top left z-up
    # 2 bottom right z-up
    # 3 bottom left z-up
    # 4 top right z-down
    direction = b'3'
    HOME_DIRECTION_INVERT = b'$23=' + direction
    print("sending " + str(HOME_DIRECTION_INVERT) + "\\n")  # Status report query.
    grblController.write(HOME_DIRECTION_INVERT + b'\n')
    while True:
        line = grblController.readline()
        print(str(line))
        if line == b"ok\r\n":
            break

    print("sending $H\\n")  # Homing Cycle.
    grblController.write(b'$H\n')
    while True:
        line = grblController.readline()
        print(str(line))
        if line == b"ok\r\n":
            break

    SET_ABSOLUTE_DISTANCE_MODE = b'G90'  # All distances and positions are Absolute values from the current origin.
    print("sending " + str(SET_ABSOLUTE_DISTANCE_MODE) + "\\n")
    grblController.write(SET_ABSOLUTE_DISTANCE_MODE + b'\n')
    while True:
        line = grblController.readline()
        print(str(line))
        if line == b"ok\r\n":
            break

    SET_COORDINATE_SYSTEM = b'G10 L20 P0 X0 Y0 Z0'
    print("sending " + str(SET_COORDINATE_SYSTEM) + "\\n")
    grblController.write(SET_COORDINATE_SYSTEM + b'\n')
    while True:
        line = grblController.readline()
        print(str(line))
        if line == b"ok\r\n":
            break

    print("sending $$\\n")  # Display Grbl Settings.
    grblController.write(b'$$\n')
    while True:
        line = grblController.readline()
        print(str(line))
        if line == b"ok\r\n":
            break

    print("sending $#\\n")  # View GCode Parameters.
    grblController.write(b'$#\n')
    while True:
        line = grblController.readline()
        print(str(line))
        if line == b"ok\r\n":
            break

    print("sending ?\\n")  # Status report query.
    grblController.write(b'?\n')
    while True:
        line = grblController.readline()
        print(str(line))
        if line == b"ok\r\n":
            break

    VIEW_GCODE_PARSER_STATE = b'$G'
    print('sending ' + str(VIEW_GCODE_PARSER_STATE) + '\\n')  # Status report query.
    grblController.write(VIEW_GCODE_PARSER_STATE + b'\n')
    while True:
        line = grblController.readline()
        print(str(line))
        if line == b"ok\r\n":
            break

    grblController.close()


