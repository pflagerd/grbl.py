from GrblController import *

lower_left_origin_machine_coordinates = (-405.000, -299.000, -84.000)

if __name__ == '__main__':
    grblController = GrblController()
    grblController.runHomingCycle()
    print(grblController.cutToMachineCoordinates(*lower_left_origin_machine_coordinates))
