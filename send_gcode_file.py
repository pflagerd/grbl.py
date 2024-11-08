from GrblController import *

workPieceLowerLeftOriginMachineCoordinates = GrblController.Vector(-405.000, -297.000, -76.0)


if __name__ == "__main__":
    safeZAboveZOrigin = 5

    grblController = GrblController()
    print(grblController.runHomingCycle())
    grblController.setOrigin(workPieceLowerLeftOriginMachineCoordinates)
    grblController.moveToMachineCoordinates(workPieceLowerLeftOriginMachineCoordinates.x, workPieceLowerLeftOriginMachineCoordinates.y)
    grblController.moveToMachineCoordinates(z=workPieceLowerLeftOriginMachineCoordinates.z + safeZAboveZOrigin)
    print(grblController.getMachineCoordinates())

    #grblController.moveToMachineCoordinates(z=workPieceLowerLeftOriginMachineCoordinates[2] + safeZAboveZOrigin)

    #grblController.sendFile("/home/oy753c/desktops/neon-candle/carveco/Toolpaths/Toadstool Logo Scaled to 30 wide - toad-ol.birch plywood.B/toadol roundover 0-3 f800 cw.gcode")
    #grblController.sendFile("/home/oy753c/desktops/neon-candle/carveco/Toolpaths/Toadstool Logo Scaled to 30 wide - toad-ol.birch plywood.B/toadol roundover 3-4 f300 ccw.gcode")
    grblController.sendFile("/home/oy753c/desktops/neon-candle/carveco/Toolpaths/Toadstool Logo Scaled to 30 wide - toad-ol.birch plywood.B/toadol roundover 0-4 f200 cw.gcode")
    grblController.sendFile("/home/oy753c/desktops/neon-candle/carveco/Toolpaths/Toadstool Logo Scaled to 30 wide - toad-ol.birch plywood.B/toadol roundover 3-4 f200 ccw.gcode")

    # grblController.moveToMachineCoordinates(workPieceLowerLeftOriginMachineCoordinates[0],
    #                                         workPieceLowerLeftOriginMachineCoordinates[1] + 280,
    #                                         -5)
    #grblController.moveToMachineCoordinates(z=safeZAboveZOrigin)
    #grblController.moveToMachineCoordinates(0, 0)
