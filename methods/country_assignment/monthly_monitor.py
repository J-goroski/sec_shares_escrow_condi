"""
monthly_monitor.py — monthly country-assignment monitoring.

For each CIK it builds one consolidated **country assignment** by combining:

  * the **EDGAR profile** (``edgar_profile.fetch_company_profile``) — the current,
    authoritative incorporation code + business address; and
  * a **validation pass over the latest filing** — manual extraction of the
    cover page + SEC-HEADER of the most recent filing of *any* form.  This
    covers **6-K and other non-XBRL forms**, and the cases where the latest
    filing carries only a principal executive office and no incorporation
    (incorporation then comes from the profile).

Each monthly run writes a timestamped **snapshot** and diffs it against the
previous month's snapshot to report **changes / differences / updates**.  (You
then compare the snapshot against your own database separately.)

Recommended monthly flow
------------------------
    from methods.country_assignment.monthly_monitor import run_monthly
    snap, changes = run_monthly(CIKS, out_dir="country_assignment/snapshots")
    # snap     — this month's assignments (also written to CSV)
    # changes  — rows that differ from last month's snapshot

For a whole-market run, pull ``submissions.zip`` once (see edgar_profile
``iter_bulk_profiles``) and build assignments profile-only (``validate_with_filing=False``)
for speed, then deep-validate just the changed CIKs.
"""

from __future__ import annotations

import glob
import os
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional

import pandas as pd
import requests

from methods.sec_filings_sync import UA, DEFAULT_DELAY, SECBlockedError
from methods.sec_filing_manual_extract import (
    fetch_submission_text, clean_submission_html, _html_to_text,
)
from methods.country_assignment.codes import (
    decode_code, country_of, text_to_country, ISO_COLLISION,
)
from methods.country_assignment.edgar_profile import fetch_company_profile
from methods.country_assignment.incorporation_validate import (
    parse_sec_header, extract_cover_incorporation_address, _norm_name, _same_name,
)

__all__ = [
    "CountryAssignment", "build_assignment", "run_monthly",
    "diff_snapshots", "load_latest_snapshot",
]


# ── Result record ─────────────────────────────────────────────────────────────

@dataclass
class CountryAssignment:
    cik: str
    entity_name: Optional[str] = None
    ticker: Optional[str] = None

    # incorporation (base = EDGAR profile, cross-checked with the latest filing)
    incorporation_code: Optional[str] = None
    incorporation_name: Optional[str] = None
    incorporation_country: Optional[str] = None
    incorporation_agreement: str = "unknown"     # agree | mismatch | unconfirmed
    iso_trap: Optional[str] = None

    # principal executive office
    hq_address: Optional[str] = None
    hq_country: Optional[str] = None
    hq_source: Optional[str] = None              # edgar_profile | filing_cover
    dual_hq: bool = False
    dual_hq_evidence: Optional[str] = None

    # provenance — latest_* is the latest COVER-BEARING filing we validated
    # against; newest_form is the very latest filing of any form (informational).
    latest_form: Optional[str] = None
    latest_filing_date: Optional[str] = None
    latest_is_xbrl: Optional[bool] = None
    newest_form: Optional[str] = None
    manual_extraction_used: bool = False
    incorporation_in_latest_filing: Optional[bool] = None

    status: str = "ok"                           # ok | review
    issues: list = field(default_factory=list)
    pulled_at: Optional[str] = None

    def as_row(self) -> dict:
        d = asdict(self)
        d["issues"] = "; ".join(self.issues) or None
        return d


# Columns diffed month-over-month, and how strictly.
_WATCH_EXACT = ["incorporation_code", "incorporation_country", "hq_country", "dual_hq"]
_WATCH_NORM  = ["hq_address"]


def _norm_addr(v) -> str:
    return re.sub(r"[^a-z0-9]", "", str(v or "").lower())


# ── Build one assignment ──────────────────────────────────────────────────────

def build_assignment(
    cik: str | int,
    session: requests.Session | None = None,
    delay: float = DEFAULT_DELAY,
    validate_with_filing: bool = True,
) -> CountryAssignment:
    """
    Build the consolidated country assignment for one CIK.

    ``validate_with_filing=False`` skips the (heavier) latest-filing download and
    returns the EDGAR-profile view only — fast, for whole-market monthly diffs.
    """
    prof = fetch_company_profile(cik, session=session, delay=delay)
    ca = CountryAssignment(
        cik=prof["cik"], entity_name=prof["entity_name"], ticker=prof["ticker"],
        pulled_at=datetime.now().isoformat(timespec="seconds"),
        latest_form=prof["latest_form"], latest_filing_date=prof["latest_filing_date"],
        latest_is_xbrl=prof["latest_is_xbrl"], newest_form=prof.get("newest_form"),
    )

    # --- incorporation: EDGAR profile is the authoritative base ---
    p_code = prof["incorporation_code"]
    p_dec  = decode_code(p_code)
    ca.incorporation_code = p_code
    ca.incorporation_name = prof["incorporation_name"]
    ca.incorporation_country = prof["incorporation_country"]
    if p_code and p_code in ISO_COLLISION and p_dec["kind"] in ("us_state", "ca_province"):
        ca.iso_trap = (f"code '{p_code}' = {p_dec['name']} ({p_dec['kind']}); "
                       f"a naive ISO read would wrongly say {ISO_COLLISION[p_code]}")

    # --- HQ: profile business address is the base ---
    ba = prof["business_address"] or {}
    ca.hq_address = ba.get("full")
    ca.hq_country = prof["hq_country"]
    ca.hq_source = "edgar_profile"

    # --- validate against the latest filing (covers 6-K / non-XBRL) ---
    header_code = cover_text = None
    if validate_with_filing and prof.get("latest_submission_txt_url"):
        ca.manual_extraction_used = True
        try:
            txt = fetch_submission_text(prof["latest_submission_txt_url"],
                                        session=session, delay=delay)
            header = parse_sec_header(txt)
            header_code = header.get("incorporation_code")
            clean, _, _ = clean_submission_html(txt, prof.get("latest_primary_document"))
            cover = extract_cover_incorporation_address(
                _html_to_text(clean)[:8000] if clean else "")
            cover_text = cover.get("incorporation_text")
            ca.incorporation_in_latest_filing = bool(cover_text)

            # principal office: prefer the filing cover when it lists address(es)
            if cover.get("addresses"):
                ca.hq_address = " | ".join(cover["addresses"])
                ca.hq_source = "filing_cover"
            ca.dual_hq = cover.get("dual_hq", False)
            ca.dual_hq_evidence = cover.get("dual_hq_evidence")
            if ca.dual_hq:
                ca.issues.append(f"dual headquarters — {ca.dual_hq_evidence}")
        except SECBlockedError:
            raise
        except Exception as exc:                        # noqa: BLE001
            ca.issues.append(f"latest-filing validation failed: {exc}")

    # --- fallback: if the profile carries no incorporation, take it from the
    #     latest filing (header code first, then the spelled cover text). This
    #     covers foreign issuers whose EDGAR profile leaves stateOfIncorporation
    #     blank (e.g. some 20-F filers). ---
    if not p_code:
        if header_code:
            hd = decode_code(header_code)
            ca.incorporation_code = header_code
            ca.incorporation_name = hd["name"]
            ca.incorporation_country = country_of(header_code)
            ca.issues.append(
                f"incorporation from latest-filing header (profile had none): {header_code}")
        elif cover_text:
            ca.incorporation_name = cover_text
            ca.incorporation_country = text_to_country(cover_text)
            ca.issues.append(
                f"incorporation from latest-filing cover (profile had none): {cover_text}")

    # --- reconcile incorporation across profile / header / cover text ---
    _reconcile_incorporation(ca, p_code, p_dec, header_code, cover_text)

    ca.status = "review" if (
        ca.incorporation_agreement in ("mismatch", "review")
        or ca.dual_hq
        or (ca.iso_trap and ca.incorporation_agreement != "agree")
    ) else "ok"
    return ca


def _reconcile_incorporation(ca, p_code, p_dec, header_code, cover_text) -> None:
    """Compare profile-code vs latest-filing header-code vs cover-text jurisdiction."""
    p = (p_code or "").strip().upper()
    h = (header_code or "").strip().upper()
    hd = decode_code(header_code)

    # 1. profile vs filing-header codes (same EDGAR scheme → compare directly)
    if p and h and p != h:
        pn = f" ({p_dec['name']})" if p_dec["name"] else ""
        hn = f" ({hd['name']})" if hd["name"] else ""
        ca.incorporation_agreement = "mismatch"
        ca.issues.append(
            f"profile incorporation {p}{pn} != latest-filing header {h}{hn}")
        return

    # 2. cover text vs profile — compared at COUNTRY level so a real disagreement
    #    (cover "Cayman Islands" vs profile K3=Hong Kong) is flagged, but mere
    #    wording (cover "England and Wales" vs profile X0=United Kingdom) is not.
    pnorm, tnorm = _norm_name(p_dec["name"]), _norm_name(cover_text)
    if pnorm and tnorm and not _same_name(pnorm, tnorm):
        pc, tc = country_of(p_code), text_to_country(cover_text)
        if pc and tc and pc == tc:
            ca.issues.append(f"note: latest-filing cover says '{cover_text}' "
                             f"(code {p} = {p_dec['name']}, same country)")
        else:
            ca.incorporation_agreement = "mismatch"
            extra = f" (cover country {tc} != profile country {pc})" if tc and pc else ""
            ca.issues.append(f"latest-filing cover says '{cover_text}' but profile "
                             f"code {p} = '{p_dec['name'] or '?'}'{extra}")
            return

    # 3. overall corroboration status
    names = [n for n in (pnorm, _norm_name(hd["name"]), tnorm) if n]
    if not names:
        ca.incorporation_agreement = "unknown"
    elif (p and h and p == h) or \
            (len(names) >= 2 and all(_same_name(a, b) for a in names for b in names)):
        ca.incorporation_agreement = "agree"
    elif len(names) == 1:
        ca.incorporation_agreement = "unconfirmed"
    else:
        ca.incorporation_agreement = "review"


# ── Monthly run + snapshots + diff ────────────────────────────────────────────

def _assignments_frame(assignments) -> pd.DataFrame:
    cols = list(CountryAssignment("").as_row().keys())
    return pd.DataFrame([a.as_row() for a in assignments], columns=cols)


def run_monthly(
    ciks,
    out_dir: str,
    month: str | None = None,
    session: requests.Session | None = None,
    delay: float = DEFAULT_DELAY,
    validate_with_filing: bool = True,
    verbose: bool = True,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Run the monthly monitor over ``ciks``: build assignments, write this month's
    snapshot to ``out_dir/country_assignment_<YYYY-MM>.csv``, diff against the
    most recent prior snapshot, and write ``out_dir/changes_<YYYY-MM>.csv``.

    Returns ``(snapshot_df, changes_df)``.
    """
    month = month or datetime.now().strftime("%Y-%m")
    os.makedirs(out_dir, exist_ok=True)

    own = session is None
    sess = session or requests.Session()
    if own:
        sess.headers.update(UA)

    assignments = []
    try:
        for c in ciks:
            try:
                ca = build_assignment(c, session=sess, delay=delay,
                                      validate_with_filing=validate_with_filing)
            except SECBlockedError:
                raise
            except Exception as exc:                    # noqa: BLE001
                if verbose:
                    print(f"[monitor] CIK {c} error: {exc}")
                continue
            assignments.append(ca)
            if verbose:
                flag = "OK    " if ca.status == "ok" else "REVIEW"
                print(f"[monitor] {ca.ticker or ca.cik:<8} {flag} "
                      f"inc={ca.incorporation_code}({ca.incorporation_country}) "
                      f"hq={ca.hq_country}{'  DUAL-HQ' if ca.dual_hq else ''}"
                      f"{'  ISO-TRAP' if ca.iso_trap else ''}")
    finally:
        if own:
            sess.close()

    snap = _assignments_frame(assignments)
    snap_path = os.path.join(out_dir, f"country_assignment_{month}.csv")
    snap.to_csv(snap_path, index=False)

    prior_path, prior = load_latest_snapshot(out_dir, before=month)
    changes = diff_snapshots(prior, snap) if prior is not None else pd.DataFrame(
        columns=["cik", "entity_name", "ticker", "change_type", "field",
                 "old_value", "new_value"])
    changes_path = os.path.join(out_dir, f"changes_{month}.csv")
    changes.to_csv(changes_path, index=False)

    if verbose:
        print(f"\n[monitor] snapshot -> {snap_path}  ({len(snap)} companies)")
        print(f"[monitor] changes  -> {changes_path}  "
              f"({len(changes)} field changes vs {os.path.basename(prior_path) if prior_path else 'n/a'})")
    return snap, changes


def load_latest_snapshot(out_dir: str, before: str | None = None):
    """Return ``(path, DataFrame)`` for the newest snapshot in ``out_dir`` whose
    month is strictly before ``before`` (or the newest overall if ``before`` is
    None).  Returns ``(None, None)`` if there is none."""
    paths = sorted(glob.glob(os.path.join(out_dir, "country_assignment_*.csv")))
    def _month(p):
        m = re.search(r"country_assignment_(\d{4}-\d{2})\.csv$", p)
        return m.group(1) if m else ""
    cand = [p for p in paths if not before or _month(p) < before]
    if not cand:
        return None, None
    path = cand[-1]
    return path, pd.read_csv(path, dtype=str, keep_default_na=False)


def diff_snapshots(
    prev: pd.DataFrame,
    curr: pd.DataFrame,
    key: str = "cik",
) -> pd.DataFrame:
    """
    Diff two snapshots on ``key`` → one row per (company, changed field).

    Reports NEW companies (added), REMOVED companies, and CHANGED watched fields
    (``incorporation_code``, ``incorporation_country``, ``hq_country``,
    ``dual_hq`` exactly; ``hq_address`` normalized to ignore formatting noise).
    """
    def _idx(df):
        return {str(r[key]): r for _, r in df.iterrows()} if len(df) else {}
    pi, ci = _idx(prev), _idx(curr)
    rows = []

    for k, r in ci.items():
        meta = {"cik": k, "entity_name": r.get("entity_name"),
                "ticker": r.get("ticker")}
        if k not in pi:
            rows.append({**meta, "change_type": "added", "field": None,
                         "old_value": None, "new_value": None})
            continue
        p = pi[k]
        for f in _WATCH_EXACT:
            ov, nv = str(p.get(f, "")), str(r.get(f, ""))
            if ov != nv:
                rows.append({**meta, "change_type": "changed", "field": f,
                             "old_value": ov, "new_value": nv})
        for f in _WATCH_NORM:
            ov, nv = p.get(f, ""), r.get(f, "")
            if _norm_addr(ov) != _norm_addr(nv):
                rows.append({**meta, "change_type": "changed", "field": f,
                             "old_value": ov, "new_value": nv})

    for k, p in pi.items():
        if k not in ci:
            rows.append({"cik": k, "entity_name": p.get("entity_name"),
                         "ticker": p.get("ticker"), "change_type": "removed",
                         "field": None, "old_value": None, "new_value": None})

    return pd.DataFrame(rows, columns=["cik", "entity_name", "ticker",
                                       "change_type", "field", "old_value",
                                       "new_value"])
