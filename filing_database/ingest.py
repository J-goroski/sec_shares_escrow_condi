"""
ingest.py — Orchestration for daily-index ingestion into the SQLite database.

Ties the three pieces together:

    daily_index.py  (SEC feed -> FilingRecords)   +
    database.py     (FilingDB persistence)        =>  auditable ingestion

Two entry points, one shared engine:

* :func:`bootstrap` — ingest an explicit ``[start_date, end_date]`` range the
  user chooses (first-time backfill).
* :func:`run_incremental` — resume from the database watermark (last parsed
  date) and catch up to today, plus retry any dates that failed on a prior run.

Fail-safe / backup behaviour
----------------------------
* **Sitemap-driven, never "+1 day".**  We only ever fetch dates the SEC's
  quarter ``index.json`` actually lists, so weekends/holidays/unpublished days
  are skipped rather than failed.
* **Per-date isolation.**  A failure on one date marks that date ``failed`` in
  the ledger and moves on; it is automatically retried on the next run.
* **Hard stop on an IP ban.**  A persistent 429 (``SECBlockedError``) aborts the
  run cleanly, leaving all un-processed dates queued for the next run.
* **Idempotent.**  Re-running any range is safe — filings UPSERT on accession.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable, Optional

import requests

from methods.sec_filings_sync import UA, DEFAULT_DELAY, SECBlockedError
from filing_database.database import FilingDB, DEFAULT_DB_PATH
from filing_database import daily_index as di


# ── Result summary ────────────────────────────────────────────────────────────

@dataclass
class IngestResult:
    run_id: int
    run_type: str
    dates_seen: int
    dates_parsed: int
    dates_failed: int
    filings_inserted: int
    filings_updated: int
    status: str
    watermark: Optional[str]


def _today_iso() -> str:
    return date.today().isoformat()


def _next_day(iso: str) -> str:
    y, m, d = (int(x) for x in iso.split("-"))
    return (date(y, m, d) + timedelta(days=1)).isoformat()


# ── Core engine ───────────────────────────────────────────────────────────────

def _process_dates(
    db: FilingDB,
    infos: list[di.IndexFileInfo],
    run_id: int,
    *,
    session: requests.Session,
    delay: float,
    enrich: bool,
    ticker_map: Optional[dict],
    log: Callable[[str], None],
) -> tuple[int, int, int, int]:
    """
    Download + parse + store each discovered index file, in date order.

    Returns (dates_parsed, dates_failed, filings_inserted, filings_updated).
    Raises ``SECBlockedError`` upward so the caller can abort the whole run.
    """
    parsed = failed = ins_total = upd_total = 0

    for info in infos:
        try:
            records = di.fetch_filings_for_date(
                info, session, delay, ticker_map=ticker_map,
            )

            if enrich and records:
                log(f"    enriching {len(records)} filings (header reads)...")
                records = [di.enrich_record(r, session, delay) for r in records]

            inserted, updated = db.upsert_filings(
                records,
                source_idx_date=info.idx_date,
                source_idx_file=info.file_name,
                run_id=run_id,
                enriched=enrich,
            )
            db.mark_idx_parsed(info.idx_date, run_id, len(records))
            parsed += 1
            ins_total += inserted
            upd_total += updated
            log(
                f"  [{info.idx_date}] {info.file_name:<24} "
                f"{len(records):>5} filings  (+{inserted} new, ~{updated} refreshed)"
            )

        except SECBlockedError:
            # IP-level rate ban — record nothing partial, stop the whole run so
            # the remaining dates are retried next time (backup run).
            db.mark_idx_failed(info.idx_date, run_id, "SECBlockedError: 429 IP ban")
            failed += 1
            raise

        except Exception as exc:  # noqa: BLE001 — isolate one bad date, keep going
            db.mark_idx_failed(info.idx_date, run_id, f"{type(exc).__name__}: {exc}")
            failed += 1
            log(f"  [{info.idx_date}] FAILED: {type(exc).__name__}: {exc} "
                f"(queued for retry next run)")

    return parsed, failed, ins_total, upd_total


def _run(
    db: FilingDB,
    start_date: str,
    end_date: str,
    run_type: str,
    *,
    delay: float,
    enrich: bool,
    resolve_tickers: bool,
    retry_failed: bool,
    log: Callable[[str], None],
) -> IngestResult:
    """Shared engine used by both bootstrap and incremental runs."""
    run_id = db.start_run(run_type, start_date, end_date)
    log(f"[run {run_id}] {run_type}: {start_date} -> {end_date}  "
        f"(UA={UA['User-Agent']}, enrich={enrich})")

    session = requests.Session()
    session.headers.update(UA)

    status = "completed"
    error: Optional[str] = None
    dates_seen = parsed = failed = ins_total = upd_total = 0

    try:
        # 1) Discovery — ask the SEC which dates actually have an index file.
        log("  discovering available daily index files (sitemap)...")
        available = di.list_available_index_files(start_date, end_date, session, delay)
        for info in available:
            db.record_idx_seen(
                info.idx_date, info.file_name, info.url, info.quarter,
                info.last_modified, info.size_label,
            )
        log(f"  {len(available)} index files published in range.")

        # 2) Decide which dates still need work.  Skip dates already parsed; keep
        #    dates that are new ('available') or previously 'failed'.
        todo: list[di.IndexFileInfo] = []
        for info in available:
            st = db.idx_status(info.idx_date)
            if st in ("parsed", "empty"):
                continue
            if st == "failed" and not retry_failed:
                continue
            todo.append(info)

        # 3) Also pull forward any historical failed/available dates BEFORE the
        #    range start (a prior aborted run left a gap) so nothing is orphaned.
        if retry_failed:
            backlog_dates = set(db.pending_idx_dates(upto=end_date))
            have = {i.idx_date for i in todo}
            extra = sorted(backlog_dates - have - {i.idx_date for i in available})
            for d in extra:
                todo.append(di.IndexFileInfo(
                    idx_date=d,
                    file_name=f"master.{di.compact_date(d)}.idx",
                    url=di.master_index_url(d),
                    quarter=di.quarter_label(d),
                    last_modified=None, size_label=None,
                ))
            todo.sort(key=lambda i: i.idx_date)

        dates_seen = len(todo)
        if not todo:
            log("  nothing to do - every published date in range is already parsed.")
        else:
            log(f"  {dates_seen} date(s) to ingest.")

            ticker_map = None
            if resolve_tickers:
                log("  loading company_tickers.json for ticker resolution...")
                try:
                    ticker_map = di.load_ticker_map(session, delay)
                    log(f"    {len(ticker_map)} CIK->ticker mappings loaded.")
                except Exception as exc:  # ticker resolution is best-effort
                    log(f"    ticker map unavailable ({exc}); continuing without tickers.")

            parsed, failed, ins_total, upd_total = _process_dates(
                db, todo, run_id,
                session=session, delay=delay, enrich=enrich,
                ticker_map=ticker_map, log=log,
            )

    except SECBlockedError as exc:
        status = "failed"
        error = str(exc)
        log(f"  ABORTED: {exc}")
    except Exception as exc:  # noqa: BLE001
        status = "failed"
        error = f"{type(exc).__name__}: {exc}"
        log(f"  ABORTED: {error}")
        # Do not re-raise: the run is recorded as failed (below) and the CLIs
        # return a non-zero exit from the 'failed' status, so a scheduler still
        # notices without a raw traceback.  Un-parsed dates retry next run.
    finally:
        session.close()
        db.finish_run(
            run_id, status,
            idx_files_seen=dates_seen,
            idx_files_parsed=parsed,
            idx_files_failed=failed,
            filings_inserted=ins_total,
            filings_updated=upd_total,
            error=error,
        )

    return IngestResult(
        run_id=run_id, run_type=run_type, dates_seen=dates_seen,
        dates_parsed=parsed, dates_failed=failed,
        filings_inserted=ins_total, filings_updated=upd_total,
        status=status, watermark=db.watermark(),
    )


# ── Public entry points ───────────────────────────────────────────────────────

def bootstrap(
    start_date: str,
    end_date: str,
    *,
    db_path: str = DEFAULT_DB_PATH,
    delay: float = DEFAULT_DELAY,
    enrich: bool = False,
    resolve_tickers: bool = True,
    log: Callable[[str], None] = print,
) -> IngestResult:
    """
    Backfill an explicit date range chosen by the user.

    Parameters
    ----------
    start_date, end_date : str
        Inclusive ISO dates ('YYYY-MM-DD').
    db_path : str
        SQLite file (created if absent).
    delay : float
        Inter-request pause; default keeps us under the SEC 10 req/s cap.
    enrich : bool
        If True, additionally read each filing's SGML header to fill
        report_date/primary_document/act/file_number/xbrl fields.  Much slower
        (one request per filing) — leave False for large backfills.
    resolve_tickers : bool
        If True, load company_tickers.json once and fill the ticker column.
    """
    with FilingDB(db_path) as db:
        return _run(
            db, start_date, end_date, "bootstrap",
            delay=delay, enrich=enrich, resolve_tickers=resolve_tickers,
            retry_failed=True, log=log,
        )


def enrich_backfill(
    *,
    db_path: str = DEFAULT_DB_PATH,
    limit: Optional[int] = None,
    forms: Optional[list[str]] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    delay: float = DEFAULT_DELAY,
    max_consecutive_failures: int = 15,
    log: Callable[[str], None] = print,
) -> IngestResult:
    """
    Fill the header-only fields (report_date, primary_document, filing_url, act,
    file_number, is_xbrl, xbrl_instance_url) for filings ingested without
    ``--enrich``.  One request per filing, so scope it with ``limit`` / ``forms``
    / ``since`` / ``until``.

    A filing is marked ``enriched`` only when the header actually yielded a
    primary document, so a transient failure leaves it queued for a later pass.
    A run of ``max_consecutive_failures`` aborts the backfill (assume rate ban).
    """
    with FilingDB(db_path) as db:
        run_id = db.start_run("enrich", since, until)
        todo = db.fetch_unenriched(limit=limit, forms=forms, since=since, until=until)
        total = len(todo)
        log(f"[run {run_id}] enrich: {total} un-enriched filing(s) "
            f"(UA={UA['User-Agent']})")

        session = requests.Session()
        session.headers.update(UA)

        done = failed = updated = 0
        consecutive = 0
        status = "completed"
        error: Optional[str] = None
        try:
            for i, (rec, sid, sfile) in enumerate(todo, 1):
                enr = di.enrich_record(rec, session, delay)
                ok = bool(enr.primary_document)
                _ins, upd = db.upsert_filings(
                    [enr], source_idx_date=sid, source_idx_file=sfile,
                    run_id=run_id, enriched=ok,
                )
                if ok:
                    done += 1
                    updated += upd
                    consecutive = 0
                else:
                    failed += 1
                    consecutive += 1
                    if consecutive >= max_consecutive_failures:
                        status = "failed"
                        error = (f"aborted after {consecutive} consecutive enrichment "
                                 f"failures (possible rate ban)")
                        log(f"  ABORTED: {error}")
                        break
                if i % 250 == 0:
                    log(f"  {i}/{total} processed ({done} enriched, {failed} failed)")
        finally:
            session.close()
            db.finish_run(
                run_id, status,
                filings_updated=updated,
                idx_files_failed=failed,
                error=error,
            )
        log(f"  enriched {done}, failed {failed} of {total}.")
        return IngestResult(
            run_id=run_id, run_type="enrich", dates_seen=total,
            dates_parsed=done, dates_failed=failed, filings_inserted=0,
            filings_updated=updated, status=status, watermark=db.watermark(),
        )


def run_incremental(
    *,
    db_path: str = DEFAULT_DB_PATH,
    end_date: Optional[str] = None,
    start_override: Optional[str] = None,
    delay: float = DEFAULT_DELAY,
    enrich: bool = False,
    resolve_tickers: bool = True,
    log: Callable[[str], None] = print,
) -> IngestResult:
    """
    Resume ingestion from the database watermark up to ``end_date`` (default
    today), retrying any dates that failed previously.

    The resume point is ``watermark + 1 day``; from there discovery decides which
    dates are actually published, so nothing is requested blindly.

    Raises ``RuntimeError`` on an empty database unless ``start_override`` is
    given — the incremental runner is meant to follow a bootstrap.
    """
    end_date = end_date or _today_iso()
    with FilingDB(db_path) as db:
        wm = db.watermark()
        if wm is None and start_override is None:
            raise RuntimeError(
                "Database has no watermark yet - run a bootstrap first, or pass "
                "start_override='YYYY-MM-DD' to seed the incremental runner."
            )
        forward_start = start_override or _next_day(wm)

        # Backup runs: any earlier date still 'failed'/'available' must be retried
        # even when the forward window (watermark+1 .. end) is empty, so a bad day
        # is never orphaned once later days advance the watermark past it.
        backlog = db.pending_idx_dates(upto=end_date)

        if forward_start > end_date and not backlog:
            log(f"[incremental] up to date - watermark {wm}, nothing on/before {end_date}.")
            # Still record a no-op run for the audit trail.
            rid = db.start_run("incremental", forward_start, end_date)
            db.finish_run(rid, "completed")
            return IngestResult(
                run_id=rid, run_type="incremental", dates_seen=0, dates_parsed=0,
                dates_failed=0, filings_inserted=0, filings_updated=0,
                status="completed", watermark=wm,
            )

        # Effective start reaches back to the oldest outstanding date so discovery
        # re-lists its quarter and the retry sweep picks it up.
        start_date = min(forward_start, backlog[0]) if backlog else forward_start
        if backlog:
            log(f"[incremental] {len(backlog)} outstanding date(s) to retry "
                f"(oldest {backlog[0]}).")
        log(f"[incremental] watermark={wm} -> resuming at {start_date}, up to {end_date}")
        return _run(
            db, start_date, end_date, "incremental",
            delay=delay, enrich=enrich, resolve_tickers=resolve_tickers,
            retry_failed=True, log=log,
        )
