from GrblController import *

lower_left_origin_machine_coordinates = (-405.000, -299.000, -84.500)

if __name__ == '__main__':
    grblController = GrblController()
    grblController.runHomingCycle()
    machineCoordinatesOrigin = grblController.cutToMachineCoordinates(*lower_left_origin_machine_coordinates)
    print(machineCoordinatesOrigin)
    grblController.startSpindleMotor()
    workpieceSize = (394, 289)
    grblController.cutToMachineCoordinates(y=str(float(machineCoordinatesOrigin[1]) + workpieceSize[1]))
    grblController.cutToMachineCoordinates(x=str(float(machineCoordinatesOrigin[0]) + workpieceSize[0]))
    grblController.cutToMachineCoordinates(y=machineCoordinatesOrigin[1])
    grblController.cutToMachineCoordinates(x=machineCoordinatesOrigin[0])
    grblController.stopSpindleMotor()
