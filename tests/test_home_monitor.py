"""Focused tests for HomeMonitor's observation-only context fusion."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import types
import unittest


# Home Assistant's AppDaemon runtime is not required for the pure/unit tests.
appdaemon_module = types.ModuleType("appdaemon")
plugins_module = types.ModuleType("appdaemon.plugins")
hass_plugin_module = types.ModuleType("appdaemon.plugins.hass")
hassapi_module = types.ModuleType("appdaemon.plugins.hass.hassapi")


class _Hass:
    pass


hassapi_module.Hass = _Hass  # type: ignore[attr-defined]
sys.modules.setdefault("appdaemon", appdaemon_module)
sys.modules.setdefault("appdaemon.plugins", plugins_module)
sys.modules.setdefault("appdaemon.plugins.hass", hass_plugin_module)
sys.modules.setdefault("appdaemon.plugins.hass.hassapi", hassapi_module)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from HomeMonitor import (  # noqa: E402
    HomeMonitor,
    derive_household_presence,
    derive_operating_mode,
    normalize_presence,
)


FIXED_NOW = datetime(2026, 7, 23, 5, 0, tzinfo=timezone.utc)


def _args() -> dict:
    return {
        "activity": {
            "initial_state": "awake",
            "wake_alarm_person": "arek",
            "wake_alarm_start_hour": 3,
            "wake_alarm_end_hour": 9,
            "force_wake_time": "09:00:00",
        },
        "persons": {
            "arek": {
                "name": "Arek",
                "person_entity": "person.arek",
                "work_zone_entity": "zone.praca_arka",
                "android_auto_entity": "binary_sensor.arek_android_auto",
                "next_alarm_entity": "sensor.arek_alarm",
            },
            "sabina": {
                "name": "Sabina",
                "person_entity": "person.sabina",
                "android_auto_entity": "binary_sensor.sabina_android_auto",
                "next_alarm_entity": "sensor.sabina_alarm",
            },
        },
        "presence": {
            "home_confirm_seconds": 60,
            "away_confirm_seconds": 120,
            "unavailable_grace_seconds": 300,
        },
        "context": {
            "sun_entity": "sun.sun",
            "max_alarm_future_hours": 36,
        },
        "controls": {
            "sleep_wake_button_name": "Sleep / Wake",
            "button_wake_time_start": 3,
            "button_wake_time_end": 12,
        },
        "mqtt_namespace": "mqtt",
        "mqtt_discovery_prefix": "homeassistant",
        "mqtt_base_topic": "home_monitor",
        "mqtt_device_id": "home_monitor",
    }


def _states(**overrides: str) -> dict[str, str]:
    states = {
        "sensor.home_monitor_activity": "awake",
        "person.arek": "home",
        "person.sabina": "home",
        "binary_sensor.arek_android_auto": "off",
        "binary_sensor.sabina_android_auto": "off",
        "sensor.arek_alarm": "2026-07-23T06:50:00+00:00",
        "sensor.sabina_alarm": "2026-07-23T06:10:00+00:00",
        "sun.sun": "above_horizon",
    }
    states.update(overrides)
    return states


class FakeHomeMonitor(HomeMonitor):
    """Minimal AppDaemon facade used to exercise initialize and callbacks."""

    def __init__(self, args: dict, states: dict[str, str]) -> None:
        self.args = args
        self.states = states
        self.service_calls: list[tuple[str, dict]] = []
        self.listeners: list[tuple] = []
        self.event_listeners: list[tuple] = []
        self.timers: list[dict] = []
        self.cancelled_timers: list[str] = []
        self.daily_schedules: list[tuple] = []
        self.repeating_schedules: list[tuple] = []
        self.logs: list[tuple[str, str]] = []
        self._fixed_now = FIXED_NOW

    def get_state(self, entity: str, **kwargs):
        return self.states.get(entity)

    def listen_state(self, callback, entity: str, **kwargs):
        self.listeners.append((callback, entity, kwargs))
        return f"listener-{len(self.listeners)}"

    def listen_event(self, callback, event: str, **kwargs):
        self.event_listeners.append((callback, event, kwargs))
        return f"event-listener-{len(self.event_listeners)}"

    def run_in(self, callback, delay: int, **kwargs):
        handle = f"timer-{len(self.timers) + 1}"
        self.timers.append(
            {"handle": handle, "callback": callback, "delay": delay, "kwargs": kwargs}
        )
        return handle

    def cancel_timer(self, handle: str):
        self.cancelled_timers.append(handle)

    def run_daily(self, callback, start: str):
        self.daily_schedules.append((callback, start))

    def run_every(self, callback, start: datetime, interval: int):
        self.repeating_schedules.append((callback, start, interval))

    def call_service(self, service: str, **kwargs):
        self.service_calls.append((service, kwargs))

    def datetime(self, aware: bool = False):
        return self._fixed_now

    def log(self, message: str, level: str = "INFO"):
        self.logs.append((level, message))

    def mqtt_payloads(self, topic: str) -> list[str]:
        return [
            kwargs["payload"]
            for service, kwargs in self.service_calls
            if service == "mqtt/publish" and kwargs["topic"] == topic
        ]


class FusionFunctionsTest(unittest.TestCase):
    def test_presence_normalization_and_household_fusion(self) -> None:
        self.assertIs(normalize_presence("home"), True)
        self.assertIs(normalize_presence("not_home"), False)
        self.assertIs(normalize_presence("Praca Arka"), False)
        self.assertIsNone(normalize_presence("unavailable"))

        self.assertEqual(derive_household_presence([True, True]), "all_home")
        self.assertEqual(derive_household_presence([True, False]), "partial")
        self.assertEqual(derive_household_presence([False, False]), "empty")
        self.assertEqual(derive_household_presence([False, None]), "uncertain")

    def test_activity_and_operating_mode(self) -> None:
        self.assertEqual(derive_operating_mode("all_home", "awake"), "home")
        self.assertEqual(derive_operating_mode("partial", "asleep"), "night")
        self.assertEqual(derive_operating_mode("empty", "awake"), "away")


class HomeMonitorAppTest(unittest.TestCase):
    def test_initialize_publishes_discovery_and_fused_states(self) -> None:
        app = FakeHomeMonitor(_args(), _states())
        app.initialize()

        discovery_topics = {
            kwargs["topic"]
            for service, kwargs in app.service_calls
            if service == "mqtt/publish" and kwargs["topic"].endswith("/config")
        }
        self.assertIn(
            "homeassistant/sensor/home_monitor_household_presence/config",
            discovery_topics,
        )
        self.assertIn(
            "homeassistant/sensor/home_monitor_occupancy/config",
            discovery_topics,
        )
        self.assertIn(
            "homeassistant/sensor/home_monitor_arek_journey/config",
            discovery_topics,
        )
        self.assertIn(
            "homeassistant/sensor/home_monitor_sabina_journey/config",
            discovery_topics,
        )
        self.assertIn(
            "homeassistant/button/home_monitor_sleep_wake/config",
            discovery_topics,
        )

        self.assertEqual(
            app.mqtt_payloads("home_monitor/state/household_presence")[-1],
            "all_home",
        )
        self.assertEqual(
            app.mqtt_payloads("home_monitor/state/operating_mode")[-1], "home"
        )
        self.assertEqual(
            app.mqtt_payloads("home_monitor/state/next_wake_time")[-1],
            "2026-07-23T06:10:00+00:00",
        )
        self.assertEqual(app.mqtt_payloads("home_monitor/availability")[-1], "online")

        discovery_payload = json.loads(
            app.mqtt_payloads(
                "homeassistant/sensor/home_monitor_household_presence/config"
            )[-1]
        )
        self.assertEqual(
            discovery_payload["default_entity_id"],
            "sensor.home_monitor_household_presence",
        )
        self.assertEqual(discovery_payload["expire_after"], 180)
        button_payload = json.loads(
            app.mqtt_payloads("homeassistant/button/home_monitor_sleep_wake/config")[-1]
        )
        self.assertEqual(
            button_payload["default_entity_id"],
            "button.home_monitor_sleep_wake",
        )
        self.assertEqual(
            button_payload["command_topic"],
            "home_monitor/command/sleep_wake",
        )
        self.assertEqual(button_payload["payload_press"], "PRESS")
        state_call = next(
            kwargs
            for service, kwargs in app.service_calls
            if service == "mqtt/publish"
            and kwargs["topic"] == "home_monitor/state/household_presence"
        )
        self.assertIs(state_call["retain"], False)
        activity_call = next(
            kwargs
            for service, kwargs in app.service_calls
            if service == "mqtt/publish"
            and kwargs["topic"] == "home_monitor/state/activity"
        )
        self.assertIs(activity_call["retain"], True)

        # Initialization only observes HA inputs and communicates through MQTT.
        self.assertTrue(
            all(
                service in {"mqtt/publish", "mqtt/subscribe"}
                for service, _ in app.service_calls
            )
        )
        self.assertTrue(
            any(
                service == "mqtt/subscribe"
                and kwargs["topic"] == "home_monitor/command/sleep_wake"
                for service, kwargs in app.service_calls
            )
        )
        self.assertTrue(
            any(
                service == "mqtt/subscribe"
                and kwargs["topic"] == "home_monitor/state/activity"
                for service, kwargs in app.service_calls
            )
        )
        self.assertEqual(app.daily_schedules[0][1], "09:00:00")

    def test_sleep_wake_button_sleeps_an_awake_house(self) -> None:
        app = FakeHomeMonitor(_args(), _states())
        app.initialize()

        app._mqtt_message_received(
            "MQTT_MESSAGE",
            {"topic": "home_monitor/command/sleep_wake", "payload": b"PRESS"},
            {},
        )

        self.assertEqual(app._activity_state, "asleep")
        self.assertEqual(app.mqtt_payloads("home_monitor/state/activity")[-1], "asleep")

    def test_sleep_wake_button_wakes_only_in_morning_window(self) -> None:
        app = FakeHomeMonitor(
            _args(),
            _states(**{"sensor.home_monitor_activity": "asleep"}),
        )
        app.initialize()

        app._mqtt_message_received(
            "MQTT_MESSAGE",
            {"topic": "home_monitor/command/sleep_wake", "payload": "PRESS"},
            {},
        )

        self.assertEqual(app._activity_state, "awake")
        self.assertEqual(app.mqtt_payloads("home_monitor/state/activity")[-1], "awake")

    def test_sleep_wake_button_does_not_wake_at_night(self) -> None:
        app = FakeHomeMonitor(
            _args(),
            _states(**{"sensor.home_monitor_activity": "asleep"}),
        )
        app._fixed_now = datetime(2026, 7, 23, 1, 0, tzinfo=timezone.utc)
        app.initialize()

        app._mqtt_message_received(
            "MQTT_MESSAGE",
            {"topic": "home_monitor/command/sleep_wake", "payload": "PRESS"},
            {},
        )

        self.assertEqual(app._activity_state, "asleep")
        self.assertTrue(
            any(
                level == "WARNING" and "wake is allowed only" in message
                for level, message in app.logs
            )
        )

    def test_only_arek_alarm_drives_wake_timer(self) -> None:
        app = FakeHomeMonitor(
            _args(), _states(**{"sensor.home_monitor_activity": "asleep"})
        )
        app.initialize()

        wake_timers = [
            timer
            for timer in app.timers
            if timer["callback"].__name__ == "_wake_alarm_triggered"
        ]
        self.assertEqual(len(wake_timers), 1)
        self.assertEqual(wake_timers[0]["delay"], 6600)
        self.assertEqual(
            app.mqtt_payloads("home_monitor/state/next_wake_time")[-1],
            "2026-07-23T06:10:00+00:00",
        )
        wake_timers[0]["callback"]({})
        self.assertEqual(app._activity_state, "awake")

    def test_short_home_gps_bounce_never_confirms_arrival(self) -> None:
        app = FakeHomeMonitor(
            _args(),
            _states(
                **{
                    "person.arek": "not_home",
                    "person.sabina": "not_home",
                    "binary_sensor.arek_android_auto": "on",
                }
            ),
        )
        app.initialize()
        arek = app._people["arek"]
        self.assertEqual(arek.journey_state(), "driving")

        app._person_presence_changed(
            "person.arek", "state", "not_home", "home", {"person_slug": "arek"}
        )
        arrival_timer = arek.timer_handle
        self.assertEqual(arek.journey_state(), "arriving")
        self.assertEqual(app.timers[-1]["delay"], 60)

        app._person_presence_changed(
            "person.arek", "state", "home", "not_home", {"person_slug": "arek"}
        )
        self.assertIn(arrival_timer, app.cancelled_timers)
        self.assertIs(arek.confirmed_home, False)
        self.assertEqual(arek.journey_state(), "driving")
        self.assertEqual(
            app.mqtt_payloads("home_monitor/state/household_presence")[-1], "empty"
        )

    def test_configured_work_zone_is_exposed_as_work_journey(self) -> None:
        app = FakeHomeMonitor(
            _args(),
            _states(
                **{
                    "person.arek": "Praca Arka",
                    "binary_sensor.arek_android_auto": "on",
                }
            ),
        )
        app.initialize()

        arek = app._people["arek"]
        self.assertEqual(arek.journey_state(), "work")
        self.assertEqual(
            app.mqtt_payloads("home_monitor/state/arek_journey")[-1], "work"
        )
        attributes = json.loads(
            app.mqtt_payloads("home_monitor/attributes/arek_journey")[-1]
        )
        self.assertTrue(attributes["at_work"])
        self.assertEqual(attributes["work_zone_entity"], "zone.praca_arka")
        self.assertEqual(attributes["current_zone_entity"], "zone.praca_arka")
        self.assertEqual(
            app.mqtt_payloads("home_monitor/state/household_presence")[-1],
            "partial",
        )

    def test_stale_arek_alarm_is_not_executed_immediately(self) -> None:
        app = FakeHomeMonitor(
            _args(),
            _states(**{"sensor.arek_alarm": "2026-07-23T04:00:00+00:00"}),
        )
        app.initialize()
        self.assertFalse(
            any(
                timer["callback"].__name__ == "_wake_alarm_triggered"
                for timer in app.timers
            )
        )
        self.assertEqual(
            app.mqtt_payloads("home_monitor/state/next_wake_time")[-1],
            "2026-07-23T06:10:00+00:00",
        )

    def test_invalid_arek_alarm_cancels_the_previous_timer(self) -> None:
        app = FakeHomeMonitor(_args(), _states())
        app.initialize()
        previous_handle = app._wake_alarm_handle

        app._person_alarm_changed(
            "sensor.arek_alarm",
            "state",
            "2026-07-23T06:50:00+00:00",
            "unavailable",
            {"person_slug": "arek"},
        )

        self.assertIn(previous_handle, app.cancelled_timers)
        self.assertIsNone(app._wake_alarm_handle)
        self.assertEqual(
            app.mqtt_payloads("home_monitor/state/next_wake_time")[-1],
            "2026-07-23T06:10:00+00:00",
        )

    def test_force_wake_deadline_updates_activity(self) -> None:
        app = FakeHomeMonitor(
            _args(), _states(**{"sensor.home_monitor_activity": "asleep"})
        )
        app.initialize()

        app._force_wake({})

        self.assertEqual(app._activity_state, "awake")
        attributes = json.loads(
            app.mqtt_payloads("home_monitor/attributes/activity")[-1]
        )
        self.assertEqual(attributes["last_transition_reason"], "force_wake_deadline")

    def test_retained_mqtt_activity_restores_when_ha_entity_is_unavailable(
        self,
    ) -> None:
        app = FakeHomeMonitor(
            _args(), _states(**{"sensor.home_monitor_activity": "unavailable"})
        )
        app.initialize()
        self.assertTrue(app._activity_restore_pending)
        self.assertEqual(app.mqtt_payloads("home_monitor/state/activity"), [])

        app._mqtt_message_received(
            "MQTT_MESSAGE",
            {"topic": "home_monitor/state/activity", "payload": "asleep"},
            {},
        )

        self.assertFalse(app._activity_restore_pending)
        self.assertEqual(app._activity_state, "asleep")
        self.assertEqual(app.mqtt_payloads("home_monitor/state/activity")[-1], "asleep")

    def test_first_start_uses_configured_activity_after_restore_window(self) -> None:
        app = FakeHomeMonitor(
            _args(), _states(**{"sensor.home_monitor_activity": "unknown"})
        )
        app.initialize()
        restore_timer = next(
            timer
            for timer in app.timers
            if timer["callback"].__name__ == "_finish_activity_restore"
        )

        restore_timer["callback"]({})

        self.assertEqual(app._activity_state, "awake")
        self.assertEqual(app.mqtt_payloads("home_monitor/state/activity")[-1], "awake")

    def test_unavailable_person_uses_grace_period_before_uncertain(self) -> None:
        app = FakeHomeMonitor(_args(), _states())
        app.initialize()
        arek = app._people["arek"]

        app._person_presence_changed(
            "person.arek", "state", "home", "unavailable", {"person_slug": "arek"}
        )
        timer = app.timers[-1]
        self.assertEqual(timer["delay"], 300)
        self.assertIs(arek.confirmed_home, True)
        self.assertEqual(
            app.mqtt_payloads("home_monitor/state/household_presence")[-1],
            "all_home",
        )

        app._confirm_presence(timer["kwargs"])
        self.assertIsNone(arek.confirmed_home)
        self.assertEqual(arek.journey_state(), "unknown")
        self.assertEqual(
            app.mqtt_payloads("home_monitor/state/household_presence")[-1],
            "partial",
        )


if __name__ == "__main__":
    unittest.main()
