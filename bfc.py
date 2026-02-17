#!/bin/python3

'''
Author: FunctionSir
License: AGPLv3
Date: 2026-02-14 23:20:22
LastEditTime: 2026-02-17 18:46:50
LastEditors: FunctionSir
Description: For DELL R730xd with a PERC H730 Mini RAID card.
FilePath: /quieter-r730xd/bfc.py
'''

####################### !!! ATTENTION !!! #######################
# BEFORE YOU START TO USE THIS SCRIPT, CHECK IT AGAIN!          #
# THIS IS ONLY TESTED WITH THE AUTHOR'S OWN DEVICE!             #
# DESIGNED FOR DELL POWEREDGE R730XD WITH PERC H730 MINI ONLY!  #
# YOU HAVE ALREADY BE WARNED!                                   #
#################################################################

import os
import subprocess
import sys
import time
import signal
import threading
import math

### CONFIG HERE ###
DISKS_COUNT = 12
DISKS_DEV = "/dev/sda"
IPMI_USER = "your/user/name"
IPMI_PASSWD_FILE = "your/passwd/file"
IPMI_HOST = "xxx.xxx.xxx.xxx"
OTHER_TEMP_STAGES = [10, 40, 70, 75, 80, 90]
OTHER_FANS_STAGES = [5, 15, 25, 35, 50, 90]
DISKS_TEMP_STAGES = [10, 30, 45, 50, 55, 60]
DISKS_FANS_STAGES = [5, 10, 15, 35, 80, 90]
INIT_REFRESHING_INTERVAL = 30
MIN_REFRESHING_INTERVAL = 5
MAX_REFRESHING_INTERVAL = 65
TEMP_STD_FOR_REFRESHING_INTERVAL = 5
REFRESHING_INTERVAL_STEPPING = 1
TIMES_BEFORE_LOWER_SPEED_CONFIRMED = 2
### END OF CONFIG ###


stop_event = threading.Event()


def get_disk_temp(dev, device):
    """ Get disk temperature.

    Args:
        dev (str): Dev. Example: /dev/sda
        device (str): Device. Example: megaraid,0

    Returns:
        int/None: Disk temperature or nothing found.
    """
    result = subprocess.run(
        ["smartctl", "-A", dev, "-d", device], capture_output=True, text=True, check=False)
    for line in result.stdout.splitlines():
        if "Current Drive Temperature" in line:
            return int(line.split()[-2])
    return None


def ipmi_get_max_temp(user, passwd_file, host):
    """Get max temp from IPMI.

    Args:
        user (str): User for ipmi.
        passwd_file (str): File to read passwd.
        host (str): Host.

    Returns:
        int/None: Max temp or nothing found.
    """
    cmd = ["ipmitool", "-I", "lanplus", "-U", user, "-f", passwd_file,
           "-H", host, "-c", "sdr", "list", "full"]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    max_temp = None
    for line in result.stdout.splitlines():
        if "degrees C" in line:
            this_temp = int(line.split(",")[1])
            if (max_temp is None) or (max_temp < this_temp):
                max_temp = this_temp
    return max_temp


def ipmi_set_fan_speed(user, passwd_file, host, percent_pwm):
    """Set fan speed (as % PWM) using IPMI.

    Args:
        user (str): User for ipmi.
        passwd_file (str): File to read passwd.
        host (str): Host.
        percent_pwm (int): Fan speed as % PWM.

    Returns:
        int: Return val of ipmitool command.
    """
    cmd = f"ipmitool -I lanplus -U {user} -f {passwd_file} -H {host} " + \
        f"raw 0x30 0x30 0x02 0xff {hex(percent_pwm)}"
    return os.system(cmd)


def ipmi_set_auto_fan(user, passwd_file, host, on):
    """Set auto fan speed control on or off using IPMI.

    Args:
        user (str): User for ipmi.
        passwd_file (str): File to read passwd.
        host (str): Host.
        on (bool): True: auto fan speed control ON; False: auto fan speed control OFF.

    Returns:
        int: Return val of ipmitool command.
    """
    cmd = f"ipmitool -I lanplus -U {user} -f {passwd_file} -H {host} " + \
        f"raw 0x30 0x30 0x01 {hex(on)}"
    return os.system(cmd)


def before_exit(x, y):
    """Enable auto fan speed control before exit.
    """
    print(f"[{time.ctime()}] "+"Signal related: "+str(x)+", "+str(y)+".")

    # Set stop_event.
    stop_event.set()

    # Wait for 5 seconds.
    time.sleep(5)

    fail_cnt = 0

    for _ in range(5):
        ret = ipmi_set_auto_fan(IPMI_USER, IPMI_PASSWD_FILE, IPMI_HOST, True)
        if ret != 0:
            fail_cnt += 1
            print(f"[{time.ctime()}] " +
                  "ERR: Can not enable auto fan speed control again! Will retry after 1s.")
            time.sleep(1)
        else:
            break
    if fail_cnt == 5:
        print(f"[{time.ctime()}] " +
              "CRITICAL: Can not enable auto fan speed control again (tried for 5 times)!")
        sys.exit(1)

    print(f"[{time.ctime()}] " +
          "INFO: Set to auto fan speed control, and exit.")
    sys.exit(0)


def calc_target_speed(t1, s1, t2, s2, tcur):
    """Calc target speed use info from two stages.

    Args:
        t1 (int): Temp of stage 1.
        s1 (int): Speed of stage 1.
        t2 (int): Temp of stage 2.
        s2 (int): Speed of stage 2.
        tcur (int): Cur temp.

    Returns:
        int: Target speed.
    """
    k = float(s2-s1)/float(t2-t1)
    b = s2-k*t2
    return int(math.ceil(k*tcur+b))


def main():
    """Function main.
    """
    # 0: Reg signal handlers.
    signal.signal(signal.SIGTERM, before_exit)
    signal.signal(signal.SIGINT, before_exit)

    # 1: Enable auto fan speed control
    ret = ipmi_set_auto_fan(IPMI_USER, IPMI_PASSWD_FILE, IPMI_HOST, True)
    if ret != 0:
        print(f"[{time.ctime()}] ERR: Can not enable auto fan speed control!")
        sys.exit(1)
    auto_flag = True
    last_fan_speed = -1
    refreshing_interval = INIT_REFRESHING_INTERVAL
    last_interval = INIT_REFRESHING_INTERVAL
    last_temp_other = None
    last_temp_disks = None
    got_lower_speed_times = 0

    # 2: Main loop.
    while not stop_event.is_set():
        cur_other_max_temp = -1024
        cur_disks_max_temp = -1024

        # 2.1 Get temp of disks.
        fail_flag = False
        for disk_no in range(DISKS_COUNT):
            this_temp = get_disk_temp(DISKS_DEV, "megaraid,"+str(disk_no))
            if this_temp is None:
                fail_flag = True
                break
            cur_disks_max_temp = max(cur_disks_max_temp, this_temp)

        # 2.2 If any failed, set fan speed to auto.
        if fail_flag:
            print(f"[{time.ctime()}] " +
                  "ERR: Some disk temp can not be read! Set to auto mode!")
            if not auto_flag:
                auto_flag = True
                ipmi_set_auto_fan(IPMI_USER, IPMI_PASSWD_FILE, IPMI_HOST, True)
            time.sleep(refreshing_interval)
            continue

        # 2.3 Get max temp from IPMI.
        this_temp = ipmi_get_max_temp(IPMI_USER, IPMI_PASSWD_FILE, IPMI_HOST)
        if this_temp is not None:
            cur_other_max_temp = max(cur_other_max_temp, this_temp)
        else:  # 2.4 If failed, set fan speed to auto.
            print(f"[{time.ctime()}] " +
                  "ERR: Temp from IPMI can not be read! Set to auto mode!")
            if not auto_flag:
                auto_flag = True
                ipmi_set_auto_fan(IPMI_USER, IPMI_PASSWD_FILE, IPMI_HOST, True)
            time.sleep(refreshing_interval)
            continue

        # 3. Get target fan speed.
        # 3.1 Process others.
        target_speed_others = 100
        for idx, this_stage in enumerate(OTHER_TEMP_STAGES):
            if cur_other_max_temp <= this_stage:
                if idx != 0:
                    target_speed_others = calc_target_speed(
                        OTHER_TEMP_STAGES[idx-1],
                        OTHER_FANS_STAGES[idx-1],
                        OTHER_TEMP_STAGES[idx],
                        OTHER_FANS_STAGES[idx],
                        cur_other_max_temp
                    )
                else:
                    target_speed_others = OTHER_FANS_STAGES[idx]
                break
        # 3.2 Process disks.
        target_speed_disks = 100
        for idx, this_stage in enumerate(DISKS_TEMP_STAGES):
            if cur_disks_max_temp <= this_stage:
                if idx != 0:
                    target_speed_disks = calc_target_speed(
                        DISKS_TEMP_STAGES[idx-1],
                        DISKS_FANS_STAGES[idx-1],
                        DISKS_TEMP_STAGES[idx],
                        DISKS_FANS_STAGES[idx],
                        cur_disks_max_temp
                    )
                else:
                    target_speed_disks = DISKS_FANS_STAGES[idx]
                break
        # 3.3 Get the larger one.
        target_speed = max(target_speed_others, target_speed_disks)

        # 4. Set fan speed.
        if auto_flag:
            ret = ipmi_set_auto_fan(
                IPMI_USER, IPMI_PASSWD_FILE, IPMI_HOST, False)
            if ret != 0:
                print(f"[{time.ctime()}] " +
                      "ERR: Can not disable auto fan speed control!")
                continue
            auto_flag = False
            last_fan_speed = -1

        fan_speed_set_flag = True

        if last_fan_speed != target_speed:
            if last_fan_speed > target_speed:
                if got_lower_speed_times < TIMES_BEFORE_LOWER_SPEED_CONFIRMED:
                    print(f"[{time.ctime()}] " +
                          f"INFO: Lower speed can be set, before/needed: {got_lower_speed_times}/" +
                          f"{TIMES_BEFORE_LOWER_SPEED_CONFIRMED}.")
                    fan_speed_set_flag = False
                got_lower_speed_times += 1
            else:
                got_lower_speed_times = 0

            if fan_speed_set_flag:
                ret = ipmi_set_fan_speed(IPMI_USER, IPMI_PASSWD_FILE, IPMI_HOST,
                                         target_speed)
                if ret != 0:
                    print(f"[{time.ctime()}] " +
                          "ERR: Fan speed can not be set! Set to auto mode!")
                    if not auto_flag:
                        auto_flag = True
                        ipmi_set_auto_fan(IPMI_USER, IPMI_PASSWD_FILE, IPMI_HOST,
                                          True)
                if last_fan_speed != target_speed:
                    print(f"[{time.ctime()}] " +
                          f"INFO: Temp: O: {cur_other_max_temp}, D: {cur_disks_max_temp}.")
                    print(f"[{time.ctime()}] " +
                          f"INFO: Fan speed changed: {last_fan_speed}% -> {target_speed}%.")
                last_fan_speed = target_speed

        new_inertval = refreshing_interval
        if (last_temp_other is not None) and (last_temp_disks is not None):
            delta_other = cur_other_max_temp-last_temp_other
            delta_disks = cur_disks_max_temp-last_temp_disks
            need_lower_inertval = (delta_other > TEMP_STD_FOR_REFRESHING_INTERVAL) or (
                delta_disks > TEMP_STD_FOR_REFRESHING_INTERVAL)
            if need_lower_inertval:
                new_inertval = min(new_inertval,
                                   int(math.floor(refreshing_interval/2)))
            else:
                new_inertval += REFRESHING_INTERVAL_STEPPING

        new_inertval = max(new_inertval, MIN_REFRESHING_INTERVAL)
        new_inertval = min(new_inertval, MAX_REFRESHING_INTERVAL)

        refreshing_interval = new_inertval

        if refreshing_interval != last_interval:
            print(f"[{time.ctime()}] " +
                  f"INFO: Temp: O: {cur_other_max_temp}, D: {cur_disks_max_temp}.")
            print(f"[{time.ctime()}] " +
                  f"INFO: Refreshing interval changed: {last_interval}s -> {refreshing_interval}s.")

        last_temp_other = cur_other_max_temp
        last_temp_disks = cur_disks_max_temp
        last_interval = refreshing_interval

        # 5. Wait for a while.
        time.sleep(refreshing_interval)


# Program entry point.
if __name__ == "__main__":
    main()
