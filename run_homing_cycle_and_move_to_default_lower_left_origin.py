from GrblController import *

lower_left_origin_machine_coordinates = (-405.000, -297.000, -79.4)

if __name__ == '__main__':
    grblController = GrblController()
    grblController.runHomingCycle()
    print(grblController.moveToMachineCoordinates(*lower_left_origin_machine_coordinates))
