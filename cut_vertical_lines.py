from GrblController import *

workPieceLowerLeftOriginMachineCoordinates = (-405.000, -297.000, -68.7)


def inclusiveFloatRange(start, stop, step):
    while start < stop + step:
        yield start  # rounding to avoid floating-point precision issues
        start += step


if __name__ == "__main__":
    grblController = GrblController()
    print('machine coordinates at home == ' + str(grblController.runHomingCycle()))

    blockWidth = 20
    currentBlockY = 0
    currentLineY = 0
    depth = 5
    feedRate = 1000
    lineHeight = 30
    safeZAboveZOrigin = 5
    widthBetweenLines = 5
    widthBetweenBlocks = 30
    workPieceMaximumX = 390
    workPieceMaximumY = 290
    workPieceMinimumX = 10
    workPieceMinimumY = 10

    grblController.moveToMachineCoordinates(z=workPieceLowerLeftOriginMachineCoordinates[2] + safeZAboveZOrigin)
    for currentZIncrement in inclusiveFloatRange(1 / 2, 3, 1 / 2):  # depth increments
        for currentBlockX in inclusiveFloatRange(0, widthBetweenBlocks, workPieceMaximumX - blockWidth - workPieceMinimumX):
            for currentLineX in inclusiveFloatRange(0, blockWidth, widthBetweenLines):
                for currentLineZ in inclusiveFloatRange(0, depth, currentZIncrement):
                    grblController.moveToMachineCoordinates(x=workPieceLowerLeftOriginMachineCoordinates[0] + workPieceMinimumX + currentBlockX + currentLineX,
                                                            y=workPieceLowerLeftOriginMachineCoordinates[1] + workPieceMinimumY + currentBlockY + currentLineY + lineHeight)

                    grblController.cutToMachineCoordinates(z=workPieceLowerLeftOriginMachineCoordinates[2] - currentLineZ,
                                                           feedRate=feedRate / 2)

                    grblController.cutToMachineCoordinates(x=workPieceLowerLeftOriginMachineCoordinates[0] + workPieceMinimumX + currentBlockX + currentLineX,
                                                           y=workPieceLowerLeftOriginMachineCoordinates[1] + workPieceMinimumY + currentBlockY + currentLineY,
                                                           feedRate=feedRate)
                    grblController.moveToMachineCoordinates(z=workPieceLowerLeftOriginMachineCoordinates[2] + safeZAboveZOrigin)

    grblController.moveToMachineCoordinates(workPieceLowerLeftOriginMachineCoordinates[0],
                                            workPieceLowerLeftOriginMachineCoordinates[1] + 280,
                                            -5)
