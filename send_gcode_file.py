from datetime import datetime

from GrblController import *

workPieceLowerLeftOriginMachineCoordinates = GrblController.Vector(-405.000, -297.000, -81.0)


if __name__ == "__main__":
    safeZAboveZOrigin = 5

    grblController = GrblController()
    print(grblController.runHomingCycle())
    grblController.setOrigin(workPieceLowerLeftOriginMachineCoordinates)
    # grblController.moveToMachineCoordinates(workPieceLowerLeftOriginMachineCoordinates.x, workPieceLowerLeftOriginMachineCoordinates.y)
    # grblController.moveToMachineCoordinates(z=workPieceLowerLeftOriginMachineCoordinates.z + safeZAboveZOrigin)
    # print(grblController.getMachineCoordinates())

    # grblController.sendFile("/home/oy753c/desktops/neon-candle/carveco/Toolpaths/Toadstool Logo Scaled to 30 wide - toad-ol.birch plywood.B/toadol roundover -5.1 f400 ccw.gcode")
    # grblController.sendFile("/home/oy753c/desktops/neon-candle/carveco/Toolpaths/Toadstool Logo Scaled to 30 wide - toad-ol.birch plywood.B/toadol roundover -5.2 f400 cw.gcode")
    #grblController.sendFile("/home/oy753c/desktops/neon-candle/carveco/Toolpaths/Toadstool Logo Scaled to 30 wide - toad-ol.birch plywood.B/toadol roundover -5.3 f400 ccw.gcode")
    # grblController.sendFile("/home/oy753c/desktops/neon-candle/carveco/Toolpaths/Toadstool Logo Scaled to 30 wide - toad-ol.birch plywood.B/toadol roundover -5.4 f400 cw.gcode")
    start = datetime.now()
    # grblController.sendFile("/home/oy753c/desktops/neon-candle/carveco/Toolpaths/Toadstool Logo Scaled to 30 wide - toad-ol.birch plywood.B/toadol roundover -5.5 f400 cw.gcode")
    # grblController.sendFile("/home/oy753c/desktops/neon-candle/carveco/Toolpaths/Toadstool Logo Scaled to 30 wide - toad-ol.birch plywood.B/toadol roundover -5.5 f400 ccw.gcode")
    # grblController.sendFile("/home/oy753c/desktops/neon-candle/carveco/Toolpaths/Toadstool Logo Scaled to 30 wide - toad-ol.birch plywood.B/toadol roundover -5.5 f200 cw.gcode")
    # grblController.sendFile("/home/oy753c/desktops/neon-candle/carveco/Toolpaths/Toadstool Logo Scaled to 30 wide - toad-ol.birch plywood.B/toadol roundover -5.5 f200 ccw.gcode")
    # grblController.sendFile("/home/oy753c/desktops/neon-candle/carveco/Toolpaths/Toadstool Logo Scaled to 30 wide - toad-ol.birch plywood.B/toadol roundover -5.5 f100 cw.gcode")
    # grblController.sendFile("/home/oy753c/desktops/neon-candle/carveco/Toolpaths/Toadstool Logo Scaled to 30 wide - toad-ol.birch plywood.B/toadol roundover -5.5 f100 ccw.gcode")

    #grblController.sendFile("/home/oy753c/desktops/neon-candle/carveco/Toolpaths/Toadstool Logo Scaled to 30 wide - toad-ol.birch plywood.B/toadol roundover 0-8.5 f800 cw.gcode")
    grblController.sendFile("/home/oy753c/desktops/neon-candle/carveco/Toolpaths/Toadstool Logo Scaled to 30 wide - toad-ol.birch plywood.B/toadol roundover 0-8.5 f200 cw.gcode")
    grblController.sendFile("/home/oy753c/desktops/neon-candle/carveco/Toolpaths/Toadstool Logo Scaled to 30 wide - toad-ol.birch plywood.B/toadol roundover 0-12.5 f800 cw.gcode")
    grblController.sendFile("/home/oy753c/desktops/neon-candle/carveco/Toolpaths/Toadstool Logo Scaled to 30 wide - toad-ol.birch plywood.B/toadol roundover 0-12.5 f200 cw.gcode")
    finish = datetime.now()
    print(f'Elapsed time was {finish - start}')

    # grblController.moveToMachineCoordinates(workPieceLowerLeftOriginMachineCoordinates[0],
    #                                         workPieceLowerLeftOriginMachineCoordinates[1] + 280,
    #                                         -5)
    #grblController.moveToMachineCoordinates(z=safeZAboveZOrigin)
    #grblController.moveToMachineCoordinates(0, 0)
