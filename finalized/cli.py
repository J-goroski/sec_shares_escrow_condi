"""
cli.py — one entry point for the whole pipeline.

Replaces the five separate scripts the working tree grew (``bootstrap.py``,
``run.py``, ``enrich.py``, ``status.py`` and ``extract.py``'s own parser), each
of which re-declared ``--db``, ``--delay`` and its own ISO-date validator.
Here those live once, on a shared parent parser, and each stage is a
subcommand.

    python -m finalized.cli <command> [options]

Commands
--------
    bootstrap   backfill an explicit date range from the daily index
    sync        catch up from the watermark to today (the scheduled job)
    enrich      fill header-only fields (report_date, primary_document, XBRL)
    facts       list / inspect the field groups that --facts accepts
    extract     queue filings and pull their XBRL facts        (level 3)
    cover       build entity_cover + security_cover from facts (level 3.5)
    headers     read HQ + incorporation from SGML headers      (level 4)
    status      read-only view of ingest + extraction state
    backup      snapshot / list / verify / restore / prune the database

Run ``python -m finalized.cli <command> --help`` for a command's options.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

from .backup import (backup_database, list_backups, prune_backups,
                     restore_backup, verify_backup)
from .core import DEFAULT_DELAY
from .cover import build_cover_tables, cover_summary
from .database import DEFAULT_DB_PATH, FilingDB
from .extract import extract_xbrl, queue_candidates
from .ingest import bootstrap, enrich_backfill, run_incremental
from .profiles import (ALL, all_groups, coverage, define_group, delete_group,
                       resolve_fields)
from .resolve import (build_final, build_resolved, build_security_final,
                      conflicts, resolved_summary)
from .covertext import covertext_summary, extract_cover_offices
from .sgml import extract_headers, header_summary


# ── shared option plumbing (declared once, inherited by every subcommand) ─────

def _valid_date(s: str) -> str:
    try:
        date.fromisoformat(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an ISO date (YYYY-MM-DD): {s!r}")
    return s


def _common() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--db", default=DEFAULT_DB_PATH,
                   help="SQLite database path (default: %(default)s).")
    p.add_argument("--delay", type=float, default=DEFAULT_DELAY,
                   help="Seconds between SEC requests (default: %(default)s).")
    return p


# ── command implementations ───────────────────────────────────────────────────

def _report(result) -> int:
    print("\n--- run complete ----------------------------------------------")
    print(f"  run id            {result.run_id}  ({result.status})")
    print(f"  dates ingested    {result.dates_parsed}  "
          f"(failed: {result.dates_failed})")
    print(f"  filings inserted  {result.filings_inserted:,}")
    print(f"  filings refreshed {result.filings_updated:,}")
    print(f"  watermark now     {result.watermark}")
    print("---------------------------------------------------------------")
    return 0 if result.status == "completed" else 1


def cmd_bootstrap(a) -> int:
    return _report(bootstrap(a.start, a.end, db_path=a.db, delay=a.delay,
                             enrich=a.enrich, resolve_tickers=not a.no_tickers))


def cmd_sync(a) -> int:
    try:
        result = run_incremental(db_path=a.db, end_date=a.end,
                                 start_override=a.start, delay=a.delay,
                                 enrich=a.enrich,
                                 resolve_tickers=not a.no_tickers)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return _report(result)


def cmd_enrich(a) -> int:
    forms = [f.strip() for f in a.forms.split(",")] if a.forms else None
    r = enrich_backfill(db_path=a.db, limit=a.limit, forms=forms,
                        since=a.since, until=a.until, delay=a.delay)
    print(f"\nenriched {r.dates_parsed:,}, failed {r.dates_failed:,}")
    return 0 if r.status == "completed" else 1


def cmd_facts(a) -> int:
    """Inspect the field groups that ``extract --facts`` accepts."""
    if a.define:
        name, _, concepts = a.define.partition("=")
        if not concepts:
            print("error: use --define name=Concept1,Concept2", file=sys.stderr)
            return 2
        n = define_group(name.strip(), concepts.split(","), db_path=a.db,
                         description=a.description)
        print(f"defined group {name.strip()!r} with {n} concept(s)")
        return 0

    if a.delete:
        print(f"deleted {a.delete!r}" if delete_group(a.delete, a.db)
              else f"no such custom group: {a.delete!r}")
        return 0

    if a.coverage:
        rows = coverage(a.coverage, a.db)
        if not rows:
            print("no concepts in that group")
            return 0
        print(f"coverage of '{a.coverage}' across "
              f"{rows[0]['total']:,} extracted filing(s)\n")
        for r in rows:
            bar = "#" * int(r["pct"] / 4)
            print(f"  {r['pct']:>5.1f}%  {r['filings']:>8,}  "
                  f"{r['concept']:<44} {bar}")
        print("\n  A concept at low coverage is not something to build an "
              "answer on without a fallback.")
        return 0

    groups = all_groups(a.db)
    if a.show:
        if a.show not in groups:
            print(f"unknown group {a.show!r}. Known: "
                  f"{', '.join(sorted(groups))}", file=sys.stderr)
            return 2
        desc, concepts = groups[a.show]
        print(f"{a.show} — {desc}\n{len(concepts)} concept(s):\n")
        for c in sorted(concepts):
            print(f"  {c}")
        return 0

    builtin = {"identity", "headquarters", "shares", "document", "financials",
               "cover"}
    print("field groups for  extract --facts <spec>\n")
    print(f"  {ALL:<14} {'-':>4}  store every fact (the default)")
    for name in sorted(groups):
        desc, concepts = groups[name]
        tag = "" if name in builtin else "  [custom]"
        print(f"  {name:<14} {len(concepts):>4}  {desc}{tag}")
    print("\nGroups compose:  --facts headquarters,shares")
    print("A bare CamelCase concept also works:  --facts Assets,Liabilities")
    print("Level 3.5 (cover) needs at least:  --facts cover")
    return 0


def cmd_cover(a) -> int:
    """Level 3.5 — build the entity/security tables from stored facts."""
    if not (a.build or a.summary):
        # A full rebuild walks every company, so it is never the implicit
        # action — say which one you meant.
        print("pass --build to (re)build, or --summary to see what exists",
              file=sys.stderr)
        return 2

    if a.summary:
        s = cover_summary(a.db)
        if not s["built"]:
            print("cover tables not built yet — run: cli cover --build")
            return 1
        print(f"  entity_cover      {s['entities']:,}")
        print(f"  security_cover    {s['securities']:,}")
        for t, n in sorted(s["by_type"].items(), key=lambda kv: -kv[1]):
            print(f"      {str(t):<10} {n:,}")
        print(f"  multi-class ciks  {s['multi_class_companies']:,}")
        return 0

    r = build_cover_tables(a.db, cik=a.cik, limit=a.limit)
    print("\n--- cover build complete --------------------------------------")
    print(f"  entity_cover    {r['entities']:,}")
    print(f"  security_cover  {r['securities']:,}")
    print(f"  skipped         {r['skipped']:,}")
    print("---------------------------------------------------------------")
    print("A registrant's share classes, listed and unlisted:")
    print("  SELECT * FROM security_cover WHERE security_type = 'equity';")
    return 0


def cmd_headers(a) -> int:
    """Level 4 source A — HQ + incorporation from the SGML header."""
    if a.summary:
        s = header_summary(a.db)
        if not s["built"]:
            print("no headers read yet — run: cli headers --run --limit 200")
            return 1
        print(f"  header_cover      {s['rows']:,} rows, {s['ciks']:,} companies")
        print(f"  with incorporation{s['with_incorporation']:>8,}  "
              f"(absent is normal — many 6-K/S-3 omit it)")
        print(f"  with a city       {s['with_city']:>8,}")
        print(f"  mail-address only {s['mail_only']:>8,}")
        return 0

    if not a.run:
        print("pass --run to read headers, or --summary to see what exists",
              file=sys.stderr)
        return 2

    r = extract_headers(a.db, cik=a.cik, limit=a.limit, delay=a.delay,
                        only_missing=not a.refresh)
    print("\n--- header read complete --------------------------------------")
    print(f"  stored            {r['stored']:,}")
    print(f"  no block for cik  {r['missing']:,}")
    print(f"  failed            {r['failed']:,}")
    print("---------------------------------------------------------------")
    return 0


def cmd_covertext(a) -> int:
    """Level 4 source C — read the printed cover for a second office."""
    if a.summary:
        s = covertext_summary(a.db)
        if not s["built"]:
            print("no cover pages read yet — "
                  "run: cli covertext --run --candidates")
            return 1
        print(f"  cover_office      {s['rows']:,} rows, {s['ciks']:,} companies")
        print(f"  dual offices      {s['dual']:,}")
        return 0

    if not a.run:
        print("pass --run (with --cik or --candidates), or --summary",
              file=sys.stderr)
        return 2
    if not (a.cik or a.candidates):
        print("this fetches a FULL document per filing — scope it with "
              "--cik or --candidates", file=sys.stderr)
        return 2

    r = extract_cover_offices(a.db, cik=a.cik, candidates=a.candidates,
                              limit=a.limit, delay=a.delay, refresh=a.refresh)
    print("\n--- cover pages read ------------------------------------------")
    print(f"  stored            {r['stored']:,}")
    print(f"  dual offices      {r['dual']:,}")
    print(f"  no cover marker   {r['skipped']:,}")
    print(f"  failed            {r['failed']:,}")
    print("---------------------------------------------------------------")
    print("Re-run `resolve --final` to fold these into entity_final.")
    return 0


def cmd_resolve(a) -> int:
    """Level 4 — one trusted current value per company per field."""
    if a.conflicts:
        rows = conflicts(a.db, limit=a.limit or 50)
        if not rows:
            print("no conflicts (or nothing resolved yet)")
            return 0
        print(f"{len(rows)} field(s) where the sources genuinely disagree\n")
        for r in rows:
            print(f"  cik {r['cik']:<9} {str(r['name'] or '')[:30]:<32} "
                  f"{r['field']}")
            print(f"     {r['source']:<12} {str(r['value'])[:30]:<32} "
                  f"{r['source_form']} {r['as_of']}   <- trusted")
            print(f"     {r['alt_source']:<12} {str(r['alt_value'])[:30]:<32} "
                  f"{r['alt_as_of'] or ''}")
        return 0

    if a.summary:
        s = resolved_summary(a.db)
        if not s["built"]:
            print("nothing resolved yet — run: cli resolve --build")
            return 1
        print(f"  entity_resolved   {s['rows']:,} fields, "
              f"{s['companies']:,} companies")
        for st, n in sorted(s["by_status"].items(), key=lambda kv: -kv[1]):
            print(f"      {st:<12} {n:,}")
        return 0

    if not (a.build or a.final):
        print("pass --build, --final, --summary or --conflicts",
              file=sys.stderr)
        return 2

    # --final implies --build unless the caller asked for only one: the
    # decoded table is derived from the evidence table, so a stale evidence
    # table would silently produce a stale conclusion.
    if a.build:
        build_resolved(a.db, cik=a.cik)
    if a.final or a.build:
        print()
        build_final(a.db)
        print()
        build_security_final(a.db)
        print("\nFinalized, decoded, with confidence:")
        print("  SELECT incorporation_state, incorporation_country,")
        print("         hq_city, hq_state, hq_country,")
        print("         incorporation_conf, hq_conf")
        print("    FROM entity_final WHERE cik = ?;")
        print("  SELECT security_class, trading_symbol, shares_outstanding,")
        print("         shares_as_of, shares_conf")
        print("    FROM security_final WHERE cik = ?;")
    return 0


def cmd_extract(a) -> int:
    if not (a.queue or a.run):
        a.queue = a.run = True          # the common case: queue then work it

    # Validate the spec before queueing, so a typo cannot burn a long run.
    try:
        resolve_fields(a.facts, a.db)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if a.queue:
        queue_candidates(a.db, include_424b2=a.include_424b2)
        print()

    if a.run:
        r = extract_xbrl(a.db, limit=a.limit, batch_size=a.batch_size,
                         delay=a.delay, retry_failed=a.retry_failed,
                         facts=a.facts)
        print("\n--- extraction complete ---------------------------------------")
        print(f"  run id          {r['run_id']}  ({r['status']})")
        print(f"  extracted       {r['done']:,} filings, {r['facts']:,} facts")
        print(f"  no inline XBRL  {r['no_xbrl']:,}")
        print(f"  failed          {r['failed']:,}")
        print("---------------------------------------------------------------")
        if r["failed"]:
            print("Retry with:  python -m finalized.cli extract --run --retry-failed")
        return 0 if r["status"] == "completed" else 1
    return 0


def cmd_status(a) -> int:
    with FilingDB(a.db) as db:
        s = db.summary()
        x = db.extraction_summary("xbrl")
        runs = db.recent_runs(a.runs)
        failed = db.failed_idx_dates()

    print("── ingest ───────────────────────────────────────────────")
    print(f"  filings           {s['filings']:,}")
    print(f"  filing dates      {s['filing_date_min']} .. {s['filing_date_max']}")
    print(f"  watermark         {s['watermark']}")
    print(f"  index days        {s['idx_parsed']:,} parsed, "
          f"{s['idx_failed']} failed, {s['idx_pending']} pending")
    print(f"  enriched filings  {s['enriched']:,}")

    print("\n── extraction ───────────────────────────────────────────")
    print(f"  queued            {x['queued']:,}")
    for status, n in sorted(x["by_status"].items()):
        print(f"      {status:<10} {n:,}")
    print(f"  facts             {x['facts']:,} across "
          f"{x['filings_with_facts']:,} filings")

    # A lean run must never look like a complete one, so surface which field
    # group each batch of filings was stored under.
    with FilingDB(a.db) as db:
        groups = db.conn.execute(
            """SELECT COALESCE(fact_group, 'all') g, COUNT(*) n
               FROM extraction_queue WHERE extractor='xbrl' AND status='done'
               GROUP BY g ORDER BY n DESC""").fetchall()
    if len(groups) > 1 or (groups and groups[0]["g"] != "all"):
        print("  stored under      " + ", ".join(
            f"{r['g']} ({r['n']:,})" for r in groups))

    cs = cover_summary(a.db)
    print("\n── cover tables (level 3.5) ─────────────────────────────")
    if not cs["built"]:
        print("  not built — run: python -m finalized.cli cover --build")
    else:
        print(f"  entity_cover      {cs['entities']:,}")
        print(f"  security_cover    {cs['securities']:,}")
        print(f"  equity rows       {cs['by_type'].get('equity', 0):,} "
              f"({cs['multi_class_companies']:,} multi-class companies)")

    hs = header_summary(a.db)
    rs = resolved_summary(a.db)
    cts = covertext_summary(a.db)
    if hs["built"] or rs["built"] or cts["built"]:
        print("\n── trusted values (level 4) ─────────────────────────────")
        if hs["built"]:
            print(f"  header_cover      {hs['rows']:,} rows "
                  f"({hs['with_incorporation']:,} with incorporation)")
        if cts["built"]:
            print(f"  cover_office      {cts['rows']:,} pages read, "
                  f"{cts['dual']:,} with two offices")
        if rs["built"]:
            print(f"  entity_resolved   {rs['rows']:,} fields, "
                  f"{rs['companies']:,} companies")
            n_conf = rs["by_status"].get("conflict", 0)
            print(f"  needs review      {n_conf:,} conflict(s)"
                  + ("  -> cli resolve --conflicts" if n_conf else ""))

    if failed:
        print(f"\n── failed index dates ({len(failed)}) ───────────────────")
        for f in failed[:10]:
            print(f"  {f['idx_date']}  attempts={f['attempts']}  {str(f['error'])[:60]}")

    print("\n── recent runs ──────────────────────────────────────────")
    for r in runs:
        print(f"  {r['run_id']:>4}  {r['run_type']:<12} {r['status']:<10} "
              f"{r['started_at']}  +{r['filings_inserted']:,}")
    return 0


def cmd_backup(a) -> int:
    if a.list:
        rows = list_backups(a.db)
        if not rows:
            print("no backups found")
        for b in rows:
            print(f"  {b.created}  {b.size_mb:>8,.1f} MB  {b.name}")
        return 0

    if a.verify:
        ok, detail = verify_backup(a.verify)
        print(f"{'OK  ' if ok else 'BAD '} {a.verify}\n     {detail}")
        return 0 if ok else 1

    if a.restore:
        safety = restore_backup(a.restore, a.db)
        print(f"restored {a.restore} -> {a.db}")
        if safety:
            print(f"previous database kept at {safety}")
        return 0

    if a.prune is not None:
        removed = prune_backups(a.db, keep=a.prune)
        print(f"removed {len(removed)} old backup(s); kept {a.prune}")
        for name in removed:
            print(f"  - {name}")
        return 0

    def _progress(done, total):
        if total and done % (2048 * 20) == 0:
            print(f"  {100 * done / total:5.1f}%", end="\r", flush=True)

    path = backup_database(a.db, progress=_progress)
    ok, detail = verify_backup(path)
    print(f"backup written: {path}")
    print(f"verify: {'OK' if ok else 'FAILED'} — {detail}")
    return 0 if ok else 1


# ── parser ────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    common = _common()
    p = argparse.ArgumentParser(
        prog="python -m finalized.cli",
        description="SEC EDGAR filing mirror and XBRL fact extraction.")
    sub = p.add_subparsers(dest="command", required=True)

    b = sub.add_parser("bootstrap", parents=[common],
                       help="Backfill an explicit date range.")
    b.add_argument("--start", type=_valid_date, required=True)
    b.add_argument("--end", type=_valid_date, required=True)
    b.add_argument("--enrich", action="store_true",
                   help="Also read each filing's header (slow; 1 req/filing).")
    b.add_argument("--no-tickers", action="store_true")
    b.set_defaults(func=cmd_bootstrap)

    s = sub.add_parser("sync", parents=[common],
                       help="Catch up from the watermark to today.")
    s.add_argument("--start", type=_valid_date, default=None,
                   help="Seed date, required only on an empty database.")
    s.add_argument("--end", type=_valid_date, default=None)
    s.add_argument("--enrich", action="store_true")
    s.add_argument("--no-tickers", action="store_true")
    s.set_defaults(func=cmd_sync)

    e = sub.add_parser("enrich", parents=[common],
                       help="Fill header-only fields (1 request per filing).")
    e.add_argument("--forms", default=None,
                   help="Comma-separated form types, e.g. 10-K,20-F,40-F.")
    e.add_argument("--since", type=_valid_date, default=None)
    e.add_argument("--until", type=_valid_date, default=None)
    e.add_argument("--limit", type=int, default=None)
    e.set_defaults(func=cmd_enrich)

    x = sub.add_parser("extract", parents=[common],
                       help="Queue filings and pull their XBRL facts.")
    x.add_argument("--queue", action="store_true",
                   help="Select candidates into the queue (no network).")
    x.add_argument("--run", action="store_true",
                   help="Work the queue. Both default to on if neither given.")
    x.add_argument("--limit", type=int, default=None)
    x.add_argument("--batch-size", type=int, default=100)
    x.add_argument("--retry-failed", action="store_true")
    x.add_argument("--include-424b2", action="store_true",
                   help="Include 424B2 pricing supplements (221k rows).")
    x.add_argument("--facts", default=None, metavar="SPEC",
                   help="Which facts to store: 'all' (default), a field group "
                        "such as cover / headquarters / shares, a comma-"
                        "separated combination, or bare CamelCase concept "
                        "names. See: cli facts --list.")
    x.set_defaults(func=cmd_extract)

    fa = sub.add_parser("facts", parents=[common],
                        help="List / inspect the field groups --facts accepts.")
    fa.add_argument("--list", action="store_true",
                    help="List every group (the default action).")
    fa.add_argument("--show", metavar="GROUP",
                    help="Print the concepts in one group.")
    fa.add_argument("--coverage", metavar="GROUP",
                    help="How often each concept appears in extracted facts.")
    fa.add_argument("--define", metavar="NAME=C1,C2",
                    help="Create a custom group.")
    fa.add_argument("--description", default=None,
                    help="Description for --define.")
    fa.add_argument("--delete", metavar="NAME", help="Delete a custom group.")
    fa.set_defaults(func=cmd_facts)

    cv = sub.add_parser("cover", parents=[common],
                        help="Level 3.5: build entity_cover + security_cover.")
    cv.add_argument("--build", action="store_true",
                    help="Rebuild both tables from stored facts (no network).")
    cv.add_argument("--summary", action="store_true",
                    help="Show what is already built.")
    cv.add_argument("--cik", default=None, help="Build one company only.")
    cv.add_argument("--limit", type=int, default=None)
    cv.set_defaults(func=cmd_cover)

    hd = sub.add_parser("headers", parents=[common],
                        help="Level 4: HQ + incorporation from SGML headers.")
    hd.add_argument("--run", action="store_true",
                    help="Read headers (one small range request per filing).")
    hd.add_argument("--summary", action="store_true",
                    help="Show what has been read.")
    hd.add_argument("--cik", default=None, help="One company only.")
    hd.add_argument("--limit", type=int, default=None,
                    help="Stop after N filings. Always scope a first run.")
    hd.add_argument("--refresh", action="store_true",
                    help="Re-read filings whose header is already stored.")
    hd.set_defaults(func=cmd_headers)

    ct = sub.add_parser("covertext", parents=[common],
                        help="Level 4: read the printed cover for a second "
                             "principal executive office.")
    ct.add_argument("--run", action="store_true",
                    help="Read cover pages (a FULL document fetch each).")
    ct.add_argument("--summary", action="store_true")
    ct.add_argument("--cik", default=None, help="One company only.")
    ct.add_argument("--candidates", action="store_true",
                    help="Work the dual-HQ candidate list from entity_final.")
    ct.add_argument("--limit", type=int, default=None)
    ct.add_argument("--refresh", action="store_true",
                    help="Re-read filings already stored.")
    ct.set_defaults(func=cmd_covertext)

    rs = sub.add_parser("resolve", parents=[common],
                        help="Level 4: trusted current value per company/field.")
    rs.add_argument("--build", action="store_true",
                    help="Rebuild entity_resolved, then entity_final "
                         "(database only, no network).")
    rs.add_argument("--final", action="store_true",
                    help="Rebuild only entity_final: decoded full names + "
                         "confidence, from existing evidence.")
    rs.add_argument("--summary", action="store_true")
    rs.add_argument("--conflicts", action="store_true",
                    help="List fields where the two sources disagree.")
    rs.add_argument("--cik", default=None, help="One company only.")
    rs.add_argument("--limit", type=int, default=None)
    rs.set_defaults(func=cmd_resolve)

    st = sub.add_parser("status", parents=[common], help="Read-only audit view.")
    st.add_argument("--runs", type=int, default=8)
    st.set_defaults(func=cmd_status)

    bk = sub.add_parser("backup", parents=[common],
                        help="Snapshot / list / verify / restore / prune.")
    g = bk.add_mutually_exclusive_group()
    g.add_argument("--list", action="store_true", help="List existing backups.")
    g.add_argument("--verify", metavar="PATH", help="Integrity-check a backup.")
    g.add_argument("--restore", metavar="PATH", help="Restore a backup in place.")
    g.add_argument("--prune", type=int, metavar="KEEP",
                   help="Delete all but the KEEP newest backups.")
    bk.set_defaults(func=cmd_backup)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
