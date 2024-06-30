"""Philips Hue routines that make my lights better."""

import argparse
import asyncio
import contextlib
import logging
from datetime import datetime
from pytz import timezone

import requests

from aiohue import HueBridgeV2
from aiohue.v2.models.contact import ContactState
from aiohue.v2.models.room import Room
from aiohue.v2.models.zone import Zone

from hue_config import *

parser = argparse.ArgumentParser(description="Hue Routines")
parser.add_argument("--debug", help="enable debug logging", action="store_true")
args = parser.parse_args()


# light rules

evening_scene_switchover_time = "20:30"  # 8:30 pm

bathroom_update_time_secs = 60 * 1  # minutes

weather_update_time_secs = 60 * 5  # minutes
weather_transition_time_ms = 1000 * 3  # seconds

# display difference in inside/outside temp
weather_temp_diff_range = 3  # degrees fahrenheit
weather_temp_brightness_diff = -20  # change in brightness at beginning of animation
weather_temp_wait_time_secs = 10  # show temp diff color for this long
# scene names
weather_temp_colder_scene = "colder"
weather_temp_same_scene = "same"
weather_temp_hotter_scene = "hotter"


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

    async with HueBridgeV2(bridge_ip, hue_app_key) as bridge:
        logging.debug(f"Connected to bridge: {bridge.bridge_id}")

        # run all routines in background continuously
        async with asyncio.TaskGroup() as tg:
            tg.create_task(weather_light_routine(bridge))
            tg.create_task(schedules_routine(bridge))
            tg.create_task(bathroom_auto_light_routine(bridge))

        # await asyncio.sleep(5)


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

                response = requests.get(
                    "https://api.openweathermap.org/data/2.5/weather"
                    f"?q={city_name}"
                    f"&appid={weather_api_key}"
                    "&units=imperial")
                response.raise_for_status()

                cur_weather = str(response.json().get("weather")[0].get("main")).lower()
                logging.debug(f"current weather: {cur_weather}")

                # animate lights for inside/outside temp difference
                try:
                    inside_temp = get_inside_temp_in_f(bridge)
                    # feels like temp
                    outside_temp = response.json().get("main").get("feels_like")
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

                    start_brightness = prev_weather_zone_brightness + weather_temp_brightness_diff
                    temp_scene_id = weather_scene_map.get(temp_scene)
                    if temp_scene_id is None:
                        raise Exception(f"could not find scene named '{temp_scene}'")

                    # show color for temp diff, dim slightly
                    await bridge.scenes.recall(temp_scene_id,
                                               duration=weather_transition_time_ms,
                                               brightness=start_brightness)
                    await asyncio.sleep(1)
                    # bring back to same brightness as before
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
            f"the time is {time} so we're changing the scene to {scene_name} in zone {zone_name} if lights are on")
        zone_state = bridge.groups.grouped_light.get(zone_group_id)
        zone_is_on = zone_state.on.on
        logging.debug(f"{zone_name} - {scene_name} - zone_is_on: {zone_is_on}")

        if zone_is_on:
            await bridge.scenes.recall(scene_id)

    except Exception as ex:
        logging.debug(msg=f"error changing scene in zone", exc_info=ex)
        return


# do stuff at certain times
async def schedules_routine(bridge):
    # setup
    try:
        living_area_group_id = ""
        living_area_id = ""
        living_area_evening_id = ""
        for group in bridge.groups:
            if isinstance(group, Zone):
                if group.metadata.name.lower() == "living area":
                    living_area_group_id = group.grouped_light
                    living_area_id = group.id
                    break

        for scene in bridge.groups.zone.get_scenes(living_area_id):
            scene_name = scene.metadata.name.lower()
            if scene_name == "evening":
                living_area_evening_id = scene.id
                logging.debug(f"found '{scene_name}' scene for schedules")
                break

    except Exception as ex:
        logging.debug(msg=f"error setting up schedules routine", exc_info=ex)
        return

    while True:
        try:
            my_timezone = "US/Eastern"
            current_datetime_eastern = datetime.now(timezone(my_timezone))
            current_time = current_datetime_eastern.strftime('%H:%M')
            logging.debug(f"current_time in {my_timezone}: {current_time}")

            if current_time == evening_scene_switchover_time:
                await change_zone_scene_at_time_if_lights_on(
                    bridge,
                    time=evening_scene_switchover_time,
                    zone_name="living area",
                    zone_group_id=living_area_group_id,
                    scene_name="evening",
                    scene_id=living_area_evening_id)

        except Exception as ex:
            logging.debug(msg=f"error running schedules", exc_info=ex)

        await asyncio.sleep(60)


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


with contextlib.suppress(KeyboardInterrupt):
    asyncio.run(main())
