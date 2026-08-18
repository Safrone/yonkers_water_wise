"""Sensors for the Yonkers WaterWise integration.

The hourly series itself goes straight into long-term statistics, which is what
feeds the Energy dashboard. These entities exist so the same figures are visible
on the device page and available to automations and templates.

Every value here trails real time by roughly a day, because that is how far
behind the utility publishes. There is deliberately no "current usage" or "usage
today" sensor: both would read zero for most of the day and invite the reader to
believe no water was used.
"""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import YonkersWaterWiseConfigEntry
from .const import DOMAIN
from .coordinator import YonkersWaterWiseCoordinator

SENSORS: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(
        key="total_usage",
        translation_key="total_usage",
        device_class=SensorDeviceClass.WATER,
        # The portal restates recent hours as late reads arrive, so the total
        # can be revised. TOTAL rather than TOTAL_INCREASING keeps a downward
        # correction from being read as a meter reset.
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfVolume.CENTUM_CUBIC_FEET,
    ),
    SensorEntityDescription(
        key="last_interval_usage",
        translation_key="last_interval_usage",
        # No device class: SensorDeviceClass.WATER only accepts total and
        # total_increasing, and a single hour's bucket is neither.
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfVolume.CENTUM_CUBIC_FEET,
        icon="mdi:water",
        suggested_display_precision=3,
    ),
    SensorEntityDescription(
        key="last_reading",
        translation_key="last_reading",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: YonkersWaterWiseConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the sensors for every meter on this account."""
    async_add_entities(
        YonkersWaterWiseSensor(coordinator, description)
        for coordinator in entry.runtime_data.coordinators
        for description in SENSORS
    )


class YonkersWaterWiseSensor(
    CoordinatorEntity[YonkersWaterWiseCoordinator], SensorEntity
):
    """A single reading derived from the latest import."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: YonkersWaterWiseCoordinator,
        description: SensorEntityDescription,
    ) -> None:
        """Initialise one sensor for one meter."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.meter_number}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.meter_number)},
            entry_type=DeviceEntryType.SERVICE,
            manufacturer="City of Yonkers",
            model="Smart water meter",
            name=f"Water meter {coordinator.meter_number}",
        )

    @property
    def native_value(self) -> float | object | None:
        """Return this sensor's value from the latest snapshot."""
        if (data := self.coordinator.data) is None:
            return None
        match self.entity_description.key:
            case "total_usage":
                return data.total
            case "last_interval_usage":
                return data.last_reading_value
            case "last_reading":
                return data.last_reading_start
        return None

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        """Expose import details, on the diagnostic sensor only."""
        if self.entity_description.key != "last_reading":
            return None
        data = self.coordinator.data
        return {
            "statistic_id": self.coordinator.statistic_id,
            "meter_number": self.coordinator.meter_number,
            "account_number": self.coordinator.account_number,
            "hours_imported_last_run": data.imported if data else 0,
            "adjusted_reads_last_run": data.adjusted if data else 0,
        }
