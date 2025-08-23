"""AutoMoLi.
Automatic Motion Lights
@mkotler / https://github.com/mkotler/ad-automoli
"""

from __future__ import annotations

# Automoli can be profiled for performance with line_profiler
# There is a non-documented config variable for rooms "profile: True"
# which will turn on profiling for the lights_on code path.  Add @profile
# to other functions to profile them.
PROFILING_AVAILABLE = True
try:
    from line_profiler import LineProfiler

    profile = LineProfiler()
except ImportError:
    PROFILING_AVAILABLE = False

    def profile(func):
        return func


from collections.abc import Iterable
from copy import deepcopy
from datetime import datetime, date, time, timedelta
from dateutil import tz
from packaging.version import Version
from enum import Enum, IntEnum
from inspect import stack
import logging
from pprint import pformat
from typing import Any
import os


# pylint: disable=import-error
import hassapi as hass
import adbase as ad

__version__ = "0.11.4"

APP_NAME = "AutoMoLi"
APP_ICON = "💡"

ON_ICON = APP_ICON
OFF_ICON = "🌑"
DIM_ICON = "🔜"
DAYTIME_SWITCH_ICON = "⏰"
ALERT_ICON = "⚠️"

# default values
DEFAULT_NAME = "daytime"
DEFAULT_LIGHT_SETTING = 100
DEFAULT_DELAY = 150
DEFAULT_DIM_METHOD = "step"
DEFAULT_DAYTIMES: list[dict[str, str | int]] = [
    dict(starttime="05:30", name="morning", light=25),
    dict(starttime="07:30", name="day", light=100),
    dict(starttime="20:30", name="evening", light=90),
    dict(starttime="22:30", name="night", light=0),
]
DEFAULT_LOGLEVEL = "INFO"
FORCE_LOG_SENSOR = "input_boolean.automoli_force_logging"
DEFAULT_UPDATE_STATS_DELAY = 1
DEFAULT_OVERRIDE_DELAY = 60
DEFAULT_WARNING_DELAY = 60
DEFAULT_COOLDOWN = 30

CONFIG_APPNAME = "default"
EVENT_MOTION_XIAOMI = "xiaomi_aqara.motion"
EVENT_AUTOMOLI_STATS = "automoli_stats"

RANDOMIZE_SEC = 5
SECONDS_PER_MIN: int = 60
DATETIME_FORMAT = "%I:%M:%S%p %Y-%m-%d"
TIME_FORMAT = "%H:%M:%S"

NOT_READY_STATES = {"unavailable", "unknown", "none"}

# Define a unique sentinel value for clear_handles
MISSING = object()


class EntityType(Enum):
    LIGHT = "light."
    MOTION = "binary_sensor.motion_sensor_"
    HUMIDITY = "sensor.humidity_"
    ILLUMINANCE = "sensor.illumination_"
    DOOR_WINDOW = "binary_sensor.door_window_sensor_"

    @property
    def idx(self) -> str:
        return self.name.casefold()

    @property
    def prefix(self) -> str:
        return str(self.value).casefold()


SENSORS_REQUIRED = [EntityType.MOTION.idx]
SENSORS_OPTIONAL = [EntityType.HUMIDITY.idx, EntityType.ILLUMINANCE.idx]

KEYWORDS = {
    EntityType.LIGHT.idx: "light.",
    EntityType.MOTION.idx: "binary_sensor.motion_sensor_",
    EntityType.HUMIDITY.idx: "sensor.humidity_",
    EntityType.ILLUMINANCE.idx: "sensor.illumination_",
    EntityType.DOOR_WINDOW.idx: "binary_sensor.door_window_sensor_",
}


def install_pip_package(
    pkg: str,
    version: str = "",
    install_name: str | None = None,
    pre_release: bool = False,
) -> None:
    import importlib
    import site
    from subprocess import check_call  # nosec
    import sys

    try:
        importlib.import_module(pkg)
    except ImportError:
        install_name = install_name if install_name else pkg
        if pre_release:
            check_call(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--upgrade",
                    "--pre",
                    f"{install_name}{version}",
                ]
            )
        else:
            check_call(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--upgrade",
                    f"{install_name}{version}",
                ]
            )
        importlib.reload(site)
    finally:
        importlib.import_module(pkg)


# install adutils library
install_pip_package("adutils", version=">=0.6.2")
from adutils import Room, hl, natural_time, py38_or_higher, py39_or_higher  # noqa
from adutils import py37_or_higher  # noqa


class DimMethod(IntEnum):
    """IntEnum representing the transition-to-off method used."""

    NONE = 0
    TRANSITION = 1
    STEP = 2


class AutoMoLi(hass.Hass):  # type: ignore
    """Automatic Motion Lights."""

    # A note on logging conventions in this file:  For convenience, calling self.lg with level=logging.DEBUG
    # is a convenient way to prevent logging the message if not currently debugging. You will see, however,
    # a number of cases where calls are preceded with "if logging.DEBUG >= self.loglevel".  This results in a
    # minor performance gain as the f-string will not be evaluated at runtime and is used in code paths where
    # timing is more essential (e.g., turning on a light).
    def lg(
        self,
        msg: str,
        *args: Any,
        level: int | None = None,
        icon: str | None = None,
        repeat: int = 1,
        log_to_ha: bool = False,
        **kwargs: Any,
    ) -> None:
        kwargs.setdefault("ascii_encode", False)

        level = level if level else self.loglevel

        if level >= self.loglevel or self.force_logging:
            message = f"{f'{icon} ' if icon else ''}{msg}"
            if not self.colorize_logging:
                message = message.replace("\033[1m", "").replace("\033[0m", "")
            _ = [self.log(message, *args, **kwargs) for _ in range(repeat)]

            if log_to_ha or self.log_to_ha:
                message = message.replace("\033[1m", "").replace("\033[0m", "")

                # Python community recommend a strategy of
                # "easier to ask for forgiveness than permission"
                # https://stackoverflow.com/a/610923/13180763
                try:
                    ha_name = self.room.name.replace("_", " ").title()
                except AttributeError:
                    ha_name = APP_NAME
                    self.lg(
                        f"{stack()[0][3]} | No room set yet, using 'AutoMoLi' for logging to HA",
                        level=logging.DEBUG,
                    )

                self.call_service(
                    "logbook/log",
                    name=ha_name,  # type:ignore
                    message=message,  # type:ignore
                    entity_id=self.entity_id,  # type:ignore
                )

    # Methods called by run_in can only have kwargs but cannot update self.lg
    # because *args and **kwargs are required for AppDaemon's self.log method
    def lg_delayed(self, kwargs: dict[str, Any] | None = None) -> None:
        kwargs.setdefault("ascii_encode", False)
        msg = kwargs.get("msg", "")
        level = kwargs.get("level", None)
        icon = kwargs.get("icon", None)
        repeat = kwargs.get("repeat", 1)
        log_to_ha = kwargs.get("log_to_ha", False)
        self.lg(
            msg=f"{msg}", level=level, icon=icon, repeat=repeat, log_to_ha=log_to_ha
        )

    # The force_logging_on method is called when FORCE_LOG_SENSOR changes to "on"
    # setting internal state of force_logging variable to save repeated lookups
    def force_logging_on(
        self, entity: str, attribute: str, old: str, new: str, kwargs: dict[str, Any]
    ) -> None:
        self.force_logging = True
        self.log_debug = True

    # The force_logging_off method is called when FORCE_LOG_SENSOR changes to "off"
    # setting internal state of force_logging variable to save repeated lookups
    def force_logging_off(
        self, entity: str, attribute: str, old: str, new: str, kwargs: dict[str, Any]
    ) -> None:
        self.force_logging = False
        self.log_debug = logging.DEBUG >= self.loglevel
        self.run_in(
            self.update_room_stats, DEFAULT_UPDATE_STATS_DELAY, stat="forceLoggingOff"
        )

    # listr will always return a set but can be cast into a list.  This is done
    # in this file because lists provide slight performance gains when only looping
    # through them but sets provide significant performance gains when doing a lookup.
    def listr(
        self,
        list_or_string: list[str] | set[str] | str | Any,
        entities_exist: bool = True,
    ) -> set[str]:
        entity_list: list[str] = []

        if isinstance(list_or_string, str):
            entity_list.append(list_or_string)
        elif isinstance(list_or_string, (list, set)):
            entity_list += list_or_string
        elif list_or_string:
            self.lg(
                f"{list_or_string} is of type {type(list_or_string)} and "
                f"not 'Union[List[str], Set[str], str]'"
            )

        return set(
            filter(self.entity_exists, entity_list) if entities_exist else entity_list
        )

    def getarg(
        self,
        name: str,
        default: Any,
    ) -> Any:
        """Get configuration options from the current app if they exist but if not fall back
        to any options defined in an app named 'default' or worst case to a default value passed in
        """
        if name in self.args:
            return self.args.pop(name)
        elif CONFIG_APPNAME in self.app_config and hasattr(
            self.app_config[CONFIG_APPNAME], name
        ):
            return getattr(self.app_config[CONFIG_APPNAME], name)
        else:
            return default

    def initialize(self) -> None:
        """Initialize a room with AutoMoLi."""

        # pylint: disable=attribute-defined-outside-init

        # do not initialize the app if it has the name == CONFIG_APPNAME,
        # only use it for global config variables
        if self.name == CONFIG_APPNAME:
            return

        self.icon = APP_ICON

        # get a real dict for the configuration
        self.args: dict[str, Any] = dict(self.args)
        # set up listener for state events
        listener: set[Any, Any, Any] = set()

        # initialize logging
        self.loglevel = logging.INFO
        self.colorize_logging = True
        self.log_to_ha = False
        self.force_logging = False

        if bool(self.getarg("debug_log", False)):
            self.loglevel = logging.DEBUG

        self.log_to_ha = bool(self.getarg("log_to_ha", False))

        # Force logging if FORCE_LOG_SENSOR is "on" and then capture any state changes
        self.force_logging = self.get_state(FORCE_LOG_SENSOR, copy=False) == "on"
        listener.add(
            self.listen_state(
                self.force_logging_on,
                entity_id=FORCE_LOG_SENSOR,
                new="on",
            )
        )
        listener.add(
            self.listen_state(
                self.force_logging_off,
                entity_id=FORCE_LOG_SENSOR,
                new="off",
            )
        )

        self.colorize_logging = bool(self.getarg("colorize_logging", True))
        self.log_debug = (logging.DEBUG >= self.loglevel) or self.force_logging

        self.lg(
            f"{stack()[0][3]} | Setting log level to {logging.getLevelName(self.loglevel)}",
            level=logging.DEBUG,
        )

        # python version check
        if not py39_or_higher:
            self.lg("")
            self.lg(f"Hey, what about trying {hl('Python >= 3.9')}‽ 🤪")
            self.lg("")
        if not py38_or_higher:
            self.lg("", icon=ALERT_ICON)
            self.lg("")
            self.lg(
                f"Please update to {hl('Python >= 3.8')} at least! 🤪", icon=ALERT_ICON
            )
            self.lg("")
            self.lg("", icon=ALERT_ICON)
        if not py37_or_higher:
            raise ValueError

        # appdaemon version check
        if not self.has_min_ad_version("4.0.7"):
            self.lg("", icon=ALERT_ICON)
            self.lg("")
            self.lg(
                f"Please update to {hl('AppDaemon >= 4.0.7')} at least! 🤪",
                icon=ALERT_ICON,
            )
            self.lg("")
            self.lg("", icon=ALERT_ICON)
            return

        # set room, use the app name if "room" is not defined
        self.room_name = (
            str(self.args.pop("room")) if "room" in self.args else self.name
        )

        # general delay
        self.delay = int(self.getarg("delay", DEFAULT_DELAY))

        # delay for events outside AutoMoLi, defaults to same as general delay
        self.delay_outside_events = int(self.getarg("delay_outside_events", self.delay))

        # directly switch to new daytime light settings
        self.transition_on_daytime_switch: bool = bool(
            self.getarg("transition_on_daytime_switch", False)
        )

        # state values
        self.states = {
            "motion_on": self.getarg("motion_state_on", None),
            "motion_off": self.getarg("motion_state_off", None),
        }

        # threshold values
        self.thresholds = {
            "humidity": self.getarg("humidity_threshold", None),
            EntityType.ILLUMINANCE.idx: self.getarg("illuminance_threshold", None),
        }

        # experimental dimming features
        self.dimming: bool = False
        self.dim: dict[str, int | DimMethod] = {}
        if (dim := self.getarg("dim", {})) and (
            seconds_before := dim.pop("seconds_before", None)
        ):

            brightness_step_pct = dim.pop("brightness_step_pct", None)

            dim_method: DimMethod | None = None
            if method := dim.pop("method", None):
                dim_method = (
                    DimMethod.TRANSITION
                    if method.lower() == "transition"
                    else DimMethod.STEP
                )
            elif brightness_step_pct:
                dim_method = DimMethod.TRANSITION
            else:
                dim_method = DimMethod.NONE

            self.dim = {  # type: ignore
                "brightness_step_pct": brightness_step_pct,
                "seconds_before": int(seconds_before),
                "method": dim_method.value,
            }

        # night mode settings
        self.night_mode: dict[str, int | str] = {}
        if night_mode := self.getarg("night_mode", {}):
            self.night_mode = self.configure_night_mode(night_mode)

        # set up sensors that will disable automoli
        self.disabled_entities = set()
        self.disable_switch_entities: list[str] = list(
            self.listr(self.getarg("disable_switch_entities", set()))
        )
        self.disable_switch_states: set[str] = self.listr(
            self.getarg("disable_switch_states", set(["off"])), False
        )
        # set up event listener for each disabled entity
        for disable_entity in self.disable_switch_entities:
            listener.add(
                self.listen_state(
                    self.disabled_change,
                    entity_id=disable_entity,
                )
            )
            # Check if disable_entity is currently on
            if self.get_state(disable_entity, copy=False) in self.disable_switch_states:
                self.disabled_entities.add(disable_entity)

        # set up sensors that will block turning on lights
        self.block_on_entities = set()
        self.block_on_switch_entities: list[str] = list(
            self.listr(self.getarg("block_on_switch_entities", set()))
        )
        self.block_on_switch_states: set[str] = self.listr(
            self.getarg("block_on_switch_states", set(["off"])), False
        )
        # set up event listener for each block_on entity
        for block_on_entity in self.block_on_switch_entities:
            listener.add(
                self.listen_state(
                    self.block_on_change,
                    entity_id=block_on_entity,
                )
            )
            # Check if block_on_entity it is currently on
            if (
                self.get_state(block_on_entity, copy=False)
                in self.block_on_switch_states
            ):
                self.block_on_entities.add(block_on_entity)

        # set up sensors that will block turning off lights
        self.block_off_entities = set()
        self.block_off_switch_entities: list[str] = list(
            self.listr(self.getarg("block_off_switch_entities", set()))
        )
        self.block_off_switch_states: set[str] = self.listr(
            self.getarg("block_off_switch_states", set(["off"])), False
        )
        # set up event listener for each block_off entity
        for block_off_entity in self.block_off_switch_entities:
            listener.add(
                self.listen_state(
                    self.block_off_change,
                    entity_id=block_off_entity,
                )
            )
            # Check if block_off_entity it is currently on
            if (
                self.get_state(block_off_entity, copy=False)
                in self.block_off_switch_states
            ):
                self.block_off_entities.add(block_off_entity)

        # sensors that will change current default delay
        self.override_delay_entities: set[str] = self.listr(
            self.getarg("override_delay_entities", set())
        )
        self.override_delay: int = int(
            self.getarg("override_delay", DEFAULT_OVERRIDE_DELAY)
        )
        self.override_delay_active: bool = False

        # store if an entity has been switched on by automoli
        # None: automoli will only turn off lights following delay after motion detected,
        #       not if there is no motion sensor and lights were just turned on manually
        #       or via automation (treated differently from False to support legacy behavior)
        # False: automoli will turn off lights after motion detected OR if the lights
        #       were just turned on manually or via automation
        # True: automoli will only turn off lights it turned on
        self.only_own_events: bool = self.getarg("only_own_events", None)
        self._switched_on_by_automoli: set[str] = set()
        self._switched_off_by_automoli: set[str] = set()
        # Cooldown period is used when a light is turned off manually, to ensure automoli
        # doesn't immediately turn it back on
        self.cooldown_period: int = int(self.getarg("cooldown", DEFAULT_COOLDOWN))

        self.disable_hue_groups: bool = self.getarg("disable_hue_groups", False)

        self.warning_flash: bool = self.getarg("warning_flash", False)
        self._warning_lights: set[str] = set()

        # eol of the old option name
        if "disable_switch_entity" in self.args:
            self.lg("", icon=ALERT_ICON)
            self.lg(
                f"Please migrate {hl('disable_switch_entity')} to {hl('disable_switch_entities')}",
                icon=ALERT_ICON,
            )
            self.lg("", icon=ALERT_ICON)
            self.args.pop("disable_switch_entity")
            return

        # currently active daytime settings
        self.active: dict[str, int | str] = {}

        # entity lists for initial discovery
        states = self.get_state()

        # define light entities switched by automoli
        self.lights: list[str] = list(self.listr(self.getarg("lights", set())))

        # warn and remove scenes and scripts from lights and recommend using after_on or after_off
        scene_or_script_found = False
        remove_list = set()
        for light in self.lights:
            if light.startswith("scene.") or light.startswith("script."):
                scene_or_script_found = True
                remove_list.add(light)
        for light in remove_list:
            self.lights.remove(light)
        if scene_or_script_found:
            self.lg(
                f"A scene or script was found in the list of lights and removed",
                icon=ALERT_ICON,
            )
            self.lg(
                f"Instead use after_on to turn on scenes or to run scripts when lights go on",
                icon=ALERT_ICON,
            )

        if not self.lights:
            room_light_group = f"light.{self.room_name}"
            if self.entity_exists(room_light_group):
                self.lights.add(room_light_group)
            else:
                self.lights.update(
                    self.find_sensors(EntityType.LIGHT.prefix, self.room_name, states)
                )

        # define a set of entities that will be switched on after lights are turned on / off
        self.after_on: set[str] = self.listr(self.getarg("after_on", set()))
        self.after_off: set[str] = self.listr(self.getarg("after_off", set()))

        # sensors
        self.sensors: dict[str, Any] = {}

        # enumerate sensors for motion detection
        self.sensors[EntityType.MOTION.idx] = self.listr(
            self.getarg(
                "motion",
                self.find_sensors(EntityType.MOTION.prefix, self.room_name, states),
            )
        )

        self.room = Room(
            name=self.room_name,
            room_lights=self.lights,
            motion=self.sensors[EntityType.MOTION.idx],
            door_window=set(),
            temperature=set(),
            push_data=dict(),
            appdaemon=self.get_ad_api(),
        )

        # requirements check:
        # - lights must exist
        # - motion must exist or only using automoli to turn off lights manually turned on after delay
        if not self.lights or not (
            self.sensors[EntityType.MOTION.idx] or self.only_own_events == False
        ):
            self.lg("")
            self.lg(
                f"{hl('No lights/sensors')} given and none found with name: "
                f"'{hl(EntityType.LIGHT.prefix)}*{hl(self.room.name)}*' or "
                f"'{hl(EntityType.MOTION.prefix)}*{hl(self.room.name)}*'",
                icon="⚠️ ",
            )
            self.lg("")
            self.lg("  docs: https://github.com/mkotler/ad-automoli")
            self.lg("")
            return

        # initialize variables for tracking room statistics
        # if track_room_stats is True sensors will be created to show room information
        # and statistics will be printed to the log at the end of each day
        self.track_room_stats: bool = bool(self.getarg("track_room_stats", False))
        self.entity_id = "automoli." + self.room_name.lower()
        self.sensor_state: str = "off"
        self.sensor_onToday: int = 0
        self.sensor_attr: dict[str, Any] = {}
        self.sensor_update_handles: set[str] = set()
        self.sensor_lastTurningOffAt: str = "<unknown>"
        self.init_room_stats()
        self.run_daily(self.reset_room_stats, "00:00:00")
        self.listen_event(self.room_event, event=EVENT_AUTOMOLI_STATS)
        self.last_room_stats_error: str = "NO_ERROR"

        if self.track_room_stats:
            self.run_daily(self.print_room_stats, "23:59:59")

        # enumerate optional sensors & disable optional features if sensors are not available
        for sensor_type in SENSORS_OPTIONAL:

            if sensor_type in self.thresholds and self.thresholds[sensor_type]:
                self.sensors[sensor_type] = self.listr(
                    self.getarg(sensor_type, None)
                ) or self.find_sensors(KEYWORDS[sensor_type], self.room_name, states)

                self.lg(f"{self.sensors[sensor_type] = }", level=logging.DEBUG)

            else:
                self.lg(
                    f"No {sensor_type} sensors → disabling features based on {sensor_type}"
                    f" - {self.thresholds[sensor_type]}.",
                    level=logging.DEBUG,
                )
                del self.thresholds[sensor_type]

        # use user-defined daytimes if available
        daytimes = self.build_daytimes(self.getarg("daytimes", DEFAULT_DAYTIMES))

        # set up event listener for each motion sensor
        for sensor in self.sensors[EntityType.MOTION.idx]:

            # listen to xiaomi sensors by default
            if not any([self.states["motion_on"], self.states["motion_off"]]):
                self.lg(
                    f"{stack()[0][3]} | No motion states configured - using event listener for {sensor}",
                    level=logging.DEBUG,
                )
                listener.add(
                    self.listen_event(
                        self.motion_event, event=EVENT_MOTION_XIAOMI, entity_id=sensor
                    )
                )

            # on/off-only sensors without events on every motion
            elif all([self.states["motion_on"], self.states["motion_off"]]):
                self.lg(
                    f"{stack()[0][3]} | Motion states configured - using state listener for {sensor}",
                    level=logging.DEBUG,
                )
                listener.add(
                    self.listen_state(
                        self.motion_detected,
                        entity_id=sensor,
                        new=self.states["motion_on"],
                    )
                )
                listener.add(
                    self.listen_state(
                        self.motion_cleared,
                        entity_id=sensor,
                        new=self.states["motion_off"],
                    )
                )
        # set up state listener for each light even if only want to turn off lights via automoli
        self.lg(
            f"{stack()[0][3]} | Adding state listeners for lights when a change happens outside automoli",
            level=logging.DEBUG,
        )
        for light in self.lights:
            # do not track scenes or scripts
            if not light.startswith("scene.") and not light.startswith("script."):
                listener.add(
                    self.listen_state(
                        self.outside_change_detected,
                        entity_id=light,
                        attribute="all",
                    )
                )
                # assume any lights that are currently on were switched on by AutoMoLi
                if self.get_state(light, copy=False) == "on":
                    self._switched_on_by_automoli.add(light)

        # Track if within cooling down period
        self.cooling_down: bool = False
        self.cooling_down_handle: str | None = None

        # set up state listener for entities that will override the current delay
        # on/off-only sensors without events on every motion
        for entity in self.override_delay_entities:
            listener.add(
                self.listen_state(
                    self.update_delay,
                    entity_id=entity,
                    new=self.states["motion_on"],
                )
            )

        self.args.update(
            {
                "room": self.room_name.capitalize(),
                "delay": self.delay,
                "delay_outside_events": self.delay_outside_events,
                "active_daytime": self.active_daytime,
                "daytimes": daytimes,
                "transition_on_daytime_switch": self.transition_on_daytime_switch,
                "lights": self.lights,
                "dim": self.dim,
                "sensors": self.sensors,
                "override_delay": self.override_delay,
                "disable_hue_groups": self.disable_hue_groups,
                "warning_flash": self.warning_flash,
                "only_own_events": self.only_own_events,
                "cooldown": self.cooldown_period,
                "track_room_stats": self.track_room_stats,
                "loglevel": self.loglevel,
            }
        )

        # add illuminance or humidity thresholds if defined
        if self.thresholds:
            self.args.update({"thresholds": self.thresholds})

        # add night mode to config if enabled
        if self.night_mode:
            self.args.update({"night_mode": self.night_mode})

        # add disable entity to config if given
        if self.disable_switch_entities:
            self.args.update({"disable_switch_entities": self.disable_switch_entities})
            self.args.update({"disable_switch_states": self.disable_switch_states})

        # add block on and off entities to config if given
        if self.block_on_switch_entities:
            self.args.update(
                {"block_on_switch_entities": self.block_on_switch_entities}
            )
            self.args.update({"block_on_switch_states": self.block_on_switch_states})
        if self.block_off_switch_entities:
            self.args.update(
                {"block_off_switch_entities": self.block_off_switch_entities}
            )
            self.args.update({"block_off_switch_states": self.block_off_switch_states})

        # add override delay entities to config if given
        if self.override_delay_entities:
            self.args.update({"override_delay_entities": self.override_delay_entities})

        # add after_on and after_off entities to config if given
        if self.after_on:
            self.args.update({"after_on": self.after_on})
        if self.after_off:
            self.args.update({"after_off": self.after_off})

        # show parsed config
        self.show_info(self.args)

        # set room as "on" if the state of any of the entities in self.lights is "on"
        if any([self.get_state(light, copy=False) == "on" for light in self.lights]):
            self.sensor_state = "on"
            self.run_in(
                self.update_room_stats,
                10,
                stat="lastOn",
                appInit=True,
                source="On at restart",
                updateDelay=10,
            )

            light_setting = (
                self.active.get("light_setting")
                if not bool(
                    self.night_mode
                    and self.get_state(self.night_mode["entity"], copy=False) == "on"
                )
                else self.night_mode.get("light")
            )

            is_brightness = isinstance(light_setting, int)
            message = (
                f"{hl(self.room.name.replace('_',' ').title())} was {hl('on')} when AutoMoLi started | "
                f"{'brightness: ' if is_brightness else ''}{hl(light_setting)}"
                f"{'%' if is_brightness else ''} | delay: {hl(natural_time(int(self.active['delay'])))}"
            )
            # Since in initialization loop, wait 10s for all rooms to load before logging
            self.run_in(self.lg_delayed, 10, msg=message, icon=ON_ICON)

            self.refresh_timer()
        else:
            self.sensor_state = "off"
            self.run_in(
                self.update_room_stats,
                10,
                stat="lastOff",
                appInit=True,
                source="Off at restart",
                updateDelay=10,
            )

        # If line_profiler is installed and room configuration has "profile: True",
        # run a performance profile for the first 2 minutes after initialization
        if PROFILING_AVAILABLE and self.getarg("profile", False):
            self.lg(f"Beginning to profile AutoMoLi code")
            profile.enable_by_count()
            self.run_in(self.end_profiling, 120)

    def end_profiling(self, kwargs: dict[str, Any] | None = None):
        profile.disable_by_count()
        timestamp = datetime.now().strftime("%Y-%m-%dT%H%M%S")
        file_name = f"{APP_NAME}_profile_{timestamp}.txt"

        # TODO: Update the hardcoded path to the path of the main_log file
        log_path = "/config/logs"
        if os.path.exists(log_path):
            directory = log_path
        else:
            directory = os.getcwd()
        path = os.path.join(directory, file_name)

        with open(path, "w") as file:
            profile.print_stats(stream=file)
        self.lg(f"Ending profile of AutoMoLi code")

    def has_min_ad_version(self, required_version: str) -> bool:
        required_version = required_version if required_version else "4.0.7"
        return bool(Version(self.get_ad_version()) >= Version(required_version))

    def switch_daytime(self, kwargs: dict[str, Any]) -> None:
        """Set new light settings according to daytime."""

        # Capture current settings to see if anything changes
        last_delay = self.active.get("delay")
        last_light_setting = self.active.get("light_setting")

        daytime = kwargs.get("daytime")

        if daytime is not None:
            self.active = daytime
            if not kwargs.get("initial"):

                delay = daytime["delay"]
                light_setting = daytime["light_setting"]
                settings_changed = (delay != last_delay) or (
                    light_setting != last_light_setting
                )
                is_brightness = isinstance(light_setting, int)
                self.lg(
                    f"{stack()[0][3]} | {self.transition_on_daytime_switch = }",
                    level=logging.DEBUG,
                )

                action_done = "Set"

                # If transition_on_daytime_switch then execute the daytime changes if:
                # - any lights are on since brightness may have changed
                # - the light_setting is a scene or script then want to execute it
                # But if the lights are all off and brightness changed, that's the one case
                # when do not want to update (or else could turn on the lights even when
                # no motion is detected)
                if self.transition_on_daytime_switch and any(
                    [self.get_state(light, copy=False) == "on" for light in self.lights]
                ):
                    self.lights_on(source="daytime change", force=True)
                    action_done = "Activated"

                if settings_changed:
                    self.lg(
                        f"{action_done} daytime {hl(daytime['daytime'])} | "
                        f"{'brightness: ' if is_brightness else ''}{hl(light_setting)}"
                        f"{'%' if is_brightness else ''}, delay: {hl(natural_time(delay))}",
                        icon=DAYTIME_SWITCH_ICON,
                    )
                # Update room stats with latest daytime
                self.run_in(
                    self.update_room_stats,
                    DEFAULT_UPDATE_STATS_DELAY,
                    stat="switchDaytime",
                )
            else:
                # Update room stats with initial daytime
                self.run_in(
                    self.update_room_stats,
                    10,
                    stat="switchDaytime",
                    updateDelay=10,
                )

    def motion_cleared(
        self, entity: str, attribute: str, old: str, new: str, _: dict[str, Any]
    ) -> None:
        """Handler for all motion sensors that do not push a certain event but instead
        the default Home Assistant `state_changed` event is used for presence detection,
        schedules the callback to switch the lights off after a `state_changed` callback
        of a motion sensors changing to "cleared" is received
        """
        # Check if got entire state object
        if attribute == "all":
            state = dict(new).get("state")
            if old != None:
                old_state = dict(old).get("state", "unknown")
            else:
                old_state = "unknown"
        else:
            state = new
            old_state = old

        # Do not process if there has not actually been a state change
        if state == old_state:
            return

        # Check that all motion sensors have cleared before starting timer.
        # If any other motion sensors are not ready (e.g., unavailable or unknown),
        # treat them like they are clear. Otherwise, motion may never clear causing
        # lights to be left on indefinitely.
        clear_states = {self.states["motion_off"]} | NOT_READY_STATES
        all_clear = all(
            [
                self.get_state(sensor, copy=False) in clear_states
                for sensor in self.sensors[EntityType.MOTION.idx]
            ]
        )

        # Capture specifics for debug logging purposes only
        not_clear = any(
            [
                self.get_state(sensor, copy=False) in {self.states["motion_on"]}
                for sensor in self.sensors[EntityType.MOTION.idx]
            ]
        )
        not_ready = any(
            [
                self.get_state(sensor, copy=False) in NOT_READY_STATES
                for sensor in self.sensors[EntityType.MOTION.idx]
            ]
        )

        self.lg(
            f"{stack()[0][3]} | {entity} changed {attribute} from {old} to {new}"
            f"{' and all sensors are clear' if all_clear else ' but waiting for all sensors to clear'}"
            f"{(' | Not Clear: ' + ', '.join([sensor for sensor in self.sensors[EntityType.MOTION.idx] if self.get_state(sensor, copy=False) in {self.states['motion_on']} ])) if not_clear else ''}"
            f"{(' | Not Ready: ' + ', '.join([sensor for sensor in self.sensors[EntityType.MOTION.idx] if self.get_state(sensor, copy=False) in NOT_READY_STATES])) if not_ready else ''}",
            level=logging.DEBUG,
        )

        if all_clear:
            self.run_in(
                self.update_room_stats,
                DEFAULT_UPDATE_STATS_DELAY,
                stat="motion_cleared",
                entity=entity,
            )
            self.refresh_timer(refresh_type="motion_cleared")
        else:
            # cancel scheduled callbacks
            self.clear_handles()

    @profile
    def motion_detected(
        self, entity: str, attribute: str, old: str, new: str, kwargs: dict[str, Any]
    ) -> None:
        """Handler for all motion sensors that do not push a certain event but instead
        the default Home Assistant `state_changed` event is used for presence detection
        maps the `state_changed` callback of a motion sensors changing to "detected"
        to the `event` callback`
        """

        self.run_in(
            self.update_room_stats,
            DEFAULT_UPDATE_STATS_DELAY,
            stat="motion_detected",
            entity=entity,
        )

        if self.log_debug:
            self.lg(
                f"{stack()[0][3]} | {entity} changed {attribute} from {old} to {new}"
                f" | {self.dimming = }",
                level=logging.DEBUG,
            )

        # cancel scheduled callbacks
        if self.room.handles_automoli:
            self.clear_handles()

            if self.log_debug:
                self.lg(
                    f"{stack()[0][3]} | Handles cleared and cancelled all scheduled timers"
                    f" | {self.dimming = }",
                    level=logging.DEBUG,
                )

        self.lights_on(source=entity)

    def motion_event(self, event: str, data: dict[str, str], _: dict[str, Any]) -> None:
        """Specialized handler for Xiaomi sensors with the Xiaomi Gateway (Aqara) integration.
        TODO: Generalize for custom events other than EVENT_MOTION_XIAOMI but motion_detected
        and motion_cleared functions already handle any sensor changes through state_changed events.
        """

        motion_trigger = data["entity_id"].replace(EntityType.MOTION.prefix, "")
        self.run_in(
            self.update_room_stats,
            DEFAULT_UPDATE_STATS_DELAY,
            stat="motion_event",
            entity=motion_trigger,
        )

        if self.log_debug:
            self.lg(
                f"{stack()[0][3]} | Received '{hl(event)}' event from "
                f"'{motion_trigger}' | {self.dimming = }",
                level=logging.DEBUG,
            )

            self.lg(
                f"{stack()[0][3]} | Ready to switch on lights and then refresh timer",
                level=logging.DEBUG,
            )

        self.lights_on(source=motion_trigger)
        self.refresh_timer()

    def disabled_change(
        self,
        entity: str,
        attribute: str,
        old: str,
        new: str | dict,
        _: dict[str, Any],
    ) -> None:
        """Listener for changes to disable_switch_entities"""

        self.lg(
            f"{stack()[0][3]} | Change detected in disable_switch_entities ({entity}) with {old = } and {new = }",
            level=logging.DEBUG,
        )
        if old != new:
            if new in self.disable_switch_states:
                self.disabled_entities.add(entity)
            elif entity in self.disabled_entities:
                self.disabled_entities.remove(entity)

    def block_on_change(
        self,
        entity: str,
        attribute: str,
        old: str,
        new: str | dict,
        _: dict[str, Any],
    ) -> None:
        """Listener for changes to block_on_switch_entities"""

        self.lg(
            f"{stack()[0][3]} | Change detected in block_on_switch_entities ({entity}) with {old = } and {new = }",
            level=logging.DEBUG,
        )
        if old != new:
            if new in self.block_on_switch_states:
                self.block_on_entities.add(entity)
            elif entity in self.block_on_entities:
                self.block_on_entities.remove(entity)

    def block_off_change(
        self,
        entity: str,
        attribute: str,
        old: str,
        new: str | dict,
        _: dict[str, Any],
    ) -> None:
        """Listener for changes to block_off_switch_entities"""

        self.lg(
            f"{stack()[0][3]} | Change detected in block_off_switch_entities ({entity}) with {old = } and {new = }",
            level=logging.DEBUG,
        )
        if old != new:
            if new in self.block_off_switch_states:
                self.block_off_entities.add(entity)
            elif entity in self.block_off_entities:
                self.block_off_entities.remove(entity)

    def outside_change_detected(
        self,
        entity: str,
        attribute: str,
        old: str,
        new: str | dict,
        _: dict[str, Any],
    ) -> None:
        """wrapper for when listening to outside light changes. on `state_changed` callback
        of a light setup a timer by calling `refresh_timer`
        """

        self.lg(
            f"{stack()[0][3]} | Change detected in {entity = } with {old = } and {new = }",
            level=logging.DEBUG,
        )

        # Check if got entire state object
        context_id = "<unknown>"
        parent_id = "<unknown>"
        user_id = "<unknown>"
        if attribute == "all":
            state = dict(new).get("state")
            if old != None:
                old_state = dict(old).get("state", "unknown")
            else:
                old_state = "unknown"
            context_id = dict(dict(new).get("context")).get("id")
            parent_id = dict(dict(new).get("context")).get("parent_id")
            user_id = dict(dict(new).get("context")).get("user_id")
        else:
            state = new
            old_state = old

        self.lg(
            f"{stack()[0][3]} | Change was caused by context_id = {context_id}, parent_id = {parent_id}, and user_id = {user_id}",
            level=logging.DEBUG,
        )

        # ensure the change wasn't because of automoli
        if (
            state == "on"
            and (
                entity in self._switched_on_by_automoli
                or entity in self._warning_lights
            )
        ) or (
            state == "off"
            and (
                entity in self._switched_off_by_automoli
                or entity in self._warning_lights
            )
        ):
            self.lg(
                f"{stack()[0][3]} | State change was due to automoli so ignoring",
                level=logging.DEBUG,
            )
            return

        # do not process if current state is in list of not ready states
        # or has not really changed (new == old)
        # assume that previous state holds until new state is available
        if state in NOT_READY_STATES:
            self.lg(
                f"{stack()[0][3]} | State of {self.get_name(entity)} changed to '{state}'"
            )
            return
        if state == old_state:
            self.lg(
                f"{stack()[0][3]} | State did not actually change so not tracking as a change by AutoMoLi",
                level=logging.DEBUG,
            )
            return

        automation = False
        source = ""
        # Determine what caused the change
        # "How to use context" from tom_l at https://community.home-assistant.io/t/how-to-use-context/723136
        if parent_id != None:
            # Change was caused by an automation
            automation = True
            automations = self.get_state(entity_id="automation")
            for automation_id in automations:
                automation_state = self.get_state(
                    entity_id=automation_id, attribute="all", copy=False
                )
                automation_context_id = dict(dict(automation_state).get("context")).get(
                    "id"
                )
                if context_id == automation_context_id:
                    source_friendly_name = dict(
                        dict(automation_state).get("attributes")
                    ).get("friendly_name")
                    source = (
                        "'" + source_friendly_name + "'"
                        if source_friendly_name != None
                        else automation_id
                    )
                    break
            # There are situations where context will not match, so just set source to generic automation
            if source == "":
                source = "automation"
        elif user_id != None:
            # Change was made in the HomeAssistant UI
            source = "manually in the HomeAssistant UI"
        else:
            # Change was likely caused by a physical device
            # Find the first device that triggered the context ID
            earliest_last_changed = None
            earliest_device_state = None
            devices = self.get_state()
            for device in devices:
                device_state = self.get_state(
                    entity_id=device, attribute="all", copy=False
                )
                device_context_id = dict(dict(device_state).get("context")).get("id")

                if context_id == device_context_id:
                    device_last_changed = dict(device_state).get("last_changed")
                    last_five_seconds = datetime.fromisoformat(device_last_changed) > (
                        (datetime.now(tz.UTC) - timedelta(seconds=5))
                    )
                    self.lg(
                        f"{stack()[0][3]} | Context ID matched for {device}. Device last changed: {device_last_changed}.  In last five seconds: {last_five_seconds}.  Earliest last changed: {earliest_last_changed}. Device last changed < Earliest last changed: { (device_last_changed < earliest_last_changed) and last_five_seconds if earliest_last_changed != None else 'N/A'}.",
                        level=logging.DEBUG,
                    )
                    if earliest_last_changed == None or (
                        device_last_changed < earliest_last_changed
                    ):
                        # If the device state change triggered a property change in another device, then could get a false positive.
                        # Therefore, excluding any devices where the state change was longer than 5 seconds ago.
                        if last_five_seconds:
                            earliest_last_changed = device_last_changed
                            earliest_device_state = device_state

            if earliest_device_state:
                source_entity_id = dict(earliest_device_state).get("entity_id")
            else:
                source_entity_id = None

            # Check if the physical device matches one of the lights
            if source_entity_id is None or source_entity_id in self.lights:
                source = "manually"
            else:
                source_domain = source_entity_id.split(".")[0]
                source_friendly_name = dict(
                    dict(earliest_device_state).get("attributes")
                ).get("friendly_name")
                source = (
                    "'" + source_friendly_name + "'"
                    if source_friendly_name != None
                    else source_entity_id
                )

                # Sometimes even when parent ID is empty, the domain can still be an automation
                if source_domain == "automation":
                    automation = True
                else:
                    source = "manually by " + source

        additional_info = ""
        if state == "on":
            additional_info = f"| delay: {hl(natural_time(int(self.active['delay'])))}"
            state_icon = ON_ICON
        elif state == "off":
            state_icon = OFF_ICON
        else:
            state_icon = None

        if automation == False:
            if old_state == "on" or old_state == "off":
                self.lg(
                    f"{hl(self.get_name(entity))} was turned '{state}' {source} {additional_info}",
                    icon=state_icon,
                )
            # otherwise handle case when state was "unavailable" or "unknown"
            else:
                self.lg(
                    f"{hl(self.get_name(entity))} changed to '{state}' from {old_state} {additional_info}",
                    icon=state_icon,
                )
        else:
            if source == "automation":
                self.lg(
                    f"{hl(self.get_name(entity))} was turned '{state}' by an automation {additional_info}",
                    icon=state_icon,
                )
            else:
                self.lg(
                    f"{hl(self.get_name(entity))} was turned '{state}' by automation {source} {additional_info}",
                    icon=state_icon,
                )

        # stop tracking the light as turned on or off by AutoMoLi
        if entity in self._switched_on_by_automoli:
            self._switched_on_by_automoli.remove(entity)
        if entity in self._switched_off_by_automoli:
            self._switched_off_by_automoli.remove(entity)

        # Get all of the lights in the room besides the one that just changed
        filtered_lights = set(filter(lambda light: light != entity, self.lights))

        how = "manually" if automation == False else "automation"
        if state == "off":
            # when all of the lights have been turned off then
            # cancel scheduled callbacks and update stats to set room off
            # otherwise don't do anything, regular delay should turn other lights off
            if all(
                [
                    self.get_state(light, copy=False) == "off"
                    for light in filtered_lights
                ]
            ):
                self.clear_handles()
                self.lg(
                    f"{stack()[0][3]} | Handles cleared and cancelled all scheduled timers",
                    level=logging.DEBUG,
                )
                # update stats to set room off when this is the last light turned off and
                # the room was on before this light was turned off (i.e., it wasn't in a NOT_READY_STATE)
                if self.sensor_state == "on":
                    self.sensor_state = "off"
                    self.run_in(
                        self.update_room_stats,
                        DEFAULT_UPDATE_STATS_DELAY,
                        stat="lastOff",
                        howChanged=how,
                        source=source,
                    )
            # if turned a light off manually, probably don't want AutoMoLi to immediately
            # turn it back on so start cooldown period
            if old_state == "on":
                self.cooling_down = True
                self.cooling_down_handle = self.run_in(
                    self.cooldown_off, self.cooldown_period
                )
        elif state == "on":
            # update stats to set room on when this is the first light turned on
            if self.sensor_state == "off":
                self.sensor_state = "on"
                self.run_in(
                    self.update_room_stats,
                    DEFAULT_UPDATE_STATS_DELAY,
                    stat="lastOn",
                    howChanged=how,
                    source=source,
                )
            # if cooldown period was on, turn it off and cancel handle
            if self.cooling_down == True:
                self.cooling_down = False
                self.cancel_timer(self.cooling_down_handle)
                self.cooling_down_handle = None
            # refresh timer if any lights turned on manually unless only_own_events is True
            if self.only_own_events == False:
                self.refresh_timer(refresh_type="outside_change")
            else:
                self.run_in(
                    self.update_room_stats,
                    DEFAULT_UPDATE_STATS_DELAY,
                    stat="onlyOwnEventsBlock",
                )

    def cooldown_off(self, _: dict[str, Any] | None = None) -> None:
        self.cooling_down = False
        self.run_in(
            self.update_room_stats, DEFAULT_UPDATE_STATS_DELAY, stat="cooldownOff"
        )

    @profile
    def clear_handles(self, handles: set[str] = MISSING) -> None:
        """clear scheduled timers/callbacks."""

        if handles is MISSING:
            handles = self.room.handles_automoli
            clear = True
            which_handles = "the room's "
        else:
            clear = False
            which_handles = ""

        if handles is None or (isinstance(handles, set) and len(handles) == 0):
            if self.log_debug:
                self.lg(
                    f"{stack()[0][3]} | clear_handles called with no handles to cancel",
                    level=logging.DEBUG,
                )
            return
        elif self.log_debug:
            self.lg(
                f"{stack()[0][3]} | Cancelling {which_handles}scheduled callbacks | {handles = }",
                level=logging.DEBUG,
            )

        # getting function references for small performance gain below
        timer_running = self.timer_running
        cancel_timer = self.cancel_timer

        for handle in handles:
            if timer_running(handle):
                cancel_timer(handle)

        if clear:
            self.room.handles_automoli.clear()
            # reset override delay status
            self.override_delay_active = False

    def refresh_timer(self, refresh_type: str = "normal") -> None:
        """refresh delay timer."""

        # Check that any lights were actually turned on.  For example, lights_on may have
        # been called but no lights were turned on if light setting was at 0%.
        if all([self.get_state(light, copy=False) == "off" for light in self.lights]):
            self.lg(
                f"{stack()[0][3]} | Lights were not turned on so clearing all timers. ",
                level=logging.DEBUG,
            )
            self.run_in(
                self.update_room_stats,
                DEFAULT_UPDATE_STATS_DELAY,
                stat="refresh_timer",
                time=-1,
            )
            self.clear_handles()
            return

        # leave dimming state
        self.dimming = False
        dim_in_sec = 0

        # if delay is currently overridden
        if self.override_delay_active:
            # clear handles and go back to normal delay unless
            # motion was cleared *after* overridden delay or
            # refresh was triggered by another override_delay message
            if refresh_type == "motion_cleared":
                # In update_room_stats this scenario is handled during the motion_cleared update
                return
            elif refresh_type != "override_delay":
                self.run_in(
                    self.update_room_stats,
                    DEFAULT_UPDATE_STATS_DELAY,
                    stat="overrideDelay",
                    enable=False,
                )
                self.clear_handles()
        # if delay is not currently overridden then still clear handles
        else:
            self.clear_handles()

        # if an external event (e.g., switch turned on manually) was detected use delay_outside_events
        if refresh_type == "outside_change":
            delay = int(self.delay_outside_events)
        elif refresh_type == "override_delay":
            delay = int(self.override_delay)
        else:
            delay = int(self.active["delay"])

        # if no delay is set or delay = 0, lights will not switched off by AutoMoLi
        if delay:

            self.lg(
                f"{stack()[0][3]} | {self.active = } | {self.delay_outside_events = }"
                f" | {refresh_type = } | {delay = } | {self.dim = }",
                level=logging.DEBUG,
            )

            if self.dim:
                dim_in_sec = int(delay) - self.dim["seconds_before"]
                self.lg(f"{stack()[0][3]} | {dim_in_sec = }", level=logging.DEBUG)

                handle = self.run_in(self.dim_lights, dim_in_sec, timeDelay=delay)

            else:
                handle = self.run_in(self.lights_off, delay, timeDelay=delay)

            self.room.handles_automoli.add(handle)

            if timer_info := self.info_timer(handle):
                self.lg(
                    f"{stack()[0][3]} | Scheduled callback to switch the lights off at "
                    f"{datetime.strftime(timer_info[0], DATETIME_FORMAT)} dimming for {dim_in_sec}s | "
                    f"{self.room.handles_automoli = }",
                    level=logging.DEBUG,
                )
                self.run_in(
                    self.update_room_stats,
                    DEFAULT_UPDATE_STATS_DELAY,
                    stat="refreshTimer",
                    time=timer_info[0],
                )

            if self.warning_flash and refresh_type != "override_delay":
                handle = self.run_in(
                    self.warning_flash_off, (int(delay) - DEFAULT_WARNING_DELAY)
                )
                self.room.handles_automoli.add(handle)
                self.lg(
                    f"{stack()[0][3]} | Scheduled callback to turn lights on after warning flash | "
                    f"{handle = }",
                    level=logging.DEBUG,
                )

        else:
            self.run_in(
                self.update_room_stats,
                DEFAULT_UPDATE_STATS_DELAY,
                stat="refreshTimer",
                time=0,
            )
            self.lg(
                f"{stack()[0][3]} | No delay was set or delay = 0, lights will not be switched off by AutoMoLi",
                level=logging.DEBUG,
            )

    def update_delay(
        self, entity: str, attribute: str, old: str, new: str, _: dict[str, Any]
    ) -> None:
        """override the time delay for turning off lights"""
        # only update the delay if any lights are on
        if any([self.get_state(light, copy=False) == "on" for light in self.lights]):
            self.override_delay_active = True
            self.run_in(
                self.update_room_stats,
                DEFAULT_UPDATE_STATS_DELAY,
                stat="overrideDelay",
                enable=True,
                entity=entity,
            )
            self.refresh_timer(refresh_type="override_delay")

    @profile
    def is_disabled(self, onoff: str = None) -> bool:
        """check if automoli is disabled via home assistant entity"""

        if self.disabled_entities:
            # Refresh timer if disabling lights from turning off
            if onoff == "off":
                self.refresh_timer()

            entities = ", ".join(
                self.get_name(entity) for entity in self.disabled_entities
            )
            # Only log first time disabled
            if self.sensor_attr.get("disabled_by", "") == "":
                self.lg(f"{APP_NAME} is disabled by {entities}")
            self.run_in(
                self.update_room_stats,
                DEFAULT_UPDATE_STATS_DELAY,
                stat="disabled",
                entity=entities,
            )
            return True

        # or because currently in cooldown period after an outside change
        if self.cooling_down and onoff == "on":
            # Do not need to refresh timer because cooling_down currently
            # only disables lights turning on
            self.lg(
                f"Motion was detected but {APP_NAME} is disabled during cooldown period"
            )
            self.run_in(
                self.update_room_stats,
                DEFAULT_UPDATE_STATS_DELAY,
                stat="disabled",
                entity="Cooling down",
            )
            return True

        return False

    @profile
    def is_blocked(self, onoff: str = None) -> bool:
        if onoff == "on":
            if self.block_on_entities:
                entities = ", ".join(
                    self.get_name(entity) for entity in self.block_on_entities
                )
                if self.sensor_attr.get("blocked_on_by", "") == "":
                    self.lg(
                        f"Motion detected in {hl(self.room.name.replace('_',' ').title())} "
                        f"but blocked by {entities}"
                    )
                self.run_in(
                    self.update_room_stats,
                    DEFAULT_UPDATE_STATS_DELAY,
                    stat="blockedOn",
                    entity=entities,
                )
                return True
        elif onoff == "off":
            # the "shower case"
            if humidity_threshold := self.thresholds.get("humidity"):
                for sensor in self.sensors[EntityType.HUMIDITY.idx]:
                    try:
                        current_humidity = float(
                            self.get_state(sensor, copy=False)  # type:ignore
                        )
                    except ValueError as error:
                        self.lg(
                            f"{stack()[0][3]} | self.get_state(sensor) raised a ValueError for {sensor}: {error}",
                            level=logging.ERROR,
                        )
                        continue

                    self.lg(
                        f"{stack()[0][3]} | {current_humidity = } >= {humidity_threshold = } "
                        f"= {current_humidity >= humidity_threshold}",
                        level=logging.DEBUG,
                    )

                    if current_humidity >= humidity_threshold:
                        self.refresh_timer()
                        # Only log first time blocked
                        if self.sensor_attr.get("blocked_off_by", "") == "":
                            self.lg(
                                f"🛁 No motion in {hl(self.room.name.replace('_',' ').title())} since "
                                f"{hl(natural_time(int(self.active['delay'])))} "
                                f"but {hl(current_humidity)}%RH > "
                                f"{hl(humidity_threshold)}%RH"
                            )
                        self.run_in(
                            self.update_room_stats,
                            DEFAULT_UPDATE_STATS_DELAY,
                            stat="blockedOff",
                            entity=sensor,
                        )
                        return True
            # other entities
            if self.block_off_entities:
                entities = ", ".join(
                    self.get_name(entity) for entity in self.block_off_entities
                )
                if self.sensor_attr.get("blocked_off_by", "") == "":
                    self.lg(
                        f"No motion in {hl(self.room.name.replace('_',' ').title())} since "
                        f"{hl(natural_time(int(self.active['delay'])))} but blocked by {entities}"
                    )
                self.run_in(
                    self.update_room_stats,
                    DEFAULT_UPDATE_STATS_DELAY,
                    stat="blockedOff",
                    entity=entities,
                )
                return True

        return False

    def dim_lights(self, kwargs: dict[str, Any]) -> None:
        # Note: lights_dimmable, lights_undimmable, and natural_time are defined in the imported library adutils
        # TODO: This codepath has not been tested / exercised in a while. Need to ensure logic still holds and
        # works as expected.

        message: str = ""

        # check logging level here first to avoid duplicate log entries when not debug logging
        if self.log_debug:
            self.lg(
                f"{stack()[0][3]} | {self.is_disabled(onoff='off') = } | {self.is_blocked(onoff='off') = }",
                level=logging.DEBUG,
            )

        # check if automoli is disabled via home assistant entity or blockers like the "shower case"
        if self.is_disabled(onoff="off") or self.is_blocked(onoff="off"):
            return

        if not any(
            [self.get_state(light, copy=False) == "on" for light in self.lights]
        ):
            return

        dim_method: DimMethod
        seconds_before: int = 10

        if (
            self.dim
            and (dim_method := DimMethod(self.dim["method"]))
            and dim_method != DimMethod.NONE
        ):

            seconds_before = int(self.dim["seconds_before"])
            dim_attributes: dict[str, int] = {}

            self.lg(
                f"{stack()[0][3]} | {dim_method = } | {seconds_before = }",
                level=logging.DEBUG,
            )

            if dim_method == DimMethod.STEP:
                dim_attributes = {
                    "brightness_step_pct": int(self.dim["brightness_step_pct"])
                }
                message = (
                    f"{hl(self.room.name.replace('_',' ').title())} → "
                    f"dim to {hl(self.dim['brightness_step_pct'])} | "
                    f"{hl('off')} in {natural_time(seconds_before)}"
                )

            elif dim_method == DimMethod.TRANSITION:
                dim_attributes = {"transition": int(seconds_before)}
                message = (
                    f"{hl(self.room.name.replace('_',' ').title())} → transition to "
                    f"{hl('off')} in ({natural_time(seconds_before)})"
                )

            self.dimming = True

            self.lg(
                f"{stack()[0][3]} | {dim_attributes = } | {self.dimming = }",
                level=logging.DEBUG,
            )

            self.lg(
                f"{stack()[0][3]} | {self.room.room_lights = }", level=logging.DEBUG
            )
            self.lg(
                f"{stack()[0][3]} | {self.room.lights_dimmable = }", level=logging.DEBUG
            )
            self.lg(
                f"{stack()[0][3]} | {self.room.lights_undimmable = }",
                level=logging.DEBUG,
            )

            if self.room.lights_undimmable:
                for light in self.room.lights_dimmable:

                    self.call_service(
                        "light/turn_off",
                        entity_id=light,  # type:ignore
                        **dim_attributes,  # type:ignore
                    )
                    self.set_state(entity_id=light, state="off")
                    if light in self._switched_on_by_automoli:
                        self._switched_on_by_automoli.remove(light)
                    self._switched_off_by_automoli.add(light)

        # workaround to switch off lights that do not support dimming
        if self.room.room_lights:
            self.room.handles_automoli.add(
                self.run_in(
                    self.turn_off_lights,
                    seconds_before,
                    lights=self.room.room_lights,
                )
            )
        # self.turn_off_lights will update "lastOff" stat in turned_off call when
        # lights do not support dimming; otherwise need to call it here
        else:
            delay = kwargs.get("timeDelay", 0)
            timeSinceMotion = (
                (natural_time(int(delay)))
                .replace("\033[1m", "")
                .replace("\033[0m", "")
                .replace("min", " min")
            )
            source = f"No motion for {timeSinceMotion}, dimming lights"
            self.sensor_state = "off"
            self.run_in(
                self.update_room_stats,
                DEFAULT_UPDATE_STATS_DELAY,
                stat="lastOff",
                source=source,
            )

        self.lg(message, icon=OFF_ICON)

    def turn_off_lights(self, kwargs: dict[str, Any]) -> None:
        # Note: This is only called from the dim_lights function. Normally,
        # turned_off is called from lights_off.
        if lights := kwargs.get("lights"):
            self.lg(f"{stack()[0][3]} | {lights = }", level=logging.DEBUG)
            for light in lights:
                self.call_service("homeassistant/turn_off", entity_id=light)
                if light in self._switched_on_by_automoli:
                    self._switched_on_by_automoli.remove(light)
                self._switched_off_by_automoli.add(light)
            self.run_in(self.turned_off, 0)

    @profile
    def lights_on(self, source: str = "<unknown>", force: bool = False) -> None:
        """Turn on the lights."""

        # check logging level here first to avoid duplicate log entries when not debug logging
        if self.log_debug:
            self.lg(
                f"{stack()[0][3]} | {self.is_disabled(onoff='on') = } | {self.is_blocked(onoff='on') = } | {self.dimming = }",
                level=logging.DEBUG,
            )

        # check if automoli is disabled via home assistant entity or blockers
        if self.is_disabled(onoff="on") or self.is_blocked(onoff="on"):
            return

        if self.log_debug:
            self.lg(
                f"{stack()[0][3]} | {self.thresholds.get(EntityType.ILLUMINANCE.idx) = }"
                f" | {self.dimming = } | {force = }",
                level=logging.DEBUG,
            )

        # getting function references for small performance gain below
        get_state = self.get_state
        call_service = self.call_service
        is_hue_group = self.active["is_hue_group"]
        lights = self.lights

        if self.thresholds.get(EntityType.ILLUMINANCE.idx):
            # Small performance optimization to only store this if there is actually a threshold
            illuminance_threshold = self.thresholds.get(EntityType.ILLUMINANCE.idx)

            # the "eco mode" check
            sensors = self.sensors[EntityType.ILLUMINANCE.idx]
            for sensor in sensors:
                if self.log_debug:
                    self.lg(
                        f"{stack()[0][3]} | {illuminance_threshold = } | "
                        f"{float(get_state(sensor, copy=False)) = }",  # type:ignore
                        level=logging.DEBUG,
                    )
                try:
                    if (
                        illuminance := float(
                            get_state(sensor, copy=False)  # type:ignore
                        )  # type:ignore
                    ) >= illuminance_threshold:
                        self.lg(
                            f"According to {hl(sensor)} its already bright enough ¯\\_(ツ)_/¯"
                            f" | {illuminance} >= {illuminance_threshold}"
                        )
                        return

                except ValueError as error:
                    self.lg(
                        f"Could not parse illuminance '{get_state(sensor, copy=False)}' "
                        f"from '{sensor}': {error}"
                    )
                    return

        light_setting = (
            self.active.get("light_setting")
            if not bool(
                self.night_mode
                and self.get_state(self.night_mode["entity"], copy=False) == "on"
            )
            else self.night_mode.get("light")
        )

        at_least_one_turned_on = False

        if isinstance(light_setting, int):

            if light_setting == 0:
                if all([get_state(entity, copy=False) == "off" for entity in lights]):
                    self.lg(
                        f"{stack()[0][3]} | No lights turned on because current 'daytime' light setting is 0",
                        level=logging.DEBUG,
                    )
                # if lights are on only turn them off if force is true (there is a daytime change)
                elif force:
                    self.run_in(self.lights_off, 0, daytimeChange=True)

            else:
                for entity in lights:
                    if self.log_debug:
                        self.lg(
                            f"{stack()[0][3]} | entity: {entity} | startswith: {entity.split('.')[0]} | switched_on_by_automoli: {entity in self._switched_on_by_automoli}",
                            level=logging.DEBUG,
                        )
                    state = get_state(entity, copy=False)
                    is_light = entity.startswith("light")
                    state_off = state == "off"
                    if is_light and (force or self.dimming or state_off):
                        call_service(
                            "homeassistant/turn_on",
                            entity_id=entity,  # type:ignore
                            brightness_pct=light_setting,  # type:ignore
                        )
                        # If AutoMoLi is just changing the light % don't record it as a change
                        if state_off:
                            if not entity in self._switched_on_by_automoli:
                                self._switched_on_by_automoli.add(entity)
                            if entity in self._switched_off_by_automoli:
                                self._switched_off_by_automoli.remove(entity)
                            at_least_one_turned_on = True

                    # Otherwise turn on any lights that are off
                    elif not is_light and state_off:
                        call_service(
                            "homeassistant/turn_on", entity_id=entity  # type:ignore
                        )
                        if not entity in self._switched_on_by_automoli:
                            self._switched_on_by_automoli.add(entity)
                        if entity in self._switched_off_by_automoli:
                            self._switched_off_by_automoli.remove(entity)
                        at_least_one_turned_on = True

                if at_least_one_turned_on:
                    if source != "daytime change" and source != "<unknown>":
                        source = self.get_name(source)

                    # if room is not already "on" update stats
                    if self.sensor_state == "off":
                        self.sensor_state = "on"
                        self.run_in(
                            self.update_room_stats,
                            DEFAULT_UPDATE_STATS_DELAY,
                            stat="lastOn",
                            source=source,
                        )

                    self.lg(
                        f"{hl(self.room.name.replace('_',' ').title())} turned {hl('on')} by {hl(source)} | "
                        f"brightness: {hl(light_setting)}%"
                        f" | delay: {hl(natural_time(int(self.active['delay'])))}",
                        icon=ON_ICON,
                    )

                    # If there are any actions to take after the lights are on then run them now
                    if self.after_on:
                        self.lg(
                            f"{stack()[0][3]} | Lights are on. Now turning on the following 'after_on' entities {self.after_on}",
                            level=logging.DEBUG,
                        )
                        not_ready = self.turn_on_entities(self.after_on)
                        if not_ready:
                            self.lg(
                                f"Some entities could not be turned on because they could not be reached",
                                icon=ALERT_ICON,
                            )

                else:
                    self.lg(
                        f"{stack()[0][3]} | Lights in {self.room.name.replace('_',' ').title()} were already on"
                        f" | {self.dimming = }",
                        level=logging.DEBUG,
                    )

        elif isinstance(light_setting, str):

            # Start by iterating through all of the lights and turn them on
            for entity in lights:
                if self.log_debug:
                    self.lg(
                        f"{stack()[0][3]} | entity: {entity} | startswith: {entity.split('.')[0]} | "
                        f"is_hue_group: {self.active['is_hue_group'] and get_state(entity_id=entity, attribute='is_hue_group', copy=False)} | "
                        f"switched_on_by_automoli: {entity in self._switched_on_by_automoli}",
                        level=logging.DEBUG,
                    )
                if is_hue_group:
                    if get_state(
                        entity_id=entity, attribute="is_hue_group", copy=False
                    ):
                        call_service(
                            "hue/hue_activate_scene",
                            group_name=self.friendly_name(entity),  # type:ignore
                            scene_name=light_setting,  # type:ignore
                        )
                        # Considering that activating a scene is the equivalent of turning it on
                        if not entity in self._switched_on_by_automoli:
                            self._switched_on_by_automoli.add(entity)
                        if entity in self._switched_off_by_automoli:
                            self._switched_off_by_automoli.remove(entity)
                        at_least_one_turned_on = True
                elif get_state(entity, copy=False) == "off":
                    call_service(
                        "homeassistant/turn_on", entity_id=entity  # type:ignore
                    )
                    if not entity in self._switched_on_by_automoli:
                        self._switched_on_by_automoli.add(entity)
                    if entity in self._switched_off_by_automoli:
                        self._switched_off_by_automoli.remove(entity)
                    at_least_one_turned_on = True

            # Then if the light_setting is a scene or script apply it after
            if light_setting.startswith("scene.") or light_setting.startswith(
                "script."
            ):
                call_service(
                    "homeassistant/turn_on", entity_id=light_setting  # type:ignore
                )

            if at_least_one_turned_on:
                if source != "daytime change" and source != "<unknown>":
                    source = self.get_name(source)

                # if room is not already "on" update stats
                if self.sensor_state == "off":
                    self.sensor_state = "on"
                    self.run_in(
                        self.update_room_stats,
                        DEFAULT_UPDATE_STATS_DELAY,
                        stat="lastOn",
                        source=source,
                    )

                self.lg(
                    f"{hl(self.room.name.replace('_',' ').title())} turned {hl('on')} by {hl(source)} | "
                    f"{'hue scene:' if self.active['is_hue_group'] else ''} "
                    f"{hl(light_setting)}"
                    f" | delay: {hl(natural_time(int(self.active['delay'])))}",
                    icon=ON_ICON,
                )

                # If there are any actions to take after the lights are on then run them now
                if self.after_on:
                    self.lg(
                        f"{stack()[0][3]} | Lights are on. Now turning on the following 'after_on' entities {self.after_on}.",
                        level=logging.DEBUG,
                    )
                    not_ready = self.turn_on_entities(self.after_on)
                    if not_ready:
                        self.lg(
                            f"Some entities could not be turned on because they could not be reached",
                            icon=ALERT_ICON,
                        )

            else:
                self.lg(
                    f"{stack()[0][3]} | Nothing to turn on becuase all lights in {self.room.name.replace('_',' ').title()} were already on"
                    f" | {self.dimming = }",
                    level=logging.DEBUG,
                )

        else:
            raise ValueError(
                f"invalid brightness/scene: {light_setting!s} in {self.room}"
            )

    def lights_off(self, kwargs: dict[str, Any]) -> None:
        """Turn off the lights."""

        # check logging level here first to avoid duplicate log entries when not debug logging
        if self.log_debug:
            self.lg(
                f"{stack()[0][3]} | {self.is_disabled(onoff='off') = } | {self.is_blocked(onoff='off') = }",
                level=logging.DEBUG,
            )

        # check if automoli is disabled via home assistant entity or blockers like the "shower case"
        if self.is_disabled(onoff="off") or self.is_blocked(onoff="off"):
            # if it is blocked then refresh the timer so it is not blocked forever
            self.refresh_timer()
            return

        # cancel scheduled callbacks
        self.clear_handles()

        self.lg(
            f"{stack()[0][3]} | "
            f"{any([self.get_state(entity, copy=False) == 'on' for entity in self.lights]) = }"
            f" | {self.lights = }",
            level=logging.DEBUG,
        )

        at_least_one_turned_off = kwargs.get("one_turned_off_already", False)
        at_least_one_error = False
        for entity in self.lights:
            state = self.get_state(entity, copy=False)
            if state == "on":
                if self.only_own_events:
                    if entity in self._switched_on_by_automoli:
                        self.call_service(
                            "homeassistant/turn_off", entity_id=entity  # type:ignore
                        )  # type:ignore
                        if entity in self._switched_on_by_automoli:
                            self._switched_on_by_automoli.remove(entity)
                        if entity not in self._switched_off_by_automoli:
                            self._switched_off_by_automoli.add(entity)
                        at_least_one_turned_off = True
                else:
                    self.call_service(
                        "homeassistant/turn_off", entity_id=entity  # type:ignore
                    )  # type:ignore
                    if entity in self._switched_on_by_automoli:
                        self._switched_on_by_automoli.remove(entity)
                    if entity not in self._switched_off_by_automoli:
                        self._switched_off_by_automoli.add(entity)
                    at_least_one_turned_off = True
            elif state in NOT_READY_STATES:
                at_least_one_error = True
                self.lg(
                    f"{entity} is not ready to be turned off with state '{state}'",
                    icon=ALERT_ICON,
                )
            # State could be "off" if there are multiple lights in an AutoMoLi room but only a subset have
            # been turned on (e.g., manually or through an automation). Therefore, catching here if state is
            # not "on", "off", or in a NOT_READY_STATE (including unknown, unavailable, or none)
            elif state != "off":
                at_least_one_error = True
                self.lg(
                    f"{entity} is in an unexpected state '{state}' while being turned off",
                    icon=ALERT_ICON,
                )

        # only run if there were no errors
        if at_least_one_turned_off and not at_least_one_error:
            delay = kwargs.get("timeDelay", 0)
            daytimeChange = kwargs.get("daytimeChange", False)
            self.run_in(
                self.turned_off, 0, timeDelay=delay, daytimeChange=daytimeChange
            )

            # If there are any actions to take after the lights are off then run them now
            if self.after_off:
                self.lg(
                    f"{stack()[0][3]} | Lights are off. Now turning on the following 'after_off' entities {self.after_off}.",
                    level=logging.DEBUG,
                )
                not_ready = self.turn_on_entities(self.after_off)
                if not_ready:
                    self.lg(
                        f"Some entities could not be turned on because they could not be reached",
                        icon=ALERT_ICON,
                    )

        # experimental | reset for xiaomi "super motion" sensors | idea from @wernerhp
        # app: https://github.com/wernerhp/appdaemon_aqara_motion_sensors
        # mod:
        # https://community.smartthings.com/t/making-xiaomi-motion-sensor-a-super-motion-sensor/139806
        #
        # This code may work for these "super motion" sensors but may cause errors for other sensors.
        # If setup only uses these sensors then uncomment the following code.
        #
        # for sensor in self.sensors[EntityType.MOTION.idx]:
        #     self.set_state(
        #         sensor,
        #         state="off",
        #         attributes=(self.get_state(sensor, attribute="all")).get(
        #             "attributes", {}
        #         ),
        #     )

        if at_least_one_error:
            self.lg(
                f"Since at least one light may not have been turned off, retrying in 60 seconds.",
                icon=ALERT_ICON,
            )
            # Retry again in 60 seconds. Pass in current state if already turned one off so can finish
            # actions after turning lights off, once there are no errors.
            handle = self.run_in(
                self.lights_off, 60, one_turned_off_already=at_least_one_turned_off
            )
            self.room.handles_automoli.add(handle)

    # Global lock ensures that multiple log writes occur together after turning off lights
    @ad.global_lock
    def turned_off(self, kwargs: dict[str, Any] | None = None) -> None:
        overrideDelay = self.override_delay_active
        # cancel scheduled callbacks
        self.clear_handles()

        delay = kwargs.get("timeDelay", self.active["delay"])
        daytimeChange = kwargs.get("daytimeChange", False)
        source = ""

        if overrideDelay:
            overriddenBy = self.sensor_attr.get("delay_overridden_by", "")
            self.lg(
                f"No motion in {hl(self.room.name.replace('_',' ').title())} for "
                f"{hl(natural_time(int(delay)))} overridden by {overriddenBy} → turned {hl('off')}",
                icon=OFF_ICON,
            )
            self.run_in(
                self.update_room_stats,
                DEFAULT_UPDATE_STATS_DELAY,
                stat="overrideDelay",
                enable=False,
            )
            source = f"No motion since {overriddenBy}"
        elif daytimeChange:
            self.lg(
                f"Daytime changed light setting to 0% in "
                f"{hl(self.room.name.replace('_',' ').title())} → turned {hl('off')}",
                icon=OFF_ICON,
            )
            source = "Daytime changed light setting to 0%"
        else:
            self.lg(
                f"No motion in {hl(self.room.name.replace('_',' ').title())} for "
                f"{hl(natural_time(int(delay)))} → turned {hl('off')}",
                icon=OFF_ICON,
            )
            timeSinceMotion = (
                (natural_time(int(delay)))
                .replace("\033[1m", "")
                .replace("\033[0m", "")
                .replace("min", " min")
            )
            source = f"No motion for {timeSinceMotion}"

        # Update room stats to record room turned off
        self.sensor_state = "off"
        self.run_in(
            self.update_room_stats,
            DEFAULT_UPDATE_STATS_DELAY,
            stat="lastOff",
            source=source,
        )

        # Log last motion if there actually was motion
        lastMotionBy = self.sensor_attr.get("last_motion_by", "")
        if lastMotionBy != "":
            if (lastMotionWhen := self.sensor_attr.get("last_motion_cleared", "")) == 0:
                lastMotionWhen = self.sensor_attr.get(
                    "last_motion_detected", "<unknown>"
                )
                self.lg(f"Last motion from {lastMotionBy} at {lastMotionWhen}.")

        # Log how long lights were on
        currentTime = datetime.now()
        try:
            lastOn = datetime.strptime(
                self.sensor_attr["last_turned_on"], DATETIME_FORMAT
            )
        except KeyError:
            lastOn = currentTime
            self.lg(
                f"{stack()[0][3]} | There is no record of the lights already being on",
                level=logging.DEBUG,
            )
        difference = int(datetime.timestamp(datetime.now())) - int(
            datetime.timestamp(lastOn)
        )
        self.lg(
            f"{hl(self.room_name.replace('_',' ').title())} was on for "
            f"{self.seconds_to_time(difference, True)} since {lastOn.strftime(DATETIME_FORMAT)}."
        )

    def turn_on_entities(self, entities: set[str]) -> set | None:
        """This is a helper function to turn on a set of entities.
        Precondition: Assume all entities are valid since already checked during initialization.
        For each entity, it will attempt to 'turn on' the entity.  Supports any
        entities that can be turned on with the generic homeassistant/turn_on service.
        As many entities as can be processed will be. Returns a set of any entities that were not ready.
        """
        not_ready: set[str] = set()

        for entity in entities:
            state = self.get_state(entity, copy=False)
            if state in NOT_READY_STATES:
                not_ready.add(entity)
            else:
                self.turn_on(entity)

        return not_ready if len(not_ready) > 0 else None

    def warning_flash_off(self, _: dict[str, Any] | None = None) -> None:
        # check if automoli is disabled via home assistant entity or blockers like the "shower case"
        # if so then don't do the warning flash
        if self.is_disabled(onoff="off") or self.is_blocked(onoff="off"):
            return

        self.lg(
            f"{stack()[0][3]} | Lights will be turned off in {hl(self.room.name.replace('_',' ').title())} in "
            f"{DEFAULT_WARNING_DELAY} seconds → flashing warning",
            level=logging.DEBUG,
        )

        # turn off lights that are on and save those in self._warning_lights to turn back on
        at_least_one_turned_off = False
        for entity in self.lights:
            if self.get_state(entity, copy=False) == "on":
                self._warning_lights.add(entity)
                self.call_service(
                    "homeassistant/turn_off", entity_id=entity  # type:ignore
                )  # type:ignore
                at_least_one_turned_off = True

        # turn lights on again in 1s
        if at_least_one_turned_off:
            self.run_in(self.warning_flash_on, 1)

    def warning_flash_on(self, _: dict[str, Any] | None = None) -> None:
        # turn lights back on after 1s delay
        for entity in self._warning_lights:
            self.call_service(
                "homeassistant/turn_on", entity_id=entity  # type:ignore
            )  # type:ignore
        # wait 1 second to clear the variable that held the lights to turn back on;
        # the wait is so outside_change_detected will recognize that this was done by AutoMoLi
        self.run_in(lambda kwargs: self._warning_lights.clear(), 1)

    def find_sensors(
        self, keyword: str, room_name: str, states: dict[str, dict[str, Any]]
    ) -> list[str]:
        """Find sensors by looking for a keyword in the friendly_name."""

        def lower_umlauts(text: str, single: bool = True) -> str:
            return (
                text.replace("ä", "a")
                .replace("ö", "o")
                .replace("ü", "u")
                .replace("ß", "s")
                if single
                else text.replace("ä", "ae")
                .replace("ö", "oe")
                .replace("ü", "ue")
                .replace("ß", "ss")
            ).lower()

        matches: list[str] = []
        for state in states.values():
            if keyword in (entity_id := state.get("entity_id", "")) and lower_umlauts(
                room_name
            ) in "|".join(
                [
                    entity_id,
                    lower_umlauts(state.get("attributes", {}).get("friendly_name", "")),
                ]
            ):
                matches.append(entity_id)

        return matches

    def configure_night_mode(
        self, night_mode: dict[str, int | str]
    ) -> dict[str, int | str]:

        # check if a enable/disable entity is given and exists
        if not (
            (nm_entity := night_mode.pop("entity")) and self.entity_exists(nm_entity)
        ):
            self.lg(
                f"{stack()[0][3]} | No night_mode entity given", level=logging.DEBUG
            )
            return {}

        if not (nm_light_setting := night_mode.pop("light")):
            return {}

        return {"entity": nm_entity, "light": nm_light_setting}

    def build_daytimes(self, daytimes: list[Any]) -> list[dict[str, int | str]] | None:
        starttimes: set[time] = set()

        for idx, daytime in enumerate(daytimes):
            dt_name = daytime.get("name", f"{DEFAULT_NAME}_{idx}")
            dt_delay = daytime.get("delay", self.delay)
            dt_light_setting = daytime.get("light", DEFAULT_LIGHT_SETTING)
            if self.disable_hue_groups:
                dt_is_hue_group = False
            else:
                dt_is_hue_group = (
                    isinstance(dt_light_setting, str)
                    and not dt_light_setting.startswith("scene.")
                    and not dt_light_setting.startswith("script.")
                    and any(
                        self.get_state(
                            entity_id=entity, attribute="is_hue_group", copy=False
                        )
                        for entity in self.lights
                    )
                )

            dt_start: time
            try:
                starttime = str(daytime.get("starttime"))
                if starttime.count(":") == 1:
                    starttime += ":00"
                dt_start = (self.parse_time(starttime)).replace(microsecond=0)
                daytime["starttime"] = dt_start
            except ValueError as error:
                raise ValueError(
                    f"missing start time in daytime '{dt_name}': {error}"
                ) from error

            # configuration for this daytime
            daytime = dict(
                daytime=dt_name,
                delay=dt_delay,
                starttime=dt_start.isoformat(),  # datetime is not serializable
                light_setting=dt_light_setting,
                is_hue_group=dt_is_hue_group,
            )

            # info about next daytime
            next_dt_name = DEFAULT_NAME
            try:
                next_starttime = str(
                    daytimes[(idx + 1) % len(daytimes)].get("starttime")
                )
                if next_starttime.count(":") == 1:
                    next_starttime += ":00"
                next_dt_name = str(daytimes[(idx + 1) % len(daytimes)].get("name"))
                next_dt_start = (self.parse_time(next_starttime)).replace(microsecond=0)
            except ValueError as error:
                raise ValueError(
                    f"missing start time in daytime '{next_dt_name}': {error}"
                ) from error

            # collect all start times for sanity check
            if dt_start in starttimes:
                raise ValueError(
                    f"Start times of all daytimes have to be unique! "
                    f"Duplicate found: {dt_start}"
                )

            starttimes.add(dt_start)

            # check if this daytime should be active now (and default true if only 1 daytime is provided)
            if (
                self.now_is_between(str(dt_start), str(next_dt_start))
                or len(daytimes) == 1
            ):
                self.switch_daytime(dict(daytime=daytime, initial=True))
                self.active_daytime = daytime.get("daytime")

            # schedule callbacks for daytime switching
            self.run_daily(
                self.switch_daytime,
                dt_start,
                random_start=-RANDOMIZE_SEC,
                random_end=RANDOMIZE_SEC,
                **dict(daytime=daytime),
            )

        return daytimes

    def show_info(self, config: dict[str, Any] | None = None) -> None:
        # check if a room is given

        if config:
            self.config = config

        if not self.config:
            self.lg(
                f"{stack()[0][3]} | No configuration available",
                icon="‼️",
                level=logging.ERROR,
            )
            return

        room = ""
        if "room" in self.config:
            room = f" · {hl(self.config['room'].capitalize())}"

        self.lg("", log_to_ha=False)
        self.lg(
            f"{hl(APP_NAME)} v{hl(__version__)}{room}", icon=self.icon, log_to_ha=False
        )
        self.lg("", log_to_ha=False)

        listeners = self.config.pop("listeners", None)

        for key, value in self.config.items():

            # hide "internal keys" when displaying config
            if key in ["module", "class"] or key.startswith("_"):
                continue

            if isinstance(value, (list, set)):
                self.print_collection(key, value, 2)
            elif isinstance(value, dict):
                self.print_collection(key, value, 2)
            else:
                self._print_cfg_setting(key, value, 2)

        if listeners:
            self.lg("  event listeners:", log_to_ha=False)
            for listener in sorted(listeners):
                self.lg(f"    · {hl(listener)}", log_to_ha=False)

        self.lg("", log_to_ha=False)

    def print_collection(
        self, key: str, collection: Iterable[Any], indentation: int = 0
    ) -> None:

        self.lg(f"{indentation * ' '}{key}:", log_to_ha=False)
        indentation = indentation + 2

        for item in collection:
            indent = indentation * " "

            if isinstance(item, dict):

                if "name" in item:
                    self.print_collection(item.pop("name", ""), item, indentation)
                else:
                    self.lg(
                        f"{indent}{hl(pformat(item, compact=True))}", log_to_ha=False
                    )

            elif isinstance(collection, dict):

                if isinstance(collection[item], set):
                    self.print_collection(item, collection[item], indentation)
                else:
                    self._print_cfg_setting(item, collection[item], indentation)

            else:
                self.lg(f"{indent}· {hl(item)}", log_to_ha=False)

    def _print_cfg_setting(self, key: str, value: int | str, indentation: int) -> None:
        unit = prefix = ""
        indent = indentation * " "

        # legacy way
        if (
            key == "delay" or key == "delay_outside_events" or key == "override_delay"
        ) and isinstance(value, int):
            unit = "min"
            min_value = f"{int(value / 60)}:{int(value % 60):02d}"
            self.lg(
                f"{indent}{key}: {prefix}{hl(min_value)}{unit} ≈ " f"{hl(value)}sec",
                ascii_encode=False,
                log_to_ha=False,
            )

        else:
            if "_units" in self.config and key in self.config["_units"]:
                unit = self.config["_units"][key]
            if "_prefixes" in self.config and key in self.config["_prefixes"]:
                prefix = self.config["_prefixes"][key]

            self.lg(f"{indent}{key}: {prefix}{hl(value)}{unit}", log_to_ha=False)

    def get_name(self, entity_id: str) -> str:
        return self.get_state(
            entity_id, attribute="friendly_name", default=entity_id, copy=False
        )

    def room_event(self, event: str, data: dict[str, str], _: dict[str, Any]) -> None:
        if event == EVENT_AUTOMOLI_STATS:
            room = data.get("room", "")
            if (
                room == ""
                or room.capitalize() == "All"
                or (room.capitalize() == (self.room_name).capitalize())
            ):
                # Since print_room_stats has kwargs and not **kwargs calling run_in with delay = 0
                self.run_in(self.print_room_stats, 0)

    #########################  Room Statistics ########################
    # "friendly_name":  Friendly name of the sensor, e.g., "Kitchen Statistics"
    # "time_lights_on_today": Total time lights in the room have been on today
    # "last_turned_on": Last time room was turned on
    # "last_turned_off": Last time room was turned off
    # "times_turned_on_by_automoli": Count of how many times AutoMoLi turned lights on
    # "times_turned_off_by_automoli": Count of how many times AutoMoLi turned lights off
    # "times_turned_on_by_automations": Count of how many times HomeAssistant automations turned lights on
    # "times_turned_off_by_automations": Count of how many times HomeAssistant automations turned lights off
    # "times_turned_on_manually": Count of how many times lights were turned on manually
    # "times_turned_off_manually": Count of how many times lights were turned off manually
    # "turned_on_by": Source that caused lights/room to be turned on
    # "turned_off_by": Source that caused lights/room to be turned off
    # "last_motion_detected": Last time motion was detected
    # "last_motion_by": Entity from which motion detected
    # "turning_off_at": Room will be turned off at this time
    # "delay_overridden_by": Entity that caused delay to be overridden
    # "blocked_on_by": Entity that is currently blocking the light to be turned on
    # "blocked_off_by": Entity that is currently blocking the light from being turned off
    # "disabled_by": Entity that is currently disabling AutoMoLi
    # "current_light_setting": Current % that the light will be turned on
    # "debug_message": Add attribute that logs last stat that was updated
    #
    # Notes:
    # - Manual/automoli counts can get out of sync if a room has multiple lights/switches
    # because automoli changes will be counted once for all lights in the room, but manual changes
    # will be counted individually per light. Can avoid this problem by separating each switch into
    # its own room.
    # - "last turned on" and "times turned on ..." will not change if the light remains on but
    # there is an attribute change (e.g., the color of the light changes).

    def init_room_stats(self, _: Any | None = None) -> None:
        entity = self.get_state(self.entity_id, copy=False)
        self.sensor_attr["friendly_name"] = (
            self.room_name.replace("_", " ").title() + " Statistics"
        )

        # Only initialize if AutoMoli stats entity doesn't exist or if last update was before today
        if entity == None:
            self.sensor_attr["time_lights_on_today"] = self.seconds_to_time(
                self.sensor_onToday
            )

            light_setting = (
                self.active.get("light_setting")
                if not bool(
                    self.night_mode
                    and self.get_state(self.night_mode["entity"], copy=False) == "on"
                )
                else self.night_mode.get("light")
            )
            if light_setting != None:
                current_light_setting = str(light_setting)
                if isinstance(light_setting, int):
                    current_light_setting = current_light_setting + "%"
                self.sensor_attr["current_light_setting"] = current_light_setting

        else:
            # Check if sensor was last updated before today
            today = date.today()
            lastUpdated: datetime = self.convert_utc(
                self.get_state(self.entity_id, attribute="last_updated", copy=False)
            )
            local_timezone = tz.tzlocal()
            lastUpdatedLocal = lastUpdated.astimezone(local_timezone)
            lastUpdatedDate = date(
                lastUpdatedLocal.year, lastUpdatedLocal.month, lastUpdatedLocal.day
            )
            if today != lastUpdatedDate:
                self.reset_room_stats()
                return
            else:
                # Read daily statistics from existing sensor
                self.sensor_attr["time_lights_on_today"] = self.get_state(
                    self.entity_id,
                    attribute="time_lights_on_today",
                    default="00:00:00",
                    copy=False,
                )

                self.sensor_onToday = (
                    datetime.strptime(
                        self.sensor_attr["time_lights_on_today"], TIME_FORMAT
                    )
                    - datetime(1900, 1, 1)
                ).total_seconds()
                if (
                    countAutomoliOn := self.get_state(
                        self.entity_id,
                        "times_turned_on_by_automoli",
                        default=0,
                        copy=False,
                    )
                ) != 0:
                    self.sensor_attr["times_turned_on_by_automoli"] = countAutomoliOn
                if (
                    countAutomoliOff := self.get_state(
                        self.entity_id,
                        "times_turned_off_by_automoli",
                        default=0,
                        copy=False,
                    )
                ) != 0:
                    self.sensor_attr["times_turned_off_by_automoli"] = countAutomoliOff
                if (
                    countAutomationOn := self.get_state(
                        self.entity_id,
                        "times_turned_on_by_automations",
                        default=0,
                        copy=False,
                    )
                ) != 0:
                    self.sensor_attr["times_turned_on_by_automations"] = (
                        countAutomationOn
                    )
                if (
                    countAutomationOff := self.get_state(
                        self.entity_id,
                        "times_turned_off_by_automations",
                        default=0,
                        copy=False,
                    )
                ) != 0:
                    self.sensor_attr["times_turned_off_by_automations"] = (
                        countAutomationOff
                    )
                if (
                    countManualOn := self.get_state(
                        self.entity_id,
                        "times_turned_on_manually",
                        default=0,
                        copy=False,
                    )
                ) != 0:
                    self.sensor_attr["times_turned_on_manually"] = countManualOn
                if (
                    countManualOff := self.get_state(
                        self.entity_id,
                        "times_turned_off_manually",
                        default=0,
                        copy=False,
                    )
                ) != 0:
                    self.sensor_attr["times_turned_off_manually"] = countManualOff

        self.sensor_state = (
            "on"
            if any(
                [self.get_state(entity, copy=False) == "on" for entity in self.lights]
            )
            else "off"
        )

        # Set last_turned_on to now() if light is on when initialize
        if self.sensor_state == "on":
            currentTime = datetime.now()
            currentTimeStr = currentTime.strftime(DATETIME_FORMAT)
            self.sensor_attr["last_turned_on"] = currentTimeStr

        # Remove debug message if debugging is off
        if (logging.DEBUG < self.loglevel) and not self.force_logging:
            self.sensor_attr.pop("debug_message", 0)

        if self.track_room_stats:
            self.set_state(
                entity_id=self.entity_id,
                state=self.sensor_state,
                attributes=self.sensor_attr,
                replace=True,
            )

    def reset_room_stats(self, _: Any | None = None) -> None:
        self.sensor_onToday = 0
        self.sensor_attr["time_lights_on_today"] = self.seconds_to_time(
            self.sensor_onToday
        )

        # If lights are on, check if they were last turned on by automoli or manually
        # If a restart happened and reset is called, assume lights were turned on manually
        if any([self.get_state(entity, copy=False) == "on" for entity in self.lights]):
            self.sensor_state = "on"
            if len(self._switched_on_by_automoli) > 0:
                self.sensor_attr["times_turned_on_by_automoli"] = 1
                self.sensor_attr.pop("times_turned_on_manually", 0)
                self.sensor_attr.pop("times_turned_on_by_automations", 0)
            else:
                self.sensor_attr.pop("times_turned_on_by_automoli", 0)
                self.sensor_attr["times_turned_on_manually"] = 1
                self.sensor_attr.pop("times_turned_on_by_automations", 0)
        else:
            self.sensor_state = "off"
            self.clear_handles(self.sensor_update_handles)
            self.sensor_update_handles.clear()
            self.sensor_attr.pop("times_turned_on_by_automoli", 0)
            self.sensor_attr.pop("times_turned_on_by_automations", 0)
            self.sensor_attr.pop("times_turned_on_manually", 0)

        self.sensor_attr.pop("times_turned_off_by_automoli", 0)
        self.sensor_attr.pop("times_turned_off_by_automations", 0)
        self.sensor_attr.pop("times_turned_off_manually", 0)

        if self.track_room_stats:
            self.set_state(
                entity_id=self.entity_id,
                state=self.sensor_state,
                attributes=self.sensor_attr,
                replace=True,
            )

    def update_room_stats(self, kwargs: dict[str, Any] | None = None):
        howChanged = kwargs.get("howChanged", "automoli") if kwargs else "automoli"
        stat = kwargs.get("stat", None) if kwargs else None
        # All calls to update_room_stats are made with a delay of 1 second, to avoid interrupting
        # the code flow (especially for time sensitive code paths like turning lights on).
        # Therefore, remove 1 second from currentTime to log the time the action actually occurred.
        updateDelay = (
            kwargs.get("updateDelay", DEFAULT_UPDATE_STATS_DELAY)
            if kwargs
            else DEFAULT_UPDATE_STATS_DELAY
        )
        currentTime = datetime.now() - timedelta(seconds=updateDelay)
        currentTimeStr = currentTime.strftime(DATETIME_FORMAT)

        # stat will be a dictionary if update_room_stats is called from run_in
        if isinstance(stat, dict):
            stat = dict(stat).get("stat", "")

        if stat == "motion_detected":
            self.sensor_attr["last_motion_detected"] = currentTimeStr
            self.sensor_attr["last_motion_by"] = self.get_name(kwargs.get("entity"))
            self.sensor_attr.pop("last_motion_cleared", "")
            self.sensor_attr["turning_off_at"] = "Waiting for motion to clear"

        elif stat == "motion_cleared":
            self.sensor_attr["last_motion_cleared"] = currentTimeStr
            self.sensor_attr["last_motion_by"] = self.get_name(kwargs.get("entity"))
            self.sensor_attr.pop("last_motion_detected", "")
            # Clearing "Waiting for motion to clear" before refresh timer call
            # sets real turning_off_at time. Setting text here to handle debugging.
            # But if there is an override delay running, then clearing motion should just
            # return to the previous turning_off_at value.
            if self.override_delay_active:
                self.sensor_attr["turning_off_at"] = self.sensor_lastTurningOffAt
            else:
                self.sensor_attr["turning_off_at"] = "Motion cleared, recalculating..."

        elif stat == "motion_event":
            self.sensor_attr["last_motion_cleared"] = currentTimeStr
            self.sensor_attr["last_motion_by"] = self.get_name(kwargs.get("entity"))
            # Setting text here to handle debugging. But if there is an override delay running,
            # then a motion event should just return to the previous turning_off_at value.
            if self.override_delay_active:
                self.sensor_attr["turning_off_at"] = self.sensor_lastTurningOffAt
            else:
                self.sensor_attr["turning_off_at"] = "Motion cleared, recalculating..."

        elif stat == "lastOn":
            self.sensor_attr["last_turned_on"] = currentTimeStr
            countAutomoliOn = self.sensor_attr.get("times_turned_on_by_automoli", 0)
            countAutomationOn = self.sensor_attr.get(
                "times_turned_on_by_automations", 0
            )
            countManualOn = self.sensor_attr.get("times_turned_on_manually", 0)
            if howChanged == "automoli":
                # do not update automoli count on reboot unless nothing has been counted
                # then assume automoli turned it on
                if not kwargs.get("appInit", False) or (
                    countAutomoliOn + countAutomationOn + countManualOn == 0
                ):
                    self.sensor_attr["times_turned_on_by_automoli"] = (
                        countAutomoliOn + 1
                    )
            elif howChanged == "automation":
                self.sensor_attr["times_turned_on_by_automations"] = (
                    countAutomationOn + 1
                )
                self.sensor_attr.pop("last_motion_detected", "")
                self.sensor_attr.pop("last_motion_cleared", "")
                self.sensor_attr.pop("last_motion_by", "")
            elif howChanged == "manually":
                self.sensor_attr["times_turned_on_manually"] = countManualOn + 1
                self.sensor_attr.pop("last_motion_detected", "")
                self.sensor_attr.pop("last_motion_cleared", "")
                self.sensor_attr.pop("last_motion_by", "")
            source = kwargs.get("source", "<unknown>")
            self.sensor_attr["last_turned_on_by"] = source
            self.sensor_attr.pop("delay_overridden_by", "")
            self.sensor_attr.pop("blocked_on_by", "")
            self.sensor_attr.pop("disabled_by", "")

            # Update onToday again every minute until light is off
            # If there is already an update timer running, cancel it (e.g., if there are multiple lights
            # in the same room and one of them was already turned on).
            self.clear_handles(self.sensor_update_handles)
            self.sensor_update_handles.clear()
            handle = self.run_every(
                self.update_room_stats, "now+60", 60, stat="updateEveryMin"
            )
            self.sensor_update_handles.add(handle)
            self.lg(
                f"{stack()[0][3]} | Scheduling call to update stats every minute | {self.sensor_update_handles = }",
                level=logging.DEBUG,
            )

        elif stat == "lastOff":
            self.clear_handles(self.sensor_update_handles)
            self.sensor_update_handles.clear()

            self.sensor_attr["last_turned_off"] = currentTimeStr
            self.sensor_onToday = int(self.sensor_onToday) + int(self.time_lights_on())
            self.sensor_attr["time_lights_on_today"] = self.seconds_to_time(
                self.sensor_onToday
            )
            if howChanged == "automoli":
                # do not update automoli count on reboot
                if not kwargs.get("appInit", False):
                    countAutomoliOff = self.sensor_attr.get(
                        "times_turned_off_by_automoli", 0
                    )
                    self.sensor_attr["times_turned_off_by_automoli"] = (
                        countAutomoliOff + 1
                    )
            elif howChanged == "automation":
                countAutomationOff = self.sensor_attr.get(
                    "times_turned_off_by_automations", 0
                )
                self.sensor_attr["times_turned_off_by_automations"] = (
                    countAutomationOff + 1
                )
            elif howChanged == "manually":
                countManualOff = self.sensor_attr.get("times_turned_off_manually", 0)
                self.sensor_attr["times_turned_off_manually"] = countManualOff + 1
            source = kwargs.get("source", "<unknown>")
            self.sensor_attr["last_turned_off_by"] = source
            self.sensor_attr.pop("turning_off_at", "")
            self.sensor_attr.pop("blocked_off_by", "")
            if self.sensor_attr.get("disabled_by", "") != "Cooling down":
                self.sensor_attr.pop("disabled_by", "")

        elif stat == "overrideDelay":
            if kwargs.get("enable"):
                self.sensor_attr["delay_overridden_by"] = self.get_name(
                    kwargs.get("entity")
                )
            else:
                self.sensor_attr.pop("delay_overridden_by", "")

        elif stat == "refreshTimer":
            time = kwargs.get("time")
            # If time is -1 then lights were not actually turned on when refresh_timer
            # function was called so no need to track turning_off_at
            if time == -1:
                self.sensor_attr.pop("turning_off_at", "")
            elif time == 0:
                turningOffAt = "Lights have to be switched off manually"
                self.sensor_attr["turning_off_at"] = turningOffAt
                self.sensor_lastTurningOffAt = turningOffAt
            else:
                # Ensure time is timezone-aware and in UTC before converting to local timezone
                local_timezone = tz.tzlocal()
                if time:
                    if getattr(time, "tzinfo", None) is None:
                        time = time.replace(tzinfo=tz.UTC)
                    time_local = time.astimezone(local_timezone)
                    turningOffAt = time_local.strftime(DATETIME_FORMAT)
                else:
                    turningOffAt = "<unknown>"
                self.sensor_attr["turning_off_at"] = turningOffAt
                self.sensor_lastTurningOffAt = turningOffAt

        elif stat == "switchDaytime":
            light_setting = (
                self.active.get("light_setting")
                if not bool(
                    self.night_mode
                    and self.get_state(self.night_mode["entity"], copy=False) == "on"
                )
                else self.night_mode.get("light")
            )
            current_light_setting = str(light_setting)
            if isinstance(light_setting, int):
                current_light_setting = current_light_setting + "%"
            self.sensor_attr["current_light_setting"] = current_light_setting

        elif stat == "blockedOn":
            if self.entity_exists(entity := kwargs.get("entity")):
                self.sensor_attr["blocked_on_by"] = self.get_name(entity)
            else:
                self.sensor_attr["blocked_on_by"] = entity

        elif stat == "blockedOff":
            if self.entity_exists(entity := kwargs.get("entity")):
                self.sensor_attr["blocked_off_by"] = self.get_name(entity)
            else:
                self.sensor_attr["blocked_off_by"] = entity

        elif stat == "disabled":
            if self.entity_exists(entity := kwargs.get("entity")):
                self.sensor_attr["disabled_by"] = self.get_name(entity)
            else:
                self.sensor_attr["disabled_by"] = entity

        elif stat == "onlyOwnEventsBlock":
            self.sensor_attr["blocked_off_by"] = "Manually turned on"

        elif stat == "cooldownOff":
            if self.sensor_attr.get("disabled_by", "") == "Cooling down":
                self.sensor_attr.pop("disabled_by", "")

        elif stat == "forceLoggingOff":
            self.sensor_attr.pop("debug_message", 0)

        # If the room is still on, record all the time it was on until now
        adjustedOnToday = int(self.sensor_onToday)
        if self.sensor_state == "on":
            adjustedOnToday += int(self.time_lights_on())
            self.sensor_attr["time_lights_on_today"] = self.seconds_to_time(
                adjustedOnToday
            )

        if self.log_debug:
            debug_message = (
                f"{stat} | now: {datetime.now().strftime('%H:%M:%S.%f')}"
                f" | time on today: {self.seconds_to_time(adjustedOnToday)} | { kwargs.get('message', '')}"
            )
            self.sensor_attr["debug_message"] = debug_message

        if self.track_room_stats:
            self.set_state(
                entity_id=self.entity_id,
                state=self.sensor_state,
                attributes=self.sensor_attr,
                replace=True,
            )

        self.lg(
            f"{stack()[0][3]} | Called by '{stat}' and updated state to {self.sensor_attr}",
            level=logging.DEBUG,
        )

        # Adding debugging to check if something unexpected happened
        if stat == "updateEveryMin":
            self.debug_room_stats(stat)

    # Room Stats can be used to find potential issues or inconsistencies
    # Right now only evaluating stats during "updateEveryMin"
    # If lights are on but shouldn't be, this is a last resort to try to force them off
    def debug_room_stats(self, stat: str) -> None:
        # As above, subtract 1 second since update_room_stats is called with a 1 second delay
        currentTime = datetime.now() - timedelta(seconds=1)
        forceLightsOff = False
        error = False

        try:
            turningOffAt = datetime.strptime(
                self.sensor_attr["turning_off_at"], DATETIME_FORMAT
            )
            # Adding 60 seconds below because updateEveryMin can trigger before the light actually has time to turn off
            if datetime.timestamp(currentTime) > (
                datetime.timestamp(turningOffAt) + 60
            ):
                # Check if all the lights are actually off but the state is wrong
                if all(
                    [
                        self.get_state(light, copy=False) == "off"
                        for light in self.lights
                    ]
                ):

                    if self.last_room_stats_error != "ROOM_ON_UNEXPECTED":
                        self.lg(
                            f"Room state is 'on' but all of the lights are off.  Updating the state and cancelling timers to not continue to update.",
                            icon=ALERT_ICON,
                        )
                        self.last_room_stats_error = "ROOM_ON_UNEXPECTED"

                    self.sensor_state = "off"
                    self.clear_handles(self.sensor_update_handles)
                    self.sensor_update_handles.clear()
                    self.sensor_attr["last_turned_off_by"] = (
                        "Error: Room was not actually on"
                    )
                    self.sensor_attr.pop("turning_off_at", "")
                    self.sensor_attr.pop("blocked_off_by", "")
                    if self.sensor_attr.get("disabled_by", "") != "Cooling down":
                        self.sensor_attr.pop("disabled_by", "")
                    if self.track_room_stats:
                        self.set_state(
                            entity_id=self.entity_id,
                            state=self.sensor_state,
                            attributes=self.sensor_attr,
                            replace=True,
                        )
                else:
                    self.lg(
                        f"Lights should have been turned off at {self.sensor_attr['turning_off_at']} but they are still on. Trying to force them off now.",
                        icon=ALERT_ICON,
                    )
                    forceLightsOff = True
            else:
                self.last_room_stats_error = "NO_ERROR"
        except KeyError:
            if self.sensor_state == "off":
                # Timer shouldn't still be running but catching an edge case

                if self.last_room_stats_error != "ROOM_OFF_UNEXPECTED":
                    self.lg(
                        f"Room state is 'off' but update every minute is still being called.  Trying to cancel timers to not continue to update.",
                        icon=ALERT_ICON,
                    )
                    self.last_room_stats_error = "ROOM_OFF_UNEXPECTED"

                self.clear_handles(self.sensor_update_handles)
                self.sensor_update_handles.clear()
            elif self.sensor_state == "on":
                # Check if all the lights are actually off but the state is wrong
                if all(
                    [
                        self.get_state(light, copy=False) == "off"
                        for light in self.lights
                    ]
                ):

                    if self.last_room_stats_error != "ROOM_ON_UNEXPECTED":
                        self.lg(
                            f"Room state is 'on' but all of the lights are off.  Updating the state and cancelling timers to not continue to update.",
                            icon=ALERT_ICON,
                        )
                        self.last_room_stats_error = "ROOM_ON_UNEXPECTED"

                    self.sensor_state = "off"
                    self.clear_handles(self.sensor_update_handles)
                    self.sensor_update_handles.clear()
                    self.sensor_attr["last_turned_off_by"] = (
                        "Error: Room was not actually on"
                    )
                    self.sensor_attr.pop("turning_off_at", "")
                    self.sensor_attr.pop("blocked_off_by", "")
                    if self.sensor_attr.get("disabled_by", "") != "Cooling down":
                        self.sensor_attr.pop("disabled_by", "")
                    if self.track_room_stats:
                        self.set_state(
                            entity_id=self.entity_id,
                            state=self.sensor_state,
                            attributes=self.sensor_attr,
                            replace=True,
                        )
                else:
                    if self.last_room_stats_error != "TURNING_OFF_AT_NOT_SET":
                        self.lg(
                            f"Lights are on but there is no time set for when they should be turned off. Check to make sure everything is working as expected.",
                            icon=ALERT_ICON,
                        )
                        self.last_room_stats_error = "TURNING_OFF_AT_NOT_SET"
            else:
                if self.last_room_stats_error != "UNEXPECTED_STATE":
                    self.lg(
                        f"The room's state is '{self.sensor_state}' which was not expected. Check to make sure everything is working as expected.",
                        icon=ALERT_ICON,
                    )
                    self.last_room_stats_error = "UNEXPECTED_STATE"
        except ValueError:
            turningOffText = self.sensor_attr["turning_off_at"]
            if turningOffText == "Waiting for motion to clear":
                try:
                    lastMotionDetected = datetime.strptime(
                        self.sensor_attr["last_motion_detected"], DATETIME_FORMAT
                    )
                    if datetime.timestamp(currentTime) > (
                        datetime.timestamp(lastMotionDetected) + 3600
                    ):
                        if (
                            self.last_room_stats_error
                            != "MOTION_NOT_CLEARED_UNEXPECTED"
                        ):
                            self.lg(
                                f"Motion has not cleared in this room for over an hour. Check to make sure everything is working as expected.",
                                icon=ALERT_ICON,
                            )
                            self.last_room_stats_error = "MOTION_NOT_CLEARED_UNEXPECTED"
                except:
                    error = True
            elif turningOffText == "Motion cleared, recalculating...":
                try:
                    lastMotionCleared = datetime.strptime(
                        self.sensor_attr["last_motion_cleared"], DATETIME_FORMAT
                    )
                    if datetime.timestamp(currentTime) > (
                        datetime.timestamp(lastMotionCleared) + 300
                    ):
                        if self.last_room_stats_error != "MOTION_CLEARED_UNEXPECTED":
                            self.lg(
                                f"Motion has cleared in this room but a new time to turn off the lights has not yet been set even after 5 minutes. Check to make sure everything is working as expected.",
                                icon=ALERT_ICON,
                            )
                            self.last_room_stats_error = "MOTION_CLEARED_UNEXPECTED"
                except:
                    error = True
            # If text is "Lights have to be switched off manually" then the caller intentionally did not set a delay.
            # So this should not be considered an error but update_room_stats will continue to be called every minute
            # to update the amount of time the lights have been on.
            elif turningOffText == "Lights have to be switched off manually":
                self.last_room_stats_error = "NO_ERROR"
                error = False
            else:
                error = True
        except:
            error = True

        if error:
            if self.last_room_stats_error != "UNEXPECTED_ERROR":
                self.lg(
                    f"The room's statistics are in a weird state. Check to make sure everything is working as expected.",
                    icon=ALERT_ICON,
                )
                self.last_room_stats_error = "UNEXPECTED_ERROR"
        if forceLightsOff:
            self.run_in(self.lights_off, 0)

    # Global lock ensures that multiple log writes occur together when printing room stats
    @ad.global_lock
    def print_room_stats(self, kwargs: dict[str, Any] | None = None) -> None:
        currentTime = datetime.now()
        adjustedOnToday = int(self.sensor_onToday)

        if self.sensor_state == "on":
            # The room is still on, record all the time it was on until now
            lastOn = currentTime
            lastTurnedOn = self.sensor_attr.get("last_turned_on", "")
            if lastTurnedOn != "":
                lastOn = datetime.strptime(
                    self.sensor_attr["last_turned_on"], DATETIME_FORMAT
                )
            adjustedOnToday = adjustedOnToday + (
                int(datetime.timestamp(currentTime)) - int(datetime.timestamp(lastOn))
            )

        if int(adjustedOnToday) != 0:
            # Print out the current stats
            automoliOn = self.sensor_attr.get("times_turned_on_by_automoli", 0)
            automationOn = self.sensor_attr.get("times_turned_on_by_automations", 0)
            automationOff = self.sensor_attr.get("times_turned_off_by_automations", 0)
            manualOn = self.sensor_attr.get("times_turned_on_manually", 0)
            manualOff = self.sensor_attr.get("times_turned_off_manually", 0)
            totalOn = automoliOn + automationOn + manualOn
            self.lg(
                f"{hl(self.room_name.replace('_',' ').title())} turned on "
                f"{totalOn} time(s) for {self.seconds_to_time(adjustedOnToday)} today"
            )
            if automationOn > 0 or manualOn > 0:
                message = ""
                if automationOn > 0:
                    message = (
                        message
                        + f"by automations {automationOn} time(s) "
                        + f"{'and ' if manualOn > 0 else ''}"
                    )
                if manualOn > 0:
                    message = message + f"manually {manualOn} time(s)"

                self.lg(
                    f"{hl(self.room_name.replace('_',' ').title())} was turned on {message}"
                )
            if automationOff > 0 or manualOff > 0:
                message = ""
                if automationOff > 0:
                    message = (
                        message
                        + f"by automations {automationOff} time(s) "
                        + f"{'and ' if manualOff > 0 else ''}"
                    )
                if manualOff > 0:
                    message = message + f"manually {manualOff} time(s)"
                self.lg(
                    f"{hl(self.room_name.replace('_',' ').title())} was turned off {message}"
                )

    def time_lights_on(self) -> int:
        # returns number of seconds the room has been on
        # since turned on or since midnight, whichever came last

        currentTime = datetime.now()
        # Python community recommend a strategy of
        # "easier to ask for forgiveness than permission"
        # https://stackoverflow.com/a/610923/13180763
        try:
            lastOn = datetime.strptime(
                self.sensor_attr["last_turned_on"], DATETIME_FORMAT
            )
        except KeyError:
            lastOn = currentTime
            self.lg(
                f"{stack()[0][3]} | The lights have not yet been turned on",
                level=logging.DEBUG,
            )

        # If last_turned_on was yesterday, record from midnight
        today = date.today()
        lastOnDate = date(lastOn.year, lastOn.month, lastOn.day)
        if today != lastOnDate:
            lastOn = datetime(today.year, today.month, today.day, 0, 0, 0)

        return int(datetime.timestamp(currentTime)) - int(datetime.timestamp(lastOn))

    def seconds_to_time(self, total, includeDays=False):
        if includeDays:
            days = int(total // (24 * 3600))
        total %= 24 * 3600
        hours = int(total // 3600)
        total %= 3600
        minutes = int(total // 60)
        total %= 60
        seconds = int(total)
        if (includeDays == False) or (days == 0):
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        elif days == 1:
            return f"{days} day, {hours:02d}:{minutes:02d}:{seconds:02d}"
        elif days > 1:
            return f"{days} days, {hours:02d}:{minutes:02d}:{seconds:02d}"
