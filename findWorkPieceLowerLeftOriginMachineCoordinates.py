from GrblController import *

workPieceLowerLeftOriginMachineCoordinates = GrblController.Vector(-405.000, -297.000, -81.0)

if __name__ == '__main__':
    grblController = GrblController()
    print(grblController.runHomingCycle())
    grblController.moveToMachineCoordinates(workPieceLowerLeftOriginMachineCoordinates)
