"""Philips Hue routines that make my lights better."""

import argparse
import asyncio
import contextlib

import logging
from datetime import datetime, timedelta

from aiohue.v2.models.grouped_light import GroupedLight
from aiohue.v2.models.motion import Motion
from pytz import timezone

import requests

from aiohue import HueBridgeV2
from aiohue.v2.models.contact import ContactState
from aiohue.v2.models.room import Room
from aiohue.v2.models.zone import Zone

from custom_holidays import CustomHolidays
from hue_config import *

parser = argparse.ArgumentParser(description="Hue Routines")
parser.add_argument("--debug", help="enable debug logging", action="store_true")
args = parser.parse_args()

# light rules

# # amount of time before the evening scene switchover that it will switch to afternoon scene
# afternoon_evening_offset_minutes = 120
# # this is the time the evening scene will be activated if we can't get sunset data
# # 8:00 pm
# evening_scene_switchover_fallback_hour = 20
# evening_scene_switchover_fallback_minute = 00
# my_timezone = "US/Eastern"
# afternoon_scene_name = "afternoon"
# evening_scene_name = "evening"
#
# bathroom_update_time_secs = 60 * 1  # minutes
#
# weather_update_time_secs = 60 * 5  # minutes
# weather_transition_time_ms = 1000 * 3  # seconds
#
# # display difference in inside/outside temp
# weather_temp_diff_range = 5  # degrees Fahrenheit
# weather_temp_brightness_diff = -20  # change in brightness at beginning of animation
# weather_temp_wait_time_secs = 10  # show temp diff color for this long
# # scene names
# weather_temp_colder_scene = "colder"
# weather_temp_same_scene = "same"
# weather_temp_hotter_scene = "hotter"

sunset_datetime = None
last_fetched_sunset_time = None

holiday_group_id = None
holiday_id = None
holiday_scene_map = dict()
holiday_last_on_datetime = None
us_ny_holidays = CustomHolidays(subdiv='NY', observed=False)

bridge = HueBridgeV2(bridge_ip, hue_app_key)


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
        bridge = b
        logging.debug(f"Connected to bridge: {bridge.bridge_id}")

        setup_variables(bridge)

        bridge.subscribe(holiday_subscriber)
        # bridge.subscribe(bathroom_off_subscriber)

        # run all routines in background continuously
        async with asyncio.TaskGroup() as tg:
            tg.create_task(weather_light_routine(bridge))
            tg.create_task(schedules_routine(bridge))
            tg.create_task(bathroom_auto_light_routine(bridge))


async def holiday_subscriber(event_type, item):
    try:
        if (isinstance(item, GroupedLight)
                and item.id == holiday_group_id
                and item.on.on is True):

            current_datetime = get_current_datetime()
            global holiday_last_on_datetime

            if (holiday_last_on_datetime is None
                    or holiday_last_on_datetime <= current_datetime - timedelta(hours=holiday_scene_interval_hours)):

                update_holiday_scenes()

                current_date = current_datetime.strftime('%Y-%m-%d')
                holiday = us_ny_holidays.get(current_date)

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


def setup_variables(bridge):
    global holiday_group_id
    global holiday_id
    for group in bridge.groups:
        if isinstance(group, Zone):
            if group.metadata.name.lower() == holiday_zone_name:
                holiday_group_id = group.grouped_light
                holiday_id = group.id
                break


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
        scene_name = scene.metadata.name.lower()
        scene_map[scene_name] = scene.id
    return scene_map


# change my light depending on weather
async def weather_light_routine(bridge):
    # setup
    try:
        weather_group_id = ""
        weather_id = ""
        for group in bridge.groups:
            if isinstance(group, Zone):
                if group.metadata.name.lower() == "weather":
                    weather_group_id = group.grouped_light
                    weather_id = group.id
                    break

        weather_scene_map = dict()
        for scene in bridge.groups.zone.get_scenes(weather_id):
            scene_name = scene.metadata.name.lower()
            scene_id = scene.id

            weather_scene_map[scene_name] = scene_id
            logging.debug(f"added '{scene_name}' weather scene to map")
        default_scene_id = weather_scene_map.get("default")

    except Exception as ex:
        logging.debug(msg=f"error setting up weather light routine", exc_info=ex)
        return

    # run routine
    while True:
        try:
            # if weather scene isn't on, don't do anything
            weather_zone_state = bridge.groups.grouped_light.get(weather_group_id)
            weather_zone_is_on = weather_zone_state.on.on
            logging.debug(f"weather_zone_is_on: {weather_zone_is_on}")

            if weather_zone_is_on:
                prev_weather_zone_brightness = weather_zone_state.dimming.brightness
                logging.debug(f"weather_zone_brightness: {prev_weather_zone_brightness}")

                weather_api_response = call_weather_api()
                parse_sunset_time_and_update(weather_api_response)

                cur_weather = str(weather_api_response.json().get("weather")[0].get("main")).lower()
                logging.debug(f"current weather: {cur_weather}")

                # animate lights for inside/outside temp difference
                try:
                    inside_temp = get_inside_temp_in_f(bridge)
                    # feels like temp
                    outside_temp = weather_api_response.json().get("main").get("feels_like")
                    logging.debug(f"outside temp: {outside_temp}")

                    upper_range = inside_temp + weather_temp_diff_range
                    lower_range = inside_temp - weather_temp_diff_range
                    if outside_temp < lower_range:
                        logging.debug(f"outside temp is lower than {lower_range} degrees")
                        temp_scene = weather_temp_colder_scene
                    elif outside_temp > upper_range:
                        logging.debug(f"outside temp is higher than {upper_range} degrees")
                        temp_scene = weather_temp_hotter_scene
                    else:
                        # outside temp close to inside
                        logging.debug(f"outside temp is close to inside temp")
                        temp_scene = weather_temp_same_scene

                    prev_weather_zone_brightness = bridge.groups.grouped_light.get(weather_group_id).dimming.brightness
                    start_brightness = get_adjusted_brightness(brightness=prev_weather_zone_brightness,
                                                               brightness_adj=weather_temp_brightness_diff)
                    temp_scene_id = weather_scene_map.get(temp_scene)
                    if temp_scene_id is None:
                        raise Exception(f"could not find scene named '{temp_scene}'")

                    # show color for temp diff, dim slightly
                    await bridge.scenes.recall(temp_scene_id,
                                               duration=weather_transition_time_ms,
                                               brightness=start_brightness)
                    await asyncio.sleep(1)
                    # bring back to same brightness as before
                    prev_weather_zone_brightness = bridge.groups.grouped_light.get(weather_group_id).dimming.brightness
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

                if scene_id is not None:
                    # turn on correct weather scene and don't change brightness
                    prev_weather_zone_brightness = bridge.groups.grouped_light.get(weather_group_id).dimming.brightness
                    await bridge.scenes.recall(scene_id,
                                               duration=weather_transition_time_ms,
                                               brightness=prev_weather_zone_brightness)
                else:
                    logging.debug(f"no scene named default in weather scene map, not changing weather light")

        except Exception as ex:
            logging.debug(msg=f"error changing weather light", exc_info=ex)

        await asyncio.sleep(weather_update_time_secs)


def get_inside_temp_in_f(bridge):
    # log all temps
    if logging.getLogger().isEnabledFor(logging.DEBUG):
        try:
            front_temp_obj = bridge.sensors.temperature.get(front_temp_id)
            front_temp_f = celsius_to_fahrenheit(front_temp_obj.temperature.temperature)
            logging.debug(f"front temp: {front_temp_f}"
                          f" - time: {front_temp_obj.temperature.temperature_report.changed}")
        except Exception as ex:
            logging.debug(msg=f"error getting front temp", exc_info=ex)

        try:
            bathroom_temp_obj = bridge.sensors.temperature.get(bathroom_temp_id)
            bathroom_temp_f = celsius_to_fahrenheit(bathroom_temp_obj.temperature.temperature)
            logging.debug(f"bathroom temp: {bathroom_temp_f}"
                          f" - time: {bathroom_temp_obj.temperature.temperature_report.changed}")
        except Exception as ex:
            logging.debug(msg=f"error getting bathroom temp", exc_info=ex)

    # return temp from living room
    living_room_temp_obj = bridge.sensors.temperature.get(living_room_temp_id)
    living_room_temp_f = celsius_to_fahrenheit(living_room_temp_obj.temperature.temperature)
    logging.debug(f"living temp: {living_room_temp_f}"
                  f" - time: {living_room_temp_obj.temperature.temperature_report.changed}")

    return living_room_temp_f


def celsius_to_fahrenheit(temp_celsius: float) -> float:
    return (temp_celsius * 1.8) + 32


# change the lights over to an evening scene (only if they are currently on)
# so your lights won't turn on when you're not home :)
# the hue app doesn't let you make a routine to switch to a scene only if those lights are on :(
# and custom apps people have built that do it cost money :(
async def change_zone_scene_at_time_if_lights_on(bridge, time, zone_name, zone_group_id, scene_name, scene_id):
    try:
        logging.debug(
            f"the time is {time} so we're changing the scene to '{scene_name}' in zone '{zone_name}' if lights are on")
        zone_state = bridge.groups.grouped_light.get(zone_group_id)
        zone_is_on = zone_state.on.on
        logging.debug(f"{zone_name} - {scene_name} - zone_is_on: {zone_is_on}")

        if zone_is_on:
            await bridge.scenes.recall(scene_id)

    except Exception as ex:
        logging.debug(msg=f"error changing scene in zone", exc_info=ex)
        return


def call_weather_api():
    response = requests.get(
        "https://api.openweathermap.org/data/2.5/weather"
        f"?q={city_name}"
        f"&appid={weather_api_key}"
        "&units=imperial")
    response.raise_for_status()

    return response


# do stuff at certain times
async def schedules_routine(bridge):
    # setup
    try:
        living_area_group_id = None
        living_area_id = None
        living_area_evening_scene_id = None
        living_area_afternoon_scene_id = None
        living_area_late_night_scene_id = None
        for group in bridge.groups:
            if isinstance(group, Zone):
                if group.metadata.name.lower() == "living area":
                    living_area_group_id = group.grouped_light
                    living_area_id = group.id
                    break

        for scene in bridge.groups.zone.get_scenes(living_area_id):
            scene_name = scene.metadata.name.lower()
            if scene_name == evening_scene_name.lower():
                living_area_evening_scene_id = scene.id
                logging.debug(f"found '{scene_name}' scene for schedules")
            elif scene_name == afternoon_scene_name.lower():
                living_area_afternoon_scene_id = scene.id
                logging.debug(f"found '{scene_name}' scene for schedules")
            elif scene_name == late_night_scene_name.lower():
                living_area_late_night_scene_id = scene.id
                logging.debug(f"found '{scene_name}' scene for schedules")

    except Exception as ex:
        logging.debug(msg=f"error setting up schedules routine", exc_info=ex)
        return

    while True:
        current_datetime_with_timezone = get_current_datetime()
        current_time = current_datetime_with_timezone.strftime('%H:%M')
        logging.debug(f"current_time in {my_timezone}: {current_time}")

        try:
            evening_scene_start_time = get_evening_scene_start_time()
            afternoon_scene_start_time = get_afternoon_scene_start_time(evening_scene_start_time)

            afternoon_switchover_time = afternoon_scene_start_time.strftime('%H:%M')
            evening_switchover_time = evening_scene_start_time.strftime('%H:%M')
            logging.debug(f"afternoon scene switchover time: {afternoon_switchover_time}")
            logging.debug(f"sunset/evening scene switchover time: {evening_switchover_time}")

            if current_time == afternoon_switchover_time:
                await change_zone_scene_at_time_if_lights_on(
                    bridge,
                    time=current_time,
                    zone_name="living area",
                    zone_group_id=living_area_group_id,
                    scene_name=afternoon_scene_name,
                    scene_id=living_area_afternoon_scene_id)

            if current_time == evening_switchover_time:
                await change_zone_scene_at_time_if_lights_on(
                    bridge,
                    time=current_time,
                    zone_name="living area",
                    zone_group_id=living_area_group_id,
                    scene_name=evening_scene_name,
                    scene_id=living_area_evening_scene_id)

            if current_time == late_night_switchover_time:
                await change_zone_scene_at_time_if_lights_on(
                    bridge,
                    time=current_time,
                    zone_name="living area",
                    zone_group_id=living_area_group_id,
                    scene_name=late_night_scene_name,
                    scene_id=living_area_late_night_scene_id)

        except Exception as ex:
            logging.debug(msg=f"error running schedules", exc_info=ex)

        await asyncio.sleep(60)


def get_current_datetime():
    return datetime.now(timezone(my_timezone))


def get_evening_scene_start_time():
    global sunset_datetime
    if sunset_datetime is None \
            or sunset_datetime.date() != get_current_datetime().date():
        try:
            return fetch_sunset_time_from_api() + timedelta(minutes=evening_scene_sunset_offset_minutes)

        except Exception as ex:
            logging.debug(msg="error updating sunset time", exc_info=ex)

    if sunset_datetime is not None:
        start_time = sunset_datetime + timedelta(minutes=evening_scene_sunset_offset_minutes)
    else:
        start_time = datetime.today().replace(hour=evening_scene_switchover_fallback_hour,
                                              minute=evening_scene_switchover_fallback_minute) + \
                     timedelta(minutes=evening_scene_sunset_offset_minutes)
    return start_time


def get_afternoon_scene_start_time(evening_scene_start_time):
    return evening_scene_start_time - timedelta(minutes=afternoon_evening_offset_minutes)


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
        raise Exception(f"Not calling api again, last called time: {last_fetched_sunset_time}")


def parse_sunset_time_and_update(weather_api_response):
    global sunset_datetime
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


async def bathroom_off_subscriber(event_type, item):
    try:
        if isinstance(item, Motion):
            if item.id == bathroom_motion_id and item.motion.motion_report.motion is False:
                bathroom_door_opened = \
                    bridge.sensors.contact.get(
                        bathroom_contact_id).contact_report.state == ContactState.NO_CONTACT

                if bathroom_door_opened:
                    logging.debug("turning bathroom off because no motion")
                    await bridge.groups.grouped_light.set_state(bathroom_group_id, False)
    except Exception as ex:
        logging.debug(msg=f"error checking bathroom motion", exc_info=ex)


# turn off bathroom lights when not needed
async def bathroom_auto_light_routine(bridge):
    # setup
    try:
        bathroom_group_id = ""
        for group in bridge.groups:
            if isinstance(group, Room):
                if group.metadata.name.lower() == "bathroom":
                    bathroom_group_id = group.grouped_light
                    break

    except Exception as ex:
        logging.debug(msg=f"error setting up bathroom light routine", exc_info=ex)
        return

    while True:
        try:
            logging.debug("checking bathroom light state")

            bathroom_group_state = bridge.groups.grouped_light.get(bathroom_group_id)
            bathroom_is_on = bathroom_group_state.on.on
            logging.debug(f"bathroom_is_on: {bathroom_is_on}")

            if bathroom_is_on:

                bathroom_door_opened = \
                    bridge.sensors.contact.get(
                        bathroom_contact_id).contact_report.state == ContactState.NO_CONTACT
                bathroom_no_motion = \
                    bridge.sensors.motion.get(bathroom_motion_id).motion.motion_report.motion is False
                logging.debug(f"bathroom_door_opened: {bathroom_door_opened}")
                logging.debug(f"bathroom_no_motion: {bathroom_no_motion}")

                if bathroom_door_opened and bathroom_no_motion:
                    logging.debug("turning bathroom off")
                    await bridge.groups.grouped_light.set_state(bathroom_group_id, False)

        except Exception as ex:
            logging.debug(msg=f"error checking bathroom lights", exc_info=ex)

        await asyncio.sleep(bathroom_update_time_secs)


def get_adjusted_brightness(brightness, brightness_adj):
    result = brightness + brightness_adj
    if result < 0:
        return 0
    if result > 100:
        return 100
    return result


def normalize_holiday_name(holiday):
    new_holiday = holiday.lower().replace(" ", "").replace("'", "").replace(".", "").replace("day", "")
    return "juneteenth" if new_holiday == "juneteenthnationalindependence" else new_holiday


with contextlib.suppress(KeyboardInterrupt):
    asyncio.run(main())
