import find_home_and_set_origin

lower_left_origin_machine_coordinates = (-405.000, -299.000, -84.000)

if __name__ == '__main__':
    perform_homing_cycle()
    move_to_machine_coordinates(*lower_left_origin_machine_coordinates)
