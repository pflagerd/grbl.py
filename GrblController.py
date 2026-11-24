import json
import re
import serial
import time
import warnings
from dataclasses import dataclass, field, fields, asdict

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
# How long the handshake waits for an answer to its '?' liveness probe before sending another.
_probeRetrySeconds = 1.0


class GrblStartupError(RuntimeError):
    """The startup handshake with the controller failed. The message says what was received."""


class GrblVersionError(GrblStartupError):
    """The controller reported a firmware version other than Grbl 1.1x, or none could be determined."""


class GrblStateError(GrblStartupError):
    """The controller is in a state the constructor cannot safely continue from (Door, Sleep, Run, ...)."""


class GrblLimitSwitchError(GrblStartupError):
    """The controller reported [MSG:Check Limits]: a limit switch is engaged at connect."""


class GrblSettingsError(GrblStartupError):
    """A motion-critical $-setting ($3, $23, $20, $130-$132) differs from the machine profile."""


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


def limitPins(pinState):
    """The limit-switch letters in a status report's Pn: value, e.g. 'XZ' from 'XZP'. '' if none.

    Pn: lists every input that is active: X, Y, Z for the limit pins, and P, D, H, R, S for the
    probe, door, hold, reset and cycle-start pins. Only X, Y and Z matter for limit switches.
    """
    return "".join(letter for letter in "XYZ" if letter in (pinState or ""))


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
    HANDSHAKE_TIMEOUT = 10.0  # seconds; the default for handshakeDeadlineSeconds

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

    def __init__(self, portDeviceName=None, portBaudRate=None, handshakeDeadlineSeconds=None, resetOnConnect=False,
                 homeOnConnect=False, profile=None, applySettings=False):
        self.profile = profile
        self.grblVersion = None  # set during the handshake, for example '1.1h'
        self.spindleMotorSpeed = 0
        self.homingPosition = profile.homingPosition if profile else self.HomingPositions.bottomLeftZUp
        self.machineCoordinates = None

        # Explicit arguments win over the profile; the profile wins over the defaults.
        self.portDeviceName = portDeviceName or (profile.portDeviceName if profile else "/dev/ttyUSB0")
        self.portBaudRate = portBaudRate or (profile.portBaudRate if profile else 115200)
        super().__init__(self.portDeviceName, self.portBaudRate, timeout=30)

        try:
            # Read whatever Grbl sends at startup, ask it for its state, and unlock it if it is in Alarm.
            # Raises GrblStartupError (or a subclass) saying what was received if that cannot be done.
            self._handshake(handshakeDeadlineSeconds, resetOnConnect)

            # Verify (or apply) the machine's $-settings before anything moves — homing with
            # a wrong $23 drives the axes away from the switches and into the hard stops.
            if profile:
                self._verifyProfileSettings(applySettings)
        except GrblStartupError:  # includes GrblSettingsError
            self.close()
            raise

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

    def _handshake(self, deadlineSeconds=None, resetOnConnect=False):
        """Read Grbl's startup output by pattern, ask for its state with '?', and act on that state.

        Uses only self.readline(), self.write() and self.send(), so it can be tested with scripted lines.
        Afterwards the results are in self.grblVersion, self.startupState, self.startupMessages,
        self.startupLineResults, self.startupAlarms and self.startupTranscript.

        '?' is the liveness probe. It is sent at once, and again every _probeRetrySeconds until a
        status report arrives or deadlineSeconds runs out. Grbl answers '?' in every state, so the
        first status report proves the controller is alive whether or not it was reset. A '?' sent
        while the controller is still resetting is lost, which is why it is retried, and why another
        is sent as soon as the welcome message arrives.

        pyserial discards any input already waiting when it opens the port (Serial.open() calls
        _reset_input_buffer()), so a welcome message is only ever seen if the controller sends it
        after the port is opened, that is, if opening the port resets the controller. The welcome
        message is therefore optional: without it, the version is asked for with $I.
        """
        self.startupTranscript = []    # every non-blank line received, decoded
        self.startupMessages = []      # the text inside each [MSG:...]
        self.startupLineResults = []   # (line, 'ok' or 'error:N') for each stored $N startup line that ran
        self.startupAlarms = []        # the code from each ALARM:N
        self.grblVersion = None        # for example '1.1h'
        self.startupState = None       # for example 'Alarm' or 'Idle'
        self._startupPartialLine = b""
        if deadlineSeconds is None:
            deadlineSeconds = self.HANDSHAKE_TIMEOUT

        start = self._now()
        deadline = start + deadlineSeconds
        nextProbe = start  # the first '?' goes out at once
        status = None

        savedTimeout = self.timeout
        self.timeout = _handshakeReadTimeoutSeconds
        try:
            if resetOnConnect:
                # Soft reset (ctrl-x). Off by default: during motion it stops the steppers and position is lost.
                # drain=False: what the controller sends after the reset is exactly what the handshake reads.
                self.softReset(drain=False)

            while status is None and self._now() < deadline:
                if self._now() >= nextProbe:
                    # '?' is a real-time byte: no newline, and no 'ok' comes back, only a status report.
                    if debug:
                        print("sending " + str(b"?"))
                    self.write(b"?")
                    nextProbe = self._now() + _probeRetrySeconds

                kind, text = self._readStartupLine()
                if kind == 'welcome':
                    self.grblVersion = _welcomePattern.search(text).group(1)
                    if not _supportedVersionPattern.match(self.grblVersion):
                        raise GrblVersionError(self._startupFailureText(
                            f"expected Grbl 1.1x firmware but the controller reported version {self.grblVersion!r}."))
                    # The controller has just reset, so any '?' sent before now was lost. Probe again at once
                    # rather than waiting for the next retry. Grbl sends nothing to mark the end of its startup
                    # output, but the status report arrives after the startup messages ([MSG:Check Limits] too).
                    if debug:
                        print("sending " + str(b"?"))
                    self.write(b"?")
                    nextProbe = self._now() + _probeRetrySeconds
                elif kind == 'status':
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
            engaged = limitPins(status['fields'].get('Pn'))
            if engaged:
                # The same refusal for a controller that was not reset, so never printed [MSG:Check Limits]:
                # the status report's Pn: field shows the limit pin is active right now.
                raise GrblLimitSwitchError(self._startupFailureText(
                    f"a limit switch is engaged (the status report shows limit pin {engaged} active). "
                    "Move the machine off the switch, or check the switch wiring, then connect again."))
            # Unlock
            self.send("$X")

    def getSettings(self):
        # Query all $-settings; Grbl answers with one "$n=value" line each, then "ok".
        gcode = "$$\n".encode('utf-8')
        if debug:
            print("sending " + str(gcode))
        self.write(gcode)  # self, not super(), like send(): lets tests substitute a scripted controller
        settings = {}
        while True:
            line = self.readline()
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
            raise GrblSettingsError(
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
            report = parseStatusReport(line.decode('utf-8', errors='replace'))
            if report and report['state'] == 'Idle' and 'MPos' in report['fields']:
                self.machineCoordinates = GrblController.Vector(tuple(float(n) for n in report['fields']['MPos'].split(',')))
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
        report = parseStatusReport(self.getStatus() or "")
        return report['state'] if report else None

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
    def softReset(self, drain=True):
        # Ctrl-X: immediate reset.  Grbl reboots and re-prints its banner; drain it all
        # so the next command doesn't read stale lines.  Returns the drained lines.
        # drain=False only sends ctrl-X, for a caller that reads the banner itself (the handshake).
        if debug:
            print("sending " + str(b'\x18'))
        self.write(b'\x18')  # self, not super(), like send(): lets tests substitute a scripted controller
        drained = []
        if not drain:
            return drained
        originalTimeout = self.timeout
        self.timeout = 2
        try:
            while True:
                line = self.readline()
                if line == b"":
                    break
                if debug:
                    print("received " + str(line))
                drained.append(line)
        finally:
            self.timeout = originalTimeout
        return drained

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
                    afterReset = self.softReset()
                    # As in the startup handshake: never unlock a machine that is sitting on a limit switch.
                    engaged = limitPins(self.getPinState())
                    if engaged or any(b"Check Limits" in resetLine for resetLine in afterReset):
                        raise GrblLimitSwitchError(
                            f"Homing toward $23={homingPosition} on {self.portDeviceName} failed with ALARM:{alarmCode}, "
                            f"and a limit switch is engaged" + (f" (limit pin {engaged} active)" if engaged else
                                                                " ([MSG:Check Limits])")
                            + ". Not unlocked with $X. Move the machine off the switch, then try again.")
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
        report = parseStatusReport(self.getStatus() or "")
        return report['fields'].get('Pn', "") if report else ""

    def runHomingCycle(self, homingPosition=None):
        if homingPosition is None:
            homingPosition = self.homingPosition  # from the profile, or bottomLeftZUp
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

