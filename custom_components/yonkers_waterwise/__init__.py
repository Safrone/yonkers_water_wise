"""The Yonkers WaterWise integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import CannotConnect, InvalidAuth, YonkersWaterWiseClient
from .const import CONF_ACCOUNT_NUMBER
from .coordinator import YonkersWaterWiseCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]


@dataclass
class RuntimeData:
    """Objects shared between the entry and its platforms."""

    client: YonkersWaterWiseClient
    account_number: str
    coordinators: list[YonkersWaterWiseCoordinator] = field(default_factory=list)


type YonkersWaterWiseConfigEntry = ConfigEntry[RuntimeData]


async def async_setup_entry(
    hass: HomeAssistant, entry: YonkersWaterWiseConfigEntry
) -> bool:
    """Set up a WaterWise account from a config entry."""
    # A private session keeps the portal's session cookie out of the shared
    # Home Assistant cookie jar.
    session = async_create_clientsession(hass)
    client = YonkersWaterWiseClient(
        session, entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD]
    )
    account_number = entry.data[CONF_ACCOUNT_NUMBER]

    try:
        await client.async_login()
        meters = await client.async_get_water_meters(account_number)
    except InvalidAuth as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except CannotConnect as err:
        raise ConfigEntryNotReady(str(err)) from err

    if not meters:
        # Not an error worth retrying: the account simply has no smart meter.
        _LOGGER.error(
            "No smart water meters are attached to account %s", account_number
        )
        return False

    runtime = RuntimeData(client=client, account_number=account_number)
    for meter_number in meters:
        coordinator = YonkersWaterWiseCoordinator(
            hass, entry, client, account_number, meter_number
        )
        await coordinator.async_config_entry_first_refresh()
        runtime.coordinators.append(coordinator)

    entry.runtime_data = runtime
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: YonkersWaterWiseConfigEntry
) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
