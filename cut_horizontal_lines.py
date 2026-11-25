from GrblController import *

if __name__ == "__main__":
    grblController = GrblController()
    lower_left_origin_machine_coordinates = grblController.runHomingCycle()
    print('machine coordinates at home == ' + str(lower_left_origin_machine_coordinates))

    cutWidth = 20

    height = 225
    length = 395
    feedRate = 1500

    x = 0
    y = 0
    z = -75.5
    while y <= height:
        grblController.moveToMachineCoordinates(x=lower_left_origin_machine_coordinates[0] + x,
                                                y=lower_left_origin_machine_coordinates[1] + y,
                                                z=lower_left_origin_machine_coordinates[2] + z + 5)

        grblController.cutToMachineCoordinates(z=lower_left_origin_machine_coordinates[2] + z, feedRate=feedRate/10)

        grblController.cutToMachineCoordinates(x=lower_left_origin_machine_coordinates[0] + x + length,
                                               y=lower_left_origin_machine_coordinates[1] + y,
                                               feedRate=feedRate)
        grblController.moveToMachineCoordinates(z=lower_left_origin_machine_coordinates[2] + z + 5)
        y += cutWidth

    grblController.moveToMachineCoordinates(z=lower_left_origin_machine_coordinates[2])



