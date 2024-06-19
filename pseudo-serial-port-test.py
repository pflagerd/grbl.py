import serial

import io
import serial
import shutil
import subprocess


args = "socat -d -d pty,raw,echo=0 pty,raw,echo=0".split()
args[0] = shutil.which(args[0])
process = subprocess.Popen(args, stderr=subprocess.PIPE)

with process.stderr:
    for line in iter(process.stderr.readline, ''):
        print(line, end='')  # Process the line (e.g., print it)

ser1 = serial.Serial('/dev/pts/5', 115200)
ser2 = serial.Serial('/dev/pts/6', 115200)

ser1.write(b'Hello from ser1\n')
print(ser2.readline())

ser2.write(b'Hello from ser2\n')
print(ser1.readline())

ser1.close()
ser2.close()
