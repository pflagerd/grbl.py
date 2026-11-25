import json
import serial
import time
import warnings
from dataclasses import dataclass, field, fields, asdict

debug = False


@dataclass
class MachineProfile:
    # Identity / connection
    name: str                              # used in logs and warnings
    portDeviceName: str = "/dev/ttyUSB0"
    portBaudRate: int = 115200

    # Geometry — the usable work envelope, in mm, measured from the
    # bottom-left corner (where G54's origin gets set after homing).
    workAreaX: float = 0.0
    workAreaY: float = 0.0
    workAreaZ: float = 0.0                 # usable depth below Z origin

    # Where the machine physically homes to.
    homingPosition: str = "3"              # HomingPositions.bottomLeftZUp

    # Motion defaults
    defaultFeedRate: float = 400.0         # mm/min for cutting moves
    rapidZClearance: float = 5.0           # safe height for travel moves

    # Tool type — laser mode changes spindle handling (M4 dynamic power, no spin-up)
    isLaser: bool = False
    defaultSpindleSpeed: float = 1000.0    # RPM, or S-value power for laser

    # Grbl $-settings this machine must have, e.g. {"$23": "3", "$130": "300.000"}.
    # Values are strings compared against the exact text Grbl echoes from `$$`.
    expectedSettings: dict = field(default_factory=dict)

    # Calibration provenance — filled in by calibrate.py
    schemaVersion: int = 1
    firmwareVersion: str = ""              # from the startup banner, e.g. "1.1h"
    calibratedOn: str = ""                 # ISO date of the capability test
    pullOff: float = 1.0                   # $27 at calibration time; baked into travel numbers
    settingsSnapshot: dict = field(default_factory=dict)   # full $$ dump at calibration
    homeableCorners: dict = field(default_factory=dict)    # {"0": True, "3": True, ...} per $23 value tried
    switchesVerified: dict = field(default_factory=dict)   # {"X": True, ...} both ends press-tested

    def save(self, path):
        with open(path, 'w') as jsonFile:
            json.dump(asdict(self), jsonFile, indent=2)
            jsonFile.write('\n')

    @classmethod
    def load(cls, path):
        with open(path) as jsonFile:
            data = json.load(jsonFile)
        knownFields = {dataclassField.name for dataclassField in fields(cls)}
        unknown = set(data) - knownFields
        if unknown:
            # A newer schema may add fields; ignore them so old code can still load.
            warnings.warn(f"Profile {path} has unknown fields (newer schema?): {sorted(unknown)}")
        return cls(**{key: value for key, value in data.items() if key in knownFields})


class GrblController(serial.Serial):
    HANDSHAKE_TIMEOUT = 10  # seconds to wait for the startup banner after opening the port
    EXPECTED_GRBL_VERSION = "1.1h"

    # Profile settings where a mismatch means motion in the wrong direction or without
    # bounds checking — a tool crash waiting to happen.  These raise instead of warning.
    CRITICAL_SETTINGS = ("$3", "$23", "$20", "$130", "$131", "$132")

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

    class HomingPositions:
        topRightZUp = '0'
        topLeftZUp = '1'
        bottomRightZUp = '2'
        bottomLeftZUp = '3'
        topRightZDown = '4'

    def __init__(self, portDeviceName=None, portBaudRate=None, homeOnConnect=False,
                 profile=None, applySettings=False):
        self.profile = profile
        self.grblVersion = None  # set from the startup banner during the handshake
        self.spindleMotorSpeed = 0
        self.homingPosition = profile.homingPosition if profile else self.HomingPositions.bottomLeftZUp
        self.machineCoordinates = None

        # Explicit arguments win over the profile; the profile wins over the defaults.
        self.portDeviceName = portDeviceName or (profile.portDeviceName if profile else "/dev/ttyUSB0")
        self.portBaudRate = portBaudRate or (profile.portBaudRate if profile else 115200)
        super().__init__(self.portDeviceName, self.portBaudRate, timeout=30)

        state = self._performHandshake()
        if state == "Alarm":
            self.send("$X")  # unlock — we saw the alarm, we're clearing it
        elif state != "Idle":
            # Run/Jog/Hold/Door: the machine is moving or suspended mid-job — possibly
            # another sender's.  Sleep/Check need a reset first.  Don't barge in.
            raise RuntimeError(f"Grbl on {self.portDeviceName} is in state '{state}', not "
                               f"Idle or Alarm — refusing to configure a machine that may "
                               f"be mid-job. Resolve that first (reset/resume), then reconnect.")

        # Verify (or apply) the machine's $-settings before anything moves — homing with
        # a wrong $23 drives the axes away from the switches and into the hard stops.
        if profile:
            self._verifyProfileSettings(applySettings)

        # DPP: Set spindle motor speed
        self.send(f'S{profile.defaultSpindleSpeed if profile else 10000}')

        # DPP: Set homing origin
        self.send('$23=' + self.homingPosition)

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

        if homeOnConnect:
            self.runHomingCycle()

    def __del__(self):
        # getattr guards against instances that never finished __init__ (e.g. the port
        # failed to open), where is_open doesn't exist yet.
        if getattr(self, 'is_open', False):
            super().close()

    def _performHandshake(self):
        # UGS-style handshake: poll with `?` about once a second until a <...> status
        # report arrives, and return the machine state parsed from it.  `?` is a
        # realtime command Grbl answers in any state, so the first valid report is the
        # liveness proof — it works whether the board rebooted on connect (DTR pulse,
        # banner arrives mid-poll and we capture the version from it) or was already
        # running (no banner ever comes, which is normal, not an error).
        originalTimeout = self.timeout
        self.timeout = 1  # one poll per second; also the per-line read wait
        try:
            statusReport = None
            deadline = time.monotonic() + self.HANDSHAKE_TIMEOUT
            while statusReport is None and time.monotonic() < deadline:
                super().write(b"?")
                while True:  # read whatever this poll interval brings: boot chatter, MSGs, the report
                    line = super().readline()
                    if debug:
                        print("received " + str(line))
                    if line == b"":  # quiet; poll again
                        break
                    if line.startswith(b"Grbl "):
                        # b"Grbl 1.1h ['$' for help]\r\n" — fresh boot; capture the version
                        self.grblVersion = line.decode("utf-8", errors="replace").split()[1]
                        if self.grblVersion != self.EXPECTED_GRBL_VERSION:
                            warnings.warn("Expected Grbl version " + self.EXPECTED_GRBL_VERSION
                                          + " but connected to " + self.grblVersion)
                    if line.startswith(b"<"):
                        statusReport = line.decode("utf-8", errors="replace")
                        break

            if statusReport is None:
                raise RuntimeError("No status report from Grbl on " + self.portDeviceName
                                   + " within " + str(self.HANDSHAKE_TIMEOUT)
                                   + "s of ? polling; not a live Grbl, or wrong port/baud rate.")

            # Drain any remaining startup lines (e.g. the '$H'|'$X' unlock MSG) so the
            # next command doesn't read stale chatter.
            self.timeout = 0.5
            while True:
                line = super().readline()
                if line == b"":
                    break
                if debug:
                    print("received " + str(line))

            # '<Alarm|MPos:...>' → 'Alarm'; '<Hold:0|...>' → 'Hold'
            return statusReport[1:].split('|')[0].split(':')[0]
        finally:
            self.timeout = originalTimeout

    def getSettings(self):
        # Query all $-settings; Grbl answers with one "$n=value" line each, then "ok".
        gcode = "$$\n".encode('utf-8')
        if debug:
            print("sending " + str(gcode))
        super().write(gcode)
        settings = {}
        while True:
            line = super().readline()
            if debug:
                print("received " + str(line))
            if line in [b"ok\r\n", b""]:
                break
            if b'error:' in line:
                raise RuntimeError("Error during getSettings(): " + str(line))
            text = line.decode('utf-8', errors='replace').strip()
            if text.startswith('$') and '=' in text:
                key, _, value = text.partition('=')
                settings[key] = value
        return settings

    def _verifyProfileSettings(self, applySettings):
        def findMismatches():
            actual = self.getSettings()
            return {key: (expected, actual.get(key))
                    for key, expected in self.profile.expectedSettings.items()
                    if actual.get(key) != expected}

        mismatches = findMismatches()
        if mismatches and applySettings:
            for key, (expected, _) in mismatches.items():
                self.send(f'{key}={expected}')
            mismatches = findMismatches()  # re-read: anything still wrong didn't take

        if not mismatches:
            return

        report = "; ".join(f"{key} is {actual} (want {expected})"
                           for key, (expected, actual) in mismatches.items())
        critical = [key for key in mismatches if key in self.CRITICAL_SETTINGS]
        if critical:
            raise RuntimeError(
                f"Machine '{self.profile.name}' has unsafe $-settings ({', '.join(critical)} "
                f"control motion direction/bounds): {report}. "
                + ("Applying them failed." if applySettings
                   else "Fix them or connect with applySettings=True."))
        warnings.warn(f"Machine '{self.profile.name}' settings differ from profile: {report}")

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
        # The machine state from a fresh status report: 'Idle', 'Run', 'Hold', 'Jog',
        # 'Alarm', 'Door', 'Check', 'Home', 'Sleep' — or None if no report came back.
        # Substate suffixes are stripped: '<Hold:0|...>' → 'Hold'.
        statusReport = self.getStatus()
        if statusReport and statusReport.startswith('<'):
            return statusReport[1:].split('|')[0].split(':')[0]
        return None

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

        feedRate = self.profile.defaultFeedRate if self.profile else 400
        if 'feedRate' in kwargs:
            feedRate = float(kwargs['feedRate'])

        spindleSpeed = self.profile.defaultSpindleSpeed if self.profile else 1000
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
    def softReset(self):
        # Ctrl-X: immediate reset.  Grbl reboots and re-prints its banner; drain it all
        # so the next command doesn't read stale lines.
        super().write(b'\x18')
        originalTimeout = self.timeout
        self.timeout = 2
        try:
            while True:
                line = super().readline()
                if line == b"":
                    break
                if debug:
                    print("received " + str(line))
        finally:
            self.timeout = originalTimeout

    def tryHomingCycle(self, homingPosition):
        # Like runHomingCycle(), but a homing failure (switch not found / lost) returns
        # the alarm code as a string instead of hanging or raising — used by the
        # calibration capability test where "this corner doesn't home" is a result.
        # Returns None on success.
        self.send('$23=' + homingPosition)
        originalTimeout = self.timeout
        self.timeout = 120  # homing seeks the full axis travel at seek rate; allow time
        try:
            super().write(b"$H\n")
            while True:
                line = super().readline()
                if debug:
                    print("received " + str(line))
                if line == b"ok\r\n":
                    self.homingPosition = homingPosition
                    return None
                if line.startswith(b"ALARM:"):
                    alarmCode = line.decode('utf-8', errors='replace').strip().partition(':')[2]
                    self.softReset()
                    self.send("$X")  # reset after a homing alarm leaves Grbl locked
                    return alarmCode
                if line == b"":
                    return "timeout"
        finally:
            self.timeout = originalTimeout

    def getPinState(self):
        # Returns the triggered-input letters from the status report's Pn: field,
        # e.g. "XY" if both those limit inputs are active, "" if nothing is triggered.
        # Note: stock Grbl shares one pin per axis, so "X" means *a* switch on X —
        # it cannot distinguish the min switch from the max switch.
        statusReport = self.getStatus()
        if statusReport and 'Pn:' in statusReport:
            return statusReport.split('Pn:')[1].split('|')[0].rstrip('>\r\n')
        return ""

    def runHomingCycle(self, homingPosition=None):
        if homingPosition is None:
            homingPosition = self.homingPosition  # from the profile, or bottomLeftZUp
        # $H answers "ok" only when homing has finished, which on a big machine can
        # take more than the port's 30 s read timeout ($25=500 mm/min over 622 mm is
        # 75 s).  send() would give up early and leave that "ok" unread, and the next
        # status query would read it as its own reply (state None).  tryHomingCycle()
        # waits up to 120 s and reports an alarm or timeout instead.
        failure = self.tryHomingCycle(homingPosition)
        if failure is not None:
            reason = "no reply within 120 s" if failure == "timeout" else f"ALARM:{failure}"
            raise RuntimeError(f"Homing failed ({reason}).")

        # $H is fully synchronous, so no G4 P0.01 is needed before reading the position.
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

    # MachineProfile verify/apply tests — exercise _verifyProfileSettings without a
    # serial port by stubbing out getSettings()/send() on an uninitialized instance.
    class FakeGrbl(GrblController):
        def __init__(self, machineSettings, profile):  # deliberately no super().__init__()
            self.machineSettings = dict(machineSettings)
            self.profile = profile
            self.sent = []

        def getSettings(self):
            return dict(self.machineSettings)

        def send(self, gcode):
            self.sent.append(gcode)
            key, _, value = gcode.partition('=')
            self.machineSettings[key] = value

    profile = MachineProfile(name="test", expectedSettings={"$3": "0", "$23": "3", "$110": "500.000"})

    # All settings already correct: no warning, no error, nothing sent
    fake = FakeGrbl({"$3": "0", "$23": "3", "$110": "500.000"}, profile)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        fake._verifyProfileSettings(applySettings=False)
    assert fake.sent == []

    # Non-critical mismatch: warns, does not raise
    fake = FakeGrbl({"$3": "0", "$23": "3", "$110": "999.000"}, profile)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        fake._verifyProfileSettings(applySettings=False)
    assert len(caught) == 1 and "$110" in str(caught[0].message)

    # Critical mismatch ($23 wrong): hard error, nothing sent
    fake = FakeGrbl({"$3": "0", "$23": "0", "$110": "500.000"}, profile)
    try:
        fake._verifyProfileSettings(applySettings=False)
        assert False, "expected RuntimeError for critical mismatch"
    except RuntimeError as runtimeError:
        assert "$23" in str(runtimeError) and "applySettings=True" in str(runtimeError)
    assert fake.sent == []

    # Critical mismatch with applySettings=True: corrected and no error
    fake = FakeGrbl({"$3": "0", "$23": "0", "$110": "500.000"}, profile)
    fake._verifyProfileSettings(applySettings=True)
    assert fake.sent == ["$23=3"]
    assert fake.machineSettings["$23"] == "3"

    # Setting missing entirely from the machine (reports as None): critical path
    fake = FakeGrbl({"$23": "3", "$110": "500.000"}, profile)
    try:
        fake._verifyProfileSettings(applySettings=False)
        assert False, "expected RuntimeError for missing critical setting"
    except RuntimeError as runtimeError:
        assert "$3" in str(runtimeError)

    # applySettings that doesn't take (send succeeds but value stays wrong): still raises
    class StubbornFakeGrbl(FakeGrbl):
        def send(self, gcode):
            self.sent.append(gcode)  # pretend to accept, change nothing

    fake = StubbornFakeGrbl({"$3": "0", "$23": "0", "$110": "500.000"}, profile)
    try:
        fake._verifyProfileSettings(applySettings=True)
        assert False, "expected RuntimeError when applying fails"
    except RuntimeError as runtimeError:
        assert "Applying them failed" in str(runtimeError)

    # MachineProfile JSON round-trip tests
    import tempfile
    import os

    original = MachineProfile(
        name="roundtrip", workAreaX=300.0, workAreaY=180.0, workAreaZ=45.0,
        expectedSettings={"$23": "3"}, firmwareVersion="1.1h", calibratedOn="2026-11-18",
        settingsSnapshot={"$0": "10", "$23": "3"},
        homeableCorners={"0": True, "3": True}, switchesVerified={"X": True, "Y": True})
    with tempfile.TemporaryDirectory() as tempDir:
        path = os.path.join(tempDir, "roundtrip.json")
        original.save(path)
        loaded = MachineProfile.load(path)
        assert loaded == original

        # A file from a newer schema (extra field) loads with a warning, not an error
        with open(path) as jsonFile:
            data = json.load(jsonFile)
        data["futureField"] = "whatever"
        with open(path, 'w') as jsonFile:
            json.dump(data, jsonFile)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            loaded = MachineProfile.load(path)
        assert loaded == original
        assert len(caught) == 1 and "futureField" in str(caught[0].message)

    # getPinState parsing (stub getStatus; no serial port involved)
    class PinFakeGrbl(FakeGrbl):
        statusReport = None

        def getStatus(self, debug=False):
            return self.statusReport

    fake = PinFakeGrbl({}, profile)
    fake.statusReport = '<Idle|MPos:0.000,0.000,0.000|Pn:XY|FS:0,0>\r\n'
    assert fake.getPinState() == "XY"
    fake.statusReport = '<Idle|MPos:0.000,0.000,0.000|Pn:Z>\r\n'
    assert fake.getPinState() == "Z"
    fake.statusReport = '<Idle|MPos:0.000,0.000,0.000|FS:0,0>\r\n'
    assert fake.getPinState() == ""
    fake.statusReport = None
    assert fake.getPinState() == ""

    # getGrblState parsing
    fake.statusReport = '<Idle|MPos:0.000,0.000,0.000|FS:0,0>\r\n'
    assert fake.getGrblState() == "Idle"
    fake.statusReport = '<Alarm|MPos:0.000,0.000,0.000|Bf:15,127|FS:0,0|WCO:-405.000,-299.000,-84.000>\r\n'
    assert fake.getGrblState() == "Alarm"
    fake.statusReport = '<Hold:0|MPos:-10.000,-5.000,0.000|FS:0,0>\r\n'
    assert fake.getGrblState() == "Hold"
    fake.statusReport = None
    assert fake.getGrblState() is None

