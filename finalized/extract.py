"""
extract.py — pull XBRL facts out of the filings already mirrored in SQLite.

This is the stage after ingestion (level 3).  ``ingest.py`` mirrors *metadata*;
this module walks that mirror, fetches each filing's XBRL instance document
when one exists, and stores every reported fact keyed back to the filing.
``cover.py`` (level 3.5) then structures those facts without re-fetching.

The tie-back
------------
Every row written to ``xbrl_facts`` carries **(accession_number, cik)** — the
primary key of ``filings`` — so a fact is always one join away from the filing,
company, form type and date it came from::

    SELECT f.entity_name, f.form_type, f.filing_date, x.concept, x.value_num
    FROM   xbrl_facts x
    JOIN   filings f USING (accession_number, cik)
    WHERE  x.concept = 'Assets';

Two requests per filing, and why
--------------------------------
The daily index does not say whether a filing has XBRL, and the instance URL
cannot be guessed: ``_build_xbrl_instance_url`` needs ``primary_document`` and
the inline-XBRL flag, both of which live in the filing's SGML header.  So each
filing costs:

1. one small **range read** of the header (``daily_index.enrich_record``) —
   this both answers "does it have XBRL?" and yields the instance URL, and the
   result is written back to ``filings`` so it is paid only once, ever;
2. one **fetch of the instance document**, only for filings that have one.

Filings with no inline XBRL (most 8-K, older filings) cost step 1 alone and are
marked ``no_xbrl`` so they are never retried.

Two scopes, and they are different questions
--------------------------------------------
**Which filings** — the candidate set is periodic reports, registration
statements and prospectuses, **excluding 424B2**, which is 221,184
medium-term-note pricing supplements from ~775 bank filers and carries no
company disclosure.  Pass ``--include-424b2`` to override.

**Which facts** — ``--facts`` names a field group from
:mod:`finalized.profiles` and stores only those concepts.  The default is
everything, because a concept you did not store cannot be recovered without
re-fetching the filing.  Filtering saves storage, never requests.

Usage
-----
    python -m finalized.cli extract --queue              # fill the queue (free)
    python -m finalized.cli status                       # where things stand
    python -m finalized.cli extract --run --limit 25     # extract 25 filings
    python -m finalized.cli extract --run                # work the whole queue
    python -m finalized.cli extract --run --retry-failed # sweep failures

    python -m finalized.cli extract --run --facts all           # default
    python -m finalized.cli extract --run --facts cover         # Level 3.5 input
    python -m finalized.cli extract --run --facts headquarters,shares

...or from Python::

    from finalized import queue_candidates, extract_xbrl
    queue_candidates()
    extract_xbrl(limit=25, facts="cover")

Queueing is free and reversible: it only copies keys out of ``filings``.  Check
``status`` before committing to a long ``--run``.
"""

from __future__ import annotations

from typing import Callable, Optional

import requests

from .core import UA, DEFAULT_DELAY, SECBlockedError
from .xbrl import fetch_and_parse
from .database import FilingDB, DEFAULT_DB_PATH
from .profiles import describe, resolve_fields
from . import daily_index as di


# ── Candidate scope ───────────────────────────────────────────────────────────
# Periodic reports + registration statements + prospectuses.  424B2 is excluded
# by default: 221,184 rows from ~775 bank filers, all structured-note pricing
# supplements.  Including it roughly doubles the job for no company disclosure.
_BASE_FORMS = (
    "10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A",
    "10-Q", "10-Q/A", "8-K", "8-K/A",
)

_CANDIDATE_TMPL = """(
    form_type IN ({forms})
    OR form_type LIKE 'S-%'
    OR form_type LIKE 'F-%'
    OR form_type LIKE 'POS%'
    OR ({prospectus})
)"""


def candidate_where(include_424b2: bool = False) -> tuple[str, tuple]:
    """The SQL predicate selecting filings worth extracting, plus its params."""
    forms = ",".join("?" * len(_BASE_FORMS))
    prospectus = ("form_type LIKE '424%'" if include_424b2
                  else "form_type LIKE '424%' AND form_type <> '424B2'")
    return (_CANDIDATE_TMPL.format(forms=forms, prospectus=prospectus),
            _BASE_FORMS)


# ── Queueing ──────────────────────────────────────────────────────────────────

def queue_candidates(
    db_path: str = DEFAULT_DB_PATH,
    include_424b2: bool = False,
    log: Callable[[str], None] = print,
) -> int:
    """Mark every candidate filing 'pending'.  No network, fully reversible."""
    where, params = candidate_where(include_424b2)
    with FilingDB(db_path) as db:
        n = db.queue_candidates(where, params, extractor="xbrl")
        s = db.extraction_summary("xbrl")
    log(f"queued {n:,} new filing(s); queue now holds {s['queued']:,}")
    for status, count in sorted(s["by_status"].items()):
        log(f"    {status:<9} {count:,}")
    return n


# ── Extraction ────────────────────────────────────────────────────────────────

def _facts_from_frame(df, keep: Optional[set] = None) -> list[dict]:
    """
    DataFrame -> list of plain dicts with NaN collapsed to None.

    ``keep`` optionally restricts the rows to a set of concepts — the "fact
    fine tuning" path.  Filtering happens here, after parsing and before
    storage: the whole instance is still parsed (it arrived in one request
    either way), we simply store less of it.
    """
    if df is None or df.empty:
        return []
    if keep is not None:
        df = df[df["concept"].isin(keep)]
        if df.empty:
            return []
    clean = df.astype(object).where(df.notna(), None)
    return clean.to_dict("records")


def extract_xbrl(
    db_path: str = DEFAULT_DB_PATH,
    limit: Optional[int] = None,
    batch_size: int = 100,
    delay: float = DEFAULT_DELAY,
    retry_failed: bool = False,
    max_consecutive_failures: int = 15,
    facts: Optional[str] = None,
    log: Callable[[str], None] = print,
) -> dict:
    """
    Work the queue: enrich -> fetch instance -> parse -> store.

    Per-filing failures are isolated and left queued for the next run; a
    persistent 429 (``SECBlockedError``) aborts the whole run cleanly, exactly
    as the ingest path does.

    ``facts`` names a field group (see :mod:`finalized.profiles`) and stores
    **only** that group's concepts — ``"headquarters,shares"``, or ``"all"`` /
    ``None`` for everything.  Everything stays the right default, because a
    concept you did not store cannot be recovered without re-fetching the
    filing; reach for a group when a run only feeds one question and you want
    the database to grow slowly.

    The group used is recorded on each queue row, so a lean extraction is never
    mistaken later for a complete one.
    """
    # Resolve up front: an unknown group must fail before the first request,
    # not after storing nothing for a thousand filings.
    keep = resolve_fields(facts, db_path)

    session = requests.Session()
    session.headers.update(UA)

    done = no_xbrl = failed = 0
    facts_total = 0
    consecutive = 0
    status = "completed"
    error: Optional[str] = None

    with FilingDB(db_path) as db:
        run_id = db.start_run("extract", None, None)
        log(f"[run {run_id}] xbrl extraction  (UA={UA['User-Agent']}, "
            f"delay={delay}s)")
        log(f"  storing {describe(facts, db_path)}")

        remaining = limit if limit is not None else float("inf")
        try:
            while remaining > 0:
                take = int(min(batch_size, remaining))
                rows = db.queue_batch("xbrl", limit=take, retry_failed=retry_failed)
                if not rows:
                    break

                for row in rows:
                    remaining -= 1
                    rec = db._row_to_record(row)
                    acc, cik = rec.accession_number, rec.cik
                    label = f"{rec.ticker or cik} {rec.form_type} {rec.filing_date}"

                    try:
                        # 1) Header read — tells us IF there is an instance, and
                        #    where.  Persisted so it is never paid twice.
                        if not rec.xbrl_instance_url:
                            rec = di.enrich_record(rec, session, delay)
                            db.upsert_filings(
                                [rec],
                                source_idx_date=row["source_idx_date"],
                                source_idx_file=row["source_idx_file"],
                                run_id=run_id,
                                enriched=bool(rec.primary_document),
                            )

                        # 2) No inline XBRL -> nothing to extract, never retry.
                        if not rec.xbrl_instance_url:
                            db.mark_extraction(acc, cik, "xbrl", "no_xbrl", run_id,
                                               fact_group=facts)
                            no_xbrl += 1
                            consecutive = 0
                            continue

                        # 3) Fetch + parse the instance, store the facts.
                        df = fetch_and_parse(rec.xbrl_instance_url,
                                             session=session, delay=delay)
                        n = db.insert_facts(_facts_from_frame(df, keep),
                                            acc, cik, run_id)
                        db.mark_extraction(acc, cik, "xbrl", "done", run_id,
                                           fact_count=n, fact_group=facts)
                        done += 1
                        facts_total += n
                        consecutive = 0
                        log(f"  {label:<34} {n:>6,} facts")

                    except SECBlockedError as exc:
                        db.mark_extraction(acc, cik, "xbrl", "failed", run_id,
                                           error=f"SECBlockedError: {exc}")
                        failed += 1
                        status, error = "failed", str(exc)
                        log(f"  ABORTED (rate ban): {exc}")
                        raise

                    except Exception as exc:            # noqa: BLE001
                        db.mark_extraction(acc, cik, "xbrl", "failed", run_id,
                                           error=f"{type(exc).__name__}: {exc}")
                        failed += 1
                        consecutive += 1
                        log(f"  {label:<34} FAILED: {type(exc).__name__}: {exc}")
                        if consecutive >= max_consecutive_failures:
                            status = "failed"
                            error = (f"aborted after {consecutive} consecutive "
                                     f"failures (possible rate ban)")
                            log(f"  ABORTED: {error}")
                            raise RuntimeError(error) from exc

        except (SECBlockedError, RuntimeError):
            pass                    # already recorded; fall through to finish_run
        finally:
            session.close()
            db.finish_run(
                run_id, status,
                idx_files_seen=done + no_xbrl + failed,
                idx_files_parsed=done,
                idx_files_failed=failed,
                filings_inserted=facts_total,
                error=error,
            )
        summary = db.extraction_summary("xbrl")

    return {"run_id": run_id, "status": status, "done": done,
            "no_xbrl": no_xbrl, "failed": failed, "facts": facts_total,
            "summary": summary}
