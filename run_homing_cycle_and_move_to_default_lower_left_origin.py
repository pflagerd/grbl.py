from GrblController import *

lower_left_origin_machine_coordinates = (-409.000, -298.000, -68.2)

if __name__ == '__main__':
    grblController = GrblController()
    grblController.runHomingCycle()
    print(grblController.moveToMachineCoordinates(*lower_left_origin_machine_coordinates))
