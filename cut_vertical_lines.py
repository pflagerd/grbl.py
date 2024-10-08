from GrblController import *

lower_left_origin_machine_coordinates = (-405.000, -299.000, -84.500)

if __name__ == "__main__":
    grblController = GrblController()
    grblController.runHomingCycle()
    print(grblController.moveFastToMachineCoordinates(*lower_left_origin_machine_coordinates))

    grblController.moveFastToMachineCoordinates(z=str(float(lower_left_origin_machine_coordinates[2]) + 5))
    grblController.moveFastToMachineCoordinates(x=str(float(lower_left_origin_machine_coordinates[0]) + 5),
                                                y=str(float(lower_left_origin_machine_coordinates[1]) + 50))

    grblController.startSpindleMotor()
    for i in range(20):
        grblController.cutToMachineCoordinates(z=str(float(lower_left_origin_machine_coordinates[2]) - 1 / 10), feedRate=100)

        grblController.cutToMachineCoordinates(x=str(float(lower_left_origin_machine_coordinates[0]) + 5 + i),
                                               y=str(float(lower_left_origin_machine_coordinates[1]) + 5),
                                               feedRate=400)

        grblController.moveFastToMachineCoordinates(z=str(float(lower_left_origin_machine_coordinates[2]) + 5))
        grblController.moveFastToMachineCoordinates(x=str(float(lower_left_origin_machine_coordinates[0]) + 5 + i),
                                                    y=str(float(lower_left_origin_machine_coordinates[1]) + 50))

    grblController.stopSpindleMotor()


