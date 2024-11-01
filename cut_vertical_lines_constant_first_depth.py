from GrblController import *

workPieceLowerLeftOriginMachineCoordinates = (-405.000, -297.000, -68.5)


if __name__ == "__main__":
    grblController = GrblController()
    print('machine coordinates at home == ' + str(grblController.runHomingCycle()))

    blockHeight = 45  # lineHeight + 15
    feedRate = 1000
    lineHeight = 30
    safeZAboveZOrigin = 5
    startDepth = 2.0
    endDepth = 3.0
    widthBetweenLines = 10
    widthBetweenBlocks = 40
    workPieceMaximumX = 380
    workPieceMaximumY = 290
    workPieceMinimumX = 10
    workPieceMinimumY = 10
    xStartCount = 24
    yCount = 2

    grblController.moveToMachineCoordinates(z=workPieceLowerLeftOriginMachineCoordinates[2] + safeZAboveZOrigin)

    feedsAndSpeeds = [[1000, 0, 3], [1000, 3, 4], [1000, 4, 4.4], [1000, 4.4, 4.5], [1000, 4.5, 4.5], [400, 4.5, 4.5], [200, 4.5, 4.5]]

    #for yCount in range(1, (workPieceMaximumY - workPieceMinimumY) // blockHeight):
    for i in range(0, len(feedsAndSpeeds)):
        for xCount in range(xStartCount, (workPieceMaximumX - workPieceMinimumX) // widthBetweenLines):
            print(f'xCount == {xCount}')
            grblController.moveToMachineCoordinates(x=workPieceLowerLeftOriginMachineCoordinates[0] + workPieceMinimumX + xCount * widthBetweenLines,
                                                    y=workPieceLowerLeftOriginMachineCoordinates[1] + workPieceMinimumY + yCount * blockHeight + lineHeight)

            grblController.cutToMachineCoordinates(z=workPieceLowerLeftOriginMachineCoordinates[2] - feedsAndSpeeds[i][1],
                                                   feedRate=feedsAndSpeeds[i][0])
            grblController.cutToMachineCoordinates(z=workPieceLowerLeftOriginMachineCoordinates[2] - feedsAndSpeeds[i][2],
                                                   feedRate=feedRate / 10)

            grblController.cutToMachineCoordinates(x=workPieceLowerLeftOriginMachineCoordinates[0] + workPieceMinimumX + xCount * widthBetweenLines,
                                                   y=workPieceLowerLeftOriginMachineCoordinates[1] + workPieceMinimumY + yCount * blockHeight,
                                                   feedRate=feedsAndSpeeds[i][0])
            grblController.cutToMachineCoordinates(z=workPieceLowerLeftOriginMachineCoordinates[2] + safeZAboveZOrigin)

    grblController.moveToMachineCoordinates(workPieceLowerLeftOriginMachineCoordinates[0],
                                            workPieceLowerLeftOriginMachineCoordinates[1] + 280,
                                            -5)
