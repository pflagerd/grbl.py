import serial
import sys

debug = False


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
        topRightZUp = '0'
        topLeftZUp = '1'
        bottomRightZUp = '2'
        bottomLeftZUp = '3'
        topRightZDown = '4'

    def __init__(self, portDeviceName="/dev/ttyUSB0", portBaudRate=115200):
        self.spindleMotorSpeed = 0
        self.homingPosition = self.HomingPositions.bottomLeftZUp
        self.machineCoordinates = None

        self.portDeviceName = portDeviceName
        self.portBaudRate = portBaudRate
        super().__init__(self.portDeviceName, self.portBaudRate, timeout=30)

        line = super().readline()
        if debug:
            print("received " + str(line))
        if line != b"\r\n":
            if debug:
                print("Unexpected response " + str(line))
            sys.exit(1)
        line = super().readline()
        if debug:
            print("received " + str(line))
        if line != b"Grbl 1.1h ['$' for help]\r\n":
            if debug:
                print("Unexpected response " + str(line))
            sys.exit(1)
        line = super().readline()
        if debug:
            print("received " + str(line))
        if line != b"[MSG:'$H'|'$X' to unlock]\r\n":
            if debug:
                print("Unexpected response " + str(line))
            sys.exit(1)

        # Unlock
        self.send("$X")

        # DPP: Set spindle motor to 1000
        self.send("S1000")

        # DPP: Set homing origin to lower left corner
        self.send('$23=' + GrblController.HomingPositions.bottomLeftZUp)

        # DPP: Turn off spindle motor
        self.send("M5")

        # DPP: Set acceleration
        self.send("$120=10")  # x 10mm/s
        self.send("$121=10")  # y
        self.send("$122=10")  # z

        # No point querying machineCoordinates with ? as they will always be inaccurate
        # since the message returned will be something like
        # b'<Alarm|MPos:0.000,0.000,0.000|Bf:15,127|FS:0,0|WCO:-405.000,-299.000,-84.000>\r\n'
        # where the "Alarm" status indicates the rest of the data is uncertain.

    def __del__(self):
        if super():  # TODO: ChatGPT suggested super().close() and it crashed so I added the if.  I don't about this.
            super().close()

    def getMachineCoordinates(self):
        # get current machine coordinates
        gcode = "?\n"

        gcode = gcode.encode('utf-8')
        if debug:
            print("sending " + str(gcode))  # Status report query.
        super().write(gcode)
        while True:
            line = super().readline()
            if debug:
                print("received " + str(line))
            # b'<Idle|MPos:-417.000,-307.000,-3.000|Bf:15,127|FS:0,0|WCO:-417.000,-307.000,-3.000>\r\n'
            if b'Idle' in line and b'MPos' in line:
                statusLines = line.decode('utf-8').split('|')
                indexOfColon = statusLines[1].index(':')
                self.machineCoordinates = GrblController.Vector(tuple(float(n) for n in statusLines[1][indexOfColon + 1:].split(',')))
                if debug:
                    print('machineCoordinates were read from status as ', self.machineCoordinates)
            if line == b"ok\r\n":
                break

        return self.machineCoordinates

    def getBuildInfo(self, debug=False):
        pass

    def getGrblState(self):
        pass

    def getGCodeParameters(self):
        pass

    def setGCodeParameter(self, parameterName, value):
        pass

    def getStartupGCode(self):
        pass

    def setStartupGCode(self):
        pass

    def getStatus(self, debug=False):
        # get current machine coordinates
        gcode = "?\n"

        gcode = gcode.encode('utf-8')
        if debug:
            print("sending " + str(gcode))  # Status report query.
        super().write(gcode)
        statusReport = None
        while True:
            line = super().readline()
            if debug:
                print("received " + str(line))
            # b'<Idle|MPos:-417.000,-307.000,-3.000|Bf:15,127|FS:0,0|WCO:-417.000,-307.000,-3.000>\r\n'
            if line.startswith(b'<'):
                statusReport = line.decode()
            if line == b"ok\r\n":
                break

        return statusReport

    def cut(self, *args, **kwargs):
        raise RuntimeError("Function not implemented yet")

    def cutClockwiseArc(self, *args, **kwargs):
        raise RuntimeError("Function not implemented yet")

    def cutCounterClockwiseArc(self, *args, **kwargs):
        raise RuntimeError("Function not implemented yet")

    def cutToMachineCoordinates(self, *args, **kwargs):
        # parse args and fail if correct ones not there
        if args and isinstance(args[0], GrblController.Vector):
            newPosition = args[0]
        else:
            newPosition = GrblController.Vector(*args, **kwargs)

        feedRate = 400
        if 'feedRate' in kwargs:
            feedRate = float(kwargs['feedRate'])

        spindleSpeed = 1000
        if 'spindleSpeed' in kwargs:
            spindleSpeed = float(kwargs['spindleSpeed'])

        # Set speed of spindle motor, and run it
        self.startSpindleMotor(spindleSpeed)

        gcode = f"G90 G53 G1 F{feedRate}"
        if newPosition.x is not None:
            gcode += f" X{newPosition.x}"
        if newPosition.y is not None:
            gcode += f" Y{newPosition.y}"
        if newPosition.z is not None:
            gcode += f" Z{newPosition.z}"

        self.sendAndWait(gcode)  # This one needs a sendAndWait() else the self.getMachineCoordinates() will not return the correct result

        # get current machine coordinates
        return self.getMachineCoordinates()

    def move(self, *args, **kwargs):
        raise RuntimeError("Function not implemented yet")

    def moveToMachineCoordinates(self, *args, **kwargs):
        # if spindle motor is running, stop it, regardless of what args are passed
        self.stopSpindleMotor()

        if args and isinstance(args[0], GrblController.Vector):
            newPosition = args[0]
        else:
            newPosition = GrblController.Vector(*args, **kwargs)

        # parse args and fail if correct ones not there
        # move to new position
        gcode = f"G90 G53 G0"
        if newPosition.x is not None:
            gcode += f" X{newPosition.x}"
        if newPosition.y is not None:
            gcode += f" Y{newPosition.y}"
        if newPosition.z is not None:
            gcode += f" Z{newPosition.z}"

        self.sendAndWait(gcode)  # This one needs a sendAndWait() else the self.getMachineCoordinates() will not return the correct result

        # get current machine coordinates
        return self.getMachineCoordinates()

    # See: https://github.com/gnea/grbl/wiki/Grbl-v1.1-Commands#:~:text=run%20as%20normal.-,%24H%20%2D%20Run%20homing%20cycle,-This%20command%20is
    def runHomingCycle(self, homingPosition=HomingPositions.bottomLeftZUp):
        self.homingPosition = homingPosition
        self.send('$23=' + homingPosition)
        self.send('$H')  # This does not need a sendAndWait() in order for self.getMachineCoordinates() to succeed, because $H is fully synchronous already.

        # Unlike other commands $H doesn't seem to need a G4 P0.01 to force sync.
        return self.getMachineCoordinates()

    def send(self, gcode):
        gcode += '\n'
        gcode = gcode.encode('utf-8')
        if debug:
            print("sending " + str(gcode))
        super().write(gcode)
        while True:
            line = super().readline()
            if debug:
                print("received " + str(line))
            if line in [b"ok\r\n", b""]:
                break
            if b'error:' in line:
                raise RuntimeError("Error during send(): " + str(line))

    def sendAndWait(self, gcode):
        self.send(gcode)
        # This is to ensure the command finishes executing so that getMachineCoordinates() will return a valid result
        # See https://github.com/gnea/grbl/wiki/Grbl-v1.1-Interface#synchronization:~:text=to%20insert%20a-,G4%20P0.01,-dwell%20command%2C%20where
        #
        return self.send('G4 P0.01')

    def sendFile(self, fileName):
        # Set default coordinate system (54)
        self.send("G54")

        print(self.getMachineCoordinates())

        with open(
                fileName,
                "r") as gcodeFile:
            self.sendLines(gcodeFile.read())

    def sendLines(self, gcodeLines):
        lineNumber = 1
        for gcodeLine in gcodeLines:
            gcode = gcodeLine.strip()
            if gcode == "":
                continue  # skip empty strings
            if gcode.startswith(";"):
                continue  # skip comment string
            print("(" + str(lineNumber) + ") " + gcode)
            self.getStatus(True)
            self.send(gcode)
            lineNumber += 1

    def setOrigin(self, *args, **kwargs):
        if not args:
            self.send("G10 P1 L20 X0 Y0 Z0")  # Allegedly P1 means the G54 coordinate system.
            return

        if args and isinstance(args[0], GrblController.Vector):
            newPosition = args[0]
        else:
            newPosition = GrblController.Vector(*args, **kwargs)

        self.send(f'G10 P1 L2 X{newPosition.x} Y{newPosition.y} Z{newPosition.z}')

    def startSpindleMotor(self, speed=1000):
        self.spindleMotorSpeed = speed
        self.send(f'S{speed}')
        return speed

    def stopSpindleMotor(self, speed=0):
        self.spindleMotorSpeed = speed
        self.send(f'S{speed} M5')
        return speed

    def unlock(self):
        self.send("$X")  # doesn't require a sendAndWait() because it's effective immediately.
        return self.getMachineCoordinates()


if __name__ == "__main__":
    # Vector Tests
    # tuple Tests
    try:
        thing = GrblController.Vector(())
        if debug:
            print(thing)
    except ValueError as valueError:
        assert valueError.args[0] == "Either provide a list/tuple or named arguments including at least one of 'x', 'y', or 'z'"

    thing = GrblController.Vector((1,))
    if debug:
        print(thing)
    assert str(thing) == '(1.0, None, None)'

    thing = GrblController.Vector((1, 2,))
    if debug:
        print(thing)
    assert str(thing) == '(1.0, 2.0, None)'

    thing = GrblController.Vector((1, 2, 3,))
    if debug:
        print(thing)
    assert str(thing) == '(1.0, 2.0, 3.0)'

    try:
        thing = GrblController.Vector((1, 2, 3, 4))
        if debug:
            print(thing)
    except ValueError as valueError:
        assert valueError.args[0] == "Either provide a list/tuple or named arguments including at least one of 'x', 'y', or 'z'"

    try:
        thing = GrblController.Vector((1, 2, 3, 4, 5))
        if debug:
            print(thing)
    except ValueError as valueError:
        assert valueError.args[0] == "Either provide a list/tuple or named arguments including at least one of 'x', 'y', or 'z'"

    try:
        thing = GrblController.Vector((1, 2, 3, 4, 5, 6))
        if debug:
            print(thing)
    except ValueError as valueError:
        assert valueError.args[0] == "Either provide a list/tuple or named arguments including at least one of 'x', 'y', or 'z'"

    try:
        thing = GrblController.Vector((1,), (2,))
        if debug:
            print(thing)
    except TypeError as typeError:
        assert typeError.args[0] == "float() argument must be a string or a real number, not 'tuple'"

    # list Tests
    try:
        thing = GrblController.Vector([])
        if debug:
            print(thing)
    except ValueError as valueError:
        assert valueError.args[0] == "Either provide a list/tuple or named arguments including at least one of 'x', 'y', or 'z'"

    thing = GrblController.Vector([1])
    if debug:
        print(thing)
    assert str(thing) == '(1.0, None, None)'

    thing = GrblController.Vector([1, 2])
    if debug:
        print(thing)
    assert str(thing) == '(1.0, 2.0, None)'

    thing = GrblController.Vector([1, 2, 3])
    if debug:
        print(thing)
    assert str(thing) == '(1.0, 2.0, 3.0)'

    try:
        thing = GrblController.Vector([1, 2, 3, 4])
        if debug:
            print(thing)
    except ValueError as valueError:
        assert valueError.args[0] == "Either provide a list/tuple or named arguments including at least one of 'x', 'y', or 'z'"

    try:
        thing = GrblController.Vector([1, 2, 3, 4, 5])
        if debug:
            print(thing)
    except ValueError as valueError:
        assert valueError.args[0] == "Either provide a list/tuple or named arguments including at least one of 'x', 'y', or 'z'"

    try:
        thing = GrblController.Vector([1, 2, 3, 4, 5, 6])
        if debug:
            print(thing)
    except ValueError as valueError:
        assert valueError.args[0] == "Either provide a list/tuple or named arguments including at least one of 'x', 'y', or 'z'"

    try:
        thing = GrblController.Vector([1], [2])
        if debug:
            print(thing)
    except TypeError as typeError:
        assert typeError.args[0] == "float() argument must be a string or a real number, not 'list'"

    # positional argument tests
    try:
        thing = GrblController.Vector()
        if debug:
            print(thing)
    except ValueError as valueError:
        assert valueError.args[0] == "Either provide a list/tuple or named arguments including at least one of 'x', 'y', or 'z'"

    thing = GrblController.Vector(1)
    if debug:
        print(thing)
    assert str(thing) == '(1.0, None, None)'

    thing = GrblController.Vector(1, 2)
    if debug:
        print(thing)
    assert str(thing) == '(1.0, 2.0, None)'

    thing = GrblController.Vector(1, 2, 3)
    if debug:
        print(thing)
    assert str(thing) == '(1.0, 2.0, 3.0)'

    try:
        thing = GrblController.Vector(1, 2, 3, 4)
        if debug:
            print(thing)
    except ValueError as valueError:
        assert valueError.args[0] == "Either provide a list/tuple or named arguments including at least one of 'x', 'y', or 'z'"

    try:
        thing = GrblController.Vector(1, 2, 3, 4, 5)
        if debug:
            print(thing)
    except ValueError as valueError:
        assert valueError.args[0] == "Either provide a list/tuple or named arguments including at least one of 'x', 'y', or 'z'"

    try:
        thing = GrblController.Vector(1, 2, 3, 4, 5, 6)
        if debug:
            print(thing)
    except ValueError as valueError:
        assert valueError.args[0] == "Either provide a list/tuple or named arguments including at least one of 'x', 'y', or 'z'"

    # keyword argument tests
    try:
        thing = GrblController.Vector(junk=3)
        if debug:
            print(thing)
    except ValueError as valueError:
        assert valueError.args[0] == "Either provide a list/tuple or named arguments including at least one of 'x', 'y', or 'z'"

    thing = GrblController.Vector(x=1)
    if debug:
        print(thing)
    assert str(thing) == '(1.0, None, None)'

    thing = GrblController.Vector(y=2)
    if debug:
        print(thing)
    assert str(thing) == '(None, 2.0, None)'

    thing = GrblController.Vector(z=3)
    if debug:
        print(thing)
    assert str(thing) == '(None, None, 3.0)'

    thing = GrblController.Vector(x=1, y=2)
    if debug:
        print(thing)
    assert str(thing) == '(1.0, 2.0, None)'

    thing = GrblController.Vector(x=1, z=3)
    if debug:
        print(thing)
    assert str(thing) == '(1.0, None, 3.0)'

    thing = GrblController.Vector(y=2, z=3)
    if debug:
        print(thing)
    assert str(thing) == '(None, 2.0, 3.0)'

    thing = GrblController.Vector(x=1, y=2, z=3)
    if debug:
        print(thing)
    assert str(thing) == '(1.0, 2.0, 3.0)'

    try:
        thing = GrblController.Vector(x=1, junk=3)
        if debug:
            print(thing)
    except ValueError as valueError:
        assert valueError.args[0] == "Either provide a list/tuple or named arguments including at least one of 'x', 'y', or 'z'"

    # Point tests
    # GrblController.Point class is effectively an alias for Grbl.Vector
    # All the tests below are derived from the Vector tests above, substituting the word Point for Vector
    # tuple Tests
    try:
        thing = GrblController.Point(())
        if debug:
            print(thing)
    except ValueError as valueError:
        assert valueError.args[0] == "Either provide a list/tuple or named arguments including at least one of 'x', 'y', or 'z'"

    thing = GrblController.Point((1,))
    if debug:
        print(thing)
    assert str(thing) == '(1.0, None, None)'

    thing = GrblController.Point((1, 2,))
    if debug:
        print(thing)
    assert str(thing) == '(1.0, 2.0, None)'

    thing = GrblController.Point((1, 2, 3,))
    if debug:
        print(thing)
    assert str(thing) == '(1.0, 2.0, 3.0)'

    try:
        thing = GrblController.Point((1, 2, 3, 4))
        if debug:
            print(thing)
    except ValueError as valueError:
        assert valueError.args[0] == "Either provide a list/tuple or named arguments including at least one of 'x', 'y', or 'z'"

    try:
        thing = GrblController.Point((1, 2, 3, 4, 5))
        if debug:
            print(thing)
    except ValueError as valueError:
        assert valueError.args[0] == "Either provide a list/tuple or named arguments including at least one of 'x', 'y', or 'z'"

    try:
        thing = GrblController.Point((1, 2, 3, 4, 5, 6))
        if debug:
            print(thing)
    except ValueError as valueError:
        assert valueError.args[0] == "Either provide a list/tuple or named arguments including at least one of 'x', 'y', or 'z'"

    try:
        thing = GrblController.Point((1,), (2,))
        if debug:
            print(thing)
    except TypeError as typeError:
        assert typeError.args[0] == "float() argument must be a string or a real number, not 'tuple'"

    # list Tests
    try:
        thing = GrblController.Point([])
        if debug:
            print(thing)
    except ValueError as valueError:
        assert valueError.args[0] == "Either provide a list/tuple or named arguments including at least one of 'x', 'y', or 'z'"

    thing = GrblController.Point([1])
    if debug:
        print(thing)
    assert str(thing) == '(1.0, None, None)'

    thing = GrblController.Point([1, 2])
    if debug:
        print(thing)
    assert str(thing) == '(1.0, 2.0, None)'

    thing = GrblController.Point([1, 2, 3])
    if debug:
        print(thing)
    assert str(thing) == '(1.0, 2.0, 3.0)'

    try:
        thing = GrblController.Point([1, 2, 3, 4])
        if debug:
            print(thing)
    except ValueError as valueError:
        assert valueError.args[0] == "Either provide a list/tuple or named arguments including at least one of 'x', 'y', or 'z'"

    try:
        thing = GrblController.Point([1, 2, 3, 4, 5])
        if debug:
            print(thing)
    except ValueError as valueError:
        assert valueError.args[0] == "Either provide a list/tuple or named arguments including at least one of 'x', 'y', or 'z'"

    try:
        thing = GrblController.Point([1, 2, 3, 4, 5, 6])
        if debug:
            print(thing)
    except ValueError as valueError:
        assert valueError.args[0] == "Either provide a list/tuple or named arguments including at least one of 'x', 'y', or 'z'"

    try:
        thing = GrblController.Point([1], [2])
        if debug:
            print(thing)
    except TypeError as typeError:
        assert typeError.args[0] == "float() argument must be a string or a real number, not 'list'"

    # positional argument tests
    try:
        thing = GrblController.Point()
        if debug:
            print(thing)
    except ValueError as valueError:
        assert valueError.args[0] == "Either provide a list/tuple or named arguments including at least one of 'x', 'y', or 'z'"

    thing = GrblController.Point(1)
    if debug:
        print(thing)
    assert str(thing) == '(1.0, None, None)'

    thing = GrblController.Point(1, 2)
    if debug:
        print(thing)
    assert str(thing) == '(1.0, 2.0, None)'

    thing = GrblController.Point(1, 2, 3)
    if debug:
        print(thing)
    assert str(thing) == '(1.0, 2.0, 3.0)'

    try:
        thing = GrblController.Point(1, 2, 3, 4)
        if debug:
            print(thing)
    except ValueError as valueError:
        assert valueError.args[0] == "Either provide a list/tuple or named arguments including at least one of 'x', 'y', or 'z'"

    try:
        thing = GrblController.Point(1, 2, 3, 4, 5)
        if debug:
            print(thing)
    except ValueError as valueError:
        assert valueError.args[0] == "Either provide a list/tuple or named arguments including at least one of 'x', 'y', or 'z'"

    try:
        thing = GrblController.Point(1, 2, 3, 4, 5, 6)
        if debug:
            print(thing)
    except ValueError as valueError:
        assert valueError.args[0] == "Either provide a list/tuple or named arguments including at least one of 'x', 'y', or 'z'"

    # keyword argument tests
    try:
        thing = GrblController.Point(junk=3)
        if debug:
            print(thing)
    except ValueError as valueError:
        assert valueError.args[0] == "Either provide a list/tuple or named arguments including at least one of 'x', 'y', or 'z'"

    thing = GrblController.Point(x=1)
    if debug:
        print(thing)
    assert str(thing) == '(1.0, None, None)'

    thing = GrblController.Point(y=2)
    if debug:
        print(thing)
    assert str(thing) == '(None, 2.0, None)'

    thing = GrblController.Point(z=3)
    if debug:
        print(thing)
    assert str(thing) == '(None, None, 3.0)'

    thing = GrblController.Point(x=1, y=2)
    if debug:
        print(thing)
    assert str(thing) == '(1.0, 2.0, None)'

    thing = GrblController.Point(x=1, z=3)
    if debug:
        print(thing)
    assert str(thing) == '(1.0, None, 3.0)'

    thing = GrblController.Point(y=2, z=3)
    if debug:
        print(thing)
    assert str(thing) == '(None, 2.0, 3.0)'

    thing = GrblController.Point(x=1, y=2, z=3)
    if debug:
        print(thing)
    assert str(thing) == '(1.0, 2.0, 3.0)'

    try:
        thing = GrblController.Point(x=1, junk=3)
        if debug:
            print(thing)
    except ValueError as valueError:
        assert valueError.args[0] == "Either provide a list/tuple or named arguments including at least one of 'x', 'y', or 'z'"
