"""Diagnostic sensor exposing how current the imported statistics are.

Usage itself is written straight into long-term statistics by the coordinator,
so it does not need a sensor entity. This entity exists so the freshness of
that import is visible — the portal typically runs about a day behind.
"""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import YonkersWaterWiseConfigEntry
from .const import DOMAIN
from .coordinator import YonkersWaterWiseCoordinator

LAST_READING = SensorEntityDescription(
    key="last_reading",
    translation_key="last_reading",
    device_class=SensorDeviceClass.TIMESTAMP,
    entity_category=EntityCategory.DIAGNOSTIC,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: YonkersWaterWiseConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one diagnostic sensor per meter."""
    async_add_entities(
        LastReadingSensor(coordinator)
        for coordinator in entry.runtime_data.coordinators
    )


class LastReadingSensor(CoordinatorEntity[YonkersWaterWiseCoordinator], SensorEntity):
    """Timestamp of the most recent hourly reading pulled from the portal."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: YonkersWaterWiseCoordinator) -> None:
        """Initialise the sensor for one meter."""
        super().__init__(coordinator)
        self.entity_description = LAST_READING
        self._attr_unique_id = f"{coordinator.meter_number}_last_reading"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.meter_number)},
            entry_type=DeviceEntryType.SERVICE,
            manufacturer="City of Yonkers",
            model="Smart water meter",
            name=f"Water meter {coordinator.meter_number}",
        )
        # Nothing is retained across restarts, so seed from the first refresh
        # and hold the value between updates that return no new readings.
        self._last_reading = (
            coordinator.data.last_reading_start if coordinator.data else None
        )

    @property
    def native_value(self):
        """Return the start of the newest imported hour."""
        if self.coordinator.data and self.coordinator.data.last_reading_start:
            self._last_reading = self.coordinator.data.last_reading_start
        return self._last_reading

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Expose details of the last import for troubleshooting."""
        data = self.coordinator.data
        return {
            "statistic_id": self.coordinator.statistic_id,
            "meter_number": self.coordinator.meter_number,
            "account_number": self.coordinator.account_number,
            "hours_imported_last_run": data.imported if data else 0,
            "adjusted_reads_last_run": data.adjusted if data else 0,
        }
