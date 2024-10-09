import serial
import sys


class GrblController(serial.Serial):
    class Vector3:
        def __init__(self, *args, **kwargs):
            if args and len(args) == 1 and isinstance(args[0], (list, tuple)):
                # Initialize from a list or tuple
                if len(args[0]) != 3:
                    raise ValueError("Exactly 3 values are required")
                self.x, self.y, self.z = tuple(float(n) for n in args[0])
            elif 'x' in kwargs or 'y' in kwargs or 'z' in kwargs:
                # Initialize from named arguments
                if 'x' in kwargs:
                    self.x = float(kwargs['x'])
                if 'y' in kwargs:
                    self.y = float(kwargs['y'])
                if 'z' in kwargs:
                    self.z = float(kwargs['z'])
            else:
                raise ValueError("Either provide a list/tuple or named arguments including at least one of 'x', 'y', or 'z'")

            self._attributes = (self.x, self.y, self.z)  # Store for index-based access

        def __getitem__(self, index):
            return self._attributes[index]

    class Point(Vector3):
        pass

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

        # TODO: DPP: Set spindle motor to 0

        # TODO: DPP: Set origin to lower left corner

        self.getMachineCoordinates()

    def __del__(self):
        if super():  # TODO: ChatGPT suggested super().close() and it crashed so I added the if.  I don't about this.
            super().close()

    def getMachineCoordinates(self):
        # get current machine coordinates
        print("sending ?\\r\\n")  # Status report query.
        super().write(b'?\r\n')
        while True:
            line = super().readline()
            print(str(line))
            # b'<Idle|MPos:-417.000,-307.000,-3.000|Bf:15,127|FS:0,0|WCO:-417.000,-307.000,-3.000>\r\n'
            if b'MPos' in line:
                statusLines = line.decode('utf-8').split('|')
                indexOfColon = statusLines[1].index(':')
                self.machineCoordinates = tuple(float(n) for n in statusLines[1][indexOfColon + 1:].split(','))
                print('machineCoordinates == ', self.machineCoordinates)
            if line == b"ok\r\n":
                break

        return self.Vector3(self.machineCoordinates)

    def cutToMachineCoordinates(self, x=None, y=None, z=None, feedRate=400):
        if x is None and y is None and z is None:
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
        if x is None and y is None and z is None:
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
