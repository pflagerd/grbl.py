#!/usr/env python

from find_home_and_set_origin import find_home_and_set_origin
from find_home_and_set_origin import top_right_z_up
from find_home_and_set_origin import top_left_z_up
from find_home_and_set_origin import bottom_right_z_up
from find_home_and_set_origin import bottom_left_z_up
from find_home_and_set_origin import top_right_z_down

if __name__ == '__main__':
    user_input = input("WARNING: Please remove any tool from the spindle.  This will probe for the minimum z-coordinate, which will potentially plunge a tool into the spoiler-board. ")
    #print("You entered:", user_input)

    top_right_up = find_home_and_set_origin(top_right_z_up)
    top_right_down = find_home_and_set_origin(top_right_z_down)
    top_left_up = find_home_and_set_origin(top_left_z_up)
    bottom_right_up = find_home_and_set_origin(bottom_right_z_up)
    bottom_left_up = find_home_and_set_origin(bottom_left_z_up)
    print(F'top_right_up == {top_right_up}')
    print(F'top_right_down == {top_right_down}')
    print(F'top_left_up == {top_left_up}')
    print(F'bottom_right_up == {bottom_right_up}')
    print(F'bottom_left_up == {bottom_left_up}')
