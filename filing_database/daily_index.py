"""
daily_index.py — Build FilingRecords from the SEC EDGAR *daily index* feed.

This is the daily-index counterpart to ``methods/sec_filings_sync.py``.  Where
that module walks the per-company submissions API (CIK -> filings), this module
walks the firm-wide daily index (date -> every filing filed that day):

    https://www.sec.gov/Archives/edgar/daily-index/<year>/QTR<q>/master.YYYYMMDD.idx

It produces the *same* ``FilingRecord`` objects so both ingest paths are
interchangeable downstream.

It reuses the locked module's shared infrastructure — the rate-limited
``_get_with_retry`` fetcher, the ``UA`` header (your email), ``SECBlockedError``,
the URL builders and the ``FilingRecord`` dataclass — so rate-limiting and
User-Agent policy are identical to the CIK path and defined in exactly one place.

Three layers, cheapest first
----------------------------
1. **Discovery (the "sitemap").**  ``list_available_index_files()`` reads each
   quarter's ``index.json`` directory listing and returns exactly which
   ``master.YYYYMMDD.idx`` files actually exist.  The runner iterates *these*
   dates instead of blindly incrementing by one day, so weekends, holidays and
   not-yet-published days are never requested (no wasted/failed pulls).

2. **Parse (1 request per day).**  ``fetch_master_index`` + ``parse_master_index``
   turn a day's master file into FilingRecords, populating every field the
   master feed provides (cik, entity_name, form_type, filing_date, accession and
   all derived URLs).  Fields the master feed does not carry (report_date,
   primary_document, file_number, act, xbrl flag) are left ``None``.

3. **Enrich (opt-in, 1 request per filing).**  ``enrich_record`` does a small
   HTTP *range* read of the filing's full-submission ``.txt`` and parses the
   SGML ``<SEC-HEADER>`` to fill primary_document, filing_url, report_date, act,
   file_number, is_xbrl and xbrl_instance_url.  This is the same header block
   ``methods/country_assignment`` already parses.  It is off by default because
   it multiplies request volume by the number of filings.
"""

from __future__ import annotations

import re
import time
import json
from dataclasses import dataclass, replace
from typing import Optional

import requests

# Reuse the locked module's shared, rate-limited plumbing verbatim.  Importing
# (not copying) keeps the SEC access policy — UA and the 10 req/s cap — in one
# authoritative place.
from methods.sec_filings_sync import (
    FilingRecord,
    UA,
    DEFAULT_DELAY,
    SECBlockedError,
    _get_with_retry,
    _build_urls,
    _build_xbrl_instance_url,
)

DAILY_INDEX_BASE = "https://www.sec.gov/Archives/edgar/daily-index"
ARCHIVES_ROOT    = "https://www.sec.gov/Archives"
COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"


# ── Transport-resilient GET ───────────────────────────────────────────────────
# The locked ``_get_with_retry`` retries 429/5xx *responses*, but a dropped or
# stalled connection raises ``requests.Timeout`` / ``requests.ConnectionError``
# before any response exists, and those propagate straight up.  This thin wrapper
# adds the missing transport-level back-off so a transient network blip becomes a
# retry instead of a run-ending crash (the fail-safe the ingest layer relies on).
# It never swallows ``SECBlockedError`` (a genuine IP ban must stop the run) nor
# ``HTTPError`` (callers inspect 403/404 themselves).

def _resilient_get(
    url: str,
    session: requests.Session,
    delay: float = DEFAULT_DELAY,
    transport_retries: int = 4,
) -> requests.Response:
    for attempt in range(transport_retries):
        try:
            return _get_with_retry(url, session, delay)
        except (requests.Timeout, requests.ConnectionError) as exc:
            wait = min(delay * 10 * (2 ** attempt), 30.0)
            print(f"  [network {type(exc).__name__}] retrying in {wait:.0f}s "
                  f"(attempt {attempt + 1}/{transport_retries})")
            time.sleep(wait)
    # One last attempt; if the connection is still dead, let it raise so the
    # caller (ingest) marks just this item failed and moves on / aborts cleanly.
    return _get_with_retry(url, session, delay)


# ── Date helpers ──────────────────────────────────────────────────────────────

def to_iso_date(yyyymmdd: str) -> str:
    """'20250402' -> '2025-04-02'.  Passes through values already ISO."""
    s = str(yyyymmdd).strip()
    if "-" in s:
        return s
    return f"{s[0:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 else s


def compact_date(iso_or_compact: str) -> str:
    """'2025-04-02' -> '20250402' (the form used in master.YYYYMMDD.idx)."""
    return str(iso_or_compact).replace("-", "")


def quarter_of(iso_date: str) -> int:
    month = int(iso_date[5:7])
    return (month - 1) // 3 + 1


def quarter_label(iso_date: str) -> str:
    return f"{iso_date[0:4]}-QTR{quarter_of(iso_date)}"


def iter_quarters(start_iso: str, end_iso: str):
    """Yield (year, quarter) tuples spanning [start, end] inclusive."""
    y, q = int(start_iso[0:4]), quarter_of(start_iso)
    ey, eq = int(end_iso[0:4]), quarter_of(end_iso)
    while (y, q) <= (ey, eq):
        yield y, q
        q += 1
        if q > 4:
            q, y = 1, y + 1


# ── Discovery: the "sitemap" of available daily index files ──────────────────

@dataclass
class IndexFileInfo:
    """One available daily master index file, from the quarter's directory JSON."""
    idx_date: str       # YYYY-MM-DD
    file_name: str      # 'master.YYYYMMDD.idx'
    url: str            # full URL
    quarter: str        # 'YYYY-QTRn'
    last_modified: Optional[str]
    size_label: Optional[str]


_MASTER_RE = re.compile(r"^master\.(\d{8})\.idx$")


def _quarter_index_url(year: int, quarter: int) -> str:
    return f"{DAILY_INDEX_BASE}/{year}/QTR{quarter}/index.json"


def list_available_index_files(
    start_date: str,
    end_date: str,
    session: requests.Session,
    delay: float = DEFAULT_DELAY,
) -> list[IndexFileInfo]:
    """
    Return the ``master.*.idx`` files the SEC actually publishes within
    [start_date, end_date] (both ISO, inclusive), discovered from each quarter's
    ``index.json`` directory listing.

    Quarters with no listing yet (e.g. a future quarter) are skipped rather than
    raising, so a range that runs slightly past today degrades gracefully.
    """
    out: list[IndexFileInfo] = []
    for year, quarter in iter_quarters(start_date, end_date):
        url = _quarter_index_url(year, quarter)
        try:
            resp = _resilient_get(url, session, delay)
        except requests.HTTPError as exc:
            # 403/404 for a quarter that does not exist yet — skip it.
            if exc.response is not None and exc.response.status_code in (403, 404):
                continue
            raise
        items = json.loads(resp.content).get("directory", {}).get("item", [])
        qlabel = f"{year}-QTR{quarter}"
        for it in items:
            m = _MASTER_RE.match(it.get("name", ""))
            if not m:
                continue
            idx_date = to_iso_date(m.group(1))
            if idx_date < start_date or idx_date > end_date:
                continue
            out.append(IndexFileInfo(
                idx_date=idx_date,
                file_name=it["name"],
                url=f"{DAILY_INDEX_BASE}/{year}/QTR{quarter}/{it['name']}",
                quarter=qlabel,
                last_modified=it.get("last-modified"),
                size_label=it.get("size"),
            ))
    out.sort(key=lambda x: x.idx_date)
    return out


def master_index_url(idx_date: str) -> str:
    """Direct URL for a date's master index, without a discovery round-trip."""
    q = quarter_of(idx_date)
    return f"{DAILY_INDEX_BASE}/{idx_date[0:4]}/QTR{q}/master.{compact_date(idx_date)}.idx"


# ── Parse: master.YYYYMMDD.idx -> FilingRecord[] ─────────────────────────────

_ACCESSION_RE = re.compile(r"(\d{10}-\d{2}-\d{6})")


def _accession_from_filename(file_name: str) -> str:
    """'edgar/data/1000177/0000919574-25-002201.txt' -> '0000919574-25-002201'."""
    m = _ACCESSION_RE.search(file_name)
    return m.group(1) if m else ""


def fetch_master_index(
    idx_date_or_info,
    session: requests.Session,
    delay: float = DEFAULT_DELAY,
) -> str:
    """
    Download one daily master index file and return its decoded text.

    Accepts either an ``IndexFileInfo`` (from discovery) or a plain ISO date
    string (direct fetch).  Uses the shared retry/back-off fetcher, so 429s and
    5xx are handled and a persistent 429 raises ``SECBlockedError``.
    """
    if isinstance(idx_date_or_info, IndexFileInfo):
        url = idx_date_or_info.url
    else:
        url = master_index_url(idx_date_or_info)
    resp = _resilient_get(url, session, delay)
    # The index files are Latin-1 (some issuer names carry accented bytes).
    return resp.content.decode("latin-1")


def parse_master_index(
    text: str,
    idx_date: str,
    ticker_map: Optional[dict[str, str]] = None,
) -> list[FilingRecord]:
    """
    Parse a master index file body into FilingRecords.

    Layout (confirmed against live data)::

        Description: ...            <- free-text header block
        ...
        CIK|Company Name|Form Type|Date Filed|File Name
        ---------------------------------------------------
        1000177|NORDIC AMERICAN TANKERS Ltd|424B5|20250402|edgar/data/1000177/0000919574-25-002201.txt
        ...

    URLs are built with the locked module's ``_build_urls`` so they are byte-for
    byte identical to the CIK-based path.  Fields the master feed does not carry
    (report_date, primary_document, file_number, act, size, is_xbrl,
    xbrl_instance_url, filing_url) are left ``None``/empty for optional
    enrichment later.
    """
    records: list[FilingRecord] = []
    started = False
    for raw in text.splitlines():
        line = raw.rstrip("\n")
        if not started:
            # Data begins on the line after the dashed separator.
            if set(line.strip()) == {"-"} and len(line.strip()) > 10:
                started = True
            continue
        if not line or line.count("|") < 4:
            continue
        cik_s, name, form, date_filed, file_name = line.split("|", 4)
        cik_s = cik_s.strip()
        if not cik_s.isdigit():
            continue
        accession = _accession_from_filename(file_name.strip())
        if not accession:
            continue
        cik_int = int(cik_s)
        # Identical URL construction to the CIK-based sync path.
        _filing_url, index_url, filing_detail_url, submission_txt_url = _build_urls(
            cik_int, accession, primary_doc="",
        )
        ticker = ""
        if ticker_map:
            ticker = ticker_map.get(str(cik_int), "")
        records.append(FilingRecord(
            cik                = str(cik_int),
            entity_name        = name.strip(),
            ticker             = ticker,
            form_type          = form.strip(),
            filing_date        = to_iso_date(date_filed.strip()),
            report_date        = None,
            accession_number   = accession,
            primary_document   = "",
            filing_url         = "",        # unknown until enrichment
            index_url          = index_url,
            filing_detail_url  = filing_detail_url,
            submission_txt_url = submission_txt_url,
            xbrl_instance_url  = None,
            file_number        = None,
            act                = None,
            size               = None,
            is_xbrl            = None,       # unknown (not in daily feed)
        ))
    return records


def fetch_filings_for_date(
    idx_date_or_info,
    session: requests.Session,
    delay: float = DEFAULT_DELAY,
    ticker_map: Optional[dict[str, str]] = None,
) -> list[FilingRecord]:
    """Convenience: download + parse one day into FilingRecords."""
    idx_date = (
        idx_date_or_info.idx_date
        if isinstance(idx_date_or_info, IndexFileInfo)
        else idx_date_or_info
    )
    text = fetch_master_index(idx_date_or_info, session, delay)
    return parse_master_index(text, idx_date, ticker_map=ticker_map)


# ── Optional: ticker resolution (one bulk file, cached per run) ───────────────

def load_ticker_map(
    session: requests.Session, delay: float = DEFAULT_DELAY
) -> dict[str, str]:
    """
    Build a {cik(int-as-str) -> ticker} map from company_tickers.json.

    The master feed has no ticker column; this single bulk file lets us fill the
    ``ticker`` field cheaply (one request for the whole run) to match the CIK
    path, which sources tickers from the submissions API.  CIKs not present
    (funds, many foreign private issuers) simply resolve to "".
    """
    resp = _resilient_get(COMPANY_TICKERS_URL, session, delay)
    data = json.loads(resp.content)
    out: dict[str, str] = {}
    for row in data.values():
        cik = str(int(row["cik_str"]))
        # Keep the first ticker seen for a CIK (matches sync's tickers[0]).
        out.setdefault(cik, row.get("ticker", ""))
    return out


# ── Optional: per-filing header enrichment ───────────────────────────────────

# SGML header field patterns (see the <SEC-HEADER> block of any submission .txt).
_H_PERIOD = re.compile(r"CONFORMED PERIOD OF REPORT:\s*(\d{8})")
_H_ACT    = re.compile(r"SEC ACT:\s*(.+)")
_H_FILENO = re.compile(r"SEC FILE NUMBER:\s*(\S+)")
_H_DOCFILE = re.compile(r"<FILENAME>([^\r\n<]+)")

# How many bytes to range-read.  The .txt is gzipped in transit, so this small
# compressed window decodes to ~100KB — comfortably past the header and the
# first <DOCUMENT>'s <FILENAME> for essentially every filing.
# Range windows for the header read.  The .txt is gzipped in transit, so a small
# compressed window decodes to a large slice; the escalation covers the rare
# filing whose <SEC-HEADER> (many filers) runs past the first window.
_HEADER_RANGE_BYTES = 12000
_HEADER_RANGE_ESCALATED = 96000


def _fetch_header_chunk(
    url: str, session: requests.Session, delay: float, range_bytes: int
) -> Optional[str]:
    """
    Range-read the head of a submission .txt and decode it, retrying 429/5xx and
    transient network errors.  Returns the decoded text, or None if unreachable.
    """
    headers = dict(UA)
    headers["Range"] = f"bytes=0-{range_bytes}"
    for attempt in range(4):
        try:
            r = session.get(url, headers=headers, timeout=30)
        except (requests.Timeout, requests.ConnectionError):
            time.sleep(min(delay * 10 * (2 ** attempt), 30.0))
            continue
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(min(delay * 10 * (2 ** attempt), 30.0))
            continue
        try:
            r.raise_for_status()
        except requests.HTTPError:
            return None
        return r.content.decode("latin-1", "replace")
    return None


def enrich_record(
    rec: FilingRecord,
    session: requests.Session,
    delay: float = DEFAULT_DELAY,
) -> FilingRecord:
    """
    Fill the fields the daily feed omits by reading the filing's SGML header.

    Populates: report_date, primary_document, filing_url, act, file_number,
    is_xbrl and xbrl_instance_url.  Returns a *new* FilingRecord (the input is
    left untouched).  On any failure the original record is returned unchanged
    so enrichment can never lose a row (it stays queued for a later pass).
    """
    chunk = _fetch_header_chunk(rec.submission_txt_url, session, delay, _HEADER_RANGE_BYTES)
    if chunk is None:
        return rec
    # Escalate once if the first window didn't reach the end of the header (a
    # submission with an unusually large multi-filer <SEC-HEADER>).
    if "</SEC-HEADER>" not in chunk:
        bigger = _fetch_header_chunk(
            rec.submission_txt_url, session, delay, _HEADER_RANGE_ESCALATED
        )
        if bigger is not None:
            chunk = bigger

    head = chunk.split("</SEC-HEADER>", 1)[0]

    report_date = None
    m = _H_PERIOD.search(head)
    if m:
        report_date = to_iso_date(m.group(1))

    act = None
    m = _H_ACT.search(head)
    if m:
        act = m.group(1).strip()

    file_number = None
    m = _H_FILENO.search(head)
    if m:
        file_number = m.group(1).strip()

    # Primary document = the first <DOCUMENT>'s <FILENAME>, which appears just
    # after </SEC-HEADER>.
    primary_document = ""
    body_after_header = chunk.split("</SEC-HEADER>", 1)[-1]
    m = _H_DOCFILE.search(body_after_header)
    if m:
        primary_document = m.group(1).strip()

    # Inline-XBRL detection: the primary document block is wrapped in an <XBRL>
    # tag for iXBRL filings.  Use that (bounded to the first document block).
    first_doc_block = body_after_header.split("</DOCUMENT>", 1)[0]
    is_inline_xbrl = "<XBRL>" in first_doc_block
    is_xbrl = is_inline_xbrl

    cik_int = int(rec.cik)
    filing_url = rec.filing_url
    xbrl_instance_url = rec.xbrl_instance_url
    if primary_document:
        filing_url, _iu, _du, _su = _build_urls(cik_int, rec.accession_number, primary_document)
        xbrl_instance_url = _build_xbrl_instance_url(
            cik_int, rec.accession_number, primary_document, is_inline_xbrl,
        )

    return replace(
        rec,
        report_date=report_date if report_date is not None else rec.report_date,
        primary_document=primary_document or rec.primary_document,
        filing_url=filing_url,
        act=act if act is not None else rec.act,
        file_number=file_number if file_number is not None else rec.file_number,
        is_xbrl=is_xbrl,
        xbrl_instance_url=xbrl_instance_url,
    )
