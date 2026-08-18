"""Fetch water usage and feed it into Home Assistant's long-term statistics."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from zoneinfo import ZoneInfo

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import VolumeConverter

from .api import (
    CannotConnect,
    IntervalReading,
    InvalidAuth,
    YonkersWaterWiseClient,
    floor_to_hour,
    normalize_readings,
)
from .const import (
    BACKFILL_CHUNK_DAYS,
    DOMAIN,
    OVERLAP_DAYS,
    UPDATE_INTERVAL,
    UTILITY_TIMEZONE,
)

_LOGGER = logging.getLogger(__name__)

# How far back to hunt for the cumulative total that precedes a refresh
# window. If nothing turns up within this span the history has a hole and we
# rebuild from scratch rather than guess.
_BASELINE_LOOKBACK = timedelta(days=30)


@dataclass(slots=True)
class UsageSnapshot:
    """What the coordinator hands to its entities after each refresh."""

    last_reading_start: datetime | None
    last_reading_value: float | None
    imported: int
    adjusted: int


class YonkersWaterWiseCoordinator(DataUpdateCoordinator[UsageSnapshot]):
    """Keep one meter's statistics in sync with the portal."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: YonkersWaterWiseClient,
        account_number: str,
        meter_number: str,
    ) -> None:
        """Initialise the coordinator for a single meter."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} {meter_number}",
            update_interval=UPDATE_INTERVAL,
            config_entry=entry,
        )
        self.client = client
        self.account_number = account_number
        self.meter_number = meter_number
        self.timezone = ZoneInfo(UTILITY_TIMEZONE)

        self.statistic_id = f"{DOMAIN}:water_{meter_number}"
        self._metadata = StatisticMetaData(
            # A water total has no meaningful average, only a running sum.
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"Water usage {meter_number}",
            source=DOMAIN,
            statistic_id=self.statistic_id,
            # Tells the recorder which converter to use if the user displays
            # these statistics in a different volume unit.
            unit_class=VolumeConverter.UNIT_CLASS,
            unit_of_measurement=UnitOfVolume.CENTUM_CUBIC_FEET,
        )

    async def _async_update_data(self) -> UsageSnapshot:
        """Pull any new readings and write them to the statistics table."""
        try:
            return await self._async_sync_statistics()
        except InvalidAuth as err:
            # Surfaces as a re-auth prompt in the UI.
            raise ConfigEntryAuthFailed(str(err)) from err
        except CannotConnect as err:
            raise UpdateFailed(str(err)) from err

    async def _async_sync_statistics(self) -> UsageSnapshot:
        """Work out the window to fetch, fetch it, and import the result."""
        window_start, baseline = await self._async_resolve_window()
        now = dt_util.now(self.timezone)

        if window_start >= now:
            _LOGGER.debug(
                "Statistics for meter %s are already current", self.meter_number
            )
            return UsageSnapshot(None, None, 0, 0)

        readings = await self._async_fetch_range(window_start, now)
        if not readings:
            _LOGGER.debug("Portal returned no readings for meter %s", self.meter_number)
            return UsageSnapshot(None, None, 0, 0)

        readings = normalize_readings(readings)

        statistics: list[StatisticData] = []
        running = baseline
        window_start_ts = window_start.timestamp()
        for reading in readings:
            # Anything before the window is already folded into `baseline`.
            if reading.start.timestamp() < window_start_ts:
                continue
            running += reading.value
            statistics.append(
                StatisticData(
                    start=reading.start,
                    state=reading.value,
                    sum=running,
                )
            )

        if not statistics:
            return UsageSnapshot(None, None, 0, 0)

        async_add_external_statistics(self.hass, self._metadata, statistics)

        adjusted = sum(1 for reading in readings if reading.adjusted)
        _LOGGER.debug(
            "Imported %d hourly statistics for meter %s (%d adjusted reads), "
            "cumulative total now %.2f CCF",
            len(statistics),
            self.meter_number,
            adjusted,
            running,
        )
        return UsageSnapshot(
            last_reading_start=readings[-1].start,
            last_reading_value=readings[-1].value,
            imported=len(statistics),
            adjusted=adjusted,
        )

    async def _async_resolve_window(self) -> tuple[datetime, float]:
        """Return where to resume from and the running total at that point.

        On the first run — or whenever the recorded history is too broken to
        resume from — this falls back to the earliest date the portal offers.
        """
        last_stats = await get_instance(self.hass).async_add_executor_job(
            partial(
                get_last_statistics,
                self.hass,
                1,
                self.statistic_id,
                True,
                {"start", "sum"},
            )
        )
        rows = last_stats.get(self.statistic_id) if last_stats else None

        if rows:
            last_start = dt_util.utc_from_timestamp(rows[0]["start"]).astimezone(
                self.timezone
            )
            # Step back a few days so corrections to recent hours get picked up.
            window_start = floor_to_hour(last_start - timedelta(days=OVERLAP_DAYS))
            baseline = await self._async_baseline_sum(window_start)
            if baseline is not None:
                return window_start, baseline
            _LOGGER.warning(
                "Could not establish a running total before %s for meter %s; "
                "rebuilding its statistics from the start",
                window_start,
                self.meter_number,
            )

        available_start, _ = await self.client.async_get_available_range(
            self.meter_number, self.account_number
        )
        return (
            datetime.combine(
                available_start, datetime.min.time(), tzinfo=self.timezone
            ),
            0.0,
        )

    async def _async_baseline_sum(self, before: datetime) -> float | None:
        """Cumulative total of the last statistic strictly before `before`.

        Returns 0.0 when `before` is at or before the very first recorded hour,
        and None when there is recorded history but none of it lands in the
        lookback window — meaning we cannot safely continue the running total.
        """
        stats = await get_instance(self.hass).async_add_executor_job(
            partial(
                statistics_during_period,
                self.hass,
                before - _BASELINE_LOOKBACK,
                before,
                {self.statistic_id},
                "hour",
                None,
                {"sum"},
            )
        )
        rows = stats.get(self.statistic_id) if stats else None
        if rows:
            return float(rows[-1]["sum"])

        # No rows in the lookback window. If nothing at all precedes `before`,
        # then `before` is the true start of history and zero is correct.
        earliest = await get_instance(self.hass).async_add_executor_job(
            partial(
                statistics_during_period,
                self.hass,
                dt_util.utc_from_timestamp(0),
                before,
                {self.statistic_id},
                "hour",
                None,
                {"sum"},
            )
        )
        if not (earliest.get(self.statistic_id) if earliest else None):
            return 0.0
        return None

    async def _async_fetch_range(
        self, start: datetime, end: datetime
    ) -> list[IntervalReading]:
        """Fetch hourly readings, chunking long backfills into yearly slices."""
        readings: list[IntervalReading] = []
        chunk_start = start
        while chunk_start < end:
            chunk_end = min(chunk_start + timedelta(days=BACKFILL_CHUNK_DAYS), end)
            chunk = await self.client.async_get_hourly_usage(
                self.account_number,
                self.meter_number,
                chunk_start,
                chunk_end,
            )
            if readings:
                # Both bounds are inclusive, so consecutive chunks repeat the
                # hour they meet on. Trim by instant rather than deduplicating
                # by timestamp: daylight-saving days legitimately carry two
                # readings labelled with the same wall-clock hour, and those
                # have to survive to normalize_readings.
                boundary = readings[-1].start.timestamp()
                chunk = [
                    reading for reading in chunk if reading.start.timestamp() > boundary
                ]
            readings.extend(chunk)
            chunk_start = chunk_end

        return readings
