"""
edgar_profile.py — pull the CURRENT EDGAR registrant profile.

The EDGAR *submissions* API (``https://data.sec.gov/submissions/CIK##########.json``)
is the real-time authoritative registrant record: it carries the company's
current ``stateOfIncorporation`` (EDGAR code) and business/mailing ``addresses``
as EDGAR holds them *today* — this is where a redomicile or an HQ move actually
shows up, and it needs no filing parsing.  It is therefore the right **primary**
source for a monthly accuracy pull.

  * One request per CIK for a watchlist (``fetch_company_profile`` / ``fetch_profiles``).
  * For the whole market, the SEC also publishes a nightly bulk dump of every
    company's submissions JSON at
    ``https://www.sec.gov/Archives/edgar/daily-index/bulkdata/submissions.zip`` —
    download it once per month and iterate locally (see ``iter_bulk_profiles``)
    instead of hitting the per-CIK endpoint thousands of times.

All incorporation/address codes are decoded with the EDGAR (not ISO) tables in
``codes.py``.  The profile also surfaces the **latest filing of any form**
(including 6-K and other non-XBRL forms) so the monitor can manually validate the
cover against this profile.
"""

from __future__ import annotations

import json
import zipfile
from typing import Iterator, Optional

import pandas as pd
import requests

from methods.sec_filings_sync import (
    UA, DEFAULT_DELAY, SECBlockedError,
    _get_with_retry, _pad_cik, SUBMISSIONS_BASE, _parse_recent_filings,
)
from methods.country_assignment.codes import decode_code, country_of

__all__ = [
    "fetch_company_profile", "fetch_profiles",
    "profile_from_submissions", "iter_bulk_profiles",
]

# Forms whose cover we can validate (incl. amendments via str.startswith).
# We prefer the latest ANNUAL report (fullest cover: incorporation + dual-HQ +
# full address); if none is on record we fall back to the latest cover-bearing
# filing — 6-K included, so a foreign issuer's latest interim report is used.
# Form 3/4/5, 11-K, ARS, SC 13*, 424B etc. are intentionally excluded.
_ANNUAL_FORM_PREFIXES = ("10-K", "20-F", "40-F")
_COVER_FORM_PREFIXES  = ("10-K", "10-Q", "20-F", "40-F", "8-K", "6-K")


def _session(session: requests.Session | None) -> tuple[requests.Session, bool]:
    if session is not None:
        return session, False
    s = requests.Session()
    s.headers.update(UA)
    return s, True


def _fmt_address(a: dict | None) -> dict:
    """Normalise an EDGAR address block → {full, street1, street2, city,
    state_code, state_name, zip, country}."""
    a = a or {}
    code = (a.get("stateOrCountry") or "").strip() or None
    desc = (a.get("stateOrCountryDescription") or "").strip() or None
    country = country_of(code) or desc
    parts = [a.get("street1"), a.get("street2"), a.get("city"),
             desc or code, a.get("zipCode")]
    full = ", ".join(str(p) for p in parts if p) or None
    return {
        "full": full,
        "street1": a.get("street1") or None,
        "street2": a.get("street2") or None,
        "city": a.get("city") or None,
        "state_code": code,
        "state_name": desc,
        "zip": a.get("zipCode") or None,
        "country": country,
    }


def profile_from_submissions(data: dict) -> dict:
    """
    Build a country-assignment profile dict from an already-fetched submissions
    JSON payload (pure, no network — also used by ``iter_bulk_profiles``).
    """
    cik_int = int(str(data.get("cik") or "0").lstrip("0") or "0")
    name = data.get("name", "")
    tickers = data.get("tickers") or []
    ticker = tickers[0] if tickers else ""

    inc_code = (data.get("stateOfIncorporation") or "").strip() or None
    dec = decode_code(inc_code)

    business = _fmt_address((data.get("addresses") or {}).get("business"))
    mailing  = _fmt_address((data.get("addresses") or {}).get("mailing"))

    # Two "latest" notions from the recent filings (newest-first):
    #   * newest  — the very latest filing of ANY form (may be a Form 3/4, ARS…)
    #   * latest  — the latest COVER-BEARING filing (10-K/10-Q/20-F/40-F/8-K/6-K
    #     + amendments) whose cover we can manually validate.  This is the "expand
    #     to 6-K" target: for a foreign issuer that just filed a 6-K, it's the 6-K.
    newest = latest = None
    try:
        recs = _parse_recent_filings(data, cik_int, name, ticker, form_filter=set())
        newest = recs[0] if recs else None
        annual = next((r for r in recs if str(r.form_type).upper()
                       .startswith(_ANNUAL_FORM_PREFIXES)), None)
        cover  = next((r for r in recs if str(r.form_type).upper()
                       .startswith(_COVER_FORM_PREFIXES)), None)
        latest = annual or cover          # prefer the richest (annual) cover
    except Exception:                                   # noqa: BLE001
        pass

    former = [f.get("name") for f in (data.get("formerNames") or []) if f.get("name")]

    return {
        "cik": str(cik_int),
        "entity_name": name,
        "tickers": tickers,
        "ticker": ticker,
        "exchanges": data.get("exchanges") or [],
        "entity_type": data.get("entityType"),
        "sic": data.get("sic"),
        "sic_description": data.get("sicDescription"),
        "category": data.get("category"),
        "fiscal_year_end": data.get("fiscalYearEnd"),
        "ein": data.get("ein"),
        "former_names": former,

        # incorporation (authoritative current value, EDGAR-decoded)
        "incorporation_code": inc_code,
        "incorporation_name": dec["name"] or (data.get("stateOfIncorporationDescription") or None),
        "incorporation_kind": dec["kind"],
        "incorporation_country": country_of(inc_code),
        "incorporation_desc_sec": data.get("stateOfIncorporationDescription") or None,

        # principal office (business address) + mailing
        "business_address": business,
        "mailing_address": mailing,
        "hq_country": business.get("country"),
        "phone": data.get("phone"),

        # latest COVER-BEARING filing (for manual validation / non-XBRL forms)
        "latest_form": getattr(latest, "form_type", None),
        "latest_filing_date": getattr(latest, "filing_date", None),
        "latest_accession": getattr(latest, "accession_number", None),
        "latest_primary_document": getattr(latest, "primary_document", None),
        "latest_is_xbrl": getattr(latest, "is_xbrl", None),
        "latest_submission_txt_url": getattr(latest, "submission_txt_url", None),
        "latest_xbrl_instance_url": getattr(latest, "xbrl_instance_url", None),
        "latest_filing_detail_url": getattr(latest, "filing_detail_url", None),
        "_latest_record": latest,          # FilingRecord, for the monitor

        # the very newest filing of any form (informational only)
        "newest_form": getattr(newest, "form_type", None),
        "newest_filing_date": getattr(newest, "filing_date", None),
    }


def fetch_company_profile(
    cik: str | int,
    session: requests.Session | None = None,
    delay: float = DEFAULT_DELAY,
) -> dict:
    """Fetch + build the current EDGAR profile for one CIK (one HTTP request)."""
    sess, owned = _session(session)
    try:
        url = f"{SUBMISSIONS_BASE}/submissions/CIK{_pad_cik(cik)}.json"
        data = _get_with_retry(url, sess, delay).json()
        prof = profile_from_submissions(data)
        prof["profile_source_url"] = url
        return prof
    finally:
        if owned:
            sess.close()


# Columns worth keeping when flattening many profiles to a DataFrame.
_FLAT_COLS = [
    "cik", "entity_name", "ticker", "incorporation_code", "incorporation_name",
    "incorporation_kind", "incorporation_country", "incorporation_desc_sec",
    "hq_country", "phone", "sic_description", "category", "entity_type",
    "latest_form", "latest_filing_date", "latest_is_xbrl", "newest_form",
]


def fetch_profiles(
    ciks,
    session: requests.Session | None = None,
    delay: float = DEFAULT_DELAY,
    verbose: bool = True,
) -> pd.DataFrame:
    """
    Fetch current EDGAR profiles for many CIKs → one tidy row per CIK.

    ``SECBlockedError`` propagates so the caller can stop and wait out the ban.
    """
    sess, owned = _session(session)
    rows = []
    try:
        for c in ciks:
            try:
                p = fetch_company_profile(c, session=sess, delay=delay)
            except SECBlockedError:
                raise
            except Exception as exc:                    # noqa: BLE001
                if verbose:
                    print(f"[profile] CIK {c} error: {exc}")
                continue
            flat = {k: p.get(k) for k in _FLAT_COLS}
            flat["business_address"] = (p.get("business_address") or {}).get("full")
            rows.append(flat)
            if verbose:
                print(f"[profile] {p.get('ticker') or p.get('cik'):<8} "
                      f"{p.get('entity_name','')[:32]:<32} "
                      f"inc={p.get('incorporation_code')}"
                      f"({p.get('incorporation_country')}) hq={p.get('hq_country')}")
    finally:
        if owned:
            sess.close()
    return pd.DataFrame(rows, columns=_FLAT_COLS + ["business_address"])


def iter_bulk_profiles(zip_path: str) -> Iterator[dict]:
    """
    Iterate country-assignment profiles from a downloaded EDGAR
    ``submissions.zip`` bulk file — the efficient path for a whole-market
    monthly run (no per-CIK requests).

    Each entry ``CIK##########.json`` is parsed with ``profile_from_submissions``.
    Sub-files (``CIK*-submissions-001.json`` continuation shards) are skipped —
    they hold only older filings, not the current profile.
    """
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            fn = info.filename
            if not (fn.startswith("CIK") and fn.endswith(".json")):
                continue
            if "submissions-" in fn:                    # continuation shard
                continue
            try:
                with zf.open(info) as fh:
                    data = json.load(fh)
                yield profile_from_submissions(data)
            except Exception:                           # noqa: BLE001
                continue
