"""
enrich.py — backfill the header-only fields for already-ingested filings.

``bootstrap.py`` / ``run.py`` store the fields the daily master index provides
and leave report_date, primary_document, filing_url, act, file_number, is_xbrl
and xbrl_instance_url empty (those live in each filing's SGML header).  This tool
fills them in, one request per filing, so you can ingest broadly first and enrich
selectively later.

Scope every run — enriching an entire multi-day backfill is a lot of requests.

Examples
--------
    # Enrich just the 10-K / 10-Q / 20-F filings already in the DB
    python filing_database/enrich.py --forms 10-K,10-Q,20-F

    # Enrich the first 500 un-enriched filings filed in June 2025
    python filing_database/enrich.py --since 2025-06-01 --until 2025-06-30 --limit 500
"""

from __future__ import annotations

import os
import sys
import argparse
from datetime import date

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from methods.sec_filings_sync import DEFAULT_DELAY          # noqa: E402
from filing_database.ingest import enrich_backfill          # noqa: E402
from filing_database.database import DEFAULT_DB_PATH, FilingDB  # noqa: E402


def _valid_date(s: str) -> str:
    try:
        date.fromisoformat(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an ISO date (YYYY-MM-DD): {s!r}")
    return s


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="enrich.py",
        description="Backfill SGML-header fields for already-ingested filings.",
    )
    p.add_argument("--db", default=DEFAULT_DB_PATH,
                   help=f"SQLite database path (default: {DEFAULT_DB_PATH}).")
    p.add_argument("--forms", default=None,
                   help="Comma-separated form types to enrich (e.g. '10-K,10-Q'). "
                        "Default: all un-enriched filings.")
    p.add_argument("--since", type=_valid_date, default=None,
                   help="Only filings with filing_date >= this (YYYY-MM-DD).")
    p.add_argument("--until", type=_valid_date, default=None,
                   help="Only filings with filing_date <= this (YYYY-MM-DD).")
    p.add_argument("--limit", type=int, default=None,
                   help="Cap the number of filings enriched this run.")
    p.add_argument("--delay", type=float, default=DEFAULT_DELAY,
                   help=f"Seconds between requests (default {DEFAULT_DELAY}).")
    args = p.parse_args(argv)

    forms = [f.strip() for f in args.forms.split(",")] if args.forms else None

    if not os.path.exists(args.db):
        p.error(f"No database at {args.db}. Run bootstrap.py first.")

    with FilingDB(args.db) as db:
        pending = db.count_unenriched(forms=forms, since=args.since, until=args.until)
    print(f"{pending} filing(s) match and are not yet enriched"
          + (f"; enriching up to {args.limit}." if args.limit else "."))

    result = enrich_backfill(
        db_path=args.db, limit=args.limit, forms=forms,
        since=args.since, until=args.until, delay=args.delay,
    )
    print("\n--- enrichment complete ----------------------------------------")
    print(f"  run id            {result.run_id}  ({result.status})")
    print(f"  filings enriched  {result.dates_parsed}")
    print(f"  filings failed    {result.dates_failed}  (left for a later pass)")
    print("----------------------------------------------------------------")
    return 0 if result.status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
