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

    print("sending $X\\n")  # Kill Alarm Lock state.
    grblController.write(b'$X\n')  # returns the number of bytes written
    line = grblController.readline()
    print(str(line))
    if line != b"[MSG:Caution: Unlocked]\r\n":
        print("Unexpected response " + str(line))
        sys.exit(1)
    line = grblController.readline()
    print(str(line))
    if line != b"ok\r\n":
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

    direction = '0'
    while True:
        # 0 top right z-up
        # 1 top left z-up
        # 2 bottom right z-up
        # 3 bottom left z-up
        # 4 top right z-down
        if direction == b'1':
            direction = b'0'
        else:
            direction = b'1'
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

