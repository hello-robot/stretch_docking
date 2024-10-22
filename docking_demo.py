import d435_rgb as dc
import d405_helpers as dh
import numpy as np
import cv2
import normalized_velocity_control as nvc
import stretch_body.robot as rb
import time
import aruco_detector as ad
import yaml
from yaml.loader import SafeLoader
from hello_helpers import hello_misc as hm
import argparse
import loop_timer as lt
import pprint as pp
from image_processing_helpers import fit_image_to_screen

def compute_visual_servoing_features(center_xyz, midline_xyz, camera_info):
    if (center_xyz is None) or (midline_xyz is None):
        return None, None
    
    center_xy = dh.pixel_from_3d(center_xyz, camera_info)
    
    length = 1.0
    end_xyz = center_xyz + (length * midline_xyz)
    end_xy = dh.pixel_from_3d(end_xyz, camera_info)
    
    midline_xy = (end_xy - center_xy)
    midline_xy_mag = np.linalg.norm(midline_xy)
    if midline_xy_mag > 0.0:
        midline_xy = midline_xy / midline_xy_mag
    else:
        midline_xy = None

    return center_xy, midline_xy


def display_visual_servoing_features(center_xy, midline_xy, image, color=None, length=100.0):
    if (center_xy is None) or (midline_xy is None):
        return

    radius = 6
    thickness = -1
    if color is None:
        color = [255, 255, 0]
    center = np.round(center_xy).astype(np.int32)
    cv2.circle(image, center, radius, color, -1, lineType=cv2.LINE_AA)

    radius = 6
    thickness = 2
    if color is None: 
        color = [255, 0, 0]
    start = center
    end = np.round(center_xy + (length * midline_xy)).astype(np.int32)
    cv2.line(image, start, end, color, thickness, lineType=cv2.LINE_AA)

def vector_error(target, current):
    err_mag = 1.0 - np.dot(target, current)
    err_sign = np.sign(np.cross(target, current))
    err = err_sign * err_mag
    return err

def get_pix_per_m(camera_info):
    # Set the pixel per meter conversion value for the
    # current image resolution. Success is sensitive
    # to this value, so it's better to set it to a
    # constant value instead of estimating it from the
    # ArUco markers.

    # PLEASE NOTE THAT THIS IS AN APPROXIMATION THAT DOES NOT TAKE
    # INTO ACCOUNT THE TILT OF THE CAMERA AND THUS DOES NOT ACCOUNT
    # FOR VARIATION ACROSS THE IMAGE DUE TO THE FLOOR PLANE BEING AT
    # AN ANGLE RELATIVE TO THE IMAGE.
    
    fx = camera_info['camera_matrix'][0,0]
    pix_per_m = fx * (1050.0/1362.04443)
    print(f'{fx=}')
    print(f'{pix_per_m=}')
    return pix_per_m

def pre_docking_center(dock_center_xy, dock_midline_xy, pix_per_m):
    # find the pre-docking waypoint
    dist_pix = pix_per_m * pre_docking_distance_m
    pre_docking_center_xy = dock_center_xy + (dist_pix * dock_midline_xy)
    return pre_docking_center_xy

def docking_pose(base_center_xy, base_midline_xy, dock_center_xy, dock_midline_xy, pix_per_m):
    pre_docking_center_xy = pre_docking_center(dock_center_xy, dock_midline_xy, pix_per_m)
    
    center_diff_xy = base_center_xy - pre_docking_center_xy
    dock_side_sign = np.sign(np.cross(dock_midline_xy, center_diff_xy))
    if dock_side_sign < 0.0:
        left_of_dock = False
    else:
        left_of_dock = True

    center_diff_xy = dock_center_xy - base_center_xy
    center_diff_xy = center_diff_xy / np.linalg.norm(center_diff_xy)
    if left_of_dock:
        abs_direction_err = abs(1.0 - np.dot(center_diff_xy, base_midline_xy))
        facing_sign = np.dot(base_midline_xy, dock_midline_xy)
    else: 
        abs_direction_err = abs(1.0 - np.dot(center_diff_xy, -base_midline_xy))
        facing_sign = np.dot(-base_midline_xy, dock_midline_xy)

    if (facing_sign > 0.0) and (abs_direction_err < (1.0 - np.cos(np.pi/2.0))):
        facing_dock = True
    else:
        facing_dock = False
        
    return facing_dock, left_of_dock


####################################
# Miscellaneous Parameters

motion_on = True
print_timing = False #True

# Defines a deadzone for mobile base rotation, since low values can
# lead to no motion and noises on some surfaces like carpets.
min_base_speed = 0.0 #0.05

successful_pre_docking_err_m = 0.01
successful_pre_docking_err_ang = 0.05
successful_rotate_err_ang = 0.01
successful_pan_err = 0.2

pre_docking_distance_m = 0.55 #0.63 #0.5

####################################
## Gains for Visual Servoing

overall_visual_servoing_velocity_scale = 0.02 #0.01 #1.0

joint_visual_servoing_velocity_scale = {
    'base_forward' : 0.1, #15.0
    'base_counterclockwise' : 400.0,
    'head_pan_counterclockwise' : 2.0
}

####################################
## Initial Pose

initial_joint_state = {
    'head_pan_pos': -(np.pi + np.pi/4.0), #-np.pi,
    'head_tilt_pos': (-np.pi/2.0) + (np.pi/14.0), #(-np.pi/2.0) + (np.pi/10.0), 
    'lift_pos' : 0.3,
    'arm_pos': 0.01,
    'wrist_yaw_pos': (np.pi * (3.5/4.0)),
    'wrist_pitch_pos': 0.0,
    'wrist_roll_pos': 0.0,
    'gripper_pos': 10.46
}

####################################
## Allowed Ranges of Motion

min_joint_state = {
    'base_odom_theta' : -100.0, #-0.8,
    'base_odom_x' : -100.0, #-0.2
    'head_pan_pos' : -(np.pi + np.pi/4.0)
    }

max_joint_state = {
    'base_odom_theta' : 100.0, #0.8,
    'base_odom_x' : 100.0, #0.2
    'head_pan_pos' : np.pi * 3.0/4.0
    }


####################################
## Zero Velocity Command

zero_vel = {
    'base_forward': 0.0,
    'base_counterclockwise': 0.0,
    'head_pan_counterclockwise': 0.0
}

####################################
## Translate Between Keys

pos_to_vel_cmd = {
    'base_odom_x' : 'base_forward', 
    'base_odom_theta' : 'base_counterclockwise',
    'head_pan_pos' : 'head_pan_counterclockwise'
}

vel_cmd_to_pos = { v:k for (k,v) in pos_to_vel_cmd.items() }

####################################


def move_to_initial_pose(robot):
    robot.head.move_to('head_pan', initial_joint_state['head_pan_pos'])
    robot.head.move_to('head_tilt', initial_joint_state['head_tilt_pos'])
    robot.push_command()
    robot.wait_command()

    robot.end_of_arm.get_joint('wrist_yaw').move_to(initial_joint_state['wrist_yaw_pos'])
    robot.end_of_arm.get_joint('wrist_pitch').move_to(initial_joint_state['wrist_pitch_pos'])
    robot.push_command()
    robot.wait_command()

    robot.arm.move_to(initial_joint_state['arm_pos'])
    robot.push_command()
    robot.wait_command()

    robot.lift.move_to(initial_joint_state['lift_pos'])
    robot.push_command()
    robot.wait_command()

    robot.end_of_arm.get_joint('stretch_gripper').move_to(initial_joint_state['gripper_pos'])
    robot.push_command()
    robot.wait_command()
        

def main(exposure, noviz=False):

    try:
        pix_per_m_av = None
        pix_per_m_n = 0
        
        camera = dc.D435i(exposure=exposure)

        time.sleep(1.0)
        
        robot = rb.Robot()
        robot.startup()
        move_to_initial_pose(robot)

        marker_info = {}
        with open('aruco_marker_info.yaml') as f:
            marker_info = yaml.load(f, Loader=SafeLoader)

        aruco_detector = ad.ArucoDetector(marker_info=marker_info, show_debug_images=True, use_apriltag_refinement=False, brighten_images=True)

        first_frame = True

        controller = nvc.NormalizedVelocityControl(robot)
        controller.reset_base_odometry()
        
        loop_timer = lt.LoopTimer()

        behaviors = ['look_for_markers', 'rotate_to_starting_pose', 'move_to_predocking_position', 'rotate_for_docking', 'back_into_dock', 'docked']
        behavior = 'look_for_markers'
        facing_dock = None
        left_of_dock = None

        while behavior != 'docked':
            print('_______________________________________')
                            
            loop_timer.start_of_iteration()

            camera_info = camera.get_camera_info()
            pix_per_m = get_pix_per_m(camera_info)
            
            color_image = camera.get_image()
                
            aruco_detector.update(color_image, camera_info)

            markers = aruco_detector.get_detected_marker_dict()
            base_center_xyz = None
            base_midline_xyz = None
            dock_center_xyz = None
            dock_midline_xyz = None
            for k in markers:
                m = markers[k]
                name = m['info']['name']
                if name == 'base_left':
                    base_center_xyz = m['pos'] + ((0.13) * m['x_axis'])
                    base_midline_xyz = -m['y_axis']
                if name == 'docking_station':
                    dock_center_xyz = m['pos']
                    dock_midline_xyz = -m['y_axis']

            # compute and display image-based task-relevant features for visual servoing
            base_center_xy, base_midline_xy = compute_visual_servoing_features(base_center_xyz, base_midline_xyz, camera_info)
            display_visual_servoing_features(base_center_xy, base_midline_xy, color_image)
            
            dock_center_xy, dock_midline_xy = compute_visual_servoing_features(dock_center_xyz, dock_midline_xyz, camera_info)
            display_visual_servoing_features(dock_center_xy, dock_midline_xy, color_image)

            direction_err = 0.0
            distance_err = 0.0
            pan_err = 0.0

            joint_state = controller.get_joint_state()
            # convert base odometry angle to be in the range -pi to pi
            joint_state['base_odom_theta'] = hm.angle_diff_rad(joint_state['base_odom_theta'], 0.0)

            print(f'{behavior=}')
                
            if behavior == 'look_for_markers':
                pan_err = -2.0
                if (base_center_xy is not None) and (base_midline_xy is not None) and (dock_center_xy is not None) and (dock_midline_xy is not None):
                    facing_dock, left_of_dock = docking_pose(base_center_xy, base_midline_xy, dock_center_xy, dock_midline_xy, pix_per_m)
                    print(f'{facing_dock=}')
                    print(f'{left_of_dock=}')
                    if not left_of_dock:
                        raise NotImplementedError('THE DOCKING DEMO DOES NOT YET WORK WHEN DOCKING FROM THE RIGHT SIDE OF THE DOCKING STATION.')
                    if facing_dock:
                        behavior = 'move_to_predocking_position'
                    else: 
                        behavior = 'rotate_to_starting_pose'
            elif behavior == 'back_into_dock':
                battery_charging = joint_state['battery_charging']
                print()
                print(f'{battery_charging=}')
                if battery_charging:
                    print('FINISHED DOCKING!')
                    behavior = 'docked'
                else:
                    distance_err = 100.0
            elif (base_center_xy is not None) and (base_midline_xy is not None) and (dock_center_xy is not None) and (dock_midline_xy is not None):
                pan_goal = dock_center_xy - base_center_xy
                pan_goal = pan_goal / np.linalg.norm(pan_goal)
                pan_curr = np.array([1.0, 0.0])
                pan_err = vector_error(pan_goal, pan_curr)
                if abs(pan_err) < successful_pan_err:
                    pan_err = 0.0

                if behavior == 'rotate_to_starting_pose':
                    center_diff_xy = dock_center_xy - base_center_xy
                    center_diff_xy = center_diff_xy / np.linalg.norm(center_diff_xy)
                                                        
                    if left_of_dock:
                        abs_direction_err = abs(1.0 - np.dot(center_diff_xy, base_midline_xy))
                        direction_err = 1.0
                    else:
                        abs_direction_err = abs(1.0 - np.dot(-center_diff_xy, base_midline_xy))
                        direction_err = -1.0

                    print(f'{abs_direction_err=}')
                    if abs_direction_err < (1.0 - np.cos(np.pi/2.0)):
                        direction_err = 0.0
                        behavior = 'move_to_predocking_position'
                        
                        
                if behavior == 'move_to_predocking_position':
                    pre_docking_center_xy = pre_docking_center(dock_center_xy, dock_midline_xy, pix_per_m)
                    pre_docking_midline_xy = dock_midline_xy

                    display_visual_servoing_features(pre_docking_center_xy, pre_docking_midline_xy, color_image)

                    # find error to the pre-docking waypoint location
                    direction = pre_docking_center_xy - base_center_xy

                    display_visual_servoing_features(base_center_xy, direction, color_image, [0,0,255], 1.0)

                    distance = np.linalg.norm(direction)
                    if distance > 0.0:
                        direction = direction / distance

                    distance_success = False
                    direction_success = False

                    if left_of_dock: 
                        distance_err = distance
                    else:
                        distance_err = -distance
                        
                    if abs(distance_err) < (pix_per_m * successful_pre_docking_err_m):
                        distance_err = 0.0
                        distance_success = True

                    if not distance_success:
                        if left_of_dock: 
                            direction_err = vector_error(direction, base_midline_xy)
                        else:
                            direction_err = vector_error(direction, -base_midline_xy)
                        if (abs(direction_err) < successful_pre_docking_err_ang):
                            direction_err = 0.0
                            direction_success = True
                    else:
                        direction_err = 0.0
                        direction_success = True
                        
                    if direction_success and distance_success:
                        behavior = 'rotate_for_docking'

                elif behavior == 'rotate_for_docking':
                    # find rotational error to make the base midline parallel to the pre-docking direction
                    parallel_err = vector_error(-dock_midline_xy, base_midline_xy)
                    direction_err = 2.0 * parallel_err
                    if abs(direction_err) < successful_rotate_err_ang:
                        direction_err = 0.0
                        behavior = 'back_into_dock'

            print()
            print('visual servoing errors')
            print(f'{direction_err=}')
            print(f'{distance_err=}')
            print(f'{pan_err=}')

            print()
            base_rotational_velocity = direction_err
            if abs(base_rotational_velocity) < min_base_speed:
                base_rotational_velocity = 0.0

            base_translational_velocity = -distance_err
            if abs(base_translational_velocity) < min_base_speed:
                base_translational_velocity = 0.0

            head_pan_velocity = -pan_err

            cmd = {}
            cmd['base_forward'] = base_translational_velocity
            cmd['base_counterclockwise'] = base_rotational_velocity
            cmd['head_pan_counterclockwise'] = head_pan_velocity 

            print('cmd before scaling velocities')
            pp.pprint(cmd)

            cmd = {k: overall_visual_servoing_velocity_scale * v for (k,v) in cmd.items()}
            cmd = {k: joint_visual_servoing_velocity_scale[k] * v for (k,v) in cmd.items()}

            if motion_on:
                print()
                print('cmd before checking joint limits')
                pp.pprint(cmd)
                cmd = { k: ( 0.0 if ((v < 0.0) and (joint_state[vel_cmd_to_pos[k]] < min_joint_state[vel_cmd_to_pos[k]])) else v ) for (k,v) in cmd.items()}
                cmd = { k: ( 0.0 if ((v > 0.0) and (joint_state[vel_cmd_to_pos[k]] > max_joint_state[vel_cmd_to_pos[k]])) else v ) for (k,v) in cmd.items()}

                print()
                print('cmd before being executed')
                pp.pprint(cmd)
                controller.set_command(cmd)

            if not noviz:
                cv2.imshow('Features Used for Visual Servoing', fit_image_to_screen(color_image))
                cv2.waitKey(1)

            loop_timer.end_of_iteration()
            if print_timing: 
                loop_timer.pretty_print()

    finally:

        robot.stop()

if __name__ == '__main__':
    
    parser = argparse.ArgumentParser(
        prog='Stretch 3 Docking Demo',
        description='This application provides a demonstration of using visual servoing to autonomously dock with the official Hello Robot docking station.')

    parser.add_argument('-e', '--exposure', action='store', type=str, default='auto', help=f'Set the D435 exposure to {dh.exposure_keywords} or an integer in the range {dh.exposure_range}') 
    parser.add_argument('-n', '--noviz', nargs='?', default=False, const=True)

    args = parser.parse_args()
    exposure = args.exposure
    noviz = args.noviz

    if not dh.exposure_argument_is_valid(exposure):
        raise argparse.ArgumentTypeError(f'The provided exposure setting, {exposure}, is not a valide keyword, {dh.exposure_keywords}, or is outside of the allowed numeric range, {dh.exposure_range}.')    
    
    main(exposure, noviz)
