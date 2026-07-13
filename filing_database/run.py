"""
run.py — incremental "keep the database current" CLI.

Reads the database watermark (the newest daily index date already ingested),
resumes at the next day, and catches up to today — pulling only the dates the
SEC daily-index sitemap actually lists, and retrying any dates that failed on a
previous run.  Safe to run on a schedule (cron / Task Scheduler).

Examples
--------
    # Catch up to today from wherever the DB left off
    python filing_database/run.py

    # Catch up only through a specific date
    python filing_database/run.py --end 2025-06-30

    # Seed the incremental runner on an empty DB (skips needing a bootstrap)
    python filing_database/run.py --start 2025-06-01
"""

from __future__ import annotations

import os
import sys
import argparse
from datetime import date

# --- make the project root importable when run directly -----------------------
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from methods.sec_filings_sync import DEFAULT_DELAY          # noqa: E402
from filing_database.ingest import run_incremental          # noqa: E402
from filing_database.database import DEFAULT_DB_PATH, FilingDB  # noqa: E402


def _valid_date(s: str) -> str:
    try:
        date.fromisoformat(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an ISO date (YYYY-MM-DD): {s!r}")
    return s


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="run.py",
        description="Incrementally ingest new SEC filings since the DB watermark.",
    )
    p.add_argument("--db", default=DEFAULT_DB_PATH,
                   help=f"SQLite database path (default: {DEFAULT_DB_PATH}).")
    p.add_argument("--end", type=_valid_date, default=None,
                   help="Ingest up to this date (YYYY-MM-DD, inclusive). "
                        "Default: today.")
    p.add_argument("--start", type=_valid_date, default=None,
                   help="Override the resume date (YYYY-MM-DD). Required only when "
                        "the database has no watermark yet (empty DB).")
    p.add_argument("--enrich", action="store_true",
                   help="Also read each filing's SGML header (slow; see bootstrap).")
    p.add_argument("--no-tickers", action="store_true",
                   help="Skip company_tickers.json ticker resolution.")
    p.add_argument("--delay", type=float, default=DEFAULT_DELAY,
                   help=f"Seconds between requests (default {DEFAULT_DELAY}).")
    args = p.parse_args(argv)

    try:
        result = run_incremental(
            db_path=args.db,
            end_date=args.end,
            start_override=args.start,
            delay=args.delay,
            enrich=args.enrich,
            resolve_tickers=not args.no_tickers,
        )
    except RuntimeError as exc:
        p.error(str(exc))
        return 2

    print("\n--- incremental run complete -----------------------------------")
    print(f"  run id            {result.run_id}  ({result.status})")
    print(f"  dates ingested    {result.dates_parsed}  (failed: {result.dates_failed})")
    print(f"  filings inserted  {result.filings_inserted}")
    print(f"  filings refreshed {result.filings_updated}")
    print(f"  watermark now     {result.watermark}")
    with FilingDB(args.db) as db:
        s = db.summary()
    print(f"  DB totals         {s['filings']} filings, "
          f"{s['idx_parsed']} days parsed, {s['idx_failed']} days failed, "
          f"{s['idx_pending']} pending")
    print("----------------------------------------------------------------")
    return 0 if result.status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
