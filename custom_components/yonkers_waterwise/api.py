"""Client for the Yonkers WaterWise (SpryPoint SpryEngage) customer portal.

The portal has no documented public API. Everything here was derived from the
portal's own front-end, which drives a Play Framework backend:

    POST /api/authenticate                                 -> session cookie
    GET  /customer/water-smart-meters                      -> accounts (inline JSON)
    GET  /api/admin/meters/water-smart-meters-for-account/{account}
    GET  /api/admin/meters/getDateRangeForMeter/{meter}/{account}
    GET  /api/shared/interval?...&period=Hourly            -> consumption series
"""

from __future__ import annotations

import json
import logging
from base64 import b64encode
from dataclasses import dataclass
from datetime import date, datetime

import aiohttp
from yarl import URL

from .const import BASE_URL

_LOGGER = logging.getLogger(__name__)

# Marker for the inline `accountOptions: ko.toJS([...])` blob that the portal
# server-renders into the smart-meter page.
_ACCOUNTS_MARKER = "accountOptions: ko.toJS("


class YonkersWaterWiseError(Exception):
    """Base error for this integration."""


class InvalidAuth(YonkersWaterWiseError):
    """Raised when the portal rejects the supplied credentials."""


class CannotConnect(YonkersWaterWiseError):
    """Raised when the portal is unreachable or answers with nonsense."""


@dataclass(frozen=True, slots=True)
class Account:
    """A billing account exposed by the portal."""

    account_number: str
    description: str


@dataclass(frozen=True, slots=True)
class IntervalReading:
    """A single consumption bucket.

    `start` is timezone-aware. `adjusted` marks buckets the utility flagged as
    a catch-up for a missed read rather than genuine usage in that hour.
    """

    start: datetime
    value: float
    adjusted: bool


def floor_to_hour(value: datetime) -> datetime:
    """Truncate a datetime to the top of its hour.

    The portal builds its hourly buckets outward from whatever `start_date` it
    is given, so a request starting at 14:06 comes back bucketed on :06 rather
    than on the hour. Home Assistant keys long-term statistics by the hour they
    start, so every request has to be floored before it goes out.
    """
    return value.replace(minute=0, second=0, microsecond=0)


def _extract_json_array(text: str, marker: str) -> list:
    """Pull the JSON array that immediately follows `marker` out of `text`.

    A non-greedy regex is not safe here because the array contains nested
    objects, so match brackets while skipping over string literals.
    """
    idx = text.find(marker)
    if idx == -1:
        raise CannotConnect(
            "Could not locate the account list in the portal page. "
            "The portal layout has probably changed."
        )
    start = text.index("[", idx)

    depth = 0
    in_string = False
    escaped = False
    for pos in range(start, len(text)):
        char = text[pos]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : pos + 1])
                except json.JSONDecodeError as err:
                    raise CannotConnect(f"Malformed account list: {err}") from err
    raise CannotConnect("Unterminated account list in the portal page.")


class YonkersWaterWiseClient:
    """Thin async wrapper around the portal's private endpoints."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        username: str,
        password: str,
    ) -> None:
        """Store credentials and the session used for every request."""
        self._session = session
        self._username = username
        self._password = password
        self._authenticated = False

    async def async_login(self) -> None:
        """Authenticate and populate the session cookie jar.

        The portal takes HTTP Basic credentials on a POST body rather than a
        form. hCaptcha is disabled for the Yonkers tenant, so the captcha field
        is sent empty; if the city ever enables it this call will start failing
        and there is no way around it from here.
        """
        auth = b64encode(f"{self._username}:{self._password}".encode()).decode()
        try:
            async with self._session.post(
                f"{BASE_URL}/api/authenticate",
                headers={
                    "Authorization": f"Basic {auth}",
                    "X-Requested-With": "XMLHttpRequest",
                },
                data={"h-captcha-response": ""},
            ) as resp:
                body = await resp.text()
                if resp.status in (400, 401, 403):
                    raise InvalidAuth(body.strip() or "Login rejected by the portal.")
                if resp.status >= 400:
                    raise CannotConnect(
                        f"Login failed with HTTP {resp.status}: {body[:200]}"
                    )
        except aiohttp.ClientError as err:
            raise CannotConnect(f"Could not reach the portal: {err}") from err

        self._authenticated = True
        _LOGGER.debug("Authenticated with the Yonkers WaterWise portal")

    async def _async_request(
        self, path: str, params: dict | None = None
    ) -> aiohttp.ClientResponse:
        """GET `path`, logging in first (or again) when the session has lapsed.

        The session JWT lives about eight hours. When it expires the portal
        answers with a redirect to the login page rather than a 401, so treat
        "we asked for JSON and got HTML" as a signal to re-authenticate.
        """
        if not self._authenticated:
            await self.async_login()

        for attempt in (1, 2):
            try:
                resp = await self._session.get(
                    f"{BASE_URL}{path}",
                    params=params,
                    headers={
                        "X-Requested-With": "XMLHttpRequest",
                        "Accept": "application/json, text/javascript, */*; q=0.01",
                    },
                )
            except aiohttp.ClientError as err:
                raise CannotConnect(f"Request to {path} failed: {err}") from err

            bounced = resp.status in (401, 403) or "/login" in resp.url.path
            if bounced and attempt == 1:
                resp.release()
                _LOGGER.debug("Session expired, re-authenticating")
                self._authenticated = False
                await self.async_login()
                continue
            if bounced:
                resp.release()
                raise InvalidAuth("Portal redirected to login after re-authenticating.")
            if resp.status >= 400:
                text = await resp.text()
                raise CannotConnect(f"{path} returned HTTP {resp.status}: {text[:200]}")
            return resp

        raise CannotConnect(f"Gave up requesting {path}")

    async def _async_get_json(self, path: str, params: dict | None = None):
        """GET `path` and decode the JSON body."""
        resp = await self._async_request(path, params)
        async with resp:
            try:
                # The portal serves JSON as application/json, but the interval
                # endpoint is inconsistent about it, so don't enforce the type.
                return await resp.json(content_type=None)
            except (aiohttp.ContentTypeError, json.JSONDecodeError) as err:
                raise CannotConnect(f"{path} did not return JSON: {err}") from err

    @staticmethod
    def _unwrap(payload, path: str):
        """Return the `data` member of a standard portal envelope."""
        if not isinstance(payload, dict) or "data" not in payload:
            raise CannotConnect(
                f"Unexpected response shape from {path}: {str(payload)[:200]}"
            )
        return payload["data"]

    async def async_get_accounts(self) -> list[Account]:
        """List the billing accounts visible to this login.

        There is no JSON endpoint for this; the portal renders the account list
        into the smart-meter page as an inline Knockout view model.
        """
        resp = await self._async_request("/customer/water-smart-meters")
        async with resp:
            html = await resp.text()

        accounts: list[Account] = []
        for raw in _extract_json_array(html, _ACCOUNTS_MARKER):
            number = raw.get("accountNumber") or raw.get("accountNumberFormatted")
            if not number:
                continue
            accounts.append(
                Account(
                    account_number=str(number),
                    description=raw.get("description") or str(number),
                )
            )

        if not accounts:
            raise CannotConnect(
                "The portal returned no billing accounts for this login."
            )
        return accounts

    async def async_get_water_meters(self, account_number: str) -> list[str]:
        """List smart water meter numbers attached to an account."""
        path = f"/api/admin/meters/water-smart-meters-for-account/{account_number}"
        data = self._unwrap(await self._async_get_json(path), path)
        return [str(meter) for meter in (data or [])]

    async def async_get_available_range(
        self, meter_number: str, account_number: str
    ) -> tuple[date, date]:
        """Return the first and last dates the portal holds data for."""
        path = f"/api/admin/meters/getDateRangeForMeter/{meter_number}/{account_number}"
        data = self._unwrap(await self._async_get_json(path), path)
        try:
            return (
                date.fromisoformat(data["startDate"]),
                date.fromisoformat(data["endDate"]),
            )
        except (KeyError, TypeError, ValueError) as err:
            raise CannotConnect(f"Could not parse the meter date range: {err}") from err

    async def async_get_hourly_usage(
        self,
        account_number: str,
        meter_number: str,
        start: datetime,
        end: datetime,
    ) -> list[IntervalReading]:
        """Fetch hourly consumption between two aware datetimes.

        Both bounds are inclusive on the portal's side, and both are floored to
        the hour before being sent. Timestamps on hourly data come back naive,
        so they are localized to the caller-supplied `start`'s timezone.
        """
        # Bucket boundaries follow the minute of `start_date`, so both bounds
        # are floored to keep the returned timestamps on the hour.
        start = floor_to_hour(start)
        end = floor_to_hour(end)

        params = {
            "account_number": account_number,
            "meter_id": meter_number,
            "service_category": "WATER",
            "format": "json",
            "period": "Hourly",
            # isoformat renders exactly what the portal expects: 2026-08-13T00:00-04:00
            "start_date": start.isoformat(timespec="minutes"),
            "end_date": end.isoformat(timespec="minutes"),
        }
        payload = await self._async_get_json("/api/shared/interval", params)

        if not isinstance(payload, list):
            raise CannotConnect(f"Unexpected interval payload: {str(payload)[:200]}")

        series = next(
            (
                item
                for item in payload
                if isinstance(item, dict)
                and item.get("seriesGroup") == "Water"
                and item.get("measurementType") == "Consumption"
            ),
            None,
        )
        if series is None:
            # A meter with no data in the window returns the temperature
            # series only, which is not an error.
            _LOGGER.debug(
                "No water consumption series for meter %s between %s and %s",
                meter_number,
                start,
                end,
            )
            return []

        tzinfo = start.tzinfo
        readings: list[IntervalReading] = []
        for point in series.get("dataPoints") or []:
            raw_date = point.get("date")
            value = point.get("value")
            if raw_date is None or value is None:
                continue
            try:
                parsed = datetime.fromisoformat(raw_date)
            except ValueError:
                _LOGGER.debug("Skipping unparseable interval timestamp %r", raw_date)
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=tzinfo)
            readings.append(
                IntervalReading(
                    start=parsed,
                    value=float(value),
                    adjusted=bool(point.get("isAdjustedMeterRead")),
                )
            )

        readings.sort(key=lambda reading: reading.start)

        if misaligned := [r for r in readings if r.start.minute or r.start.second]:
            # Long-term statistics are keyed by the hour, so off-hour buckets
            # would be recorded against the wrong period.
            _LOGGER.warning(
                "Portal returned %d hourly readings that are not on the hour "
                "(first: %s); statistics for meter %s may be misaligned",
                len(misaligned),
                misaligned[0].start.isoformat(),
                meter_number,
            )

        return readings

    def build_export_url(
        self,
        account_number: str,
        meter_number: str,
        start: datetime,
        end: datetime,
        period: str = "Hourly",
    ) -> str:
        """Build the portal's own CSV download URL, for troubleshooting."""
        return str(
            URL(f"{BASE_URL}/api/shared/interval").with_query(
                {
                    "account_number": account_number,
                    "meter_id": meter_number,
                    "service_category": "WATER",
                    "format": "csv",
                    "period": period,
                    "start_date": start.isoformat(timespec="minutes"),
                    "end_date": end.isoformat(timespec="minutes"),
                }
            )
        )


def normalize_readings(readings: list[IntervalReading]) -> list[IntervalReading]:
    """Force readings onto strictly increasing timestamps.

    Hourly timestamps arrive as naive local time, and the portal labels
    daylight-saving days inconsistently. Observed behaviour:

    * Spring forward (2026-03-08): 02:00 is never emitted and 03:00 appears
      twice, so there is a genuine duplicate that `fold` cannot resolve
      because 03:00 EDT is not an ambiguous time.
    * Fall back (2025-11-02): a single 01:00 is emitted for what is really a
      25-hour day, so no duplicate appears at all.

    Statistics are keyed by start time, so a repeated timestamp would
    overwrite its twin and quietly lose an hour of water. Shift the
    duplicate into the second pass of the hour where that is meaningful,
    and otherwise merge its usage into the preceding bucket so the
    cumulative total stays correct even though the hour it lands in is
    approximate.
    """
    normalized: list[IntervalReading] = []
    for reading in readings:
        # Compare absolute instants, not wall-clock values: Python ignores
        # `fold` when comparing two datetimes that share a tzinfo, which is
        # exactly the case the fall-back branch below depends on.
        previous_ts = normalized[-1].start.timestamp() if normalized else None
        if previous_ts is None or reading.start.timestamp() > previous_ts:
            normalized.append(reading)
            continue

        shifted = reading.start.replace(fold=1)
        if shifted.timestamp() > previous_ts:
            _LOGGER.debug("Resolved ambiguous DST hour at %s", reading.start)
            normalized.append(IntervalReading(shifted, reading.value, reading.adjusted))
            continue

        previous = normalized[-1]
        _LOGGER.debug(
            "Merging duplicate reading at %s into the preceding hour",
            reading.start,
        )
        normalized[-1] = IntervalReading(
            previous.start,
            previous.value + reading.value,
            previous.adjusted or reading.adjusted,
        )
    return normalized
