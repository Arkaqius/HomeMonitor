from datetime import datetime, timezone, time
import appdaemon.plugins.hass.hassapi as hass


class HomeMonitor(hass.Hass):

    def initialize(self) -> None:
        self.awake_state = self.args["awake_state"]
        self.ux_awake_state = self.args["ux_awake_state"]
        self.next_alarm_sensor = self.args["next_alarm_sensor"]
        self.reset_time = self.args["reset_time"]
        self.wake_time_start = int(self.args["wake_time_start"])
        self.wake_time_end = int(self.args["wake_time_end"])

        self._alarm_handle = None  # timer handle for run_in

        self.listen_state(self.ux_awake_state_changed, self.ux_awake_state)
        self.listen_state(self.alarm_time_set, self.next_alarm_sensor)

        current_alarm_value = self.get_state(self.next_alarm_sensor)
        if current_alarm_value not in (
            None,
            "",
            "unknown",
            "unavailable",
            "Unavailable",
        ):
            self.alarm_time_set(
                self.next_alarm_sensor, "state", "", current_alarm_value, {}
            )
        else:
            self.log(
                f"next_alarm_sensor has no usable state yet: {current_alarm_value!r}",
                level="DEBUG",
            )

        self.run_daily(self.reset_awake, self.reset_time)

    def ux_awake_state_changed(
        self, entity: str, attribute: str, old: str, new: str, kwargs: dict
    ) -> None:
        """
        Handle the event when the ux_awake_state changes.
        """
        if new == "on":
            self.log("User is awake.")
            self.set_state("binary_sensor.monitor_awake_state", state="awake")
        elif new == "off":
            self.log("User is asleep.")
            self.set_state("binary_sensor.monitor_awake_state", state="sleep")

    def _parse_iso_datetime(self, value: str) -> datetime | None:
        if value is None:
            return None
        v = str(value).strip()
        if not v or v.lower() in ("unknown", "unavailable", "none"):
            return None

        # HA sometimes uses Z for UTC
        if v.endswith("Z"):
            v = v[:-1] + "+00:00"

        # Prefer fromisoformat (handles offsets like +01:00 and microseconds)
        try:
            dt = datetime.fromisoformat(v)
            # Ensure tz-aware (some integrations may omit tz)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            # Fallback formats if needed
            for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z"):
                try:
                    return datetime.strptime(v, fmt)
                except ValueError:
                    pass
        return None

    def alarm_time_set(
        self, entity: str, attribute: str, old: str, new: str, kwargs: dict
    ) -> None:
        # Only react to state updates (defensive)
        if attribute != "state":
            return

        next_alarm_time = self._parse_iso_datetime(new)
        if next_alarm_time is None:
            self.log(f"Ignoring invalid/empty next alarm value: {new!r}", level="DEBUG")
            return

        self.log(f"next_alarm_time: {next_alarm_time.isoformat()}", level="DEBUG")

        current_time = datetime.now(timezone.utc).astimezone(next_alarm_time.tzinfo)
        self.log(f"current_time: {current_time.isoformat()}", level="DEBUG")

        waking_hours_start = time(self.wake_time_start, 0)
        waking_hours_end = time(self.wake_time_end, 0)

        # Cancel previously scheduled callback to prevent duplicates
        if self._alarm_handle is not None:
            try:
                self.cancel_timer(self._alarm_handle)
            except Exception:
                pass
            self._alarm_handle = None

        if waking_hours_start <= next_alarm_time.time() <= waking_hours_end:
            seconds_to_alarm = int((next_alarm_time - current_time).total_seconds())

            # If it’s already in the past (or “now”), run soon rather than scheduling negative
            if seconds_to_alarm <= 0:
                seconds_to_alarm = 0

            self._alarm_handle = self.run_in(self.alarm_triggered, seconds_to_alarm)

            # HA state should be a string, not a datetime object
            self.set_state("sensor.next_awake_time", state=next_alarm_time.isoformat())

            self.log(
                f"Scheduled alarm_triggered in {seconds_to_alarm} seconds", level="INFO"
            )
        else:
            self.log("Alarm is outside of waking hours, ignoring.", level="INFO")

    def alarm_triggered(self, _: Any) -> None:
        """
        Callback for when the alarm time is hit.
        """
        self.log("Alarm was hit. Setting user state to asleep.")
        self.turn_on(self.ux_awake_state)
        self.set_state(self.awake_state, state="awake")

    def reset_awake(self, _: Any) -> None:
        """
        Callback for reset awake state
        """
        self.log("Reset awake by deadline.")
        self.turn_on(self.ux_awake_state)
        self.set_state(self.awake_state, state="awake")
