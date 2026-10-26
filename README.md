# Data engine - weekly series for the Saturday brief

One row per ISO week, appended to `data/series.csv` by GitHub Actions, plus a regenerated `data/brief-block.md` that is the market snapshot **ready to paste into section 3 of the brief**. No server, no Colab, no cost.

---

## v2.1 - what changed (October 2026)

| | v2.0 | v2.1 |
|---|---|---|
| Brent | one continuous future (`brent_usd`, Yahoo `BZ=F`) | **plus dated Brent** (`brent_dated_usd`), EIA's physical North Sea price via FRED, **with its observation date** (`brent_dated_date`) |
| Futures | a continuous price, contract unknown | **the contract behind it** (`brent_contract`, `gold_contract`, e.g. `Z26`). A roll prints as `roll Z26→F27`, never as a fake week-on-week change |
| Schema | fixed | **migrates itself**: new columns are appended at the end and earlier weeks are padded with blanks. Any other header change is refused |
| `sources_ok` on a repair | recounted data fields (~26) | **stays in source calls** (12-15), the unit every normal run uses |
| Schedule | Saturday 08:30 Bangkok on paper; it landed 13:00-14:40 (GitHub's scheduler runs late at :00 and :30) | **Saturday 05:17 Bangkok** (Friday 22:17 UTC), **backup 09:17**, both off the busy minutes. Rows are dated by the Bangkok calendar, so the series keeps its Saturday dates |
| Self-test | 46 checks | **99 checks** |

**The week it would have mattered.** On Fri 23 Oct 2026 the engine stored Brent at $111.70, +1.7% on the week. December Brent actually settled at $116.72, +6.2%: Yahoo's continuous series had moved to the January contract a week before December expired, and January traded 4.3% lower. v2.1 stores the contract next to the price, so that week prints `roll Z26→F27` instead of a change between two different contracts - and the dated line shows what a physical cargo cost.

### Upgrading from v2.0

1. **Replace `fetch.py`, `README.md` and `.github/workflows/weekly.yml`.** GitHub web or mobile: open the file → pencil (edit) → paste → Commit, or Add file → Upload files. **The workflow changes only its schedule and the week label on its data commits (now the Bangkok week), and there is no new secret.**
2. **Optional, recommended: one dry run.** Actions → Weekly series for the brief → Run workflow → tick **dry run**. The Self-test step should print `99/99 checks passed`, and the Fetch step prints `Dry run: series.csv would be migrated to the 32-column schema on a real run.` Nothing is written.
3. **The next real run migrates `data/series.csv` in place**, then appends the week as usual. The four new columns are added at the end, every earlier week gets blanks in them, and its existing cells are not touched. The migration is committed together with the week's row.

> If the Fetch step ever prints **`Schema check failed`**, the CSV header was edited by hand or reordered. Nothing was collected or written. Restore the original header from the git history and run again.

**This replaces the daily version.** Four things changed and one of them matters most.

| | Old | New |
|---|---|---|
| Schedule | daily, 09:15 Bangkok | **weekly, Saturday 05:17 Bangkok, backup 09:17** - before the brief |
| Output | `series.csv` only | `series.csv` **plus a paste-ready brief block with week-over-week deltas** |
| Safety | none | **`--selftest` runs offline before every live fetch; the job stops if the logic is broken** |
| Quote sources | Stooq only | **Two per field, tried in order, plus a sanity band that rejects out-of-range values** |
| New series | - | `ffr` + `fed_target_lo/hi` (NY Fed, keyless), `set_index` (Thai SET), `iso_week` |
| Secrets | one, load-bearing | **one, optional - no column depends on it** |

---

## Read this before you commit the schedule

**Weekly collection is a one-way door.** Daily rows can always be collapsed into weeks; weekly rows can never be expanded into days. You lose the ability to ever ask "when in the week did it cross" - retroactively, for good.

Two reasons that is acceptable here, and one way out:

1. Edition 3 already retired level-thresholds on drifting series in favour of direction-and-window. Weekly resolution matches the method the brief actually uses now.
2. The brief is consumed weekly. A daily series was producing 365 rows a year to answer 52 questions.
3. **If you ever want it back, change one character.** `cron: "17 22 * * 5"` → `cron: "17 22 * * *"` (and delete the backup line) and the same script runs daily, unchanged. It is idempotent per ISO week, so on a daily schedule it writes the first run of each week and no-ops the rest - or change the key to `date` if you want a true daily series.

Do this now if you are unsure. Storage is free and the decision is only reversible in one direction.

---

## What it costs

| Component | Cost | The actual limit |
|---|---|---|
| Actions on a **public** repo | **Free, unlimited minutes** | None. This is the one that matters |
| Actions on a **private** repo | Free tier | 2,000 min/month. This job takes ~40s → ~3 min/month |
| CoinGecko public API | Free, no key | ~10-30 calls/min. We make **2 calls/week** |
| Frankfurter (ECB rates) | Free, no key | None published. **1 call/week** |
| Yahoo chart API | Free, no key | Informal. **8 quote calls plus 3-7 contract lookups a week** - primary for quotes |
| Stooq CSV quotes | Free, no key | Fallback only. Zero calls in a week where Yahoo answers |
| NY Fed markets API | Free, no key | None published. **1 call/week** |
| FRED | Free; key optional | **1 keyless call/week** for dated Brent (public graph CSV). With the key: the API for the same series, plus 2 calls for the official yields |

**About 16-20 HTTP calls per week, all keyless unless the optional FRED key is set.** CoinGecko's `/simple/price` returns five assets in one call rather than five, and `/global` gives total cap and dominance rather than deriving them. The contract lookups stop at the first exact price match.

> GitHub disables scheduled workflows on repos with **no commit activity for 60 days**. This one commits weekly, so it keeps itself alive. If you pause it for two months, re-enable it in the Actions tab.

---

## Already uploaded the files? Three things left.

**You are here: repo created, `fetch.py` and the workflow pushed, no secrets set.** That is the intended state - no column depends on a secret. What remains:

**1 · Confirm the workflow path.** It must be **exactly** `.github/workflows/weekly.yml`, at the repository root. Not in a subfolder, not next to `fetch.py`. Actions looks nowhere else, and a workflow in the wrong place produces no error - it simply never runs. The repo itself can be named anything.

**2 · Turn on write permissions.** ⚠️ **This is the step that will otherwise fail your first run.** **Settings → Actions → General → Workflow permissions → "Read and write permissions" → Save.** New repositories often default to read-only. The job declares `permissions: contents: write`, but a repository-level read-only setting overrides it, and the `git push` at the end of the Commit step dies with a 403 after everything else has succeeded.

**3 · Run it once by hand.** **Actions → "Weekly series for the brief" → Run workflow.** Tick **dry run** the first time.

Then nothing. **The cron is already in the file - once it is on the default branch, the weekly schedule is live without any further action.** Saturdays 05:17 Bangkok (Friday 22:17 UTC), with a backup at 09:17 Bangkok that does nothing if the first run already recorded the week.

> **Why 05:17 and not 08:30.** GitHub runs scheduled workflows on a best-effort basis and delays them most at the top and half of the hour. The old `30 1 * * 6` (08:30 Bangkok) landed between 13:00 and 14:40 Bangkok in October 2026. An off-peak minute plus three hours of lead time keeps the data in place for a Saturday-morning brief, and Friday 22:17 UTC is already after the US equity and CME futures closes in both summer and winter time.

> Two conditions on the schedule: the workflow file must be on the **default branch** (a workflow sitting only on a feature branch never fires on cron), and Actions must be enabled for the repo.

---

## Setup

### 1. The repo

**Public is the better default** - unlimited Actions minutes, and `raw.githubusercontent.com` gives a direct URL to pull the CSV during a brief with no auth.

> **Public repo means public data. Market series only.** Never positions, holdings, order sizes or account values. The CSV as designed contains only prices anyone can look up, and that rule is written into the top of `fetch.py` so it survives you forgetting it. Anything position-shaped goes in a **separate private repo**.

### 2. The files

```
your-repo/
├── fetch.py
├── data/
│   ├── series.csv         ← created on first run, do not make it yourself
│   └── brief-block.md     ← regenerated every run
└── .github/
    └── workflows/
        └── weekly.yml     ← rename weekly.yml to this exact path
```

`.github/workflows/weekly.yml` must be that exact path. Actions looks nowhere else, and this is the single most common reason a workflow silently never runs. **Delete the old `daily.yml`** or you will collect twice and the ISO-week guard will make the daily runs look like silent no-ops.

Via the web UI: **Add file → Create new file**, paste the full path including folders into the filename box and GitHub creates the directories.

### 3. Secrets: one, and it is optional

**`FRED_API_KEY` is the only secret in the design, and no column depends on it.**

| Column | Keyless source | With the key |
|---|---|---|
| `ffr` (EFFR) | **New York Fed markets API** | unchanged |
| `fed_target_lo` / `fed_target_hi` | **New York Fed markets API** | unchanged |
| `us10y` / `us30y` | Yahoo, then Stooq | upgraded to FRED's official series |
| `brent_dated_usd` (v2.1) | **FRED's public graph CSV** (EIA series `DCOILBRENTEU`) | the FRED API, same series |
| everything else | CoinGecko · Frankfurter · Yahoo → Stooq | unchanged |

So the key buys exactly one thing: the two Treasury yields move from a scraped quote to the official series. Worth having, not worth blocking on.

> An unset secret resolves to an **empty string** in Actions, not an error. The script treats empty as absent, prints one line saying so, and does **not** log it as a source failure - so a missing key never turns the run red.

To add it: free key at `fred.stlouisfed.org/docs/api/api_key.html`, then **Settings → Secrets and variables → Actions → New repository secret**, named `FRED_API_KEY`.

**On EFFR specifically:** the NY Fed is the *primary* source, not a fallback - FRED's `DFF` series republishes it. Going direct removes the key from the critical path and adds the **FOMC target range**, which is what the brief's rate forecasts actually resolve against ("the FOMC raises the target range"). Those rows are now machine-resolvable rather than hand-checked.

### 4. First run

**Actions tab → Weekly series for the brief → Run workflow.** Tick **dry run** the first time: it fetches everything, prints the row, and writes nothing. That proves connectivity before anything is committed.

Read two things in the log.

**The Self-test step** should print `99/99 checks passed`. It runs entirely offline, before the fetch, so if it fails the problem is the code, not the network - and nothing was written.

**`sources_failed` in the Fetch output.** It should be empty or close to it. A name in there means that column is blank for the week, which is survivable - the run still succeeds. **If every quoted column fails at once while crypto and FX are fine, that is a source blocking the runner by IP, not a symbol problem** - see the troubleshooting table.

Then run it again **without** dry run. `data/series.csv` and `data/brief-block.md` appear, committed by `series-bot`.

> **Expected on the first real run: every delta in the block reads `-`.** There is one row, so there is nothing to compare against. Week two is when the file starts being useful.

> **A second run in the same ISO week collects nothing by design** - one row per week is the contract. It still **regenerates the block from the CSV**, so the two can never drift, but the numbers do not change.
>
> ⚠️ **To fix a week after repairing a source, tick `repair` on Run workflow.** It fills **only the blank fields** and leaves every value that is already there alone.
>
> **Why repair rather than replace, and it is not a preference.** A full replace re-reads sources that already worked, which does two unwanted things: it overwrites a good reading with a later one, and - the decisive one - **it can BLANK a good field if that source happens to be down at the moment of the repair.** Repair cannot make the row worse. Replace can. `force` still exists for a deliberate fresh snapshot, but `repair` is the right tool for a broken column.

⚠️ **Always start a run with "Run workflow" on the workflow page. Never "Re-run jobs" on a previous run** - a re-run replays the original commit, so it uses the tree *and the workflow file* as they were then. That produces two confusing symptoms at once: stale action versions in the log, and a push rejected because the branch has moved on since.

---

## If the first run fails

| Symptom | Cause | Fix |
|---|---|---|
| No workflow appears in the Actions tab | File is not at `.github/workflows/weekly.yml` | Move it. The path is exact |
| Cron never fires, manual run works | Workflow is not on the **default branch** | Merge it to the default branch |
| Everything green until **Commit**, then `403` | Repository workflow permissions are read-only | Settings → Actions → General → **Read and write permissions** |
| Commit succeeds, then **`! [rejected] main -> main (fetch first)`** | The remote moved after checkout. Usually because **"Re-run jobs" was used instead of "Run workflow"** - a re-run replays the *original commit*, so it checks out a stale tree and an old copy of the workflow file | Fixed: the Commit step now rebases and retries up to three times. **And always start runs with "Run workflow", never "Re-run jobs"** |
| Warning still names **v4/v5** after you updated the file | Same cause - a re-run uses the workflow file *as it was at that commit* | Start a fresh **Run workflow** |
| Commit step says `nothing to commit` | This ISO week is already recorded | Correct behaviour. The script is idempotent per week |
| **Self-test** fails | Code problem, not network | Nothing was written. Send me the log |
| Job fails after writing a row | A whole block (crypto or FX) was down | Row is written, run marked red so slow rot is visible. Check next week |
| Schedule stops after ~2 months | GitHub disables cron on repos with no commits for 60 days | Re-enable in the Actions tab. Weekly commits normally prevent this |
| `data/` folder never appears | **Dry run was ticked** - it writes nothing, by design | Run again with the box unticked |
| **Every quoted column `n/a`, crypto and FX fine** | A quote source is blocking the runner by IP. Happened to Stooq on 13 Sept 2026 - all eight symbols failed while every other source succeeded | Fixed: quotes now try **Yahoo first, Stooq second**. If both ever fail together, reorder `QUOTE_SOURCES` or add a third |
| A column is blank but its source looks up fine by hand | The value failed its **sanity band** and was rejected rather than stored | Check `SANE` in `fetch.py`. Rejection is deliberate - a wrong number is worse than a blank |
| Run is clean but **the table does not change** | This ISO week is already recorded, so nothing was collected | Re-run with **`repair` ticked** to fill the blank columns |
| Some columns filled, others still `n/a` after a repair | Those sources are genuinely down. Repair reports exactly which it filled and which stayed blank, and exits non-zero if any remain | Check the named sources. The filled columns are kept regardless |
| Yellow "Node.js 20 is deprecated" notice | Action majors still target Node 20 | Cosmetic, the job runs. Fixed in this version: **checkout@v5, setup-python@v6** |
| Fetch step: **`Schema check failed`** (v2.1) | The CSV header was edited by hand, reordered, or is from a newer version | Nothing was written. Restore the header from the git history and run again |
| `fred/DCOILBRENTEU` in `sources_failed` (v2.1) | FRED is down, or EIA's latest print is older than 16 days (publication delay, US holiday) | Dated Brent stays blank for the week; a `repair` run later in the week fills it once FRED updates |
| A contract label is blank (v2.1) | Yahoo's single-contract quotes did not answer | The price is unaffected; the delta prints without the roll check that week |

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
| Gold (GC=F, Z26) | $4,008 | -4.1% |
| Brent futures (BZ=F, F27) | $111.70 | roll Z26→F27 |
| Brent dated (EIA, obs 2026-10-20) | $115.22 | -9.0% |
| Dated minus BZ=F (days differ) | +$3.52 | - |
| Fed funds (EFFR) | 3.63% | flat |
| FOMC target range | 3.50-3.75% | - |
...

*Contract roll this week: BZ=F Z26→F27. The w/w change would compare two different contracts, so it is not printed.*

*Sources unavailable this run: stooq/cb.f*
*12 weeks on record.*
```

**What this is actually for.** Live spot levels can be pulled during the brief itself - that was never the gap. What cannot be pulled live is a **week-over-week delta against a stored row measured the same way**, or a series to chart later, or a fixed definition that makes a threshold mean something across weeks. That is the file's job. The levels are a convenience; the deltas and the consistency are the product.

Conventions in the block: a missing series reads **`n/a`**, never `0` - a blank and a zero are different facts. A move under a tenth of a point reads **`flat`**, because a column of `+0.0%` looks like movement. Failed sources are named **on the block itself**, so a sparse week is visible rather than silent.

v2.1 adds four: **`roll X→Y`** replaces the delta when the contract behind a future changed; **`(prev unlabeled)`** marks a change measured against a week stored before contract labels existed (v2.0 rows); **`(contract unknown)`** marks a week whose label could not be found; and **`same print`** means the dated-Brent observation did not change since last week (FRED posts EIA's prices weekly, with a lag). **`Dated minus BZ=F`** is the premium of a physical cargo over the future, flagged **`(days differ)`** when the two prints are from different days, which is usual.

---

## The stablecoin column, and a watch item it closes

`stablecoin_proxy_mcap` is **USDT + USDC only** - roughly 85-90% of total stablecoin float, not the aggregate, and named `proxy` so it can never be mistaken for one.

That is deliberate. Edition 3 logged that the stablecoin float "needs a single fixed source before it can carry a threshold again", after a comparison between a named-majors sum and an aggregate produced a phantom contraction. **A proxy with a fixed definition, collected the same way every week, is exactly what a threshold needs** - the level matters less than the fact that consecutive readings are the same measurement. That watch item can close once there are a few rows.

---

## Failure behaviour

- **One source down** → that column is blank, the name lands in `sources_failed` and on the brief block, the run succeeds.
- **A whole block down** (all CoinGecko, or all FX) → row still written, **run marked failed** so it appears red in the Actions tab.
- **Both core blocks down** → nothing written, run fails. A row of blanks is worse than no row.
- **Self-test fails** → the job stops before fetching. Broken logic never reaches the file that is the record.

Slow rot is the real risk with an unattended collector: a source changes its schema, the column quietly goes empty, and nobody notices for four months. Naming failures in the committed output is what makes that visible without anyone checking the Actions tab.

---

## Commands

```bash
python fetch.py              # collect, append, regenerate the block
python fetch.py --repair     # fill ONLY the blank fields in this week's row
python fetch.py --force      # replace this week's row entirely (fresh snapshot)
python fetch.py --dry-run    # fetch and print, write nothing
python fetch.py --selftest   # 99 checks, offline, no network
```

The self-test covers parsing, the EUR/USD inversion, the stablecoin sum, FRED overwriting Stooq yields when the key is present, **a full collection run with no key at all** (EFFR and the target range survive, yields fall back to Stooq, nothing goes blank, and the absent key is not logged as a failure), partial-failure handling, blank-not-zero, ISO-week stamping, schema alignment across appends, idempotence, delta computation up and down, the target-range render, and the formatting edges. It deliberately fails one source so the partial-failure path is exercised rather than assumed, and the Stooq and FRED yield fixtures carry **different values** so the fallback checks actually discriminate.

v2.1 adds: contract identification by price match and the short-name fallback; a roll across two weeks rendering as `roll Z26→F27`; dated Brent through the API and the keyless CSV (both header formats), with stale prints rejected and no orphan dates; the spread line; schema migration (padding, byte-identical old cells, idempotence, and refusal of reordered, longer or ragged files); repair filling a value together with its label and keeping `sources_ok` in source calls; rendering of weeks stored before labels existed; and the Bangkok run clock (a Friday-night UTC run is filed under the Bangkok Saturday, in the same ISO week).

## Adding a series

Append to the **end** of `FIELDS`, add the fetch, add a line to `render_block`, and add a self-test check. Never insert into the middle of `FIELDS` - historic rows misalign and the CSV is the record, not a cache. Since v2.1 the existing CSV migrates itself on the next run: an old header that is a prefix of `FIELDS` is extended, and anything else is refused. If the new column is metadata about another value (a date, a label), add it to `META_FIELDS` and `COMPANIONS` so a repair moves it together with its value.
