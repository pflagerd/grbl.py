from GrblController import *

lower_left_origin_machine_coordinates = (-405.000, -299.000, -84.000)

if __name__ == '__main__':
    grblController = GrblController()
    grblController.runHomingCycle()
    grblController.move_to_machine_coordinates(*lower_left_origin_machine_coordinates)
