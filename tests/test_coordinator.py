"""Tests for the statistics import.

These cover the logic that the unit tests cannot reach: resolving the window to
fetch, carrying the cumulative total across refreshes, and writing rows the
recorder accepts. This is the part that only runs against a real database.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from functools import partial
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
from homeassistant.components.recorder import Recorder, get_instance
from homeassistant.components.recorder.statistics import (
    get_metadata,
    statistics_during_period,
)
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.yonkers_waterwise.api import IntervalReading
from custom_components.yonkers_waterwise.const import (
    CONF_ACCOUNT_NUMBER,
    DOMAIN,
    OVERLAP_DAYS,
)
from custom_components.yonkers_waterwise.coordinator import (
    YonkersWaterWiseCoordinator,
)

TZ = ZoneInfo("America/New_York")
ACCOUNT = "1234567"
METER = "7654321"
STATISTIC_ID = f"{DOMAIN}:water_{METER}"

# Well before "now", so every reading is safely in the past, and long enough
# that the overlap window lands mid-series rather than before the first row.
SERIES_START = datetime.now(TZ).replace(minute=0, second=0, microsecond=0) - timedelta(
    days=20
)


def _readings(count: int, value: float = 0.01, *, start=None) -> list[IntervalReading]:
    """Build `count` consecutive hourly readings."""
    origin = start or SERIES_START
    return [
        IntervalReading(origin + timedelta(hours=i), value, False) for i in range(count)
    ]


def _make_client(readings: list[IntervalReading]) -> MagicMock:
    """A stand-in portal client that serves a fixed series."""
    client = MagicMock()
    client.async_get_available_range = AsyncMock(
        return_value=(SERIES_START.date(), date.today())
    )

    async def _usage(account, meter, start, end):
        # Mimic the portal: return only what falls inside the window.
        lo, hi = start.timestamp(), end.timestamp()
        return [r for r in readings if lo <= r.start.timestamp() <= hi]

    client.async_get_hourly_usage = AsyncMock(side_effect=_usage)
    return client


def _coordinator(hass, client) -> YonkersWaterWiseCoordinator:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ACCOUNT_NUMBER: ACCOUNT, "username": "u", "password": "p"},
        unique_id=ACCOUNT,
    )
    entry.add_to_hass(hass)
    return YonkersWaterWiseCoordinator(hass, entry, client, ACCOUNT, METER)


async def _read_back(hass) -> list[dict]:
    """Return every statistic recorded for our meter, oldest first."""
    await async_wait_recording_done(hass)
    stats = await get_instance(hass).async_add_executor_job(
        partial(
            statistics_during_period,
            hass,
            SERIES_START - timedelta(days=30),
            datetime.now(TZ) + timedelta(days=1),
            {STATISTIC_ID},
            "hour",
            None,
            {"sum", "state"},
        )
    )
    return stats.get(STATISTIC_ID, [])


async def test_first_run_backfills_and_accumulates(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """A fresh install imports the whole series as a running total."""
    values = [0.1, 0.2, 0.0, 0.3, 0.4]
    readings = [
        IntervalReading(SERIES_START + timedelta(hours=i), v, False)
        for i, v in enumerate(values)
    ]
    coordinator = _coordinator(hass, _make_client(readings))

    await coordinator.async_refresh()
    rows = await _read_back(hass)

    assert len(rows) == len(values)
    assert [pytest.approx(r["state"]) for r in rows] == values
    assert [pytest.approx(r["sum"]) for r in rows] == [0.1, 0.3, 0.3, 0.6, 1.0]
    assert coordinator.last_update_success


async def test_metadata_is_registered_for_the_energy_dashboard(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """The statistic must be external, summed, and denominated in CCF.

    The Energy dashboard only offers a source that carries a sum and a volume
    unit, so these fields are load-bearing rather than cosmetic.
    """
    coordinator = _coordinator(hass, _make_client(_readings(3)))
    await coordinator.async_refresh()
    await async_wait_recording_done(hass)

    metadata = await get_instance(hass).async_add_executor_job(
        partial(get_metadata, hass, statistic_ids={STATISTIC_ID})
    )

    assert STATISTIC_ID in metadata, "no metadata row was written"
    _, meta = metadata[STATISTIC_ID]
    assert meta["source"] == DOMAIN
    assert meta["has_sum"] is True
    assert meta["has_mean"] is False
    assert meta["unit_of_measurement"] == UnitOfVolume.CENTUM_CUBIC_FEET


async def test_second_refresh_resumes_the_running_total(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """The overlap window must continue the total, not restart or double it.

    The series is long enough that stepping back `OVERLAP_DAYS` lands inside it,
    so the coordinator has to look up the cumulative total preceding the window
    rather than falling back to zero.
    """
    hours = (OVERLAP_DAYS + 3) * 24
    first = _readings(hours)
    coordinator = _coordinator(hass, _make_client(first))
    await coordinator.async_refresh()
    rows_first = await _read_back(hass)
    assert len(rows_first) == hours

    # A day of new readings arrives; the overlap re-fetches recent hours too.
    extended = _readings(hours + 24)
    coordinator.client = _make_client(extended)
    await coordinator.async_refresh()
    rows_second = await _read_back(hass)

    assert len(rows_second) == hours + 24, "new hours were not appended"

    sums = [r["sum"] for r in rows_second]
    assert sums == sorted(sums), "cumulative total went backwards"
    assert sums[-1] == pytest.approx(0.01 * (hours + 24)), "total was lost or doubled"

    # The untouched prefix must be identical across refreshes.
    assert rows_second[0]["sum"] == pytest.approx(rows_first[0]["sum"])


async def test_revised_values_in_the_overlap_are_corrected(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """The utility revises recent hours, and the re-import must follow."""
    hours = (OVERLAP_DAYS + 3) * 24
    coordinator = _coordinator(hass, _make_client(_readings(hours)))
    await coordinator.async_refresh()
    await _read_back(hass)

    # The final hour is restated upward.
    revised = _readings(hours)
    revised[-1] = IntervalReading(revised[-1].start, 5.0, False)
    coordinator.client = _make_client(revised)
    await coordinator.async_refresh()
    rows = await _read_back(hass)

    assert rows[-1]["state"] == pytest.approx(5.0)
    assert rows[-1]["sum"] == pytest.approx(0.01 * (hours - 1) + 5.0)


async def test_no_readings_is_not_a_failure(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """An empty window is normal — the portal lags about a day."""
    coordinator = _coordinator(hass, _make_client([]))
    await coordinator.async_refresh()

    assert coordinator.last_update_success
    assert await _read_back(hass) == []
