#!/usr/bin/env python3
"""Exercise the WaterWise client outside Home Assistant.

Useful for checking credentials, seeing what the portal actually returns, and
confirming a change to api.py before reloading the integration.

    pip install aiohttp yarl
    export YWW_USERNAME='you@example.com'
    export YWW_PASSWORD='...'
    python3 scripts/fetch_usage.py --days 3
    python3 scripts/fetch_usage.py --days 30 --csv usage.csv
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import getpass
import importlib
import os
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp

# Load the integration's modules as a package without importing the Home
# Assistant-dependent __init__.py alongside them.
_PKG_DIR = (
    Path(__file__).resolve().parent.parent / "custom_components" / "yonkers_waterwise"
)
_pkg = types.ModuleType("_yww")
_pkg.__path__ = [str(_PKG_DIR)]
sys.modules["_yww"] = _pkg

const = importlib.import_module("_yww.const")
api = importlib.import_module("_yww.api")


async def main() -> int:
    """Log in, enumerate meters, and dump recent hourly usage."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--days", type=int, default=3, help="how many days back to fetch (default: 3)"
    )
    parser.add_argument("--account", help="account number (default: first found)")
    parser.add_argument("--meter", help="meter number (default: first found)")
    parser.add_argument("--csv", help="also write the readings to this CSV file")
    args = parser.parse_args()

    username = os.environ.get("YWW_USERNAME") or input("Email: ").strip()
    password = os.environ.get("YWW_PASSWORD") or getpass.getpass("Password: ")

    tz = ZoneInfo(const.UTILITY_TIMEZONE)

    async with aiohttp.ClientSession() as session:
        client = api.YonkersWaterWiseClient(session, username, password)

        print("Authenticating...")
        await client.async_login()
        print("  ok")

        accounts = await client.async_get_accounts()
        print(f"\nAccounts ({len(accounts)}):")
        for account in accounts:
            print(f"  {account.account_number}  {account.description}")

        account_number = args.account or accounts[0].account_number

        meters = await client.async_get_water_meters(account_number)
        print(f"\nSmart water meters on {account_number}: {meters or 'none'}")
        if not meters:
            return 1
        meter_number = args.meter or meters[0]

        first, last = await client.async_get_available_range(
            meter_number, account_number
        )
        print(f"Portal holds data for meter {meter_number} from {first} to {last}")

        end = datetime.now(tz)
        start = end - timedelta(days=args.days)
        print(
            f"\nFetching hourly usage {start:%Y-%m-%d %H:%M} -> {end:%Y-%m-%d %H:%M} ..."
        )
        readings = await client.async_get_hourly_usage(
            account_number, meter_number, start, end
        )

        if not readings:
            print("  no readings returned")
            return 0

        total = sum(reading.value for reading in readings)
        adjusted = [reading for reading in readings if reading.adjusted]
        print(f"  {len(readings)} hourly readings, {total:.2f} CCF total")
        print(f"  newest reading: {readings[-1].start:%Y-%m-%d %H:%M %Z}")
        print(f"  lag behind now: {end - readings[-1].start}")
        if adjusted:
            print(f"  {len(adjusted)} flagged as missed-read adjustments")

        print("\n  last 24 buckets:")
        for reading in readings[-24:]:
            flag = "  (adjusted)" if reading.adjusted else ""
            print(
                f"    {reading.start:%Y-%m-%d %H:%M %Z}  {reading.value:8.3f} CCF{flag}"
            )

        if args.csv:
            # Blocking IO is fine here: this is a one-shot CLI script.
            with open(args.csv, "w", newline="") as handle:  # noqa: ASYNC230
                writer = csv.writer(handle)
                writer.writerow(["start", "ccf", "adjusted"])
                for reading in readings:
                    writer.writerow(
                        [reading.start.isoformat(), reading.value, reading.adjusted]
                    )
            print(f"\nWrote {len(readings)} rows to {args.csv}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
