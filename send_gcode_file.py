from datetime import datetime

from GrblController import *

workPieceLowerLeftOriginMachineCoordinates = GrblController.Vector(-409.000, -298.000, -70)

if __name__ == "__main__":
    safeZAboveZOrigin = 5

    grblController = GrblController()
    print(grblController.runHomingCycle())
    print(grblController.moveToMachineCoordinates(*workPieceLowerLeftOriginMachineCoordinates))
    print(grblController.setOrigin(*workPieceLowerLeftOriginMachineCoordinates))

    start = datetime.now()
    # grblController.sendFile("/home/oy753c/desktops/neon-candle/carveco/Toolpaths/Toadstool Logo Scaled to 30 wide - sto.birch plywood.B/lower left sto climb inside.transformed.gcode")
    grblController.sendFile("/home/oy753c/desktops/neon-candle/carveco/Toolpaths/Toadstool Logo Scaled to 30 wide - sto.birch plywood.B/cutToadstoolStoLowerLeft.gcode")
    finish = datetime.now()
    print(f'Elapsed time was {finish - start}')