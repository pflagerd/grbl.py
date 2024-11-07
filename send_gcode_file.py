from GrblController import *

workPieceLowerLeftOriginMachineCoordinates = (-405.000, -297.000, -68.5)


if __name__ == "__main__":
    grblController = GrblController()
    print(grblController.getMachineCoordinates())

    safeZAboveZOrigin = 5

    #grblController.moveToMachineCoordinates(z=workPieceLowerLeftOriginMachineCoordinates[2] + safeZAboveZOrigin)

    grblController.sendFile("/home/oy753c/desktops/neon-candle/carveco/Toolpaths/Toadstool Logo Scaled to 30 wide - toad-ol.birch plywood.B/toadol roundover 0-0 f1000 cw.gcode")

    # grblController.moveToMachineCoordinates(workPieceLowerLeftOriginMachineCoordinates[0],
    #                                         workPieceLowerLeftOriginMachineCoordinates[1] + 280,
    #                                         -5)
    #grblController.moveToMachineCoordinates(z=safeZAboveZOrigin)
    #grblController.moveToMachineCoordinates(0, 0)
