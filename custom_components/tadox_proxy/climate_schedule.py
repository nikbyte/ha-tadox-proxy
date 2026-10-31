"""Apply the integration-owned weekly schedule below Manual and safety layers."""
from __future__ import annotations

import datetime as dt
import time

from homeassistant.components.climate import PRESET_BOOST, PRESET_NONE, HVACMode
from homeassistant.core import callback
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.util import dt as dt_util

from .const import CONF_WEEKLY_PRESET_SCHEDULE
from .schedule import next_weekly_event_ts, weekly_block_at, weekly_preset_at


class ScheduleMixin:
    """Keep the active weekly block current across edits, restarts and week rollovers."""

    def _schedule(self) -> dict:
        return self._config_entry.options.get(CONF_WEEKLY_PRESET_SCHEDULE, {})

    def _scheduled_action(self) -> str | None:
        schedule = self._schedule()
        return weekly_preset_at(schedule, dt_util.now()) if schedule else None

    def _next_schedule_boundary(self) -> float | None:
        schedule = self._schedule()
        return next_weekly_event_ts(schedule, dt_util.now()) if schedule else None

    def _cancel_weekly_schedule_timer(self) -> None:
        if self._schedule_unsub is not None:
            self._schedule_unsub()
            self._schedule_unsub = None

    def _arm_weekly_schedule_timer(self) -> None:
        self._cancel_weekly_schedule_timer()
        boundary = self._next_schedule_boundary()
        if boundary is not None:
            when = dt.datetime.fromtimestamp(boundary, dt.UTC)
            self._schedule_unsub = async_track_point_in_time(
                self.hass, self._async_weekly_schedule_tick, when
            )

    @callback
    def _async_weekly_schedule_tick(self, _now) -> None:
        self._schedule_unsub = None
        self._arm_weekly_schedule_timer()
        self.hass.async_create_task(self._async_apply_schedule_state())

    async def _async_start_weekly_schedule(self) -> None:
        if not self._schedule():
            self._cancel_weekly_schedule_timer()
            self._schedule_action_state = None
            self._schedule_block_at_ts = None
            effects = self._layers.schedule_changed(None, None, time.time(), boundary=False)
            self.async_write_ha_state()
            await self._async_handle_layer_effects(effects)
            return
        self._arm_weekly_schedule_timer()
        await self._async_apply_schedule_state()

    async def _async_apply_schedule_state(self) -> None:
        """Update the schedule base and let window/presence layers take priority."""
        block = weekly_block_at(self._schedule(), dt_util.now())
        block_at_ts, action = block if block else (None, None)
        boundary = (
            self._schedule_block_at_ts is not None
            and block_at_ts != self._schedule_block_at_ts
        )
        if boundary:
            self._schedule_manual_off = False
        self._schedule_block_at_ts = block_at_ts
        self._schedule_action_state = action
        effects = self._layers.schedule_changed(
            action, self._next_schedule_boundary(), time.time(), boundary=boundary
        )
        await self._async_handle_layer_effects(effects)
        self.async_write_ha_state()

    async def _async_sync_schedule_hvac(self) -> bool:
        """Apply the current schedule's mode before sending any setpoint.

        Return True when the HVAC transition handled the regulation cycle.
        A user's Off or named preset lasts until the next actual block,
        including across reloads and unchanged sensor reports.
        """
        action = self._schedule_action_state
        if (
            action is None
            or self._schedule_manual_off
            or self._layers.user_choice.preset == PRESET_NONE
            or self._layers.schedule_preset_override
            or not self._source_available
        ):
            return False

        held = (
            self._layers.window_holds
            or self._layers.nobody_home_holds
            or self._layers.active_preset == PRESET_BOOST
        )
        mode = HVACMode.OFF if action == "off" and not held else HVACMode.HEAT
        if mode == self._hvac_mode:
            return False
        await self.async_set_hvac_mode(mode, _from_schedule=True)
        return True

    def _schedule_attributes(self) -> dict:
        """Persist which block ran and whether the user switched it off."""
        if not self._schedule():
            return {}
        return {
            "scheduled_action": self._schedule_action_state,
            "schedule_block_at_ts": self._schedule_block_at_ts,
            "schedule_manual_off": self._schedule_manual_off,
        }
