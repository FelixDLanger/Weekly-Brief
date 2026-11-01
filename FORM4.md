# Form 4 cluster feed

A second weekly job next to the market series. It reads SEC's daily form index, opens every Form 4, keeps **open-market purchases (transaction code P)**, and flags issuers where **two or more insiders bought within 14 days**. Each cluster gets its market cap (price x shares outstanding) and the date of its last 10-Q/10-K/20-F/40-F.

| File | What it is |
|---|---|
| `data/insider-block.md` | Paste-ready table for the brief and the terminal's "insider filings first" rule |
| `data/insider-clusters.csv` | This window's clusters |
| `data/insider-purchases.csv` | Every code-P purchase seen, one row per transaction (the base-rate record for the AEM S4 study) |
| `data/form4-state.json` | The last EDGAR index day processed |

## One-time setup: the SEC secret

SEC's fair-access policy requires automated requests to identify themselves. Add a repository secret:

**Settings → Secrets and variables → Actions → New repository secret**, name `SEC_USER_AGENT`, value like `Weekly-Brief yourname@example.com`.

It lives only in the secret. It is never in the code, never printed, and the script stops before making any request if it is missing or has no e-mail address.

Then: **Actions → Weekly insider clusters (Form 4) → Run workflow → tick dry run.** The Self-test step should print `33/33 checks passed`. The first real run reaches back 14 days.

## Rules built in

- At most 8 requests a second (SEC's limit is 10). Form 4 volume is roughly 1,000 a business day, so a weekly run reads about 5,000 filings in 10-15 minutes; the first run about twice that.
- Code P with "acquired" only. Sales, option exercises (M), gifts at zero price and the derivative table are ignored.
- 4/A amendments are skipped (they restate earlier filings). Joint filers count once: the first reporting owner listed.
- An index day is processed once. A missing day older than two days is a weekend or holiday; a missing recent day stops the walk and is retried next run.
- A 403 from SEC stops the run and writes nothing.
- Blank beats wrong: market cap or results date stay blank if a source does not answer.

```bash
python form4.py --selftest   # 33 checks, offline
python form4.py --dry-run    # needs SEC_USER_AGENT; writes nothing
```

A cluster is a lead, not a signal. The terminal still applies its gates (filings, 10x arithmetic, V7) before anything is scored.
