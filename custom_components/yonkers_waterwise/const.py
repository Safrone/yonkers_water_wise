"""Constants for the Yonkers WaterWise integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "yonkers_waterwise"

BASE_URL: Final = "https://mywateraccount.yonkersny.gov"

# The portal reports interval timestamps in the utility's local time, with no
# UTC offset on hourly data. Yonkers is fixed to Eastern time.
UTILITY_TIMEZONE: Final = "America/New_York"

# Water volumes come back as CCF (hundred cubic feet).
CCF: Final = "CCF"

CONF_ACCOUNT_NUMBER: Final = "account_number"
CONF_METER_NUMBER: Final = "meter_number"

# Readings land roughly a day late, so there is nothing to gain from polling
# aggressively.
UPDATE_INTERVAL: Final = timedelta(hours=3)

# On incremental refreshes, re-fetch this far behind the newest statistic we
# already hold. Late-arriving reads and "missed read" adjustments rewrite
# recent history, so overlapping lets those corrections land.
OVERLAP_DAYS: Final = 5

# Backfill is requested in chunks; the API happily serves a year at a time.
BACKFILL_CHUNK_DAYS: Final = 365
