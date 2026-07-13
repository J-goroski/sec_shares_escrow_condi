"""
status.py — inspect the filing database (no SQL required).

Prints the ingest watermark, per-run audit log, any failed/pending dates, and
headline counts.  Read-only; safe to run any time, even mid-ingest (WAL).

Examples
--------
    python filing_database/status.py
    python filing_database/status.py --db ./my.sqlite --runs 20
"""

from __future__ import annotations

import os
import sys
import argparse

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from filing_database.database import FilingDB, DEFAULT_DB_PATH, SCHEMA_VERSION  # noqa: E402


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="status.py", description="Show filing-database status.")
    p.add_argument("--db", default=DEFAULT_DB_PATH,
                   help=f"SQLite database path (default: {DEFAULT_DB_PATH}).")
    p.add_argument("--runs", type=int, default=10, help="How many recent runs to show.")
    args = p.parse_args(argv)

    if not os.path.exists(args.db):
        print(f"No database at {args.db}. Run bootstrap.py first.")
        return 1

    with FilingDB(args.db) as db:
        s = db.summary()
        print("=== filing database ===")
        print(f"  path            {args.db}")
        print(f"  schema version  {SCHEMA_VERSION}")
        print(f"  watermark       {s['watermark']}  (resume incremental from the next day)")
        print(f"  filings         {s['filings']:,}  "
              f"({s['enriched']:,} enriched)")
        print(f"  filing dates    {s['filing_date_min']} .. {s['filing_date_max']}")
        print(f"  index days      {s['idx_parsed']} parsed, "
              f"{s['idx_failed']} failed, {s['idx_pending']} pending")

        failed = db.failed_idx_dates()
        if failed:
            print("\n  !! failed/outstanding dates (retried automatically next run):")
            for f in failed:
                print(f"     {f['idx_date']}  attempts={f['attempts']}  {f['error']}")

        print(f"\n=== last {args.runs} runs ===")
        for r in db.recent_runs(args.runs):
            span = ""
            if r["requested_start"] or r["requested_end"]:
                span = f"  [{r['requested_start']}..{r['requested_end']}]"
            print(f"  #{r['run_id']:<3} {r['run_type']:<11} {r['status']:<9} "
                  f"{(r['started_at'] or ''):<20}{span}")
            print(f"        days parsed={r['idx_files_parsed']} failed={r['idx_files_failed']} "
                  f"| filings +{r['filings_inserted']} ~{r['filings_updated']}"
                  + (f" | ERROR: {r['error']}" if r["error"] else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
