"""Native Evohome control through a real Home Assistant service registry."""

from dataclasses import replace

from homeassistant.const import EVENT_CALL_SERVICE, UnitOfTemperature
import pytest
from pytest_homeassistant.common import async_capture_events

from custom_components.better_thermostat.utils.controlling import control_trv

from .conftest import make_entry, set_room_sensor, setup_entry, wait_for_startup
from .device_profiles import RAMSES_ZONE


@pytest.mark.parametrize(
    "fake_trv",
    [RAMSES_ZONE, replace(RAMSES_ZONE, system_unit=UnitOfTemperature.FAHRENHEIT)],
    indirect=True,
)
async def test_evohome_control_uses_only_native_permanent_overrides(hass, fake_trv):
    """External regulation must survive the next Honeywell schedule switchpoint."""
    profile = fake_trv.profile
    events = async_capture_events(hass, EVENT_CALL_SERVICE)
    set_room_sensor(hass, 18.0, UnitOfTemperature.CELSIUS)
    entry = make_entry(profile)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    await control_trv(bt, profile.entity_id)
    native = [e.data for e in events if e.data["domain"] == "ramses_cc"]
    assert native
    assert all(e["service"] == "set_zone_mode" for e in native)
    assert all(e["service_data"]["mode"] == "permanent_override" for e in native)
    assert not fake_trv.set_hvac_mode_calls
    generic = [
        e
        for e in events
        if e.data["domain"] == "climate"
        and e.data["service_data"].get("entity_id") == profile.entity_id
    ]
    assert not generic
    before = len(fake_trv.set_temperature_calls)
    await control_trv(bt, profile.entity_id)
    assert len(fake_trv.set_temperature_calls) == before
