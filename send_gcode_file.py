from GrblController import *

workPieceLowerLeftOriginMachineCoordinates = (-405.000, -297.000, -68.5)


if __name__ == "__main__":
    grblController = GrblController()
    #print('machine coordinates at home == ' + str(grblController.runHomingCycle()))
    print('machine coordinates == ' + str(grblController.unlock()))

    safeZAboveZOrigin = 5

    #grblController.moveToMachineCoordinates(z=workPieceLowerLeftOriginMachineCoordinates[2] + safeZAboveZOrigin)

    with open("/home/oy753c/desktops/neon-candle/carveco/Toolpaths/Toadstool Logo Scaled to 30 wide - toad-ol.birch plywood.B/o 0-0 fr1000 climb.gcode", "rt") as gcodeFile:
        for gcodeLine in gcodeFile:
            gcode = gcodeLine.rstrip()
            grblController.sendAndWait(gcode)

    # grblController.moveToMachineCoordinates(workPieceLowerLeftOriginMachineCoordinates[0],
    #                                         workPieceLowerLeftOriginMachineCoordinates[1] + 280,
    #                                         -5)
    grblController.moveToMachineCoordinates(z=safeZAboveZOrigin)
    grblController.moveToMachineCoordinates(0, 0)
