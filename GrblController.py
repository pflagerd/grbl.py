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
        self.spindleMotorSpeed = 0
        self.homingPosition = self.HomingPositions.bottomLeftZUp
        self.machineCoordinates = None

        self.portDeviceName = portDeviceName
        self.portBaudRate = portBaudRate
        super().__init__(self.portDeviceName, self.portBaudRate, timeout=30)

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

        self.getMachineCoordinates()

        # TODO: DPP: Set spindle motor to 0

        # TODO: DPP: Set origin to lower left corner

    def __del__(self):
        if super():  # TODO: ChatGPT suggested super().close() and it crashed so I added the if.  I don't about this.
            super().close()

    def getMachineCoordinates(self):
        # get current machine coordinates
        print("sending ?\\n")  # Status report query.
        super().write(b'?\n')
        while True:
            line = super().readline()
            print(str(line))
            # b'<Idle|MPos:-417.000,-307.000,-3.000|Bf:15,127|FS:0,0|WCO:-417.000,-307.000,-3.000>\r\n'
            if b'MPos' in line:
                statusLines = line.decode('utf-8').split('|')
                indexOfColon = statusLines[1].index(':')
                self.machineCoordinates = statusLines[1][indexOfColon + 1:].split(',')
                print('machineCoordinates == ', self.machineCoordinates)
            if line == b"ok\r\n":
                break

        return list(self.machineCoordinates)

    def cutToMachineCoordinates(self, x=None, y=None, z=None, feedRate=400):
        if x is None and y is None and z in None:
            print("At least one of x, y and z must be set")
            return self.getMachineCoordinates()

        gcode = f"G90 G53 G1 F{feedRate}"
        if x is not None:
            gcode += f" X{x}"
        if y is not None:
            gcode += f" Y{y}"
        if z is not None:
            gcode += f" Z{z}"

        gcode += "\r\n"

        print(gcode)
        super().write(gcode.encode('utf-8'))
        while True:
            line = super().readline()
            print(str(line))
            if line in [b"ok\r\n", b""]:
                break

        if line == b'ok\r\n':
            if x is not None:
                self.machineCoordinates[0] = x
            if y is not None:
                self.machineCoordinates[1] = y
            if z is not None:
                self.machineCoordinates[2] = z

        # get current machine coordinates
        return self.getMachineCoordinates()

    def moveFastToMachineCoordinates(self, x=None, y=None, z=None):
        if x is None and y is None and z in None:
            print("At least one of x, y and z must be set")
            return self.getMachineCoordinates()

        gcode = f"G90 G53 G0 "
        if x is not None:
            gcode += f" X{x}"
        if y is not None:
            gcode += f" Y{y}"
        if z is not None:
            gcode += f" Z{z}"

        gcode += "\r\n"

        print(gcode)
        super().write(gcode.encode('utf-8'))
        while True:
            line = super().readline()
            print(str(line))
            if line in [b"ok\r\n", b""]:
                break

        if line == b'ok\r\n':
            if x is not None:
                self.machineCoordinates[0] = x
            if y is not None:
                self.machineCoordinates[1] = y
            if z is not None:
                self.machineCoordinates[2] = z

        # get current machine coordinates
        return self.getMachineCoordinates()

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

    def startSpindleMotor(self, speed=1000):
        self.spindleMotorSpeed = speed
        grbl = f'S{speed} M3\r\n'
        print(grbl)
        super().write(grbl.encode('utf-8'))
        while True:
            line = super().readline()
            print(str(line))
            if line in [b"ok\r\n", b'']:
                break

        return speed

    def stopSpindleMotor(self, speed=0):
        self.spindleMotorSpeed = speed
        grbl = f'S{speed} M5\r\n'
        print(grbl)
        super().write(grbl.encode('utf-8'))
        while True:
            line = super().readline()
            print(str(line))
            if line in [b"ok\r\n", b'']:
                break

        return speed
