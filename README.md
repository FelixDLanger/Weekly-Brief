# Weekly-Brief-
# Data engine - weekly series for the Saturday brief

One row per ISO week, appended to `data/series.csv` by GitHub Actions, plus a regenerated `data/brief-block.md` that is the market snapshot **ready to paste into section 3 of the brief**. No server, no Colab, no cost.

**This replaces the daily version.** Four things changed and one of them matters most.

| | Old | New |
|---|---|---|
| Schedule | daily, 09:15 Bangkok | **weekly, Saturday 08:30 Bangkok** - before the brief |
| Output | `series.csv` only | `series.csv` **plus a paste-ready brief block with week-over-week deltas** |
| Safety | none | **`--selftest` runs offline before every live fetch; the job stops if the logic is broken** |
| New series | - | `ffr` + `fed_target_lo/hi` (NY Fed, keyless), `set_index` (Thai SET), `iso_week` |
| Secrets | one, load-bearing | **one, optional - no column depends on it** |

---

## Read this before you commit the schedule

**Weekly collection is a one-way door.** Daily rows can always be collapsed into weeks; weekly rows can never be expanded into days. You lose the ability to ever ask "when in the week did it cross" - retroactively, for good.

Two reasons that is acceptable here, and one way out:

1. Edition 3 already retired level-thresholds on drifting series in favour of direction-and-window. Weekly resolution matches the method the brief actually uses now.
2. The brief is consumed weekly. A daily series was producing 365 rows a year to answer 52 questions.
3. **If you ever want it back, change one character.** `cron: "30 1 * * 6"` â `cron: "30 1 * * *"` and the same script runs daily, unchanged. It is idempotent per ISO week, so on a daily schedule it writes the first run of each week and no-ops the rest - or change the key to `date` if you want a true daily series.

Do this now if you are unsure. Storage is free and the decision is only reversible in one direction.

---

## What it costs

| Component | Cost | The actual limit |
|---|---|---|
| Actions on a **public** repo | **Free, unlimited minutes** | None. This is the one that matters |
| Actions on a **private** repo | Free tier | 2,000 min/month. This job takes ~40s â ~3 min/month |
| CoinGecko public API | Free, no key | ~10-30 calls/min. We make **2 calls/week** |
| Frankfurter (ECB rates) | Free, no key | None published. **1 call/week** |
| Stooq CSV quotes | Free, no key | Informal. **8 small calls/week** |
| NY Fed markets API | Free, no key | None published. **1 call/week** |
| FRED | Free, optional key | 120 req/min. **2 calls/week**, skipped if no key |

**13 HTTP calls per week, 11 of them keyless.** CoinGecko's `/simple/price` returns five assets in one call rather than five, and `/global` gives total cap and dominance rather than deriving them.

> GitHub disables scheduled workflows on repos with **no commit activity for 60 days**. This one commits weekly, so it keeps itself alive. If you pause it for two months, re-enable it in the Actions tab.

---

## Setup

### 1. The repo

**Public is the better default** - unlimited Actions minutes, and `raw.githubusercontent.com` gives a direct URL to pull the CSV during a brief with no auth.

> **Public repo means public data. Market series only.** Never positions, holdings, order sizes or account values. The CSV as designed contains only prices anyone can look up, and that rule is written into the top of `fetch.py` so it survives you forgetting it. Anything position-shaped goes in a **separate private repo**.

### 2. The files

```
your-repo/
âââ fetch.py
âââ data/
â   âââ series.csv         â created on first run, do not make it yourself
â   âââ brief-block.md     â regenerated every run
âââ .github/
    âââ workflows/
        âââ weekly.yml     â rename weekly.yml to this exact path
```

`.github/workflows/weekly.yml` must be that exact path. Actions looks nowhere else, and this is the single most common reason a workflow silently never runs. **Delete the old `daily.yml`** or you will collect twice and the ISO-week guard will make the daily runs look like silent no-ops.

Via the web UI: **Add file â Create new file**, paste the full path including folders into the filename box and GitHub creates the directories.

### 3. Secrets: one, and it is optional

**`FRED_API_KEY` is the only secret in the design, and no column depends on it.**

| Column | Keyless source | With the key |
|---|---|---|
| `ffr` (EFFR) | **New York Fed markets API** | unchanged |
| `fed_target_lo` / `fed_target_hi` | **New York Fed markets API** | unchanged |
| `us10y` / `us30y` | Stooq | upgraded to FRED's official series |
| everything else | CoinGecko Â· Frankfurter Â· Stooq | unchanged |

So the key buys exactly one thing: the two Treasury yields move from a scraped quote to the official series. Worth having, not worth blocking on.

> An unset secret resolves to an **empty string** in Actions, not an error. The script treats empty as absent, prints one line saying so, and does **not** log it as a source failure - so a missing key never turns the run red.

To add it: free key at `fred.stlouisfed.org/docs/api/api_key.html`, then **Settings â Secrets and variables â Actions â New repository secret**, named `FRED_API_KEY`.

**On EFFR specifically:** the NY Fed is the *primary* source, not a fallback - FRED's `DFF` series republishes it. Going direct removes the key from the critical path and adds the **FOMC target range**, which is what the brief's rate forecasts actually resolve against ("the FOMC raises the target range"). Those rows are now machine-resolvable rather than hand-checked.

### 4. First run

**Actions tab â Weekly series for the brief â Run workflow.** Tick **dry run** the first time: it fetches everything, prints the row, and writes nothing. That proves connectivity before anything is committed.

Then check one thing in the output: **`sources_failed`**. `^set` (the Thai SET index) is the one symbol I could not verify against Stooq from here, because outbound HTTP to market APIs is blocked in the sandbox this was written in. If it appears in `sources_failed`, try `set.th` or drop the column - everything else is unchanged by it.

Run it again without dry run and the first row lands.

---

## What it produces

`data/brief-block.md`, regenerated in full every week:

```
### Market snapshot - 2026-09-19 (week 2026-W38)

| Series | Level | w/w |
|---|---|---|
| BTC | $81,000 | +4.8% |
| SOL | $95.00 | -6.9% |
| USD/THB | 32.410 | flat |
| Fed funds (EFFR) | 3.63% | flat |
| FOMC target range | 3.50-3.75% | - |
...

*Sources unavailable this run: stooq/cb.f*
*12 weeks on record.*
```

**What this is actually for.** Live spot levels can be pulled during the brief itself - that was never the gap. What cannot be pulled live is a **week-over-week delta against a stored row measured the same way**, or a series to chart later, or a fixed definition that makes a threshold mean something across weeks. That is the file's job. The levels are a convenience; the deltas and the consistency are the product.

Conventions in the block: a missing series reads **`n/a`**, never `0` - a blank and a zero are different facts. A move under a tenth of a point reads **`flat`**, because a column of `+0.0%` looks like movement. Failed sources are named **on the block itself**, so a sparse week is visible rather than silent.

---

## The stablecoin column, and a watch item it closes

`stablecoin_proxy_mcap` is **USDT + USDC only** - roughly 85-90% of total stablecoin float, not the aggregate, and named `proxy` so it can never be mistaken for one.

That is deliberate. Edition 3 logged that the stablecoin float "needs a single fixed source before it can carry a threshold again", after a comparison between a named-majors sum and an aggregate produced a phantom contraction. **A proxy with a fixed definition, collected the same way every week, is exactly what a threshold needs** - the level matters less than the fact that consecutive readings are the same measurement. That watch item can close once there are a few rows.

---

## Failure behaviour

- **One source down** â that column is blank, the name lands in `sources_failed` and on the brief block, the run succeeds.
- **A whole block down** (all CoinGecko, or all FX) â row still written, **run marked failed** so it appears red in the Actions tab.
- **Both core blocks down** â nothing written, run fails. A row of blanks is worse than no row.
- **Self-test fails** â the job stops before fetching. Broken logic never reaches the file that is the record.

Slow rot is the real risk with an unattended collector: a source changes its schema, the column quietly goes empty, and nobody notices for four months. Naming failures in the committed output is what makes that visible without anyone checking the Actions tab.

---

## Commands

```bash
python fetch.py              # collect, append, regenerate the block
python fetch.py --dry-run    # fetch and print, write nothing
python fetch.py --selftest   # 30 checks, offline, no network
```

The self-test covers parsing, the EUR/USD inversion, the stablecoin sum, FRED overwriting Stooq yields when the key is present, **a full collection run with no key at all** (EFFR and the target range survive, yields fall back to Stooq, nothing goes blank, and the absent key is not logged as a failure), partial-failure handling, blank-not-zero, ISO-week stamping, schema alignment across appends, idempotence, delta computation up and down, the target-range render, and the formatting edges. It deliberately fails one source so the partial-failure path is exercised rather than assumed, and the Stooq and FRED yield fixtures carry **different values** so the fallback checks actually discriminate.

## Adding a series

Append to the **end** of `FIELDS`, add the fetch, add a line to `render_block`, and add a self-test check. Never insert into the middle of `FIELDS` - historic rows misalign and the CSV is the record, not a cache.
