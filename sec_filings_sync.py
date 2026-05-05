"""
sec_filings_sync.py — Synchronous SEC EDGAR filing metadata fetcher.

Pulls filing metadata (form type, dates, URLs, XBRL flag) for a list of
CIK codes from the SEC EDGAR submissions API.

Usage
-----
Import and call fetch_filings_for_ciks() directly:

    from sec_filings_sync import fetch_filings_for_ciks
    filings = fetch_filings_for_ciks(ciks=['320193'], form_types=['10-K'])

The returned list of FilingRecord objects can be passed directly to the
extraction functions in sec_extractors.py and escrow_shares.py.
"""

import time
import requests
from dataclasses import dataclass
from typing import Optional


# ── SEC EDGAR access policy ───────────────────────────────────────────────────
# The SEC requires all automated requests to identify themselves via a
# descriptive User-Agent header.  Using your email address is the standard
# accepted format.  Requests without a valid User-Agent may be blocked.
UA = {"User-Agent": "na"}

# Base URLs for the two EDGAR API endpoints we use:
#   SUBMISSIONS_BASE  — company metadata and filing index (data.sec.gov)
#   ARCHIVES_BASE     — actual filing documents (www.sec.gov/Archives/...)
SUBMISSIONS_BASE = "https://data.sec.gov"
ARCHIVES_BASE    = "https://www.sec.gov/Archives/edgar/data"

# ── Rate limiting ─────────────────────────────────────────────────────────────
# The SEC EDGAR fair-access policy caps automated requests at 10 per second.
# DEFAULT_DELAY of 0.11 s ≈ 9 req/s, leaving a small buffer below the limit.
# Increase this value (e.g. 0.5) if you are pulling many filings in one run
# to be more conservative.  429 responses are always retried automatically
# with exponential back-off regardless of the delay setting.
SEC_MAX_RPS    = 10
DEFAULT_DELAY  = 0.11


# ── Custom exception ──────────────────────────────────────────────────────────

class SECBlockedError(RuntimeError):
    """
    Raised when the SEC EDGAR servers are still returning 429 after all
    retry attempts have been exhausted.

    This typically means the IP has been placed in a 10-minute cooling-off
    period by the SEC's rate-limiting infrastructure.  The safe response is
    to stop all requests and wait at least 10 minutes before retrying.
    """


# ── HTTP helper ───────────────────────────────────────────────────────────────

def _get_with_retry(
    url: str,
    session: requests.Session,
    delay: float,
    retries: int = 4,
) -> requests.Response:
    """
    HTTP GET with automatic retry on rate-limit (429) and server errors (5xx).

    Back-off formula on 429:  min(delay * 10 * 2^attempt, 60s)
    So with delay=0.11 the waits are roughly 1.1 s, 2.2 s, 4.4 s, 8.8 s.
    A hard ceiling of 60 s prevents indefinitely long waits.

    Raises SECBlockedError if a 429 is still returned after all retries —
    this signals a 10-minute IP ban rather than a transient rate spike.
    """
    for attempt in range(retries):
        r = session.get(url, timeout=30)

        if r.status_code == 429:
            # SEC rate-limit hit — wait with exponential back-off and retry
            wait = min(delay * 10 * (2 ** attempt), 60.0)
            print(f"  [429 rate-limit] sleeping {wait:.0f}s (attempt {attempt + 1}/{retries})")
            time.sleep(wait)
            continue

        if r.status_code >= 500 and attempt < retries - 1:
            # Transient server error — short wait then retry
            time.sleep(delay * 5)
            continue

        r.raise_for_status()
        time.sleep(delay)   # polite inter-request pause
        return r

    # All retries exhausted — make one final attempt
    r = session.get(url, timeout=30)
    if r.status_code == 429:
        # Still blocked after all back-offs — SEC 10-minute IP ban in effect
        raise SECBlockedError(
            "SEC rate limit: still receiving 429 after all retries. "
            "The SEC enforces a 10-minute cooling-off period when the rate "
            "limit is exceeded. Stop all requests and wait 10 minutes."
        )
    r.raise_for_status()
    time.sleep(delay)
    return r


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class FilingRecord:
    """
    Metadata for a single SEC filing.

    filing_url   — direct URL to the primary filing document (HTML)
    index_url    — URL to the filing's index folder (lists all attached files)
    is_xbrl      — True when the filing includes inline XBRL (iXBRL) tagging,
                   which enables structured data extraction
    """
    cik: str
    entity_name: str
    ticker: str
    form_type: str
    filing_date: str
    report_date: Optional[str]
    accession_number: str
    primary_document: str
    filing_url: str
    index_url: str
    file_number: Optional[str]
    act: Optional[str]
    size: Optional[int]
    is_xbrl: bool


# ── Internal helpers ──────────────────────────────────────────────────────────

def _pad_cik(cik: str | int) -> str:
    # EDGAR's submissions endpoint requires the CIK zero-padded to 10 digits,
    # e.g. CIK 320193 → "0000320193"
    return str(cik).lstrip("0").zfill(10)


def _build_urls(cik_int: int, accession: str, primary_doc: str) -> tuple[str, str]:
    # Accession numbers like "0000320193-25-000079" map to folder "000032019325000079"
    folder = accession.replace("-", "")
    filing_url = f"{ARCHIVES_BASE}/{cik_int}/{folder}/{primary_doc}"
    index_url  = f"{ARCHIVES_BASE}/{cik_int}/{folder}/"
    return filing_url, index_url


def _parse_recent_filings(
    data: dict,
    cik_int: int,
    entity_name: str,
    ticker: str,
    form_filter: set[str],
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[FilingRecord]:
    """
    Parse the 'filings.recent' section of a CIK submissions JSON response
    into a list of FilingRecord objects.

    EDGAR returns filings newest-first.  Once a filing_date falls before
    start_date we can stop iterating because all remaining entries are older —
    this avoids scanning the entire history for large filers.
    """
    recent = data.get("filings", {}).get("recent", {})
    if not recent:
        return []

    # Each key in 'recent' is a parallel list — index i in every list
    # corresponds to the same filing.
    forms        = recent.get("form", [])
    accessions   = recent.get("accessionNumber", [])
    dates        = recent.get("filingDate", [])
    primary_docs = recent.get("primaryDocument", [])
    report_dates = recent.get("reportDate", [])
    file_numbers = recent.get("fileNumber", [])
    acts         = recent.get("act", [])
    sizes        = recent.get("size", [])
    is_xbrls     = recent.get("isXBRL", [])

    def _get(lst, i):
        return lst[i] if i < len(lst) else None

    records = []
    for i, form in enumerate(forms):
        filing_date = _get(dates, i) or ""

        # Early exit — filings are newest-first so once we go before start_date
        # all remaining entries are also before it
        if start_date and filing_date and filing_date < start_date:
            break

        if end_date and filing_date and filing_date > end_date:
            continue

        if form_filter and form not in form_filter:
            continue

        accession   = _get(accessions, i) or ""
        primary_doc = _get(primary_docs, i) or ""
        filing_url, index_url = _build_urls(cik_int, accession, primary_doc)

        records.append(FilingRecord(
            cik              = str(cik_int),
            entity_name      = entity_name,
            ticker           = ticker,
            form_type        = form,
            filing_date      = filing_date,
            report_date      = _get(report_dates, i),
            accession_number = accession,
            primary_document = primary_doc,
            filing_url       = filing_url,
            index_url        = index_url,
            file_number      = _get(file_numbers, i),
            act              = _get(acts, i),
            size             = _get(sizes, i),
            is_xbrl          = bool(_get(is_xbrls, i)),
        ))
    return records


def _fetch_submissions(cik: str, session: requests.Session, delay: float = DEFAULT_DELAY) -> dict:
    """Fetch the submissions JSON for a single CIK from data.sec.gov."""
    url = f"{SUBMISSIONS_BASE}/submissions/CIK{_pad_cik(cik)}.json"
    return _get_with_retry(url, session, delay).json()


# ── Public API ────────────────────────────────────────────────────────────────

def fetch_filings_for_ciks(
    ciks: list[str | int],
    form_types: list[str] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    delay: float = DEFAULT_DELAY,
) -> list[FilingRecord]:
    """
    Fetch SEC filing metadata for one or more CIK codes.

    Hits the EDGAR submissions endpoint for each CIK, filters to the
    requested form types and date range, and returns a flat sorted list
    of FilingRecord objects ready for downstream extraction.

    Parameters
    ----------
    ciks : list of str or int
        SEC CIK codes.  Leading zeros are optional — "320193" and
        "0000320193" both work.
    form_types : list of str, optional
        Form types to include, e.g. ["10-K", "10-Q", "8-K"].
        Pass None or [] to return every form type.
    start_date : str, optional
        Earliest filing date to include, inclusive ("YYYY-MM-DD").
    end_date : str, optional
        Latest filing date to include, inclusive ("YYYY-MM-DD").
    delay : float
        Seconds between HTTP requests.  Default (0.11 s) stays safely
        under the SEC 10 req/s cap.  429 responses retry automatically.

    Returns
    -------
    list of FilingRecord
        Sorted by filing_date descending (most recent first).
    """
    form_filter = set(form_types) if form_types else set()
    results: list[FilingRecord] = []

    # A single session is reused across all CIK requests so the UA header
    # and any connection pooling are shared.  The try/finally ensures the
    # session is closed even if an exception is raised mid-loop.
    session = requests.Session()
    session.headers.update(UA)
    try:
        for raw_cik in ciks:
            cik_int = int(str(raw_cik).lstrip("0") or "0")
            try:
                data        = _fetch_submissions(str(raw_cik), session, delay)
                entity_name = data.get("name", "Unknown")
                tickers     = data.get("tickers", [])
                ticker      = tickers[0] if tickers else ""
                records     = _parse_recent_filings(
                    data, cik_int, entity_name, ticker,
                    form_filter, start_date, end_date,
                )
                results.extend(records)
                print(f"[sync] {cik_int:010d}  {entity_name:<35}  {len(records):>4} filings matched")
            except SECBlockedError:
                # IP is banned — propagate immediately so the caller can stop
                raise
            except requests.HTTPError as exc:
                print(f"[sync] CIK {raw_cik} HTTP {exc.response.status_code}: {exc}")
            except Exception as exc:
                print(f"[sync] CIK {raw_cik} error: {exc}")
    finally:
        session.close()

    results.sort(key=lambda r: r.filing_date, reverse=True)
    return results
