#!/usr/env python
import serial
import sys

port = "/dev/ttyUSB0"

top_right_z_up =    b'0'
top_left_z_up =     b'1'
bottom_right_z_up = b'2'
bottom_left_z_up =  b'3'
top_right_z_down =  b'4'

def find_and_set_home(corner=bottom_left_z_up):
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

    print("sending $I\\n")  # View Build Info
    grblController.write(b'$I\n')
    while True:
        line = grblController.readline()
        print(str(line))
        if line == b"ok\r\n":
            break

    print("sending $N\\n")  # View saved start up code
    grblController.write(b'$N\n')
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

    # b'0' top right z-up
    # b'1' top left z-up
    # b'2' bottom right z-up
    # b'3' bottom left z-up
    # b'4' top right z-down
    home_direction_invert = b'$23=' + corner
    print("sending " + str(home_direction_invert) + "\\n")  # Status report query.
    grblController.write(home_direction_invert + b'\n')
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


if __name__ == '__main__':
    find_and_set_home()
