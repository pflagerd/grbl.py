#!/usr/bin/env -S sh -c 'd=$(realpath "$0");exec "${d%/*}/.venv-helper" "$d" "$@"'

import serial
import sys

port = "/dev/ttyUSB0"

if __name__ == '__main__':
    grblSerial = serial.Serial(port, baudrate=115200)

    looping = True
    while looping:
        line = grblSerial.readline()
        print(str(line))
        line = input("> ")
        if len(line) != 0:
            print(str(line))
            line += "\n"
            grblSerial.write(line.encode('utf-8'))

    grblSerial.close()
