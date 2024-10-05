#!/usr/env python

from find_and_set_home import find_and_set_home
from find_and_set_home import top_right_z_up
from find_and_set_home import top_left_z_up
from find_and_set_home import bottom_right_z_up
from find_and_set_home import bottom_left_z_up
from find_and_set_home import top_right_z_down

if __name__ == '__main__':
    user_input = input("WARNING: Please remove any tool from the spindle.  This will probe for the minimum z-coordinate, which will potentially plunge a tool into the spoiler-board. ")
    #print("You entered:", user_input)

    top_right_up = find_and_set_home(top_right_z_up)
    top_right_down = find_and_set_home(top_right_z_down)
    top_left_up = find_and_set_home(top_left_z_up)
    bottom_right_up = find_and_set_home(bottom_right_z_up)
    bottom_left_up = find_and_set_home(bottom_left_z_up)
    print(F'top_right_up == {top_right_up}')
    print(F'top_right_down == {top_right_down}')
    print(F'top_left_up == {top_left_up}')
    print(F'bottom_right_up == {bottom_right_up}')
    print(F'bottom_left_up == {bottom_left_up}')
