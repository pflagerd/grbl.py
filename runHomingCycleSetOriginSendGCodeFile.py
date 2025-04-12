from datetime import datetime

from GrblController import *

safeZAboveZOrigin = 5


def runHomingCycleSetOriginSendGCodeFile(x, y, z, fileName):
    grblController = GrblController()
    print(grblController.runHomingCycle())
    print(grblController.moveToMachineCoordinates(x, y, z))
    print(grblController.setOrigin(x, y, z))

    start = datetime.now()
    grblController.sendFile(fileName)
    finish = datetime.now()
    print(f'Elapsed time was {finish - start}')


if __name__ == "__main__":
    runHomingCycleSetOriginSendGCodeFile(-409.000, -298.000, -70)