from GrblController import *

lower_left_origin_machine_coordinates = (-405.000, -297.000, -79.4)

if __name__ == "__main__":
    grblController = GrblController()
    print('machine coordinates at home == ' + str(grblController.runHomingCycle()))

    count = 11
    between = 5

    left = 10
    top = 220
    length = 375
    feedRate = 500

    for i in range(count):
        grblController.moveToMachineCoordinates(x=lower_left_origin_machine_coordinates[0] + left,
                                                y=lower_left_origin_machine_coordinates[1] + top + i * between)
        grblController.cutToMachineCoordinates(z=lower_left_origin_machine_coordinates[2] - i,
                                               feedRate=500)

        grblController.cutToMachineCoordinates(x=lower_left_origin_machine_coordinates[0] + left + length,
                                               y=lower_left_origin_machine_coordinates[1] + top + i * between,
                                               feedRate=feedRate)
        grblController.moveToMachineCoordinates(z=lower_left_origin_machine_coordinates[2] + 5)

    grblController.moveToMachineCoordinates(lower_left_origin_machine_coordinates[0],
                                            lower_left_origin_machine_coordinates[1] + 280,
                                            -5)



