"""
sec_filing_manual_extract.py — Advanced / manual extraction from the raw full
submission text (the ``submission_txt_url``).

Where ``sec_xbrl_extract`` reads the *structured* inline-XBRL instance, this
module works off the **complete submission text file** — the full raw SGML
submission that EDGAR serves at ``<accession>.txt``.  It exists to get at the
things that are NOT cleanly machine-tagged:

  1. **A clean, self-contained HTML render of the primary document.**  The raw
     ``.txt`` is an SGML envelope wrapping the primary HTML document plus every
     exhibit (XBRL, certifications, graphics as uuencoded/base64 blobs, …), and
     the primary HTML itself is riddled with inline-XBRL (``ix:*``) wrapper tags
     and hidden metadata blocks.  ``clean_submission_html`` peels the envelope,
     keeps only the primary document, strips the inline-XBRL cruft (unwrapping
     the tags so the visible numbers/tables survive), and returns readable HTML
     that a browser can render.  ``process_submission(..., compress=True)`` then
     writes it out gzip-compressed (``*.clean.html.gz``).

  2. **Manual table extraction** — geographic asset tables, revenue-by-geography,
     and revenue product/operating-segment tables.  These live in the notes and
     are often only *partially* XBRL-tagged, so we pull them straight from the
     HTML with pandas, tagging each table by the keyword category it matched.

  3. **Concentration "footnote" statements** — the free-text disclosures such as
     "Substantially all of our revenue is derived from the United States" or
     "The majority of our long-lived assets are located in China".  There is no
     structured field for these; ``extract_concentration_statements`` scans the
     cleaned text for a quantifier + subject (revenue/assets) + geography and
     returns the sentence, the category, and the geography it names.

Design
------
This module is **additive** — it imports the shared, rate-limited SEC fetcher
from ``sec_filings_sync`` (treated as read-only) and does not modify any
existing file.  All SEC traffic still flows through the one polite request path
(``_get_with_retry``, 10 req/s cap with 429 back-off), exactly like
``sec_xbrl_extract``.

Usage
-----
    from methods.sec_filings_sync import fetch_filings_for_ciks
    from methods.sec_filing_manual_extract import process_submission

    filings = fetch_filings_for_ciks(ciks=["320193"], form_types=["10-K"])
    res = process_submission(filings[0], save_dir="clean_html", compress=True)

    res.clean_html         # cleaned, browser-renderable HTML (str)
    res.saved_path         # e.g. "clean_html/AAPL_10-K_2025-11-01_..clean.html.gz"
    res.tables             # list of matched tables (geographic/revenue/segment)
    res.catalog            # summary DataFrame of those tables
    res.statements         # DataFrame of concentration footnote sentences

Batch several filings at once:

    from methods.sec_filing_manual_extract import (
        process_filings, statements_frame, tables_frame,
    )
    results = process_filings(filings, save_dir="clean_html")
    all_statements = statements_frame(results)   # one flat DataFrame
    all_tables     = tables_frame(results)        # one flat catalog
"""

from __future__ import annotations

import gzip
import io
import os
import re
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd
from lxml import html as lxml_html

# Reuse the sync module's SEC access policy and rate-limited fetcher so *all*
# SEC traffic — metadata, XBRL, and now the full submission text — goes through
# a single polite request path.  We deliberately do not re-implement the
# 10 req/s back-off logic here.  (sec_filings_sync is treated as read-only.)
from methods.sec_filings_sync import (
    UA,
    DEFAULT_DELAY,
    SECBlockedError,
    _get_with_retry,
)

import requests

__all__ = [
    "ManualExtraction",
    "fetch_submission_text",
    "split_submission_documents",
    "pick_primary_document",
    "clean_submission_html",
    "clean_document_html",
    "save_clean_html",
    "extract_tables",
    "find_segment_tables",
    "extract_concentration_statements",
    "process_submission",
    "process_filings",
    "statements_frame",
    "tables_frame",
]


# ── Identity columns copied onto every output so results stay self-describing ──
# Mirrors sec_xbrl_extract._IDENTITY_FIELDS; kept local so this module has no
# dependency on that module's private names.
_IDENTITY_FIELDS = [
    "cik", "entity_name", "ticker", "form_type",
    "filing_date", "report_date", "accession_number",
]


# ══════════════════════════════════════════════════════════════════════════════
#  Keyword vocabularies — what makes a table / sentence "of interest"
# ══════════════════════════════════════════════════════════════════════════════

# A table is flagged for a category if any of its keywords appears in the table's
# nearby heading/caption text OR in the table's own cell text.  The matched
# category names and keywords are recorded so a caller can filter precisely.
_TABLE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "geographic": (
        "geographic", "geographical", "geography", "by country", "by region",
        "by geographic area", "long-lived assets", "long lived assets",
        "outside the united states", "property, plant and equipment by",
        "assets by geographic", "net assets by",
    ),
    "revenue": (
        "disaggregation of revenue", "revenue by", "net revenue", "net sales",
        "total revenue", "revenues", "revenue", "sales by",
    ),
    "segment": (
        "reportable segment", "operating segment", "reportable operating",
        "segment information", "segment", "segments",
    ),
    "product": (
        "products and services", "product and service", "by product",
        "revenue by product", "product line", "products", "product",
    ),
}

# Categories that make a table "interesting" for manual extraction (all of them).
_INTEREST_CATEGORIES = tuple(_TABLE_KEYWORDS.keys())


# --- Concentration ("footnote") statement vocabulary --------------------------

# Quantifier phrases that signal a concentration statement.
_QUANTIFIERS = (
    "substantially all", "substantial majority", "substantial portion",
    "vast majority", "the majority", "a majority", "majority of",
    "significant portion", "a significant portion", "large portion",
    "predominantly", "primarily", "nearly all", "almost all",
    "the substantial majority", "concentrated in", "concentration of",
)

# "primarily"/"predominantly" are weak: they also head the ubiquitous MD&A
# explanation idiom ("net sales increased primarily due to …"), which is NOT a
# concentration statement.  When one of these is followed by an explanation
# continuation we don't treat it as a qualifying quantifier.
_WEAK_QUANTIFIERS = {"primarily", "predominantly"}
_EXPLANATION_TAIL_RE = re.compile(
    r"\b(?:primarily|predominantly)\s+(?:due to|attributable to|driven by|"
    r"related to|offset by|because|reflecting|resulting from|as a result|"
    r"a result of|the result of|the increase|the decrease|from (?:higher|lower|"
    r"increased|decreased|the))")

# Subject words, grouped so we can classify the statement's category.
_SUBJECTS: dict[str, tuple[str, ...]] = {
    "assets": (
        "long-lived assets", "long lived assets", "assets", "property",
        "properties", "property and equipment", "property, plant",
    ),
    "revenue": (
        "revenue", "revenues", "net revenue", "net sales", "sales", "turnover",
        "income",
    ),
    "operations": (
        "operations", "operating", "manufacturing", "workforce", "employees",
        "personnel", "business",
    ),
}

# Location cues — phrases that tie a subject to a *place*.  A qualifying
# sentence needs a quantifier + subject + (a named geography OR a STRONG cue).
# Strong cues assert physical location/operation; they qualify a sentence even
# when its place isn't in the geography list (e.g. "operations in the R.O.C.").
_STRONG_LOCATION_CUES = (
    "located in", "located outside", "located within", "located outside of",
    "based in", "operations in", "operate in", "operating in",
    "operations are located", "conducted in", "conduct our operations in",
    "conducts its business", "reside in", "residing in", "reside outside",
    "situated in", "concentrated in", "headquartered in",
    "in the united states", "outside the united states", "in china",
    "in the people's republic", "in the prc",
)
# Weak cues (derivation/attribution) are common in revenue-*composition* notes
# ("revenue is primarily generated from cloud services"), so they no longer
# qualify a match on their own — only alongside a named geography.
_WEAK_LOCATION_CUES = (
    "derived from", "derived within", "generated in", "generated from",
    "attributable to", "originate", "originated in", "sourced from",
)

# Named geographies, matched longest-first so "United States" wins over "States".
_GEOGRAPHIES = (
    "the People's Republic of China", "People's Republic of China",
    "the United States of America", "United States of America",
    "the United States", "United States", "the United Kingdom", "United Kingdom",
    "the Republic of Ireland", "the Netherlands", "the United Arab Emirates",
    "United Arab Emirates", "Saudi Arabia", "South Korea", "South Africa",
    "Hong Kong", "New Zealand", "Latin America", "North America", "South America",
    "the Americas", "Americas", "Greater China", "the Chinese mainland",
    "Chinese mainland", "mainland China", "Asia Pacific", "Asia-Pacific",
    "Asia Pacific region", "Middle East", "European Union", "the Caribbean",
    "the Cayman Islands", "Cayman Islands", "the Republic of Panama",
    "the R.O.C.", "R.O.C.", "the PRC", "the U.S.",
    "China", "Taiwan", "Japan", "Korea", "India", "Germany", "France",
    "Ireland", "Netherlands", "Switzerland", "Luxembourg", "Belgium", "Austria",
    "Italy", "Spain", "Portugal", "Sweden", "Norway", "Finland", "Denmark",
    "Poland", "Russia", "Ukraine", "Turkey", "Israel", "Egypt", "Nigeria",
    "Kenya", "Canada", "Mexico", "Brazil", "Argentina", "Chile", "Colombia",
    "Panama", "Singapore", "Malaysia", "Thailand", "Vietnam", "Indonesia",
    "Philippines", "Australia", "Bermuda", "Europe", "Africa", "Asia",
    "Oceania", "APAC", "EMEA", "LATAM", "PRC", "ROC", "U.S.", "U.K.", "USA", "UK",
)


# ══════════════════════════════════════════════════════════════════════════════
#  Networking — download the raw full-submission text
# ══════════════════════════════════════════════════════════════════════════════

def _session(session: requests.Session | None) -> tuple[requests.Session, bool]:
    """Return (session, owned).  Creates a UA-tagged session if none given.

    Mirrors sec_xbrl_extract._session so this module can run standalone while
    still funnelling requests through the shared UA + rate-limited fetcher.
    """
    if session is not None:
        return session, False
    s = requests.Session()
    s.headers.update(UA)
    return s, True


def _decode_submission(content: bytes) -> str:
    """
    Decode raw submission bytes to text, tolerating EDGAR's mixed encodings.

    Inline-XBRL era filings are UTF-8, but a great many primary documents are
    Windows-1252 (cp1252) — smart quotes (’ “ ”), en/em dashes, and the section
    sign (§) all live in the 0x80–0x9F range that is *invalid* UTF-8.  We try
    strict UTF-8 first (correct for modern filings), fall back to cp1252 (which
    renders those punctuation bytes properly instead of as U+FFFD), and finally
    latin-1 (never fails) so a stray byte can't kill a multi-megabyte submission.
    """
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            return content.decode(enc)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def fetch_submission_text(
    url: str,
    session: requests.Session | None = None,
    delay: float = DEFAULT_DELAY,
) -> str:
    """
    Download the complete submission text file (the ``<accession>.txt`` full
    SGML submission) and return it decoded to a string.

    Uses the shared, rate-limited SEC fetcher (10 req/s cap with 429 back-off).
    Raises ``SECBlockedError`` if EDGAR's rate limiter has issued a cooling-off
    ban — stop and wait 10 minutes, exactly as elsewhere in the pipeline.
    """
    sess, owned = _session(session)
    try:
        resp = _get_with_retry(url, sess, delay)
        return _decode_submission(resp.content)
    finally:
        if owned:
            sess.close()


# ══════════════════════════════════════════════════════════════════════════════
#  SGML envelope → individual documents
# ══════════════════════════════════════════════════════════════════════════════
#
# A full submission text file looks like:
#
#   <SEC-DOCUMENT>...
#   <SEC-HEADER>...</SEC-HEADER>
#   <DOCUMENT>
#   <TYPE>10-K
#   <SEQUENCE>1
#   <FILENAME>aapl-20250927.htm
#   <DESCRIPTION>10-K
#   <TEXT>
#   ...primary HTML (with inline-XBRL ix:* tags)...
#   </TEXT>
#   </DOCUMENT>
#   <DOCUMENT> ...EX-31.1 certification... </DOCUMENT>
#   <DOCUMENT> ...GRAPHIC (uuencoded)...   </DOCUMENT>
#   ...
#
# The <TYPE>/<SEQUENCE>/<FILENAME>/<DESCRIPTION> tags are SGML-style with no
# closing tag — the value is the remainder of that line.

_DOCUMENT_RE = re.compile(r"<DOCUMENT>(.*?)</DOCUMENT>", re.DOTALL | re.IGNORECASE)
_TEXT_RE     = re.compile(r"<TEXT>(.*?)</TEXT>", re.DOTALL | re.IGNORECASE)


def _sgml_field(block: str, name: str) -> Optional[str]:
    """Value of a no-close SGML header field (<TYPE>, <FILENAME>, …) in a block."""
    m = re.search(rf"<{name}>\s*([^\r\n<]+)", block, re.IGNORECASE)
    return m.group(1).strip() if m else None


def split_submission_documents(submission_text: str) -> list[dict]:
    """
    Split a full submission text file into its constituent documents.

    Returns one dict per ``<DOCUMENT>`` with keys:
        seq, type, filename, description, text  (text = the <TEXT> body)

    The ``text`` of the primary HTML document is exactly what
    ``clean_document_html`` expects.
    """
    docs: list[dict] = []
    for i, m in enumerate(_DOCUMENT_RE.finditer(submission_text)):
        block = m.group(1)
        tmatch = _TEXT_RE.search(block)
        text = tmatch.group(1) if tmatch else ""
        docs.append({
            "seq":         _sgml_field(block, "SEQUENCE") or str(i + 1),
            "type":        _sgml_field(block, "TYPE"),
            "filename":    _sgml_field(block, "FILENAME"),
            "description": _sgml_field(block, "DESCRIPTION"),
            "text":        text,
        })
    return docs


_HTML_EXT_RE = re.compile(r"\.(?:htm|html)$", re.IGNORECASE)


def pick_primary_document(
    docs: list[dict],
    primary_document: str | None = None,
) -> Optional[dict]:
    """
    Choose the primary filing document from a split submission.

    Preference order:
      1. The document whose FILENAME matches the record's ``primary_document``.
      2. The lowest-sequence HTML document that is not an XBRL/graphic exhibit.
      3. The first HTML document of any kind.
    Returns None if the submission holds no HTML document.
    """
    if not docs:
        return None

    if primary_document:
        want = primary_document.strip().lower()
        for d in docs:
            if (d.get("filename") or "").strip().lower() == want:
                return d

    def _is_html(d: dict) -> bool:
        fn = d.get("filename") or ""
        return bool(_HTML_EXT_RE.search(fn))

    def _is_exhibit_type(d: dict) -> bool:
        t = (d.get("type") or "").upper()
        # EX-101.* = XBRL; GRAPHIC/ZIP/JSON = binary/aux; those aren't the doc.
        return t.startswith("EX-101") or t in ("GRAPHIC", "ZIP", "JSON", "XML")

    html_docs = [d for d in docs if _is_html(d)]
    if not html_docs:
        return None

    def _seq_key(d: dict) -> int:
        try:
            return int(d.get("seq") or 9999)
        except (TypeError, ValueError):
            return 9999

    primaries = [d for d in html_docs if not _is_exhibit_type(d)]
    pool = primaries or html_docs
    return sorted(pool, key=_seq_key)[0]


# ══════════════════════════════════════════════════════════════════════════════
#  HTML cleaning — strip the inline-XBRL cruft, keep the readable document
# ══════════════════════════════════════════════════════════════════════════════

# Inline-XBRL wrapper tags carry no visible content of their own beyond the
# number/text they wrap.  We drop the two metadata containers outright and
# unwrap the rest (keeping their children/text).
_IX_DROP_WHOLE = ("ix:header", "ix:hidden", "ix:references", "ix:resources")


def _iter_all(el):
    """Yield every element node (skipping comments / PIs)."""
    for node in el.iter():
        if isinstance(node.tag, str):
            yield node


def clean_document_html(
    raw_html: str,
    strip_styles: bool = False,
    drop_scripts: bool = True,
    collapse_whitespace: bool = True,
) -> str:
    """
    Clean one primary HTML document into readable, browser-renderable HTML.

    Steps:
      * parse leniently (EDGAR HTML is not always well-formed);
      * drop inline-XBRL metadata blocks (``ix:header``/``ix:hidden``/…) that
        hold no visible content;
      * unwrap the remaining inline-XBRL wrapper tags (``ix:nonFraction``,
        ``ix:nonNumeric``, …) so the numbers and tables they wrap survive as
        plain HTML;
      * drop ``<script>`` (and, optionally, ``<style>`` + inline ``style=``);
      * remove HTML comments;
      * optionally collapse inter-tag whitespace to shrink the output.

    Parameters
    ----------
    raw_html : str
        The ``<TEXT>`` body of the primary document from the split submission.
    strip_styles : bool
        Also remove ``<style>`` elements and inline ``style`` attributes.
        Default False (keeps the filing's visual formatting).
    drop_scripts : bool
        Remove ``<script>`` elements.  Default True.
    collapse_whitespace : bool
        Collapse runs of whitespace between tags to trim file size.  Default True.

    Returns
    -------
    str
        Cleaned HTML, ready to write to a ``.html`` file or gzip.
    """
    doc = lxml_html.fromstring(raw_html)

    # 1. Drop inline-XBRL metadata containers entirely (case-insensitive match
    #    on the literal ix: tag, since the HTML parser doesn't resolve XML NS).
    for node in list(_iter_all(doc)):
        tag = node.tag.lower()
        if tag in _IX_DROP_WHOLE:
            parent = node.getparent()
            if parent is not None:
                parent.remove(node)

    # 2. Unwrap remaining inline-XBRL wrapper tags, preserving their content.
    for node in list(_iter_all(doc)):
        if node.tag.lower().startswith("ix:"):
            node.drop_tag()

    # 3. Remove scripts / styles as requested.
    if drop_scripts:
        for node in doc.xpath("//script"):
            node.drop_tree()
    if strip_styles:
        for node in doc.xpath("//style"):
            node.drop_tree()
        for node in doc.xpath("//*[@style]"):
            node.attrib.pop("style", None)

    # 4. Strip comments (lxml exposes them as elements with a callable tag).
    for node in doc.iter():
        if not isinstance(node.tag, str):
            parent = node.getparent()
            if parent is not None:
                parent.remove(node)

    html_out = lxml_html.tostring(doc, encoding="unicode", method="html")

    if collapse_whitespace:
        # Collapse whitespace runs that sit *between* tags only, so we never
        # touch text the reader sees.
        html_out = re.sub(r">\s+<", "> <", html_out)
        html_out = re.sub(r"[ \t\r\f\v]{2,}", " ", html_out)

    return html_out


def clean_submission_html(
    submission_text: str,
    primary_document: str | None = None,
    **clean_kwargs,
) -> tuple[str, Optional[dict], list[dict]]:
    """
    Convenience: split a full submission, pick its primary document, and clean it.

    Returns ``(clean_html, primary_doc_meta, all_docs)``.  ``clean_html`` is ""
    if the submission has no HTML primary document (rare — e.g. a graphics-only
    submission).
    """
    docs = split_submission_documents(submission_text)
    primary = pick_primary_document(docs, primary_document)
    if primary is None:
        return "", None, docs
    clean = clean_document_html(primary["text"], **clean_kwargs)
    return clean, primary, docs


# ── Saving (optionally gzip-compressed) ───────────────────────────────────────

def _safe_slug(*parts: object) -> str:
    """Build a filesystem-safe filename stem from identity parts."""
    raw = "_".join(str(p) for p in parts if p not in (None, "", "None"))
    return re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("_") or "filing"


def save_clean_html(
    clean_html: str,
    path: str,
    compress: bool = True,
) -> str:
    """
    Write cleaned HTML to disk.

    If ``compress`` is True the file is gzip-compressed and ``.gz`` is appended
    to ``path`` when not already present (so the result is a ``*.html.gz`` that
    a browser can open once decompressed, or that most viewers open directly).
    Returns the actual path written.
    """
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    if compress:
        if not path.endswith(".gz"):
            path = path + ".gz"
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            fh.write(clean_html)
    else:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(clean_html)
    return path


# ══════════════════════════════════════════════════════════════════════════════
#  Manual table extraction — geographic / revenue / segment / product
# ══════════════════════════════════════════════════════════════════════════════

def _norm(text: str | None) -> str:
    """Collapse whitespace in a text fragment."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def _preceding_caption(table_el, limit: int = 400) -> str:
    """
    Best-effort caption/heading for a table: the nearest non-empty text that
    precedes it in document order.  Walks preceding siblings, then ascends to
    the parent and repeats, so a heading a level or two up is still found.
    """
    node = table_el
    while node is not None:
        collected: list[str] = []
        for prev in node.itersiblings(preceding=True):
            t = _norm(prev.text_content()) if hasattr(prev, "text_content") else ""
            if t:
                collected.append(t)
            if sum(len(x) for x in collected) >= limit:
                break
        if collected:
            # siblings were walked nearest-first; reverse for reading order
            return _norm(" ".join(reversed(collected)))[-limit:]
        node = node.getparent()
    return ""


def _match_categories(text: str) -> dict[str, list[str]]:
    """Return {category: [matched keywords]} for the table categories present."""
    low = text.lower()
    hits: dict[str, list[str]] = {}
    for category, keywords in _TABLE_KEYWORDS.items():
        matched = [kw for kw in keywords if kw in low]
        if matched:
            hits[category] = matched
    return hits


def _tidy_frame(df: pd.DataFrame) -> pd.DataFrame:
    """
    Lightly de-noise a table parsed from filing HTML.

    Filing tables lean on empty cells (colspan/indentation, a lone "$" or ")"
    in its own column) for visual layout, which pandas surfaces as all-NaN rows
    and columns.  We drop only *fully* empty rows/columns — conservative enough
    that no real value is lost — and reset the index for readability.
    """
    if df is None or df.empty:
        return df
    df = df.dropna(axis=1, how="all").dropna(axis=0, how="all")
    return df.reset_index(drop=True)


def _table_to_frame(table_el, tidy: bool = True) -> Optional[pd.DataFrame]:
    """Parse a single <table> element into a DataFrame (None on failure)."""
    html_str = lxml_html.tostring(table_el, encoding="unicode", method="html")
    try:
        frames = pd.read_html(io.StringIO(html_str))
    except Exception:
        return None
    if not frames:
        return None
    return _tidy_frame(frames[0]) if tidy else frames[0]


def extract_tables(
    clean_html: str,
    categories: tuple[str, ...] | None = _INTEREST_CATEGORIES,
    identity: dict | None = None,
    tidy: bool = True,
) -> list[dict]:
    """
    Extract tables of interest from cleaned HTML.

    For each ``<table>`` we look at its nearby heading/caption plus its own cell
    text and record every keyword category it matches.  With ``categories`` set
    (the default — geographic / revenue / segment / product), only tables that
    match at least one of those are returned; pass ``categories=None`` to get
    *every* table with whatever categories it happened to match.

    Returns a list of dicts, each:
        table_index   — position of the <table> in the document (0-based)
        categories    — list of matched category names
        keywords      — {category: [matched keywords]}
        caption       — nearest preceding heading text (best effort)
        n_rows/n_cols — shape of the parsed table
        dataframe     — the parsed pandas DataFrame
        (+ identity columns if `identity` given)
    """
    if not clean_html.strip():
        return []
    doc = lxml_html.fromstring(clean_html)
    out: list[dict] = []
    for idx, table_el in enumerate(doc.xpath("//table")):
        caption   = _preceding_caption(table_el)
        table_txt = _norm(table_el.text_content())
        hits      = _match_categories(caption + " \n " + table_txt)
        if not hits:
            continue
        if categories is not None and not (set(hits) & set(categories)):
            continue

        frame = _table_to_frame(table_el, tidy=tidy)
        if frame is None or frame.empty:
            continue

        row = {
            "table_index": idx,
            "categories":  sorted(hits.keys()),
            "keywords":    hits,
            "caption":     caption[:200],
            "n_rows":      int(frame.shape[0]),
            "n_cols":      int(frame.shape[1]),
            "dataframe":   frame,
        }
        if identity:
            row = {**{k: identity.get(k) for k in _IDENTITY_FIELDS}, **row}
        out.append(row)
    return out


# Focused convenience wrappers ------------------------------------------------

def find_segment_tables(
    clean_html: str,
    category: str,
    identity: dict | None = None,
) -> list[dict]:
    """Tables matching a single category ('geographic'|'revenue'|'segment'|'product')."""
    if category not in _TABLE_KEYWORDS:
        raise ValueError(
            f"unknown category {category!r}; choose from {list(_TABLE_KEYWORDS)}")
    return extract_tables(clean_html, categories=(category,), identity=identity)


# ══════════════════════════════════════════════════════════════════════════════
#  Concentration "footnote" statements
# ══════════════════════════════════════════════════════════════════════════════

# Split cleaned text into sentence-ish spans on terminal punctuation.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?;])\s+(?=[A-Z(\"'])")

# Block-level boundaries.  Filing headings carry no terminal punctuation, so
# without a break they glue onto the following sentence and produce spurious
# quantifier+subject+geography co-occurrences.  We inject a newline at each
# block close / <br> before extracting text, so headings and paragraphs stay
# on their own lines and sentence splitting respects real boundaries.
_BR_RE = re.compile(r"(?is)<br\s*/?>")
# Match a block *closing* tag and keep it (capture group) — we insert a newline
# in front of it rather than removing it, so the HTML structure is preserved.
# (Stripping the close tags leaves thousands of unclosed elements that the
# lenient parser collapses into a broken tree, losing most of the text.)
_BLOCK_CLOSE_RE = re.compile(
    r"(?is)(</(?:p|div|tr|li|h[1-6]|td|th|section|article|ul|ol|table|thead|"
    r"tbody|caption|blockquote|figcaption)\s*>)")


def _html_to_text(clean_html: str) -> str:
    """
    Visible text of cleaned HTML, with block boundaries preserved as newlines.

    Runs of spaces/tabs within a line are collapsed, but block-level element
    boundaries become line breaks — so a heading (which has no closing period)
    does not merge into the paragraph beneath it.
    """
    if not clean_html.strip():
        return ""
    marked = _BR_RE.sub("\n", clean_html)
    marked = _BLOCK_CLOSE_RE.sub(r"\n\1", marked)   # newline BEFORE the close tag
    doc = lxml_html.fromstring(marked)
    text = doc.text_content()
    lines = (re.sub(r"[ \t\xa0]+", " ", ln).strip() for ln in text.split("\n"))
    return "\n".join(ln for ln in lines if ln)


def _iter_sentences(text: str):
    """Yield candidate sentences, respecting line breaks then terminal punctuation."""
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        for sentence in _SENTENCE_SPLIT_RE.split(line):
            s = sentence.strip()
            if s:
                yield s


def _find_geographies(sentence: str) -> list[str]:
    """Named geographies mentioned in a sentence (longest-match, de-duped)."""
    found: list[str] = []
    low = sentence.lower()
    for geo in _GEOGRAPHIES:                     # already longest-first
        g = geo.lower()
        if g in low and not any(g in f.lower() for f in found):
            found.append(geo)
    return found


def _classify_subject(sentence_low: str) -> tuple[Optional[str], Optional[str]]:
    """Return (category, matched_subject_phrase) for the first subject present."""
    for category, subjects in _SUBJECTS.items():
        for subj in subjects:                    # phrases are longest-first-ish
            if re.search(rf"\b{re.escape(subj)}\b", sentence_low):
                return category, subj
    return None, None


def extract_concentration_statements(
    source: str,
    identity: dict | None = None,
    is_html: bool = True,
    max_sentence_chars: int = 500,
) -> pd.DataFrame:
    """
    Scan text for geographic-concentration disclosures and return them tidily.

    A sentence qualifies when it contains **all three** of:
      * a quantifier ("substantially all", "the majority of", "primarily", …),
      * a subject (revenue / assets / operations), and
      * either a location cue ("located in", "derived from", …) or a named
        geography (United States, China, …).

    This catches statements such as:
      * "Substantially all of our revenue is derived from the United States."
      * "The majority of our long-lived assets are located in China."

    Parameters
    ----------
    source : str
        Cleaned HTML (default) or plain text — see ``is_html``.
    identity : dict, optional
        Filing identity columns to stamp onto each row.
    is_html : bool
        Treat ``source`` as HTML and extract its visible text first.  Set False
        if you already have plain text.
    max_sentence_chars : int
        Skip pathologically long "sentences" (usually un-split table dumps).

    Returns
    -------
    pandas.DataFrame with columns:
        [identity…], category, quantifier, subject, geographies, statement
    """
    text = _html_to_text(source) if is_html else source
    rows: list[dict] = []
    seen: set[str] = set()

    for s in _iter_sentences(text):
        if len(s) > max_sentence_chars:
            continue
        low = s.lower()

        # Pick the first qualifying quantifier — skipping a weak one
        # ("primarily"/"predominantly") when it heads an MD&A explanation.
        quant = None
        for q in _QUANTIFIERS:
            if q not in low:
                continue
            if q in _WEAK_QUANTIFIERS and _EXPLANATION_TAIL_RE.search(low):
                continue
            quant = q
            break
        if quant is None:
            continue

        category, subject = _classify_subject(low)
        if category is None:
            continue

        # Require a place: a named geography, or a strong locational cue for
        # places we don't enumerate.  Weak derivation cues alone don't qualify.
        geos = _find_geographies(s)
        has_strong_cue = any(cue in low for cue in _STRONG_LOCATION_CUES)
        if not geos and not has_strong_cue:
            continue

        # De-duplicate identical statements repeated across the filing.
        key = re.sub(r"\s+", " ", low).strip()
        if key in seen:
            continue
        seen.add(key)

        row = {
            "category":    category,
            "quantifier":  quant,
            "subject":     subject,
            "geographies": ", ".join(geos) if geos else None,
            "statement":   s,
        }
        if identity:
            row = {**{k: identity.get(k) for k in _IDENTITY_FIELDS}, **row}
        rows.append(row)

    cols = ([k for k in _IDENTITY_FIELDS] if identity else []) + [
        "category", "quantifier", "subject", "geographies", "statement"]
    return pd.DataFrame(rows, columns=cols)


# ══════════════════════════════════════════════════════════════════════════════
#  Orchestration — one filing, or a batch
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class ManualExtraction:
    """
    Result of manually processing one filing's full submission text.

    Attributes
    ----------
    identity          — filing identity columns (cik, ticker, form_type, …)
    primary_filename  — the primary document's filename (or None)
    documents         — metadata for every document in the submission
    clean_html        — cleaned, browser-renderable HTML of the primary document
    text              — visible plain text of the cleaned HTML
    tables            — list of matched tables of interest (see extract_tables)
    statements        — DataFrame of concentration footnote sentences
    saved_path        — where clean_html was written, if save_dir was given
    submission_txt_url— source URL processed
    """
    identity: dict
    primary_filename: Optional[str]
    documents: list[dict]
    clean_html: str
    text: str
    tables: list[dict] = field(default_factory=list)
    statements: pd.DataFrame = field(default_factory=pd.DataFrame)
    saved_path: Optional[str] = None
    submission_txt_url: Optional[str] = None

    @property
    def catalog(self) -> pd.DataFrame:
        """Summary DataFrame of the matched tables (without the frames themselves)."""
        if not self.tables:
            return pd.DataFrame(columns=[
                "table_index", "categories", "caption", "n_rows", "n_cols"])
        keep = ["table_index", "categories", "caption", "n_rows", "n_cols"]
        return pd.DataFrame([{k: t.get(k) for k in keep} for t in self.tables])

    def tables_for(self, category: str) -> list[pd.DataFrame]:
        """The parsed DataFrames whose match includes ``category``."""
        return [t["dataframe"] for t in self.tables
                if category in (t.get("categories") or [])]


def _record_to_dict(rec) -> dict:
    """Normalise a FilingRecord / dict / DataFrame-row / URL string to a dict."""
    if isinstance(rec, str):
        return {"submission_txt_url": rec}
    if isinstance(rec, dict):
        return rec
    if hasattr(rec, "__dataclass_fields__"):
        from dataclasses import asdict
        return asdict(rec)
    # any object with the expected attributes
    fields = _IDENTITY_FIELDS + ["submission_txt_url", "primary_document"]
    return {k: getattr(rec, k, None) for k in fields}


def process_submission(
    filing,
    session: requests.Session | None = None,
    delay: float = DEFAULT_DELAY,
    save_dir: str | None = None,
    compress: bool = True,
    want_tables: bool = True,
    want_statements: bool = True,
    clean_kwargs: dict | None = None,
) -> ManualExtraction:
    """
    Download and manually process one filing's full submission text.

    Given a ``FilingRecord`` (or dict / DataFrame row / raw ``submission_txt_url``
    string), this:
      1. downloads the full submission via the shared rate-limited fetcher;
      2. cleans the primary document into readable HTML (and writes it to
         ``save_dir`` as ``*.clean.html[.gz]`` if given);
      3. extracts geographic / revenue / segment / product tables; and
      4. extracts concentration "footnote" statements.

    Parameters
    ----------
    filing :
        A ``FilingRecord`` dataclass, a dict, a ``sync_df`` row (dict), or a raw
        ``submission_txt_url`` string.
    session : requests.Session, optional
        Reuse an existing UA-tagged session (created if omitted).
    delay : float
        Seconds between requests (shared SEC rate limiter). Default ~9 req/s.
    save_dir : str, optional
        If given, the cleaned HTML is written here.  No file is written if None.
    compress : bool
        Gzip the saved HTML (``*.clean.html.gz``).  Default True.
    want_tables / want_statements : bool
        Toggle the two extraction passes independently.
    clean_kwargs : dict, optional
        Passed through to ``clean_document_html`` (e.g. ``{"strip_styles": True}``).

    Returns
    -------
    ManualExtraction
    """
    rec = _record_to_dict(filing)
    url = rec.get("submission_txt_url")
    if not url:
        raise ValueError(
            "no submission_txt_url on the filing — pass a FilingRecord, a "
            "sync_df row, or the .txt URL string directly")

    identity = {k: rec.get(k) for k in _IDENTITY_FIELDS}
    primary_document = rec.get("primary_document")

    submission_text = fetch_submission_text(url, session=session, delay=delay)
    clean_html, primary, docs = clean_submission_html(
        submission_text, primary_document, **(clean_kwargs or {}))

    text = _html_to_text(clean_html) if clean_html else ""

    tables = (extract_tables(clean_html, identity=identity)
              if (want_tables and clean_html) else [])
    statements = (extract_concentration_statements(clean_html, identity=identity)
                  if (want_statements and clean_html)
                  else pd.DataFrame())

    saved_path = None
    if save_dir and clean_html:
        stem = _safe_slug(
            identity.get("ticker") or identity.get("cik"),
            identity.get("form_type"),
            identity.get("filing_date"),
            rec.get("accession_number"),
        )
        saved_path = save_clean_html(
            clean_html, os.path.join(save_dir, f"{stem}.clean.html"),
            compress=compress)

    return ManualExtraction(
        identity=identity,
        primary_filename=(primary or {}).get("filename"),
        documents=docs,
        clean_html=clean_html,
        text=text,
        tables=tables,
        statements=statements,
        saved_path=saved_path,
        submission_txt_url=url,
    )


def process_filings(
    filings,
    session: requests.Session | None = None,
    delay: float = DEFAULT_DELAY,
    save_dir: str | None = None,
    compress: bool = True,
    want_tables: bool = True,
    want_statements: bool = True,
    clean_kwargs: dict | None = None,
    verbose: bool = True,
) -> list[ManualExtraction]:
    """
    Run ``process_submission`` over many filings, sharing one session.

    Accepts a ``sync_df`` DataFrame, a list of ``FilingRecord`` dataclasses, a
    list of dicts, or a list of ``submission_txt_url`` strings.  A single bad
    filing is logged and skipped (except ``SECBlockedError``, which propagates
    so the caller can stop and wait out the ban).

    Returns a list of ``ManualExtraction`` (one per successfully processed
    filing).  Use ``statements_frame`` / ``tables_frame`` to flatten them.
    """
    if isinstance(filings, pd.DataFrame):
        records = filings.to_dict("records")
    else:
        records = list(filings)

    sess, owned = _session(session)
    results: list[ManualExtraction] = []
    try:
        for rec in records:
            rd = _record_to_dict(rec)
            label = (f"{rd.get('ticker') or rd.get('cik')} "
                     f"{rd.get('form_type')} {rd.get('filing_date')}")
            if not rd.get("submission_txt_url"):
                if verbose:
                    print(f"[manual] {label:<28} SKIP (no submission_txt_url)")
                continue
            try:
                res = process_submission(
                    rd, session=sess, delay=delay, save_dir=save_dir,
                    compress=compress, want_tables=want_tables,
                    want_statements=want_statements, clean_kwargs=clean_kwargs)
            except SECBlockedError:
                raise                       # propagate — caller must stop & wait
            except Exception as exc:        # noqa: BLE001 — one bad filing != halt
                if verbose:
                    print(f"[manual] {label:<28} ERROR: {exc}")
                continue

            results.append(res)
            if verbose:
                print(f"[manual] {label:<28} "
                      f"{len(res.tables):>2} tables, "
                      f"{len(res.statements):>2} statements"
                      f"{'  -> ' + res.saved_path if res.saved_path else ''}")
    finally:
        if owned:
            sess.close()
    return results


# ── Flatteners for batch results ──────────────────────────────────────────────

def statements_frame(results: list[ManualExtraction]) -> pd.DataFrame:
    """Concatenate the concentration statements from many results into one frame."""
    frames = [r.statements for r in results
              if isinstance(r.statements, pd.DataFrame) and not r.statements.empty]
    if not frames:
        return pd.DataFrame(columns=_IDENTITY_FIELDS + [
            "category", "quantifier", "subject", "geographies", "statement"])
    return pd.concat(frames, ignore_index=True)


def tables_frame(results: list[ManualExtraction]) -> pd.DataFrame:
    """
    Concatenate the matched-table catalogs from many results into one frame.

    Keeps the parsed ``dataframe`` in an object column so each table is still
    available programmatically alongside its identity and matched categories.
    """
    rows: list[dict] = []
    for r in results:
        for t in r.tables:
            rows.append({
                **{k: r.identity.get(k) for k in _IDENTITY_FIELDS},
                "table_index": t.get("table_index"),
                "categories":  t.get("categories"),
                "caption":     t.get("caption"),
                "n_rows":      t.get("n_rows"),
                "n_cols":      t.get("n_cols"),
                "dataframe":   t.get("dataframe"),
            })
    cols = _IDENTITY_FIELDS + [
        "table_index", "categories", "caption", "n_rows", "n_cols", "dataframe"]
    return pd.DataFrame(rows, columns=cols)
