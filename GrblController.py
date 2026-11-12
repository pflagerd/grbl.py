import re
import serial
import time
import warnings

debug = False

# What Grbl 1.1h can send at startup is documented in https://github.com/pflagerd/grbl.py/issues/13,
# with links to the firmware source (https://github.com/gnea/grbl, version 1.1h build 20190830).

# Firmware versions the startup handshake accepts: Grbl 1.1 with any letter (1.1f, 1.1h, ...).
_supportedVersionPattern = re.compile(r"^1\.1[a-z]$")
# report.c report_init_message(): "\r\nGrbl 1.1h ['$' for help]\r\n"
_welcomePattern = re.compile(r"Grbl (\S+) \['\$' for help\]")
# report.c report_realtime_status(): "<Idle|MPos:0.000,0.000,0.000|Bf:15,127|FS:0,0>", "<Hold:0|...>", "<Door:1|...>"
_statusPattern = re.compile(r"<([A-Za-z]+)(?::(\d+))?([^>]*)>")
# report.c report_build_info(): "[VER:1.1h.20190830:]"
_buildInfoPattern = re.compile(r"\[VER:(\d+\.\d+[a-z]?)\.(\d+):")
# report.c report_execute_startup_message(): ">G54:ok" or ">G99:error:20"
_startupLinePattern = re.compile(r"^>(.*):(ok|error:\d+)$")

# How long one read may block while the handshake is running.
_handshakeReadTimeoutSeconds = 0.5


class GrblStartupError(RuntimeError):
    """The startup handshake with the controller failed. The message says what was received."""


class GrblVersionError(GrblStartupError):
    """The controller reported a firmware version other than Grbl 1.1x, or none could be determined."""


class GrblStateError(GrblStartupError):
    """The controller is in a state the constructor cannot safely continue from (Door, Sleep, Run, ...)."""


class GrblLimitSwitchError(GrblStartupError):
    """The controller reported [MSG:Check Limits]: a limit switch is engaged at connect."""


def classifyLine(rawLine):
    """Classify one line received from Grbl by how it starts, not by where it comes in the sequence.

    rawLine may be bytes or str. Returns (kind, text) where text is the decoded, stripped line and kind is one of:
    'blank', 'welcome', 'message', 'startupLine', 'alarm', 'status', 'buildInfo', 'ok', 'error', 'unknown'.
    """
    if isinstance(rawLine, (bytes, bytearray)):
        text = bytes(rawLine).decode('utf-8', errors='replace')  # stray bytes must not raise
    else:
        text = rawLine
    text = text.strip()

    if text == "":
        return 'blank', text
    if _welcomePattern.search(text):  # search(), so stray bytes in front of the welcome message are tolerated
        return 'welcome', text
    if text.startswith("[MSG:"):
        return 'message', text
    if text.startswith("[VER:"):
        return 'buildInfo', text
    if text.startswith(">"):
        return 'startupLine', text
    if text.startswith("ALARM:"):
        return 'alarm', text
    if text.startswith("<"):
        return 'status', text
    if text == "ok":
        return 'ok', text
    if text.startswith("error:"):
        return 'error', text
    return 'unknown', text


def parseStatusReport(text):
    """Parse a Grbl 1.1 status report such as '<Idle|MPos:0.000,0.000,0.000|Bf:15,127|FS:0,0>'.

    Returns {'state': 'Idle', 'subState': None, 'fields': {'MPos': '0.000,0.000,0.000', ...}},
    or None if text is not a status report. For '<Hold:0|...>' state is 'Hold' and subState is '0'.
    """
    match = _statusPattern.search(text)
    if not match:
        return None
    fields = {}
    for field in match.group(3).split('|'):
        if ':' in field:
            name, value = field.split(':', 1)
            fields[name] = value
    return {'state': match.group(1), 'subState': match.group(2), 'fields': fields}


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

    def __init__(self, portDeviceName="/dev/ttyUSB0", portBaudRate=115200, handshakeDeadlineSeconds=10.0, resetOnConnect=False):
        self.spindleMotorSpeed = 0
        self.homingPosition = self.HomingPositions.bottomLeftZUp
        self.machineCoordinates = None

        self.portDeviceName = portDeviceName
        self.portBaudRate = portBaudRate
        super().__init__(self.portDeviceName, self.portBaudRate, timeout=30)

        # Read whatever Grbl sends at startup, ask it for its state, and unlock it if it is in Alarm.
        # Raises GrblStartupError (or a subclass) saying what was received if that cannot be done.
        try:
            self._handshake(handshakeDeadlineSeconds, resetOnConnect)
        except GrblStartupError:
            self.close()
            raise

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

    def _now(self):
        # A method so that tests can substitute a fake clock.
        return time.monotonic()

    def _startupFailureText(self, what):
        if self.startupTranscript:
            received = "Received: " + "; ".join(repr(line) for line in self.startupTranscript)
        else:
            received = "Received nothing at all."
        return f"Grbl startup on {self.portDeviceName} failed: {what} {received}"

    def _readStartupLine(self):
        # Reads one line during the handshake, records it, and returns (kind, text).
        # kind is 'timeout' if nothing complete arrived within the read timeout.
        raw = self.readline()
        if debug:
            print("received " + str(raw))
        if raw == b"":
            return 'timeout', ""
        if not raw.endswith(b"\n"):
            # The read timed out part-way through a line. Keep the piece and wait for the rest.
            self._startupPartialLine += raw
            return 'timeout', ""
        raw = self._startupPartialLine + raw
        self._startupPartialLine = b""

        kind, text = classifyLine(raw)
        if kind == 'blank':
            return kind, text
        self.startupTranscript.append(text)
        if kind == 'message':
            self.startupMessages.append(text[len("[MSG:"):].rstrip("]"))
        elif kind == 'startupLine':
            match = _startupLinePattern.match(text)
            self.startupLineResults.append((match.group(1), match.group(2)) if match else (text[1:], ""))
        elif kind == 'alarm':
            self.startupAlarms.append(text[len("ALARM:"):])
        return kind, text

    def _queryBuildVersion(self, deadline):
        # Asks for the firmware version with $I. Used when no welcome message was seen, because the
        # controller was not reset by opening the port. $I is only accepted in the Idle and Alarm states.
        if debug:
            print("sending " + str(b"$I\n"))
        self.write(b"$I\n")
        version = None
        while self._now() < deadline:
            kind, text = self._readStartupLine()
            if kind == 'buildInfo':
                match = _buildInfoPattern.search(text)
                if match:
                    version = match.group(1)
            elif kind in ('ok', 'error'):
                break
        return version

    def _handshake(self, deadlineSeconds=10.0, resetOnConnect=False):
        """Read Grbl's startup output by pattern, ask for its state with '?', and act on that state.

        Uses only self.readline(), self.write() and self.send(), so it can be tested with scripted lines.
        Afterwards the results are in self.grblVersion, self.startupState, self.startupMessages,
        self.startupLineResults, self.startupAlarms and self.startupTranscript.

        The first half of deadlineSeconds is spent waiting for the welcome message. If none arrives,
        the controller was presumably not reset by opening the port, and '?' is sent anyway.

        pyserial discards any input already waiting when it opens the port (Serial.open() calls
        _reset_input_buffer()), so a welcome message is only ever seen if the controller sends it
        after the port is opened, that is, if opening the port resets the controller.
        """
        self.startupTranscript = []    # every non-blank line received, decoded
        self.startupMessages = []      # the text inside each [MSG:...]
        self.startupLineResults = []   # (line, 'ok' or 'error:N') for each stored $N startup line that ran
        self.startupAlarms = []        # the code from each ALARM:N
        self.grblVersion = None        # for example '1.1h'
        self.startupState = None       # for example 'Alarm' or 'Idle'
        self._startupPartialLine = b""

        start = self._now()
        deadline = start + deadlineSeconds
        welcomeDeadline = start + deadlineSeconds / 2.0
        status = None
        statusRequested = False

        savedTimeout = self.timeout
        self.timeout = _handshakeReadTimeoutSeconds
        try:
            if resetOnConnect:
                # Soft reset (ctrl-x). Off by default: during motion it stops the steppers and position is lost.
                if debug:
                    print("sending " + str(b"\x18"))
                self.write(b"\x18")

            while status is None and self._now() < deadline:
                if not statusRequested and self._now() >= welcomeDeadline:
                    # No welcome message. Ask for the state anyway, in case the controller is running but was not reset.
                    if debug:
                        print("sending " + str(b"?"))
                    self.write(b"?")
                    statusRequested = True

                kind, text = self._readStartupLine()
                if kind == 'welcome':
                    self.grblVersion = _welcomePattern.search(text).group(1)
                    if not _supportedVersionPattern.match(self.grblVersion):
                        raise GrblVersionError(self._startupFailureText(
                            f"expected Grbl 1.1x firmware but the controller reported version {self.grblVersion!r}."))
                    # Grbl sends nothing to mark the end of its startup output, so ask for a status report:
                    # it arrives after the startup messages. '?' is a real-time byte: no newline, and no 'ok' comes back.
                    # Sent (again) here because a '?' sent before the reset finished would have been lost.
                    if debug:
                        print("sending " + str(b"?"))
                    self.write(b"?")
                    statusRequested = True
                elif kind == 'status' and statusRequested:
                    status = parseStatusReport(text)

            if status is not None and self.grblVersion is None and status['state'] in ('Idle', 'Alarm'):
                self.grblVersion = self._queryBuildVersion(deadline)
        finally:
            self.timeout = savedTimeout

        if status is None:
            raise GrblStartupError(self._startupFailureText(
                f"no status report arrived within {deadlineSeconds} seconds."))

        self.startupState = status['state']
        if self.startupState not in ('Idle', 'Alarm'):
            raise GrblStateError(self._startupFailureText(
                f"the controller is in the {self.startupState} state; it must be Idle or Alarm to continue."))

        if self.grblVersion is None:
            raise GrblVersionError(self._startupFailureText(
                "could not determine the firmware version (no welcome message, and no [VER:...] reply to $I)."))
        if not _supportedVersionPattern.match(self.grblVersion):
            raise GrblVersionError(self._startupFailureText(
                f"expected Grbl 1.1x firmware but the controller reported version {self.grblVersion!r}."))

        for startupLine, result in self.startupLineResults:
            if result != "ok":
                # Not raised: the machine is usable, and raising would lock out the tool needed to fix $N.
                warnings.warn(f"Grbl stored startup line {startupLine!r} failed with {result}. Check it with $N.")

        if self.startupState == 'Alarm':
            if "Check Limits" in self.startupMessages:
                # Deliberately not unlocked with $X: the machine is sitting on a limit switch.
                raise GrblLimitSwitchError(self._startupFailureText(
                    "a limit switch is engaged ([MSG:Check Limits]). "
                    "Move the machine off the switch, or check the switch wiring, then connect again."))
            # Unlock
            self.send("$X")

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
        self.write(gcode)  # self, not super(): same behaviour, but lets tests substitute a scripted controller
        while True:
            line = self.readline()
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
        for gcodeLine in gcodeLines.split('\n'):
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
