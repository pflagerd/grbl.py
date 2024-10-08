from GrblController import *

lower_left_origin_machine_coordinates = (-405.000, -299.000, -84.000)

if __name__ == "__main__":
    grblController = GrblController()
    grblController.runHomingCycle()
    print(grblController.moveFastToMachineCoordinates(*lower_left_origin_machine_coordinates))

    grblController.moveFastToMachineCoordinates(z=str(float(lower_left_origin_machine_coordinates[2]) + 5))
    grblController.moveFastToMachineCoordinates(x=str(float(lower_left_origin_machine_coordinates[2]) + 5),
                                                y=str(float(lower_left_origin_machine_coordinates[2]) + 50))

    for i in range(20):
        grblController.cutToMachineCoordinates(z=str(float(lower_left_origin_machine_coordinates[2]) - 1 / 10, feed=100))

        grblController.cutToMachineCoordinates(x=str(float(lower_left_origin_machine_coordinates[2]) + 5 + i),
                                               y=str(float(lower_left_origin_machine_coordinates[2]) + 5),
                                               feed=400)

        grblController.moveFastToMachineCoordinates(z=str(float(lower_left_origin_machine_coordinates[2]) + 5))
        grblController.moveFastToMachineCoordinates(x=str(float(lower_left_origin_machine_coordinates[2]) + 5 + i),
                                                    y=str(float(lower_left_origin_machine_coordinates[2]) + 50))




