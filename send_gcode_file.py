from datetime import datetime

from GrblController import *

workPieceLowerLeftOriginMachineCoordinates = GrblController.Vector(-405.000, -297.000, -81.0)

if __name__ == "__main__":
    safeZAboveZOrigin = 5

    grblController = GrblController()
    print(grblController.runHomingCycle())

    start = datetime.now()
    grblController.sendFile("/home/oy753c/desktops/neon-candle/carveco/Toolpaths/Toadstool Logo Scaled to 30 wide - toad-ol.birch plywood.B/toadol roundover 0-12.5 f200 cw.gcode")
    finish = datetime.now()
    print(f'Elapsed time was {finish - start}')