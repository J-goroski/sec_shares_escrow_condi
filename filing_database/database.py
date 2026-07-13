"""
database.py — SQLite persistence layer for the daily-index filing ingest.

This module owns the schema and every read/write against the SQLite file.  It
is deliberately isolated from the SEC-fetching logic (``daily_index.py``) and
the orchestration (``ingest.py``) so the storage contract is easy to audit.

Design goals (good, auditable database practice)
-------------------------------------------------
* **Idempotent writes.**  Filings are keyed by (accession_number, cik) and
  written with UPSERT, so re-running any date range never duplicates rows — it
  refreshes provenance instead.  The composite key matches the true grain of the
  daily index: an ownership or group filing (Form 3/4/5, SC 13D/G, a corporate
  family's 8-K) is listed once per associated filer CIK, so each (filing, filer)
  pair is its own row — exactly as the CIK-based sync path would return it.
* **A watermark ledger.**  ``idx_files`` records every daily ``master.*.idx``
  file we have *seen* and its processing ``status``.  The incremental runner
  reads the high-water mark from this table (the newest ``parsed`` date) rather
  than guessing "last + 1", and failed dates stay queued for the next run.
* **A run log.**  ``ingest_runs`` records one row per execution (bootstrap or
  incremental) with counts, the exact User-Agent used, timestamps and status —
  a complete audit trail of when data entered the database and how.
* **Full provenance on every filing.**  Each ``filings`` row carries the
  ``source_idx_file`` / ``source_idx_date`` it came from and the run ids that
  first and last touched it.

Tables
------
``meta``         key/value store (schema version, creation time).
``ingest_runs``  one row per bootstrap/incremental execution.
``idx_files``    one row per daily master index file (the watermark ledger).
``filings``      one row per filing — the FilingRecord fields plus provenance.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, fields
from datetime import datetime, timezone
from typing import Iterable, Iterator, Optional

# The canonical filing schema lives in the locked sync module — we import it so
# the daily-index path stores *exactly* the same fields as the CIK-based path.
from methods.sec_filings_sync import FilingRecord, UA


SCHEMA_VERSION = 1

# Default location for the database file: alongside this package.
DEFAULT_DB_PATH = os.path.join(os.path.dirname(__file__), "edgar_filings.sqlite")


# ── Timestamp helper ──────────────────────────────────────────────────────────

def utc_now() -> str:
    """ISO-8601 UTC timestamp to the second — used for every audit column."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── Schema ────────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- One row per execution of the bootstrap or the incremental runner.
CREATE TABLE IF NOT EXISTS ingest_runs (
    run_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_type          TEXT    NOT NULL,           -- 'bootstrap' | 'incremental'
    requested_start   TEXT,                       -- YYYY-MM-DD (may be NULL)
    requested_end     TEXT,                       -- YYYY-MM-DD
    started_at        TEXT    NOT NULL,           -- ISO-8601 UTC
    finished_at       TEXT,                       -- ISO-8601 UTC (NULL while running)
    status            TEXT    NOT NULL,           -- 'running'|'completed'|'failed'
    idx_files_seen    INTEGER NOT NULL DEFAULT 0,
    idx_files_parsed  INTEGER NOT NULL DEFAULT 0,
    idx_files_failed  INTEGER NOT NULL DEFAULT 0,
    filings_inserted  INTEGER NOT NULL DEFAULT 0,
    filings_updated   INTEGER NOT NULL DEFAULT 0,
    user_agent        TEXT,                        -- the UA (email) used, for audit
    host              TEXT,
    error             TEXT
);

-- The watermark ledger: one row per daily master.*.idx file we know about.
-- The incremental high-water mark is MAX(idx_date) WHERE status='parsed'.
CREATE TABLE IF NOT EXISTS idx_files (
    idx_date       TEXT PRIMARY KEY,              -- YYYY-MM-DD (the index date)
    idx_file_name  TEXT NOT NULL,                 -- 'master.YYYYMMDD.idx'
    idx_url        TEXT NOT NULL,                 -- full URL fetched
    quarter        TEXT NOT NULL,                 -- e.g. '2025-QTR2'
    last_modified  TEXT,                          -- from the sitemap listing
    size_label     TEXT,                          -- from the sitemap listing
    status         TEXT NOT NULL,                 -- 'available'|'parsed'|'failed'|'empty'
    filing_count   INTEGER,                       -- rows parsed from the file
    attempts       INTEGER NOT NULL DEFAULT 0,
    first_seen_at  TEXT NOT NULL,
    processed_at   TEXT,
    run_id         INTEGER,                       -- run that last processed it
    error          TEXT,
    FOREIGN KEY (run_id) REFERENCES ingest_runs(run_id)
);

-- One row per (filing, filer CIK).  Mirrors FilingRecord + provenance/audit
-- columns.  The daily index lists ownership/group filings once per associated
-- CIK, so the natural key is the (accession_number, cik) pair, not accession
-- alone — this preserves every filer association and matches the CIK sync grain.
CREATE TABLE IF NOT EXISTS filings (
    accession_number   TEXT NOT NULL,
    cik                TEXT NOT NULL,
    entity_name        TEXT,
    ticker             TEXT,
    form_type          TEXT,
    filing_date        TEXT,                       -- YYYY-MM-DD
    report_date        TEXT,                       -- YYYY-MM-DD (NULL until enriched)
    primary_document   TEXT,
    filing_url         TEXT,
    index_url          TEXT,
    filing_detail_url  TEXT,
    submission_txt_url TEXT,
    xbrl_instance_url  TEXT,
    file_number        TEXT,
    act                TEXT,
    size               INTEGER,
    is_xbrl            INTEGER,                    -- 0/1/NULL
    -- provenance / audit
    source_idx_date    TEXT NOT NULL,             -- daily index date it came from
    source_idx_file    TEXT NOT NULL,             -- 'master.YYYYMMDD.idx'
    enriched           INTEGER NOT NULL DEFAULT 0,-- 1 once header fields are filled
    first_seen_run     INTEGER,
    last_seen_run      INTEGER,
    ingested_at        TEXT NOT NULL,
    updated_at         TEXT,
    PRIMARY KEY (accession_number, cik),
    FOREIGN KEY (source_idx_date) REFERENCES idx_files(idx_date)
);

CREATE INDEX IF NOT EXISTS ix_filings_cik      ON filings(cik);
CREATE INDEX IF NOT EXISTS ix_filings_acc      ON filings(accession_number);
CREATE INDEX IF NOT EXISTS ix_filings_form     ON filings(form_type);
CREATE INDEX IF NOT EXISTS ix_filings_date     ON filings(filing_date);
CREATE INDEX IF NOT EXISTS ix_filings_srcdate  ON filings(source_idx_date);
"""


# The FilingRecord fields, in declaration order, so we can map a record onto the
# filings table without hand-maintaining a second column list.
_FILING_FIELDS = [f.name for f in fields(FilingRecord)]


class FilingDB:
    """
    Thin, explicit data-access object around a single SQLite file.

    All methods are synchronous and take/return plain Python types or
    ``FilingRecord`` objects.  Open with :meth:`connect` (or use it as a context
    manager) and it will create the schema on first use.
    """

    def __init__(self, path: str = DEFAULT_DB_PATH):
        self.path = path
        self.conn: Optional[sqlite3.Connection] = None

    # -- lifecycle --------------------------------------------------------------

    def connect(self) -> "FilingDB":
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30.0)
        self.conn.row_factory = sqlite3.Row
        # Pragmas for durability + concurrency:
        #   WAL      — readers never block the writer (safe to query mid-run)
        #   FK on    — enforce the run_id / idx_date foreign keys
        #   busy 30s — wait rather than error if the file is briefly locked
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()
        return self

    def _init_schema(self) -> None:
        assert self.conn is not None
        self.conn.executescript(_SCHEMA)
        # Record schema version / creation time exactly once.
        self.conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES('created_at', ?)",
            (utc_now(),),
        )
        self.conn.commit()

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def __enter__(self) -> "FilingDB":
        return self.connect()

    def __exit__(self, *exc) -> None:
        self.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """Transaction scope — commit on success, roll back on error."""
        assert self.conn is not None, "call connect() first"
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # -- run log ----------------------------------------------------------------

    def start_run(
        self,
        run_type: str,
        requested_start: Optional[str] = None,
        requested_end: Optional[str] = None,
    ) -> int:
        """Open a new run row (status='running') and return its run_id."""
        with self._tx() as c:
            cur = c.execute(
                """INSERT INTO ingest_runs
                       (run_type, requested_start, requested_end, started_at,
                        status, user_agent, host)
                   VALUES (?, ?, ?, ?, 'running', ?, ?)""",
                (
                    run_type,
                    requested_start,
                    requested_end,
                    utc_now(),
                    UA.get("User-Agent", ""),
                    os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME") or "",
                ),
            )
            return int(cur.lastrowid)

    def finish_run(
        self,
        run_id: int,
        status: str,
        *,
        idx_files_seen: int = 0,
        idx_files_parsed: int = 0,
        idx_files_failed: int = 0,
        filings_inserted: int = 0,
        filings_updated: int = 0,
        error: Optional[str] = None,
    ) -> None:
        """Close a run row with final counts and status."""
        with self._tx() as c:
            c.execute(
                """UPDATE ingest_runs
                      SET finished_at=?, status=?, idx_files_seen=?,
                          idx_files_parsed=?, idx_files_failed=?,
                          filings_inserted=?, filings_updated=?, error=?
                    WHERE run_id=?""",
                (
                    utc_now(), status, idx_files_seen, idx_files_parsed,
                    idx_files_failed, filings_inserted, filings_updated,
                    error, run_id,
                ),
            )

    # -- idx-file ledger --------------------------------------------------------

    def record_idx_seen(
        self,
        idx_date: str,
        idx_file_name: str,
        idx_url: str,
        quarter: str,
        last_modified: Optional[str],
        size_label: Optional[str],
    ) -> None:
        """
        Register that a daily index file exists (status='available').

        Never downgrades an already-'parsed' row — re-discovering a date that is
        already ingested just refreshes the sitemap metadata.
        """
        with self._tx() as c:
            c.execute(
                """INSERT INTO idx_files
                       (idx_date, idx_file_name, idx_url, quarter, last_modified,
                        size_label, status, first_seen_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'available', ?)
                   ON CONFLICT(idx_date) DO UPDATE SET
                        idx_file_name = excluded.idx_file_name,
                        idx_url       = excluded.idx_url,
                        quarter       = excluded.quarter,
                        last_modified = excluded.last_modified,
                        size_label    = excluded.size_label""",
                (idx_date, idx_file_name, idx_url, quarter, last_modified,
                 size_label, utc_now()),
            )

    def mark_idx_parsed(
        self, idx_date: str, run_id: int, filing_count: int
    ) -> None:
        with self._tx() as c:
            status = "parsed" if filing_count > 0 else "empty"
            c.execute(
                """UPDATE idx_files
                      SET status=?, filing_count=?, processed_at=?, run_id=?,
                          attempts=attempts+1, error=NULL
                    WHERE idx_date=?""",
                (status, filing_count, utc_now(), run_id, idx_date),
            )

    def mark_idx_failed(self, idx_date: str, run_id: int, error: str) -> None:
        with self._tx() as c:
            c.execute(
                """UPDATE idx_files
                      SET status='failed', processed_at=?, run_id=?,
                          attempts=attempts+1, error=?
                    WHERE idx_date=?""",
                (utc_now(), run_id, error[:1000], idx_date),
            )

    def watermark(self) -> Optional[str]:
        """
        Newest successfully-parsed index date (YYYY-MM-DD), or None if the DB
        has never parsed a day.  This is the incremental resume point.
        """
        assert self.conn is not None
        row = self.conn.execute(
            "SELECT MAX(idx_date) AS d FROM idx_files WHERE status IN ('parsed','empty')"
        ).fetchone()
        return row["d"] if row and row["d"] else None

    def pending_idx_dates(self, upto: Optional[str] = None) -> list[str]:
        """
        Dates we know exist but have NOT parsed yet (status 'available' or
        'failed') — i.e. the backlog + failed dates to retry on the next run.
        Optionally bounded to <= ``upto``.
        """
        assert self.conn is not None
        sql = "SELECT idx_date FROM idx_files WHERE status IN ('available','failed')"
        params: list = []
        if upto:
            sql += " AND idx_date <= ?"
            params.append(upto)
        sql += " ORDER BY idx_date"
        return [r["idx_date"] for r in self.conn.execute(sql, params).fetchall()]

    def idx_status(self, idx_date: str) -> Optional[str]:
        assert self.conn is not None
        row = self.conn.execute(
            "SELECT status FROM idx_files WHERE idx_date=?", (idx_date,)
        ).fetchone()
        return row["status"] if row else None

    # -- filings ----------------------------------------------------------------

    def upsert_filings(
        self,
        records: Iterable[FilingRecord],
        *,
        source_idx_date: str,
        source_idx_file: str,
        run_id: int,
        enriched: bool = False,
    ) -> tuple[int, int]:
        """
        Insert or refresh a batch of filings.  Returns (inserted, updated).

        Keyed on (accession_number, cik) so repeated ingests of the same date
        are idempotent.  On conflict we refresh the record fields and provenance
        but preserve the original ``ingested_at`` / ``first_seen_run``.
        """
        assert self.conn is not None
        inserted = updated = 0
        now = utc_now()
        enr = 1 if enriched else 0
        with self._tx() as c:
            for rec in records:
                d = asdict(rec)
                exists = c.execute(
                    "SELECT 1 FROM filings WHERE accession_number=? AND cik=?",
                    (rec.accession_number, rec.cik),
                ).fetchone()
                params = {
                    **{k: d.get(k) for k in _FILING_FIELDS},
                    "is_xbrl": (None if d.get("is_xbrl") is None else int(bool(d["is_xbrl"]))),
                    "source_idx_date": source_idx_date,
                    "source_idx_file": source_idx_file,
                    "enriched": enr,
                    "run_id": run_id,
                    "now": now,
                }
                c.execute(
                    """INSERT INTO filings (
                            accession_number, cik, entity_name, ticker, form_type,
                            filing_date, report_date, primary_document, filing_url,
                            index_url, filing_detail_url, submission_txt_url,
                            xbrl_instance_url, file_number, act, size, is_xbrl,
                            source_idx_date, source_idx_file, enriched,
                            first_seen_run, last_seen_run, ingested_at, updated_at)
                       VALUES (
                            :accession_number, :cik, :entity_name, :ticker, :form_type,
                            :filing_date, :report_date, :primary_document, :filing_url,
                            :index_url, :filing_detail_url, :submission_txt_url,
                            :xbrl_instance_url, :file_number, :act, :size, :is_xbrl,
                            :source_idx_date, :source_idx_file, :enriched,
                            :run_id, :run_id, :now, NULL)
                       ON CONFLICT(accession_number, cik) DO UPDATE SET
                            entity_name=excluded.entity_name,
                            -- ticker/primary_document/filing_url use '' (not NULL) as
                            -- their "unknown" sentinel in the master-index parse, so
                            -- NULLIF(...,'') stops a bare re-ingest from wiping a value
                            -- an earlier ticker-resolved / enriched pass already filled.
                            ticker=COALESCE(NULLIF(excluded.ticker,''), filings.ticker),
                            form_type=excluded.form_type,
                            filing_date=excluded.filing_date,
                            report_date=COALESCE(excluded.report_date, filings.report_date),
                            primary_document=COALESCE(NULLIF(excluded.primary_document,''), filings.primary_document),
                            filing_url=COALESCE(NULLIF(excluded.filing_url,''), filings.filing_url),
                            index_url=excluded.index_url,
                            filing_detail_url=excluded.filing_detail_url,
                            submission_txt_url=excluded.submission_txt_url,
                            xbrl_instance_url=COALESCE(excluded.xbrl_instance_url, filings.xbrl_instance_url),
                            file_number=COALESCE(excluded.file_number, filings.file_number),
                            act=COALESCE(excluded.act, filings.act),
                            size=COALESCE(excluded.size, filings.size),
                            is_xbrl=COALESCE(excluded.is_xbrl, filings.is_xbrl),
                            source_idx_date=excluded.source_idx_date,
                            source_idx_file=excluded.source_idx_file,
                            enriched=MAX(filings.enriched, excluded.enriched),
                            last_seen_run=excluded.last_seen_run,
                            updated_at=:now""",
                    params,
                )
                if exists:
                    updated += 1
                else:
                    inserted += 1
        return inserted, updated

    def count_filings(self) -> int:
        assert self.conn is not None
        return int(self.conn.execute("SELECT COUNT(*) AS n FROM filings").fetchone()["n"])

    def _row_to_record(self, row: sqlite3.Row) -> FilingRecord:
        """Rebuild a FilingRecord from a filings row (for enrichment backfill)."""
        d = {k: row[k] for k in _FILING_FIELDS}
        if d.get("is_xbrl") is not None:
            d["is_xbrl"] = bool(d["is_xbrl"])
        return FilingRecord(**d)

    def _unenriched_where(self, forms, since, until) -> tuple[str, list]:
        sql, params = " WHERE enriched=0", []
        if forms:
            sql += " AND form_type IN (%s)" % ",".join("?" * len(forms))
            params += list(forms)
        if since:
            sql += " AND filing_date >= ?"
            params.append(since)
        if until:
            sql += " AND filing_date <= ?"
            params.append(until)
        return sql, params

    def count_unenriched(self, forms=None, since=None, until=None) -> int:
        assert self.conn is not None
        where, params = self._unenriched_where(forms, since, until)
        return int(self.conn.execute(
            "SELECT COUNT(*) AS n FROM filings" + where, params
        ).fetchone()["n"])

    def fetch_unenriched(
        self, limit=None, forms=None, since=None, until=None
    ) -> list[tuple[FilingRecord, str, str]]:
        """
        Return (record, source_idx_date, source_idx_file) triples for filings
        not yet header-enriched, oldest first.  Used by the enrichment backfill.
        """
        assert self.conn is not None
        where, params = self._unenriched_where(forms, since, until)
        sql = "SELECT * FROM filings" + where + " ORDER BY filing_date, accession_number"
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        rows = self.conn.execute(sql, params).fetchall()
        return [(self._row_to_record(r), r["source_idx_date"], r["source_idx_file"])
                for r in rows]

    def recent_runs(self, limit: int = 10) -> list[dict]:
        assert self.conn is not None
        rows = self.conn.execute(
            "SELECT * FROM ingest_runs ORDER BY run_id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def failed_idx_dates(self) -> list[dict]:
        assert self.conn is not None
        rows = self.conn.execute(
            "SELECT idx_date, attempts, error FROM idx_files "
            "WHERE status='failed' ORDER BY idx_date"
        ).fetchall()
        return [dict(r) for r in rows]

    def summary(self) -> dict:
        """Small dashboard dict for the CLIs to print after a run."""
        assert self.conn is not None
        c = self.conn
        row = c.execute(
            """SELECT COUNT(*) AS filings,
                      MIN(filing_date) AS min_date,
                      MAX(filing_date) AS max_date,
                      SUM(enriched) AS enriched
                 FROM filings"""
        ).fetchone()
        idx = c.execute(
            """SELECT
                   SUM(status IN ('parsed','empty')) AS parsed,
                   SUM(status='failed')             AS failed,
                   SUM(status='available')          AS pending
                 FROM idx_files"""
        ).fetchone()
        return {
            "filings": row["filings"] or 0,
            "filing_date_min": row["min_date"],
            "filing_date_max": row["max_date"],
            "enriched": row["enriched"] or 0,
            "watermark": self.watermark(),
            "idx_parsed": idx["parsed"] or 0,
            "idx_failed": idx["failed"] or 0,
            "idx_pending": idx["pending"] or 0,
        }
