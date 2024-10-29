from GrblController import *

lower_left_origin_machine_coordinates = (-405.000, -297.000, -68.7)

if __name__ == "__main__":
    grblController = GrblController()
    print('machine coordinates at home == ' + str(grblController.runHomingCycle()))

    betweenBlocks = 30
    betweenLines = 5
    blockCount = 10
    depthIncrement = 1 / 2
    feedRate = 100
    left = 10
    length = 30
    lineCount = 5
    top = 50 * 4

    grblController.moveToMachineCoordinates(z=lower_left_origin_machine_coordinates[2] + 5)
    for j in range(blockCount):
        for i in range(lineCount):
            grblController.moveToMachineCoordinates(x=lower_left_origin_machine_coordinates[0] + left + i * betweenLines + j * betweenBlocks,
                                                    y=lower_left_origin_machine_coordinates[1] + top)

            grblController.cutToMachineCoordinates(z=lower_left_origin_machine_coordinates[2] - depthIncrement * j,
                                                   feedRate=feedRate / 2)

            grblController.cutToMachineCoordinates(x=lower_left_origin_machine_coordinates[0] + left + i * betweenLines + j * betweenBlocks,
                                                   y=lower_left_origin_machine_coordinates[1] + top - length,
                                                   feedRate=feedRate)
            grblController.moveToMachineCoordinates(z=lower_left_origin_machine_coordinates[2] + 5)

    grblController.moveToMachineCoordinates(lower_left_origin_machine_coordinates[0],
                                            lower_left_origin_machine_coordinates[1] + 280,
                                            -5)



