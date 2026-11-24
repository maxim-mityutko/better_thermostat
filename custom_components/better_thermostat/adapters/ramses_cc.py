"""Native permanent overrides for RAMSES RF Evohome heating zones."""

from __future__ import annotations

from collections.abc import Mapping
import logging
import math

from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from ..utils.const import DOMAIN, CalibrationOutput
from ..utils.helpers import state_is_live
from ..utils.retry import WriteWithheldError
from .base import AdapterCapabilities, DeviceChannels
from .generic import (
    get_calibration_offset,
    get_calibration_offset_step,
    get_max_calibration_offset,
    get_min_calibration_offset,
    set_valve,
)
from .types import AdapterHost, AdapterProbeHost

__all__ = [
    "CAPABILITIES",
    "get_calibration_offset",
    "get_calibration_offset_step",
    "get_max_calibration_offset",
    "get_min_calibration_offset",
    "set_valve",
]

_LOGGER = logging.getLogger(__name__)
CAPABILITIES = AdapterCapabilities(offset_write=False, valve_write=False)


def _zone_error(reason: str) -> HomeAssistantError:
    """Explain why a native zone decision could not be applied."""
    return HomeAssistantError(
        reason,
        translation_domain=DOMAIN,
        translation_key="ramses_zone_error",
        translation_placeholders={"reason": reason},
    )


def zone_bounds(host: AdapterProbeHost, entity_id: str) -> tuple[float, float]:
    """Validate ownership and zone metadata, returning native Celsius bounds."""
    return validate_zone(host.hass, entity_id)


def validate_zone(hass: HomeAssistant, entity_id: str) -> tuple[float, float]:
    """Require a native heating zone with a usable action and Celsius bounds."""
    entry = er.async_get(hass).async_get(entity_id)
    state = hass.states.get(entity_id)
    if (
        entry is None
        or entry.platform != "ramses_cc"
        or state is None
        or state.domain != "climate"
        or state.attributes.get("zone_index") is None
    ):
        raise _zone_error(f"{entity_id} is not a supported RAMSES heating zone")
    config = state.attributes.get("config")
    if not isinstance(config, Mapping):
        raise _zone_error(f"{entity_id} has no RAMSES zone configuration")
    try:
        low = float(config["min_temp"])
        high = float(config["max_temp"])
    except (KeyError, TypeError, ValueError) as err:
        raise _zone_error(f"{entity_id} has no usable zone bounds") from err
    if not math.isfinite(low) or not math.isfinite(high) or low >= high:
        raise _zone_error(f"{entity_id} has invalid zone bounds")
    if not hass.services.has_service("ramses_cc", "set_zone_mode"):
        raise _zone_error("RAMSES RF set_zone_mode action is unavailable")
    return low, high


async def get_info(self: AdapterProbeHost, entity_id: str) -> DeviceChannels:
    """Require a configured heating zone before offering native control."""
    zone_bounds(self, entity_id)
    return DeviceChannels(offset_write=False, valve_write=False)


async def init(self: AdapterHost, entity_id: str) -> None:
    """Validate the zone when integrations have initialized."""
    zone_bounds(self, entity_id)
    _validate_calibration(self, entity_id)


def _validate_calibration(self: AdapterHost, entity_id: str) -> None:
    """Reject stored offset or valve configurations before issuing a command."""
    if self.real_trvs[entity_id].calibration not in (
        None,
        CalibrationOutput.TARGET_TEMP_BASED,
    ):
        raise _zone_error(
            "RAMSES zones require target temperature based or no calibration"
        )


def zone_control_confirmed(state: State, temperature: float) -> bool:
    """Require native override readback as well as its Celsius setpoint."""
    mode = state.attributes.get("mode")
    if not isinstance(mode, Mapping) or mode.get("mode") != "permanent_override":
        return False
    value = mode.get("setpoint")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    config = state.attributes.get("config")
    if not isinstance(config, Mapping):
        return False
    minimum = config.get("min_temp")
    if not isinstance(minimum, (int, float)):
        return False
    expected = "off" if temperature <= minimum else "heat"
    return state.state == expected and abs(value - temperature) < 0.01


async def apply_zone_control(
    self: AdapterHost, entity_id: str, hvac_mode: str, temperature: float | None
) -> None:
    """Write one complete zone decision in Celsius with the caller's context."""
    state = self.hass.states.get(entity_id)
    if not state_is_live(state):
        raise WriteWithheldError(f"{entity_id} is unavailable")
    low, high = zone_bounds(self, entity_id)
    _validate_calibration(self, entity_id)
    if hvac_mode == "off":
        temperature = low
    elif hvac_mode != "heat":
        raise _zone_error(f"Unsupported RAMSES HVAC mode: {hvac_mode}")
    if temperature is None or not math.isfinite(temperature):
        raise _zone_error("RAMSES heating requires a finite calculated setpoint")
    if not low <= temperature <= high:
        raise _zone_error("RAMSES setpoint is outside the zone configuration")
    _LOGGER.debug(
        "entity=%s desired_hvac=%s desired_setpoint=%s override=permanent_override "
        "reported_hvac=%s reported_override=%s action=set_zone_mode",
        entity_id,
        hvac_mode,
        temperature,
        state.state,
        state.attributes.get("mode"),
    )
    await self.hass.services.async_call(
        "ramses_cc",
        "set_zone_mode",
        {"entity_id": entity_id, "mode": "permanent_override", "setpoint": temperature},
        blocking=True,
        context=self.context,
    )


async def set_temperature(
    self: AdapterHost, entity_id: str, temperature: float
) -> None:
    """Keep auxiliary setpoint writes on the native permanent channel."""
    await apply_zone_control(self, entity_id, "heat", temperature)


async def set_hvac_mode(self: AdapterHost, entity_id: str, hvac_mode: str) -> None:
    """Refuse incomplete heating decisions; OFF needs no calculated target."""
    await apply_zone_control(self, entity_id, hvac_mode, None)


async def set_calibration_offset(
    self: AdapterHost, entity_id: str, calibration_offset: float
) -> bool:
    """Evohome zones expose no writable calibration offset."""
    return False
