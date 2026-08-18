# Yonkers WaterWise for Home Assistant

Imports hourly water usage from the City of Yonkers [WaterWise
portal](https://mywateraccount.yonkersny.gov) into Home Assistant's long-term
statistics, so it shows up in the Energy dashboard's water section with the
correct historical timestamps.

## What it does

On first setup it backfills every hour the portal holds — currently about 14
months — and afterwards tops up the recent window on each refresh. Usage is
written straight into the statistics table rather than through a sensor
entity, which is what lets historical data land at the time it actually
happened instead of the time it was fetched.

Each meter also gets one diagnostic `Last reading` sensor so you can see how
current the import is.

## Installation

**HACS:** add this repository as a custom repository of type *Integration*,
then install *Yonkers WaterWise*.

**Manually:** copy `custom_components/yonkers_waterwise` into your Home
Assistant `config/custom_components/` directory.

Restart Home Assistant, then add the integration from
*Settings → Devices & Services → Add Integration → Yonkers WaterWise* and sign
in with the same email and password you use on the portal.

## Adding it to the Energy dashboard

Go to *Settings → Dashboards → Energy → Water consumption → Add water source*
and pick **Water usage &lt;meter number&gt;**.

Usage is reported in **CCF** (hundred cubic feet), the unit the city bills in.
One CCF is 748.05 US gallons.

## Things worth knowing

**Readings run about a day behind.** The portal is typically ~24 hours behind
real time, so this is not a live flow meter and cannot be used for immediate
leak detection. The integration polls every 3 hours, which is more than enough.

**Recent hours get rewritten.** Each refresh re-fetches the last few days,
because the utility revises recent readings as late data arrives. Statistics
for those hours are overwritten in place.

**Meters go quiet, and the catch-up lands in one bucket.** The portal flags
buckets with `isAdjustedMeterRead` when the meter did not report. Measured over
a full year on one meter, 821 of 8,737 hours (9.4%) were flagged and every one
of them was exactly zero — they are zero-filled gaps, not volume. The water
used during a gap shows up as a lump in the first *unflagged* hour afterwards.
Gaps came in 58 runs, the longest 125 hours, which was followed by a single
0.76 CCF hour.

The practical consequences: cumulative totals are correct, but an individual
hour can read zero when the meter was merely silent, and a single hour can
carry days of usage. Do not build overnight leak detection on hour-level
values. The flagged count for each refresh is exposed on the diagnostic
sensor's `adjusted_reads_last_run` attribute, which is a useful signal that the
meter has been out of contact.

**Daily figures can disagree by a day.** Summing the hourly series over a year
matched the portal's own daily series exactly (42.83 CCF both ways), but 33
individual days differed — always as adjacent offsetting pairs, where a
catch-up lump straddles midnight and corrects itself the next day.

**Daylight-saving days are approximate.** Hourly timestamps arrive as naive
local time, and the portal handles the transitions inconsistently: in March it
emits `03:00` twice and never emits `02:00`; in November it emits a single
`01:00` for a 25-hour day. The integration guarantees the daily total is right
and that no hour silently overwrites another, but on those two days an hour of
usage may be attributed to the neighbouring bucket.

**Multiple accounts.** If your login covers more than one water account, setup
asks which one to use. Add the integration again to track the others.

## Troubleshooting

`scripts/fetch_usage.py` exercises the same client outside Home Assistant,
which is the quickest way to tell a credential problem from an integration
problem:

```bash
pip install -r requirements-dev.txt
export YWW_USERNAME='you@example.com'
export YWW_PASSWORD='...'
python3 scripts/fetch_usage.py --days 3
python3 scripts/fetch_usage.py --days 30 --csv usage.csv
```

To see what the integration is doing, add to `configuration.yaml`:

```yaml
logger:
  logs:
    custom_components.yonkers_waterwise: debug
```

## How it works

The portal is a [SpryPoint](https://sprypoint.com) SpryEngage customer portal
running on Play Framework. There is no documented public API; these endpoints
were derived from the portal's own front-end JavaScript and may change without
notice.

| Purpose | Endpoint |
| --- | --- |
| Log in | `POST /api/authenticate` (HTTP Basic credentials) |
| List accounts | `GET /customer/water-smart-meters` (inline Knockout view model) |
| List meters | `GET /api/admin/meters/water-smart-meters-for-account/{account}` |
| Available date range | `GET /api/admin/meters/getDateRangeForMeter/{meter}/{account}` |
| Hourly usage | `GET /api/shared/interval?...&period=Hourly` |

The session is a `PLAY_SESSION_SESUG` JWT valid for about eight hours; the
client re-authenticates automatically when it lapses.

One quirk worth knowing if you extend this: the interval endpoint builds its
hourly buckets outward from whatever `start_date` you pass, so asking for
`14:06` returns buckets stamped `:06` instead of on the hour. Statistics are
keyed by the hour they start, so the client floors both bounds before sending
them, and warns if an off-hour timestamp comes back anyway.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

## Disclaimer

Not affiliated with or endorsed by the City of Yonkers or SpryPoint. It uses
undocumented endpoints and will break if the portal changes.
