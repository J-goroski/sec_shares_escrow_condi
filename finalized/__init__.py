"""
finalized — the settled core of the SEC EDGAR pipeline.

A self-contained package covering the pieces that have proven out:

    core.py         SEC access policy: the ONE rate-limited fetcher, the UA
                    header, SECBlockedError, FilingRecord, URL builders, and
                    the CIK-driven submissions-API path.
    daily_index.py  the date-driven path: daily-index discovery, master.idx
                    parsing, ticker resolution, SGML header enrichment.
    database.py     SQLite schema + data access (filings, idx_files,
                    ingest_runs, extraction_queue, xbrl_facts).
    ingest.py       orchestration: bootstrap / run_incremental / enrich_backfill.
    extract.py      level 3 — XBRL fact extraction, keyed back to the filing.
    profiles.py     level 3 fine tuning — named field groups deciding which
                    concepts a run stores ('all', 'cover', 'headquarters'...).
    cover.py        level 3.5 — entity_cover + security_cover: one clean row
                    per company and one per share class, listed or not.
    sgml.py         level 4 — HQ + state of incorporation read from any
                    filing's SGML header, XBRL or not; the cross-check and
                    gap-filler for cover.py.
    resolve.py      level 4 — one trusted current value per company per field,
                    with its as-of date, its source, and a graded verdict on
                    whether the sources agreed.
    covertext.py    level 4 — reads the PRINTED cover page for the one thing
                    XBRL never states: a second principal executive office.
    jurisdiction.py decodes EDGAR codes and ISO codes, which are different
                    tables that share letters (CA = California vs Canada).
    xbrl.py         XBRL instance parsing -> tidy facts, cover pages.
    backup.py       online (WAL-safe) database snapshots.
    cli.py          one command-line entry point for all of the above.

The levels
----------
    1-2  ingest      mirror filing metadata            -> filings
    3    extract     pull XBRL facts                   -> xbrl_facts
    3.5  cover       structure entity + securities     -> entity_cover,
                                                          security_cover

Two ways in, one record shape
-----------------------------
    by CIK    core.fetch_filings_for_ciks(["320193"])       -> [FilingRecord]
    by date   ingest.bootstrap("2026-01-01", "2026-01-31")  -> SQLite

Both produce identical ``FilingRecord`` fields, so everything downstream works
the same either way.  See README.md for the full guide.

Quick start
-----------
    from finalized import bootstrap, run_incremental, FilingDB

    bootstrap("2026-06-01", "2026-06-30")     # mirror a month
    run_incremental()                         # stay current

    python -m finalized.cli status

Level 3 -> 3.5 in two commands::

    python -m finalized.cli extract --run --facts cover
    python -m finalized.cli cover --build

Then the rule the cover tables exist to make true::

    SELECT * FROM security_cover WHERE security_type = 'equity'

returns exactly the registrant's share classes, listed and unlisted.
"""

from .core import (
    FilingRecord,
    SECBlockedError,
    UA,
    DEFAULT_DELAY,
    fetch_filings_for_ciks,
)
from .database import FilingDB, DEFAULT_DB_PATH, SCHEMA_VERSION
from .ingest import bootstrap, run_incremental, enrich_backfill, IngestResult
from .extract import queue_candidates, extract_xbrl, candidate_where
from .profiles import (
    ALL,
    FIELD_GROUPS,
    COVER_REQUIRED,
    all_groups,
    coverage,
    define_group,
    delete_group,
    resolve_fields,
)
from .cover import (
    build_cover_tables,
    cover_summary,
    merge_listing_rows,
    refine_security_type,
    rehydrate,
    PERIODIC_FORMS,
)
from .sgml import (
    HeaderFacts,
    LEVEL4_BASE_FORMS,
    extract_headers,
    header_summary,
    level4_where,
    parse_header,
)
from .covertext import (
    COVER_FORMS,
    CoverPage,
    covertext_summary,
    extract_cover_offices,
    parse_cover,
    to_text,
)
from .jurisdiction import (
    CANADA,
    UNITED_STATES,
    Jurisdiction,
    Office,
    country_from_iso,
    country_from_text,
    country_of,
    decode,
    resolve_office,
    same_country,
    state_of,
)
from .resolve import (
    build_final,
    build_resolved,
    build_security_final,
    compare,
    conflicts,
    grade,
    grade_shares,
    normalise,
    resolved_summary,
)
from .backup import backup_database, list_backups, restore_backup, verify_backup
from . import daily_index, xbrl

__all__ = [
    # record shape + access policy
    "FilingRecord", "SECBlockedError", "UA", "DEFAULT_DELAY",
    # the two ways in
    "fetch_filings_for_ciks",       # by CIK  (submissions API)
    "bootstrap", "run_incremental",  # by date (daily index)
    # storage
    "FilingDB", "DEFAULT_DB_PATH", "SCHEMA_VERSION", "IngestResult",
    # enrichment + extraction (level 3)
    "enrich_backfill", "queue_candidates", "extract_xbrl", "candidate_where",
    # field selection (level 3 fine tuning)
    "ALL", "FIELD_GROUPS", "COVER_REQUIRED", "all_groups", "coverage",
    "define_group", "delete_group", "resolve_fields",
    # cover structuring (level 3.5)
    "build_cover_tables", "cover_summary", "merge_listing_rows",
    "refine_security_type", "rehydrate", "PERIODIC_FORMS",
    # SGML header facts + trusted-value resolution (level 4)
    "HeaderFacts", "LEVEL4_BASE_FORMS", "extract_headers", "header_summary",
    "level4_where", "parse_header",
    "build_resolved", "compare", "conflicts", "normalise", "resolved_summary",
    # finalized, decoded values + confidence (level 4)
    "build_final", "build_security_final", "grade", "grade_shares",
    # printed cover page: the second principal executive office (level 4)
    "COVER_FORMS", "CoverPage", "covertext_summary", "extract_cover_offices",
    "parse_cover", "to_text",
    # jurisdiction decoding -- EDGAR codes and ISO codes are NOT the same table
    "CANADA", "UNITED_STATES", "Jurisdiction", "Office", "country_from_iso",
    "country_from_text", "country_of", "decode", "resolve_office",
    "same_country", "state_of",
    # backups
    "backup_database", "list_backups", "restore_backup", "verify_backup",
    # submodules
    "daily_index", "xbrl",
]
