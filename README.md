# [![automoli](https://socialify.git.ci/mkotler/ad-automoli/image?description=0&font=KoHo&forks=1&language=1&logo=https%3A%2F%2Femojipedia-us.s3.dualstack.us-west-1.amazonaws.com%2Fthumbs%2F240%2Fapple%2F237%2Felectric-light-bulb_1f4a1.png&owner=0&pulls=1&stargazers=1&theme=Light)](https://github.com/mkotler/ad-automoli)

<!-- # AutoMoLi - **Auto**matic **Mo**tion **Li**ghts -->

## Acknowledgment
Many thanks to **Ben Lebherz** ([@benleb](https://github.com/benleb) | [@ben_leb](https://twitter.com/ben_leb)) for the initial idea and implementation of [**AutoMoLi**](https://github.com/benleb/ad-automoli). This version has diverged significantly from the original including a number of additional features. However, I have kept the name in recognition of that starting point.      

## Description

#### Fully *automatic light management* based on conditions like motion, humidity, and more as an [AppDaemon](https://github.com/home-assistant/appdaemon) app.

🕓 multiple **daytimes** to define different scenes for morning, noon, ...  
💡 supports **Hue** (for Hue Rooms/Groups) & **Home Assistant** scenes  
🔌 switches **lights** and **plugs** (with lights)  
☀️ supports **illumination sensors** to switch the light just if needed  
💦 supports **humidity sensors** as blocker (the "*shower case*")  
🔍 **automatic** discovery of **lights** and **sensors**  
⛰️ **stable** and **tested** by many people with different homes  

## Getting Started

## Installation

[Download](https://github.com/mkotler/ad-automoli/tree/main/apps/automoli) the `automoli.py` file from inside the `apps/automoli` directory. Create an `automoli` directory under your local `apps` directory, then add the configuration to enable the `automoli` module.

### Example App Configuration

Add your configuration to apps/apps.yaml under the appdaemon directory. An example configuration with two rooms is below.

```yaml
livingroom:
  module: automoli
  class: AutoMoLi
  room: livingroom
  disable_switch_entities:
    - input_boolean.automoli
    - input_boolean.disable_my_house
  delay: 600
  daytimes:
      # This rule "morning" uses a scene, the scene.livingroom_morning Home Assistant scene will be used
    - { starttime: "sunrise", name: morning, light: "scene.livingroom_morning" }

    - { starttime: "07:30", name: day, light: "scene.livingroom_working" }

      # This rule"evening" uses a percentage brightness value, and the lights specified in lights: below will be set to 90%
    - { starttime: "sunset-01:00", name: evening, light: 90 }

    - { starttime: "22:30", name: night, light: 20 }

      # This rule has the lights set to 0, so they will no turn on during this time period
    - { starttime: "23:30", name: more_night, light: 0 }

  # If you are using an illuminance sensor you can set the lowest value here that blocks the lights turning on if its already light enough
  illuminance: sensor.illuminance_livingroom
  illuminance_threshold: 100

  # You can specify a light group or list of lights here
  lights:
    - light.livingroom

  # You can specify a list of motion sensors here
  motion:
    - binary_sensor.motion_sensor_153d000224f421
    - binary_sensor.motion_sensor_128d4101b95fb7

  # See below for info on humidity
  humidity:
    - sensor.humidity_128d4101b95fb7

bathroom:
  module: automoli
  class: AutoMoLi
  room: bathroom
  delay: 180
  motion_state_on: "on"
  motion_state_off: "off"
  daytimes:
    - { starttime: "05:30", name: morning, light: 45 }
    - { starttime: "07:30", name: day, light: "scene.bathroom_day" }
    - { starttime: "20:30", name: evening, light: 100 }
    - { starttime: "sunset+01:00", name: night, light: 0 }

  # As this is a bathroom there could be the case that when taking a bath or shower, motion is not detected and the lights turn off, which isnt helpful, so the following settings allow you to use a humidity sensor and humidity threshold to prevent this by detecting the humidity from the shower and blocking the lights turning off.
  humidity:
    - sensor.humidity_128d4101b95fb7
  humidity_threshold: 75

  lights:
    - light.bathroom
    - switch.plug_68fe8b4c9fa1
  motion:
    - binary_sensor.motion_sensor_158d033224e141
```

## Auto-Discovery of Lights and Sensors

[**AutoMoLi**](https://github.com/mkotler/ad-automoli) is built around **rooms**. Every room or area in your home is represented as a seperate app in [AppDaemon](https://github.com/AppDaemon/appdaemon) with separate light setting. In your configuration you will have **one config block** for every **room**, see example configuration.  
For the auto-discovery of your lights and sensors to work, AutoMoLi expects motion sensors and lights including a **room** name (can also be something else than a real room) like below:

* *sensor.illumination_`room`*
* *binary_sensor.motion_sensor_`room`*
* *binary_sensor.motion_sensor_`room`_something*
* *light.`room`*

AutoMoLi will detect them automatically. Manually configured entities will take precedence, but **need** to follow the naming scheme above.

## Basic Configuration Options

key | optional | type | default | description
-- | -- | -- | -- | --
`module` | False | string | automoli | The module name of the app.
`class` | False | string | AutoMoLi | The name of the Class.
`room` | True | string | app name | The "room" used to find matching sensors/lights. If blank then it will use the top level name in the configuration file.
`delay` | True | integer | 150 | Seconds without motion until lights will switched off. Can be disabled (lights stay always on) with `0`
`daytimes` | True | list | *see code* | Different daytimes with light settings (see below)
`lights` | True | list/string | *auto detect* | Light entities that are both turned on and off by automoli
`motion` | True | list/string | *auto detect* | Motion sensor entities
`motion_state_on` | True | integer | | If using motion sensors which don't send events if already activated, like Xiaomi do with the Xiaomi Gateway (Aqara) integration, add this to your config with "on". This will listen to state changes instead
`motion_state_off` | True | integer | | If using motion sensors which don't send events if already activated, like Xiaomi do with the Xiaomi Gateway (Aqara) integration, add this to your config with "off". This will listen to the state changes instead
`illuminance` | True | list/string |  | Illuminance sensor entities
`illuminance_threshold` | True | integer |  | If illuminance is *above* this value, lights will *not switched on*
`humidity` | True | list/string |  | Humidity sensor entities
`humidity_threshold` | True | integer |  | If humidity is *above* this value, lights will *not switched off*

### daytimes

key | optional | type | default | description
-- | -- | -- | -- | --
`starttime` | False | string | | Time this daytime starts or sunrise|sunset [+|- HH:MM]
`name` | False | string | | A name for this daytime
`delay` | True | integer | 150 | Seconds without motion until lights will switched off. Can be disabled (lights stay always on) with `0`. Setting this will overwrite the global `delay` setting for this daytime.
`light` | False | integer/string | | Light setting (percent integer value (0-100) in or scene entity

Note: If there is only one daytime, the light and delay settings will be applied for the entire day, regardless of the starttime.

## Advanced Configuration Options

key | optional | type | default | description
-- | -- | -- | -- | --
`dependencies` | True | string | None | If you set configuration options under an app named "default" then those will become the defaults across all rooms (but can still be overridden within a specific room). Specify `dependencies: default` so that any changes to the "default" app will be automatically picked up.
`transition_on_daytime_switch` | True | bool | False | directly activate a daytime on its start time (instead to just set it as active daytime used if lights are switched from off to on)
`only_own_events` | True | bool | None | Track if automoli switched this light on. If not, automoli will not switch the light off. (see below)
`delay_outside_events` | True | integer | same as delay | Seconds without motion until lights will switched off, if they were turned on by an event outside automoli (e.g., manually, via automation, etc.). Can be disabled (lights stay always on) with `0`
`disable_switch_entities` | True | list/string | | One or more Home Assistant Entities as switch for AutoMoLi. If the state of **any** entity is *off*, AutoMoLi is *deactivated*. (Use an *input_boolean* for example)
`disable_switch_states` | True | list/string | ["off"] | Custom states for `disable_switch_entities`. If the state of **any** entity is *in this list*, AutoMoLi is *deactivated*. Can be used to disable with `media_players` in `playing` state for example.
`block_on_switch_entities` | True | list/string | | If the state of **any** entity is *off*, AutoMoLi will not turn *on* lights until the entity is no longer *off*. (Use an *input_boolean* for example)
`block_on_switch_states` | True | list/string | ["off"] | Custom states for `block_on_switch_entities`. If the state of **any** entity is *in this list*, AutoMoLi will not turn *on* lights until the entity is no longer in this list. Can be used to block turning on bedroom lights if someone is in bed, for example.
`block_off_switch_entities` | True | list/string | | If the state of **any** entity is *off*, AutoMoLi will not turn *off* lights until the entity is no longer *off*. (Use an *input_boolean* for example)
`block_off_switch_states` | True | list/string | ["off"] | Custom states for `block_off_switch_entities`. If the state of **any** entity is *in this list*, AutoMoLi will not turn *off* lights until the entity is no longer in this list. Can be used to block turning off lights if the bathroom door is closed for example.
`disable_hue_groups` | False | boolean | | Disable the use of Hue Groups/Scenes
`override_delay_entities` | True | list/string |  | One ore more Home Assistant Entities that when a state change to "on" happens will override the delay (e.g., opening a door would reduce the timer to default 60 seconds for turning off the room's  lights )
`override_delay` | True | integer | 60 | Seconds to update delay to when one of the entities in `override_delay_entities` changes its state to "on"
`warning_flash` | True | boolean | false | Flash the lights (off and then on) 60 seconds before AutoMoLi will turn them off
`debug_log` | True | bool | false | Activate debug logging (for this room)
`colorize_logging` | True | bool | True | Use ANSI colors in the log. On by default but can be turned off to remove escape codes for viewers that do not support coloring. 
`track_room_stats` | True | boolean | false | Create sensors to show room statistics and print a daily summary in the log at midnight for how long lights were on that day. Even if this is false, firing the event "automoli_stats" will print a summary manually. 

### only_own_events

state | description
-- | --
None | Lights will be turned off after motion is detected, regardless of whether AutoMoLi turned the lights on.
False | Lights will be turned off after motion is detected, regardless of whether AutoMoLi turned the lights on AND after the delay if they were turned on outside AutoMoLi (e.g., manually or via an automation). 
True | Lights will only be turned off after motion is detected, if AutoMoLi turned the lights on.

## Home Assistant
**AutoMoLi** has been designed to be integrated into [**Home Assistant**](https://www.home-assistant.io/). As part of that integration, AutoMoLi will create an entity per room with attributes that track what is happening in the room.  Each entity is named automoli.room_name. Here is an example of automoli.office:

property | status
-- | --
friendly name | Office Statistics
current light setting | 50%
last motion detected | 03:55:57PM 2024-02-11
last motion by | Office Motion
last turned on | 02:53:17PM 2024-02-11
last turned on by | Office Motion
turning off at | 04:25:57PM 2024-02-11
time lights on today | 07:14:00
times turned on by automoli | 7
times turned off by automoli | 5
times turned off manually | 1

Notes:
* The statistics are not maintained during restarts (of either Home Assistant or AppDaemon)
* "time lights on today" will reset at midnight
* "times turned on/off by automoli" and "times turned on/off manually" can get out of sync if a room has multiple lights or switches because automoli changes will be counted once for all lights in the room, but manual changes are counted individually per light. 

---

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details
