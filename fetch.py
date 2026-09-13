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
"""

import argparse
import csv
import io
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).parent
CSV_PATH = ROOT / "data" / "series.csv"
BLOCK_PATH = ROOT / "data" / "brief-block.md"
TIMEOUT = 25
UA = {"User-Agent": "brief-dataengine/2.0 (+github actions)"}

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
]

successes: list[str] = []
failures: list[str] = []


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


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Brief block
# ---------------------------------------------------------------------------
def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


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
        line("Gold", "gold_usd", 0, "$"),
        line("Brent", "brent_usd", 2, "$"),
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
    partial-failure path is exercised rather than assumed."""

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
            # GC=F and BZ=F fail so the FALLBACK path is exercised, not assumed.
            if sym in ("GC=F", "BZ=F"):
                raise RuntimeError("simulated primary outage")
            prices = {
                "SAP": 216.00, "GME": 21.50, "NVDA": 180.00,
                "^SET.BK": 1200.00,
                "^TNX": 43.12,   # the x10 convention - must normalise to 4.312
                "^TYX": 999.0,   # absurd - must be REJECTED by the sane band
            }
            return _Resp({"chart": {"result": [
                {"meta": {"regularMarketPrice": prices[sym]}}]}})
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
        if "stlouisfed" in url:
            series = params.get("series_id")
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
    check("missing series shows n/a, not 0", "| Brent | n/a |" in block)
    check("failure surfaced in block", "Sources unavailable" in block)
    check("week count stated", "2 weeks on record" in block)
    check("target range rendered", "| FOMC target range | 3.50-3.75% |" in block)

    print("Self-test: formatting edges")
    check("pct handles blank", pct("", "100") == "")
    check("pct flags unchanged as flat", pct("100", "100") == "flat")
    check("pct handles zero base", pct("5", "0") == "")
    check("fmt handles None", fmt(None) == "n/a")

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
    when = when or datetime.now(timezone.utc)
    y, w, _ = when.isocalendar()
    row = {k: "" for k in FIELDS}
    row["date"] = when.strftime("%Y-%m-%d")
    row["iso_week"] = f"{y}-W{w:02d}"

    fetch_coingecko(row, http)
    fetch_fx(row, http)
    fetch_quotes(row, http)
    fetch_nyfed(row, http)
    fetch_fred(row, http)

    row["sources_ok"] = len(successes)
    row["sources_failed"] = ";".join(failures)
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch and print, write nothing")
    ap.add_argument("--selftest", action="store_true",
                    help="run the pipeline offline against fixtures")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    now = datetime.now(timezone.utc)
    y, w, _ = now.isocalendar()
    iso_week = f"{y}-W{w:02d}"
    rows = read_rows()

    if already_recorded(iso_week, rows) and not args.dry_run:
        print(f"{iso_week} already recorded - nothing to do.")
        return 0

    print(f"Collecting for {iso_week} ...")
    row = collect()

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

    append(row)
    rows = read_rows()
    BLOCK_PATH.write_text(render_block(rows), encoding="utf-8")
    print(f"Wrote {iso_week}: {len(successes)} ok, {len(failures)} failed. "
          f"{len(rows)} weeks on record.")

    if crypto_dead or fx_dead:
        print("A core block is down - row written, run marked failed.",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
