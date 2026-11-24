"""Evohome receives complete calculated overrides without generic heat writes."""

from unittest.mock import patch

from homeassistant.core import Context, State
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
import pytest

from custom_components.better_thermostat.adapters import delegate, ramses_cc
from custom_components.better_thermostat.utils.retry import WriteWithheldError
from tests.factories import ThermostatStandIn, trv_record

ENTITY = "climate.zone"


@pytest.fixture
async def zone(hass):
    er.async_get(hass).async_get_or_create(
        "climate", "ramses_cc", "01:123456_00", suggested_object_id="zone"
    )
    hass.states.async_set(
        ENTITY,
        "heat",
        {
            "zone_index": "00",
            "config": {"min_temp": 7.0, "max_temp": 30.0},
            "mode": {"mode": "follow_schedule", "setpoint": 18.0},
            "temperature": 18.0,
        },
    )
    calls = []

    async def write(call):
        calls.append(call)

    hass.services.async_register("ramses_cc", "set_zone_mode", write)
    host = ThermostatStandIn()
    host.hass = hass
    host.device_name = "Room"
    host.context = Context()
    host.real_trvs = {ENTITY: trv_record(entity_id=ENTITY, adapter=ramses_cc)}
    return host, calls


@pytest.mark.parametrize(
    "mode,target,expected",
    [("heat", 18.0, 18.0), ("heat", 22.0, 22.0), ("off", None, 7.0)],
)
async def test_native_override_preserves_the_decision(zone, mode, target, expected):
    """The zone receives the calculated Celsius target and the command origin."""
    host, calls = zone
    host.hass.config.units = US_CUSTOMARY_SYSTEM
    await ramses_cc.apply_zone_control(host, ENTITY, mode, target)
    assert len(calls) == 1
    assert calls[0].data == {
        "entity_id": ENTITY,
        "mode": "permanent_override",
        "setpoint": expected,
    }
    assert calls[0].context is host.context


@pytest.mark.parametrize(
    "mode,target",
    [("heat", None), ("heat", float("nan")), ("heat", 31.0), ("cool", 18.0)],
)
async def test_incomplete_or_unsupported_heating_is_refused(zone, mode, target):
    """Heating activation requires a valid calculated target."""
    host, calls = zone
    with pytest.raises(HomeAssistantError):
        await ramses_cc.apply_zone_control(host, ENTITY, mode, target)
    assert calls == []


async def test_supported_zone_loads_native_adapter(zone):
    """Configured RAMSES zones use the native combined channel."""
    host, _ = zone
    assert await delegate.load_adapter(host, "ramses_cc", ENTITY) is ramses_cc
    assert not (await ramses_cc.get_info(host, ENTITY)).offset_write
    await ramses_cc.init(host, ENTITY)


@pytest.mark.parametrize(
    "attributes",
    [
        {},
        {"zone_index": "00"},
        {"zone_index": "00", "config": {"min_temp": 30, "max_temp": 7}},
    ],
)
async def test_unsupported_metadata_cannot_receive_zone_commands(zone, attributes):
    """Controllers and incompletely configured zones must fail closed."""
    host, calls = zone
    host.hass.states.async_set(ENTITY, "heat", attributes)
    with pytest.raises(HomeAssistantError):
        await ramses_cc.apply_zone_control(host, ENTITY, "heat", 18)
    assert not calls


async def test_missing_service_never_uses_generic_heat(zone):
    """A lost native action must not reactivate the generic 25 degree path."""
    host, calls = zone
    host.hass.services.async_remove("ramses_cc", "set_zone_mode")
    with pytest.raises(HomeAssistantError, match="unavailable"):
        await ramses_cc.apply_zone_control(host, ENTITY, "heat", 18)
    assert not calls


async def test_unavailable_zone_is_withheld(zone):
    """An offline zone is left for the scheduler's recovery cycle."""
    host, calls = zone
    host.hass.states.async_set(ENTITY, "unavailable")
    with pytest.raises(WriteWithheldError):
        await ramses_cc.apply_zone_control(host, ENTITY, "off", None)
    assert not calls


@pytest.mark.parametrize(
    "override,mode,value,confirmed",
    [
        ("follow_schedule", "heat", 18.0, False),
        ("permanent_override", "heat", 18.0, True),
        ("permanent_override", "off", 18.0, False),
        ("permanent_override", "heat", 19.0, False),
        ("permanent_override", "off", 7.0, True),
    ],
)
def test_readback_requires_the_complete_override(override, mode, value, confirmed):
    """Matching numbers on a native schedule cannot confirm permanent control."""
    state = State(
        ENTITY,
        mode,
        {"config": {"min_temp": 7}, "mode": {"mode": override, "setpoint": value}},
    )
    target = 7.0 if value == 7.0 else 18.0
    assert ramses_cc.zone_control_confirmed(state, target) is confirmed


async def test_broken_native_adapter_cannot_fall_back(zone):
    """An import failure must not silently enable generic heating commands."""
    host, _ = zone
    with (
        patch.object(delegate, "_import_adapter", side_effect=ImportError("broken")),
        pytest.raises(ImportError),
    ):
        await delegate.load_adapter(host, "ramses_cc", ENTITY)


@pytest.mark.parametrize("field", ["thermostat", "cooler"])
async def test_unsupported_zone_is_rejected_in_configuration(zone, field):
    """Configuration must explain unsupported native zone selections."""
    from custom_components.better_thermostat.config_flow import _unusable_climate_error

    host, _ = zone
    host.hass.states.async_set(ENTITY, "heat", {"supported_features": 1})
    assert _unusable_climate_error(host.hass, field, [ENTITY]) == (
        "ramses_zone_unsupported",
        {"entity": ENTITY},
    )


async def test_zone_cannot_be_selected_for_cooling(zone):
    """The native heating adapter must not offer cooling control."""
    from custom_components.better_thermostat.config_flow import _unusable_climate_error

    host, _ = zone
    error = _unusable_climate_error(host.hass, "cooler", [ENTITY])
    assert error is not None
    assert error[0] == "ramses_zone_unsupported"


@pytest.mark.parametrize(
    "attributes",
    [
        {},
        {"mode": {}},
        {"mode": {"mode": "permanent_override", "setpoint": None}},
        {"mode": {"mode": "permanent_override", "setpoint": True}},
        {"mode": {"mode": "permanent_override", "setpoint": 18}},
        {"mode": {"mode": "permanent_override", "setpoint": 18}, "config": {}},
    ],
)
def test_incomplete_readback_is_not_confirmation(attributes):
    """Missing RF metadata leaves the command awaiting confirmation."""
    assert not ramses_cc.zone_control_confirmed(State(ENTITY, "heat", attributes), 18)


async def test_rf_failure_remains_a_write_failure(zone):
    """A failed RF send must reach the scheduler's retry handling."""
    host, _ = zone

    async def fail(call):
        raise HomeAssistantError("RF disconnected")

    host.hass.services.async_register("ramses_cc", "set_zone_mode", fail)
    with pytest.raises(HomeAssistantError, match="RF disconnected"):
        await ramses_cc.apply_zone_control(host, ENTITY, "heat", 18)


async def test_auxiliary_writes_use_native_commands(zone):
    """Maintenance must not accidentally reintroduce generic climate writes."""
    host, calls = zone
    await ramses_cc.set_temperature(host, ENTITY, 19)
    await ramses_cc.set_hvac_mode(host, ENTITY, "off")
    with pytest.raises(HomeAssistantError):
        await ramses_cc.set_hvac_mode(host, ENTITY, "heat")
    assert [call.data["setpoint"] for call in calls] == [19, 7]
    assert await ramses_cc.set_calibration_offset(host, ENTITY, 1) is False


@pytest.mark.parametrize(
    "calibration", ["local_calibration_based", "direct_valve_based"]
)
async def test_stored_unsupported_calibration_cannot_control_a_zone(zone, calibration):
    """Old offset or valve configurations require correction before zone control."""
    from custom_components.better_thermostat.utils.const import CalibrationOutput

    host, calls = zone
    host.real_trvs[ENTITY].calibration = CalibrationOutput(calibration)
    with pytest.raises(HomeAssistantError):
        await ramses_cc.init(host, ENTITY)
    with pytest.raises(HomeAssistantError):
        await ramses_cc.apply_zone_control(host, ENTITY, "heat", 18)
    assert not calls


async def test_combined_dispatch_records_channel_failure(zone):
    """Native service failures must use the shared channel outage tracking."""
    host, _ = zone
    with (
        patch.object(
            ramses_cc, "apply_zone_control", side_effect=TypeError("invalid RF command")
        ),
        pytest.raises(TypeError),
    ):
        await delegate.apply_zone_control(host, ENTITY, "heat", 18)
    assert "temperature" in host.real_trvs[ENTITY].unreachable_write_channels


async def test_combined_dispatch_respects_device_release(zone):
    """Releasing a zone must stop writes waiting in the shared dispatcher."""
    host, calls = zone
    host.devices_released = True
    with pytest.raises(WriteWithheldError):
        await delegate.apply_zone_control(host, ENTITY, "heat", 18)
    assert not calls


async def test_superseded_decision_is_withheld_before_an_rf_attempt(zone):
    """A queued retry must not transmit an obsolete heating decision."""
    host, calls = zone
    with pytest.raises(WriteWithheldError):
        await delegate.apply_zone_control(
            host, ENTITY, "heat", 18, still_current=lambda: False
        )
    assert not calls


async def test_configured_zone_is_accepted_as_a_heater(zone):
    """A fully configured Evohome zone can be selected for heating."""
    from custom_components.better_thermostat.config_flow import _unusable_climate_error

    host, _ = zone
    state = host.hass.states.get(ENTITY)
    host.hass.states.async_set(
        ENTITY, "heat", dict(state.attributes) | {"supported_features": 1}
    )
    assert _unusable_climate_error(host.hass, "thermostat", [ENTITY]) is None


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"min_temp": "bad", "max_temp": 30},
        {"min_temp": float("nan"), "max_temp": 30},
    ],
)
async def test_unreadable_configuration_is_refused(zone, config):
    """Native control requires finite configuration bounds rather than defaults."""
    host, _ = zone
    host.hass.states.async_set(ENTITY, "heat", {"zone_index": "00", "config": config})
    with pytest.raises(HomeAssistantError):
        await ramses_cc.get_info(host, ENTITY)


async def test_other_integration_cannot_receive_native_zone_commands(zone):
    """Zone-like attributes must not bypass entity registry ownership."""
    host, calls = zone
    er.async_get(host.hass).async_remove(ENTITY)
    with pytest.raises(HomeAssistantError):
        await ramses_cc.apply_zone_control(host, ENTITY, "heat", 18)
    assert not calls
