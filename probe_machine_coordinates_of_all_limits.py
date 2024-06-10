#!/usr/env python

from find_and_set_home import find_and_set_home
from find_and_set_home import top_right_z_up
from find_and_set_home import top_left_z_up
from find_and_set_home import bottom_right_z_up
from find_and_set_home import bottom_left_z_up
from find_and_set_home import top_right_z_down

if __name__ == '__main__':
    find_and_set_home(top_right_z_up)
    find_and_set_home(top_right_z_down)
    find_and_set_home(top_left_z_up)
    find_and_set_home(bottom_right_z_up)
    find_and_set_home(bottom_left_z_up)
