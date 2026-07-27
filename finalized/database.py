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

import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, fields
from datetime import datetime, timezone
from typing import Iterable, Iterator, Optional

# The canonical filing schema lives in the locked sync module — we import it so
# the daily-index path stores *exactly* the same fields as the CIK-based path.
from .core import FilingRecord, UA


SCHEMA_VERSION = 2      # v2 added extraction_queue + xbrl_facts

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

-- ── Extraction (see README.md, "Extract") ───────────────────────────────────
-- The work queue for pulling data OUT of filings.  Deliberately the same shape
-- as idx_files: a durable per-item ledger is what makes a long run resumable,
-- lets one bad filing fail in isolation, and gives the next run a retry list.
CREATE TABLE IF NOT EXISTS extraction_queue (
    accession_number TEXT NOT NULL,
    cik              TEXT NOT NULL,
    extractor        TEXT NOT NULL,       -- 'xbrl' | 'text' | ...
    status           TEXT NOT NULL,       -- 'pending'|'done'|'no_xbrl'|'failed'
    fact_count       INTEGER,
    -- Which field group the run stored, or NULL for a full extraction.  Also
    -- registered in _MIGRATIONS so databases created before this column gain
    -- it on connect.
    fact_group       TEXT,
    attempts         INTEGER NOT NULL DEFAULT 0,
    error            TEXT,
    run_id           INTEGER,
    queued_at        TEXT NOT NULL,
    processed_at     TEXT,
    PRIMARY KEY (accession_number, cik, extractor),
    FOREIGN KEY (accession_number, cik)
        REFERENCES filings(accession_number, cik)
);

CREATE INDEX IF NOT EXISTS ix_queue_status ON extraction_queue(extractor, status);

-- One row per reported XBRL fact.  Columns mirror
-- xbrl.parse_instance_bytes() so the parser's output lands here
-- without reshaping, plus the (accession_number, cik) tie-back to `filings`.
CREATE TABLE IF NOT EXISTS xbrl_facts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    accession_number TEXT NOT NULL,       -- ← the tie-back to filings
    cik              TEXT NOT NULL,       -- ←
    concept          TEXT NOT NULL,
    namespace        TEXT,
    value            TEXT,
    value_num        REAL,
    is_numeric       INTEGER,
    is_nil           INTEGER,
    unit             TEXT,
    decimals         TEXT,
    period_type      TEXT,                -- 'duration' | 'instant'
    period_start     TEXT,
    period_end       TEXT,
    period_instant   TEXT,
    is_dimensioned   INTEGER,
    segment          TEXT,
    dimensions       TEXT,                -- JSON blob of the axis/member pairs
    context_id       TEXT,
    source_url       TEXT,                -- the instance document parsed
    run_id           INTEGER,
    extracted_at     TEXT NOT NULL,
    FOREIGN KEY (accession_number, cik)
        REFERENCES filings(accession_number, cik)
);

-- Kept deliberately few: this table grows to tens of millions of rows and each
-- extra index is paid on every insert.
CREATE INDEX IF NOT EXISTS ix_facts_filing  ON xbrl_facts(accession_number, cik);
CREATE INDEX IF NOT EXISTS ix_facts_concept ON xbrl_facts(concept);
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
        # Creation time is written once; the schema version tracks the current
        # code, so an existing database picks up newly-added tables (all the
        # CREATEs are IF NOT EXISTS) and records that it is now at v2.
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES('created_at', ?)",
            (utc_now(),),
        )
        self.conn.commit()
        self._migrate()

    # Additive column migrations, applied on every connect.
    #
    # This exists because of a trap: ``CREATE TABLE IF NOT EXISTS`` silently
    # does nothing when the table already exists, so a column added to _SCHEMA
    # above reaches new databases only — every database created before the
    # change keeps the old shape and fails at the first query touching the new
    # column.  Register the column here as well and both cases are covered.
    #
    #     _MIGRATIONS = {"filings": {"new_col": "TEXT"}}
    #
    # Entries must be idempotent (they are re-checked on every connect) and
    # additive only: SQLite cannot drop or retype a column in place, so a
    # destructive change needs a table rebuild, not an entry here.
    _MIGRATIONS: dict[str, dict[str, str]] = {
        # Which field group a run stored, or NULL for a full extraction.  This
        # is what stops a lean run being mistaken for a complete one later:
        # without it you cannot tell "this filing does not report Assets" from
        # "we only kept ten concepts for this filing".
        "extraction_queue": {"fact_group": "TEXT"},
        # cover.py owns this table, but its CREATE TABLE IF NOT EXISTS cannot
        # add a column to one that already exists — so the column is registered
        # here too, exactly the trap this mechanism exists for.
        "security_cover": {"shares_as_of": "TEXT"},
    }

    def _migrate(self) -> None:
        assert self.conn is not None
        for table, columns in self._MIGRATIONS.items():
            existing = {r["name"] for r in
                        self.conn.execute(f"PRAGMA table_info({table})")}
            if not existing:                      # table does not exist yet
                continue
            for col, decl in columns.items():
                if col not in existing:
                    self.conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
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

    # -- extraction queue --------------------------------------------------------

    def queue_candidates(
        self,
        where_sql: str,
        params: Iterable = (),
        extractor: str = "xbrl",
    ) -> int:
        """
        Add every filing matching ``where_sql`` to the queue as 'pending'.

        Cheap and reversible — it only copies keys out of ``filings``, nothing is
        fetched.  Re-running is a no-op for rows already queued (``DO NOTHING``),
        so widening the form filter later simply tops the queue up.
        """
        assert self.conn is not None
        with self._tx() as c:
            cur = c.execute(
                f"""INSERT INTO extraction_queue
                        (accession_number, cik, extractor, status, queued_at)
                    SELECT accession_number, cik, ?, 'pending', ?
                      FROM filings WHERE {where_sql}
                    ON CONFLICT(accession_number, cik, extractor) DO NOTHING""",
                (extractor, utc_now(), *params),
            )
            return cur.rowcount

    def queue_batch(
        self, extractor: str = "xbrl", limit: int = 200, retry_failed: bool = False,
    ) -> list[sqlite3.Row]:
        """Next pending filings, joined to the full `filings` row to work from."""
        assert self.conn is not None
        states = ("pending", "failed") if retry_failed else ("pending",)
        qs = ",".join("?" * len(states))
        return self.conn.execute(
            f"""SELECT f.* FROM extraction_queue q
                  JOIN filings f
                    ON f.accession_number = q.accession_number AND f.cik = q.cik
                 WHERE q.extractor = ? AND q.status IN ({qs})
                 ORDER BY f.filing_date DESC
                 LIMIT ?""",
            (extractor, *states, int(limit)),
        ).fetchall()

    def mark_extraction(
        self,
        accession_number: str,
        cik: str,
        extractor: str,
        status: str,
        run_id: int,
        fact_count: Optional[int] = None,
        error: Optional[str] = None,
        fact_group: Optional[str] = None,
    ) -> None:
        """Close out one queue row: 'done' | 'no_xbrl' | 'failed'.

        ``fact_group`` records which concept subset was stored (NULL = the full
        fact set), so a later reader can tell a lean extraction from a complete
        one instead of concluding the filing did not report a concept.
        """
        with self._tx() as c:
            c.execute(
                """UPDATE extraction_queue
                      SET status=?, fact_count=?, error=?, run_id=?,
                          fact_group=?, attempts=attempts+1, processed_at=?
                    WHERE accession_number=? AND cik=? AND extractor=?""",
                (status, fact_count, (error or "")[:1000] or None, run_id,
                 fact_group, utc_now(), accession_number, cik, extractor),
            )

    # -- extracted facts ---------------------------------------------------------

    _FACT_COLS = (
        "concept", "namespace", "value", "value_num", "is_numeric", "is_nil",
        "unit", "decimals", "period_type", "period_start", "period_end",
        "period_instant", "is_dimensioned", "segment", "dimensions",
        "context_id", "source_url",
    )

    def insert_facts(
        self, facts: Iterable[dict], accession_number: str, cik: str, run_id: int,
    ) -> int:
        """
        Store the parsed facts for ONE filing, stamped with its identity.

        Replaces any facts previously stored for this (filing, run-independent)
        key so a re-extraction supersedes rather than duplicates.
        """
        assert self.conn is not None
        now = utc_now()
        rows = []
        for f in facts:
            dims = f.get("dimensions")
            if isinstance(dims, (dict, list)):
                dims = json.dumps(dims, ensure_ascii=False, default=str)
            vals = [f.get(k) for k in self._FACT_COLS]
            vals[self._FACT_COLS.index("dimensions")] = dims
            # numpy/pandas scalars are not sqlite-native; coerce the flags
            for i, k in enumerate(self._FACT_COLS):
                if k in ("is_numeric", "is_nil", "is_dimensioned"):
                    vals[i] = None if vals[i] is None else int(bool(vals[i]))
                elif k == "value_num" and vals[i] is not None:
                    try:
                        vals[i] = float(vals[i])
                    except (TypeError, ValueError):
                        vals[i] = None
            rows.append((accession_number, cik, *vals, run_id, now))

        placeholders = ",".join("?" * (len(self._FACT_COLS) + 4))
        with self._tx() as c:
            c.execute("DELETE FROM xbrl_facts WHERE accession_number=? AND cik=?",
                      (accession_number, cik))
            c.executemany(
                f"""INSERT INTO xbrl_facts
                        (accession_number, cik, {", ".join(self._FACT_COLS)},
                         run_id, extracted_at)
                    VALUES ({placeholders})""",
                rows,
            )
        return len(rows)

    def extraction_summary(self, extractor: str = "xbrl") -> dict:
        """Queue state + fact totals, for the CLI to print."""
        assert self.conn is not None
        c = self.conn
        by_status = {
            r["status"]: r["n"] for r in c.execute(
                "SELECT status, COUNT(*) AS n FROM extraction_queue "
                "WHERE extractor=? GROUP BY status", (extractor,))
        }
        facts = c.execute(
            "SELECT COUNT(*) AS n, COUNT(DISTINCT accession_number || '|' || cik) "
            "AS filings FROM xbrl_facts").fetchone()
        return {
            "queued": sum(by_status.values()),
            "by_status": by_status,
            "facts": facts["n"] or 0,
            "filings_with_facts": facts["filings"] or 0,
        }

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
