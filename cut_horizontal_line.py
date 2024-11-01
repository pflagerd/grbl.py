from GrblController import *

lower_left_origin_machine_coordinates = (-405.000, -297.000, -76.7)

if __name__ == "__main__":
    grblController = GrblController()
    print('machine coordinates at home == ' + str(grblController.runHomingCycle()))

    depth = 1
    left = 10
    y = 10
    length = 220
    feedRate = 1000
    safeZOffset = 5
    widthBetweenHorizontalLines = 10

    for depth in range(1, 14):
        grblController.moveToMachineCoordinates(x=lower_left_origin_machine_coordinates[0] + left,
                                                y=lower_left_origin_machine_coordinates[1] + y)
        grblController.moveToMachineCoordinates(z=lower_left_origin_machine_coordinates[2])
        grblController.cutToMachineCoordinates(z=lower_left_origin_machine_coordinates[2] - depth,
                                               feedRate=feedRate/10)

        grblController.cutToMachineCoordinates(x=lower_left_origin_machine_coordinates[0] + left + length,
                                               y=lower_left_origin_machine_coordinates[1] + y,
                                               feedRate=feedRate)
        grblController.cutToMachineCoordinates(z=lower_left_origin_machine_coordinates[2] + safeZOffset)
        y += widthBetweenHorizontalLines

    grblController.moveToMachineCoordinates(lower_left_origin_machine_coordinates[0],
                                            lower_left_origin_machine_coordinates[1] + 280,
                                            -5)



