from GrblController import *

workPieceLowerLeftOriginMachineCoordinates = (-405.000, -297.000, -68.5)


if __name__ == "__main__":
    grblController = GrblController()
    print('machine coordinates at home == ' + str(grblController.runHomingCycle()))

    blockHeight = 40  # lineHeight + 10
    blockWidth = 20
    currentBlockY = 4 * blockHeight
    currentLineY = 0
    depth = 4.5
    feedRate = 1000
    lineHeight = 30
    safeZAboveZOrigin = 5
    widthBetweenLines = 10
    widthBetweenBlocks = 40
    workPieceMaximumX = 390
    workPieceMaximumY = 290
    workPieceMinimumX = 10
    workPieceMinimumY = 10

    grblController.moveToMachineCoordinates(z=workPieceLowerLeftOriginMachineCoordinates[2] + safeZAboveZOrigin)

    currentBlockX = 0
    currentZIncrement = 1 / 2  # this changes for each block.
    while True:  # move block by block using currentBlockX
        currentLineX = 0
        while True:  # move line by line using currentLineX
            currentLineZ = 0
            lastCurrentZIncrement = currentZIncrement
            while True:  # move currentZIncrement by currentZIncrement
                print(f'currentLineZ == {currentLineZ}, lastCurrentZIncrement was {lastCurrentZIncrement}')
                grblController.moveToMachineCoordinates(x=workPieceLowerLeftOriginMachineCoordinates[0] + workPieceMinimumX + currentBlockX + currentLineX,
                                                        y=workPieceLowerLeftOriginMachineCoordinates[1] + workPieceMinimumY + currentBlockY + currentLineY + lineHeight)

                grblController.cutToMachineCoordinates(z=workPieceLowerLeftOriginMachineCoordinates[2],
                                                       feedRate=feedRate)
                grblController.cutToMachineCoordinates(z=workPieceLowerLeftOriginMachineCoordinates[2] - currentLineZ,
                                                       feedRate=feedRate / 10)

                grblController.cutToMachineCoordinates(x=workPieceLowerLeftOriginMachineCoordinates[0] + workPieceMinimumX + currentBlockX + currentLineX,
                                                       y=workPieceLowerLeftOriginMachineCoordinates[1] + workPieceMinimumY + currentBlockY + currentLineY,
                                                       feedRate=feedRate)
                grblController.moveToMachineCoordinates(z=workPieceLowerLeftOriginMachineCoordinates[2] + safeZAboveZOrigin)
                if currentLineZ == depth:
                    break
                if currentLineZ + lastCurrentZIncrement < depth:
                    currentLineZ += lastCurrentZIncrement
                    continue
                lastCurrentZIncrement = depth - currentLineZ
                currentLineZ = depth
                continue
            if currentLineX < blockWidth:
                currentLineX += widthBetweenLines
                continue
            break
        currentZIncrement += 1 / 2
        if currentBlockX < (workPieceMaximumX - workPieceMinimumX) - widthBetweenBlocks:  # I have to think this through.  I always struggle with inclusive/exclusive. (workPieceMaximumX - workPieceMinimumX) is the effective available workpiece width
            currentBlockX += widthBetweenBlocks
            continue
        break

    grblController.moveToMachineCoordinates(workPieceLowerLeftOriginMachineCoordinates[0],
                                            workPieceLowerLeftOriginMachineCoordinates[1] + 280,
                                            -5)
