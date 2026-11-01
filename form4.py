#!/usr/bin/env python3
"""
Weekly insider-purchase clusters from SEC Form 4 filings.

Reads EDGAR's daily form index, opens each Form 4, keeps open-market
purchases (transaction code P), groups them by issuer over a rolling window
(14 days by default) and flags issuers where two or more insiders bought.
Clusters are joined with market cap and the date of the last results filing.

    python form4.py              collect new index days, write, regenerate
    python form4.py --dry-run    collect and print, write nothing
    python form4.py --selftest   offline, against fixtures, no network

Outputs (all regenerated or appended every run):
    data/insider-purchases.csv   every code-P purchase seen, one row per transaction
    data/insider-clusters.csv    the current window's clusters
    data/insider-block.md        paste-ready table for the brief
    data/form4-state.json        the last EDGAR index day processed

Design rules:
  - PUBLIC DATA ONLY. Insider filings are public. Never add positions,
    holdings or account values to this repo.
  - SEC fair access: every request to sec.gov carries a User-Agent with the
    operator's contact details. It comes ONLY from the SEC_USER_AGENT
    environment variable (a GitHub Secret). There is no default: without it
    the script stops before making a single request. It is never printed.
  - At most 8 requests a second to SEC (its limit is 10).
  - Idempotent: purchases are keyed by accession number and line, and an
    index day is processed once. A rerun the same day adds nothing.
  - Form 4/A amendments are skipped (they restate earlier filings), and
    joint filers count as one insider: the first reporting owner listed.
"""

import argparse
import csv
import io
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

try:
    import requests
except ImportError:          # the self-test does not need it
    requests = None

ROOT = Path(__file__).parent
DATA = ROOT / "data"
PURCHASES = DATA / "insider-purchases.csv"
CLUSTERS = DATA / "insider-clusters.csv"
BLOCK = DATA / "insider-block.md"
STATE = DATA / "form4-state.json"

WINDOW_DAYS = 14
MIN_INSIDERS = 2
FIRST_RUN_DAYS = 14          # how far back the very first run reaches
TIMEOUT = 30
MIN_INTERVAL = 0.125         # seconds between SEC requests -> 8 per second
YAHOO_UA = {"User-Agent": "brief-dataengine/2.1 (+github actions)"}

BKK = timezone(timedelta(hours=7), "Asia/Bangkok")

PURCHASE_FIELDS = [
    "accession", "line", "filed", "issuer_cik", "issuer_name", "ticker",
    "owner_cik", "owner_name", "role", "txn_date", "shares", "price",
    "value", "direct",
]
CLUSTER_FIELDS = [
    "issuer_cik", "issuer_name", "ticker", "insiders", "purchases",
    "shares", "value", "first_buy", "last_buy", "roles", "market_cap",
    "last_results", "last_results_form",
]
RESULTS_FORMS = ("10-Q", "10-K", "20-F", "40-F")


class SecRefused(RuntimeError):
    """SEC answered 403: the User-Agent is missing or not accepted."""


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class SecClient:
    """Every sec.gov request goes through here: one User-Agent, one rate."""

    def __init__(self, user_agent, http=None, sleep=time.sleep, clock=time.monotonic):
        ua = (user_agent or "").strip()
        if "@" not in ua or len(ua) < 6:
            raise SystemExit(
                "SEC_USER_AGENT is not set (or has no contact e-mail). SEC's "
                "fair-access policy requires one. Add it as a repository secret: "
                "Settings -> Secrets and variables -> Actions -> SEC_USER_AGENT, "
                'for example "Weekly-Brief name@example.com". No request was made.')
        self.headers = {"User-Agent": ua, "Accept-Encoding": "gzip, deflate"}
        self.http = http or requests
        self.sleep, self.clock = sleep, clock
        self._last = None
        self.calls = 0

    def get(self, url):
        """Returns (status, text). 404 is a normal answer; 403 is fatal."""
        for attempt in range(3):
            if self._last is not None:
                wait = MIN_INTERVAL - (self.clock() - self._last)
                if wait > 0:
                    self.sleep(wait)
            self._last = self.clock()
            self.calls += 1
            r = self.http.get(url, headers=self.headers, timeout=TIMEOUT)
            if r.status_code == 403:
                raise SecRefused(
                    "SEC returned 403. The SEC_USER_AGENT secret is missing, "
                    "malformed, or the runner is being throttled. Nothing was written.")
            if r.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                self.sleep(2 + 3 * attempt)
                continue
            return r.status_code, r.text
        return r.status_code, r.text


# ---------------------------------------------------------------------------
# EDGAR parsing
# ---------------------------------------------------------------------------
_IDX_LINE = re.compile(r"^(.*?)\s{2,}(.*?)\s+(\d{1,10})\s+(\d{8})\s+(edgar/\S+)\s*$")


def index_url(day):
    q = (day.month - 1) // 3 + 1
    return (f"https://www.sec.gov/Archives/edgar/daily-index/{day.year}/QTR{q}/"
            f"form.{day.strftime('%Y%m%d')}.idx")


def parse_form_index(text):
    """Form 4 entries from a daily form.idx, one per filing (a filing is
    listed once for the issuer and once for each reporting owner)."""
    out, seen, body = [], set(), False
    for line in text.splitlines():
        if not body:
            body = line.startswith("-----")
            continue
        m = _IDX_LINE.match(line)
        if not m or m.group(1).strip() != "4":
            continue
        path = m.group(5)
        if path in seen:
            continue
        seen.add(path)
        d = m.group(4)
        out.append({"path": path, "filed": f"{d[:4]}-{d[4:6]}-{d[6:]}",
                    "accession": path.rsplit("/", 1)[-1].replace(".txt", "")})
    return out


def _strip_ns(root):
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def _val(node, path):
    """Text at path, looking through a <value> wrapper if there is one."""
    if node is None:
        return ""
    el = node.find(path)
    if el is None:
        return ""
    inner = el.find("value")
    text = inner.text if inner is not None else el.text
    return (text or "").strip()


def _flag(node, path):
    return _val(node, path).lower() in ("1", "true")


def _num(text):
    try:
        return float(text.replace(",", ""))
    except (AttributeError, ValueError):
        return None


def parse_form4(text, accession, filed):
    """Code-P open-market purchases in one Form 4 submission (.txt with an
    embedded <XML> ownership document). Returns a list of purchase rows."""
    m = re.search(r"<XML>(.*?)</XML>", text, re.S | re.I)
    xml = (m.group(1) if m else text).strip()
    try:
        root = _strip_ns(ET.fromstring(xml))
    except ET.ParseError:
        return []
    if _val(root, "documentType") != "4":
        return []
    issuer = root.find("issuer")
    owner = root.find("reportingOwner")          # first listed = the insider
    if issuer is None or owner is None:
        return []
    rel = owner.find("reportingOwnerRelationship")
    roles = []
    if _flag(rel, "isDirector"):
        roles.append("Director")
    if _flag(rel, "isOfficer"):
        title = _val(rel, "officerTitle")
        roles.append(f"Officer ({title})" if title else "Officer")
    if _flag(rel, "isTenPercentOwner"):
        roles.append("10% owner")
    if _flag(rel, "isOther"):
        roles.append("Other")
    base = {
        "accession": accession, "filed": filed,
        "issuer_cik": _val(issuer, "issuerCik").lstrip("0"),
        "issuer_name": _val(issuer, "issuerName"),
        "ticker": _val(issuer, "issuerTradingSymbol").upper(),
        "owner_cik": _val(owner, "reportingOwnerId/rptOwnerCik").lstrip("0"),
        "owner_name": _val(owner, "reportingOwnerId/rptOwnerName"),
        "role": "; ".join(roles),
    }
    rows = []
    table = root.find("nonDerivativeTable")
    if table is None:
        return rows
    for i, t in enumerate(table.findall("nonDerivativeTransaction"), start=1):
        if _val(t, "transactionCoding/transactionCode") != "P":
            continue
        if _val(t, "transactionAmounts/transactionAcquiredDisposedCode") != "A":
            continue
        shares = _num(_val(t, "transactionAmounts/transactionShares"))
        price = _num(_val(t, "transactionAmounts/transactionPricePerShare"))
        if not shares or not price:
            continue                              # no price = not a market trade
        rows.append({**base, "line": i,
                     "txn_date": _val(t, "transactionDate")[:10],
                     "shares": shares, "price": price,
                     "value": round(shares * price, 2),
                     "direct": _val(t, "ownershipNature/directOrIndirectOwnership")})
    return rows


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------
def read_csv(path):
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_csv(path, fields, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)


def read_state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def merge_purchases(existing, new):
    """Union keyed by (accession, line). Existing rows are never changed."""
    keys = {(r["accession"], str(r["line"])) for r in existing}
    added = [r for r in new if (r["accession"], str(r["line"])) not in keys]
    merged = existing + added
    merged.sort(key=lambda r: (r["txn_date"], r["accession"], int(r["line"])))
    return merged, added


# ---------------------------------------------------------------------------
# Clusters
# ---------------------------------------------------------------------------
def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def find_clusters(purchases, as_of, days=WINDOW_DAYS, min_insiders=MIN_INSIDERS):
    start = (as_of - timedelta(days=days - 1)).isoformat()
    end = as_of.isoformat()
    by_issuer = {}
    for r in purchases:
        if not (start <= r["txn_date"] <= end):
            continue
        by_issuer.setdefault(r["issuer_cik"], []).append(r)
    out = []
    for cik, rows in by_issuer.items():
        owners = {r["owner_cik"] for r in rows}
        if len(owners) < min_insiders:
            continue
        roles = sorted({r["role"] or "n/a" for r in rows})
        out.append({
            "issuer_cik": cik,
            "issuer_name": rows[-1]["issuer_name"],
            "ticker": rows[-1]["ticker"],
            "insiders": len(owners),
            "purchases": len(rows),
            "shares": round(sum(_f(r["shares"]) for r in rows), 2),
            "value": round(sum(_f(r["value"]) for r in rows), 2),
            "first_buy": min(r["txn_date"] for r in rows),
            "last_buy": max(r["txn_date"] for r in rows),
            "roles": " | ".join(roles),
            "market_cap": "", "last_results": "", "last_results_form": "",
        })
    out.sort(key=lambda c: (-c["insiders"], -c["value"]))
    return out


def enrich(cluster, sec, http=None):
    """Market cap = Yahoo price x shares outstanding (SEC company facts);
    last results = newest 10-Q/10-K/20-F/40-F in SEC submissions. Blank
    when a source does not answer - a blank is a fact, a guess is not."""
    cik10 = cluster["issuer_cik"].zfill(10)
    shares = None
    try:
        st, text = sec.get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10}.json")
        if st == 200:
            facts = json.loads(text)["facts"]["dei"]["EntityCommonStockSharesOutstanding"]["units"]["shares"]
            shares = max(facts, key=lambda x: x.get("end", ""))["val"]
    except (KeyError, ValueError, TypeError):
        shares = None
    try:
        st, text = sec.get(f"https://data.sec.gov/submissions/CIK{cik10}.json")
        if st == 200:
            rec = json.loads(text)["filings"]["recent"]
            for form, day in zip(rec["form"], rec["filingDate"]):
                if form in RESULTS_FORMS:
                    cluster["last_results"], cluster["last_results_form"] = day, form
                    break
    except (KeyError, ValueError, TypeError):
        pass
    price = None
    if cluster["ticker"]:
        try:
            sym = cluster["ticker"].replace(".", "-")
            r = (http or requests).get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
                params={"range": "5d", "interval": "1d"}, headers=YAHOO_UA, timeout=TIMEOUT)
            if r.status_code == 200:
                price = float(r.json()["chart"]["result"][0]["meta"]["regularMarketPrice"])
        except Exception:
            price = None
    if shares and price:
        cluster["market_cap"] = round(shares * price)
    return cluster


def _money(v):
    x = _f(v)
    if not x:
        return "n/a"
    if x >= 1e9:
        return f"${x/1e9:,.2f}bn"
    if x >= 1e6:
        return f"${x/1e6:,.1f}m"
    return f"${x:,.0f}"


def render_block(clusters, as_of, days=WINDOW_DAYS, scanned=None):
    start = as_of - timedelta(days=days - 1)
    out = [
        "<!-- generated by form4.py - do not hand-edit -->",
        f"### Insider purchase clusters - {start.isoformat()} to {as_of.isoformat()}",
        "",
        f"Issuers where **{MIN_INSIDERS} or more insiders** bought in the open "
        f"market (Form 4 code P) within {days} days.",
        "",
    ]
    if not clusters:
        out += ["*No clusters in this window.*", ""]
    else:
        out += ["| Issuer | Insiders | Buys | Value | Market cap | Last results | Roles |",
                "|---|---|---|---|---|---|---|"]
        for c in clusters:
            name = f"{c['issuer_name']} ({c['ticker']})" if c["ticker"] else c["issuer_name"]
            res = (f"{c['last_results']} {c['last_results_form']}"
                   if c["last_results"] else "n/a")
            out.append(f"| {name} | {c['insiders']} | {c['purchases']} | "
                       f"{_money(c['value'])} | {_money(c['market_cap'])} | {res} | {c['roles']} |")
        out.append("")
    note = f"{scanned} Form 4 filings read this run. " if scanned is not None else ""
    out.append(f"*{note}Code P only; 4/A amendments skipped; joint filers count once. "
               f"A cluster is a lead for the terminal's insider-first rule, not a signal by itself.*")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
def days_to_process(state, today):
    last = state.get("last_index_day")
    start = (date.fromisoformat(last) + timedelta(days=1)) if last else today - timedelta(days=FIRST_RUN_DAYS)
    d, out = start, []
    while d <= today:
        out.append(d)
        d += timedelta(days=1)
    return out


def collect(sec, state, today, log=print):
    """Walk index days in order. A missing index more than two days old is a
    weekend or holiday and is passed; a missing recent one stops the walk so
    the next run picks it up once SEC has published it."""
    new_rows, last_done, scanned = [], state.get("last_index_day"), 0
    for day in days_to_process(state, today):
        st, text = sec.get(index_url(day))
        if st == 404:
            if (today - day).days > 2:
                last_done = day.isoformat()
                continue
            log(f"  - {day}: index not published yet; stopping here")
            break
        if st != 200:
            log(f"  ! {day}: index answered {st}; stopping here")
            break
        entries = parse_form_index(text)
        for e in entries:
            st2, doc = sec.get("https://www.sec.gov/Archives/" + e["path"])
            if st2 == 200:
                new_rows += parse_form4(doc, e["accession"], e["filed"])
                scanned += 1
        log(f"  - {day}: {len(entries)} Form 4 filings")
        last_done = day.isoformat()
    return new_rows, last_done, scanned


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    sec = SecClient(os.environ.get("SEC_USER_AGENT"))
    today = datetime.now(timezone.utc).date()
    state = read_state()
    try:
        new_rows, last_done, scanned = collect(sec, state, today)
    except SecRefused as e:
        print(f"Stopped: {e}", file=sys.stderr)
        return 1
    existing = read_csv(PURCHASES)
    merged, added = merge_purchases(existing, new_rows)
    as_of = date.fromisoformat(last_done) if last_done else today
    clusters = [enrich(c, sec) for c in find_clusters(merged, as_of)]
    block = render_block(clusters, as_of, scanned=scanned)
    print(f"{scanned} filings read, {len(added)} new purchases, "
          f"{len(clusters)} clusters, {sec.calls} SEC requests.")
    if args.dry_run:
        print(block)
        print("dry run - nothing written.")
        return 0
    write_csv(PURCHASES, PURCHASE_FIELDS, merged)
    write_csv(CLUSTERS, CLUSTER_FIELDS, clusters)
    BLOCK.write_text(block, encoding="utf-8")
    STATE.write_text(json.dumps({"last_index_day": last_done}, indent=2) + "\n", encoding="utf-8")
    return 0


# ---------------------------------------------------------------------------
# Self-test - offline
# ---------------------------------------------------------------------------
def _form4_xml(issuer_cik, ticker, owner_cik, owner, txns, doc="4", director=True,
               officer_title="", ten=False, ns=False):
    rows = ""
    for code, ad, shares, price, d in txns:
        rows += f"""
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>{d}</value></transactionDate>
      <transactionCoding><transactionFormType>4</transactionFormType><transactionCode>{code}</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>{shares}</value></transactionShares>
        <transactionPricePerShare><value>{price}</value><footnoteId id="F1"/></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>{ad}</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
      <ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature>
    </nonDerivativeTransaction>"""
    xmlns = ' xmlns="http://www.sec.gov/edgar/ownership"' if ns else ""
    return f"""<SEC-DOCUMENT>
<DOCUMENT><TYPE>4<TEXT>
<XML>
<?xml version="1.0"?>
<ownershipDocument{xmlns}>
  <schemaVersion>X0508</schemaVersion>
  <documentType>{doc}</documentType>
  <issuer><issuerCik>{issuer_cik.zfill(10)}</issuerCik><issuerName>Issuer {ticker}</issuerName><issuerTradingSymbol>{ticker.lower()}</issuerTradingSymbol></issuer>
  <reportingOwner>
    <reportingOwnerId><rptOwnerCik>{owner_cik.zfill(10)}</rptOwnerCik><rptOwnerName>{owner}</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship><isDirector>{'1' if director else '0'}</isDirector><isOfficer>{'true' if officer_title else 'false'}</isOfficer><officerTitle>{officer_title}</officerTitle><isTenPercentOwner>{'1' if ten else '0'}</isTenPercentOwner></reportingOwnerRelationship>
  </reportingOwner>
  <reportingOwner>
    <reportingOwnerId><rptOwnerCik>0000999999</rptOwnerCik><rptOwnerName>Joint Filer LLC</rptOwnerName></reportingOwnerId>
  </reportingOwner>
  <nonDerivativeTable>{rows}
  </nonDerivativeTable>
  <derivativeTable><derivativeTransaction><transactionCoding><transactionCode>P</transactionCode></transactionCoding></derivativeTransaction></derivativeTable>
</ownershipDocument>
</XML>
</TEXT></DOCUMENT></SEC-DOCUMENT>"""


class _R:
    def __init__(self, status, text="", payload=None):
        self.status_code, self.text, self._p = status, text, payload

    def json(self):
        return self._p


class _FakeHTTP:
    def __init__(self, docs, indexes, refuse=False):
        self.docs, self.indexes, self.refuse = docs, indexes, refuse
        self.seen_headers = []
        self.urls = []

    def get(self, url, headers=None, timeout=None, params=None):
        self.seen_headers.append(headers or {})
        self.urls.append(url)
        if self.refuse:
            return _R(403)
        if "daily-index" in url:
            key = url.rsplit("form.", 1)[1][:8]
            return _R(200, self.indexes[key]) if key in self.indexes else _R(404)
        if "/Archives/edgar/data/" in url:
            path = url.split("/Archives/", 1)[1]
            return _R(200, self.docs[path]) if path in self.docs else _R(404)
        if "companyfacts" in url:
            return _R(200, json.dumps({"facts": {"dei": {"EntityCommonStockSharesOutstanding": {
                "units": {"shares": [{"end": "2026-06-30", "val": 10_000_000},
                                     {"end": "2026-09-30", "val": 12_000_000}]}}}}}))
        if "submissions" in url:
            return _R(200, json.dumps({"filings": {"recent": {
                "form": ["4", "8-K", "10-Q", "10-K"],
                "filingDate": ["2026-10-29", "2026-10-28", "2026-10-27", "2026-03-01"]}}}))
        if "finance.yahoo.com" in url:
            return _R(200, payload={"chart": {"result": [{"meta": {"regularMarketPrice": 5.0}}]}})
        return _R(404)


def _index(day, entries):
    head = ("Description:           Daily Index of EDGAR Dissemination Feed by Form Type\n"
            "Last Data Received:    Oct 30, 2026\n\n"
            "Form Type   Company Name                                                  CIK         Date Filed  File Name\n"
            + "-" * 140 + "\n")
    lines = [f"{f:<12}{name:<62}{cik:<12}{day:<12}{path}" for f, name, cik, path in entries]
    return head + "\n".join(lines) + "\n"


def selftest():
    global DATA, PURCHASES, CLUSTERS, BLOCK, STATE
    import tempfile
    checks, problems = 0, []

    def check(label, cond):
        nonlocal checks
        checks += 1
        print(("  ok   " if cond else "  FAIL ") + label)
        if not cond:
            problems.append(label)

    print("Self-test: guard rails")
    try:
        SecClient("")
        check("no User-Agent stops the run before any request", False)
    except SystemExit:
        check("no User-Agent stops the run before any request", True)
    try:
        SecClient("Weekly-Brief bot")
        check("a User-Agent without contact e-mail is refused", False)
    except SystemExit:
        check("a User-Agent without contact e-mail is refused", True)
    t = [0.0]
    slept = []
    fake_clock = lambda: t[0]
    def fake_sleep(s):
        slept.append(s)
        t[0] += s
    c = SecClient("Test Bot test@example.com", http=_FakeHTTP({}, {}), sleep=fake_sleep, clock=fake_clock)
    for _ in range(5):
        c.get("https://www.sec.gov/Archives/edgar/daily-index/2026/QTR4/form.20261031.idx")
    check("rate limit: never two requests closer than 0.125 s",
          len(slept) == 4 and all(abs(s - MIN_INTERVAL) < 1e-9 for s in slept))
    check("the User-Agent comes from the secret, on every request",
          all(h.get("User-Agent") == "Test Bot test@example.com" for h in c.http.seen_headers))
    r = SecClient("Test Bot test@example.com", http=_FakeHTTP({}, {}, refuse=True), sleep=lambda s: None)
    try:
        r.get("https://www.sec.gov/x")
        check("403 from SEC is fatal", False)
    except SecRefused:
        check("403 from SEC is fatal", True)
    check("index URL uses the right quarter",
          index_url(date(2026, 10, 30)).endswith("/2026/QTR4/form.20261030.idx")
          and "/QTR1/" in index_url(date(2026, 3, 31)))

    print("Self-test: parsing")
    p_a1 = "edgar/data/111/0000111-26-000001.txt"
    p_a2 = "edgar/data/111/0000111-26-000002.txt"
    p_b1 = "edgar/data/222/0000222-26-000001.txt"
    p_c1 = "edgar/data/333/0000333-26-000001.txt"
    p_amend = "edgar/data/111/0000111-26-000003.txt"
    docs = {
        # issuer 111 (ACME): two insiders buy -> cluster
        p_a1: _form4_xml("111", "ACME", "5001", "Doe Jane", [
            ("P", "A", "1,000", "4.50", "2026-10-27"),
            ("S", "D", "500", "4.60", "2026-10-27"),        # sale: ignored
            ("P", "A", "500", "4.40", "2026-10-28")], officer_title="CFO", director=False),
        p_a2: _form4_xml("111", "ACME", "5002", "Roe Rick", [
            ("P", "A", "2000", "4.45", "2026-10-29"),
            ("M", "A", "3000", "1.00", "2026-10-29")], ns=True),   # option exercise: ignored
        # issuer 222: one insider, two buys -> not a cluster
        p_b1: _form4_xml("222", "BETA", "6001", "Solo Sam", [
            ("P", "A", "100", "10", "2026-10-28"), ("P", "A", "100", "11", "2026-10-29")]),
        # issuer 333: gift at zero price -> no purchase
        p_c1: _form4_xml("333", "GAMA", "7001", "Gift Gus", [("P", "A", "100", "0", "2026-10-28")]),
        p_amend: _form4_xml("111", "ACME", "5003", "Late Lou", [("P", "A", "1", "4", "2026-10-28")], doc="4/A"),
    }
    idx = {
        "20261029": _index("20261029", [
            ("4", "ACME CORP", "111", p_a1), ("4", "DOE JANE", "5001", p_a1),
            ("4", "ACME CORP", "111", p_a2), ("4/A", "ACME CORP", "111", p_amend),
            ("8-K", "ACME CORP", "111", "edgar/data/111/0000111-26-000009.txt")]),
        "20261030": _index("20261030", [
            ("4", "BETA INC", "222", p_b1), ("4", "GAMA LTD", "333", p_c1),
            ("SC 13D/A", "SOME FUND LP", "444", "edgar/data/444/0000444-26-000001.txt")]),
    }
    entries = parse_form_index(idx["20261029"])
    check("index: Form 4 only, each filing once (issuer + owner lines deduped)",
          [e["path"] for e in entries] == [p_a1, p_a2])
    check("index: 4/A, 8-K and multi-word form types are skipped",
          all(e["path"] not in (p_amend,) for e in entries)
          and parse_form_index(idx["20261030"])[0]["path"] == p_b1
          and len(parse_form_index(idx["20261030"])) == 2)
    check("index: filing date and accession parsed",
          entries[0]["filed"] == "2026-10-29" and entries[0]["accession"] == "0000111-26-000001")
    rows = parse_form4(docs[p_a1], "0000111-26-000001", "2026-10-29")
    check("form 4: only code P with A, sale ignored", [r["line"] for r in rows] == [1, 3])
    check("form 4: shares with thousands separator, price behind a footnote",
          rows[0]["shares"] == 1000 and rows[0]["price"] == 4.5 and rows[0]["value"] == 4500)
    check("form 4: CIKs without leading zeros, ticker upper case",
          rows[0]["issuer_cik"] == "111" and rows[0]["owner_cik"] == "5001" and rows[0]["ticker"] == "ACME")
    check("form 4: officer title in the role", rows[0]["role"] == "Officer (CFO)")
    check("form 4: joint filer ignored (first owner is the insider)",
          all(r["owner_cik"] == "5001" for r in rows))
    rows2 = parse_form4(docs[p_a2], "0000111-26-000002", "2026-10-29")
    check("form 4: namespaced XML parsed; option exercise (M) ignored",
          len(rows2) == 1 and rows2[0]["owner_cik"] == "5002" and rows2[0]["role"] == "Director")
    check("form 4: zero-price acquisition is not a purchase",
          parse_form4(docs[p_c1], "x", "2026-10-30") == [])
    check("form 4: 4/A document is skipped", parse_form4(docs[p_amend], "x", "2026-10-29") == [])
    check("form 4: derivative-table code P is ignored",
          len(parse_form4(docs[p_b1], "x", "2026-10-30")) == 2)
    check("form 4: garbage does not crash", parse_form4("<XML>not xml</XML>", "x", "2026-10-30") == [])

    print("Self-test: full run, incremental state, idempotence")
    tmp = Path(tempfile.mkdtemp())
    DATA, PURCHASES = tmp / "data", tmp / "data" / "insider-purchases.csv"
    CLUSTERS, BLOCK, STATE = DATA / "insider-clusters.csv", DATA / "insider-block.md", DATA / "form4-state.json"
    http = _FakeHTTP(docs, idx)
    sec = SecClient("Test Bot test@example.com", http=http, sleep=lambda s: None)
    today = date(2026, 10, 31)
    state = {"last_index_day": "2026-10-28"}
    logs = []
    new_rows, last_done, scanned = collect(sec, state, today, log=logs.append)
    check("walk: stops at the unpublished recent day, keeps the last good one",
          last_done == "2026-10-30" and any("not published" in l for l in logs))
    check("walk: four filings read, five purchases found", scanned == 4 and len(new_rows) == 5)
    old = days_to_process({"last_index_day": "2026-10-20"}, date(2026, 10, 31))
    http_gap = _FakeHTTP(docs, idx)
    sec_gap = SecClient("Test Bot test@example.com", http=http_gap, sleep=lambda s: None)
    _, last_gap, _ = collect(sec_gap, {"last_index_day": "2026-10-20"}, date(2026, 10, 31), log=lambda m: None)
    check("walk: old missing days (weekends, holidays) are passed",
          len(old) == 11 and last_gap == "2026-10-30")
    check("walk: the first run reaches back 14 days",
          days_to_process({}, today)[0] == today - timedelta(days=14))
    merged, added = merge_purchases([], new_rows)
    as_of = date.fromisoformat(last_done)
    clusters = find_clusters(merged, as_of)
    check("cluster: two insiders at ACME, one at BETA is not a cluster",
          [c["ticker"] for c in clusters] == ["ACME"] and clusters[0]["insiders"] == 2)
    check("cluster: purchases and value summed",
          clusters[0]["purchases"] == 3 and abs(clusters[0]["value"] - (4500 + 2200 + 8900)) < 0.01)
    check("cluster: buys outside the window drop out",
          find_clusters(merged, date(2026, 11, 20)) == [])
    c0 = enrich(dict(clusters[0]), sec, http=http)
    check("enrich: market cap = latest shares outstanding x price",
          c0["market_cap"] == 12_000_000 * 5)
    check("enrich: last results is the newest 10-Q/10-K, not the 8-K",
          c0["last_results"] == "2026-10-27" and c0["last_results_form"] == "10-Q")
    write_csv(PURCHASES, PURCHASE_FIELDS, merged)
    again, added2 = merge_purchases(read_csv(PURCHASES), new_rows)
    check("idempotent: the same filings add nothing on a rerun",
          added2 == [] and len(again) == len(merged))
    blk = render_block([c0], as_of, scanned=scanned)
    check("block: names the window", "2026-10-17 to 2026-10-30" in blk)
    check("block: cluster row with money formats",
          "| Issuer ACME (ACME) | 2 | 3 | $15,600 | $60.0m | 2026-10-27 10-Q |" in blk)
    check("block: empty window says so", "No clusters in this window" in render_block([], as_of))
    check("no request ever left without the secret User-Agent",
          all(h.get("User-Agent") == "Test Bot test@example.com"
              for h in http.seen_headers if h is not YAHOO_UA))

    print(f"\n{checks - len(problems)}/{checks} checks passed.")
    if problems:
        print("FAILED: " + "; ".join(problems), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
