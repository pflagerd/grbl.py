import serial

import serial

ser1 = serial.Serial('/dev/pts/5', 115200)
ser2 = serial.Serial('/dev/pts/6', 115200)

ser1.write(b'Hello from ser1\n')
print(ser2.readline())

ser2.write(b'Hello from ser2\n')
print(ser1.readline())

ser1.close()
ser2.close()
