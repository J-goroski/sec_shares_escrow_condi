"""
filing_database — daily-index ingestion of SEC EDGAR filings into SQLite.

The daily-index counterpart to ``methods/sec_filings_sync.py``: instead of
walking one company at a time (CIK -> filings), it walks the firm-wide daily
index (date -> every filing) and persists the same ``FilingRecord`` fields into
an auditable SQLite database.

Public surface
--------------
    from filing_database import bootstrap, run_incremental, FilingDB

    bootstrap("2025-06-01", "2025-06-05")     # first-time backfill
    run_incremental()                          # keep current (resume from watermark)

CLIs:
    python filing_database/bootstrap.py --start 2025-06-01 --end 2025-06-05
    python filing_database/run.py
"""

from filing_database.database import FilingDB, DEFAULT_DB_PATH, SCHEMA_VERSION
from filing_database.ingest import (
    bootstrap,
    run_incremental,
    enrich_backfill,
    IngestResult,
)
from filing_database import daily_index

__all__ = [
    "FilingDB",
    "DEFAULT_DB_PATH",
    "SCHEMA_VERSION",
    "bootstrap",
    "run_incremental",
    "enrich_backfill",
    "IngestResult",
    "daily_index",
]
