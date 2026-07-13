# filing_database — daily-index ingestion into SQLite

Ingests **every SEC EDGAR filing** into an auditable SQLite database by walking
the firm-wide **daily index** feed, and keeps it current incrementally.

This is the date-driven counterpart to [`methods/sec_filings_sync.py`](../methods/sec_filings_sync.py):

| | source | grain | use |
|---|---|---|---|
| `methods/sec_filings_sync.py` | submissions API, **per CIK** | filings for the companies you name | "give me Apple's 10-Ks" |
| `filing_database/` (this package) | **daily index**, per date | *every* filing filed on each day | "mirror all filings from date A to date B, then stay current" |

Both produce the **same `FilingRecord` fields**. This package imports the locked
sync module's `FilingRecord`, its rate-limited fetcher (`_get_with_retry`, the
10 req/s cap), the `UA` header (your email), and the URL builders — so access
policy and record shape live in exactly one place.

---

## Quick start

```bash
# 1) One-time backfill of a date range you choose (fast: ~1 request per day)
python filing_database/bootstrap.py --start 2025-06-01 --end 2025-06-30

# 2) Keep it current — resumes from the DB watermark, catches up to today.
#    Safe to run on a schedule (Task Scheduler / cron).
python filing_database/run.py

# 3) See what's in the database (watermark, run log, failed dates, counts)
python filing_database/status.py

# 4) (Optional) fill the header-only fields for filings you care about
python filing_database/enrich.py --forms 10-K,10-Q,20-F
```

The database is created at `filing_database/edgar_filings.sqlite` by default
(`--db` to override on any command).

---

## The four commands

### `bootstrap.py` — pick a date range
```
python filing_database/bootstrap.py --start YYYY-MM-DD --end YYYY-MM-DD [--enrich] [--no-tickers] [--db PATH] [--delay S]
```
Backfills the inclusive `[start, end]` range. Idempotent — re-running a range
that is already ingested does nothing.

### `run.py` — incremental catch-up
```
python filing_database/run.py [--end YYYY-MM-DD] [--start YYYY-MM-DD] [--enrich] [--no-tickers] [--db PATH]
```
Reads the **watermark** (newest daily-index date already parsed), resumes at the
next day, and ingests up to `--end` (default: today). It also **retries any
dates that failed** on a previous run. On an empty database it asks you to
bootstrap first (or pass `--start` to seed it).

### `status.py` — read-only audit view
```
python filing_database/status.py [--runs N] [--db PATH]
```
Prints the watermark, filing/enrichment counts, any failed/outstanding dates,
and the recent run log. Safe to run mid-ingest (WAL mode).

### `enrich.py` — backfill header fields (optional, slow)
```
python filing_database/enrich.py [--forms 10-K,10-Q] [--since D] [--until D] [--limit N] [--db PATH]
```
See *Enrichment* below. One request per filing — always scope it.

You can also drive everything from Python / a notebook:
```python
from filing_database import bootstrap, run_incremental, enrich_backfill, FilingDB

bootstrap("2025-06-01", "2025-06-30")
run_incremental()
enrich_backfill(forms=["10-K"], limit=500)
```

---

## What gets stored (and what needs enrichment)

The daily `master.YYYYMMDD.idx` feed carries five columns
(`CIK | Company Name | Form Type | Date Filed | File Name`). From those we
populate, with **full fidelity**, and using the same URL construction as the
CIK-based path:

* `cik`, `entity_name`, `form_type`, `filing_date`, `accession_number`
* `index_url`, `filing_detail_url`, `submission_txt_url`
* `ticker` — resolved from `company_tickers.json` (one bulk file per run; blank
  for filers with no ticker, e.g. individuals filing Form 4, funds, many FPIs)

The remaining `FilingRecord` fields are **not in the daily feed** and are left
`NULL`/empty until you run enrichment:

* `report_date`, `primary_document`, `filing_url`, `act`, `file_number`,
  `is_xbrl`, `xbrl_instance_url`

### Enrichment
`enrich.py` (or `enrich_backfill(...)`) reads each filing's SGML `<SEC-HEADER>`
(a small HTTP *range* read, not the whole document) and fills those fields —
the same header block `methods/country_assignment` parses. It is **one request
per filing**, so it is opt-in and should be scoped with `--forms` / `--since` /
`--until` / `--limit`. A filing is marked `enriched=1` only once the header
actually yields a primary document, so transient failures simply leave it queued
for the next pass. Because the master fields use empty-string sentinels, a plain
re-ingest never overwrites an enriched value (`COALESCE(NULLIF(...))`).

---

## Data model & grain

The daily index lists a filing **once per associated CIK**: a Form 4 appears
under both the issuer and the reporting owner; a corporate family's 8-K appears
under each entity. That is the same grain the CIK-based sync produces, so the
natural key is the **`(accession_number, cik)` pair**, not accession alone. All
writes are UPSERTs on that key, so ingestion is fully idempotent.

### Tables

| table | purpose |
|---|---|
| `filings` | one row per `(filing, filer CIK)` — the `FilingRecord` fields + provenance |
| `idx_files` | **watermark ledger**: one row per daily `master.*.idx` file, with `status` (`available`/`parsed`/`empty`/`failed`), `filing_count`, `attempts`, sitemap `last_modified`/`size_label` |
| `ingest_runs` | one row per execution (`bootstrap`/`incremental`/`enrich`): timestamps, status, counts, and the exact `user_agent` used |
| `meta` | schema version + creation time |

Every `filings` row also records **provenance**: `source_idx_date`,
`source_idx_file` (the exact `master.YYYYMMDD.idx` it came from), `first_seen_run`,
`last_seen_run`, `ingested_at`, `updated_at`, and `enriched`.

The **watermark** is `MAX(idx_date) WHERE status IN ('parsed','empty')` — keyed
on the *index date*, deliberately **not** `filing_date`, because EDGAR
re-disseminates old filings (e.g. draft registration statements, `DRS`) on later
dates, which would otherwise corrupt a filing-date watermark.

### Example queries
```sql
-- All 10-Ks ingested in a window
SELECT cik, entity_name, filing_date, submission_txt_url
FROM filings WHERE form_type='10-K' AND filing_date BETWEEN '2025-06-01' AND '2025-06-30';

-- Which master file did a filing come from, and when did we ingest it?
SELECT accession_number, cik, source_idx_file, ingested_at, first_seen_run
FROM filings WHERE accession_number='0000320193-25-000001';

-- Ingestion audit trail
SELECT * FROM ingest_runs ORDER BY run_id DESC;
SELECT idx_date, status, filing_count, attempts FROM idx_files ORDER BY idx_date;
```

---

## Fail-safes (why runs don't waste requests or lose data)

* **Sitemap-driven, never blind "+1 day".** Before fetching, the runner reads
  each quarter's `index.json` directory listing and only requests
  `master.YYYYMMDD.idx` files the SEC actually publishes. Weekends, holidays and
  not-yet-published days are simply skipped — not failed.
* **Transport retries.** The locked fetcher retries `429`/`5xx` *responses*; this
  package adds a wrapper that also retries dropped/stalled connections
  (`Timeout`/`ConnectionError`) with exponential back-off — the gap that
  otherwise turns a transient network blip into a crash.
* **Per-date isolation + backup runs.** A failure on one date marks just that
  date `failed` in the ledger and moves on; the next `run.py` automatically
  retries every outstanding date, even ones now older than the watermark.
* **Hard stop on an IP ban.** A persistent `429` (`SECBlockedError`) aborts the
  run cleanly, leaving all un-processed dates queued. The enrichment backfill has
  its own circuit breaker (stops after many consecutive failures).
* **Idempotent & non-destructive.** Re-running any range never duplicates rows,
  and a plain re-ingest never clobbers ticker/enriched values.
* **Identifies itself.** Every request sends the `UA` header
  (`example@email.com` — change it to your own), and each run records that UA in `ingest_runs` for
  audit.

---

## Operational notes

* **Volume.** A trading day is ~4,000–7,000 filings. A month bootstraps in a
  minute or two (one request per day + one ticker file). Enrichment is the only
  expensive operation (one request per filing) — scope it.
* **Scheduling.** `run.py` is designed for Task Scheduler / cron. It exits
  non-zero if a run ends in a `failed` state, so a scheduler can alert; the next
  run picks up wherever it left off.
* **Files.** SQLite in WAL mode creates `-wal`/`-shm` sidecar files next to the
  database; that is normal.
* **Dependencies.** Only `requests` (already in the project `requirements.txt`).

## Layout
```
filing_database/
  daily_index.py   # SEC daily-index feed -> FilingRecord (discovery, parse, enrich)
  database.py      # SQLite schema + data-access object (FilingDB)
  ingest.py        # orchestration: bootstrap / run_incremental / enrich_backfill
  bootstrap.py     # CLI: backfill a chosen date range
  run.py           # CLI: incremental catch-up from the watermark
  enrich.py        # CLI: backfill header fields
  status.py        # CLI: read-only audit view
```
