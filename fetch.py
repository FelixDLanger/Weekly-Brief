#!/usr/bin/env python3
"""
Weekly series collector for the Saturday brief.

Appends one row per ISO week to data/series.csv and regenerates
data/brief-block.md - a paste-ready market snapshot with week-over-week
deltas, formatted for section 3 of the brief.

    python fetch.py              collect, write, regenerate the block
    python fetch.py --dry-run    fetch and print, write nothing
    python fetch.py --selftest   run the whole pipeline offline against
                                 fixtures. No network. Exits non-zero on
                                 any logic failure.

Design rules:
  - NO API KEY IS REQUIRED FOR ANY COLUMN. One optional secret exists
    (FRED_API_KEY) and every field it touches has a keyless fallback:
    without it the two Treasury yields come from the public quote chain
    instead of the official series, and nothing else changes. EFFR and the
    FOMC target range come from the New York Fed, keyless.
  - Every quoted series has TWO sources tried in order (Yahoo, then Stooq)
    and a plausible-range check. A value outside its band is rejected rather
    than stored: a blank is recoverable, a silently wrong number is not.
  - Individual source failures are tolerated: the field is written empty and
    named in sources_failed. Losing a WHOLE block (crypto or fx) fails the run.
  - Idempotent per ISO week. Re-running on the same Saturday is a no-op.
  - PUBLIC DATA ONLY. Never add positions, holdings, order sizes or account
    values to this file. If the repo is public, this rule is the whole fence.

v2.1 (October 2026):
  - DATED Brent (EIA's physical North Sea price, via FRED) next to the Brent
    future, with its own observation date. Keyless by default; the FRED API
    is used only when FRED_API_KEY is set.
  - The CONTRACT behind each continuous future (BZ=F, GC=F) is stored as a
    label such as "Z26", so a roll prints as "roll Z26->F27" instead of a
    fake week-on-week change between two different contracts.
  - The CSV schema MIGRATES itself: new columns are appended at the end and
    earlier weeks are padded with blanks. Any other header change is refused.
  - sources_ok keeps one unit - successful source calls - on repair runs too.
"""

import argparse
import csv
import io
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).parent
CSV_PATH = ROOT / "data" / "series.csv"
BLOCK_PATH = ROOT / "data" / "brief-block.md"
TIMEOUT = 25
UA = {"User-Agent": "brief-dataengine/2.1 (+github actions)"}

# The brief runs on Bangkok time (UTC+7, no daylight saving). Rows are dated
# and keyed by the BANGKOK calendar, so the Friday-night UTC run is filed under
# the Saturday it serves - the same date convention as every row before it.
BKK = timezone(timedelta(hours=7), "Asia/Bangkok")


def run_clock():
    """Now, on the brief's clock."""
    return datetime.now(timezone.utc).astimezone(BKK)

# Column order is FIXED. To add a series, append to the END of this list.
# Never insert in the middle - historic rows misalign and the CSV is the record.
FIELDS = [
    "date", "iso_week",
    # crypto
    "btc_usd", "btc_mcap", "eth_usd", "sol_usd", "sol_mcap",
    "total_crypto_mcap", "btc_dominance",
    "usdt_mcap", "usdc_mcap", "stablecoin_proxy_mcap",
    # equities
    "sap_adr", "gme", "nvda", "set_index",
    # fx
    "usdthb", "usdsgd", "eurusd",
    # commodities / rates
    "gold_usd", "brent_usd", "us10y", "us30y",
    "ffr", "fed_target_lo", "fed_target_hi",
    # meta
    "sources_ok", "sources_failed",
    # v2.1 - appended at the END, per the rule above
    "brent_dated_usd", "brent_dated_date", "brent_contract", "gold_contract",
]

# Metadata travels WITH a value but is not a value: it is never counted as a
# data field and never repaired on its own.
META_FIELDS = ("date", "iso_week", "sources_ok", "sources_failed",
               "brent_dated_date", "brent_contract", "gold_contract")

# A companion is written together with its value, and only with it - so a
# repaired value can never sit next to a label from a different reading.
COMPANIONS = {
    "brent_dated_usd": ("brent_dated_date",),
    "brent_usd": ("brent_contract",),
    "gold_usd": ("gold_contract",),
}

successes: list[str] = []
failures: list[str] = []
provenance: dict[str, str] = {}      # field -> the source that supplied it
_yahoo_meta: dict[str, dict] = {}    # symbol -> chart meta, this run only


def reset_run_state():
    """One collection, one clean slate. collect() can run twice in a process
    (repair, the self-test), and a success from an earlier pass must never be
    counted again or lend its provenance to a later value."""
    global successes, failures, provenance, _yahoo_meta
    successes, failures, provenance, _yahoo_meta = [], [], {}, {}


def ok(name):
    successes.append(name)


def fail(name, err):
    failures.append(name)
    print(f"  ! {name}: {err}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------
def fetch_coingecko(row, http=requests):
    """Two calls, not five. /simple/price returns every asset at once and
    /global gives total cap and dominance without deriving them."""
    try:
        r = http.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={
                "ids": "bitcoin,ethereum,solana,tether,usd-coin",
                "vs_currencies": "usd",
                "include_market_cap": "true",
            },
            headers=UA, timeout=TIMEOUT,
        )
        r.raise_for_status()
        d = r.json()
        row["btc_usd"] = d["bitcoin"]["usd"]
        row["btc_mcap"] = d["bitcoin"]["usd_market_cap"]
        row["eth_usd"] = d["ethereum"]["usd"]
        row["sol_usd"] = d["solana"]["usd"]
        row["sol_mcap"] = d["solana"]["usd_market_cap"]
        row["usdt_mcap"] = d["tether"]["usd_market_cap"]
        row["usdc_mcap"] = d["usd-coin"]["usd_market_cap"]
        # PROXY, named honestly. USDT + USDC is ~85-90% of total stablecoin
        # float. It is not the aggregate - but it IS a single fixed definition,
        # which is exactly what the brief's stablecoin watch item asked for.
        row["stablecoin_proxy_mcap"] = (
            d["tether"]["usd_market_cap"] + d["usd-coin"]["usd_market_cap"]
        )
        ok("coingecko/prices")
    except Exception as e:
        fail("coingecko/prices", e)

    try:
        r = http.get("https://api.coingecko.com/api/v3/global",
                     headers=UA, timeout=TIMEOUT)
        r.raise_for_status()
        g = r.json()["data"]
        row["total_crypto_mcap"] = g["total_market_cap"]["usd"]
        row["btc_dominance"] = g["market_cap_percentage"]["btc"]
        ok("coingecko/global")
    except Exception as e:
        fail("coingecko/global", e)


def fetch_fx(row, http=requests):
    """Frankfurter, ECB reference rates. Free, no key, weekday publication."""
    try:
        r = http.get("https://api.frankfurter.app/latest",
                     params={"from": "USD", "to": "THB,SGD,EUR"},
                     headers=UA, timeout=TIMEOUT)
        r.raise_for_status()
        rates = r.json()["rates"]
        row["usdthb"] = rates.get("THB")
        row["usdsgd"] = rates.get("SGD")
        eur_per_usd = rates.get("EUR")
        # Quote EUR/USD the conventional way: USD per EUR.
        row["eurusd"] = round(1 / eur_per_usd, 6) if eur_per_usd else None
        ok("frankfurter/fx")
    except Exception as e:
        fail("frankfurter/fx", e)


def yahoo_last_close(symbol, http=requests):
    """Yahoo chart endpoint. Free, no key, and - unlike Stooq - it answers
    requests from cloud datacentre IP ranges, which is what GitHub runners
    have. This is the PRIMARY source as of 13 Sept 2026."""
    r = http.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                 params={"range": "5d", "interval": "1d"},
                 headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    _yahoo_meta[symbol] = res.get("meta") or {}
    price = res["meta"].get("regularMarketPrice")
    if price in (None, ""):
        raise ValueError("no regularMarketPrice in meta")
    v = float(price)
    # ^TNX and ^TYX have historically been quoted at ten times the yield.
    # Yahoo now serves the plain percentage, but the convention has flipped
    # before, so normalise rather than trust it. The SANE band below is the
    # backstop if it ever flips again in the other direction.
    if symbol in ("^TNX", "^TYX") and v > 20:
        v = v / 10.0
    return v


def stooq_last_close(symbol, http=requests):
    r = http.get("https://stooq.com/q/l/",
                 params={"s": symbol, "f": "sd2t2ohlcv", "h": "", "e": "csv"},
                 headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    rows = list(csv.DictReader(io.StringIO(r.text)))
    if not rows:
        raise ValueError("empty response")
    close = rows[0].get("Close")
    if close in (None, "", "N/D"):
        raise ValueError(f"no close in response: {rows[0]}")
    return float(close)


# Ordered fallback per field. Yahoo first because Stooq returned nothing at
# all from the GitHub runner on 13 Sept 2026 - all eight symbols failed while
# every other source succeeded, which is the signature of an IP-range block
# rather than a symbol problem. Stooq is kept as the second source because it
# works from other environments and a second source is the whole point.
QUOTE_SOURCES = {
    "sap_adr":   [("yahoo", "SAP"),      ("stooq", "sap.us")],
    "gme":       [("yahoo", "GME"),      ("stooq", "gme.us")],
    "nvda":      [("yahoo", "NVDA"),     ("stooq", "nvda.us")],
    "set_index": [("yahoo", "^SET.BK"),  ("stooq", "^set")],
    "gold_usd":  [("yahoo", "GC=F"),     ("stooq", "xauusd")],
    "brent_usd": [("yahoo", "BZ=F"),     ("stooq", "cb.f")],
    "us10y":     [("yahoo", "^TNX"),     ("stooq", "10usy.b")],
    "us30y":     [("yahoo", "^TYX"),     ("stooq", "30usy.b")],
}

# Plausible ranges. A value outside its band is REJECTED, not stored - a
# silently wrong number is far worse than a blank, and a source changing its
# scale or its symbol meaning is exactly how that happens.
SANE = {
    "sap_adr":   (20, 2000),
    "gme":       (0.5, 2000),
    "nvda":      (1, 10000),
    "set_index": (100, 10000),
    "gold_usd":  (200, 20000),
    "brent_usd": (5, 500),
    "us10y":     (0.1, 20),
    "us30y":     (0.1, 20),
    "brent_dated_usd": (5, 500),
}

FETCHERS = {"yahoo": yahoo_last_close, "stooq": stooq_last_close}


def fetch_quotes(row, http=requests):
    """Try each source in order, take the first value that passes its sanity
    band, and name the source that actually supplied it. Individually
    tolerant: a blank column for one week is not a problem; a silently wrong
    column would be."""
    for field, sources in QUOTE_SOURCES.items():
        errors = []
        for name, sym in sources:
            try:
                v = FETCHERS[name](sym, http)
                lo, hi = SANE[field]
                if not (lo <= v <= hi):
                    raise ValueError(
                        f"{v} outside sane band {lo}-{hi} for {field}")
                row[field] = v
                provenance[field] = f"{name}/{sym}"
                ok(f"{name}/{sym}")
                break
            except Exception as e:
                errors.append(f"{name}/{sym}: {e}")
        else:
            fail(field, " | ".join(errors))


def fetch_nyfed(row, http=requests):
    """New York Fed markets API. Free, NO KEY, and it is the primary source
    for EFFR rather than a fallback - FRED only republishes this.

    It also returns the FOMC TARGET RANGE, which is what the rate forecasts
    in the brief actually resolve against ("the FOMC raises the target
    range"). That makes those rows machine-resolvable instead of hand-checked.
    """
    try:
        r = http.get(
            "https://markets.newyorkfed.org/api/rates/unsecured/effr/last/1.json",
            headers=UA, timeout=TIMEOUT,
        )
        r.raise_for_status()
        obs = r.json()["refRates"][0]
        row["ffr"] = float(obs["percentRate"])
        if obs.get("targetRateFrom") is not None:
            row["fed_target_lo"] = float(obs["targetRateFrom"])
        if obs.get("targetRateTo") is not None:
            row["fed_target_hi"] = float(obs["targetRateTo"])
        ok("nyfed/effr")
    except Exception as e:
        fail("nyfed/effr", e)


FRED_SERIES = {"us10y": "DGS10", "us30y": "DGS30"}


def fetch_fred(row, http=requests):
    """OPTIONAL, and the only optional thing here. It upgrades the two
    Treasury yields from Stooq to the official series. Nothing is lost by
    never setting the key - EFFR comes from the NY Fed, keyless."""
    key = os.environ.get("FRED_API_KEY")
    if not key:
        print("  - FRED_API_KEY not set: yields stay on Stooq (nothing lost)")
        return
    for field, series in FRED_SERIES.items():
        try:
            r = http.get("https://api.stlouisfed.org/fred/series/observations",
                         params={"series_id": series, "api_key": key,
                                 "file_type": "json", "sort_order": "desc",
                                 "limit": 10},
                         headers=UA, timeout=TIMEOUT)
            r.raise_for_status()
            for obs in r.json()["observations"]:
                if obs["value"] not in (".", "", None):
                    row[field] = float(obs["value"])
                    break
            ok(f"fred/{series}")
        except Exception as e:
            fail(f"fred/{series}", e)


DATED_BRENT_SERIES = "DCOILBRENTEU"   # EIA, Europe Brent Spot Price FOB
DATED_MAX_AGE_DAYS = 16               # FRED posts EIA's spot prices weekly, with a lag


def _parse_fred_graph_csv(text):
    """FRED's keyless graph CSV. The header is 'observation_date,<ID>' (since
    2024) or 'DATE,<ID>' (older) - both are accepted. Missing days are '.' or
    empty and are skipped. Returns the latest (date, value)."""
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        raise ValueError("empty response")
    header = [h.strip().lower() for h in rows[0]]
    if len(header) < 2 or header[0] not in ("observation_date", "date"):
        raise ValueError(f"unexpected header: {rows[0]}")
    latest = None
    for r in rows[1:]:
        if len(r) < 2:
            continue
        day, value = r[0].strip(), r[1].strip()
        if value in ("", "."):
            continue
        latest = (day, float(value))
    if latest is None:
        raise ValueError("no observations")
    return latest


def fetch_dated_brent(row, http=requests, when=None):
    """Dated Brent: the price of a physical North Sea cargo, EIA via FRED.

    brent_usd (the future) answers "what is a barrel for later delivery";
    this answers "what does a cargo cost now". In a supply shock the two can
    sit $20 apart for weeks, so the brief needs both - and the brief's own
    forecasts resolve on the EIA series, not on the future.

    Keyless by default (FRED's public graph CSV); the API is used only when
    FRED_API_KEY is set. The observation date is stored with the value, and a
    reading older than DATED_MAX_AGE_DAYS is REJECTED: a stale print stored
    under this week's date would look like a fresh one."""
    when = when or run_clock()
    src = f"fred/{DATED_BRENT_SERIES}"
    key = os.environ.get("FRED_API_KEY")
    try:
        if key:
            r = http.get("https://api.stlouisfed.org/fred/series/observations",
                         params={"series_id": DATED_BRENT_SERIES, "api_key": key,
                                 "file_type": "json", "sort_order": "desc",
                                 "limit": 10},
                         headers=UA, timeout=TIMEOUT)
            r.raise_for_status()
            obs = next((o for o in r.json()["observations"]
                        if o.get("value") not in (".", "", None)), None)
            if obs is None:
                raise ValueError("no observations")
            day, value = obs["date"], float(obs["value"])
        else:
            start = (when - timedelta(days=45)).strftime("%Y-%m-%d")
            r = http.get("https://fred.stlouisfed.org/graph/fredgraph.csv",
                         params={"id": DATED_BRENT_SERIES, "cosd": start},
                         headers=UA, timeout=TIMEOUT)
            r.raise_for_status()
            day, value = _parse_fred_graph_csv(r.text)
        age = (when.date() - datetime.strptime(day, "%Y-%m-%d").date()).days
        if age > DATED_MAX_AGE_DAYS:
            raise ValueError(f"latest observation {day} is {age} days old")
        if age < -1:
            raise ValueError(f"observation {day} is dated after the run")
        lo, hi = SANE["brent_dated_usd"]
        if not (lo <= value <= hi):
            raise ValueError(f"{value} outside sane band {lo}-{hi}")
        row["brent_dated_usd"] = value
        row["brent_dated_date"] = day
        provenance["brent_dated_usd"] = src
        ok(src)
    except Exception as e:
        fail(src, e)


MONTH_CODES = "FGHJKMNQUVXZ"
MONTH_ABBR = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def contract_code(year, month):
    """CME-style contract code: month letter plus two-digit year, e.g. Z26."""
    return f"{MONTH_CODES[month - 1]}{year % 100:02d}"


def _add_months(year, month, k):
    m = month - 1 + k
    return year + m // 12, m % 12 + 1


def brent_candidates(when):
    """Brent lists a contract for every month, and the front month sits two
    or three months ahead (December expires on the last business day of
    October). Four candidates cover every point in the cycle."""
    return [contract_code(*_add_months(when.year, when.month, k))
            for k in (1, 2, 3, 4)]


GOLD_ACTIVE_MONTHS = (2, 4, 6, 8, 10, 12)


def gold_candidates(when):
    """COMEX gold's active months are Feb, Apr, Jun, Aug, Oct and Dec, and
    the continuous series rolls before a delivery month starts - so the front
    is always an active month strictly after the current one."""
    out, y, m = [], when.year, when.month
    while len(out) < 3:
        y, m = _add_months(y, m, 1)
        if m in GOLD_ACTIVE_MONTHS:
            out.append(contract_code(y, m))
    return out


# field: (continuous symbol, Yahoo contract suffix, root, candidates, label column)
CONTRACT_SPECS = {
    "brent_usd": ("BZ=F", ".NYM", "BZ", brent_candidates, "brent_contract"),
    "gold_usd": ("GC=F", ".CMX", "GC", gold_candidates, "gold_contract"),
}
CONTRACT_MATCH_TOL = 0.005    # a contract matches if within 0.5% of the continuous price

_SHORTNAME_MONTH = re.compile(
    r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+'?(\d{2}|\d{4})\b")


def _label_from_shortname(name):
    m = _SHORTNAME_MONTH.search(name or "")
    if not m:
        return None
    return contract_code(int(m.group(2)) % 100, MONTH_ABBR.index(m.group(1)) + 1)


def fetch_contracts(row, http=requests, when=None):
    """Which contract is behind each continuous future? METADATA ONLY: the
    label never changes a value, and failing to find it is not a source
    failure.

    It exists because a continuous futures series rolls from one contract to
    the next before expiry, and a week-on-week change across a roll compares
    two different contracts - on 23 Oct 2026 that turned a +6.2% week for
    December Brent into a printed +1.7%. With the label stored, the block
    prints the roll instead of a fake change.

    Method: price-match the continuous quote against the individual contracts
    that could be the front month; fall back to the month in Yahoo's short
    name only if no contract could be fetched at all."""
    when = when or run_clock()
    for field, (cont, suffix, root, candidates, label_col) in CONTRACT_SPECS.items():
        if provenance.get(field) != f"yahoo/{cont}":
            continue   # blank, or from a fallback with no contract (e.g. spot gold)
        price = num(row.get(field))
        if not price:
            continue
        best, fetched_any = None, False
        for code in candidates(when):
            try:
                p = yahoo_last_close(f"{root}{code}{suffix}", http)
            except Exception:
                continue
            fetched_any = True
            gap = abs(p / price - 1)
            if gap <= CONTRACT_MATCH_TOL and (best is None or gap < best[0]):
                best = (gap, code)
                if gap < 1e-9:
                    break
        label = best[1] if best else None
        if label is None and not fetched_any:
            label = _label_from_shortname((_yahoo_meta.get(cont) or {}).get("shortName"))
        if label:
            row[label_col] = label
        else:
            print(f"  - {field}: contract behind {cont} not identified "
                  f"(label left blank, value unaffected)")


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------
class SchemaError(RuntimeError):
    """The CSV's header is not one this version can safely extend."""


def migrate_schema(path=None, write=True):
    """Bring an existing CSV up to the current FIELDS before anything reads
    or writes it. Returns "none" (no file yet), "current", "migrated", or -
    with write=False - "would-migrate".

    The only schema change ever allowed is appending columns at the END (the
    rule above FIELDS). So a header that is a strict prefix of FIELDS is
    padded: the new columns are added and every earlier row gets blanks in
    them, its existing cells untouched. Anything else - a reordered, renamed
    or longer header, or a ragged row - is REFUSED with SchemaError. The CSV
    is the record, and a silent re-map would corrupt it. The rewrite goes
    through a temporary file and an atomic rename, so a crash mid-way leaves
    the original intact."""
    path = path or CSV_PATH
    if not path.exists() or path.stat().st_size == 0:
        return "none"
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        body = list(reader)
    if header is None:
        return "none"
    if header == FIELDS:
        return "current"
    if len(header) >= len(FIELDS) or header != FIELDS[:len(header)]:
        raise SchemaError(
            f"{path.name}: header is not a prefix of the current FIELDS, so "
            f"it cannot be extended safely. Restore the original header. "
            f"Found {len(header)} columns: {header}")
    for i, r in enumerate(body, start=2):
        if len(r) != len(header):
            raise SchemaError(
                f"{path.name}: line {i} has {len(r)} cells against "
                f"{len(header)} header columns - refusing to migrate a ragged file")
    if not write:
        return "would-migrate"
    pad = [""] * (len(FIELDS) - len(header))
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(FIELDS)
        for r in body:
            w.writerow(r + pad)
    os.replace(tmp, path)
    return "migrated"


def schema_state(path=None):
    """Read-only version of migrate_schema, for dry runs."""
    return migrate_schema(path, write=False)


def read_rows():
    if not CSV_PATH.exists():
        return []
    with CSV_PATH.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def append(row):
    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    is_new = not CSV_PATH.exists()
    with CSV_PATH.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        if is_new:
            w.writeheader()
        w.writerow(row)


def replace_week(row):
    """Rewrite the file with this ISO week's row swapped for a fresh one.

    One row per ISO week is the contract, so a re-run inside the same week
    must UPDATE that week rather than append a duplicate or refuse to run.
    This is what makes a source fix recoverable without waiting for the next
    Saturday - which is exactly the hole the plain idempotence guard left."""
    rows = read_rows()
    out = [row if r.get("iso_week") == row["iso_week"] else r for r in rows]
    with CSV_PATH.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(out)


DATA_FIELDS = [f for f in FIELDS if f not in META_FIELDS]

# Which source call supplies each data field. A repair adds one to
# sources_ok per family that newly supplied a value, which keeps the column's
# unit (successful source calls) the same as on a normal run. Quoted fields
# are one call each.
SOURCE_FAMILY = {
    **{f: "coingecko/prices" for f in (
        "btc_usd", "btc_mcap", "eth_usd", "sol_usd", "sol_mcap",
        "usdt_mcap", "usdc_mcap", "stablecoin_proxy_mcap")},
    **{f: "coingecko/global" for f in ("total_crypto_mcap", "btc_dominance")},
    **{f: "frankfurter/fx" for f in ("usdthb", "usdsgd", "eurusd")},
    **{f: "nyfed/effr" for f in ("ffr", "fed_target_lo", "fed_target_hi")},
    "brent_dated_usd": "fred/DCOILBRENTEU",
}


def blanks_in(existing):
    return [f for f in DATA_FIELDS if existing.get(f) in (None, "")]


def repair_week(iso_week, rows, http=requests, when=None):
    """Fill ONLY the fields that are currently blank. Never touch a value
    that is already there.

    A full replace would re-read sources that worked and overwrite good data
    with a later reading - and, worse, could BLANK a good field if that
    source happens to be down at the moment of the repair. Repair cannot make
    the row worse; replace can. So repair is the default for a same-week
    re-run and --force is the explicit opt-in to a whole fresh snapshot.
    """
    existing = next(r for r in rows if r.get("iso_week") == iso_week)
    missing = blanks_in(existing)
    if not missing:
        return existing, [], []

    fresh = collect(http, when=when)
    filled = [f for f in missing if fresh.get(f) not in (None, "")]

    merged = dict(existing)
    for f in filled:
        merged[f] = fresh[f]
        for c in COMPANIONS.get(f, ()):
            merged[c] = fresh.get(c, "")
    still_blank = [f for f in missing if f not in filled]
    merged["sources_failed"] = ";".join(still_blank)
    # v2.0 recounted non-blank data FIELDS here (about 26) while a normal run
    # counts successful SOURCE CALLS (12 on a clean week), so the column
    # changed its unit on every repaired week. Keep the unit instead.
    base = int(num(existing.get("sources_ok")) or 0)
    merged["sources_ok"] = base + len(
        {SOURCE_FAMILY.get(f, f"quote/{f}") for f in filled})
    return merged, filled, still_blank


def write_block():
    """Always regenerate from whatever is in the CSV. The block is DERIVED,
    so it must never be allowed to drift from the file it is derived from -
    including on a run that collects nothing."""
    rows = read_rows()
    if rows:
        BLOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        BLOCK_PATH.write_text(render_block(rows), encoding="utf-8")
    return rows


# ---------------------------------------------------------------------------
# Brief block
# ---------------------------------------------------------------------------
def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def last_business_day(date_str):
    """The trading day before a run date: Saturday's run reads Friday's close."""
    try:
        d = datetime.strptime(date_str or "", "%Y-%m-%d").date()
    except ValueError:
        return ""
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def pct(now, prev):
    a, b = num(now), num(prev)
    if a is None or b is None or b == 0:
        return ""
    change = (a / b - 1) * 100
    # Below a tenth of a point is noise in a weekly table, and a column of
    # "+0.0%" reads as movement when there was none.
    if abs(change) < 0.05:
        return "flat"
    return f"{change:+.1f}%"


def fmt(v, dp=2, prefix="", suffix=""):
    x = num(v)
    if x is None:
        return "n/a"
    return f"{prefix}{x:,.{dp}f}{suffix}"


def target_line(cur):
    """The FOMC target range, which is what the rate forecasts resolve
    against. Printed as a range, so no delta column."""
    lo, hi = num(cur.get("fed_target_lo")), num(cur.get("fed_target_hi"))
    if lo is None or hi is None:
        return "| FOMC target range | n/a | - |"
    return f"| FOMC target range | {lo:.2f}-{hi:.2f}% | - |"


def render_block(rows):
    """Regenerated in full every run. This is the paste-ready snapshot for
    section 3 of the brief - it replaces retyping figures out of a browser,
    which is where transcription errors come from."""
    cur = rows[-1]
    prev = rows[-2] if len(rows) > 1 else {}

    def line(label, field, dp=2, prefix="", suffix="", show_delta=True):
        v = fmt(cur.get(field), dp, prefix, suffix)
        d = pct(cur.get(field), prev.get(field)) if show_delta else ""
        return f"| {label} | {v} | {d or '-'} |"

    rolls = []

    def future_line(label, symbol, field, cfield, dp):
        """A continuous future, named with the contract behind it. Across a
        roll the week-on-week change compares two different contracts, so it
        is replaced by the roll itself."""
        c, pc = cur.get(cfield) or "", prev.get(cfield) or ""
        name = f"{label} ({symbol}, {c})" if c else f"{label} ({symbol})"
        v = fmt(cur.get(field), dp, "$")
        if num(cur.get(field)) is None:
            return f"| {name} | {v} | - |"
        if c and pc and c != pc:
            rolls.append(f"{symbol} {pc}→{c}")
            return f"| {name} | {v} | roll {pc}→{c} |"
        d = pct(cur.get(field), prev.get(field))
        if d and c and not pc:
            d += " (prev unlabeled)"
        elif d and pc and not c:
            d += " (contract unknown)"
        return f"| {name} | {v} | {d or '-'} |"

    def dated_line():
        """Dated Brent carries its own observation date. FRED posts EIA's
        prices weekly with a lag, so two runs can store the same print - that
        is said plainly instead of showing as 'flat'."""
        d = cur.get("brent_dated_date") or ""
        pd = prev.get("brent_dated_date") or ""
        name = f"Brent dated (EIA, obs {d})" if d else "Brent dated (EIA)"
        v = fmt(cur.get("brent_dated_usd"), 2, "$")
        if num(cur.get("brent_dated_usd")) is None:
            return f"| {name} | {v} | - |"
        if d and d == pd:
            return f"| {name} | {v} | same print |"
        delta = pct(cur.get("brent_dated_usd"), prev.get("brent_dated_usd"))
        return f"| {name} | {v} | {delta or '-'} |"

    def spread_line():
        """Physical minus futures: the premium for a barrel now over a barrel
        later. The two prints usually come from different days - say so."""
        dated, fut = num(cur.get("brent_dated_usd")), num(cur.get("brent_usd"))
        if dated is None or fut is None:
            return None
        label = "Dated minus BZ=F"
        obs = cur.get("brent_dated_date") or ""
        fday = last_business_day(cur.get("date"))
        if obs and fday and obs != fday:
            label += " (days differ)"
        gap = dated - fut
        return f"| {label} | {'+' if gap >= 0 else '-'}${abs(gap):,.2f} | - |"

    stable_bn = num(cur.get("stablecoin_proxy_mcap"))
    stable_txt = f"${stable_bn/1e9:,.1f}bn" if stable_bn else "n/a"
    stable_d = pct(cur.get("stablecoin_proxy_mcap"),
                   prev.get("stablecoin_proxy_mcap"))
    total_bn = num(cur.get("total_crypto_mcap"))
    total_txt = f"${total_bn/1e12:,.3f}tn" if total_bn else "n/a"
    total_d = pct(cur.get("total_crypto_mcap"), prev.get("total_crypto_mcap"))

    out = [
        f"<!-- generated by fetch.py - do not hand-edit -->",
        f"### Market snapshot - {cur.get('date')} (week {cur.get('iso_week')})",
        "",
        "| Series | Level | w/w |",
        "|---|---|---|",
        line("BTC", "btc_usd", 0, "$"),
        line("BTC dominance", "btc_dominance", 1, "", "%"),
        line("ETH", "eth_usd", 0, "$"),
        line("SOL", "sol_usd", 2, "$"),
        f"| Total crypto cap | {total_txt} | {total_d or '-'} |",
        f"| Stablecoin proxy (USDT+USDC) | {stable_txt} | {stable_d or '-'} |",
        line("USD/THB", "usdthb", 3),
        line("USD/SGD", "usdsgd", 4),
        line("EUR/USD", "eurusd", 4),
        future_line("Gold", "GC=F", "gold_usd", "gold_contract", 0),
        future_line("Brent futures", "BZ=F", "brent_usd", "brent_contract", 2),
        dated_line(),
        spread_line(),
        line("US 10y", "us10y", 2, "", "%"),
        line("US 30y", "us30y", 2, "", "%"),
        line("Fed funds (EFFR)", "ffr", 2, "", "%"),
        target_line(cur),
        line("SET index", "set_index", 2),
        line("NVDA", "nvda", 2, "$"),
        line("GME", "gme", 2, "$"),
        line("SAP ADR", "sap_adr", 2, "$"),
        "",
    ]
    out = [x for x in out if x is not None]
    if rolls:
        out.append(f"*Contract roll this week: {', '.join(rolls)}. The w/w "
                   f"change would compare two different contracts, so it is "
                   f"not printed.*")
        out.append("")
    if cur.get("sources_failed"):
        out.append(f"*Sources unavailable this run: {cur['sources_failed']}*")
        out.append("")
    out.append(f"*{len(rows)} weeks on record. Deltas are week-over-week "
               f"against the previous stored row, not a calendar week.*")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Self-test - the whole pipeline, offline
# ---------------------------------------------------------------------------
class _Resp:
    def __init__(self, payload=None, text=None):
        self._payload, self.text = payload, text

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeHTTP:
    """Replays fixtures. Deliberately fails one Stooq symbol so the
    partial-failure path is exercised rather than assumed.

    v2.1: futures_up serves BZ=F and GC=F (by default both fail, to keep the
    fallback path tested), plus the individual contracts behind them;
    contracts_down makes every single-contract lookup fail; dated sets the
    latest dated-Brent observation (date, value)."""

    def __init__(self, futures_up=False, contracts_down=False,
                 brent_cont=116.72, dated=("2026-09-08", "97.40")):
        self.futures_up, self.contracts_down = futures_up, contracts_down
        self.brent_cont, self.dated = brent_cont, dated
        self.contracts = {
            "BZX26.NYM": 120.10, "BZZ26.NYM": 116.72, "BZF27.NYM": 111.70,
            "BZG27.NYM": 108.00, "BZH27.NYM": 105.40,
            "GCZ26.CMX": 4007.9, "GCG27.CMX": 4040.5, "GCJ27.CMX": 4070.2,
        }

    def _day_after(self, day):
        d = datetime.strptime(day, "%Y-%m-%d") + timedelta(days=1)
        return d.strftime("%Y-%m-%d")

    def get(self, url, params=None, headers=None, timeout=None):
        params = params or {}
        if "simple/price" in url:
            return _Resp({
                "bitcoin":  {"usd": 77316.38, "usd_market_cap": 1.54e12},
                "ethereum": {"usd": 2534.83,  "usd_market_cap": 3.05e11},
                "solana":   {"usd": 102.06,   "usd_market_cap": 5.9878e10},
                "tether":   {"usd": 1.0,      "usd_market_cap": 1.72e11},
                "usd-coin": {"usd": 1.0,      "usd_market_cap": 7.4e10},
            })
        if "api/v3/global" in url:
            return _Resp({"data": {"total_market_cap": {"usd": 2.742379e12},
                                   "market_cap_percentage": {"btc": 56.6}}})
        if "frankfurter" in url:
            return _Resp({"rates": {"THB": 32.41, "SGD": 1.2845,
                                    "EUR": 0.9174}})
        if "query1.finance.yahoo.com" in url:
            sym = url.rsplit("/", 1)[-1]
            # By default GC=F and BZ=F fail so the FALLBACK path is exercised,
            # not assumed.
            if sym in ("GC=F", "BZ=F") and not self.futures_up:
                raise RuntimeError("simulated primary outage")
            if sym.endswith((".NYM", ".CMX")):
                if self.contracts_down or sym not in self.contracts:
                    raise RuntimeError("no such contract")
                return _Resp({"chart": {"result": [{"meta": {
                    "regularMarketPrice": self.contracts[sym]}}]}})
            prices = {
                "SAP": 216.00, "GME": 21.50, "NVDA": 180.00,
                "^SET.BK": 1200.00,
                "^TNX": 43.12,   # the x10 convention - must normalise to 4.312
                "^TYX": 999.0,   # absurd - must be REJECTED by the sane band
                "BZ=F": self.brent_cont, "GC=F": 4007.9,
            }
            meta = {"regularMarketPrice": prices[sym]}
            short = {"BZ=F": "Brent Crude Oil Last Day Financ",
                     "GC=F": "Gold Dec 26"}
            if sym in short:
                meta["shortName"] = short[sym]
            return _Resp({"chart": {"result": [{"meta": meta}]}})
        if "stooq.com" in url:
            sym = params.get("s")
            if sym == "cb.f":       # both sources down for brent -> blank
                raise RuntimeError("simulated source outage")
            # Yields deliberately DIFFER from the FRED fixture so the
            # "FRED overwrites" and "no key" checks actually discriminate.
            prices = {"sap.us": 214.30, "gme.us": 21.15, "nvda.us": 178.42,
                      "^set": 1198.40, "xauusd": 3420.0,
                      "10usy.b": 4.28, "30usy.b": 4.81}
            return _Resp(text="Symbol,Date,Time,Open,High,Low,Close,Volume\n"
                              f"{sym},2026-09-11,22:00:00,0,0,0,"
                              f"{prices.get(sym, 1.0)},0\n")
        if "newyorkfed.org" in url:
            return _Resp({"refRates": [{
                "effectiveDate": "2026-09-10", "type": "EFFR",
                "percentRate": 3.63,
                "targetRateFrom": 3.5, "targetRateTo": 3.75,
            }]})
        if "fredgraph.csv" in url:
            day, value = self.dated
            return _Resp(text="observation_date,DCOILBRENTEU\n"
                              "2026-08-31,95.10\n"
                              f"{day},{value}\n"
                              f"{self._day_after(day)},.\n")
        if "stlouisfed" in url:
            series = params.get("series_id")
            if series == "DCOILBRENTEU":
                day, value = self.dated
                return _Resp({"observations": [
                    {"date": self._day_after(day), "value": "."},
                    {"date": day, "value": value}]})
            vals = {"DGS10": "4.31", "DGS30": "4.88"}
            return _Resp({"observations": [{"value": "."},
                                           {"value": vals[series]}]})
        raise RuntimeError(f"unmocked url: {url}")


def selftest():
    global CSV_PATH, BLOCK_PATH, successes, failures
    import tempfile
    checks, problems = 0, []

    def check(label, cond):
        nonlocal checks
        checks += 1
        if cond:
            print(f"  ok   {label}")
        else:
            print(f"  FAIL {label}")
            problems.append(label)

    tmp = Path(tempfile.mkdtemp())
    CSV_PATH = tmp / "data" / "series.csv"
    BLOCK_PATH = tmp / "data" / "brief-block.md"
    os.environ["FRED_API_KEY"] = "selftest"
    http = _FakeHTTP()

    print("Self-test: week 1")
    successes, failures = [], []
    r1 = collect(http, when=datetime(2026, 9, 12, tzinfo=timezone.utc))
    append(r1)
    check("header written once", CSV_PATH.read_text().count("date,iso_week") == 1)
    check("btc parsed", r1["btc_usd"] == 77316.38)
    check("eurusd inverted to USD-per-EUR", abs(r1["eurusd"] - 1.090037) < 1e-4)
    check("stablecoin proxy summed", r1["stablecoin_proxy_mcap"] == 1.72e11 + 7.4e10)
    check("FRED overwrote Stooq yields when key present", r1["us10y"] == 4.31)
    check("ffr collected from NY Fed", r1["ffr"] == 3.63)
    check("fed target range captured",
          r1["fed_target_lo"] == 3.5 and r1["fed_target_hi"] == 3.75)
    check("primary source used when it works", r1["sap_adr"] == 216.00)
    check("falls back when primary fails", r1["gold_usd"] == 3420.0)
    check("both sources down leaves the field blank, not zero",
          r1["brent_usd"] == "" and "brent_usd" in r1["sources_failed"])
    check("yield x10 convention normalised at source",
          abs(yahoo_last_close("^TNX", http) - 4.312) < 1e-6)
    check("plain-percent yield left alone",
          yahoo_last_close("SAP", http) == 216.00)
    check("out-of-band value REJECTED, fallback used", r1["us30y"] == 4.88)
    check("iso_week stamped", r1["iso_week"] == "2026-W37")
    check("no unexpected columns", set(r1) <= set(FIELDS))

    # The whole point of the secret design: ONE optional secret, and every
    # column it touches has a keyless fallback. Nothing goes blank without it.
    print("Self-test: no FRED key at all")
    successes, failures = [], []
    del os.environ["FRED_API_KEY"]
    rk = collect(http, when=datetime(2026, 9, 12, tzinfo=timezone.utc))
    check("ffr survives with no secret", rk["ffr"] == 3.63)
    check("target range survives with no secret", rk["fed_target_hi"] == 3.75)
    check("yield x10 normalised, no key", abs(rk["us10y"] - 4.312) < 1e-6)
    check("absurd yield rejected, second source used, no key",
          rk["us30y"] == 4.81)
    check("no column goes blank without the key",
          all(rk[f] != "" for f in ("ffr", "us10y", "us30y")))
    check("absent key is not logged as a failure",
          not any("fred" in f for f in failures))
    os.environ["FRED_API_KEY"] = "selftest"

    print("Self-test: idempotence")
    check("same week already recorded",
          already_recorded("2026-W37", read_rows()))
    check("new week not recorded",
          not already_recorded("2026-W38", read_rows()))

    print("Self-test: week 2 and deltas")
    successes, failures = [], []
    r2 = collect(http, when=datetime(2026, 9, 19, tzinfo=timezone.utc))
    r2["btc_usd"] = 81000.0          # +4.8%
    r2["sol_usd"] = 95.0             # -6.9%
    append(r2)
    rows = read_rows()
    check("two rows stored", len(rows) == 2)
    check("columns aligned after append",
          list(rows[0].keys()) == FIELDS and list(rows[1].keys()) == FIELDS)
    block = render_block(rows)
    BLOCK_PATH.write_text(block, encoding="utf-8")
    check("delta computed up", "+4.8%" in block)
    check("delta computed down", "-6.9%" in block)
    check("missing series shows n/a, not 0",
          "| Brent futures (BZ=F) | n/a |" in block)
    check("failure surfaced in block", "Sources unavailable" in block)
    check("week count stated", "2 weeks on record" in block)
    check("target range rendered", "| FOMC target range | 3.50-3.75% |" in block)

    print("Self-test: force-replace and block refresh")
    successes, failures = [], []
    before = len(read_rows())
    r2b = collect(http, when=datetime(2026, 9, 19, tzinfo=timezone.utc))
    r2b["btc_usd"] = 90000.0
    replace_week(r2b)
    rows2 = read_rows()
    check("force replaces, does not append", len(rows2) == before)
    check("replaced row carries the new value",
          rows2[-1]["btc_usd"] == "90000.0")
    check("earlier week untouched", rows2[0]["iso_week"] == "2026-W37")
    check("column order preserved after rewrite",
          list(rows2[0].keys()) == FIELDS and list(rows2[1].keys()) == FIELDS)
    write_block()
    check("block refreshed from the rewritten CSV",
          "$90,000" in BLOCK_PATH.read_text())

    print("Self-test: repair fills blanks and touches nothing else")
    successes, failures = [], []
    rows_now = read_rows()
    target = rows_now[-1]["iso_week"]
    # Blank one field that HAS a value and keep a known-good one to compare.
    good_btc = rows_now[-1]["btc_usd"]
    rows_now[-1]["gold_usd"] = ""
    rows_now[-1]["btc_usd"] = "12345.0"      # a value repair must NOT overwrite
    with CSV_PATH.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader(); w.writerows(rows_now)

    merged, filled, still = repair_week(
        target, read_rows(), http, when=datetime(2026, 9, 19, tzinfo=timezone.utc))
    check("repair fills the blank field", merged["gold_usd"] == 3420.0)
    check("repair does NOT overwrite an existing value",
          merged["btc_usd"] == "12345.0")
    check("repair reports what it filled", filled == ["gold_usd"])
    check("repair leaves genuinely dead fields blank",
          merged["brent_usd"] == "" and "brent_usd" in still)
    replace_week(merged)
    check("repair does not add a row", len(read_rows()) == len(rows_now))
    m2, filled2, _ = repair_week(target, read_rows(), http)
    check("repair is a no-op when only dead fields remain", filled2 == [])

    print("Self-test: formatting edges")
    check("pct handles blank", pct("", "100") == "")
    check("pct flags unchanged as flat", pct("100", "100") == "flat")
    check("pct handles zero base", pct("5", "0") == "")
    check("fmt handles None", fmt(None) == "n/a")

    # ------------------------------------------------------------------ v2.1
    def raises(fn, exc=Exception):
        try:
            fn()
        except exc:
            return True
        except Exception:
            return False
        return False

    def write_rows(rows_):
        with CSV_PATH.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows_)

    print("Self-test: v2.1 contract labels and dated Brent")
    check("spot fallback carries no contract label", r1["gold_contract"] == "")
    check("dated brent stored on the legacy weeks too",
          r1["brent_dated_usd"] == 97.40 and r1["brent_dated_date"] == "2026-09-08")
    tmp2 = Path(tempfile.mkdtemp())
    CSV_PATH = tmp2 / "data" / "series.csv"
    BLOCK_PATH = tmp2 / "data" / "brief-block.md"
    os.environ["FRED_API_KEY"] = "selftest"
    hf = _FakeHTTP(futures_up=True, dated=("2026-10-20", "115.22"))
    ra = collect(hf, when=datetime(2026, 10, 24, tzinfo=timezone.utc))
    check("brent future from the primary source", ra["brent_usd"] == 116.72)
    check("brent contract identified by price match", ra["brent_contract"] == "Z26")
    check("gold contract identified by price match", ra["gold_contract"] == "Z26")
    check("dated brent collected via the API", ra["brent_dated_usd"] == 115.22)
    check("dated brent carries its observation date",
          ra["brent_dated_date"] == "2026-10-20")
    check("contract lookups are not counted as sources",
          not any(".NYM" in s or ".CMX" in s for s in successes))
    check("sources_ok counts source calls, dated series included",
          ra["sources_ok"] == 15)
    check("brent candidates span the cycle",
          brent_candidates(datetime(2026, 10, 24)) == ["X26", "Z26", "F27", "G27"])
    check("gold candidates are active months after this one",
          gold_candidates(datetime(2026, 12, 3)) == ["G27", "J27", "M27"])
    append(ra)
    hf.brent_cont = 111.70        # the continuous series has rolled to January
    rb = collect(hf, when=datetime(2026, 10, 31, tzinfo=timezone.utc))
    check("roll detected: the new contract label", rb["brent_contract"] == "F27")
    append(rb)
    blk = render_block(read_rows())
    check("roll printed instead of a cross-contract delta",
          "| Brent futures (BZ=F, F27) | $111.70 | roll Z26→F27 |" in blk)
    check("roll explained under the table",
          "Contract roll this week: BZ=F Z26→F27." in blk)
    check("an unchanged contract keeps its delta",
          "| Gold (GC=F, Z26) | $4,008 | flat |" in blk)
    check("an unchanged dated print says so",
          "| Brent dated (EIA, obs 2026-10-20) | $115.22 | same print |" in blk)
    check("spread printed, different days flagged",
          "| Dated minus BZ=F (days differ) | +$3.52 | - |" in blk)
    check("last business day before a Saturday run is Friday",
          last_business_day("2026-10-31") == "2026-10-30")

    print("Self-test: v2.1 keyless dated Brent, staleness, fallbacks")
    del os.environ["FRED_API_KEY"]
    rk2 = collect(hf, when=datetime(2026, 10, 24, tzinfo=timezone.utc))
    check("dated brent keyless via the graph CSV",
          rk2["brent_dated_usd"] == 115.22 and rk2["brent_dated_date"] == "2026-10-20")
    check("keyless dated brent is not logged as a failure",
          not any("fred" in f for f in failures))
    check("legacy DATE header parsed, missing day skipped",
          _parse_fred_graph_csv("DATE,DCOILBRENTEU\n2026-10-19,118.82\n"
                                "2026-10-20,.\n") == ("2026-10-19", 118.82))
    check("unexpected graph header refused",
          raises(lambda: _parse_fred_graph_csv("foo,bar\n1,2\n")))
    rs = collect(hf, when=datetime(2026, 11, 20, tzinfo=timezone.utc))
    check("stale dated print rejected and named",
          rs["brent_dated_usd"] == "" and "fred/DCOILBRENTEU" in rs["sources_failed"])
    check("a rejected print leaves no orphan date", rs["brent_dated_date"] == "")
    hd = _FakeHTTP(futures_up=True, contracts_down=True,
                   dated=("2026-10-20", "115.22"))
    rf = collect(hd, when=datetime(2026, 10, 24, tzinfo=timezone.utc))
    check("gold label falls back to Yahoo's short name", rf["gold_contract"] == "Z26")
    check("brent label left blank when nothing identifies it",
          rf["brent_contract"] == "" and rf["brent_usd"] == 116.72)
    check("an unidentified contract is not a source failure",
          rf["sources_failed"] == "")
    check("short-name parser reads 'Dec 26'", _label_from_shortname("Gold Dec 26") == "Z26")
    check("short-name parser ignores names without a month",
          _label_from_shortname("Brent Crude Oil Last Day Financ") is None)
    ra2 = collect(hf, when=datetime(2026, 10, 24, tzinfo=timezone.utc))
    ra3 = collect(hf, when=datetime(2026, 10, 24, tzinfo=timezone.utc))
    check("a second collection does not double-count sources",
          ra2["sources_ok"] == ra3["sources_ok"] == 13)

    print("Self-test: v2.1 schema migration")
    old_fields = FIELDS[:-4]
    mig = tmp2 / "old.csv"
    rows_old = [[f"v{i}_{j}" for j in range(len(old_fields))] for i in range(2)]
    with mig.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(old_fields)
        w.writerows(rows_old)
    before_bytes = mig.read_bytes()
    check("old schema detected, dry check writes nothing",
          schema_state(mig) == "would-migrate" and mig.read_bytes() == before_bytes)
    check("migration reports migrated", migrate_schema(mig) == "migrated")
    with mig.open(newline="", encoding="utf-8") as f:
        mrows = list(csv.reader(f))
    check("header is now the current FIELDS", mrows[0] == FIELDS)
    check("every row padded to the full width",
          all(len(r) == len(FIELDS) for r in mrows[1:]))
    check("old cells unchanged", [r[:len(old_fields)] for r in mrows[1:]] == rows_old)
    check("new columns blank for earlier weeks",
          all(r[len(old_fields):] == [""] * 4 for r in mrows[1:]))
    after_first = mig.read_bytes()
    check("migration is idempotent",
          migrate_schema(mig) == "current" and mig.read_bytes() == after_first)
    check("no temporary file left behind", not (tmp2 / "old.csv.tmp").exists())
    bad = tmp2 / "bad.csv"
    swapped = list(old_fields)
    swapped[2], swapped[3] = swapped[3], swapped[2]
    bad.write_text(",".join(swapped) + "\r\n", encoding="utf-8")
    bad_before = bad.read_bytes()
    check("reordered header refused, file untouched",
          raises(lambda: migrate_schema(bad), SchemaError) and bad.read_bytes() == bad_before)
    longer = tmp2 / "longer.csv"
    longer.write_text(",".join(FIELDS + ["mystery"]) + "\r\n", encoding="utf-8")
    check("header longer than FIELDS refused",
          raises(lambda: migrate_schema(longer), SchemaError))
    ragged = tmp2 / "ragged.csv"
    ragged.write_text(",".join(old_fields) + "\r\n" + "a,b\r\n", encoding="utf-8")
    check("ragged file refused", raises(lambda: migrate_schema(ragged), SchemaError))
    check("missing file is not an error", migrate_schema(tmp2 / "none.csv") == "none")
    keep = CSV_PATH
    CSV_PATH = mig
    append(collect(hf, when=datetime(2026, 10, 24, tzinfo=timezone.utc)))
    with mig.open(newline="", encoding="utf-8") as f:
        check("a new row aligns after migration",
              all(len(r) == len(FIELDS) for r in csv.reader(f)))
    CSV_PATH = keep

    print("Self-test: v2.1 repair keeps units and companions")
    os.environ["FRED_API_KEY"] = "selftest"
    rows_r = read_rows()
    rows_r[-1].update({"gold_usd": "", "gold_contract": "",
                       "brent_contract": "", "sources_ok": "12"})
    write_rows(rows_r)
    wk = rows_r[-1]["iso_week"]
    m3, filled3, _ = repair_week(wk, read_rows(), hf,
                                 when=datetime(2026, 10, 31, tzinfo=timezone.utc))
    check("repair fills a value and its label together",
          m3["gold_usd"] == 4007.9 and m3["gold_contract"] == "Z26")
    check("repair never labels a value it did not fetch", m3["brent_contract"] == "")
    check("repair adds one per source call", m3["sources_ok"] == 13)
    replace_week(m3)
    rows_r2 = read_rows()
    rows_r2[-1].update({"btc_usd": "", "eth_usd": "", "sources_ok": "12"})
    write_rows(rows_r2)
    m4, filled4, _ = repair_week(wk, read_rows(), hf,
                                 when=datetime(2026, 10, 31, tzinfo=timezone.utc))
    check("two fields from one call count once",
          sorted(filled4) == ["btc_usd", "eth_usd"] and m4["sources_ok"] == 13)

    print("Self-test: v2.1 renders older rows")
    labeled = dict(read_rows()[-1])
    labeled["brent_contract"] = "F27"
    unlabeled = dict(labeled)
    for k in ("brent_contract", "gold_contract", "brent_dated_usd", "brent_dated_date"):
        unlabeled[k] = ""
    unlabeled["brent_usd"] = "116.72"
    check("first labeled week flags the unlabeled base",
          "(prev unlabeled)" in render_block([unlabeled, labeled]))
    check("an unlabeled week after a labeled one says so",
          "(contract unknown)" in render_block([labeled, unlabeled]))

    print("Self-test: v2.1 Bangkok run clock")
    fri_utc = datetime(2026, 10, 30, 22, 17, tzinfo=timezone.utc)
    sat_bkk = fri_utc.astimezone(BKK)
    check("Friday 22:17 UTC is Saturday 05:17 in Bangkok",
          sat_bkk.strftime("%Y-%m-%d %H:%M") == "2026-10-31 05:17")
    check("same ISO week on both sides of the date line",
          fri_utc.isocalendar()[:2] == sat_bkk.isocalendar()[:2])
    rr = collect(hf, when=sat_bkk)
    check("a Friday-night UTC run is filed under the Bangkok Saturday",
          rr["date"] == "2026-10-31" and rr["iso_week"] == "2026-W44")
    check("the run clock is UTC+7", run_clock().utcoffset() == timedelta(hours=7))

    print(f"\n{checks - len(problems)}/{checks} checks passed.")
    if problems:
        print("FAILED: " + "; ".join(problems), file=sys.stderr)
        return 1
    print("\n--- generated brief block ---")
    print(block)
    return 0


# ---------------------------------------------------------------------------
def already_recorded(iso_week, rows):
    return any(r.get("iso_week") == iso_week for r in rows)


def collect(http=requests, when=None):
    when = when or run_clock()
    reset_run_state()
    y, w, _ = when.isocalendar()
    row = {k: "" for k in FIELDS}
    row["date"] = when.strftime("%Y-%m-%d")
    row["iso_week"] = f"{y}-W{w:02d}"

    fetch_coingecko(row, http)
    fetch_fx(row, http)
    fetch_quotes(row, http)
    fetch_nyfed(row, http)
    fetch_fred(row, http)
    fetch_dated_brent(row, http, when)
    fetch_contracts(row, http, when)

    row["sources_ok"] = len(successes)
    row["sources_failed"] = ";".join(failures)
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch and print, write nothing")
    ap.add_argument("--repair", action="store_true",
                    help="fill only the BLANK fields in this week's row, "
                         "leaving every value that is already there alone")
    ap.add_argument("--force", action="store_true",
                    help="re-collect and REPLACE this week's row entirely "
                         "(a fresh snapshot - can blank a field whose source "
                         "is down; prefer --repair)")
    ap.add_argument("--selftest", action="store_true",
                    help="run the pipeline offline against fixtures")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    # Bring the record up to the current schema BEFORE anything reads or
    # writes it. A dry run only reports what a real run would do.
    try:
        state = schema_state() if args.dry_run else migrate_schema()
    except SchemaError as e:
        print(f"Schema check failed - nothing collected or written. {e}",
              file=sys.stderr)
        return 1
    if state == "migrated":
        print(f"Migrated {CSV_PATH.name} to the {len(FIELDS)}-column schema; "
              f"earlier weeks carry blanks in the new columns.")
    elif state == "would-migrate":
        print(f"Dry run: {CSV_PATH.name} would be migrated to the "
              f"{len(FIELDS)}-column schema on a real run.")

    now = run_clock()
    y, w, _ = now.isocalendar()
    iso_week = f"{y}-W{w:02d}"
    rows = read_rows()

    recorded = already_recorded(iso_week, rows)

    # REPAIR: same week, blanks present, and not an explicit full replace.
    if recorded and args.repair and not args.dry_run and not args.force:
        existing = next(r for r in rows if r.get("iso_week") == iso_week)
        missing = blanks_in(existing)
        if not missing:
            write_block()
            print(f"{iso_week} has no blank fields - nothing to repair.")
            return 0
        print(f"Repairing {iso_week}: {len(missing)} blank field(s) "
              f"-> {', '.join(missing)}")
        merged, filled, still_blank = repair_week(iso_week, rows, when=now)
        replace_week(merged)
        rows = write_block()
        print(f"Filled {len(filled)}: {', '.join(filled) or 'none'}. "
              f"Still blank: {', '.join(still_blank) or 'none'}. "
              f"Untouched fields kept their original values.")
        return 0 if not still_blank else 1

    if recorded and not args.dry_run and not args.force:
        # Regenerate the block even so: it is derived from the CSV and must
        # not be allowed to go stale behind a no-op.
        write_block()
        print(f"{iso_week} already recorded - nothing collected. "
              f"Block refreshed from {len(rows)} row(s). "
              f"Use --repair to fill blanks, --force for a fresh snapshot.")
        return 0

    print(f"Collecting for {iso_week} ...")
    row = collect(when=now)

    if args.dry_run:
        print(json.dumps(row, indent=2, default=str))
        print("dry run - nothing written.")
        return 0

    # A whole block failing is a pipeline problem, not a bad day at one vendor.
    crypto_dead = not any(s.startswith("coingecko") for s in successes)
    fx_dead = not any(s.startswith("frankfurter") for s in successes)
    if crypto_dead and fx_dead:
        print("Both core blocks failed - not writing a row.", file=sys.stderr)
        return 1

    if recorded:
        replace_week(row)
        verb = "Replaced"
    else:
        append(row)
        verb = "Wrote"
    rows = write_block()
    print(f"{verb} {iso_week}: {len(successes)} ok, {len(failures)} failed. "
          f"{len(rows)} weeks on record.")

    if crypto_dead or fx_dead:
        print("A core block is down - row written, run marked failed.",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
