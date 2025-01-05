"""Philips Hue routines that make my lights better."""

import argparse
import asyncio
import contextlib
import logging
from datetime import datetime, timedelta

import requests
from aiohue import HueBridgeV2
from aiohue.v2.models.button import ButtonEvent
from aiohue.v2.models.contact import ContactState
from aiohue.v2.models.grouped_light import GroupedLight
from aiohue.v2.models.resource import ResourceTypes
from aiohue.v2.models.room import Room
from aiohue.v2.models.zone import Zone
from pytz import timezone

from custom_holidays import CustomHolidays
from hue_config import *

parser = argparse.ArgumentParser(description="Hue Routines")
parser.add_argument("--debug", help="enable debug logging", action="store_true")
args = parser.parse_args()

bridge = HueBridgeV2(bridge_ip, hue_app_key)

room_name_to_type_map = None
room_type_zone = "zone"
room_type_room = "room"

room_name_to_id_map = None
room_name_to_grouped_light_id_map = None

weather_group_name = "weather"
weather_group_id = None
weather_id = None
weather_scene_map = None
temp_sensor_map = None

hour_min_format = "%H:%M"

rooms_to_time_scenes_map = None
rooms_to_time_scene_datetimes_sorted_map = None

scene_start_time_sunset = "Sunset"
sunset_datetime = None
last_fetched_sunset_time = None

# {motion_id: [room_name, off_time_seconds, optional_contact_sensor_id]}
motion_id_to_room_map = None
# {motion_id: scheduled_off_datetime}
motion_room_scheduled_off_time_map = None

# {button_id: [room_name, device_name, button_control_id]}
button_id_to_room_map = None

# change if you want to use different state-specific holidays
holiday_state = "NY"
holiday_group_id = None
holiday_id = None
holiday_scene_map = dict()
holiday_last_on_datetime = None
us_and_state_holidays = CustomHolidays(subdiv=holiday_state, observed=False)

holiday_zone_name = "holiday"

# weather temperature scene names
weather_temp_colder_scene = "colder"
weather_temp_same_scene = "same"
weather_temp_hotter_scene = "hotter"
weather_temp_freezing_scene = "freezing"

# DEFAULTS #
# You can adjust these variables in a separate hue_config.py file by creating a variable
# in that file with the same name but without '_default` at the end.

# this is the fallback sunset time if we can't get sunset data
# 8pm
fallback_sunset_hour_default = 20
fallback_sunset_minute_default = 00

my_timezone_default = "US/Eastern"

weather_update_time_secs_default = 60 * 5  # minutes
weather_transition_time_ms_default = 1000 * 3  # seconds

# temperate range to determine outside temp difference
weather_temp_diff_range_default = 5  # degrees Fahrenheit

holiday_scene_check_interval_hours_default = 1


async def main():
    """
    Run it.
    First make a separate hue_config.py file with your own variables/secrets.
    """
    if args.debug:
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)-15s %(levelname)-5s %(name)s -- %(message)s",
        )

    async with HueBridgeV2(bridge_ip, hue_app_key) as b:
        global bridge

        # check if certain features are enabled in hue_config.py
        scheduled_scene_change_rooms = find_hue_config_var("scheduled_scene_change_rooms")

        global weather_group_id

        bridge = b
        logging.debug(f"Connected to bridge: {bridge.bridge_id}")
        logging.debug("--- RUNNING SETUP ---")

        update_vars(bridge)

        # run all routines in background continuously
        async with asyncio.TaskGroup() as tg:
            logging.debug("--- Creating configured routines ---")
            # routine to update program variables every so often
            logging.debug("Creating variable update routine")
            tg.create_task(update_variables_routine(bridge))

            if holiday_group_id:
                logging.debug("Creating subscriber for holiday lights")
                logging.debug("-----------------------------------")
                logging.debug("Holidays you can create scenes for:")
                us_and_state_holidays.get(get_current_datetime().strftime('%Y-%m-%d'))
                for date, holiday in sorted(us_and_state_holidays.items()):
                    logging.debug(f"{date} - {holiday}")
                logging.debug("-----------------------------------")
                # routine to change lights on holidays
                bridge.groups.subscribe(callback=holiday_subscriber, id_filter=holiday_group_id)
            if weather_group_id:
                # routine to change lights depending on weather
                logging.debug("Creating weather light routine")
                tg.create_task(weather_light_routine(bridge))
            if scheduled_scene_change_rooms:
                # routine to switch over scenes at certain times when lights are on
                logging.debug("Creating schedules routine for configured rooms")
                tg.create_task(schedules_routine(bridge, scheduled_scene_change_rooms))
            if motion_id_to_room_map:
                # routine to turn off lights in motion rooms
                logging.debug("Creating on/off routines for motion sensors")
                tg.create_task(motion_room_off_routine(bridge))
                for key_motion_id in motion_id_to_room_map:
                    # routine to turn on time based scenes in motion rooms
                    bridge.sensors.motion.subscribe(callback=motion_time_based_subscriber,
                                                    id_filter=key_motion_id)
            if button_id_to_room_map:
                # routine to turn on time based scenes with a button/switch
                logging.debug("Creating subscribers for configured switches to activate time-based scenes ")
                for key_button_id in button_id_to_room_map:
                    bridge.sensors.button.subscribe(callback=button_time_based_subscriber,
                                                    id_filter=key_button_id)
            logging.debug("--- SETUP DONE ---")


def find_hue_config_var(var_name: str):
    """
    Tries to find variable by the supplied name in a separate config file and returns the value if found.
    If not found, returns None.
    :param var_name: the variable name to look for
    :return: the value of the variable if found, otherwise returns None
    """
    var = None
    if var_name in globals():
        var = globals()[var_name]
    return var


async def update_variables_routine(bridge):
    while True:
        await asyncio.sleep(60 * 15)  # update every 15 mins
        update_vars(bridge)


def update_vars(bridge):
    try:
        update_weather_vars(bridge)
        update_holiday_vars(bridge)
        update_time_based_scene_map_vars(bridge)
        update_motion_time_based_vars(bridge)
        update_button_time_based_vars(bridge)
        update_room_id_map(bridge)

    except Exception as ex:
        logging.debug(msg=f"error updating global variables", exc_info=ex)


def update_room_id_map(bridge):
    global room_name_to_id_map
    global room_name_to_grouped_light_id_map
    try:
        room_name_to_id_map = {}
        room_name_to_grouped_light_id_map = {}
        for room in bridge.groups.room:
            room_name = normalize_string(room.metadata.name)
            room_name_to_grouped_light_id_map[room_name] = room.grouped_light
            room_name_to_id_map[room_name] = room.id
        for room in bridge.groups.zone:
            room_name = normalize_string(room.metadata.name)
            room_name_to_grouped_light_id_map[room_name] = room.grouped_light
            room_name_to_id_map[room_name] = room.id

    except Exception as ex:
        logging.debug(msg=f"error updating room id map", exc_info=ex)


def update_time_based_scene_map_vars(bridge):
    global room_name_to_type_map
    global rooms_to_time_scenes_map
    global rooms_to_time_scene_datetimes_sorted_map

    room_name_to_type_map = {}
    rooms_to_time_scenes_map = {}
    rooms_to_time_scene_datetimes_sorted_map = {}

    my_timezone = find_hue_config_var("my_timezone")
    if not my_timezone:
        my_timezone = my_timezone_default

    for group in bridge.groups:
        # setup auto time-based scenes for room
        if isinstance(group, Zone):
            room_type = room_type_zone
        elif isinstance(group, Room):
            room_type = room_type_room
        else:
            room_type = None
        if not room_type:
            continue

        room_name = normalize_string(group.metadata.name)
        room_name_to_type_map[room_name] = room_type

        room_time_scenes_map = {}
        group_id = group.id
        if room_type == room_type_zone:
            scenes = bridge.groups.zone.get_scenes(group_id)
        else:
            # must be room type and not zone
            scenes = bridge.groups.room.get_scenes(group_id)
        for scene in scenes:
            scene_name = scene.metadata.name
            add_scene_to_time_map(room_time_scenes_map, scene_name, scene.id)

        if room_time_scenes_map is not None and len(room_time_scenes_map) != 0:
            # setup sorted scene datetimes to be used for time-based scenes
            current_datetime = get_current_datetime()
            room_scene_datetimes_sorted = []
            tz = timezone(my_timezone)
            for scene_time in room_time_scenes_map:
                scene_datetime = (datetime.strptime(scene_time, hour_min_format)
                                  .replace(year=current_datetime.year,
                                           month=current_datetime.month,
                                           day=current_datetime.day))
                scene_datetime = tz.localize(scene_datetime)
                room_scene_datetimes_sorted.append(scene_datetime)
            room_scene_datetimes_sorted.sort(reverse=True)

            # set time based scenes for room in global map
            rooms_to_time_scenes_map[room_name] = room_time_scenes_map
            rooms_to_time_scene_datetimes_sorted_map[room_name] = room_scene_datetimes_sorted
    logging.debug(f"updated rooms_to_time_scene_datetimes_sorted_map: {rooms_to_time_scene_datetimes_sorted_map}")


def update_button_time_based_vars(bridge):
    global button_id_to_room_map

    try:
        button_time_based_rooms = find_hue_config_var("button_time_based_rooms")
        if button_time_based_rooms:
            button_id_to_room_map = {}
            for button_config in button_time_based_rooms:
                room_name = normalize_string(button_config[0])
                device_name = normalize_string(button_config[1])
                button_control_id = button_config[2]
                for device in bridge.devices:
                    if device.metadata and device.metadata.name and normalize_string(
                            device.metadata.name) == device_name:
                        for resource in device.services:
                            if resource.rtype == ResourceTypes.BUTTON:
                                button = bridge.sensors.button.get(id=resource.rid)
                                if button.metadata.control_id == button_control_id:
                                    button_id = button.id
                                    button_id_to_room_map[button_id] = [room_name, device_name, button_control_id]
                        break

            logging.debug(f"updated button_id_to_room_map: {button_id_to_room_map}")

    except Exception as ex:
        logging.debug(msg=f"error updating motion time based variables", exc_info=ex)


def update_motion_time_based_vars(bridge):
    global motion_id_to_room_map
    global motion_room_scheduled_off_time_map

    try:
        motion_time_based_rooms = find_hue_config_var("motion_time_based_rooms")
        if motion_time_based_rooms:
            motion_id_to_room_map = {}
            if not motion_room_scheduled_off_time_map:
                # instantiate if not instantiated
                motion_room_scheduled_off_time_map = {}
            for motion_config in motion_time_based_rooms:
                room_name = normalize_string(motion_config[0])
                room_off_time_seconds = motion_config[1]
                motion_id = None
                optional_contact_id = None

                for motion_sensor in bridge.sensors.motion:
                    sensor_name = normalize_string(bridge.sensors.get_device(id=motion_sensor.id).metadata.name)
                    if room_name in sensor_name:
                        motion_id = motion_sensor.id
                        break
                if not motion_id:
                    logging.debug(f"error: could not find expected motion sensor named for {room_name}")
                    continue

                for contact_sensor in bridge.sensors.contact:
                    contact_sensor_name = normalize_string(
                        bridge.sensors.get_device(id=contact_sensor.id).metadata.name)
                    if room_name in contact_sensor_name:
                        logging.debug(f"found contact sensor [{contact_sensor_name}] to use for {room_name}")
                        optional_contact_id = contact_sensor.id
                        break

                motion_room_info = [room_name, room_off_time_seconds]
                if optional_contact_id:
                    motion_room_info.append(optional_contact_id)

                motion_id_to_room_map[motion_id] = motion_room_info

    except Exception as ex:
        logging.debug(msg=f"error updating motion time based variables", exc_info=ex)


def update_holiday_vars(bridge):
    global holiday_group_id
    global holiday_id

    try:
        for group in bridge.groups:
            if isinstance(group, Zone):
                if normalize_string(group.metadata.name) == normalize_string(holiday_zone_name):
                    holiday_group_id = group.grouped_light
                    holiday_id = group.id
                    break

    except Exception as ex:
        logging.debug(msg=f"error updating holiday variables", exc_info=ex)


def update_weather_vars(bridge):
    global weather_group_id
    global weather_id
    global weather_scene_map
    global weather_group_name
    global temp_sensor_map

    try:
        for group in bridge.groups:
            if isinstance(group, Zone):
                if normalize_string(group.metadata.name) == weather_group_name:
                    weather_group_id = group.grouped_light
                    weather_id = group.id
                    break

        if not weather_group_id or not weather_id:
            return

        weather_scene_map = dict()
        for scene in bridge.groups.zone.get_scenes(weather_id):
            scene_name = normalize_string(scene.metadata.name)
            scene_id = scene.id

            weather_scene_map[scene_name] = scene_id

        logging.debug(f"weather_scene_map: {weather_scene_map}")

        for temp_sensor in bridge.sensors.temperature:
            if not temp_sensor.enabled:
                continue
            if temp_sensor_map is None:
                temp_sensor_map = {}
            temp_sensor_device = bridge.devices.get(id=temp_sensor.owner.rid)
            if temp_sensor_device.metadata and temp_sensor_device.metadata.name:
                temp_name = normalize_string(temp_sensor_device.metadata.name)
                temp_sensor_map[temp_name] = temp_sensor.id

        if temp_sensor_map:
            logging.debug(f"temp_sensor_map: {temp_sensor_map}")


    except Exception as ex:
        logging.debug(msg=f"error updating weather variables", exc_info=ex)
        return


def add_scene_to_time_map(time_scenes_map, scene_name, scene_id):
    try:
        # Example scene names with time: "Evening (8pm)", "Evening (Sunset + 30m)"
        # time in parentheses will be used as scene start time
        name_parts = scene_name.split("(")
        if len(name_parts) > 1:
            scene_start_time = normalize_string(name_parts[1].split(")")[0])
            if normalize_string(scene_start_time_sunset) in scene_start_time:
                # start time in scene name uses sunset offset time
                scene_start_datetime = parse_sunset_offset_time_from_scene_name(scene_start_time)
            else:
                # start time in scene name is in hour:min am/pm format
                normalized_scene_start_time = normalize_am_pm_time(scene_start_time)
                scene_start_datetime = datetime.strptime(normalized_scene_start_time, "%I:%M %p")
            logging.debug(f"scene_name: {scene_name}, scene_start_datetime: {scene_start_datetime}")

            # map format: { scene start time -> scene id }
            time_string = scene_start_datetime.strftime(hour_min_format)
            time_scenes_map[time_string] = scene_id
    except Exception as ex:
        logging.debug(msg=f"error parsing scene name:{scene_name} when adding to time scenes map", exc_info=ex)
        return


def parse_sunset_offset_time_from_scene_name(scene_start_time: str):
    scene_start_datetime = get_sunset_time()
    if len(scene_start_time) == len(scene_start_time_sunset):
        # start time is just "sunset"
        return scene_start_datetime

    # get offset from sunset time in scene name
    is_positive_offset = True
    positive_offset = scene_start_time.split("+")

    if len(positive_offset) > 1:
        offset = positive_offset[1]
    else:
        negative_offset = scene_start_time.split("-")
        if len(negative_offset) > 1:
            offset = negative_offset[1]
            is_positive_offset = False
        else:
            raise Exception(f"scene_start_time: '{scene_start_time}' does not contain + or -")

    index = 0
    while index < len(offset):
        if offset[index] == "h" or offset[index] == "m":
            break
        index += 1
    if index == 0 or index == len(offset):
        raise Exception(f"could not find time unit 'h' or 'm' in offset: {offset}")

    offset_amount = int(offset[:index])
    if not is_positive_offset:
        offset_amount = -offset_amount
    if offset[index] == "h":
        scene_start_datetime = scene_start_datetime + timedelta(hours=offset_amount)
    else:
        # offset has 'm' so is in minutes
        scene_start_datetime = scene_start_datetime + timedelta(minutes=offset_amount)

    return scene_start_datetime


def normalize_am_pm_time(time_string):
    time_string = normalize_string(time_string)
    time_parts = time_string.split("a")
    time_is_am = False
    if len(time_parts) > 1:
        time_is_am = True
        time_string = time_parts[0]
    else:
        time_string = time_string.split("p")[0]

    split_on_colon = time_string.split(":")
    if len(split_on_colon) == 1:
        time_string = time_string + ":00"
    if len(time_string) == 4:
        time_string = "0" + time_string
    time_string = time_string + " "
    if time_is_am:
        time_string = time_string + "AM"
    else:
        time_string = time_string + "PM"

    return time_string


def update_holiday_scenes():
    global holiday_scene_map
    holiday_scene_map = dict()
    for scene in bridge.groups.zone.get_scenes(holiday_id):
        scene_name = normalize_holiday_name(scene.metadata.name)
        holiday_scene_map[scene_name] = scene.id
    return holiday_scene_map


def discover_scenes_in_zone(zone_id):
    scene_map = dict()
    for scene in bridge.groups.zone.get_scenes(zone_id):
        scene_name = normalize_string(scene.metadata.name)
        scene_map[scene_name] = scene.id
    return scene_map


async def button_time_based_subscriber(event_type, item):
    try:
        global button_id_to_room_map
        if item.button.button_report.event == ButtonEvent.INITIAL_PRESS:
            logging.debug(f"button initial press: {item}")
            button_id = item.id
            button_config = button_id_to_room_map[button_id]
            room_name = button_config[0]
            # device_name = button_config[1]
            # button_control_id = button_config[2]
            room_group_id = room_name_to_grouped_light_id_map[room_name]
            grouped_light = bridge.groups.grouped_light.get(id=room_group_id)

            if grouped_light.on.on:
                # light is on, button press turns off
                await bridge.groups.grouped_light.set_state(id=room_group_id, on=False)
            else:
                # light is off, button press turns on to time-based scene
                logging.debug(f"button press in {room_name} when lights are off, turning lights on")
                await turn_on_room_to_time_based_scene(room_name=room_name, room_group_id=room_group_id)

    except Exception as ex:
        logging.debug(msg=f"error processing event for time-based button press", exc_info=ex)


async def turn_on_room_to_time_based_scene(room_name: str, room_group_id: str):
    scene_id = find_time_based_scene_for_current_time(room_name)
    if scene_id:
        await bridge.scenes.recall(scene_id)
    else:
        await bridge.groups.grouped_light.set_state(id=room_group_id, on=True)


def find_time_based_scene_for_current_time(room_name: str):
    room_time_scenes_map = rooms_to_time_scenes_map[room_name]
    room_scene_datetimes_sorted = rooms_to_time_scene_datetimes_sorted_map[room_name]
    if room_time_scenes_map is None or room_scene_datetimes_sorted is None:
        logging.debug(f"could not find time based scenes for {room_name}, "
                      f"room_time_scenes_map: {room_time_scenes_map}, "
                      f"room_scene_datetimes_sorted:{room_scene_datetimes_sorted}")
        return None

    current_datetime = get_current_datetime()
    datetime_after = room_scene_datetimes_sorted[0]
    logging.debug(f"{room_name} default datetime_after: {datetime_after}")
    logging.debug(f"{room_name} current_datetime to compare to sorted scene times: {current_datetime}")
    for scene_datetime in room_scene_datetimes_sorted:
        if current_datetime >= scene_datetime:
            datetime_after = scene_datetime
            logging.debug(f"{room_name} found new datetime_after: {datetime_after}")
            break

    datetime_after_string = datetime_after.strftime(hour_min_format)
    logging.debug(f"{room_name} datetime_after_string: {datetime_after_string}")
    scene_id = room_time_scenes_map.get(datetime_after_string)
    if not scene_id:
        logging.debug(f"could not find scene_id for datetime_after_string: {datetime_after_string}, "
                      f"in {room_name}"
                      f"room_time_scenes_map: {room_time_scenes_map}, "
                      f"room_scene_datetimes_sorted:{room_scene_datetimes_sorted}")
    return scene_id


async def motion_time_based_subscriber(event_type, item):
    try:
        global motion_id_to_room_map
        global room_name_to_grouped_light_id_map
        if item.motion.motion:
            motion_id = item.id
            motion_config = motion_id_to_room_map[motion_id]
            room_name = motion_config[0]
            off_time_seconds = motion_config[1]

            schedule_motion_lights_off_time(motion_id, off_time_seconds)

            room_group_id = room_name_to_grouped_light_id_map[room_name]
            grouped_light = bridge.groups.grouped_light.get(id=room_group_id)
            if not grouped_light.on.on:
                # motion while lights are off, turn them on
                logging.debug(f"detected motion in {room_name} when lights are off, turning lights on")
                await turn_on_room_to_time_based_scene(room_name=room_name, room_group_id=room_group_id)

    except Exception as ex:
        logging.debug(msg=f"error processing event for time-based motion", exc_info=ex)


def schedule_motion_lights_off_time(motion_id: str, off_time_seconds: int):
    try:
        global motion_room_scheduled_off_time_map
        if not motion_room_scheduled_off_time_map:
            motion_room_scheduled_off_time_map = {}

        current_datetime = get_current_datetime()
        scheduled_off_datetime = current_datetime + timedelta(seconds=off_time_seconds)

        motion_room_scheduled_off_time_map[motion_id] = scheduled_off_datetime

    except Exception as ex:
        logging.debug(msg=f"error scheduling next lights off time for motion sensor", exc_info=ex)


async def holiday_subscriber(event_type, item):
    try:
        if (isinstance(item, GroupedLight)
                and item.id == holiday_group_id
                and item.on.on is True):

            current_datetime = get_current_datetime()
            global holiday_last_on_datetime

            holiday_scene_check_interval_hours = find_hue_config_var("holiday_scene_check_interval_hours")
            if not holiday_scene_check_interval_hours:
                holiday_scene_check_interval_hours = holiday_scene_check_interval_hours_default

            if (holiday_last_on_datetime is None
                    or holiday_last_on_datetime <= current_datetime - timedelta(
                        hours=holiday_scene_check_interval_hours)):

                update_holiday_scenes()

                current_date = current_datetime.strftime('%Y-%m-%d')
                holiday = us_and_state_holidays.get(current_date)

                if holiday is not None:
                    logging.debug(f"it's a holiday! {holiday}")
                    normalized_holiday_name = normalize_holiday_name(holiday)
                    scene_id = holiday_scene_map.get(normalized_holiday_name)
                    if scene_id is not None:
                        prev_brightness = item.dimming.brightness
                        await bridge.scenes.recall(id=scene_id, brightness=prev_brightness)

            holiday_last_on_datetime = current_datetime

    except Exception as ex:
        logging.debug(msg=f"error activating holiday lights", exc_info=ex)


# change my light depending on weather
async def weather_light_routine(bridge):
    global weather_group_name
    global weather_group_id
    global weather_id
    global weather_scene_map

    # run routine
    while True:
        try:
            if not weather_scene_map:
                return

            weather_update_time_secs = find_hue_config_var("weather_update_time_secs")
            if not weather_update_time_secs:
                weather_update_time_secs = weather_update_time_secs_default

            weather_temp_diff_range = find_hue_config_var("weather_temp_diff_range")
            if not weather_temp_diff_range:
                weather_temp_diff_range = weather_temp_diff_range_default

            weather_transition_time_ms = find_hue_config_var("weather_transition_time_ms")
            if not weather_transition_time_ms:
                weather_transition_time_ms = weather_transition_time_ms_default

            default_scene_id = weather_scene_map.get("default")

            # if weather scene isn't on, don't do anything
            weather_zone_state = bridge.groups.grouped_light.get(weather_group_id)
            weather_zone_is_on = weather_zone_state.on.on
            logging.debug(f"weather_zone_is_on: {weather_zone_is_on}")

            if weather_zone_is_on:
                prev_weather_zone_brightness = weather_zone_state.dimming.brightness

                weather_api_response = call_weather_api()
                parse_sunset_time_and_update(weather_api_response)

                cur_weather = normalize_string(str(weather_api_response.json().get("weather")[0].get("main")))
                logging.debug(f"current weather: {cur_weather}")

                # animate lights for inside/outside temp difference
                try:
                    inside_temp = get_inside_temp_in_f(bridge)
                    # feels like temp
                    outside_temp = weather_api_response.json().get("main").get("feels_like")
                    logging.debug(f"inside temp: {inside_temp}")
                    logging.debug(f"outside temp: {outside_temp}")

                    upper_range = inside_temp + weather_temp_diff_range
                    lower_range = inside_temp - weather_temp_diff_range
                    freezing_temp = 32
                    # check if there is a freezing scene first
                    if outside_temp <= freezing_temp and weather_scene_map.get(weather_temp_freezing_scene) is not None:
                        logging.debug(f"outside temp is lower than freezing_temp: {freezing_temp}")
                        temp_scene = weather_temp_freezing_scene
                    elif outside_temp < lower_range:
                        logging.debug(f"outside temp is lower than {lower_range} degrees")
                        temp_scene = weather_temp_colder_scene
                    elif outside_temp > upper_range:
                        logging.debug(f"outside temp is higher than {upper_range} degrees")
                        temp_scene = weather_temp_hotter_scene
                    else:
                        # outside temp close to inside
                        logging.debug(f"outside temp is close to inside temp")
                        temp_scene = weather_temp_same_scene

                    temp_scene_id = weather_scene_map.get(temp_scene)
                    if temp_scene_id is None:
                        raise Exception(f"could not find scene named '{temp_scene}'")

                    # show color for temp diff
                    await bridge.scenes.recall(temp_scene_id,
                                               duration=weather_transition_time_ms,
                                               brightness=prev_weather_zone_brightness)
                    await asyncio.sleep(10)

                except Exception as ex:
                    logging.debug(msg=f"error changing light for inside/outside temp difference", exc_info=ex)

                # change to scene for current weather
                scene_id = weather_scene_map.get(cur_weather)
                if scene_id is None:
                    logging.debug(f"no scene named '{cur_weather}' in weather scene map")
                    if default_scene_id is not None:
                        scene_id = default_scene_id

                # if scene_id was found and weather zone is still on
                if scene_id is not None and bridge.groups.grouped_light.get(weather_group_id).on.on:
                    # refetch current light brightness in case it was changed in the meantime
                    prev_weather_zone_brightness = bridge.groups.grouped_light.get(weather_group_id).dimming.brightness
                    # turn on correct weather scene
                    await bridge.scenes.recall(scene_id,
                                               duration=weather_transition_time_ms,
                                               brightness=prev_weather_zone_brightness)
                else:
                    logging.debug(f"no scene named default in weather scene map, not changing weather light")

        except Exception as ex:
            logging.debug(msg=f"error changing weather light", exc_info=ex)

        await asyncio.sleep(weather_update_time_secs)


def get_inside_temp_in_f(bridge):
    global temp_sensor_map

    if not temp_sensor_map:
        raise Exception("temp_sensor_map is empty")

    temperature_difference_sensor_name = find_hue_config_var("temperature_difference_sensor_name")
    if temperature_difference_sensor_name:
        temperature_difference_sensor_name = normalize_string(temperature_difference_sensor_name)

    temp_to_return = None

    for temp_name, temp_id in temp_sensor_map.items():
        sensor_temp_obj = bridge.sensors.temperature.get(temp_id)
        sensor_temp_f = celsius_to_fahrenheit(sensor_temp_obj.temperature.temperature)
        logging.debug(f"{temp_name} temp: {sensor_temp_f}"
                      f" - time: {sensor_temp_obj.temperature.temperature_report.changed}")
        if not temperature_difference_sensor_name or temperature_difference_sensor_name == temp_name:
            temp_to_return = sensor_temp_f

    return temp_to_return


def celsius_to_fahrenheit(temp_celsius: float) -> float:
    return (temp_celsius * 1.8) + 32


# change the lights over to a new scene at certain times (only if they are currently on)
# so your lights won't turn on when you're not home :)
# the hue app doesn't let you make a routine to switch to a scene only if those lights are on :(
# and custom apps people have built that do it cost money :(
async def change_zone_scene_at_time_if_lights_on(bridge, time, room_name, room_group_id, scene_id):
    try:
        group_state = bridge.groups.grouped_light.get(room_group_id)
        room_is_on = group_state.on.on

        if room_is_on:
            logging.debug(
                f"time is {time} and lights are on in {room_name} so we're changing the scene")
            await bridge.scenes.recall(scene_id)

    except Exception as ex:
        logging.debug(msg=f"error changing scene in {room_name}", exc_info=ex)
        return


def call_weather_api():
    weather_api_key = find_hue_config_var("weather_api_key")
    if not weather_api_key:
        raise Exception("Trying to call weather api but 'weather_api_key' is undefined, please add variable to "
                        "config file")
    weather_city_name = find_hue_config_var("weather_city_name")
    if not weather_city_name:
        raise Exception("Trying to call weather api but 'weather_city_name' is undefined, please add variable to "
                        "config file")

    response = requests.get(
        "https://api.openweathermap.org/data/2.5/weather"
        f"?q={weather_city_name}"
        f"&appid={weather_api_key}"
        "&units=imperial")
    response.raise_for_status()

    return response


# do stuff at certain times
async def schedules_routine(bridge, input_scheduled_room_names: list):
    # setup
    global rooms_to_time_scenes_map
    global room_name_to_grouped_light_id_map

    # normalize input room names
    scheduled_room_names = []
    for room_name in input_scheduled_room_names:
        scheduled_room_names.append(normalize_string(room_name))

    while True:
        try:
            current_datetime_with_timezone = get_current_datetime()
            current_time = current_datetime_with_timezone.strftime('%H:%M')

            for room_name in scheduled_room_names:
                try:
                    room_time_scenes_map = rooms_to_time_scenes_map[room_name]
                    scene_id_for_current_time = room_time_scenes_map.get(current_time)
                    if scene_id_for_current_time is not None:
                        room_group_id = room_name_to_grouped_light_id_map[room_name]
                        await change_zone_scene_at_time_if_lights_on(
                            bridge,
                            time=current_time,
                            room_name=room_name,
                            room_group_id=room_group_id,
                            scene_id=scene_id_for_current_time)
                except Exception as ex:
                    logging.debug(msg=f"error checking {room_name} in schedules routine", exc_info=ex)

        except Exception as ex:
            logging.debug(msg=f"error running schedules", exc_info=ex)

        await asyncio.sleep(60)


def get_current_datetime():
    my_timezone = find_hue_config_var("my_timezone")
    if not my_timezone:
        my_timezone = my_timezone_default

    current_time = datetime.now(timezone(my_timezone))
    # uncomment for testing
    # return datetime.strptime("5:12 pm", "%I:%M %p").replace(year=current_time.year, day=current_time.day, month=current_time.month)
    return current_time


def get_sunset_time():
    global sunset_datetime
    fallback_sunset_hour = find_hue_config_var("fallback_sunset_hour")
    if not fallback_sunset_hour:
        fallback_sunset_hour = fallback_sunset_hour_default

    fallback_sunset_minute = find_hue_config_var("fallback_sunset_minute")
    if not fallback_sunset_minute:
        fallback_sunset_minute = fallback_sunset_minute_default

    if sunset_datetime is None \
            or sunset_datetime.date() != get_current_datetime().date():
        try:
            fetched_time = fetch_sunset_time_from_api()
            if fetched_time:
                return fetched_time

        except Exception as ex:
            logging.debug(msg=f"ERROR calling api for sunset time, msg: {ex}")

    if sunset_datetime is not None:
        sunset_time = sunset_datetime
    else:
        sunset_time = datetime.today().replace(hour=fallback_sunset_hour,
                                               minute=fallback_sunset_minute)
    return sunset_time


def fetch_sunset_time_from_api():
    api_fetch_interval_mins = 10
    current_time = get_current_datetime()
    global last_fetched_sunset_time

    if (last_fetched_sunset_time is None
            or last_fetched_sunset_time <= current_time - timedelta(minutes=api_fetch_interval_mins)):
        last_fetched_sunset_time = current_time

        weather_api_response = call_weather_api()
        fetched_sunset_time = parse_sunset_time_and_update(weather_api_response)
        if fetched_sunset_time is not None:
            return fetched_sunset_time
        else:
            raise Exception("Error calling weather api/parsing response")
    else:
        # api was already called recently, don't call again
        return None


def parse_sunset_time_and_update(weather_api_response):
    global sunset_datetime
    my_timezone = find_hue_config_var("my_timezone")
    if not my_timezone:
        my_timezone = my_timezone_default

    try:
        if sunset_datetime is None \
                or sunset_datetime.date() != get_current_datetime().date():
            sunset_unix_utc = weather_api_response.json().get("sys").get("sunset")
            sunset_datetime = datetime.fromtimestamp(sunset_unix_utc, timezone(my_timezone))
            logging.debug(f"sunset datetime: {sunset_datetime}")
        return sunset_datetime
    except Exception as ex:
        logging.debug(msg="error parsing sunset from weather api response", exc_info=ex)
        return None


# turn off lights in a room when there is no motion for some time.
# time to turn off lights based on motion is scheduled separately and stored in motion_room_scheduled_off_time_map.
# (if room has a contact/door sensor, lights will only turn off if door is open. if closed, a new off time will
# be scheduled to check for later)
async def motion_room_off_routine(bridge):
    while True:
        try:
            global motion_room_scheduled_off_time_map
            global motion_id_to_room_map
            global room_name_to_grouped_light_id_map
            current_datetime = get_current_datetime()

            if motion_room_scheduled_off_time_map:
                scheduled_off_time_map_copy = dict(motion_room_scheduled_off_time_map)
                for motion_id, scheduled_off_datetime in scheduled_off_time_map_copy.items():

                    motion_config = motion_id_to_room_map[motion_id]
                    room_name = motion_config[0]
                    off_time_seconds = motion_config[1]
                    optional_contact_id = None
                    if 2 < len(motion_config):
                        optional_contact_id = motion_config[2]
                    room_group_id = room_name_to_grouped_light_id_map[room_name]

                    if current_datetime < scheduled_off_datetime:
                        # not scheduled off time yet, pass
                        continue

                    if bridge.sensors.motion.get(motion_id).motion.motion:
                        # there is motion, don't turn lights off and schedule new off time
                        schedule_motion_lights_off_time(motion_id, off_time_seconds)
                        continue

                    if optional_contact_id and bridge.sensors.contact.get(
                            optional_contact_id).contact_report.state == ContactState.CONTACT:
                        # door is closed, don't turn lights off and schedule new off time
                        schedule_motion_lights_off_time(motion_id, off_time_seconds)
                        continue

                    # now turn lights off and remove scheduled off time
                    logging.debug(f"turning {room_name} off since no motion")
                    await bridge.groups.grouped_light.set_state(id=room_group_id, on=False)
                    del motion_room_scheduled_off_time_map[motion_id]

        except Exception as ex:
            logging.debug(msg=f"error checking scheduled times for motion lights off routine", exc_info=ex)

        # run every 3 seconds
        await asyncio.sleep(3)


def get_adjusted_brightness(brightness, brightness_adj):
    result = brightness + brightness_adj
    if result < 0:
        return 0
    if result > 100:
        return 100
    return result


def normalize_holiday_name(holiday):
    new_holiday = normalize_string(holiday).replace("'", "").replace(".", "").replace("day", "")
    return "juneteenth" if new_holiday.startswith("juneteenth") else new_holiday


def normalize_string(input_string: str):
    return input_string.lower().replace(" ", "")


with contextlib.suppress(KeyboardInterrupt):
    asyncio.run(main())
