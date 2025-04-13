from GrblController import *

if __name__ == '__main__':
    grblController = GrblController()
    grblController.runHomingCycle()
    print(grblController.moveToMachineCoordinates(-333, -150, -68))
