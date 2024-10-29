from GrblController import *

lower_left_origin_machine_coordinates = (-405.000, -297.000, -68.7)

if __name__ == "__main__":
    grblController = GrblController()
    print('machine coordinates at home == ' + str(grblController.runHomingCycle()))

    betweenBlocks = 30
    betweenLines = 5
    blockWidth = 30
    depth = 5.1
    feedRate = 1000
    left = 10
    length = 30
    top = 50
    workPieceWidth = 360

    grblController.moveToMachineCoordinates(z=lower_left_origin_machine_coordinates[2] + 5)
    for l in range(depthIncrement, 2.1,  1 / 2)
        for k in range(left, betweenBlocks, workPieceWidth):
            for j in range(left, betweenLines, blockWidth):
                for i in range(0, depth, l):
                    grblController.moveToMachineCoordinates(x=lower_left_origin_machine_coordinates[0] + left + j + k,
                                                            y=lower_left_origin_machine_coordinates[1] + top)

                    grblController.cutToMachineCoordinates(z=lower_left_origin_machine_coordinates[2] - i,
                                                           feedRate=feedRate / 2)

                    grblController.cutToMachineCoordinates(x=lower_left_origin_machine_coordinates[0] + left + j + k,
                                                           y=lower_left_origin_machine_coordinates[1] + top - length,
                                                           feedRate=feedRate)
                    grblController.moveToMachineCoordinates(z=lower_left_origin_machine_coordinates[2] + 5)

    grblController.moveToMachineCoordinates(lower_left_origin_machine_coordinates[0],
                                            lower_left_origin_machine_coordinates[1] + 280,
                                            -5)



