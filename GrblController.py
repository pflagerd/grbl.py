import serial
import sys

from types import SimpleNamespace


class GrblController(serial.Serial):
    class HomingPositions:
        topRightZUp = b'0'
        topLeftZUp = b'1'
        bottomRightZUp = b'2'
        bottomLeftZUp = b'3'
        topRightZDown = b'4'

    def __init__(self, portDeviceName="/dev/ttyUSB0", portBaudRate=115200):

        self.homingPosition = self.HomingPositions.bottomLeftZUp

        self.portDeviceName = portDeviceName
        self.portBaudRate = portBaudRate
        super().__init__(self.portDeviceName, self.portBaudRate)

        line = super().readline()
        print(str(line))
        if line != b"\r\n":
            print("Unexpected response " + str(line))
            sys.exit(1)
        line = super().readline()
        print(str(line))
        if line != b"Grbl 1.1h ['$' for help]\r\n":
            print("Unexpected response " + str(line))
            sys.exit(1)
        line = super().readline()
        print(str(line))
        if line != b"[MSG:'$H'|'$X' to unlock]\r\n":
            print("Unexpected response " + str(line))
            sys.exit(1)

    def __del__(self):
        if super():
            super().close()

    # See: https://github.com/gnea/grbl/wiki/Grbl-v1.1-Commands#:~:text=run%20as%20normal.-,%24H%20%2D%20Run%20homing%20cycle,-This%20command%20is
    def runHomingCycle(self, homingPosition=HomingPositions.bottomLeftZUp):
        self.homingPosition = homingPosition
        home_direction_invert = b'$23=' + homingPosition
        print("sending " + str(home_direction_invert) + "\\n")
        super().write(home_direction_invert + b'\n')
        while True:
            line = super().readline()
            print(str(line))
            if line == b"ok\r\n":
                break

        print("sending $H\\n")  # Homing Cycle.
        super().write(b'$H\n')
        while True:
            line = super().readline()
            print(str(line))
            if line == b"ok\r\n":
                break
