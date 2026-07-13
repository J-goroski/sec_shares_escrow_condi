"""
bootstrap.py — one-time backfill CLI for a user-chosen date range.

Fetches every filing the SEC published between two dates via the daily index and
stores it in the SQLite database, with a full audit trail.  Run this once to
seed the database; afterwards use ``run.py`` to keep it current.

Examples
--------
    # Backfill a quarter (fast: 1 request per trading day)
    python filing_database/bootstrap.py --start 2025-04-01 --end 2025-06-30

    # Backfill and fully enrich each filing's header fields (slow)
    python filing_database/bootstrap.py --start 2025-06-01 --end 2025-06-05 --enrich

    # Custom database location
    python filing_database/bootstrap.py --start 2025-06-01 --end 2025-06-05 --db ./my.sqlite
"""

from __future__ import annotations

import os
import sys
import argparse
from datetime import date

# --- make the project root importable when run directly -----------------------
# `python filing_database/bootstrap.py` puts *this* folder on sys.path, not the
# project root, so `from methods...`/`from filing_database...` would fail.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from methods.sec_filings_sync import DEFAULT_DELAY          # noqa: E402
from filing_database.ingest import bootstrap                # noqa: E402
from filing_database.database import DEFAULT_DB_PATH, FilingDB  # noqa: E402


def _valid_date(s: str) -> str:
    try:
        date.fromisoformat(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an ISO date (YYYY-MM-DD): {s!r}")
    return s


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="bootstrap.py",
        description="Backfill SEC filings for a date range from the daily index.",
    )
    p.add_argument("--start", required=True, type=_valid_date,
                   help="First filing date to ingest (YYYY-MM-DD, inclusive).")
    p.add_argument("--end", required=True, type=_valid_date,
                   help="Last filing date to ingest (YYYY-MM-DD, inclusive).")
    p.add_argument("--db", default=DEFAULT_DB_PATH,
                   help=f"SQLite database path (default: {DEFAULT_DB_PATH}).")
    p.add_argument("--enrich", action="store_true",
                   help="Also read each filing's SGML header to fill report_date, "
                        "primary_document, act, file_number and XBRL fields "
                        "(one request per filing — slow).")
    p.add_argument("--no-tickers", action="store_true",
                   help="Skip company_tickers.json ticker resolution.")
    p.add_argument("--delay", type=float, default=DEFAULT_DELAY,
                   help=f"Seconds between requests (default {DEFAULT_DELAY}).")
    args = p.parse_args(argv)

    if args.start > args.end:
        p.error(f"--start ({args.start}) must be on or before --end ({args.end}).")

    result = bootstrap(
        args.start, args.end,
        db_path=args.db,
        delay=args.delay,
        enrich=args.enrich,
        resolve_tickers=not args.no_tickers,
    )

    print("\n--- bootstrap complete -----------------------------------------")
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
    # Non-zero exit if the run aborted, so schedulers/CI notice.
    return 0 if result.status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
