from GrblController import *

lower_left_origin_machine_coordinates = (-405.000, -299.000, -84.800)

if __name__ == "__main__":
    grblController = GrblController()
    print('machine coordinates at home == ' + str(grblController.runHomingCycle()))

    count = 5
    between = 4
    depth = 0
    left = 5 + 270
    top = 150
    length = 90
    feedRate = 1000

    for i in range(count):
        grblController.moveToMachineCoordinates(z=lower_left_origin_machine_coordinates[2] + 5)
        grblController.moveToMachineCoordinates(x=lower_left_origin_machine_coordinates[0] + left + i * between,
                                                y=lower_left_origin_machine_coordinates[1] + top)

        grblController.cutToMachineCoordinates(z=lower_left_origin_machine_coordinates[2] - depth,
                                               feedRate=500)

        grblController.cutToMachineCoordinates(x=lower_left_origin_machine_coordinates[0] + left + i * between,
                                               y=lower_left_origin_machine_coordinates[1] + top - length,
                                               feedRate=feedRate)

    grblController.moveToMachineCoordinates(z=lower_left_origin_machine_coordinates[2] + 5)
    grblController.moveToMachineCoordinates(lower_left_origin_machine_coordinates[0],
                                            lower_left_origin_machine_coordinates[1] + 280,
                                            -5)



