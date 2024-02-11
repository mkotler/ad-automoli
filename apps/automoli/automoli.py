"""AutoMoLi.
   Automatic Motion Lights
  @benleb / https://github.com/benleb/ad-automoli
"""

from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from datetime import datetime, date, time, timedelta
from dateutil import tz
from distutils.version import StrictVersion
from enum import Enum, IntEnum
from inspect import stack
import logging
from pprint import pformat
from typing import Any

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

        if level >= self.loglevel:
            message = f"{f'{icon} ' if icon else ' '}{msg}"
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
                        "No room set yet, using 'AutoMoLi' for logging to HA",
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
        elif (
            CONFIG_APPNAME in self.app_config
            and name in self.app_config[CONFIG_APPNAME]
        ):
            return self.app_config[CONFIG_APPNAME][name]
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

        self.loglevel = (
            logging.DEBUG if bool(self.getarg("debug_log", False)) else logging.INFO
        )

        self.log_to_ha = bool(self.getarg("log_to_ha", False))

        self.colorize_logging = bool(self.getarg("colorize_logging", True))

        self.lg(
            f"setting log level to {logging.getLevelName(self.loglevel)}",
            level=logging.DEBUG,
        )

        # python version check
        if not py39_or_higher:
            self.lg("")
            self.lg(f" hey, what about trying {hl('Python >= 3.9')}‽ 🤪")
            self.lg("")
        if not py38_or_higher:
            icon_alert = "⚠️"
            self.lg("", icon=icon_alert)
            self.lg("")
            self.lg(
                f" please update to {hl('Python >= 3.8')} at least! 🤪", icon=icon_alert
            )
            self.lg("")
            self.lg("", icon=icon_alert)
        if not py37_or_higher:
            raise ValueError

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

        # on/off switch via input.boolean
        self.disable_switch_entities: set[str] = self.listr(
            self.getarg("disable_switch_entities", set())
        )
        self.disable_switch_states: set[str] = self.listr(
            self.getarg("disable_switch_states", set(["off"])), False
        )

        # additional sensors that will block turning on or off lights
        self.block_on_switch_entities: set[str] = self.listr(
            self.getarg("block_on_switch_entities", set())
        )
        self.block_on_switch_states: set[str] = self.listr(
            self.getarg("block_on_switch_states", set(["off"])), False
        )
        self.block_off_switch_entities: set[str] = self.listr(
            self.getarg("block_off_switch_entities", set())
        )
        self.block_off_switch_states: set[str] = self.listr(
            self.getarg("block_off_switch_states", set(["off"])), False
        )

        # sensors that will change current default delay
        self.override_delay_entities: set[str] = self.listr(
            self.getarg("override_delay_entities", set())
        )
        self.override_delay: int = int(
            self.getarg("override_delay", DEFAULT_OVERRIDE_DELAY)
        )
        self.override_delay_active: bool = False

        # store if an entity has been switched on by automoli
        # None: automoli will turn off lights after motion detected
        #       (regardless of whether automoli turned light on originally;
        #       treated differently from False to support legacy behavior)
        # False: automoli will turn off lights after motion detected OR delay
        #       (regardless of whether automoli turned light on originally)
        # True: automoli will only turn off lights it turned on
        self.only_own_events: bool = self.getarg("only_own_events", None)
        self._switched_on_by_automoli: set[str] = set()
        self._switched_off_by_automoli: set[str] = set()

        self.disable_hue_groups: bool = self.getarg("disable_hue_groups", False)

        self.warning_flash: bool = self.getarg("warning_flash", False)
        self._warning_lights: set[str] = set()

        # eol of the old option name
        if "disable_switch_entity" in self.args:
            icon_alert = "⚠️"
            self.lg("", icon=icon_alert)
            self.lg(
                f" please migrate {hl('disable_switch_entity')} to {hl('disable_switch_entities')}",
                icon=icon_alert,
            )
            self.lg("", icon=icon_alert)
            self.args.pop("disable_switch_entity")
            return

        # currently active daytime settings
        self.active: dict[str, int | str] = {}

        # entity lists for initial discovery
        states = self.get_state()

        # define light entities switched by automoli
        self.lights: set[str] = self.listr(self.getarg("lights", set()))
        if not self.lights:
            room_light_group = f"light.{self.room_name}"
            if self.entity_exists(room_light_group):
                self.lights.add(room_light_group)
            else:
                self.lights.update(
                    self.find_sensors(EntityType.LIGHT.prefix, self.room_name, states)
                )

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
            self.lg("  docs: https://github.com/benleb/ad-automoli")
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
        self.sensor_update_handle: str | None = None
        self.init_room_stats()
        self.run_daily(self.reset_room_stats, "00:00:00")
        self.listen_event(self.room_event, event=EVENT_AUTOMOLI_STATS)

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

        # set up event listener for each sensor
        listener: set[Any, Any, Any] = set()
        for sensor in self.sensors[EntityType.MOTION.idx]:

            # listen to xiaomi sensors by default
            if not any([self.states["motion_on"], self.states["motion_off"]]):
                self.lg(
                    "no motion states configured - using event listener",
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
                    "both motion states configured - using state listener",
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
        # set up state listener for each light, if tracking events outside automoli
        if self.only_own_events == False:
            self.lg(
                "handling events outside AutoMoLi - adding state listeners for lights",
                level=logging.DEBUG,
            )
            at_least_one_turned_on = False
            for light in self.lights:
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
                "override_delay_entities": self.override_delay_entities,
                "override_delay": self.override_delay,
                "disable_hue_groups": self.disable_hue_groups,
                "warning_flash": self.warning_flash,
                "only_own_events": self.only_own_events,
                "track_room_stats": self.track_room_stats,
                "loglevel": self.loglevel,
            }
        )

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

        # show parsed config
        self.show_info(self.args)

        if any([self.get_state(light, copy=False) == "on" for light in self.lights]):
            self.run_in(
                self.update_room_stats,
                0,
                stat="lastOn",
                appInit=True,
                source="On at restart",
            )

            light_setting = (
                self.active.get("light_setting")
                if not self.night_mode_active()
                else self.night_mode.get("light")
            )
            message = (
                f"{hl(self.room.name.replace('_',' ').title())} was {hl('on')} when AutoMoLi started → "
                f"brightness: {hl(light_setting)}%"
                f" | delay: {hl(natural_time(int(self.active['delay'])))}"
            )
            # Since in initialization loop, wait 10s for all rooms to load before logging
            self.run_in(self.lg_delayed, 10, msg=message, icon=ON_ICON)

            self.refresh_timer()
        else:
            self.run_in(
                self.update_room_stats,
                0,
                stat="lastOff",
                appInit=True,
                source="Off at restart",
            )

    def switch_daytime(self, kwargs: dict[str, Any]) -> None:
        """Set new light settings according to daytime."""

        daytime = kwargs.get("daytime")

        if daytime is not None:
            self.active = daytime
            if not kwargs.get("initial"):

                delay = daytime["delay"]
                light_setting = daytime["light_setting"]
                if isinstance(light_setting, str):
                    is_scene = True
                    # if its a ha scene, remove the "scene." part
                    if "." in light_setting:
                        light_setting = (light_setting.split("."))[1]
                else:
                    is_scene = False

                self.lg(
                    f"{stack()[0][3]}: {self.transition_on_daytime_switch = }",
                    level=logging.DEBUG,
                )

                action_done = "set"

                if self.transition_on_daytime_switch and any(
                    [self.get_state(light, copy=False) == "on" for light in self.lights]
                ):
                    self.lights_on(source="daytime change", force=True)
                    action_done = "activated"

                self.lg(
                    f"{action_done} daytime {hl(daytime['daytime'])} → "
                    f"{'scene' if is_scene else 'brightness'}: {hl(light_setting)}"
                    f"{'' if is_scene else '%'}, delay: {hl(natural_time(delay))}",
                    icon=DAYTIME_SWITCH_ICON,
                )
        # Update room stats with latest daytime
        self.run_in(self.update_room_stats, 0, stat="switchDaytime")

    def motion_cleared(
        self, entity: str, attribute: str, old: str, new: str, _: dict[str, Any]
    ) -> None:
        """wrapper for motion sensors that do not push a certain event but.
        instead the default HA `state_changed` event is used for presence detection
        schedules the callback to switch the lights off after a `state_changed` callback
        of a motion sensors changing to "cleared" is received
        """
        state = new
        old_state = old
        # Check if got entire state object
        if attribute == "all":
            state = dict(new).get("state")
            old_state = dict(old).get("state", "unknown")

        # do not process if state has changed from off to unavailable or unknown (or vice versa)
        # or if state has not actually changed (new == old)
        if (
            (
                state in ("unavailable", "unknown")
                and old_state == self.states["motion_off"]
            )
            or (
                state == self.states["motion_off"]
                and old_state in ("unavailable", "unknown")
            )
            or state == old_state
        ):
            return

        # start the timer if motion is cleared
        self.lg(
            f"{stack()[0][3]}: {entity} changed {attribute} from {old} to {new}",
            level=logging.DEBUG,
        )

        # Handle case when motion sensor went from "on" to unknown or unavailable
        states = {self.states["motion_off"], "unknown", "unavailable"}

        if all(
            [
                self.get_state(sensor, copy=False) in states
                for sensor in self.sensors[EntityType.MOTION.idx]
            ]
        ):
            # all motion sensors off, starting timer
            self.refresh_timer(refresh_type="motion_cleared")
            self.run_in(self.update_room_stats, 0, stat="motion_cleared", entity=entity)
        else:
            # cancel scheduled callbacks
            self.clear_handles()

    def motion_detected(
        self, entity: str, attribute: str, old: str, new: str, kwargs: dict[str, Any]
    ) -> None:
        """wrapper for motion sensors that do not push a certain event but.
        instead the default HA `state_changed` event is used for presence detection
        maps the `state_changed` callback of a motion sensors changing to "detected"
        to the `event` callback`
        """

        self.lg(
            f"{stack()[0][3]}: {entity} changed {attribute} from {old} to {new}",
            level=logging.DEBUG,
        )

        # cancel scheduled callbacks
        self.clear_handles()

        self.lg(
            f"{stack()[0][3]}: handles cleared and cancelled all scheduled timers"
            f" | {self.dimming = }",
            level=logging.DEBUG,
        )

        # calling motion event handler
        data: dict[str, Any] = {"entity_id": entity, "new": new, "old": old}
        self.motion_event("motion_state_changed_detection", data, kwargs)

    def motion_event(self, event: str, data: dict[str, str], _: dict[str, Any]) -> None:
        """Main handler for motion events."""

        # Process motion event even if AutoMoLi is currently blocked/disabled
        # is_blocked and is_disabled checked during lights_on and lights_off calls
        motion_trigger = data["entity_id"].replace(EntityType.MOTION.prefix, "")
        self.run_in(self.update_room_stats, 0, stat="motion", entity=motion_trigger)
        self.lg(
            f"{stack()[0][3]}: received '{hl(event)}' event from "
            f"'{motion_trigger}' | {self.dimming = }",
            level=logging.DEBUG,
        )

        # turn on the lights if not all are already on
        if self.dimming or not all(
            [self.get_state(light, copy=False) == "on" for light in self.lights]
        ):
            self.lg(
                f"{stack()[0][3]}: switching on | {self.dimming = }",
                level=logging.DEBUG,
            )
            self.lights_on(source=motion_trigger)
        else:
            refresh = ""
            if event != "motion_state_changed_detection":
                refresh = " → refreshing timer"
            self.lg(
                f"{stack()[0][3]}: lights in {self.room.name.replace('_',' ').title()} already on {refresh}"
                f" | {self.dimming = }",
                level=logging.DEBUG,
            )

        if event != "motion_state_changed_detection":
            self.refresh_timer()

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
            f"outside_change_detected called for {entity = } with {old = } and {new = }",
            level=logging.DEBUG,
        )

        state = new
        old_state = old
        # Check if got entire state object
        if attribute == "all":
            state = dict(new).get("state")
            old_state = dict(old).get("state", "unknown")
            context_id = dict(dict(new).get("context")).get("id")

        # ensure the change wasn't because of automoli
        if (state == "on" and entity in self._switched_on_by_automoli) or (
            state == "off" and entity in self._switched_off_by_automoli
        ):
            return

        # do not process if current state is 'unavailable', 'unknown',
        # or has not really changed (new == old)
        # assume that previous state holds until new state is available
        if state == "unavailable" or state == "unknown" or state == old_state:
            return

        # Determine if state change was caused by an automation
        automation_name = ""
        source = ""
        automations = self.get_state(entity_id="automation")
        for automation in automations:
            automation_state = self.get_state(entity_id=automation, attribute="all")
            automation_context_id = dict(dict(automation_state).get("context")).get(
                "id"
            )
            if context_id == automation_context_id:
                automation_name = dict(dict(automation_state).get("attributes")).get(
                    "friendly_name"
                )
        if automation_name == "":
            if old_state == "on" or old_state == "off":
                self.lg(f"{hl(self.get_name(entity))} was turned '{state}' manually")
            # otherwise handle case when state was "unavailable" or "unknown"
            else:
                self.lg(
                    f"{hl(self.get_name(entity))} changed to '{state}' from {old_state}"
                )
            source = "Manually"
        else:
            self.lg(
                f"{hl(self.get_name(entity))} was turned '{state}' by automation '{automation_name}'"
            )
            source = automation_name

        # stop tracking the light as turned on or off by AutoMoLi
        if entity in self._switched_on_by_automoli:
            self._switched_on_by_automoli.remove(entity)
        if entity in self._switched_off_by_automoli:
            self._switched_off_by_automoli.remove(entity)

        # Get all of the lights in the room besides the one that just changed
        filtered_lights = set(filter(lambda light: light != entity, self.lights))

        how = "manually" if automation_name == "" else "automation"
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
                    f"{stack()[0][3]}: handles cleared and cancelled all scheduled timers",
                    level=logging.DEBUG,
                )
                self.run_in(
                    self.update_room_stats,
                    0,
                    stat="lastOff",
                    howChanged=how,
                    source=source,
                )
            # if turned a light off manually, probably don't want AutoMoLi to immediately
            # turn it back on so start cooldown period
            if old_state == "on":
                self.cooling_down = True
                self.cooling_down_handle = self.run_in(
                    self.cooldown_off, DEFAULT_COOLDOWN
                )
        elif state == "on":
            # update stats to set room on when this is the first light turned on
            if self.sensor_state == "off":
                self.run_in(
                    self.update_room_stats,
                    0,
                    stat="lastOn",
                    howChanged=how,
                    source=source,
                )
            # refresh timer if any lights turned on manually
            self.refresh_timer(refresh_type="outside_change")

    def cooldown_off(self, _: dict[str, Any] | None = None) -> None:
        self.cooling_down = False

    def has_min_ad_version(self, required_version: str) -> bool:
        required_version = required_version if required_version else "4.0.7"
        return bool(
            StrictVersion(self.get_ad_version()) >= StrictVersion(required_version)
        )

    def clear_handles(self, handles: set[str] = None) -> None:
        """clear scheduled timers/callbacks."""

        if not handles:
            handles = deepcopy(self.room.handles_automoli)
            self.room.handles_automoli.clear()

        if self.has_min_ad_version("4.0.7"):
            for handle in handles:
                if self.timer_running(handle):
                    self.cancel_timer(handle)
        else:
            for handle in handles:
                self.cancel_timer(handle)

        # reset override delay status
        self.override_delay_active = False

        self.lg(f"{stack()[0][3]}: cancelled scheduled callbacks", level=logging.DEBUG)

    def refresh_timer(self, refresh_type: str = "normal") -> None:
        """refresh delay timer."""

        fnn = f"{stack()[0][3]}:"

        # leave dimming state
        self.dimming = False

        dim_in_sec = 0

        # if delay is currently overridden
        if self.override_delay_active:
            # clear handles and go back to normal delay unless
            # motion was cleared *after* overridden delay or
            # refresh was triggered by another override_delay message
            if refresh_type == "motion_cleared":
                return
            elif refresh_type != "override_delay":
                self.run_in(
                    self.update_room_stats, 0, stat="overrideDelay", enable=False
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
                f"{fnn} {self.active = } | {self.delay_outside_events = }"
                f" | {refresh_type = } | {delay = } | {self.dim = }",
                level=logging.DEBUG,
            )

            if self.dim:
                dim_in_sec = int(delay) - self.dim["seconds_before"]
                self.lg(f"{fnn} {dim_in_sec = }", level=logging.DEBUG)

                handle = self.run_in(self.dim_lights, dim_in_sec, timeDelay=delay)

            else:
                handle = self.run_in(self.lights_off, delay, timeDelay=delay)

            self.room.handles_automoli.add(handle)

            if timer_info := self.info_timer(handle):
                self.lg(
                    f"{fnn} scheduled callback to switch off the lights in {dim_in_sec}s at"
                    f"({timer_info[0].isoformat()}) | "
                    f"handles: {self.room.handles_automoli = }",
                    level=logging.DEBUG,
                )
                self.run_in(
                    self.update_room_stats, 0, stat="refreshTimer", time=timer_info[0]
                )

            if self.warning_flash and refresh_type != "override_delay":
                handle = self.run_in(
                    self.warning_flash_off, (int(delay) - DEFAULT_WARNING_DELAY)
                )
                self.room.handles_automoli.add(handle)

        else:
            self.lg(
                "No delay was set or delay = 0, lights will not be switched off by AutoMoLi",
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
                0,
                stat="overrideDelay",
                enable=True,
                entity=entity,
            )
            self.refresh_timer(refresh_type="override_delay")

    def night_mode_active(self) -> bool:
        return bool(
            self.night_mode
            and self.get_state(self.night_mode["entity"], copy=False) == "on"
        )

    def is_disabled(self, onoff: str = None) -> bool:
        """check if automoli is disabled via home assistant entity"""
        for entity in self.disable_switch_entities:
            if (
                state := self.get_state(entity, copy=False)
            ) and state in self.disable_switch_states:
                # Refresh timer if disabling lights from turning off
                if onoff == "off":
                    self.refresh_timer()
                # Only log first time disabled
                if self.sensor_attr.get("disabled_by", "") == "":
                    self.lg(
                        f"{APP_NAME} is disabled by {self.get_name(entity)} with state '{state}'"
                    )
                self.run_in(self.update_room_stats, 0, stat="disabled", entity=entity)
                return True

        # or because currently in cooldown period after an outside change
        if self.cooling_down:
            # Do not need to refresh timer because cooling_down currently
            # only disables lights turning on
            self.lg(f"{APP_NAME} is disabled during cooldown period")
            self.run_in(
                self.update_room_stats, 0, stat="disabled", entity="Cooling down"
            )
            return True

        return False

    def is_blocked(self, onoff: str = None) -> bool:
        if onoff == "on":
            for entity in self.block_on_switch_entities:
                if (
                    state := self.get_state(entity, copy=False)
                ) and state in self.block_on_switch_states:
                    # Do not need to refresh timer when blocking lights turning on
                    # Only log first time blocked
                    if self.sensor_attr.get("blocked_on_by", "") == "":
                        self.lg(
                            f"Motion detected in {hl(self.room.name.replace('_',' ').title())} "
                            f"but blocked by {self.get_name(entity)} with state '{state}'"
                        )
                    self.run_in(
                        self.update_room_stats, 0, stat="blockedOn", entity=entity
                    )
                    return True
        elif onoff == "off":
            # the "shower case"
            if humidity_threshold := self.thresholds.get("humidity"):
                for sensor in self.sensors[EntityType.HUMIDITY.idx]:
                    try:
                        current_humidity = float(
                            self.get_state(sensor)  # type:ignore
                        )
                    except ValueError as error:
                        self.lg(
                            f"self.get_state(sensor) raised a ValueError for {sensor}: {error}",
                            level=logging.ERROR,
                        )
                        continue

                    self.lg(
                        f"{stack()[0][3]}: {current_humidity = } >= {humidity_threshold = } "
                        f"= {current_humidity >= humidity_threshold}",
                        level=logging.DEBUG,
                    )

                    if current_humidity >= humidity_threshold:
                        self.refresh_timer()
                        # Only log first time blocked
                        if self.sensor_attr.get("blocked_off_by", "") == "":
                            self.lg(
                                f"🛁 No motion in {hl(self.room.name.replace('_',' ').title())} since "
                                f"{hl(natural_time(int(self.active['delay'])))} → "
                                f"but {hl(current_humidity)}%RH > "
                                f"{hl(humidity_threshold)}%RH"
                            )
                        self.run_in(
                            self.update_room_stats, 0, stat="blockedOff", entity=sensor
                        )
                        return True
            # other entities
            for entity in self.block_off_switch_entities:
                if (
                    state := self.get_state(entity, copy=False)
                ) and state in self.block_off_switch_states:
                    self.refresh_timer()
                    # Only log first time blocked
                    if self.sensor_attr.get("blocked_off_by", "") == "":
                        self.lg(
                            f"No motion in {hl(self.room.name.replace('_',' ').title())} since "
                            f"{hl(natural_time(int(self.active['delay'])))} → "
                            f"but blocked by {self.get_name(entity)} with state '{state}'"
                        )
                    self.run_in(
                        self.update_room_stats, 0, stat="blockedOff", entity=entity
                    )
                    return True
        return False

    def dim_lights(self, kwargs: dict[str, Any]) -> None:

        message: str = ""

        # check logging level here first to avoid duplicate log entries when not debug logging
        if logging.DEBUG >= self.loglevel:
            self.lg(
                f"{stack()[0][3]}: {self.is_disabled(onoff='off') = } | {self.is_blocked(onoff='off') = }",
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
                f"{stack()[0][3]}: {dim_method = } | {seconds_before = }",
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
                f"{stack()[0][3]}: {dim_attributes = } | {self.dimming = }",
                level=logging.DEBUG,
            )

            self.lg(f"{stack()[0][3]}: {self.room.room_lights = }", level=logging.DEBUG)
            self.lg(
                f"{stack()[0][3]}: {self.room.lights_dimmable = }", level=logging.DEBUG
            )
            self.lg(
                f"{stack()[0][3]}: {self.room.lights_undimmable = }",
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

        delay = kwargs.get("timeDelay", 0)
        timeSinceMotion = (
            (natural_time(int(delay))).replace("\033[1m", "").replace("\033[0m", "")
        )
        source = f"No motion for {timeSinceMotion}, dimming lights"
        self.run_in(self.update_room_stats, 0, stat="lastOff", source=source)
        self.lg(message, icon=OFF_ICON)

    def turn_off_lights(self, kwargs: dict[str, Any]) -> None:
        if lights := kwargs.get("lights"):
            self.lg(f"{stack()[0][3]}: {lights = }", level=logging.DEBUG)
            for light in lights:
                self.call_service("homeassistant/turn_off", entity_id=light)
                if light in self._switched_on_by_automoli:
                    self._switched_on_by_automoli.remove(light)
                self._switched_off_by_automoli.add(light)
            self.run_in(self.turned_off, 0)

    def lights_on(self, source: str = "<unknown>", force: bool = False) -> None:
        """Turn on the lights."""

        # check logging level here first to avoid duplicate log entries when not debug logging
        if logging.DEBUG >= self.loglevel:
            self.lg(
                f"{stack()[0][3]}: {self.is_disabled(onoff='on') = } | {self.is_blocked(onoff='on') = } | {self.dimming = }",
                level=logging.DEBUG,
            )

        # check if automoli is disabled via home assistant entity or blockers
        if self.is_disabled(onoff="on") or self.is_blocked(onoff="on"):
            return

        self.lg(
            f"{stack()[0][3]}: {self.thresholds.get(EntityType.ILLUMINANCE.idx) = }"
            f" | {self.dimming = } | {force = } | {bool(force or self.dimming) = }",
            level=logging.DEBUG,
        )

        force = bool(force or self.dimming)

        if source != "daytime change" and source != "<unknown>":
            source = self.get_name(source)

        if illuminance_threshold := self.thresholds.get(EntityType.ILLUMINANCE.idx):

            # the "eco mode" check
            for sensor in self.sensors[EntityType.ILLUMINANCE.idx]:
                self.lg(
                    f"{stack()[0][3]}: {self.thresholds.get(EntityType.ILLUMINANCE.idx) = } | "
                    f"{float(self.get_state(sensor, copy=False)) = }",  # type:ignore
                    level=logging.DEBUG,
                )
                try:
                    if (
                        illuminance := float(
                            self.get_state(sensor)  # type:ignore
                        )  # type:ignore
                    ) >= illuminance_threshold:
                        self.lg(
                            f"According to {hl(sensor)} its already bright enough ¯\\_(ツ)_/¯"
                            f" | {illuminance} >= {illuminance_threshold}"
                        )
                        return

                except ValueError as error:
                    self.lg(
                        f"could not parse illuminance '{self.get_state(sensor, copy=False)}' "
                        f"from '{sensor}': {error}"
                    )
                    return

        light_setting = (
            self.active.get("light_setting")
            if not self.night_mode_active()
            else self.night_mode.get("light")
        )

        if isinstance(light_setting, str):

            # last check until we switch all the lights on... really!
            if not force and all(
                [self.get_state(light, copy=False) == "on" for light in self.lights]
            ):
                self.lg("¯\\_(ツ)_/¯")
                return

            for entity in self.lights:

                if self.active["is_hue_group"] and self.get_state(
                    entity_id=entity, attribute="is_hue_group"
                ):
                    self.call_service(
                        "hue/hue_activate_scene",
                        group_name=self.friendly_name(entity),  # type:ignore
                        scene_name=light_setting,  # type:ignore
                    )
                    self._switched_on_by_automoli.add(entity)
                    if entity in self._switched_off_by_automoli:
                        self._switched_off_by_automoli.remove(entity)
                    continue

                item = light_setting if light_setting.startswith("scene.") else entity

                self.call_service(
                    "homeassistant/turn_on", entity_id=item  # type:ignore
                )  # type:ignore
                self._switched_on_by_automoli.add(item)
                if item in self._switched_off_by_automoli:
                    self._switched_off_by_automoli.remove(item)

            self.run_in(self.update_room_stats, 0, stat="lastOn", source=source)

            self.lg(
                f"{hl(self.room.name.replace('_',' ').title())} turned {hl('on')} by {hl(source)} → "
                f"{'hue' if self.active['is_hue_group'] else 'ha'} scene: "
                f"{hl(light_setting.replace('scene.', ''))}"
                f" | delay: {hl(natural_time(int(self.active['delay'])))}",
                icon=ON_ICON,
            )

        elif isinstance(light_setting, int):

            if light_setting == 0:
                if all(
                    [
                        self.get_state(entity, copy=False) == "off"
                        for entity in self.lights
                    ]
                ):
                    self.lg(
                        "no lights turned on because current 'daytime' light setting is 0",
                        level=logging.DEBUG,
                    )
                else:
                    self.run_in(self.lights_off, 0, daytimeChange=True)

            else:
                # last check until we switch all the lights on... really!
                if not force and all(
                    [self.get_state(light, copy=False) == "on" for light in self.lights]
                ):
                    self.lg("¯\\_(ツ)_/¯")
                    return

                for entity in self.lights:
                    if entity.startswith("switch."):
                        self.call_service(
                            "homeassistant/turn_on", entity_id=entity  # type:ignore
                        )
                    else:
                        self.call_service(
                            "homeassistant/turn_on",
                            entity_id=entity,  # type:ignore
                            brightness_pct=light_setting,  # type:ignore
                        )
                    self._switched_on_by_automoli.add(entity)
                    if entity in self._switched_off_by_automoli:
                        self._switched_off_by_automoli.remove(entity)

                self.lg(
                    f"{hl(self.room.name.replace('_',' ').title())} turned {hl('on')} by {hl(source)} → "
                    f"brightness: {hl(light_setting)}%"
                    f" | delay: {hl(natural_time(int(self.active['delay'])))}",
                    icon=ON_ICON,
                )
                self.run_in(self.update_room_stats, 0, stat="lastOn", source=source)
        else:
            raise ValueError(
                f"invalid brightness/scene: {light_setting!s} " f"in {self.room}"
            )

    def lights_off(self, kwargs: dict[str, Any]) -> None:
        """Turn off the lights."""

        # check logging level here first to avoid duplicate log entries when not debug logging
        if logging.DEBUG >= self.loglevel:
            self.lg(
                f"{stack()[0][3]}: {self.is_disabled(onoff='off') = } | {self.is_blocked(onoff='off') = }",
                level=logging.DEBUG,
            )

        # check if automoli is disabled via home assistant entity or blockers like the "shower case"
        if self.is_disabled(onoff="off") or self.is_blocked(onoff="off"):
            return

        # cancel scheduled callbacks
        self.clear_handles()

        self.lg(
            f"{stack()[0][3]}: "
            f"{any([self.get_state(entity, copy=False) == 'on' for entity in self.lights]) = }"
            f" | {self.lights = }",
            level=logging.DEBUG,
        )

        # if any([self.get_state(entity) == "on" for entity in self.lights]):
        if all([self.get_state(entity, copy=False) == "off" for entity in self.lights]):
            return

        at_least_one_turned_off = False
        for entity in self.lights:
            if self.get_state(entity, copy=False) == "on":
                if self.only_own_events:
                    if entity in self._switched_on_by_automoli:
                        self.call_service(
                            "homeassistant/turn_off", entity_id=entity  # type:ignore
                        )  # type:ignore
                        if entity in self._switched_on_by_automoli:
                            self._switched_on_by_automoli.remove(entity)
                        self._switched_off_by_automoli.add(entity)
                        at_least_one_turned_off = True
                else:
                    self.call_service(
                        "homeassistant/turn_off", entity_id=entity  # type:ignore
                    )  # type:ignore
                    if entity in self._switched_on_by_automoli:
                        self._switched_on_by_automoli.remove(entity)
                    self._switched_off_by_automoli.add(entity)
                    at_least_one_turned_off = True
        if at_least_one_turned_off:
            delay = kwargs.get("timeDelay", 0)
            daytimeChange = kwargs.get("daytimeChange", False)
            self.run_in(
                self.turned_off, 0, timeDelay=delay, daytimeChange=daytimeChange
            )

        # experimental | reset for xiaomi "super motion" sensors | idea from @wernerhp
        # app: https://github.com/wernerhp/appdaemon_aqara_motion_sensors
        # mod:
        # https://community.smartthings.com/t/making-xiaomi-motion-sensor-a-super-motion-sensor/139806
        for sensor in self.sensors[EntityType.MOTION.idx]:
            self.set_state(
                sensor,
                state="off",
                attributes=(self.get_state(sensor, attribute="all")).get(
                    "attributes", {}
                ),
            )

    # Global lock ensures that multiple log writes occur together after turning off lights
    @ad.global_lock
    def turned_off(self, kwargs: dict[str, Any] | None = None) -> None:
        # cancel scheduled callbacks
        self.clear_handles()

        delay = kwargs.get("timeDelay", self.active["delay"])
        daytimeChange = kwargs.get("daytimeChange", False)
        source = ""

        if self.override_delay_active:
            overriddenBy = self.sensor_attr.get("delay_overridden_by", "")
            self.lg(
                f"No motion in {hl(self.room.name.replace('_',' ').title())} for "
                f"{hl(natural_time(int(delay)))} overridden by {overriddenBy} → turned {hl('off')}",
                icon=OFF_ICON,
            )
            self.run_in(self.update_room_stats, 0, stat="overrideDelay", enable=False)
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
                (natural_time(int(delay))).replace("\033[1m", "").replace("\033[0m", "")
            )
            source = f"No motion for {timeSinceMotion}"

        # Update room stats to record room turned off
        self.run_in(self.update_room_stats, 0, stat="lastOff", source=source)

        # Log last motion if there actually was motion
        lastMotionBy = self.sensor_attr.get("last_motion_by", "")
        if lastMotionBy != "":
            if (lastMotionWhen := self.sensor_attr.get("last_motion_cleared", "")) == 0:
                lastMotionWhen = self.sensor_attr.get(
                    "last_motion_detected", "<unknown>"
                )
                self.lg(f"  Last motion from {lastMotionBy} at {lastMotionWhen}.")

        # Log how long lights were on
        lastOn = datetime.strptime(self.sensor_attr["last_turned_on"], DATETIME_FORMAT)
        difference = int(datetime.timestamp(datetime.now())) - int(
            datetime.timestamp(lastOn)
        )
        self.lg(
            f"  {hl(self.room_name.replace('_',' ').title())} was on for "
            f"{self.seconds_to_time(difference, True)} since {lastOn.strftime(DATETIME_FORMAT)}."
        )

    def warning_flash_off(self, _: dict[str, Any] | None = None) -> None:
        # check if automoli is disabled via home assistant entity or blockers like the "shower case"
        # if so then don't do the warning flash
        if self.is_disabled(onoff="off") or self.is_blocked(onoff="off"):
            return

        self.lg(
            f"lights will be turned off in {hl(self.room.name.replace('_',' ').title())} in "
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
                if entity in self._switched_on_by_automoli:
                    self._switched_on_by_automoli.remove(entity)
                self._switched_off_by_automoli.add(entity)

        # turn lights on again in 1s
        if at_least_one_turned_off:
            self.run_in(self.warning_flash_on, 1)

    def warning_flash_on(self, _: dict[str, Any] | None = None) -> None:
        # turn lights back on after 1s delay
        for entity in self._warning_lights:
            self.call_service(
                "homeassistant/turn_on", entity_id=entity  # type:ignore
            )  # type:ignore
            if entity in self._switched_off_by_automoli:
                self._switched_off_by_automoli.remove(entity)
            self._switched_on_by_automoli.add(entity)
        self._warning_lights.clear()

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
            self.lg("no night_mode entity given", level=logging.DEBUG)
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
                    and any(
                        self.get_state(entity_id=entity, attribute="is_hue_group")
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
            self.lg("no configuration available", icon="‼️", level=logging.ERROR)
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
        return self.get_state(entity_id, attribute="friendly_name", default=entity_id)

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
    # Note: Manual/automoli counts can get out of sync if a room has multiple lights/switches
    # because automoli changes will be counted once for all lights in the room, but manual changes
    # will be counted individually per light. Can avoid this problem by separating each switch into
    # its own room.

    def init_room_stats(self, _: Any | None = None) -> None:
        entity = self.get_state(self.entity_id)
        self.sensor_attr["friendly_name"] = (
            self.room_name.replace("_", " ").title() + " Statistics"
        )

        # Only initialize if entity doesn't exist or if last update was before today
        if entity == None:
            self.sensor_attr["time_lights_on_today"] = self.seconds_to_time(
                self.sensor_onToday
            )

            light_setting = (
                self.active.get("light_setting")
                if not self.night_mode_active()
                else self.night_mode.get("light")
            )
            if light_setting != None:
                self.sensor_attr["current_light_setting"] = str(light_setting) + "%"

        else:
            # Check if sensor was last updated before today
            today = date.today()
            lastUpdated: datetime = self.convert_utc(
                self.get_state(self.entity_id, attribute="last_updated")
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
                    self.entity_id, attribute="time_lights_on_today", default="00:00:00"
                )

                self.sensor_onToday = (
                    datetime.strptime(
                        self.sensor_attr["time_lights_on_today"], TIME_FORMAT
                    )
                    - datetime(1900, 1, 1)
                ).total_seconds()
                if (
                    countAutomoliOn := self.get_state(
                        self.entity_id, "times_turned_on_by_automoli", default=0
                    )
                ) != 0:
                    self.sensor_attr["times_turned_on_by_automoli"] = countAutomoliOn
                if (
                    countAutomoliOff := self.get_state(
                        self.entity_id, "times_turned_off_by_automoli", default=0
                    )
                ) != 0:
                    self.sensor_attr["times_turned_off_by_automoli"] = countAutomoliOff
                if (
                    countAutomationOn := self.get_state(
                        self.entity_id, "times_turned_on_by_automations", default=0
                    )
                ) != 0:
                    self.sensor_attr["times_turned_on_by_automations"] = (
                        countAutomationOn
                    )
                if (
                    countAutomationOff := self.get_state(
                        self.entity_id, "times_turned_off_by_automations", default=0
                    )
                ) != 0:
                    self.sensor_attr["times_turned_off_by_automations"] = (
                        countAutomationOff
                    )
                if (
                    countManualOn := self.get_state(
                        self.entity_id, "times_turned_on_manually", default=0
                    )
                ) != 0:
                    self.sensor_attr["times_turned_on_manually"] = countManualOn
                if (
                    countManualOff := self.get_state(
                        self.entity_id, "times_turned_off_manually", default=0
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
        if logging.DEBUG < self.loglevel:
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
        howChanged = kwargs.get("howChanged", "automoli")
        stat = kwargs.get("stat", None)
        currentTime = datetime.now()
        currentTimeStr = currentTime.strftime(DATETIME_FORMAT)

        self.sensor_state = (
            "on"
            if any(
                [self.get_state(entity, copy=False) == "on" for entity in self.lights]
            )
            else "off"
        )

        # stat will be a dictionary if update_room_stats is called from run_in
        if isinstance(stat, dict):
            stat = dict(stat).get("stat", "")

        if stat == "motion":
            self.sensor_attr["last_motion_detected"] = currentTimeStr
            self.sensor_attr["last_motion_by"] = self.get_name(kwargs.get("entity"))
            self.sensor_attr.pop("last_motion_cleared", "")
            self.sensor_attr["turning_off_at"] = "Waiting for motion to clear"

        elif stat == "motion_cleared":
            self.sensor_attr["last_motion_cleared"] = currentTimeStr
            self.sensor_attr["last_motion_by"] = self.get_name(kwargs.get("entity"))
            self.sensor_attr.pop("last_motion_detected", "")

        elif stat == "lastOn":
            # Should not need this line with the get_state call above
            # However, get_state is not returning correctly immediately
            self.sensor_state = "on"

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
            self.sensor_update_handle = self.run_every(
                self.update_room_stats, "now+60", 60, stat="updateEveryMin"
            )

        elif stat == "lastOff":
            # Should not need this line with the get_state call above
            # However, get_state is not returning correctly immediately
            self.sensor_state = "off"

            self.sensor_attr["last_turned_off"] = currentTimeStr
            self.sensor_onToday = int(self.sensor_onToday) + int(
                self._get_adjusted_time_on()
            )
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
            self.sensor_attr.pop("disabled_by", "")

        elif stat == "overrideDelay":
            if kwargs.get("enable"):
                self.sensor_attr["delay_overridden_by"] = self.get_name(
                    kwargs.get("entity")
                )
            else:
                self.sensor_attr.pop("delay_overridden_by", "")

        elif stat == "refreshTimer":
            if self.sensor_state == "on":
                self.sensor_attr["turning_off_at"] = datetime.strftime(
                    kwargs.get("time"), DATETIME_FORMAT
                )
            else:
                self.sensor_attr.pop("turning_off_at", "")

        elif stat == "switchDaytime":
            light_setting = (
                self.active.get("light_setting")
                if not self.night_mode_active()
                else self.night_mode.get("light")
            )
            self.sensor_attr["current_light_setting"] = str(light_setting) + "%"

        elif stat == "blockedOn":
            self.sensor_attr["blocked_on_by"] = self.get_name(kwargs.get("entity"))

        elif stat == "blockedOff":
            self.sensor_attr["blocked_off_by"] = self.get_name(kwargs.get("entity"))

        elif stat == "disabled":
            if self.entity_exists(entity := kwargs.get("entity")):
                self.sensor_attr["disabled_by"] = self.get_name(entity)
            else:
                self.sensor_attr["disabled_by"] = entity

        # If the room is still on, record all the time it was on until now
        if self.sensor_state == "on":
            adjustedOnToday = int(self.sensor_onToday) + int(
                self._get_adjusted_time_on()
            )
            self.sensor_attr["time_lights_on_today"] = self.seconds_to_time(
                adjustedOnToday
            )
        else:
            if self.timer_running(self.sensor_update_handle):
                self.cancel_timer(self.sensor_update_handle)

        if logging.DEBUG >= self.loglevel:
            log_message = (
                f"{stat} | {datetime.now().strftime('%H:%M:%S.%f')}"
                f" | {self.sensor_onToday = } | { kwargs.get('message', '')}"
            )
            self.sensor_attr["debug_message"] = log_message
            self.lg(f"{log_message} | {self.sensor_attr = }", level=logging.DEBUG)

        if self.track_room_stats:
            self.set_state(
                entity_id=self.entity_id,
                state=self.sensor_state,
                attributes=self.sensor_attr,
                replace=True,
            )

        self.lg(
            f"Update stats was called with {stat = } and updated state to {self.sensor_attr = }",
            level=logging.DEBUG,
        )

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
                f"{hl(self.room_name.replace('_',' ').title())} was turned on "
                f"{totalOn} time(s) for a total of {self.seconds_to_time(adjustedOnToday)} today"
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

    def _get_adjusted_time_on(self) -> int:
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
                "Trying to get the time the room has been on but there is no record of it being turned on",
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
            days = total // (24 * 3600)
        total %= 24 * 3600
        hours = total // 3600
        total %= 3600
        minutes = total // 60
        total %= 60
        seconds = int(total)
        if (includeDays == False) or (days == 0):
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        elif days == 1:
            return f"{days} day, {hours:02d}:{minutes:02d}:{seconds:02d}"
        elif days > 1:
            return f"{days} days, {hours:02d}:{minutes:02d}:{seconds:02d}"
