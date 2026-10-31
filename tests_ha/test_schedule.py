"""Integration-owned weekly schedule and its Manual/Presence behavior."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from homeassistant.components.climate import (
    PRESET_AWAY,
    PRESET_BOOST,
    PRESET_ECO,
    PRESET_NONE,
    HVACMode,
)
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.util import dt as dt_util

from .helpers import PRESENCE_SENSOR, TRV, async_advance, async_set_state, async_setup_proxy

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def _options(freezer, first: str, next_preset: str | None = None) -> dict:
    freezer.move_to(datetime(2026, 4, 6, 6, tzinfo=UTC))
    now = dt_util.now()
    blocks = [{"start": "00:00", "preset": first}]
    if next_preset is not None:
        blocks.append({"start": (now + timedelta(minutes=1)).strftime("%H:%M"), "preset": next_preset})
    return {"weekly_preset_schedule": {WEEKDAYS[now.weekday()]: blocks}}


async def test_schedule_applies_active_and_next_block(hass, freezer):
    proxy = await async_setup_proxy(hass, options=_options(freezer, PRESET_ECO, "comfort"))
    assert proxy.preset == PRESET_ECO
    assert proxy.state.attributes["scheduled_action"] == PRESET_ECO
    await async_advance(hass, freezer, 61)
    assert proxy.preset == "comfort"
    assert proxy.state.attributes["scheduled_action"] == "comfort"


async def test_manual_until_next_block_survives_reload(hass, freezer):
    options = {
        **_options(freezer, PRESET_ECO, "comfort"),
        "manual_mode_action": "schedule",
    }
    proxy = await async_setup_proxy(hass, options=options)
    await proxy.call("climate", "set_temperature", proxy.climate, temperature=21.5)
    deadline = proxy.state.attributes["manual_return_at_ts"]
    assert proxy.preset == PRESET_NONE
    await async_advance(hass, freezer, 20)
    assert await hass.config_entries.async_reload(proxy.entry.entry_id)
    await hass.async_block_till_done()
    assert proxy.preset == PRESET_NONE
    assert proxy.state.attributes["manual_return_at_ts"] == deadline
    await async_advance(hass, freezer, 41)
    assert proxy.preset == "comfort"
    assert "manual_return_at_ts" not in proxy.state.attributes


async def test_timed_manual_returns_to_current_schedule_block(hass, freezer):
    options = {
        **_options(freezer, PRESET_ECO, "comfort"),
        "manual_mode_action": "timer",
        "manual_mode_duration_min": 2,
    }
    proxy = await async_setup_proxy(hass, options=options)
    await proxy.call("climate", "set_temperature", proxy.climate, temperature=21.5)
    await async_advance(hass, freezer, 61)
    assert proxy.preset == PRESET_NONE
    await async_advance(hass, freezer, 60)
    assert proxy.preset == "comfort"


async def test_schedule_off_yields_to_presence_away_then_returns(hass, freezer):
    options = {
        **_options(freezer, "off"),
        "presence_away_delay_s": 1,
        "presence_home_delay_s": 1,
    }
    proxy = await async_setup_proxy(hass, options=options)
    assert proxy.state.state == HVACMode.OFF
    await async_set_state(hass, PRESENCE_SENSOR, STATE_OFF)
    await async_advance(hass, freezer, 2)
    assert proxy.preset == PRESET_AWAY
    assert proxy.state.state == HVACMode.HEAT
    await async_set_state(hass, PRESENCE_SENSOR, STATE_ON)
    await async_advance(hass, freezer, 2)
    assert proxy.state.state == HVACMode.OFF


async def test_manual_off_survives_reports_options_and_reload_until_next_block(hass, freezer):
    proxy = await async_setup_proxy(hass, options=_options(freezer, PRESET_ECO, "comfort"))
    await proxy.call("climate", "set_hvac_mode", proxy.climate, hvac_mode=HVACMode.OFF)
    await async_set_state(hass, PRESENCE_SENSOR, STATE_ON, {"fresh": True})
    await proxy.call("number", "set_value", proxy.number("comfort_target"), value=22)
    assert proxy.state.state == HVACMode.OFF

    assert await hass.config_entries.async_reload(proxy.entry.entry_id)
    await hass.async_block_till_done()
    assert proxy.state.state == HVACMode.OFF

    await async_advance(hass, freezer, 61)
    assert proxy.state.state == HVACMode.HEAT
    assert proxy.preset == "comfort"


async def test_manual_preset_survives_protection_and_reload_until_next_block(hass, freezer):
    options = {
        **_options(freezer, "comfort", PRESET_ECO),
        "presence_away_delay_s": 1,
        "presence_home_delay_s": 1,
    }
    proxy = await async_setup_proxy(hass, options=options)
    await proxy.call("climate", "set_preset_mode", proxy.climate, preset_mode="frost_protection")
    await async_set_state(hass, PRESENCE_SENSOR, STATE_OFF)
    await async_advance(hass, freezer, 2)
    assert proxy.preset == PRESET_AWAY
    await async_set_state(hass, PRESENCE_SENSOR, STATE_ON)
    await async_advance(hass, freezer, 2)
    assert proxy.preset == "frost_protection"

    assert await hass.config_entries.async_reload(proxy.entry.entry_id)
    await hass.async_block_till_done()
    assert proxy.preset == "frost_protection"
    await async_advance(hass, freezer, 57)
    assert proxy.preset == PRESET_ECO


async def test_schedule_waits_for_source_recovery_before_sending_off(hass, freezer):
    proxy = await async_setup_proxy(hass, options=_options(freezer, PRESET_ECO, "off"))
    await async_set_state(hass, TRV, STATE_UNAVAILABLE)
    count = len(proxy.trv_commands("set_hvac_mode"))
    await async_advance(hass, freezer, 61)
    assert proxy.state.state == STATE_UNAVAILABLE
    assert len(proxy.trv_commands("set_hvac_mode")) == count

    await async_set_state(
        hass, TRV, HVACMode.HEAT, {"temperature": 20.0, "current_temperature": 22.0}
    )
    assert proxy.state.state == HVACMode.OFF
    assert proxy.trv_commands("set_hvac_mode")[-1]["hvac_mode"] == HVACMode.OFF


async def test_boost_overrides_off_block_and_returns_to_it(hass, freezer):
    options = {**_options(freezer, "off"), "boost_duration": 1}
    proxy = await async_setup_proxy(hass, options=options, trv_adopts=True)
    await proxy.call("climate", "set_preset_mode", proxy.climate, preset_mode=PRESET_BOOST)
    assert proxy.state.state == HVACMode.HEAT
    assert proxy.preset == PRESET_BOOST
    await async_advance(hass, freezer, 61)
    assert proxy.state.state == HVACMode.OFF


async def test_manual_expiry_at_off_boundary_sends_no_intermediate_heat_target(hass, freezer):
    options = {
        **_options(freezer, "comfort", "off"),
        "manual_mode_action": "timer",
        "manual_mode_duration_min": 1,
    }
    proxy = await async_setup_proxy(hass, options=options, trv_adopts=True)
    await proxy.call("climate", "set_temperature", proxy.climate, temperature=21.5)
    count = len(proxy.trv_commands("set_temperature"))
    await async_advance(hass, freezer, 61)
    assert proxy.state.state == HVACMode.OFF
    assert len(proxy.trv_commands("set_temperature")) == count


async def test_missed_block_ends_manual_off_after_downtime(hass, freezer):
    proxy = await async_setup_proxy(hass, options=_options(freezer, PRESET_ECO, "comfort"))
    await proxy.call("climate", "set_hvac_mode", proxy.climate, hvac_mode=HVACMode.OFF)
    assert await hass.config_entries.async_unload(proxy.entry.entry_id)
    await async_advance(hass, freezer, 61)
    assert await hass.config_entries.async_setup(proxy.entry.entry_id)
    await hass.async_block_till_done()
    assert proxy.state.state == HVACMode.HEAT
    assert proxy.preset == "comfort"
