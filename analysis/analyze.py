"""
analyze.py — run the whole analysis pipeline for a list of companies.

For each ticker/CIK this fetches the company's latest annual report (10-K /
20-F / 40-F) ONCE, reuses its text and XBRL cover across all three engines,
and persists every derived fact (with evidence + provenance) to the research
store where the viewer's review queue picks it up:

    icb     ICB industry/supersector/sector/subsector classification
    voting  votes per share by share class (falls back to the DEF 14A when
            the annual report doesn't state voting rights — e.g. Coca-Cola)
    adr     ADR/ADS ratio(s) (cover title -> filing prose -> local LLM)

CLI
---
    python -m analysis.analyze META GOOGL KO SHEL
    python -m analysis.analyze 320193 --kinds icb,adr --no-llm
    python -m analysis.analyze SHEL --model gemma2:2b --db my.sqlite

All SEC traffic flows through the shared rate-limited fetcher; the local LLM
is optional (without Ollama the deterministic layers still run).
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import asdict
from typing import Optional

import requests

from methods.sec_filings_sync import (UA, SECBlockedError, _get_with_retry,
                                      _pad_cik, SUBMISSIONS_BASE,
                                      fetch_filings_for_ciks)
from methods.sec_xbrl_extract import parse_filings, cover_pages
from methods.sec_filing_manual_extract import process_submission

from .adr_ratio import adr_ratios_for_securities, extract_adr_ratio, _ADS_HINT
from .voting_rights import extract_voting_rights
from .icb_classify import classify_icb, business_description
from .research_store import ResearchDB

ANNUAL_FORMS = ("10-K", "20-F", "40-F")
ALL_KINDS = ("icb", "voting", "adr")

_CONF_RANK = {"high": 0, "medium": 1, "low": 2, "review": 3}


def _worst_confidence(values) -> str:
    """The least-confident label wins for the unit's headline confidence."""
    vals = [v or "medium" for v in values] or ["medium"]
    return max(vals, key=lambda v: _CONF_RANK.get(v, 1))

_ticker_map: dict[str, str] = {}


def _ascii(s: object) -> str:
    """Console-safe rendering (Windows cp1252 chokes on filing characters)."""
    return str(s).encode("ascii", "replace").decode()


def resolve_cik(query: str, session: requests.Session) -> Optional[str]:
    """'META' or '1326801' -> numeric CIK string."""
    q = str(query).strip()
    if q.isdigit():
        return str(int(q))
    global _ticker_map
    if not _ticker_map:
        data = _get_with_retry("https://www.sec.gov/files/company_tickers.json",
                               session, 0.12).json()
        _ticker_map = {v["ticker"].upper(): str(v["cik_str"])
                       for v in data.values()}
    return _ticker_map.get(q.upper())


def analyze_company(
    cik_or_ticker: str,
    kinds: tuple[str, ...] = ALL_KINDS,
    use_llm: bool = True,
    model: str | None = None,
    db: ResearchDB | None = None,
    session: requests.Session | None = None,
    clean_dir: str = "clean_html",
    verbose: bool = True,
) -> dict:
    """Run the requested engines for one company; persist + return results."""
    own_session = session is None
    if own_session:
        session = requests.Session()
        session.headers.update(UA)
    own_db = db is None
    if own_db:
        db = ResearchDB()

    out: dict = {"query": cik_or_ticker, "saved": {}}
    try:
        cik = resolve_cik(cik_or_ticker, session)
        if not cik:
            out["error"] = f"could not resolve '{cik_or_ticker}' to a CIK"
            return out
        out["cik"] = cik

        hdr = _get_with_retry(
            f"{SUBMISSIONS_BASE}/submissions/CIK{_pad_cik(cik)}.json",
            session, 0.12).json()
        name = hdr.get("name")
        tickers = hdr.get("tickers") or []
        ticker = tickers[0] if tickers else None
        sic, sic_desc = hdr.get("sic"), hdr.get("sicDescription")
        out.update(name=name, ticker=ticker, sic=sic)

        filings = fetch_filings_for_ciks(
            [cik], form_types=list(ANNUAL_FORMS) + ["DEF 14A"])
        annual = next((f for f in filings if f.form_type in ANNUAL_FORMS), None)
        proxy = next((f for f in filings if f.form_type == "DEF 14A"), None)
        if annual is None:
            out["error"] = "no annual report (10-K/20-F/40-F) on file"
            return out
        src = dict(source_form=annual.form_type,
                   source_accession=annual.accession_number,
                   source_filing_date=annual.filing_date)
        ident = dict(cik=cik, ticker=ticker, entity_name=name)

        # one text fetch, shared by every engine
        ext = process_submission(annual, session=session, save_dir=clean_dir,
                                 want_tables=False, want_statements=False)
        text = ext.text or ""

        # XBRL cover securities (for the ADR engine)
        security_df = None
        if annual.xbrl_instance_url:
            try:
                facts = parse_filings([annual], session=session, verbose=False)
                if not facts.empty:
                    _, security_df = cover_pages(facts)
            except Exception as exc:                     # noqa: BLE001
                if verbose:
                    print(f"  [cover] {_ascii(exc)}")

        if "icb" in kinds:
            desc = business_description(text, annual.form_type)
            c = classify_icb(name, sic, sic_desc, desc,
                             use_llm=use_llm, model=model)
            res = {k: v for k, v in asdict(c).items() if v is not None}
            out["icb"] = res
            out["saved"]["icb"] = db.save(
                "icb", cik, res, **{k: v for k, v in ident.items()
                                    if k != "cik"}, **src,
                evidence=c.evidence, method=c.method,
                model=(model or "auto") if c.method and "llm" in c.method else None,
                confidence=c.confidence)
            if verbose:
                print(f"  icb    {_ascii(c.path_display)}  [{c.confidence}]")

        if "voting" in kinds:
            rights = extract_voting_rights(text, use_llm=use_llm, model=model)
            v_src = src
            if not rights and proxy is not None:
                pext = process_submission(proxy, session=session,
                                          save_dir=clean_dir,
                                          want_tables=False,
                                          want_statements=False)
                rights = extract_voting_rights(pext.text or "",
                                               use_llm=use_llm, model=model)
                v_src = dict(source_form=proxy.form_type,
                             source_accession=proxy.accession_number,
                             source_filing_date=proxy.filing_date)
            if rights:
                res = [ {k: v for k, v in asdict(r).items()
                         if k != "evidence" and v is not None}
                        for r in rights ]
                conf = _worst_confidence(r.confidence for r in rights)
                out["voting"] = res
                out["saved"]["voting"] = db.save(
                    "voting", cik, res, **{k: v for k, v in ident.items()
                                           if k != "cik"}, **v_src,
                    evidence="\n".join(dict.fromkeys(
                        r.evidence for r in rights if r.evidence)),
                    method="+".join(sorted({r.method for r in rights
                                            if r.method})),
                    model=(model or "auto") if use_llm else None,
                    confidence=conf)
                if verbose:
                    for r in rights:
                        print(f"  voting {r.class_label}: "
                              f"{r.votes_per_share} [{r.confidence}]")
            elif verbose:
                print("  voting no statements found")

        if "adr" in kinds:
            adr_df = adr_ratios_for_securities(
                security_df,
                filing_texts={annual.accession_number: text},
                use_llm=use_llm, model=model) \
                if security_df is not None else None
            rows = adr_df.to_dict("records") if adr_df is not None and \
                not adr_df.empty else []
            # foreign filer with no ADS cover line: the ratio may still live
            # in the prose (TSM/AZN register only the underlying shares)
            if not rows and annual.form_type in ("20-F", "40-F"):
                r = extract_adr_ratio(filing_text=text, use_llm=use_llm,
                                      model=model)
                if r.is_ads and r.shares_per_ads is not None:
                    rows = [{**ident, **{"form_type": annual.form_type,
                             "filing_date": annual.filing_date,
                             "accession_number": annual.accession_number},
                             "security_title": None,
                             "shares_per_ads": r.shares_per_ads,
                             "ratio_display": r.ratio_display,
                             "underlying": r.underlying,
                             "method": r.method, "confidence": r.confidence,
                             "evidence": r.evidence}]
            if rows:
                keep = ("security_title", "shares_per_ads", "ratio_display",
                        "underlying", "method", "confidence", "trading_symbol",
                        "exchange")
                res = [{k: row.get(k) for k in keep
                        if row.get(k) is not None} for row in rows]
                out["adr"] = res
                out["saved"]["adr"] = db.save(
                    "adr", cik, res, **{k: v for k, v in ident.items()
                                        if k != "cik"}, **src,
                    evidence="\n".join(str(row.get("evidence"))
                                       for row in rows if row.get("evidence")),
                    method="+".join(sorted({str(row.get("method"))
                                            for row in rows
                                            if row.get("method")})) or None,
                    model=(model or "auto") if use_llm else None,
                    confidence=_worst_confidence(
                        row.get("confidence") for row in rows))
                if verbose:
                    for row in rows:
                        print(f"  adr    {_ascii(row.get('ratio_display') or 'ratio unknown')}"
                              f" [{row.get('confidence')}]")
            elif verbose:
                print("  adr    no ADS found")
        return out
    finally:
        if own_db:
            db.close()
        if own_session:
            session.close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Derive ICB / voting-rights / ADR facts for companies "
                    "and store them for analyst review.")
    p.add_argument("companies", nargs="+",
                   help="tickers or CIKs (e.g. META GOOGL KO SHEL)")
    p.add_argument("--kinds", default=",".join(ALL_KINDS),
                   help="comma list of icb,voting,adr (default: all)")
    p.add_argument("--no-llm", action="store_true",
                   help="deterministic layers only (no Ollama)")
    p.add_argument("--model", default=None, help="Ollama model override")
    p.add_argument("--db", default=None, help="research sqlite path")
    args = p.parse_args(argv)

    kinds = tuple(k.strip() for k in args.kinds.split(",") if k.strip())
    bad = [k for k in kinds if k not in ALL_KINDS]
    if bad:
        print(f"unknown kind(s): {', '.join(bad)}")
        return 2

    db = ResearchDB(args.db) if args.db else ResearchDB()
    session = requests.Session()
    session.headers.update(UA)
    failures = 0
    try:
        for q in args.companies:
            print(f"[analyze] {q}")
            try:
                res = analyze_company(q, kinds=kinds, use_llm=not args.no_llm,
                                      model=args.model, db=db, session=session)
            except SECBlockedError:
                print("SEC rate limit hit (10-minute cool-off) - stopping.")
                return 3
            except Exception as exc:                    # noqa: BLE001
                print(f"  error: {_ascii(exc)}")
                failures += 1
                continue
            if res.get("error"):
                print(f"  error: {_ascii(res['error'])}")
                failures += 1
    finally:
        db.close()
        session.close()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
