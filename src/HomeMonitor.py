"""Home context fusion for AppDaemon.

The app observes noisy Home Assistant entities, turns them into stable abstract
states, and exposes those states through Home Assistant MQTT discovery.  It does
not operate gates, garage doors, alarms, or other physical devices.

The app owns and persists its abstract activity state.  Existing Home Assistant
automations can keep consuming their current entities while they are migrated;
this component does not modify or operate them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
import json
import re
from typing import Any, Iterable, Optional

import appdaemon.plugins.hass.hassapi as hass


INVALID_STATES = {"", "none", "unknown", "unavailable"}


def _normalized_state(value: Any) -> Optional[str]:
    """Return a comparable HA state, or ``None`` when it is unusable."""
    if value is None:
        return None
    state = str(value).strip().lower()
    return None if state in INVALID_STATES else state


def normalize_presence(value: Any) -> Optional[bool]:
    """Map a person state to home/away/unknown.

    Named zones are deliberately treated as away.  Only the HA ``home`` zone is
    considered home.
    """
    state = _normalized_state(value)
    if state is None:
        return None
    return state == "home"


def normalize_binary(value: Any) -> Optional[bool]:
    """Map a binary sensor/input_boolean state to true/false/unknown."""
    state = _normalized_state(value)
    if state == "on":
        return True
    if state == "off":
        return False
    return None


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    normalized = str(value).strip().lower()
    if normalized in {"true", "yes", "on", "1"}:
        return True
    if normalized in {"false", "no", "off", "0"}:
        return False
    return default


def derive_household_presence(values: Iterable[Optional[bool]]) -> str:
    """Fuse stable per-person states into one household presence state."""
    states = list(values)
    if not states or all(value is None for value in states):
        return "uncertain"
    if all(value is True for value in states):
        return "all_home"
    if all(value is False for value in states):
        return "empty"
    if any(value is True for value in states):
        return "partial"
    return "uncertain"


def derive_operating_mode(household_presence: str, activity: str) -> str:
    """Derive the current high-level operating mode without causing actions."""
    if household_presence == "empty":
        return "away"
    if household_presence == "uncertain" or activity == "unknown":
        return "uncertain"
    if activity == "asleep":
        return "night"
    return "home"


def derive_time_period(
    now: datetime,
    sun_state: Any,
    morning_start_hour: int,
    evening_start_hour: int,
    night_start_hour: int,
) -> str:
    """Return a stable, human-friendly part of day."""
    hour = now.hour
    if hour < morning_start_hour or hour >= night_start_hour:
        return "night"
    if hour < 12:
        return "morning"
    if hour >= evening_start_hour or _normalized_state(sun_state) == "below_horizon":
        return "evening"
    return "day"


def _slugify(value: Any, fallback: str = "entity") -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower()).strip("_")
    return slug or fallback


def _zone_state_from_entity(entity_id: Any) -> Optional[str]:
    """Return the person-state name represented by a configured zone entity."""
    if entity_id is None:
        return None
    raw_entity_id = str(entity_id).strip()
    if not raw_entity_id.startswith("zone."):
        return None
    return _normalized_state(raw_entity_id.split(".", 1)[1].replace("_", " "))


@dataclass(frozen=True)
class PersonConfig:
    slug: str
    name: str
    person_entity: str
    work_zone_entity: Optional[str] = None
    android_auto_entity: Optional[str] = None
    next_alarm_entity: Optional[str] = None


@dataclass
class PersonRuntime:
    config: PersonConfig
    work_zone_state: Optional[str] = None
    raw_state: str = "unknown"
    raw_home: Optional[bool] = None
    confirmed_home: Optional[bool] = None
    driving: Optional[bool] = None
    alarm: Optional[datetime] = None
    pending_confirmation: bool = False
    candidate_home: Optional[bool] = None
    timer_handle: Any = None

    def at_work(self) -> bool:
        """Return whether the stable away state is the configured work zone."""
        return (
            self.confirmed_home is False
            and self.work_zone_state is not None
            and self.raw_state == self.work_zone_state
        )

    def journey_state(self) -> str:
        """Return the abstract per-person state used by downstream automations."""
        if self.pending_confirmation and self.candidate_home is True:
            return "arriving"
        if self.pending_confirmation and self.candidate_home is False:
            return "leaving"
        if self.confirmed_home is True:
            return "home"
        if self.at_work():
            return "work"
        if self.confirmed_home is False and self.driving is True:
            return "driving"
        if self.confirmed_home is False:
            return "away"
        return "unknown"


@dataclass(frozen=True)
class MqttEntity:
    key: str
    domain: str
    name: str
    icon: str
    device_class: Optional[str] = None
    entity_category: Optional[str] = None


class HomeMonitor(hass.Hass):
    """Fuse household context and publish it as MQTT-discovered HA entities."""

    def initialize(self) -> None:
        """Read configuration, attach listeners, and publish initial context."""
        self._published_payloads: dict[str, str] = {}
        self._people: dict[str, PersonRuntime] = {}
        self._wake_alarm_handle: Any = None
        self._activity_state: Optional[str] = None
        self._activity_restore_pending = True
        self._activity_reason = "restoring"
        self._activity_changed_at: Optional[datetime] = None

        if not self._configure():
            return

        self._load_people()
        if not self._people:
            self.log("At least one valid persons entry is required", level="ERROR")
            return

        self._register_entities()
        self._subscribe_controls()
        self._attach_listeners()
        self._initialize_inputs()

        self._publish_availability("online")
        self._recompute_and_publish(force=True)
        if self._activity_restore_pending:
            self.run_in(self._finish_activity_restore, 1)
        self.run_in(self._republish_initial_state, 2)

        self.run_daily(self._force_wake, self.force_wake_time)
        first_tick = self.datetime() + timedelta(seconds=self.heartbeat_seconds)
        self.run_every(self._heartbeat, first_tick, self.heartbeat_seconds)

        self.log(
            f"Home context fusion started for {', '.join(self._people)}; "
            f"MQTT base topic={self.mqtt_base_topic}",
            level="INFO",
        )

    def _configure(self) -> bool:
        activity = self.args.get("activity", {}) or {}
        initial_activity = str(activity.get("initial_state", "awake")).strip().lower()
        if initial_activity not in {"awake", "asleep"}:
            self.log(
                f"Invalid activity.initial_state={initial_activity!r}; using awake",
                level="WARNING",
            )
            initial_activity = "awake"
        self.initial_activity_state = initial_activity
        self.wake_alarm_person = _slugify(
            activity.get("wake_alarm_person", "arek"), "arek"
        )
        self.wake_alarm_start_hour = int(activity.get("wake_alarm_start_hour", 3))
        self.wake_alarm_end_hour = int(activity.get("wake_alarm_end_hour", 9))
        self.force_wake_time = str(activity.get("force_wake_time", "09:00:00"))

        presence = self.args.get("presence", {}) or {}
        self.home_confirm_seconds = max(
            0, int(presence.get("home_confirm_seconds", 60))
        )
        self.away_confirm_seconds = max(
            0, int(presence.get("away_confirm_seconds", 120))
        )
        self.unavailable_grace_seconds = max(
            0, int(presence.get("unavailable_grace_seconds", 300))
        )

        context = self.args.get("context", {}) or {}
        self.sun_entity = context.get("sun_entity", "sun.sun")
        self.morning_start_hour = int(context.get("morning_start_hour", 5))
        self.evening_start_hour = int(context.get("evening_start_hour", 18))
        self.night_start_hour = int(context.get("night_start_hour", 22))
        self.max_alarm_future_hours = max(
            1, int(context.get("max_alarm_future_hours", 36))
        )

        controls = self.args.get("controls", {}) or {}
        self.sleep_wake_button_name = str(
            controls.get("sleep_wake_button_name", "Sleep / Wake")
        )
        self.button_wake_time_start = int(
            controls.get("button_wake_time_start", self.wake_alarm_start_hour)
        )
        self.button_wake_time_end = int(controls.get("button_wake_time_end", 12))

        self.mqtt_namespace = self.args.get("mqtt_namespace", "mqtt")
        self.mqtt_discovery_prefix = self._topic(
            self.args.get("mqtt_discovery_prefix", "homeassistant")
        )
        self.mqtt_base_topic = self._topic(
            self.args.get("mqtt_base_topic", "home_monitor")
        )
        self.mqtt_device_id = _slugify(
            self.args.get("mqtt_device_id", "home_monitor"), "home_monitor"
        )
        self.mqtt_device_name = str(self.args.get("mqtt_device_name", "Home Monitor"))
        self.mqtt_qos = int(self.args.get("mqtt_qos", 0))
        self.mqtt_retain_state = _as_bool(
            self.args.get("mqtt_retain_state"), default=False
        )
        self.mqtt_expire_after = max(0, int(self.args.get("mqtt_expire_after", 180)))
        self.heartbeat_seconds = max(15, int(self.args.get("heartbeat_seconds", 60)))
        self.availability_topic = f"{self.mqtt_base_topic}/availability"
        self.sleep_wake_command_topic = f"{self.mqtt_base_topic}/command/sleep_wake"
        return True

    def _load_people(self) -> None:
        configured_people = self.args.get("persons", {}) or {}
        if not isinstance(configured_people, dict):
            self.log("persons must be a mapping", level="ERROR")
            return

        for configured_slug, data in configured_people.items():
            if not isinstance(data, dict):
                self.log(
                    f"Ignoring persons.{configured_slug}: expected a mapping",
                    level="ERROR",
                )
                continue
            person_entity = data.get("person_entity") or data.get("entity_id")
            if not person_entity:
                self.log(
                    f"Ignoring persons.{configured_slug}: person_entity is required",
                    level="ERROR",
                )
                continue
            slug = _slugify(configured_slug, "person")
            work_zone_entity = data.get("work_zone_entity")
            work_zone_state = _zone_state_from_entity(work_zone_entity)
            if work_zone_entity and work_zone_state is None:
                self.log(
                    f"Ignoring persons.{configured_slug}.work_zone_entity="
                    f"{work_zone_entity!r}: expected a zone.* entity ID",
                    level="WARNING",
                )
                work_zone_entity = None
            config = PersonConfig(
                slug=slug,
                name=str(data.get("name", str(configured_slug).title())),
                person_entity=str(person_entity),
                work_zone_entity=(
                    str(work_zone_entity) if work_zone_entity is not None else None
                ),
                android_auto_entity=data.get("android_auto_entity"),
                next_alarm_entity=data.get("next_alarm_entity"),
            )
            self._people[slug] = PersonRuntime(
                config=config,
                work_zone_state=work_zone_state,
            )

    def _attach_listeners(self) -> None:
        if self.sun_entity:
            self.listen_state(self._context_input_changed, self.sun_entity)

        for slug, runtime in self._people.items():
            config = runtime.config
            self.listen_state(
                self._person_presence_changed,
                config.person_entity,
                person_slug=slug,
            )
            if config.android_auto_entity:
                self.listen_state(
                    self._android_auto_changed,
                    config.android_auto_entity,
                    person_slug=slug,
                )
            if config.next_alarm_entity:
                self.listen_state(
                    self._person_alarm_changed,
                    config.next_alarm_entity,
                    person_slug=slug,
                )

    def _initialize_inputs(self) -> None:
        for runtime in self._people.values():
            config = runtime.config
            raw_presence = self.get_state(config.person_entity)
            self._observe_presence(runtime, raw_presence, initial=True)
            if config.android_auto_entity:
                runtime.driving = normalize_binary(
                    self.get_state(config.android_auto_entity)
                )
            if config.next_alarm_entity:
                runtime.alarm = self._parse_iso_datetime(
                    self.get_state(config.next_alarm_entity)
                )

        restored_activity = _normalized_state(
            self.get_state(f"sensor.{self.mqtt_device_id}_activity")
        )
        if restored_activity in {"awake", "asleep"}:
            self._restore_activity(restored_activity, "home_assistant_restore")

        self._schedule_wake_alarm()

    # ------------------------------------------------------------------
    # Presence confirmation and fusion

    def _person_presence_changed(
        self, entity: str, attribute: str, old: Any, new: Any, kwargs: dict
    ) -> None:
        if attribute != "state":
            return
        person_slug = kwargs.get("person_slug")
        if not isinstance(person_slug, str):
            return
        runtime = self._people.get(person_slug)
        if runtime is None:
            return
        self._observe_presence(runtime, new)
        self._recompute_and_publish()

    def _observe_presence(
        self, runtime: PersonRuntime, raw_state: Any, initial: bool = False
    ) -> None:
        normalized = _normalized_state(raw_state)
        observed_home = normalize_presence(raw_state)
        runtime.raw_state = normalized or "unknown"
        runtime.raw_home = observed_home

        if initial:
            runtime.confirmed_home = observed_home
            runtime.pending_confirmation = False
            runtime.candidate_home = None
            return

        if observed_home == runtime.confirmed_home and observed_home is not None:
            self._cancel_presence_timer(runtime)
            runtime.pending_confirmation = False
            runtime.candidate_home = None
            return

        if (
            runtime.pending_confirmation
            and runtime.candidate_home is observed_home
            and runtime.timer_handle is not None
        ):
            return

        self._cancel_presence_timer(runtime)
        runtime.pending_confirmation = True
        runtime.candidate_home = observed_home

        if observed_home is True:
            delay = self.home_confirm_seconds
        elif observed_home is False:
            delay = self.away_confirm_seconds
        else:
            delay = self.unavailable_grace_seconds

        runtime.timer_handle = self.run_in(
            self._confirm_presence,
            delay,
            person_slug=runtime.config.slug,
            expected_home=observed_home,
        )
        self.log(
            f"{runtime.config.name}: raw presence={runtime.raw_state}; "
            f"waiting {delay}s before confirmation",
            level="DEBUG",
        )

    def _confirm_presence(self, kwargs: dict) -> None:
        person_slug = kwargs.get("person_slug")
        if not isinstance(person_slug, str):
            return
        runtime = self._people.get(person_slug)
        if runtime is None:
            return
        expected_home = kwargs.get("expected_home")
        runtime.timer_handle = None
        if runtime.raw_home is not expected_home:
            return

        previous = runtime.confirmed_home
        runtime.confirmed_home = expected_home
        runtime.pending_confirmation = False
        runtime.candidate_home = None
        self.log(
            f"{runtime.config.name}: stable presence changed "
            f"from {previous!r} to {expected_home!r} (raw={runtime.raw_state})",
            level="INFO",
        )
        self._recompute_and_publish()

    def _cancel_presence_timer(self, runtime: PersonRuntime) -> None:
        if runtime.timer_handle is None:
            return
        try:
            self.cancel_timer(runtime.timer_handle)
        except Exception as error:
            self.log(
                f"Could not cancel presence timer for {runtime.config.name}: {error}",
                level="WARNING",
            )
        runtime.timer_handle = None

    def _android_auto_changed(
        self, entity: str, attribute: str, old: Any, new: Any, kwargs: dict
    ) -> None:
        if attribute != "state":
            return
        person_slug = kwargs.get("person_slug")
        if not isinstance(person_slug, str):
            return
        runtime = self._people.get(person_slug)
        if runtime is None:
            return
        runtime.driving = normalize_binary(new)
        self._recompute_and_publish()

    def _person_alarm_changed(
        self, entity: str, attribute: str, old: Any, new: Any, kwargs: dict
    ) -> None:
        if attribute != "state":
            return
        person_slug = kwargs.get("person_slug")
        if not isinstance(person_slug, str):
            return
        runtime = self._people.get(person_slug)
        if runtime is None:
            return
        runtime.alarm = self._parse_iso_datetime(new)
        if person_slug == self.wake_alarm_person:
            self._schedule_wake_alarm()
        self._recompute_and_publish()

    def _context_input_changed(
        self, entity: str, attribute: str, old: Any, new: Any, kwargs: dict
    ) -> None:
        if attribute == "state":
            self._recompute_and_publish()

    def _next_wake(self, now: datetime) -> tuple[Optional[datetime], Optional[str]]:
        latest = now + timedelta(hours=self.max_alarm_future_hours)
        candidates: list[tuple[datetime, str]] = []
        for slug, runtime in self._people.items():
            alarm = runtime.alarm
            if alarm is None:
                continue
            local_alarm = alarm.astimezone(now.tzinfo)
            if now < local_alarm <= latest and self._in_waking_window(
                local_alarm.time()
            ):
                candidates.append((local_alarm, slug))
        if not candidates:
            return None, None
        return min(candidates, key=lambda item: item[0])

    def _recompute_and_publish(self, force: bool = False) -> None:
        if not self._people:
            return
        now = self._now()
        presence = derive_household_presence(
            runtime.confirmed_home for runtime in self._people.values()
        )
        activity = self._activity_state or "unknown"
        sun_state = self.get_state(self.sun_entity) if self.sun_entity else None
        period = derive_time_period(
            now,
            sun_state,
            self.morning_start_hour,
            self.evening_start_hour,
            self.night_start_hour,
        )
        mode = derive_operating_mode(presence, activity)
        next_wake, next_wake_person = self._next_wake(now)

        people_states = {
            slug: runtime.journey_state() for slug, runtime in self._people.items()
        }
        stable_home = {
            slug: runtime.confirmed_home for slug, runtime in self._people.items()
        }
        common_attributes = {
            "people": people_states,
            "stable_home": stable_home,
            "updated_at": now.isoformat(),
        }

        self._publish_entity(
            "household_presence",
            presence,
            {
                **common_attributes,
                "raw_states": {
                    slug: runtime.raw_state for slug, runtime in self._people.items()
                },
            },
            force,
        )
        occupancy = (
            "occupied"
            if presence in {"all_home", "partial"}
            else "empty" if presence == "empty" else "uncertain"
        )
        self._publish_entity("occupancy", occupancy, common_attributes, force)
        self._publish_entity(
            "activity",
            activity,
            {
                "last_transition_reason": self._activity_reason,
                "last_transition_at": (
                    self._activity_changed_at.isoformat()
                    if self._activity_changed_at
                    else None
                ),
                "updated_at": now.isoformat(),
            },
            force,
        )
        self._publish_entity(
            "operating_mode",
            mode,
            {
                "household_presence": presence,
                "activity": activity,
                "time_period": period,
                "updated_at": now.isoformat(),
            },
            force,
        )
        self._publish_entity(
            "time_period",
            period,
            {
                "sun_entity": self.sun_entity,
                "sun_state": sun_state,
                "local_hour": now.hour,
                "updated_at": now.isoformat(),
            },
            force,
        )
        self._publish_entity(
            "next_wake_time",
            next_wake.isoformat() if next_wake else "unknown",
            {
                "person": next_wake_person,
                "source_entity": (
                    self._people[next_wake_person].config.next_alarm_entity
                    if next_wake_person
                    else None
                ),
                "candidate_alarms": {
                    slug: runtime.alarm.isoformat() if runtime.alarm else None
                    for slug, runtime in self._people.items()
                },
                "updated_at": now.isoformat(),
            },
            force,
        )

        for slug, runtime in self._people.items():
            config = runtime.config
            self._publish_entity(
                f"{slug}_journey",
                runtime.journey_state(),
                {
                    "person_entity": config.person_entity,
                    "raw_person_state": runtime.raw_state,
                    "confirmed_home": runtime.confirmed_home,
                    "pending_confirmation": runtime.pending_confirmation,
                    "pending_state": (
                        "home"
                        if runtime.candidate_home is True
                        else "away" if runtime.candidate_home is False else "unknown"
                    ),
                    "android_auto_entity": config.android_auto_entity,
                    "android_auto_connected": runtime.driving,
                    "work_zone_entity": config.work_zone_entity,
                    "at_work": runtime.at_work(),
                    "current_zone_entity": (
                        "zone.home"
                        if runtime.confirmed_home is True
                        else config.work_zone_entity if runtime.at_work() else None
                    ),
                    "next_alarm_entity": config.next_alarm_entity,
                    "updated_at": now.isoformat(),
                },
                force,
            )

    # ------------------------------------------------------------------
    # MQTT discovery and state publishing

    def _entity_definitions(self) -> list[MqttEntity]:
        definitions = [
            MqttEntity(
                "household_presence",
                "sensor",
                "Household presence",
                "mdi:home-account",
            ),
            MqttEntity(
                "occupancy",
                "sensor",
                "Occupancy",
                "mdi:home-account",
            ),
            MqttEntity("activity", "sensor", "Household activity", "mdi:sleep"),
            MqttEntity("operating_mode", "sensor", "Operating mode", "mdi:home-switch"),
            MqttEntity("time_period", "sensor", "Time period", "mdi:theme-light-dark"),
            MqttEntity(
                "next_wake_time",
                "sensor",
                "Next wake time",
                "mdi:alarm",
                device_class="timestamp",
            ),
        ]
        definitions.extend(
            MqttEntity(
                f"{slug}_journey",
                "sensor",
                f"{runtime.config.name} journey",
                "mdi:map-marker-account",
            )
            for slug, runtime in self._people.items()
        )
        return definitions

    def _register_entities(self) -> None:
        self._mqtt_entities = {
            definition.key: definition for definition in self._entity_definitions()
        }
        for definition in self._mqtt_entities.values():
            entity_id = f"{definition.domain}.{self.mqtt_device_id}_{definition.key}"
            object_id = entity_id.split(".", 1)[1]
            payload: dict[str, Any] = {
                "name": definition.name,
                "unique_id": f"{self.mqtt_device_id}_{definition.key}",
                "default_entity_id": entity_id,
                "state_topic": self._state_topic(definition.key),
                "json_attributes_topic": self._attributes_topic(definition.key),
                "availability_topic": self.availability_topic,
                "payload_available": "online",
                "payload_not_available": "offline",
                "icon": definition.icon,
                "device": {
                    "identifiers": [self.mqtt_device_id],
                    "name": self.mqtt_device_name,
                    "manufacturer": "AppDaemon",
                    "model": "HomeMonitor context fusion",
                },
                "origin": {"name": "HomeMonitor AppDaemon"},
            }
            if self.mqtt_expire_after:
                payload["expire_after"] = self.mqtt_expire_after
            if definition.device_class:
                payload["device_class"] = definition.device_class
            if definition.entity_category:
                payload["entity_category"] = definition.entity_category

            discovery_topic = (
                f"{self.mqtt_discovery_prefix}/{definition.domain}/"
                f"{object_id}/config"
            )
            self._publish(discovery_topic, payload, retain=True)

        button_object_id = f"{self.mqtt_device_id}_sleep_wake"
        button_payload = {
            "name": self.sleep_wake_button_name,
            "unique_id": button_object_id,
            "default_entity_id": f"button.{button_object_id}",
            "command_topic": self.sleep_wake_command_topic,
            "payload_press": "PRESS",
            "retain": False,
            "availability_topic": self.availability_topic,
            "payload_available": "online",
            "payload_not_available": "offline",
            "icon": "mdi:sleep",
            "device": {
                "identifiers": [self.mqtt_device_id],
                "name": self.mqtt_device_name,
                "manufacturer": "AppDaemon",
                "model": "HomeMonitor context fusion",
            },
            "origin": {"name": "HomeMonitor AppDaemon"},
        }
        self._publish(
            f"{self.mqtt_discovery_prefix}/button/{button_object_id}/config",
            button_payload,
            retain=True,
        )

    def _subscribe_controls(self) -> None:
        """Subscribe to the control and persistent activity-state topics."""
        try:
            for topic in (
                self.sleep_wake_command_topic,
                self._state_topic("activity"),
            ):
                self.listen_event(
                    self._mqtt_message_received,
                    "MQTT_MESSAGE",
                    topic=topic,
                    namespace=self.mqtt_namespace,
                )
                self.call_service(
                    "mqtt/subscribe",
                    topic=topic,
                    namespace=self.mqtt_namespace,
                )
        except Exception as error:
            self.log(
                f"MQTT subscription failed: {error}",
                level="ERROR",
            )

    def _mqtt_message_received(self, event_name: str, data: dict, kwargs: dict) -> None:
        """Handle the command button and retained activity-state restoration."""
        topic = (data or {}).get("topic") or (kwargs or {}).get("topic")
        payload = (data or {}).get("payload")
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8", errors="ignore")
        normalized_payload = str(payload or "").strip()

        if (
            topic == self.sleep_wake_command_topic
            and normalized_payload.upper() == "PRESS"
        ):
            self._handle_sleep_wake_request()
            return
        if topic == self._state_topic("activity"):
            restored_activity = normalized_payload.lower()
            if self._activity_restore_pending and restored_activity in {
                "awake",
                "asleep",
            }:
                self._restore_activity(restored_activity, "mqtt_restore")
                self._recompute_and_publish(force=True)
            return

        self.log(
            f"Ignoring unsupported MQTT control: "
            f"topic={topic}, payload={normalized_payload}",
            level="DEBUG",
        )

    def _handle_sleep_wake_request(self) -> None:
        """Put the house to sleep, or wake it during the morning window."""
        now = self._now()

        if self._activity_state == "awake":
            self.log(
                f"Sleep / Wake pressed at {now.isoformat()}: "
                "setting house activity to asleep.",
                level="INFO",
            )
            self._set_activity("asleep", "sleep_wake_button")
            return

        if self._activity_state == "asleep" and self._in_hour_window(
            now.time(),
            self.button_wake_time_start,
            self.button_wake_time_end,
        ):
            self.log(
                f"Sleep / Wake pressed at {now.isoformat()}: "
                "setting house activity to awake.",
                level="INFO",
            )
            self._set_activity("awake", "sleep_wake_button")
            return

        if self._activity_state == "asleep":
            self.log(
                f"Sleep / Wake pressed at {now.isoformat()}, but wake is allowed "
                f"only from {self.button_wake_time_start:02d}:00 to "
                f"{self.button_wake_time_end:02d}:00.",
                level="WARNING",
            )
            return

        self.log(
            "Sleep / Wake ignored while activity state is being restored.",
            level="WARNING",
        )

    def _publish_entity(
        self,
        key: str,
        state: Any,
        attributes: dict[str, Any],
        force: bool,
    ) -> None:
        if key == "activity" and self._activity_restore_pending:
            return

        state_payload = str(state)
        attributes_payload = json.dumps(
            attributes, sort_keys=True, separators=(",", ":"), default=str
        )
        state_topic = self._state_topic(key)
        attributes_topic = self._attributes_topic(key)

        retain = key == "activity" or self.mqtt_retain_state
        if force or self._published_payloads.get(state_topic) != state_payload:
            self._publish(state_topic, state_payload, retain=retain)
            self._published_payloads[state_topic] = state_payload
        if (
            force
            or self._published_payloads.get(attributes_topic) != attributes_payload
        ):
            self._publish(
                attributes_topic,
                attributes_payload,
                retain=retain,
            )
            self._published_payloads[attributes_topic] = attributes_payload

    def _publish_availability(self, state: str) -> None:
        self._publish(self.availability_topic, state, retain=True)

    def _publish(self, topic: str, payload: Any, retain: bool) -> None:
        if isinstance(payload, (dict, list)):
            payload = json.dumps(
                payload, sort_keys=True, separators=(",", ":"), default=str
            )
        try:
            self.call_service(
                "mqtt/publish",
                topic=topic,
                payload=payload,
                retain=retain,
                qos=self.mqtt_qos,
                namespace=self.mqtt_namespace,
            )
        except Exception as error:
            self.log(f"MQTT publish failed for {topic}: {error}", level="ERROR")

    def _state_topic(self, key: str) -> str:
        return f"{self.mqtt_base_topic}/state/{key}"

    def _attributes_topic(self, key: str) -> str:
        return f"{self.mqtt_base_topic}/attributes/{key}"

    @staticmethod
    def _topic(value: Any) -> str:
        normalized = str(value).strip().strip("/")
        return normalized or "home_monitor"

    def _heartbeat(self, kwargs: dict) -> None:
        self._publish_availability("online")
        self._recompute_and_publish(force=True)

    def _republish_initial_state(self, kwargs: dict) -> None:
        """Republish after discovery subscribers have had time to attach."""
        self._publish_availability("online")
        self._recompute_and_publish(force=True)

    # ------------------------------------------------------------------
    # Persistent activity state and wake scheduling

    def _restore_activity(self, state: str, reason: str) -> None:
        """Restore valid persisted activity without treating it as a transition."""
        if state not in {"awake", "asleep"}:
            return
        self._activity_state = state
        self._activity_restore_pending = False
        self._activity_reason = reason
        self._activity_changed_at = self._now()
        self.log(f"Restored house activity as {state} from {reason}.", level="INFO")

    def _finish_activity_restore(self, kwargs: dict) -> None:
        """Use the configured default only when no retained state was available."""
        if not self._activity_restore_pending:
            return
        self._set_activity(self.initial_activity_state, "configured_initial_state")

    def _set_activity(self, state: str, reason: str) -> None:
        """Set and publish the single authoritative abstract activity state."""
        if state not in {"awake", "asleep"}:
            self.log(f"Ignoring invalid activity state: {state!r}", level="ERROR")
            return
        if not self._activity_restore_pending and self._activity_state == state:
            return

        previous = self._activity_state or "unknown"
        self._activity_state = state
        self._activity_restore_pending = False
        self._activity_reason = reason
        self._activity_changed_at = self._now()
        self.log(
            f"House activity changed from {previous} to {state}; reason={reason}.",
            level="INFO",
        )
        self._recompute_and_publish()

    def _local_timezone(self):
        return self._now().tzinfo

    def _now(self) -> datetime:
        now = self.datetime(aware=True)
        if now.tzinfo is None:
            now = now.astimezone()
        return now

    def _parse_iso_datetime(self, value: Any) -> Optional[datetime]:
        state = _normalized_state(value)
        if state is None:
            return None
        raw = str(value).strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=self._local_timezone())
        return parsed

    def _in_waking_window(self, alarm_time: time) -> bool:
        return self._in_hour_window(
            alarm_time, self.wake_alarm_start_hour, self.wake_alarm_end_hour
        )

    @staticmethod
    def _in_hour_window(value: time, start_hour: int, end_hour: int) -> bool:
        start = time(start_hour, 0)
        end = time(end_hour, 0)
        if start <= end:
            return start <= value <= end
        return value >= start or value <= end

    def _schedule_wake_alarm(self) -> None:
        """Schedule the configured person's next valid alarm as a wake trigger."""
        self._cancel_wake_alarm_timer()
        runtime = self._people.get(self.wake_alarm_person)
        if runtime is None:
            self.log(
                f"Wake alarm person {self.wake_alarm_person!r} is not configured.",
                level="WARNING",
            )
            return
        next_alarm = runtime.alarm
        if next_alarm is None:
            self.log(
                f"{runtime.config.name} wake alarm is invalid or empty.",
                level="DEBUG",
            )
            return
        now = self._now()
        next_alarm_local = next_alarm.astimezone(now.tzinfo)
        if next_alarm_local <= now:
            self.log(
                f"{runtime.config.name} wake alarm is stale: "
                f"{next_alarm_local.isoformat()}",
                level="WARNING",
            )
            return
        if next_alarm_local > now + timedelta(hours=self.max_alarm_future_hours):
            self.log(
                f"{runtime.config.name} wake alarm is too far in the future.",
                level="DEBUG",
            )
            return
        if not self._in_waking_window(next_alarm_local.time()):
            self.log(
                f"{runtime.config.name} alarm is outside waking hours, ignoring.",
                level="INFO",
            )
            return

        delay = max(0, int((next_alarm_local - now).total_seconds()))
        self._wake_alarm_handle = self.run_in(self._wake_alarm_triggered, delay)
        self.log(
            f"Scheduled {runtime.config.name} alarm wake in {delay} seconds.",
            level="INFO",
        )

    def _cancel_wake_alarm_timer(self) -> None:
        if self._wake_alarm_handle is None:
            return
        try:
            self.cancel_timer(self._wake_alarm_handle)
        except Exception as error:
            self.log(f"Could not cancel wake alarm timer: {error}", level="WARNING")
        self._wake_alarm_handle = None

    def _wake_alarm_triggered(self, kwargs: dict) -> None:
        self._wake_alarm_handle = None
        self._set_activity("awake", f"{self.wake_alarm_person}_alarm")

    def _force_wake(self, kwargs: dict) -> None:
        self._set_activity("awake", "force_wake_deadline")

    def terminate(self) -> None:
        """Mark discovered entities unavailable during an orderly app unload."""
        if hasattr(self, "availability_topic"):
            try:
                self._publish_availability("offline")
            except Exception as error:
                self.log(
                    f"Could not publish HomeMonitor offline: {error}",
                    level="WARNING",
                )
