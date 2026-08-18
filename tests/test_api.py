"""Tests for the portal client's parsing and normalisation helpers."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from _yww.api import (
    CannotConnect,
    IntervalReading,
    _extract_json_array,
    floor_to_hour,
    normalize_readings,
)

TZ = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
MARKER = "accountOptions: ko.toJS("


def _readings(day: str, hours: list[str], value: float = 1.0) -> list[IntervalReading]:
    """Build readings from naive local hour labels, as the portal sends them."""
    return [
        IntervalReading(
            datetime.fromisoformat(f"{day}T{hour}:00").replace(tzinfo=TZ),
            value,
            False,
        )
        for hour in hours
    ]


def _instants(readings: list[IntervalReading]) -> list[float]:
    return [reading.start.timestamp() for reading in readings]


class TestExtractJsonArray:
    """The account list is scraped out of an inline Knockout view model."""

    def test_extracts_simple_array(self) -> None:
        text = 'x accountOptions: ko.toJS([{"accountNumber":"1234567"}]) y'
        assert _extract_json_array(text, MARKER) == [{"accountNumber": "1234567"}]

    def test_survives_brackets_and_quotes_inside_strings(self) -> None:
        """A non-greedy regex would stop at the ']' inside the address."""
        text = (
            'accountOptions: ko.toJS([{"address1":"1 EXAMPLE ST ]a[b",'
            '"note":"he said \\"hi\\" ]"}]) trailing'
        )
        parsed = _extract_json_array(text, MARKER)
        assert parsed[0]["address1"] == "1 EXAMPLE ST ]a[b"
        assert parsed[0]["note"] == 'he said "hi" ]'

    def test_handles_nested_structures(self) -> None:
        text = 'accountOptions: ko.toJS([{"meters":[{"id":[1,2]}]}]) rest'
        assert _extract_json_array(text, MARKER) == [{"meters": [{"id": [1, 2]}]}]

    def test_raises_when_marker_absent(self) -> None:
        with pytest.raises(CannotConnect, match="Could not locate"):
            _extract_json_array("nothing here", MARKER)

    def test_raises_when_array_unterminated(self) -> None:
        with pytest.raises(CannotConnect, match="Unterminated"):
            _extract_json_array('accountOptions: ko.toJS([{"a":1}', MARKER)


class TestNormalizeReadings:
    """Daylight-saving days are the only place timestamps misbehave."""

    def test_ordinary_day_passes_through_untouched(self) -> None:
        readings = _readings("2026-08-16", [f"{hour:02d}" for hour in range(24)])
        assert normalize_readings(readings) == readings

    def test_spring_forward_duplicate_is_merged_not_dropped(self) -> None:
        """Observed 2026-03-08: the portal emits 03:00 twice and skips 02:00.

        03:00 EDT is not ambiguous, so `fold` cannot separate the pair. Merging
        keeps the water in the total; dropping would quietly lose an hour.
        """
        readings = _readings("2026-03-08", ["00", "01", "03", "03", "04"])
        result = normalize_readings(readings)

        assert len(result) == 4
        assert sum(r.value for r in result) == pytest.approx(5.0)
        assert result[2].value == pytest.approx(2.0)

    def test_fall_back_single_label_is_left_alone(self) -> None:
        """Observed 2025-11-02: only one 01:00 arrives, for a 25-hour day."""
        readings = _readings("2025-11-01", ["23"]) + _readings(
            "2025-11-02", ["00", "01", "02"]
        )
        result = normalize_readings(readings)

        assert len(result) == 4
        assert sum(r.value for r in result) == pytest.approx(4.0)

    def test_fall_back_duplicate_is_split_across_the_repeated_hour(self) -> None:
        """Defensive path: if 01:00 ever arrives twice, fold must separate it.

        This is the case that catches Python ignoring `fold` when two aware
        datetimes share a tzinfo — a naive `<` comparison merges them instead.
        """
        readings = _readings("2025-11-02", ["00", "01", "01", "02"])
        result = normalize_readings(readings)

        assert len(result) == 4
        assert all(r.value == pytest.approx(1.0) for r in result)
        # The repeated hour maps to 05:00 and 06:00 UTC, an hour apart.
        assert result[1].start.astimezone(UTC).hour == 5
        assert result[2].start.astimezone(UTC).hour == 6

    @pytest.mark.parametrize(
        ("day", "hours"),
        [
            ("2026-03-08", ["00", "01", "03", "03", "04", "05"]),
            ("2025-11-02", ["00", "01", "01", "02", "03"]),
            ("2026-08-16", ["00", "01", "02", "03"]),
        ],
    )
    def test_output_is_always_strictly_increasing(self, day, hours) -> None:
        """Statistics are keyed by start time, so collisions lose data."""
        result = normalize_readings(_readings(day, hours))
        instants = _instants(result)
        assert instants == sorted(instants)
        assert len(set(instants)) == len(instants)

    @pytest.mark.parametrize(
        ("day", "hours"),
        [
            ("2026-03-08", ["00", "01", "03", "03", "04"]),
            ("2025-11-02", ["00", "01", "01", "02"]),
        ],
    )
    def test_total_volume_is_never_lost(self, day, hours) -> None:
        readings = _readings(day, hours, value=2.5)
        result = normalize_readings(readings)
        assert sum(r.value for r in result) == pytest.approx(2.5 * len(hours))

    def test_adjusted_flag_survives_a_merge(self) -> None:
        readings = _readings("2026-03-08", ["00", "03"])
        readings.append(IntervalReading(readings[-1].start, 1.0, adjusted=True))
        result = normalize_readings(readings)
        assert result[-1].adjusted is True

    def test_empty_input(self) -> None:
        assert normalize_readings([]) == []


class TestFloorToHour:
    """Hourly buckets follow the minute given in `start_date`.

    Requesting 14:06 returns buckets stamped :06 rather than on the hour, which
    would record every statistic against the wrong period. Both request bounds
    are floored so that cannot happen.
    """

    @pytest.mark.parametrize(
        ("given", "expected"),
        [
            ("2026-08-15T14:06:33", "2026-08-15T14:00:00"),
            ("2026-08-15T14:37:00", "2026-08-15T14:00:00"),
            ("2026-08-15T14:00:00", "2026-08-15T14:00:00"),
            ("2026-08-15T00:59:59", "2026-08-15T00:00:00"),
        ],
    )
    def test_truncates_to_the_top_of_the_hour(self, given, expected) -> None:
        result = floor_to_hour(datetime.fromisoformat(given).replace(tzinfo=TZ))
        assert result == datetime.fromisoformat(expected).replace(tzinfo=TZ)
        assert result.microsecond == 0

    def test_preserves_timezone(self) -> None:
        value = datetime.fromisoformat("2026-08-15T14:06:33").replace(tzinfo=TZ)
        assert floor_to_hour(value).tzinfo is TZ

    def test_is_idempotent(self) -> None:
        value = datetime.fromisoformat("2026-08-15T14:06:33").replace(tzinfo=TZ)
        assert floor_to_hour(floor_to_hour(value)) == floor_to_hour(value)
