"""
sec_extractors.py — downstream data extractors for SEC EDGAR FilingRecords.

Public API
----------
extract_shares(record, session=None, delay=DEFAULT_DELAY) -> list[SharesResult]
    Extract all share classes (outstanding + issued) from a single FilingRecord.
    Returns one SharesResult per class — e.g. 3 rows for Alphabet (A/B/C).

    Extraction hierarchy:
    1. Inline iXBRL tags in the primary document  (most accurate; handles scale)
    2. Text regex on the cover-page body text      (fallback for non-iXBRL)

extract_company_locations(record, session=None, delay=DEFAULT_DELAY) -> LocationResult
    Extract state of incorporation and principal HQ address.
    Returns the business / principal-executive-offices address, NOT mailing.

    Extraction hierarchy:
    1. Inline iXBRL DEI tags in the primary document
    2. Text regex on the cover-page body text

batch_extract_shares(records, delay=DEFAULT_DELAY) -> list[SharesResult]
    Batch version — flat list, multiple rows per filing for multi-class companies.

batch_extract_locations(records, delay=DEFAULT_DELAY) -> list[LocationResult]
    Batch version — one LocationResult per FilingRecord.

extract_escrow_shares(record, session=None, delay=DEFAULT_DELAY) -> list[EscrowSharesResult]
    Scan a single filing for escrow/contingent/earnout/founder share disclosures.
    Returns one EscrowSharesResult per distinct finding (empty list if none found).
    Uses trigger-keyword scanning with proximity-anchored share matching and
    placement-verb validation — no external API.

batch_extract_escrow_shares(records, delay=DEFAULT_DELAY) -> list[EscrowSharesResult]
    Batch version — flat list across all filings, reuses one HTTP session.

SEC rate limit: 10 req/s.  DEFAULT_DELAY (0.11 s) → ~9 req/s with headroom.
429 responses are retried automatically with exponential back-off.
"""

import re
import time
import requests
from dataclasses import dataclass
from typing import Optional
from bs4 import BeautifulSoup

from sec_filings_sync import FilingRecord, UA, _pad_cik, _get_with_retry, DEFAULT_DELAY


# ── Result types ──────────────────────────────────────────────────────────────

@dataclass
class SharesResult:
    cik: str
    entity_name: str
    form_type: str
    filing_date: str
    accession_number: str
    share_class: str          # e.g. "Class A Common Stock", "Common Stock"
    shares_outstanding: Optional[int]
    shares_issued: Optional[int]
    source: str               # "ixbrl" | "text" | "none"
    notes: Optional[str]


@dataclass
class LocationResult:
    cik: str
    entity_name: str
    form_type: str
    filing_date: str
    accession_number: str
    state_of_incorporation: Optional[str]
    country_of_incorporation: Optional[str]
    hq_address1: Optional[str]
    hq_address2: Optional[str]
    hq_city: Optional[str]
    hq_state: Optional[str]
    hq_zip: Optional[str]
    hq_country: Optional[str]
    source: str               # "ixbrl" | "text" | "ixbrl+text" | "none"
    notes: Optional[str]


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(UA)
    return s


def _fetch_html(url: str, session: requests.Session, delay: float) -> str:
    return _get_with_retry(url, session, delay).text


# ── iXBRL helpers — shared by shares and location extraction ─────────────────

def _parse_ixbrl_numeric(tag) -> Optional[int]:
    """
    Parse a numeric value from an inline XBRL tag, applying the scale attribute.

    SEC filings often use scale=6 (millions) so "5,822" in the tag text
    represents 5,822,000,000 shares.  scale=0 / omitted means the value
    is literal.
    """
    raw = tag.get_text(strip=True).replace(",", "").replace(" ", "")
    if not raw:
        return None
    try:
        scale = int(tag.get("scale", 0) or 0)
        val = round(float(raw) * (10 ** scale))
        return int(val) if val >= 0 else None
    except (ValueError, TypeError):
        return None


def _get_dim_member(soup: BeautifulSoup, ctx_id: str) -> Optional[str]:
    """
    Resolve an XBRL contextRef ID to its explicit-member value (the dimension value
    that identifies the share class, e.g. "us-gaap:CommonClassAMember").

    Returns None when the context has no segment / dimension (i.e. the filing
    does not distinguish share classes via XBRL dimensions).

    Uses regex on the raw element string because html.parser may not walk into
    namespace-prefixed XML child tags (xbrldi:explicitMember) reliably.
    """
    if not ctx_id:
        return None
    ctx_el = soup.find(id=ctx_id)
    if not ctx_el:
        return None
    m = re.search(
        r"<[^>]*explicitmember[^>]*>\s*([^<]+?)\s*</",
        str(ctx_el),
        re.IGNORECASE,
    )
    return m.group(1).strip() if m else None


# XBRL class-of-stock member → human-readable label
_MEMBER_MAP = {
    "commonclassamember":  "Class A Common Stock",
    "commonclassbmember":  "Class B Common Stock",
    "commonclasscmember":  "Class C Common Stock",
    "commonclassdmember":  "Class D Common Stock",
    "commonstockmember":   "Common Stock",
    "capitalclasscmember": "Class C Capital Stock",
}


def _member_to_label(member_val: str) -> str:
    """Map a raw XBRL member string to a display-friendly class label."""
    # Strip namespace prefix: "us-gaap:CommonClassAMember" → "CommonClassAMember"
    local = member_val.split(":")[-1] if ":" in member_val else member_val
    key = re.sub(r"[^a-z]", "", local.lower())
    if key in _MEMBER_MAP:
        return _MEMBER_MAP[key]
    # Generic: extract a class letter from "CommonClassAMember", "CapitalClassCMember"
    m = re.search(r"(?:class|series)\s*([A-Z])", local, re.I)
    if m:
        letter = m.group(1).upper()
        if "apital" in local:
            return f"Class {letter} Capital Stock"
        return f"Class {letter} Common Stock"
    return "Common Stock"


# ── Shares — iXBRL extraction ─────────────────────────────────────────────────

def _extract_ixbrl_shares(soup: BeautifulSoup) -> list[tuple[str, int]]:
    """
    Find inline iXBRL tags for EntityCommonStockSharesOutstanding and return
    a list of (class_label, share_count) tuples — one per share class.

    Handles scale attributes so "5,822" with scale=6 → 5,822,000,000.
    """
    tags = soup.find_all(
        attrs={"name": re.compile(r"dei:EntityCommonStockSharesOutstanding", re.I)}
    )
    if not tags:
        # Some filers omit the namespace prefix in the attribute
        tags = soup.find_all(
            attrs={"name": re.compile(r"EntityCommonStockSharesOutstanding", re.I)}
        )

    results: list[tuple[str, int]] = []
    seen_vals: set[int] = set()

    for tag in tags:
        val = _parse_ixbrl_numeric(tag)
        if val is None or val <= 0:
            continue

        ctx_id   = tag.get("contextref", "")
        member   = _get_dim_member(soup, ctx_id)
        label    = _member_to_label(member) if member else "Common Stock"

        if val not in seen_vals:
            seen_vals.add(val)
            results.append((label, val))

    return results


# ── Shares — text extraction fallback ────────────────────────────────────────

# Matches: "X shares of [Class Y] [common/capital/...] stock ... outstanding"
_SHARE_CLASS_RE = re.compile(
    r"([\d,]+)\s+shares?\s+of\s+"
    r"(?:(?:the\s+)?[Cc]ompany'?s?\s+)?"                     # optional "the Company's"
    r"((?:Class\s+[A-Z][a-z]?\s+)?"                          # optional "Class A/B/C"
    r"(?:Series\s+[A-Z][a-z]?\s+)?"                          # optional "Series A/B"
    r"(?:[Cc]ommon|[Oo]rdinary|[Cc]apital|[Pp]referred)\s+"  # stock type word
    r"(?:[Ss]tock|[Ss]hares?))"                               # "Stock" or "Shares"
    r"[^.;]{0,80}?"                                           # par value / other text
    r"(?:issued\s+and\s+)?outstanding",
    re.IGNORECASE,
)


def _extract_text_share_classes(text: str) -> list[tuple[str, int]]:
    """Extract (class_label, share_count) pairs from plain cover-page text."""
    results: list[tuple[str, int]] = []
    seen: set[int] = set()

    for m in _SHARE_CLASS_RE.finditer(text):
        count_str = m.group(1).replace(",", "")
        class_raw = re.sub(r"\s+", " ", m.group(2)).strip()
        try:
            count = int(count_str)
            if count > 10_000 and count not in seen:
                seen.add(count)
                results.append((class_raw, count))
        except ValueError:
            continue

    return results


# ── Shares — public API ───────────────────────────────────────────────────────

def extract_shares(
    record: FilingRecord,
    session: Optional[requests.Session] = None,
    delay: float = DEFAULT_DELAY,
) -> list[SharesResult]:
    """
    Extract all share classes (outstanding) from a FilingRecord.

    Returns one SharesResult per share class found — e.g. three rows for
    Alphabet (Class A / B / C).  Single-class companies return one row.

    Extraction order:
    1. Inline iXBRL tags in the primary document — handles scaled values
       (scale=6 means "5,822" in HTML = 5,822,000,000 shares) and resolves
       XBRL context dimensions to get class labels (Class A / B / C etc.).
    2. Text regex on cover-page body text — fallback for non-iXBRL filings
       or when iXBRL tags are absent.

    Args:
        record:  FilingRecord from fetch_filings_for_ciks().
        session: Optional requests.Session (created internally if not supplied).
        delay:   Seconds to sleep after each HTTP request (SEC 10 req/s cap).

    Returns:
        List of SharesResult — at least one element.  shares_outstanding is
        None when the value could not be found.
    """
    own_session = session is None
    if own_session:
        session = _make_session()

    notes_parts: list[str] = []

    try:
        if not record.filing_url:
            return [_empty_shares(record, "none", "no filing_url")]

        try:
            html = _fetch_html(record.filing_url, session, delay)
        except Exception as exc:
            return [_empty_shares(record, "none", f"fetch_error={exc}")]

        soup = BeautifulSoup(html, "html.parser")

        # ── 1. Inline iXBRL ───────────────────────────────────────────────
        pairs = _extract_ixbrl_shares(soup)
        if pairs:
            return [
                SharesResult(
                    cik=record.cik,
                    entity_name=record.entity_name,
                    form_type=record.form_type,
                    filing_date=record.filing_date,
                    accession_number=record.accession_number,
                    share_class=label,
                    shares_outstanding=count,
                    shares_issued=None,
                    source="ixbrl",
                    notes=None,
                )
                for label, count in pairs
            ]

        # ── 2. Text regex fallback ────────────────────────────────────────
        for tag in soup(["script", "style", "head"]):
            tag.decompose()
        text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
        pairs = _extract_text_share_classes(text)
        if pairs:
            return [
                SharesResult(
                    cik=record.cik,
                    entity_name=record.entity_name,
                    form_type=record.form_type,
                    filing_date=record.filing_date,
                    accession_number=record.accession_number,
                    share_class=label,
                    shares_outstanding=count,
                    shares_issued=None,
                    source="text",
                    notes=None,
                )
                for label, count in pairs
            ]

        notes_parts.append("no share data found")
        return [_empty_shares(record, "none", "; ".join(notes_parts))]

    finally:
        if own_session:
            session.close()


def _empty_shares(record: FilingRecord, source: str, notes: str) -> SharesResult:
    return SharesResult(
        cik=record.cik,
        entity_name=record.entity_name,
        form_type=record.form_type,
        filing_date=record.filing_date,
        accession_number=record.accession_number,
        share_class="unknown",
        shares_outstanding=None,
        shares_issued=None,
        source=source,
        notes=notes or None,
    )


# ── Location — iXBRL extraction ───────────────────────────────────────────────

def _extract_ixbrl_locations(soup: BeautifulSoup) -> dict:
    """Extract address and incorporation fields from inline iXBRL DEI tags."""

    def _val(concept: str) -> Optional[str]:
        tags = soup.find_all(attrs={"name": re.compile(concept, re.I)})
        if not tags:
            # Try without namespace prefix
            tags = soup.find_all(
                attrs={"name": re.compile(concept.split(":")[-1], re.I)}
            )
        return tags[0].get_text(strip=True) or None if tags else None

    return {
        "state_of_incorporation":   _val("dei:EntityIncorporationStateCountryCode"),
        "country_of_incorporation": None,
        "hq_address1": _val("dei:EntityAddressAddressLine1"),
        "hq_address2": _val("dei:EntityAddressAddressLine2"),
        "hq_city":     _val("dei:EntityAddressCityOrTown"),
        "hq_state":    _val("dei:EntityAddressStateOrProvince"),
        "hq_zip":      _val("dei:EntityAddressPostalZipCode"),
        "hq_country":  None,
    }


# ── Location — text extraction fallback ──────────────────────────────────────

# Incorporation state patterns — value may appear before OR after the label
_INC_BEFORE = re.compile(
    r"([A-Z][a-zA-Z ]{2,30})\s*[\r\n]+[^\r\n]*"
    r"[Ss]tate\s+or\s+other\s+jurisdiction\s+of\s+incorporation"
)
_INC_AFTER = re.compile(
    r"[Ss]tate\s+or\s+other\s+jurisdiction\s+of\s+incorporation"
    r"[^\r\n]*organization\)?[:\s,\r\n]*([A-Z][a-zA-Z ]{2,30})",
    re.DOTALL,
)
_INC_PHRASES = [
    re.compile(r"\ba\s+([A-Z][a-z]{2,20}(?:\s+[A-Z][a-z]+)?)\s+corporation\b"),
    re.compile(
        r"incorporated\s+(?:under\s+the\s+laws\s+of|in)\s+"
        r"(?:the\s+[Ss]tate\s+of\s+)?([A-Z][a-z]{2,20}(?:\s+[A-Z][a-z]+)?)"
    ),
    re.compile(
        r"organized\s+under\s+the\s+laws\s+of\s+"
        r"(?:the\s+[Ss]tate\s+of\s+)?([A-Z][a-z]{2,20}(?:\s+[A-Z][a-z]+)?)"
    ),
]
_JUNK = {"the", "a", "an", "our", "its", "this", "that", "which", "state"}

# City / State(full or abbrev) / Zip — handles "City , CA  94043" or
# "Mountain View , California 94043" or "Menlo Park, California 94025"
_CITY_STATE_ZIP = re.compile(
    r"([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)*)"  # city (capitalized words, no loose spaces)
    r"\s*,\s*"                                   # comma (allows spaces around it)
    r"([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)*)"  # state: full name or 2-letter abbrev
    r"\s+"
    r"(\d{5}(?:-\d{4})?)"                        # zip
)


def _extract_text_locations(html: str) -> dict:
    """Parse incorporation state and HQ address from raw filing HTML text."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "head"]):
        tag.decompose()
    # Keep newlines for line-by-line patterns; collapse horizontal whitespace
    text = re.sub(r"[ \t]+", " ", soup.get_text("\n", strip=True))

    # ── Incorporation state ──────────────────────────────────────────────
    inc_state = None

    m = _INC_BEFORE.search(text)
    if m:
        c = m.group(1).strip().rstrip(",.")
        if c.lower() not in _JUNK:
            inc_state = c

    if inc_state is None:
        m = _INC_AFTER.search(text)
        if m:
            c = m.group(1).strip().rstrip(",.")
            if c.lower() not in _JUNK and len(c) > 2:
                inc_state = c

    if inc_state is None:
        for pat in _INC_PHRASES:
            m = pat.search(text)
            if m:
                c = m.group(1).strip().rstrip(",.")
                if c.lower() not in _JUNK:
                    inc_state = c
                    break

    # ── HQ address — anchor search around "principal executive offices" ──
    peo = re.search(r"principal\s+executive\s+offices", text, re.IGNORECASE)
    window = text
    if peo:
        s = max(0, peo.start() - 600)
        e = min(len(text), peo.end() + 600)
        window = text[s:e]

    csz = _CITY_STATE_ZIP.search(window)
    hq_city = hq_state = hq_zip = hq_addr1 = hq_addr2 = None

    if csz:
        hq_city  = csz.group(1).strip()
        hq_state = csz.group(2).strip()
        hq_zip   = csz.group(3)

        # Lines before the city/state/zip are candidate street address lines
        pre   = window[: csz.start()]
        lines = [l.strip() for l in pre.split("\n") if l.strip()]
        # Drop lines that look like the label itself
        addr_lines = [
            l for l in lines[-5:]
            if not re.search(
                r"principal|executive|offices|address|registrant|zip\s+code",
                l, re.IGNORECASE
            )
            and len(l) > 3
        ]
        if addr_lines:
            hq_addr1 = addr_lines[-1]
        if len(addr_lines) >= 2:
            hq_addr2 = addr_lines[-2]

    return {
        "state_of_incorporation":   inc_state,
        "country_of_incorporation": None,
        "hq_address1": hq_addr1,
        "hq_address2": hq_addr2,
        "hq_city":     hq_city,
        "hq_state":    hq_state,
        "hq_zip":      hq_zip,
        "hq_country":  None,
    }


# ── Location — public API ─────────────────────────────────────────────────────

def extract_company_locations(
    record: FilingRecord,
    session: Optional[requests.Session] = None,
    delay: float = DEFAULT_DELAY,
) -> LocationResult:
    """
    Extract state of incorporation and principal HQ address from a FilingRecord.

    Tries inline iXBRL DEI tags first (EntityAddressAddressLine1,
    EntityAddressCityOrTown, EntityAddressStateOrProvince,
    EntityAddressPostalZipCode, EntityIncorporationStateCountryCode).
    Falls back to text regex extraction from the cover page when iXBRL tags
    are absent.

    Returns the business / principal-executive-offices address — NOT the
    mailing address.  Fields are None when not found.

    Args:
        record:  FilingRecord from fetch_filings_for_ciks().
        session: Optional requests.Session.
        delay:   Seconds between HTTP requests (SEC 10 req/s cap).

    Returns:
        LocationResult dataclass.
    """
    own_session = session is None
    if own_session:
        session = _make_session()

    _EMPTY = {
        "state_of_incorporation": None, "country_of_incorporation": None,
        "hq_address1": None, "hq_address2": None,
        "hq_city": None, "hq_state": None, "hq_zip": None, "hq_country": None,
    }
    loc = dict(_EMPTY)
    source = "none"
    notes_parts: list[str] = []

    try:
        if not record.filing_url:
            notes_parts.append("no filing_url")
            return _make_loc_result(record, loc, "none", notes_parts)

        try:
            html = _fetch_html(record.filing_url, session, delay)
        except Exception as exc:
            notes_parts.append(f"fetch_error={exc}")
            return _make_loc_result(record, loc, "none", notes_parts)

        soup = BeautifulSoup(html, "html.parser")

        # ── 1. Inline iXBRL ───────────────────────────────────────────────
        ixbrl_loc = _extract_ixbrl_locations(soup)
        for k, v in ixbrl_loc.items():
            if v is not None:
                loc[k] = v
        if any(v for v in loc.values()):
            source = "ixbrl"

        # ── 2. Text fallback for any still-missing fields ─────────────────
        needs_text = loc["hq_city"] is None or loc["state_of_incorporation"] is None
        if needs_text:
            text_loc = _extract_text_locations(html)
            for k, v in text_loc.items():
                if loc[k] is None and v is not None:
                    loc[k] = v
            if source == "none":
                source = "text"
            elif source == "ixbrl":
                source = "ixbrl+text"

    finally:
        if own_session:
            session.close()

    return _make_loc_result(record, loc, source, notes_parts)


def _make_loc_result(
    record: FilingRecord,
    loc: dict,
    source: str,
    notes_parts: list[str],
) -> LocationResult:
    return LocationResult(
        cik=record.cik,
        entity_name=record.entity_name,
        form_type=record.form_type,
        filing_date=record.filing_date,
        accession_number=record.accession_number,
        **loc,
        source=source,
        notes="; ".join(notes_parts) or None,
    )


# ── Batch helpers ─────────────────────────────────────────────────────────────

def batch_extract_shares(
    records: list[FilingRecord],
    delay: float = DEFAULT_DELAY,
) -> list[SharesResult]:
    """
    Extract shares for a list of FilingRecords.

    Multi-class companies (Alphabet, Meta, etc.) produce multiple rows — one
    per share class.  The result is a flat list suitable for DataFrame conversion.

    Args:
        records:  FilingRecords from fetch_filings_for_ciks().
        delay:    Seconds between HTTP requests (SEC 10 req/s cap).

    Returns:
        Flat list of SharesResult — possibly more items than len(records).
    """
    all_results: list[SharesResult] = []
    session = _make_session()
    try:
        for record in records:
            results = extract_shares(record, session=session, delay=delay)
            all_results.extend(results)
            for r in results:
                out = f"{r.shares_outstanding:,}" if r.shares_outstanding else "n/a"
                print(
                    f"  [shares] {record.entity_name:<30} {record.form_type:<6}"
                    f" {record.filing_date}  {r.share_class:<30}"
                    f"  outstanding={out:>22}  source={r.source}"
                )
    finally:
        session.close()
    return all_results


def batch_extract_locations(
    records: list[FilingRecord],
    delay: float = DEFAULT_DELAY,
) -> list[LocationResult]:
    """
    Extract incorporation state and HQ address for a list of FilingRecords.

    Args:
        records:  FilingRecords from fetch_filings_for_ciks().
        delay:    Seconds between HTTP requests (SEC 10 req/s cap).

    Returns:
        List of LocationResult objects in the same order as records.
    """
    results: list[LocationResult] = []
    session = _make_session()
    try:
        for record in records:
            result = extract_company_locations(record, session=session, delay=delay)
            results.append(result)
            print(
                f"  [loc] {record.entity_name:<30} {record.form_type:<6}"
                f" {record.filing_date}"
                f"  inc={result.state_of_incorporation or 'n/a':<12}"
                f"  city={result.hq_city or 'n/a':<20}"
                f"  source={result.source}"
            )
    finally:
        session.close()
    return results


# ── Escrow shares — data class ───────────────────────────────────────────────

@dataclass
class EscrowSharesResult:
    """One row per escrow/contingent finding within a filing."""
    cik: str
    entity_name: str
    form_type: str
    filing_date: str
    accession_number: str
    shares_in_escrow: int
    share_class: str
    escrow_type: str        # earnout | founder | performance | lockup | contingent | escrow | general
    trigger_hint: str       # ≤300 chars describing the release condition
    source_text: str        # ≤500 chars matched passage for human review


# ── Escrow shares — helper patterns (ported from escrow_shares.py) ────────────

# Par-value noise to strip from class labels, e.g. ", $0.0001 par value"
_PAR_VALUE_RE = re.compile(r",?\s*\$[\d.]+\s+par\s+value\b[^,;)]*", re.IGNORECASE)

# Normalisation: collapse non-alphanumeric runs for dedup keys
_NORM_RE = re.compile(r"[^a-z0-9]+")

# Trigger keywords — positions in text worth opening a window around
_TRIGGER_RE = re.compile(
    r"\b(?:"
    r"escrow"
    r"|contingent\s+shares?"
    r"|earn[- ]?out\s+shares?"
    r"|earnout\s+shares?"
    r"|founder\s+shares?"
    r"|performance\s+shares?"
    r"|milestone\s+shares?"
    r")\b",
    re.IGNORECASE,
)

# Share count with optional modifier and class label — named groups
_WINDOW_SHARES_RE = re.compile(
    r"(?P<shares>[\d,]{3,})\s+"
    r"(?P<modifier>contingent|earn[- ]?out|earnout|founder|performance|milestone|restricted)?\s*"
    r"(?:(?P<cls_adj>[A-Za-z][^,\n.()]{2,60}?)\s+)?"
    r"shares?"
    r"(?:\s+of\s+(?P<cls_of>[^,\n.()]{4,100}))?",
    re.IGNORECASE,
)

# Explicit placement verb — confirms a count is actually *being placed in* escrow
_PLACEMENT_RE = re.compile(
    r"\b(?:"
    r"placed\s+(?:in(?:to)?|in\s+an?)"
    r"|held\s+in(?:\s+an?)?"
    r"|deposited\s+(?:in(?:to)?|in\s+an?)"
    r"|put\s+(?:in(?:to)?|in\s+an?)"
    r"|contributed\s+to(?:\s+an?)?"
    r"|transferred\s+to(?:\s+an?)?"
    r"|released\s+from"
    r"|subject\s+to\s+(?:an?\s+)?escrow"
    r")\s+(?:an?\s+)?escrow",
    re.IGNORECASE,
)

# Priority-ordered type classifiers — first match wins
_ESCROW_TYPE_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bearn[- ]?out\b|\bearnout\b",        re.IGNORECASE), "earnout"),
    (re.compile(r"\bfounder\b",                          re.IGNORECASE), "founder"),
    (re.compile(r"\bperformance\b|\bmilestone\b",        re.IGNORECASE), "performance"),
    (re.compile(r"\block[- ]?up\b",                      re.IGNORECASE), "lockup"),
    (re.compile(r"\bcontingent\b",                       re.IGNORECASE), "contingent"),
    (re.compile(r"\bspac\b|\bde[- ]?spac\b|\bmerger\b", re.IGNORECASE), "contingent"),
]

# Captures the release-condition fragment for trigger_hint
_RELEASE_HINT_RE = re.compile(
    r"(?:to\s+be\s+released|released|vest|converted|forfeited|distributed)"
    r"[^.!?\n]{0,300}",
    re.IGNORECASE,
)

# Trims trailing grammatical noise from extracted class labels
_CLASS_TRAIL_RE = re.compile(
    r"\s+(?:"
    r"to\s+(?:the|a|an|holders?|such|all|certain|Legacy|each)\b"
    r"|will\s+be\b"
    r"|were\s"
    r"|are\s"
    r"|is\s"
    r"|has\s(?:been\s)?"
    r"|have\s(?:been\s)?"
    r"|had\s(?:been\s)?"
    r"|and\s"
    r"|that\s+(?:were|are|is|have|had)\b"
    r"|which\s+(?:were|are|is|have|had)\b"
    r"|\w+ed\s+(?:in|to|into)\b"
    r").*$",
    re.IGNORECASE | re.DOTALL,
)

_SCAN_BYTES   = 800_000   # read at most 800 KB of filing text
_WIN_BEFORE   = 600
_WIN_AFTER    = 400


def _clean_title(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().title()


def _norm_class(raw: str | None) -> str:
    if not raw:
        return ""
    c = _PAR_VALUE_RE.sub("", raw)
    return _NORM_RE.sub("", c.lower())


def _trim_class(raw: str | None) -> str | None:
    if not raw:
        return None
    trimmed = _CLASS_TRAIL_RE.sub("", raw.strip())
    return _clean_title(trimmed.strip()) if trimmed.strip() else None


def _best_class(m: re.Match) -> str | None:
    cls_of  = (m.group("cls_of")  or "").strip()
    cls_adj = (m.group("cls_adj") or "").strip()
    return _trim_class(cls_of or cls_adj)


def _classify_escrow_type(window: str) -> str:
    for pattern, label in _ESCROW_TYPE_RULES:
        if pattern.search(window):
            return label
    if _PLACEMENT_RE.search(window):
        return "escrow"
    return "general"


def _extract_trigger_hint(window: str) -> str:
    m = _RELEASE_HINT_RE.search(window)
    return m.group(0).strip()[:300] if m else ""


# ── Core text scanner ─────────────────────────────────────────────────────────

def _find_escrow_in_text(text: str) -> list[dict]:
    """
    Scan plain text for escrow/contingent share disclosures.
    Returns a list of finding dicts (no filing-identity fields yet).
    One dict per distinct (shares, class) pair found.
    """
    results: list[dict] = []
    seen: set[tuple] = set()

    for trigger in _TRIGGER_RE.finditer(text):
        pos     = trigger.start()
        w_start = max(0, pos - _WIN_BEFORE)
        w_end   = min(len(text), pos + _WIN_AFTER)
        window  = text[w_start:w_end]

        trigger_pos_in_window = pos - w_start

        # Anchor proximity to the placement verb when one exists — prevents
        # a share count from an unrelated sentence stealing the match.
        placement_match = _PLACEMENT_RE.search(window)
        anchor = placement_match.start() if placement_match else trigger_pos_in_window

        # Rank candidate share counts by distance to anchor, then by size (larger preferred)
        candidates: list[tuple[int, int, re.Match]] = []
        for sm in _WINDOW_SHARES_RE.finditer(window):
            try:
                n = int(sm.group("shares").replace(",", ""))
            except ValueError:
                continue
            if n < 1_000:
                continue
            candidates.append((abs(sm.start() - anchor), n, sm))

        if not candidates:
            continue

        candidates.sort(key=lambda t: (t[0], -t[1]))

        # If the closest candidate is already recorded this trigger overlaps
        # a captured event — skip entirely rather than fall through to a worse match.
        _, n0, sm0 = candidates[0]
        if (n0, _norm_class(_best_class(sm0))) in seen:
            continue

        _, n, sm = candidates[0]
        share_class_raw = _best_class(sm)

        # Gate: only record when we have an explicit placement verb OR the
        # trigger keyword itself implies escrow — avoids "escrow" appearing
        # in an unrelated context next to a share count.
        trigger_text = trigger.group(0).lower()
        is_escrow_word = "escrow" in trigger_text
        has_placement  = bool(_PLACEMENT_RE.search(window))
        is_named_type  = any(
            kw in trigger_text
            for kw in ("contingent", "earn", "earnout", "founder", "performance", "milestone")
        )
        if is_escrow_word and not has_placement and not is_named_type:
            continue

        key = (n, _norm_class(share_class_raw))
        if key in seen:
            continue
        seen.add(key)

        results.append({
            "shares_in_escrow": n,
            "share_class":      share_class_raw or "Common Stock",
            "escrow_type":      _classify_escrow_type(window),
            "trigger_hint":     _extract_trigger_hint(window),
            "source_text":      window.strip()[:500],
        })

    return results


# ── Escrow shares — public API ────────────────────────────────────────────────

def extract_escrow_shares(
    record: FilingRecord,
    session: requests.Session | None = None,
    delay: float = DEFAULT_DELAY,
) -> list[EscrowSharesResult]:
    """
    Scan a single filing for escrow/contingent/earnout share disclosures.

    Fetches the primary filing document (up to 800 KB), strips HTML to plain
    text, then applies trigger-keyword scanning with proximity-anchored share
    matching.  Returns one EscrowSharesResult per distinct finding — a filing
    with no escrow language returns an empty list.

    Args:
        record:  FilingRecord from fetch_filings_for_ciks().
        session: Optional requests.Session (created internally if not provided).
        delay:   Seconds between HTTP requests (SEC 10 req/s cap).

    Returns:
        List of EscrowSharesResult (empty if nothing found).
    """
    own_session = session is None
    if own_session:
        session = _make_session()

    try:
        resp  = _get_with_retry(record.filing_url, session, delay)
        raw   = resp.content[:_SCAN_BYTES]
        text  = BeautifulSoup(raw.decode("utf-8", errors="replace"), "html.parser").get_text(" ", strip=True)
        findings = _find_escrow_in_text(text)

        return [
            EscrowSharesResult(
                cik              = record.cik,
                entity_name      = record.entity_name,
                form_type        = record.form_type,
                filing_date      = record.filing_date,
                accession_number = record.accession_number,
                shares_in_escrow = f["shares_in_escrow"],
                share_class      = f["share_class"],
                escrow_type      = f["escrow_type"],
                trigger_hint     = f["trigger_hint"],
                source_text      = f["source_text"],
            )
            for f in findings
        ]

    except Exception as exc:
        print(f"  [escrow] {record.entity_name} {record.form_type} {record.filing_date}  error: {exc}")
        return []
    finally:
        if own_session:
            session.close()


def batch_extract_escrow_shares(
    records: list[FilingRecord],
    delay: float = DEFAULT_DELAY,
) -> list[EscrowSharesResult]:
    """
    Scan a list of filings for escrow/contingent/earnout share disclosures.

    Reuses one HTTP session across all filings.  Returns a flat list — a
    filing with multiple distinct escrow structures produces multiple rows,
    a filing with none produces zero rows.

    Args:
        records: FilingRecords from fetch_filings_for_ciks().
        delay:   Seconds between HTTP requests (SEC 10 req/s cap).

    Returns:
        Flat list of EscrowSharesResult across all filings.
    """
    results: list[EscrowSharesResult] = []
    session = _make_session()
    try:
        for record in records:
            found = extract_escrow_shares(record, session=session, delay=delay)
            print(
                f"  [escrow] {record.entity_name:<30} {record.form_type:<6}"
                f" {record.filing_date}"
                f"  findings={len(found)}"
            )
            results.extend(found)
    finally:
        session.close()
    return results


# Backward-compatible aliases
extract_contingent_shares       = extract_escrow_shares
batch_extract_contingent_shares = batch_extract_escrow_shares
