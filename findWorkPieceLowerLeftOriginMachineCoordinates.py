from GrblController import *

#workPieceLowerLeftOriginMachineCoordinates = GrblController.Vector(-405.000, -297.000, -81.0)
workPieceLowerLeftOriginMachineCoordinates = GrblController.Vector(-407.000, -300.000, -89.0)   # -89

if __name__ == '__main__':
    grblController = GrblController()
    print(grblController.runHomingCycle())
    grblController.moveToMachineCoordinates(workPieceLowerLeftOriginMachineCoordinates)
