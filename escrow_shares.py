"""
escrow_shares.py — Escrow / contingent share detector for SEC filings.

Scans the plain text of SEC filings for shares held in escrow, contingent
shares, earn-out shares, founder shares, and similar locked or restricted
arrangements.

There is no standard XBRL tag for escrow shares, so detection is entirely
text-based.  The approach:

  1. Scan for trigger keywords (escrow, contingent shares, earn-out shares,
     founder shares, performance shares, milestone shares).
  2. Around each hit, open a ±600/400-char context window.
  3. Inside the window, find the share count closest to a placement verb
     ("placed in escrow", "held in escrow", etc.).  Proximity is measured
     to the verb, not the bare keyword, so unrelated counts in the same
     paragraph are not accidentally captured.
  4. Validate: a bare mention of "escrow" with no placement verb and no
     named arrangement type (earnout, founder, etc.) is skipped.
  5. Classify the arrangement type (earnout → founder → performance →
     lockup → contingent → escrow → general) by priority-order pattern match.
  6. Extract a trigger-hint fragment (how/when shares are released).

Every finding includes source_text (the matched window, ≤500 chars) so
results can be manually reviewed.

Public API
----------
find_escrow_shares(filings, delay=DEFAULT_DELAY) -> pd.DataFrame
    Pass a list of FilingRecord objects from sec_filings_sync.py.
    Returns a DataFrame with one row per distinct (cik, shares, class)
    finding.  Returns an empty DataFrame with correct columns when nothing
    is found.

    Columns: cik, entity_name, form_type, filing_date,
             shares_in_escrow, share_class, escrow_type,
             trigger_hint, source_text.

scan_filing_for_escrow(record, session, delay) -> list[dict]
    Lower-level single-filing scanner.  Returns raw finding dicts without
    the entity-identity fields attached yet.
"""

from __future__ import annotations

import re

import pandas as pd
import requests
from bs4 import BeautifulSoup

from sec_filings_sync import FilingRecord, _get_with_retry, DEFAULT_DELAY, SECBlockedError
from sec_extractors import _make_session


# ── Text-cleaning helpers ─────────────────────────────────────────────────────

# Strips par-value boilerplate from class labels so "Class E Common Stock,
# $0.0001 par value" normalises to "Class E Common Stock" for dedup keys.
_PAR_VALUE_RE = re.compile(r",?\s*\$[\d.]+\s+par\s+value\b[^,;)]*", re.IGNORECASE)


def _clean_title(s: str) -> str:
    """Collapse whitespace and title-case a string."""
    return re.sub(r"\s+", " ", s).strip().title()


def _strip_html(html: str) -> str:
    """Strip HTML tags and return plain text with single spaces."""
    return BeautifulSoup(html, "html.parser").get_text(" ", strip=True)


# ── Trigger-keyword scanner ───────────────────────────────────────────────────
# Finds positions in text that are worth examining for escrow share counts.

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

# ── Share count pattern (used within a context window) ────────────────────────
# Matches: [N] [modifier]? [class_adj]? shares? [of class_of]?
# Groups: shares, modifier, cls_adj, cls_of
_WINDOW_SHARES_RE = re.compile(
    r"(?P<shares>[\d,]{3,})\s+"
    r"(?P<modifier>contingent|earn[- ]?out|earnout|founder|performance|milestone|restricted)?\s*"
    r"(?:(?P<cls_adj>[A-Za-z][^,\n.()]{2,60}?)\s+)?"
    r"shares?"
    r"(?:\s+of\s+(?P<cls_of>[^,\n.()]{4,100}))?",
    re.IGNORECASE,
)

# ── Escrow placement verbs ────────────────────────────────────────────────────
# Used to confirm a share count is actually being placed IN escrow
# (vs. just mentioned near the word "escrow" for unrelated reasons).
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

# ── Escrow-type classifiers ───────────────────────────────────────────────────
# Evaluated in priority order — first match wins.
_ESCROW_TYPE_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bearn[- ]?out\b|\bearnout\b",        re.IGNORECASE), "earnout"),
    (re.compile(r"\bfounder\b",                          re.IGNORECASE), "founder"),
    (re.compile(r"\bperformance\b|\bmilestone\b",        re.IGNORECASE), "performance"),
    (re.compile(r"\block[- ]?up\b",                      re.IGNORECASE), "lockup"),
    (re.compile(r"\bcontingent\b",                       re.IGNORECASE), "contingent"),
    (re.compile(r"\bspac\b|\bde[- ]?spac\b|\bmerger\b", re.IGNORECASE), "contingent"),
]

# ── Trigger-hint extraction ───────────────────────────────────────────────────
# Captures text describing WHEN shares are released.
_RELEASE_HINT_RE = re.compile(
    r"(?:to\s+be\s+released|released|vest|converted|forfeited|distributed)"
    r"[^.!?\n]{0,300}",
    re.IGNORECASE,
)

# ── Class-label cleanup ───────────────────────────────────────────────────────
_NORM_RE = re.compile(r"[^a-z0-9]+")

# Trims trailing grammatical noise from an extracted class label.
# e.g. "Class E common stock of Alliance to the Legacy..." → "Class E Common Stock"
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

_WINDOW_BEFORE = 600   # chars before trigger keyword to examine
_WINDOW_AFTER  = 400   # chars after  trigger keyword to examine
_SCAN_BYTES    = 800_000   # read at most 800 KB of each filing


# ── Helper functions ──────────────────────────────────────────────────────────

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
    """Pick the most informative class label from a window match."""
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
    Scan plain text for escrow / contingent share disclosures.

    Returns a list of raw finding dicts (no filing-identity fields).
    One dict per distinct (shares, normalised_class) pair.
    """
    results: list[dict] = []
    seen: set[tuple] = set()

    for trigger in _TRIGGER_RE.finditer(text):
        pos     = trigger.start()
        w_start = max(0, pos - _WINDOW_BEFORE)
        w_end   = min(len(text), pos + _WINDOW_AFTER)
        window  = text[w_start:w_end]

        trigger_pos_in_window = pos - w_start

        # Anchor proximity to the placement verb when present — prevents a
        # nearby unrelated share count from stealing the match.
        placement_match = _PLACEMENT_RE.search(window)
        anchor = placement_match.start() if placement_match else trigger_pos_in_window

        # Rank candidates by distance to anchor; break ties by preferring larger counts.
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
        # a captured event — skip entirely so we don't fall through to a worse match.
        _, n0, sm0 = candidates[0]
        if (n0, _norm_class(_best_class(sm0))) in seen:
            continue

        _, n, sm = candidates[0]
        share_class_raw = _best_class(sm)

        # Gate: only record when we have an explicit placement verb OR the trigger
        # keyword itself implies escrow — avoids bare "escrow" near an unrelated count.
        trigger_text      = trigger.group(0).lower()
        is_escrow_word    = "escrow" in trigger_text
        has_placement     = bool(_PLACEMENT_RE.search(window))
        is_named_type     = any(
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


# ── Sync per-filing scanner ───────────────────────────────────────────────────

def scan_filing_for_escrow(
    record: FilingRecord,
    session: requests.Session,
    delay: float = DEFAULT_DELAY,
) -> list[dict]:
    """
    Fetch a single filing's primary document and scan for escrow disclosures.

    Returns a list of raw finding dicts (entity-identity fields not yet attached).
    """
    resp = _get_with_retry(record.filing_url, session, delay)
    raw  = resp.content[:_SCAN_BYTES]
    text = _strip_html(raw.decode("utf-8", errors="replace"))
    findings = _find_escrow_in_text(text)
    for f in findings:
        f["form_type"]   = record.form_type
        f["filing_date"] = record.filing_date
    return findings


# ── Public API ────────────────────────────────────────────────────────────────

_COLS = [
    "cik", "entity_name", "form_type", "filing_date",
    "shares_in_escrow", "share_class", "escrow_type",
    "trigger_hint", "source_text",
]


def find_escrow_shares(
    filings: list[FilingRecord],
    delay: float = DEFAULT_DELAY,
) -> pd.DataFrame:
    """
    Scan a list of FilingRecords for escrow / contingent share disclosures.

    Fetches each primary document synchronously, strips HTML to plain text,
    then applies trigger-keyword scanning with proximity-anchored share matching
    and placement-verb validation.  No external APIs required.

    Parameters
    ----------
    filings : list of FilingRecord
        As returned by ``fetch_filings_for_ciks()`` in sec_filings_sync.py.
    delay : float
        Seconds between HTTP requests (SEC 10 req/s cap; default 0.11 s).

    Returns
    -------
    pd.DataFrame
        Columns: cik, entity_name, form_type, filing_date,
                 shares_in_escrow, share_class, escrow_type,
                 trigger_hint, source_text.
        Empty DataFrame (with correct columns) when nothing is found.
    """
    rows: list[dict] = []
    seen_global: set[tuple] = set()   # dedup across filings for same CIK

    session = _make_session()
    try:
        for record in filings:
            try:
                findings = scan_filing_for_escrow(record, session, delay)
            except SECBlockedError:
                # IP is banned — propagate immediately so the caller can stop
                raise
            except Exception as exc:
                print(f"  [escrow] {record.entity_name} {record.form_type}"
                      f" {record.filing_date}  error: {exc}")
                continue

            added = 0
            for f in findings:
                key = (record.cik, f["shares_in_escrow"], _norm_class(f["share_class"]))
                if key in seen_global:
                    continue
                seen_global.add(key)
                rows.append({
                    "cik":             record.cik,
                    "entity_name":     record.entity_name,
                    "form_type":       f["form_type"],
                    "filing_date":     f["filing_date"],
                    "shares_in_escrow": f["shares_in_escrow"],
                    "share_class":     f["share_class"],
                    "escrow_type":     f["escrow_type"],
                    "trigger_hint":    f["trigger_hint"],
                    "source_text":     f["source_text"],
                })
                added += 1

            print(f"  [escrow] {record.entity_name:<30} {record.form_type:<6}"
                  f" {record.filing_date}  findings={added}")
    finally:
        session.close()

    if not rows:
        return pd.DataFrame(columns=_COLS)

    df = pd.DataFrame(rows)
    df["filing_date"]      = pd.to_datetime(df["filing_date"], errors="coerce")
    df["shares_in_escrow"] = pd.to_numeric(df["shares_in_escrow"], errors="coerce")
    return df[_COLS].reset_index(drop=True)


