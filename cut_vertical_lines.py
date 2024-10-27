from GrblController import *

lower_left_origin_machine_coordinates = (-405.000, -299.000, -85.105)

if __name__ == "__main__":
    grblController = GrblController()
    print('machine coordinates at home == ' + str(grblController.runHomingCycle()))

    depth = 0  # 1 / 10
    left = 360
    top = 55
    length = 50

    for i in range(20):
        grblController.moveToMachineCoordinates(z=lower_left_origin_machine_coordinates[2] + 5)
        grblController.moveToMachineCoordinates(x=lower_left_origin_machine_coordinates[0] + left + i,
                                                y=lower_left_origin_machine_coordinates[1] + top)

        grblController.cutToMachineCoordinates(z=lower_left_origin_machine_coordinates[2] - depth, feedRate=100)

        grblController.cutToMachineCoordinates(x=lower_left_origin_machine_coordinates[0] + left + i,
                                               y=lower_left_origin_machine_coordinates[1] + top - length)

    grblController.moveToMachineCoordinates(lower_left_origin_machine_coordinates[0], lower_left_origin_machine_coordinates[1] + 200, -5)



