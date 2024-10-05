import serial
import sys

from types import SimpleNamespace


class GrblController(serial.Serial):
    HomingPositions = SimpleNamespace()

    def __init__(self, portDeviceName="/dev/ttyUSB0", portBaudRate=115200):
        self.HomingPositions.topRightZUp    = b'0'
        self.HomingPositions.topLeftZUp     = b'1'
        self.HomingPositions.bottomRightZUp = b'2'
        self.HomingPositions.bottomLeftZUp  = b'3'
        self.HomingPositions.topRightZDown  = b'4'

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
        super().close()

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
