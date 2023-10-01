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

    print("sending G90\\n")  # Absolute Coordinates
    grblController.write(b'G90\n')
    while True:
        line = grblController.readline()
        print(str(line))
        if line == b"ok\r\n":
            break

    print("sending G54\\n")  # Coordinate System 1
    grblController.write(b'G54\n')
    while True:
        line = grblController.readline()
        print(str(line))
        if line == b"ok\r\n":
            break

    print("sending G10 L20 P1 X0 Y0 Z0\\n")  # Reset origin
    grblController.write(b'G10 L20 P1 X0 Y0 Z0\n')
    while True:
        line = grblController.readline()
        print(str(line))
        if line == b"ok\r\n":
            break

    COORDINATED_MOVE_AT_RAPID_RATE = b'G0'  # A Rapid positioning move at the Rapid Feed Rate. In Laser mode Laser will be turned off.
    x = 175
    y = 161
    z = -81
    print("sending " + str(COORDINATED_MOVE_AT_RAPID_RATE) + " X" + str(x) + " Y" + str(y) + " Z" + str(z) + "\\n")
    grblController.write(COORDINATED_MOVE_AT_RAPID_RATE + b' X' + str(x).encode() + b' Y' + str(y).encode() + b' Z' + str(z).encode() + b'\n')
    while True:
        line = grblController.readline()
        print(str(line))
        if line == b"ok\r\n":
            break

    SET_FEED_RATE_30MMPM = "F30"  # Appears not to like being run by itself.
    LINEAR_MOVE = 'G1'  # Move left 1.2mm/2 (radius of the nail).
    x -= 0.6
    print("sending " + SET_FEED_RATE_30MMPM + " " + LINEAR_MOVE + " X" + str(x) + "\\n")
    grblController.write((SET_FEED_RATE_30MMPM + " " + LINEAR_MOVE + ' X' + str(x)).encode() + b'\n')
    while True:
        line = grblController.readline()
        print(str(line))
        if line == b"ok\r\n":
            break

    # print("sending G4 P3\\n")  # Dwell for 3 seconds
    grblController.write(b'M3 S10000 G4 P3\n')
    while True:
        line = grblController.readline()
        print(str(line))
        if line == b"ok\r\n":
            break

    XY_PLANE = 'G17'
    print("sending " + XY_PLANE + "\\n")
    grblController.write(XY_PLANE.encode() + b'\n')
    while True:
        line = grblController.readline()
        print(str(line))
        if line == b"ok\r\n":
            break

    CLOCKWISE_ARC = 'G2'
    i = 0.6
    j = 0
    while z > -82:
        print("sending " + SET_FEED_RATE_30MMPM + " " + CLOCKWISE_ARC + " X" + str(x) + " Y" + str(y) + " Z" + str(z) + " I" + str(i) + " J" + str(j) + "\\n")
        grblController.write((SET_FEED_RATE_30MMPM + " " + CLOCKWISE_ARC + " X" + str(x) + " Y" + str(y) + " Z" + str(z) + " I" + str(i) + " J" + str(j)).encode() + b'\n')
        while True:
            line = grblController.readline()
            print(str(line))
            if line == b"ok\r\n":
                break
        z -= 0.25

    z = 0
    print("sending " + str(COORDINATED_MOVE_AT_RAPID_RATE) + " Z" + str(z) + "\\n")
    grblController.write(COORDINATED_MOVE_AT_RAPID_RATE + (' Z' + str(z)).encode() + b'\n')
    while True:
        line = grblController.readline()
        print(str(line))
        if line == b"ok\r\n":
            break

    SPINDLE_OFF = 'M5'  # Move left 1.2mm/2 (radius of the nail).
    print("sending " + SPINDLE_OFF + "\\n")
    grblController.write(SPINDLE_OFF.encode() + b'\n')
    while True:
        line = grblController.readline()
        print(str(line))
        if line == b"ok\r\n":
            break

    #
    #
    #
    #
    #
    #
    # print("sending G4 P0\\n")  # Dwell for 0 seconds
    # grblController.write(b'G4 P0\n')
    # while True:
    #     line = grblController.readline()
    #     print(str(line))
    #     if line == b"ok\r\n":
    #         break
    #
    # print("sending ?\\n")  # Status report query.
    # grblController.write(b'?\n')
    # while True:
    #     line = grblController.readline()
    #     print(str(line))
    #     if line == b"ok\r\n":
    #         break
    #
    # print("sending $$\\n")  # Display Grbl Settings.
    # grblController.write(b'$$\n')
    # while True:
    #     line = grblController.readline()
    #     print(str(line))
    #     if line == b"ok\r\n":
    #         break
    #
    # print("sending $#\\n")  # View GCode Parameters.
    # grblController.write(b'$#\n')
    # while True:
    #     line = grblController.readline()
    #     print(str(line))
    #     if line == b"ok\r\n":
    #         break
    #
    # VIEW_GCODE_PARSER_STATE = b'$G'
    # print('sending ' + str(VIEW_GCODE_PARSER_STATE) + '\\n')  # Status report query.
    # grblController.write(VIEW_GCODE_PARSER_STATE + b'\n')
    # while True:
    #     line = grblController.readline()
    #     print(str(line))
    #     if line == b"ok\r\n":
    #         break
    #
    # print("sending $N\\n")  # View start up script
    # grblController.write(b'$N\n')
    # while True:
    #     line = grblController.readline()
    #     print(str(line))
    #     if line == b"ok\r\n":
    #         break
    #
