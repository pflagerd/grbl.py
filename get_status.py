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

    # print("sending $X\\n")  # Kill Alarm Lock state.
    # grblController.write(b'$X\n')  # returns the number of bytes written
    # line = grblController.readline()
    # print(str(line))
    # if line != b"[MSG:Caution: Unlocked]\r\n":
    #     print("Unexpected response " + str(line))
    #     sys.exit(1)
    # line = grblController.readline()
    # print(str(line))
    # if line != b"ok\r\n":
    #     print("Unexpected response " + str(line))
    #     sys.exit(1)

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
        # b'<Idle|MPos:-417.000,-307.000,-3.000|Bf:15,127|FS:0,0|WCO:-417.000,-307.000,-3.000>\r\n'
        if b'MPos' in line:
            statusLines = line.decode('utf-8').split('|')
            indexOfColon = statusLines[1].index(':')
            machineCoordinates = statusLines[1][indexOfColon + 1:].split(',')
            print('machineCoordinates == ', machineCoordinates)
        if line == b"ok\r\n":
            break

    grblController.close()
