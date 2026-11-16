"""Lights: dimmable (Light Lightness) and tunable white (Light CTL)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_HS_COLOR,
    ATTR_TRANSITION,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AoiConfigEntry
from .const import ROLE_LIGHT
from .coordinator import MeshCoordinator, NodeRecord
from .entity import AoiNodeEntity
from .mesh.node import ROLE_LIGHTNESS
from .mesh.quirks import SENGLED_RGB_CID_PID

LIGHTNESS_MAX = 0xFFFF


def brightness_to_lightness(brightness: int) -> int:
    # 0 maps to Lightness 0 (off); HA turns brightness 0 into turn_off before it reaches us.
    return max(0, min(255, brightness)) * 257  # 255 -> 65535


def lightness_to_brightness(lightness: int) -> int:
    return max(1, round(lightness * 255 / LIGHTNESS_MAX))


def transition_ms(kwargs: dict[str, Any]) -> int | None:
    if (seconds := kwargs.get(ATTR_TRANSITION)) is None:
        return None
    return int(float(seconds) * 1000)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AoiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    for record in coordinator.nodes.values():
        if record.role == ROLE_LIGHT:
            async_add_entities(
                [AoiLight(coordinator, record)], config_subentry_id=record.subentry_id
            )


class AoiLight(AoiNodeEntity, LightEntity):
    _attr_name = None
    _attr_supported_features = LightEntityFeature.TRANSITION

    def __init__(self, coordinator: MeshCoordinator, record: NodeRecord) -> None:
        super().__init__(coordinator, record, "light")
        node = record.node
        modes = set()
        if node.is_color:
            modes.add(ColorMode.HS)
        if node.is_tunable:
            modes.add(ColorMode.COLOR_TEMP)
            self._attr_min_color_temp_kelvin = node.kelvin_min
            self._attr_max_color_temp_kelvin = node.kelvin_max
        if not modes:
            modes.add(ColorMode.BRIGHTNESS if ROLE_LIGHTNESS in node.models else ColorMode.ONOFF)
        self._attr_supported_color_modes = modes
        self._color_mode = ColorMode.HS if node.is_color else next(iter(modes))

    @property
    def _sengled_rgb(self) -> bool:
        """The two tested Sengled CID/PID lights mix white into HSL above 50%."""
        node = self.record.node
        return node.is_color and (node.cid, node.pid) == SENGLED_RGB_CID_PID

    @property
    def color_mode(self) -> ColorMode:
        return self._color_mode

    @property
    def is_on(self) -> bool | None:
        return self.record.state.on

    @property
    def brightness(self) -> int | None:
        if (
            ColorMode.ONOFF in self._attr_supported_color_modes
            or self.record.state.lightness is None
        ):
            return None
        if self._sengled_rgb and self._color_mode == ColorMode.HS:
            return max(1, min(255, round(self.record.state.lightness * 255 / 32767)))
        return lightness_to_brightness(self.record.state.lightness)

    @property
    def color_temp_kelvin(self) -> int | None:
        if ColorMode.COLOR_TEMP not in self._attr_supported_color_modes:
            return None
        return self.record.state.kelvin

    @property
    def hs_color(self) -> tuple[float, float] | None:
        if not self.record.node.is_color:
            return None
        hue, saturation = self.record.state.hue, self.record.state.saturation
        if hue is None or saturation is None:
            return None
        return hue * 360 / 65535, saturation * 100 / 65535

    async def async_turn_on(self, **kwargs: Any) -> None:
        brightness = kwargs.get(ATTR_BRIGHTNESS)
        hs_color = kwargs.get(ATTR_HS_COLOR)
        if ATTR_COLOR_TEMP_KELVIN in kwargs:
            self._color_mode = ColorMode.COLOR_TEMP
        elif hs_color is not None:
            self._color_mode = ColorMode.HS
        # Sengled CID 0x08B4/PID 0x0008 maps RGB brightness into the
        # vivid HSL half-range. CTL/white and other devices are unchanged.
        sengled_hs = self._sengled_rgb and self._color_mode == ColorMode.HS
        if brightness is not None:
            mesh_lightness = (
                round(max(0, min(255, brightness)) * 32767 / 255)
                if sengled_hs
                else brightness_to_lightness(brightness)
            )
        elif hs_color is not None and sengled_hs:
            mesh_lightness = min(self.record.state.lightness or 32767, 32767)
        else:
            mesh_lightness = None
        await self.coordinator.async_turn_on(
            self.record.subentry_id,
            lightness=mesh_lightness,
            kelvin=kwargs.get(ATTR_COLOR_TEMP_KELVIN),
            hue=None if hs_color is None else round(hs_color[0] * 65535 / 360) % 65536,
            saturation=None if hs_color is None else round(hs_color[1] * 65535 / 100),
            transition_ms=transition_ms(kwargs),
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_turn_off(
            self.record.subentry_id, transition_ms=transition_ms(kwargs)
        )
