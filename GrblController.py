import serial
import sys


class GrblController(serial.Serial):
    class Vector:
        def __init__(self, *args, **kwargs):
            if args and len(args) == 1 and isinstance(args[0], (list, tuple)):
                # Initialize from a list or tuple
                listOrTuple = args[0]
                args = tuple(float(n) for n in listOrTuple)
            if args and 0 < len(args) < 4:  # check out this cool syntax for chained comparisons
                self.x = float(args[0])
                self.y = self.z = None
                if len(args) > 1:
                    self.y = float(args[1])
                if len(args) > 2:
                    self.z = float(args[2])
                self._attributes = (self.x, self.y, self.z)  # Store for index-based access
            elif 'x' in kwargs or 'y' in kwargs or 'z' in kwargs:
                # Initialize from named arguments
                self.x = self.y = self.z = None
                if 'x' in kwargs:
                    self.x = float(kwargs['x'])
                if 'y' in kwargs:
                    self.y = float(kwargs['y'])
                if 'z' in kwargs:
                    self.z = float(kwargs['z'])
                self._attributes = (self.x, self.y, self.z)  # Store for index-based access
            else:
                raise ValueError("Either provide a list/tuple or named arguments including at least one of 'x', 'y', or 'z'")

        def __getitem__(self, index):
            return self._attributes[index]

        def __str__(self):
            return f'({self._attributes[0]}, {self._attributes[1]}, {self._attributes[2]})'

    class Point(Vector):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)

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

        # TODO: DPP: Set spindle motor to 1000

        # TODO: DPP: Set origin to lower left corner

        # TODO: DPP: Turn off spindle motor

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
                self.machineCoordinates = GrblController.Vector(tuple(float(n) for n in statusLines[1][indexOfColon + 1:].split(',')))
                print('machineCoordinates == ', self.machineCoordinates)
            if line == b"ok\r\n":
                break

        return self.machineCoordinates

    def cutToMachineCoordinates(self, *args, **kwargs):
        # parse args and fail if correct ones not there
        newPosition = GrblController.Vector(*args, **kwargs)

        feedRate = 400
        if kwargs['feedRate']:
            feedRate = float(feedRate)

        spindleSpeed = 1000
        if kwargs['feedRate']:
            feedRate = float(feedRate)

        # Set speed of spindle motor, and run it
        gcode = f"S{spindleSpeed} M3\r\n"
        print(gcode)
        super().write(gcode.encode('utf-8'))
        while True:
            line = super().readline()
            print(str(line))
            if line in [b"ok\r\n", b""]:
                break

        gcode = f"G90 G53 G1 F{feedRate}"
        if newPosition.x is not None:
            gcode += f" X{newPosition.x}"
        if newPosition.y is not None:
            gcode += f" Y{newPosition.y}"
        if newPosition.z is not None:
            gcode += f" Z{newPosition.z}"

        gcode += "\r\n"

        print(gcode)
        super().write(gcode.encode('utf-8'))
        while True:
            line = super().readline()
            print(str(line))
            if line in [b"ok\r\n", b""]:
                break

        # get current machine coordinates
        return self.getMachineCoordinates()

    def moveToMachineCoordinates(self, *args, **kwargs):
        # if spindle motor is running, stop it, regardless of what args are passed
        gcode = f"M5\r\n"
        print(gcode)
        super().write(gcode.encode('utf-8'))
        while True:
            line = super().readline()
            print(str(line))
            if line in [b"ok\r\n", b""]:
                break

        # parse args and fail if correct ones not there
        # move to new position
        newPosition = GrblController.Vector(*args, **kwargs)
        gcode = f"G90 G53 G0 "
        if newPosition.x is not None:
            gcode += f" X{newPosition.x}"
        if newPosition.y is not None:
            gcode += f" Y{newPosition.y}"
        if newPosition.z is not None:
            gcode += f" Z{newPosition.z}"

        gcode += "\r\n"

        print(gcode)
        super().write(gcode.encode('utf-8'))
        while True:
            line = super().readline()
            print(str(line))
            if line in [b"ok\r\n", b""]:
                break

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

        return self.getMachineCoordinates()

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


if __name__ == "__main__":
    # Vector Tests
    # tuple Tests
    try:
        thing = GrblController.Vector(())
        print(thing)
    except ValueError as valueError:
        assert valueError.args[0] == "Either provide a list/tuple or named arguments including at least one of 'x', 'y', or 'z'"

    thing = GrblController.Vector((1,))
    print(thing)
    assert str(thing) == '(1.0, None, None)'

    thing = GrblController.Vector((1, 2,))
    print(thing)
    assert str(thing) == '(1.0, 2.0, None)'

    thing = GrblController.Vector((1, 2, 3,))
    print(thing)
    assert str(thing) == '(1.0, 2.0, 3.0)'

    try:
        thing = GrblController.Vector((1, 2, 3, 4))
        print(thing)
    except ValueError as valueError:
        assert valueError.args[0] == "Either provide a list/tuple or named arguments including at least one of 'x', 'y', or 'z'"

    try:
        thing = GrblController.Vector((1, 2, 3, 4, 5))
        print(thing)
    except ValueError as valueError:
        assert valueError.args[0] == "Either provide a list/tuple or named arguments including at least one of 'x', 'y', or 'z'"

    try:
        thing = GrblController.Vector((1, 2, 3, 4, 5, 6))
        print(thing)
    except ValueError as valueError:
        assert valueError.args[0] == "Either provide a list/tuple or named arguments including at least one of 'x', 'y', or 'z'"

    try:
        thing = GrblController.Vector((1,), (2,))
        print(thing)
    except TypeError as typeError:
        assert typeError.args[0] == "float() argument must be a string or a real number, not 'tuple'"

    # list Tests
    try:
        thing = GrblController.Vector([])
        print(thing)
    except ValueError as valueError:
        assert valueError.args[0] == "Either provide a list/tuple or named arguments including at least one of 'x', 'y', or 'z'"

    thing = GrblController.Vector([1])
    print(thing)
    assert str(thing) == '(1.0, None, None)'

    thing = GrblController.Vector([1, 2])
    print(thing)
    assert str(thing) == '(1.0, 2.0, None)'

    thing = GrblController.Vector([1, 2, 3])
    print(thing)
    assert str(thing) == '(1.0, 2.0, 3.0)'

    try:
        thing = GrblController.Vector([1, 2, 3, 4])
        print(thing)
    except ValueError as valueError:
        assert valueError.args[0] == "Either provide a list/tuple or named arguments including at least one of 'x', 'y', or 'z'"

    try:
        thing = GrblController.Vector([1, 2, 3, 4, 5])
        print(thing)
    except ValueError as valueError:
        assert valueError.args[0] == "Either provide a list/tuple or named arguments including at least one of 'x', 'y', or 'z'"

    try:
        thing = GrblController.Vector([1, 2, 3, 4, 5, 6])
        print(thing)
    except ValueError as valueError:
        assert valueError.args[0] == "Either provide a list/tuple or named arguments including at least one of 'x', 'y', or 'z'"

    try:
        thing = GrblController.Vector([1], [2])
        print(thing)
    except TypeError as typeError:
        assert typeError.args[0] == "float() argument must be a string or a real number, not 'list'"

    # positional argument tests
    try:
        thing = GrblController.Vector()
        print(thing)
    except ValueError as valueError:
        assert valueError.args[0] == "Either provide a list/tuple or named arguments including at least one of 'x', 'y', or 'z'"

    thing = GrblController.Vector(1)
    print(thing)
    assert str(thing) == '(1.0, None, None)'

    thing = GrblController.Vector(1, 2)
    print(thing)
    assert str(thing) == '(1.0, 2.0, None)'

    thing = GrblController.Vector(1, 2, 3)
    print(thing)
    assert str(thing) == '(1.0, 2.0, 3.0)'

    try:
        thing = GrblController.Vector(1, 2, 3, 4)
        print(thing)
    except ValueError as valueError:
        assert valueError.args[0] == "Either provide a list/tuple or named arguments including at least one of 'x', 'y', or 'z'"

    try:
        thing = GrblController.Vector(1, 2, 3, 4, 5)
        print(thing)
    except ValueError as valueError:
        assert valueError.args[0] == "Either provide a list/tuple or named arguments including at least one of 'x', 'y', or 'z'"

    try:
        thing = GrblController.Vector(1, 2, 3, 4, 5, 6)
        print(thing)
    except ValueError as valueError:
        assert valueError.args[0] == "Either provide a list/tuple or named arguments including at least one of 'x', 'y', or 'z'"

    # keyword argument tests
    try:
        thing = GrblController.Vector(junk=3)
        print(thing)
    except ValueError as valueError:
        assert valueError.args[0] == "Either provide a list/tuple or named arguments including at least one of 'x', 'y', or 'z'"

    thing = GrblController.Vector(x=1)
    print(thing)
    assert str(thing) == '(1.0, None, None)'

    thing = GrblController.Vector(y=2)
    print(thing)
    assert str(thing) == '(None, 2.0, None)'

    thing = GrblController.Vector(z=3)
    print(thing)
    assert str(thing) == '(None, None, 3.0)'

    thing = GrblController.Vector(x=1, y=2)
    print(thing)
    assert str(thing) == '(1.0, 2.0, None)'

    thing = GrblController.Vector(x=1, z=3)
    print(thing)
    assert str(thing) == '(1.0, None, 3.0)'

    thing = GrblController.Vector(y=2, z=3)
    print(thing)
    assert str(thing) == '(None, 2.0, 3.0)'

    thing = GrblController.Vector(x=1, y=2, z=3)
    print(thing)
    assert str(thing) == '(1.0, 2.0, 3.0)'

    try:
        thing = GrblController.Vector(x=1, junk=3)
        print(thing)
    except ValueError as valueError:
        assert valueError.args[0] == "Either provide a list/tuple or named arguments including at least one of 'x', 'y', or 'z'"

    # Point tests
    # GrblController.Point class is effectively an alias for Grbl.Vector
    # All the tests below are derived from the Vector tests above, substituting the word Point for Vector
    # tuple Tests
    try:
        thing = GrblController.Point(())
        print(thing)
    except ValueError as valueError:
        assert valueError.args[0] == "Either provide a list/tuple or named arguments including at least one of 'x', 'y', or 'z'"

    thing = GrblController.Point((1,))
    print(thing)
    assert str(thing) == '(1.0, None, None)'

    thing = GrblController.Point((1, 2,))
    print(thing)
    assert str(thing) == '(1.0, 2.0, None)'

    thing = GrblController.Point((1, 2, 3,))
    print(thing)
    assert str(thing) == '(1.0, 2.0, 3.0)'

    try:
        thing = GrblController.Point((1, 2, 3, 4))
        print(thing)
    except ValueError as valueError:
        assert valueError.args[0] == "Either provide a list/tuple or named arguments including at least one of 'x', 'y', or 'z'"

    try:
        thing = GrblController.Point((1, 2, 3, 4, 5))
        print(thing)
    except ValueError as valueError:
        assert valueError.args[0] == "Either provide a list/tuple or named arguments including at least one of 'x', 'y', or 'z'"

    try:
        thing = GrblController.Point((1, 2, 3, 4, 5, 6))
        print(thing)
    except ValueError as valueError:
        assert valueError.args[0] == "Either provide a list/tuple or named arguments including at least one of 'x', 'y', or 'z'"

    try:
        thing = GrblController.Point((1,), (2,))
        print(thing)
    except TypeError as typeError:
        assert typeError.args[0] == "float() argument must be a string or a real number, not 'tuple'"

    # list Tests
    try:
        thing = GrblController.Point([])
        print(thing)
    except ValueError as valueError:
        assert valueError.args[0] == "Either provide a list/tuple or named arguments including at least one of 'x', 'y', or 'z'"

    thing = GrblController.Point([1])
    print(thing)
    assert str(thing) == '(1.0, None, None)'

    thing = GrblController.Point([1, 2])
    print(thing)
    assert str(thing) == '(1.0, 2.0, None)'

    thing = GrblController.Point([1, 2, 3])
    print(thing)
    assert str(thing) == '(1.0, 2.0, 3.0)'

    try:
        thing = GrblController.Point([1, 2, 3, 4])
        print(thing)
    except ValueError as valueError:
        assert valueError.args[0] == "Either provide a list/tuple or named arguments including at least one of 'x', 'y', or 'z'"

    try:
        thing = GrblController.Point([1, 2, 3, 4, 5])
        print(thing)
    except ValueError as valueError:
        assert valueError.args[0] == "Either provide a list/tuple or named arguments including at least one of 'x', 'y', or 'z'"

    try:
        thing = GrblController.Point([1, 2, 3, 4, 5, 6])
        print(thing)
    except ValueError as valueError:
        assert valueError.args[0] == "Either provide a list/tuple or named arguments including at least one of 'x', 'y', or 'z'"

    try:
        thing = GrblController.Point([1], [2])
        print(thing)
    except TypeError as typeError:
        assert typeError.args[0] == "float() argument must be a string or a real number, not 'list'"

    # positional argument tests
    try:
        thing = GrblController.Point()
        print(thing)
    except ValueError as valueError:
        assert valueError.args[0] == "Either provide a list/tuple or named arguments including at least one of 'x', 'y', or 'z'"

    thing = GrblController.Point(1)
    print(thing)
    assert str(thing) == '(1.0, None, None)'

    thing = GrblController.Point(1, 2)
    print(thing)
    assert str(thing) == '(1.0, 2.0, None)'

    thing = GrblController.Point(1, 2, 3)
    print(thing)
    assert str(thing) == '(1.0, 2.0, 3.0)'

    try:
        thing = GrblController.Point(1, 2, 3, 4)
        print(thing)
    except ValueError as valueError:
        assert valueError.args[0] == "Either provide a list/tuple or named arguments including at least one of 'x', 'y', or 'z'"

    try:
        thing = GrblController.Point(1, 2, 3, 4, 5)
        print(thing)
    except ValueError as valueError:
        assert valueError.args[0] == "Either provide a list/tuple or named arguments including at least one of 'x', 'y', or 'z'"

    try:
        thing = GrblController.Point(1, 2, 3, 4, 5, 6)
        print(thing)
    except ValueError as valueError:
        assert valueError.args[0] == "Either provide a list/tuple or named arguments including at least one of 'x', 'y', or 'z'"

    # keyword argument tests
    try:
        thing = GrblController.Point(junk=3)
        print(thing)
    except ValueError as valueError:
        assert valueError.args[0] == "Either provide a list/tuple or named arguments including at least one of 'x', 'y', or 'z'"

    thing = GrblController.Point(x=1)
    print(thing)
    assert str(thing) == '(1.0, None, None)'

    thing = GrblController.Point(y=2)
    print(thing)
    assert str(thing) == '(None, 2.0, None)'

    thing = GrblController.Point(z=3)
    print(thing)
    assert str(thing) == '(None, None, 3.0)'

    thing = GrblController.Point(x=1, y=2)
    print(thing)
    assert str(thing) == '(1.0, 2.0, None)'

    thing = GrblController.Point(x=1, z=3)
    print(thing)
    assert str(thing) == '(1.0, None, 3.0)'

    thing = GrblController.Point(y=2, z=3)
    print(thing)
    assert str(thing) == '(None, 2.0, 3.0)'

    thing = GrblController.Point(x=1, y=2, z=3)
    print(thing)
    assert str(thing) == '(1.0, 2.0, 3.0)'

    try:
        thing = GrblController.Point(x=1, junk=3)
        print(thing)
    except ValueError as valueError:
        assert valueError.args[0] == "Either provide a list/tuple or named arguments including at least one of 'x', 'y', or 'z'"
