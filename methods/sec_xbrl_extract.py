"""
sec_xbrl_extract.py — Parse EDGAR-extracted XBRL instance documents into tidy
tables.

This module consumes the ``xbrl_instance_url`` produced by
``sec_filings_sync.fetch_filings_for_ciks`` and turns each extracted instance
document (e.g. "aapl-20260430_htm.xml") into a clean, long-format pandas
DataFrame with **one row per reported fact** — value, unit, period, and any
dimensional (segment) breakdown resolved into plain columns.

Why long ("tidy") format?
-------------------------
A raw XBRL instance stores three things apart from one another:

  * facts    — the numbers/strings, each pointing at a context + unit by *ref*
  * contexts — the period (instant vs. duration), entity (CIK), and dimensions
  * units    — USD, shares, USD/shares, pure

Raw, a fact like ``<us-gaap:Revenues contextRef="c-3" unitRef="usd">…`` is
meaningless on its own.  "Cleaning" XBRL means *resolving those references* so
every fact becomes one self-describing row.  Long format is the right
primitive: you never lose information and can always pivot it to a
financial-statement view or filter it down afterwards.

Usage
-----
    from methods.sec_filings_sync import fetch_filings_for_ciks
    from methods.sec_xbrl_extract import parse_filings

    filings = fetch_filings_for_ciks(ciks=["320193"], form_types=["10-Q"])
    facts   = parse_filings(filings)        # tidy long DataFrame

Parse a single instance directly (no metadata fetch needed):

    from methods.sec_xbrl_extract import fetch_and_parse
    df = fetch_and_parse(
        "https://www.sec.gov/Archives/edgar/data/320193/"
        "000032019326000011/aapl-20260430_htm.xml"
    )
"""

from __future__ import annotations

import requests
import re
import unicodedata

import pandas as pd
from lxml import etree

# Reuse the sync module's SEC access policy and rate-limited fetcher so *all*
# SEC traffic — metadata and XBRL documents alike — goes through a single
# polite request path.  We deliberately do not re-implement the 10 req/s
# back-off logic here; sharing it avoids two competing rate limiters hammering
# EDGAR from the same IP.  (sec_filings_sync is treated as read-only.)
from methods.sec_filings_sync import (
    UA,
    DEFAULT_DELAY,
    SECBlockedError,
    _get_with_retry,
)


# ── XBRL namespaces ───────────────────────────────────────────────────────────
# The XBRL 2.1 instance spec fixes these URIs.  Element tags in lxml come back
# fully qualified as "{namespace-uri}localName", so we match against the URIs
# rather than prefixes (prefixes vary from filing to filing).
NS_XBRLI  = "http://www.xbrl.org/2003/instance"     # context, unit, period, entity
NS_XBRLDI = "http://xbrl.org/2006/xbrldi"           # explicit/typed dimension members
NS_LINK   = "http://www.xbrl.org/2003/linkbase"     # footnote links (not facts)
NS_XSI    = "http://www.w3.org/2001/XMLSchema-instance"  # xsi:nil

# Namespaces whose elements are structural, never reported facts.  Any top-level
# element NOT in this set that carries a contextRef is treated as a fact.
_NON_FACT_NS = {NS_XBRLI, NS_LINK, NS_XBRLDI}


# ── Small helpers ─────────────────────────────────────────────────────────────

def _split_tag(tag: str) -> tuple[str | None, str]:
    """Split an lxml "{uri}local" tag into (namespace_uri, local_name)."""
    if tag.startswith("{"):
        uri, local = tag[1:].split("}", 1)
        return uri, local
    return None, tag


def _local(qname: str | None) -> str | None:
    """Drop the prefix from a QName measure, e.g. "iso4217:USD" -> "USD"."""
    if qname is None:
        return None
    qname = qname.strip()
    return qname.split(":", 1)[1] if ":" in qname else qname


def _to_float(text: str | None) -> float | None:
    """Parse a fact's text as a float, tolerating stray whitespace/commas.

    Extracted instance values are already fully scaled (the inline-XBRL
    ``scale``/``sign`` attributes are applied during EDGAR's extraction), so no
    scaling is needed here — just a straight numeric cast.
    """
    if text is None:
        return None
    t = text.strip().replace(",", "")
    if t == "":
        return None
    try:
        return float(t)
    except ValueError:
        return None


# ── Context / unit lookup tables ──────────────────────────────────────────────

def _parse_contexts(root: etree._Element) -> dict[str, dict]:
    """
    Build {context_id -> resolved context info}.

    Each entry carries the period (instant vs. duration), the entity CIK, and a
    dimensions dict mapping each axis QName to its member QName.  Dimensions can
    live in <entity><segment> or in <scenario>; we read both.
    """
    contexts: dict[str, dict] = {}

    for ctx in root.findall(f"{{{NS_XBRLI}}}context"):
        cid = ctx.get("id")
        if cid is None:
            continue

        # --- period ---------------------------------------------------------
        period      = ctx.find(f"{{{NS_XBRLI}}}period")
        period_type = None
        p_instant = p_start = p_end = None
        if period is not None:
            instant = period.find(f"{{{NS_XBRLI}}}instant")
            if instant is not None:
                period_type = "instant"
                p_instant   = (instant.text or "").strip() or None
            else:
                start = period.find(f"{{{NS_XBRLI}}}startDate")
                end   = period.find(f"{{{NS_XBRLI}}}endDate")
                period_type = "duration"
                p_start = (start.text or "").strip() if start is not None else None
                p_end   = (end.text or "").strip() if end is not None else None

        # --- entity CIK -----------------------------------------------------
        ident = ctx.find(f"{{{NS_XBRLI}}}entity/{{{NS_XBRLI}}}identifier")
        entity_cik = None
        if ident is not None and ident.text:
            # identifier is the 10-digit zero-padded CIK; strip zeros for display
            entity_cik = ident.text.strip().lstrip("0") or "0"

        # --- dimensions (segment + scenario) --------------------------------
        dims: dict[str, str] = {}
        containers = [
            ctx.find(f"{{{NS_XBRLI}}}entity/{{{NS_XBRLI}}}segment"),
            ctx.find(f"{{{NS_XBRLI}}}scenario"),
        ]
        for container in containers:
            if container is None:
                continue
            for member in container:
                _, tag = _split_tag(member.tag)
                axis = member.get("dimension")
                if axis is None:
                    continue
                if tag == "explicitMember":
                    dims[axis] = (member.text or "").strip()
                elif tag == "typedMember":
                    # Typed dimensions hold arbitrary inner XML; capture the
                    # inner text of the first child (or the raw text) as value.
                    child = next(iter(member), None)
                    dims[axis] = (
                        (child.text or "").strip() if child is not None
                        else (member.text or "").strip()
                    )

        contexts[cid] = {
            "period_type":   period_type,
            "period_instant": p_instant,
            "period_start":  p_start,
            "period_end":    p_end,
            "entity_cik":    entity_cik,
            "dimensions":    dims,
        }
    return contexts


def _parse_units(root: etree._Element) -> dict[str, str | None]:
    """
    Build {unit_id -> readable unit string}.

    Simple units:   <measure>iso4217:USD</measure>      -> "USD"
    Ratio units:    <divide><unitNumerator>…USD</…>
                            <unitDenominator>…shares</…> -> "USD/shares"
    """
    units: dict[str, str | None] = {}
    for unit in root.findall(f"{{{NS_XBRLI}}}unit"):
        uid = unit.get("id")
        if uid is None:
            continue
        divide = unit.find(f"{{{NS_XBRLI}}}divide")
        if divide is not None:
            num = divide.find(
                f"{{{NS_XBRLI}}}unitNumerator/{{{NS_XBRLI}}}measure")
            den = divide.find(
                f"{{{NS_XBRLI}}}unitDenominator/{{{NS_XBRLI}}}measure")
            units[uid] = f"{_local(num.text if num is not None else None)}/" \
                         f"{_local(den.text if den is not None else None)}"
        else:
            measures = unit.findall(f"{{{NS_XBRLI}}}measure")
            units[uid] = (
                "*".join(_local(m.text) for m in measures) if measures else None
            )
    return units


def _segment_str(dimensions: dict[str, str]) -> str:
    """Flatten a dimensions dict into a stable, human-readable string.

    "" for the default (consolidated, un-dimensioned) member — the figures you
    usually want first.  Otherwise "Axis=Member; Axis=Member" sorted by axis.
    """
    if not dimensions:
        return ""
    return "; ".join(f"{axis}={member}" for axis, member in sorted(dimensions.items()))


# ── Core parser (no network) ──────────────────────────────────────────────────

def parse_instance_bytes(content: bytes, source_url: str | None = None) -> pd.DataFrame:
    """
    Parse the bytes of an extracted XBRL instance document into a tidy
    long-format DataFrame — one row per reported fact.

    Parameters
    ----------
    content : bytes
        Raw XML bytes of the "*_htm.xml" instance document.
    source_url : str, optional
        Recorded in the ``source_url`` column for traceability.

    Returns
    -------
    pandas.DataFrame with columns:
        concept, namespace, value, value_num, is_numeric, is_nil, unit,
        decimals, period_type, period_start, period_end, period_instant,
        is_dimensioned, segment, dimensions, context_id, entity_cik, source_url
    """
    # recover=True lets us tolerate the occasional malformed entity/encoding
    # quirk in older filings rather than hard-failing the whole document.
    parser = etree.XMLParser(recover=True, huge_tree=True)
    root   = etree.fromstring(content, parser=parser)
    if root is None:
        raise ValueError("Could not parse XBRL instance (empty/invalid XML)")

    contexts = _parse_contexts(root)
    units    = _parse_units(root)

    rows: list[dict] = []
    for el in root:
        tag = el.tag
        # Skip comments/processing instructions (non-string tags) and any
        # element without a contextRef — only facts reference a context.
        if not isinstance(tag, str):
            continue
        context_ref = el.get("contextRef")
        if context_ref is None:
            continue

        uri, local = _split_tag(tag)
        if uri in _NON_FACT_NS:
            continue

        ctx      = contexts.get(context_ref, {})
        dims     = ctx.get("dimensions", {})
        unit_ref = el.get("unitRef")
        unit     = units.get(unit_ref) if unit_ref else None

        is_nil = el.get(f"{{{NS_XSI}}}nil") == "true"
        text   = None if is_nil else el.text
        value_num = _to_float(text)
        # A fact is numeric if it declares a unit; unit-less facts (dei text,
        # policy strings, etc.) stay as text even if they happen to parse.
        is_numeric = unit_ref is not None

        decimals = el.get("decimals")

        rows.append({
            "concept":        local,
            "namespace":      uri,
            "value":          None if text is None else text.strip(),
            "value_num":      value_num if is_numeric else None,
            "is_numeric":     is_numeric,
            "is_nil":         is_nil,
            "unit":           unit,
            "decimals":       decimals,
            "period_type":    ctx.get("period_type"),
            "period_start":   ctx.get("period_start"),
            "period_end":     ctx.get("period_end"),
            "period_instant": ctx.get("period_instant"),
            "is_dimensioned": bool(dims),
            "segment":        _segment_str(dims),
            "dimensions":     dims,          # dict, for precise programmatic access
            "context_id":     context_ref,
            "entity_cik":     ctx.get("entity_cik"),
            "source_url":     source_url,
        })

    return pd.DataFrame(rows, columns=[
        "concept", "namespace", "value", "value_num", "is_numeric", "is_nil",
        "unit", "decimals", "period_type", "period_start", "period_end",
        "period_instant", "is_dimensioned", "segment", "dimensions",
        "context_id", "entity_cik", "source_url",
    ])


# ── Networked parsers ─────────────────────────────────────────────────────────

def _session(session: requests.Session | None) -> tuple[requests.Session, bool]:
    """Return (session, owned).  Creates a UA-tagged session if none given."""
    if session is not None:
        return session, False
    s = requests.Session()
    s.headers.update(UA)
    return s, True


def fetch_and_parse(
    url: str,
    session: requests.Session | None = None,
    delay: float = DEFAULT_DELAY,
) -> pd.DataFrame:
    """
    Download one extracted XBRL instance document and parse it into a tidy
    long-format DataFrame.

    Uses the shared, rate-limited SEC fetcher (10 req/s cap with 429 back-off).
    Raises ``SECBlockedError`` if EDGAR's rate limiter has issued a cooling-off
    ban — stop and wait 10 minutes, exactly as with the metadata fetch.
    """
    sess, owned = _session(session)
    try:
        resp = _get_with_retry(url, sess, delay)
        return parse_instance_bytes(resp.content, source_url=url)
    finally:
        if owned:
            sess.close()


# Identity columns copied from each filing onto its facts, so a combined table
# stays self-describing.  Names match sec_filings_sync.FilingRecord fields.
_IDENTITY_FIELDS = [
    "cik", "entity_name", "ticker", "form_type",
    "filing_date", "report_date", "accession_number",
]


def _iter_records(filings) -> list[dict]:
    """Normalise input to a list of plain dicts.

    Accepts a pandas DataFrame (e.g. ``sync_df``), a list of FilingRecord
    dataclasses, or a list of dicts.
    """
    if isinstance(filings, pd.DataFrame):
        return filings.to_dict("records")
    records = []
    for f in filings:
        if isinstance(f, dict):
            records.append(f)
        elif hasattr(f, "__dataclass_fields__"):
            from dataclasses import asdict
            records.append(asdict(f))
        else:                       # any object with the expected attributes
            records.append({k: getattr(f, k, None)
                            for k in _IDENTITY_FIELDS + ["xbrl_instance_url"]})
    return records


def parse_filings(
    filings,
    session: requests.Session | None = None,
    delay: float = DEFAULT_DELAY,
    verbose: bool = True,
) -> pd.DataFrame:
    """
    Download and parse the XBRL instance for every filing that has one, then
    concatenate the results into a single tidy DataFrame.

    Each filing's facts are stamped with the filing's identity columns
    (cik, entity_name, ticker, form_type, filing_date, report_date,
    accession_number) so the combined table is self-describing.

    Parameters
    ----------
    filings :
        A ``sync_df`` DataFrame, a list of ``FilingRecord`` dataclasses, or a
        list of dicts.  Any row whose ``xbrl_instance_url`` is None/empty is
        skipped (e.g. 8-Ks and older non-inline-XBRL filings).
    session : requests.Session, optional
        Reuse an existing UA-tagged session; one is created if omitted.
    delay : float
        Seconds between requests (shared SEC rate limiter). Default ~9 req/s.
    verbose : bool
        Print per-filing progress.

    Returns
    -------
    pandas.DataFrame
        All facts across all filings, identity columns first.  Empty (with the
        full column set) if nothing had an XBRL instance.
    """
    records = _iter_records(filings)
    sess, owned = _session(session)

    frames: list[pd.DataFrame] = []
    try:
        for rec in records:
            url = rec.get("xbrl_instance_url")
            if not url:
                continue

            ident = {k: rec.get(k) for k in _IDENTITY_FIELDS}
            label = f"{ident.get('ticker') or ident.get('cik')} " \
                    f"{ident.get('form_type')} {ident.get('filing_date')}"
            try:
                df = fetch_and_parse(url, session=sess, delay=delay)
            except SECBlockedError:
                raise                    # propagate — caller must stop & wait
            except Exception as exc:     # noqa: BLE001 — one bad filing shouldn't halt the batch
                if verbose:
                    print(f"[xbrl] {label:<28} ERROR: {exc}")
                continue

            # Stamp identity columns at the front of the fact table.
            for k, v in ident.items():
                df.insert(0, k, v)
            # insert() prepends, so re-order identity columns to declared order
            df = df[_IDENTITY_FIELDS +
                    [c for c in df.columns if c not in _IDENTITY_FIELDS]]

            frames.append(df)
            if verbose:
                print(f"[xbrl] {label:<28} {len(df):>5} facts")
    finally:
        if owned:
            sess.close()

    if not frames:
        # Return an empty frame with the expected schema for a stable API.
        empty = parse_instance_bytes(
            b'<xbrl xmlns="http://www.xbrl.org/2003/instance"/>')
        for k in reversed(_IDENTITY_FIELDS):
            empty.insert(0, k, pd.Series(dtype=object))
        return empty

    return pd.concat(frames, ignore_index=True)


# ── Convenience: pivot to a wide period view ──────────────────────────────────

def pivot_concepts(
    facts: pd.DataFrame,
    consolidated_only: bool = True,
) -> pd.DataFrame:
    """
    Optional helper: reshape tidy facts into a wide concept×period grid for a
    single filing's numeric facts — handy for eyeballing a statement.

    Parameters
    ----------
    facts : DataFrame
        Output of parse_instance_bytes / fetch_and_parse (single filing).
    consolidated_only : bool
        Keep only the default (un-dimensioned) member — the top-line figures.
    """
    df = facts[facts["is_numeric"]].copy()
    if consolidated_only:
        df = df[~df["is_dimensioned"]]
    # A single period key: instant date, else the duration end date.
    df["period"] = df["period_instant"].fillna(df["period_end"])
    return (
        df.pivot_table(index="concept", columns="period",
                       values="value_num", aggfunc="first")
          .sort_index()
    )


# ══════════════════════════════════════════════════════════════════════════════
#  Cover-page transform:  entity level  ↔  security level
# ══════════════════════════════════════════════════════════════════════════════
#
# The DEI ("Document & Entity Information") cover page carries two conceptually
# different kinds of information:
#
#   * ENTITY level   — facts about the registrant as a whole: name, state of
#                      incorporation, principal office address, phone, filer
#                      category, fiscal period, etc.  One row per filing.
#
#   * SECURITY level — facts about each registered trading line (share class,
#                      listed note series, ADS, preferred…): title, ticker,
#                      exchange and — for equity — shares outstanding.  One row
#                      per (filing, security).
#
# The hard part is that filers tag securities in three different shapes, all of
# which this transform normalises to the same output:
#
#   1. MULTIPLE securities (Apple, Alphabet, BBVA): each is dimensioned by a
#      "security axis" — us-gaap:StatementClassOfStockAxis for domestic filers,
#      ifrs-full:ClassesOfShareCapitalAxis for IFRS/foreign (20-F) filers.
#   2. SINGLE security (Tesla, Disney, most companies): NO axis is used at all —
#      Security12bTitle / TradingSymbol / shares are reported un-dimensioned.
#      We synthesise one "default" security from those un-dimensioned facts.
#   3. Shares reported per class vs. as one un-dimensioned company total — the
#      total is attached to the sole equity security when there is exactly one.

# Dimension axes that separate registered securities.  Additional axes are also
# discovered per-filing from whatever axis the listing facts are tagged on.
_KNOWN_SECURITY_AXES = (
    "us-gaap:StatementClassOfStockAxis",       # domestic (us-gaap) filers
    "ifrs-full:ClassesOfShareCapitalAxis",     # IFRS / foreign (20-F, 40-F) filers
)
# Backwards-compatible alias.
STOCK_CLASS_AXIS = _KNOWN_SECURITY_AXES[0]

# dei concepts that describe a *listed security*, not the entity as a whole.
# Their presence un-dimensioned is what signals a single-security filer.
_LISTING_CONCEPTS = {
    "Security12bTitle", "TradingSymbol", "NoTradingSymbolFlag",
    "SecurityExchangeName", "SecurityTradingCurrency",
    "EntityListingParValuePerShare",
}

# Axes a listing fact may be tagged on that do NOT identify a distinct security:
#   EntityListingsExchangeAxis — the *same* security listed on multiple exchanges
#   LegalEntityAxis            — which subsidiary issued it (e.g. Entergy's utils)
# Treating these as security axes would split one security into several rows, so
# they are excluded from security identity.
_NON_SECURITY_AXES = {
    "dei:EntityListingsExchangeAxis",
    "dei:LegalEntityAxis",
}

# Friendly column names for the common un-dimensioned (entity-level) dei facts.
# Any dei concept not listed keeps its raw XBRL local name as its column.
_ENTITY_RENAME = {
    "EntityRegistrantName":               "registrant_name",
    "EntityCentralIndexKey":              "cik_dei",
    "EntityIncorporationStateCountryCode": "incorporation_state",
    "EntityTaxIdentificationNumber":      "tax_id",
    "EntityFileNumber":                   "file_number",
    "EntityFilerCategory":                "filer_category",
    "EntityCurrentReportingStatus":       "current_reporting_status",
    "EntityInteractiveDataCurrent":       "interactive_data_current",
    "EntityShellCompany":                 "shell_company",
    "EntitySmallBusiness":                "small_business",
    "EntityEmergingGrowthCompany":        "emerging_growth_company",
    "CurrentFiscalYearEndDate":           "fiscal_year_end",
    "DocumentFiscalYearFocus":            "fiscal_year_focus",
    "DocumentFiscalPeriodFocus":          "fiscal_period_focus",
    "DocumentType":                       "document_type",
    "DocumentPeriodEndDate":              "period_end",
    "AmendmentFlag":                      "amendment_flag",
    "EntityAddressAddressLine1":          "address_line1",
    "EntityAddressAddressLine2":          "address_line2",
    "EntityAddressCityOrTown":            "address_city",
    "EntityAddressStateOrProvince":       "address_state",
    "EntityAddressPostalZipCode":         "address_zip",
    "CityAreaCode":                       "phone_area_code",
    "LocalPhoneNumber":                   "phone_local",
    "EntityCommonStockSharesOutstanding": "common_shares_outstanding",
}

# Friendly column names for the per-security (dimensioned) dei facts.
_SECURITY_RENAME = {
    "Security12bTitle":                   "security_title",
    "TradingSymbol":                      "trading_symbol",
    "NoTradingSymbolFlag":                "no_trading_symbol_flag",
    "SecurityExchangeName":               "exchange",
    "EntityCommonStockSharesOutstanding": "shares_outstanding",
}


def _clean_text(val):
    """Tidy a cover-page string value: normalise non-breaking spaces, strip
    zero-width / directional Unicode format characters (common in ADR titles),
    and collapse runs of whitespace."""
    if not isinstance(val, str):
        return val
    val = val.replace("\xa0", " ")
    val = "".join(ch for ch in val if unicodedata.category(ch) != "Cf")
    val = re.sub(r"\s+", " ", val).strip()
    return val or None


def _member_label(member: str) -> str:
    """"us-gaap:CommonClassAMember" -> "CommonClassA"; drop prefix + 'Member'."""
    name = member.split(":", 1)[1] if ":" in member else member
    if name.endswith("Member"):
        name = name[:-len("Member")]
    return name


def _classify_security(member: str | None, title: str | None) -> str:
    """Classify a security from its axis member QName and/or 12(b) title text."""
    low = f"{member or ''} {title or ''}".lower()
    if any(t in low for t in ("note", "debenture", "bond")):
        return "debt"
    if "warrant" in low:
        return "warrant"
    # "unit" covers LP/LLC common units and SPAC units; the rest cover the
    # common/preferred/ordinary/ADS/capital-stock families.
    if any(t in low for t in ("stock", "class", "ordinary", "share",
                              "preferred", "depositary", "capital", "unit")):
        return "equity"
    return "other"


def _default_class_label(title: str | None) -> str | None:
    """A security_class label for an un-dimensioned (single) security."""
    low = (title or "").lower()
    if "common" in low:
        return "CommonStock"
    if "ordinary" in low:
        return "OrdinaryShares"
    if "capital" in low:
        return "CapitalStock"
    if "preferred" in low:
        return "PreferredStock"
    return None


def _num(val):
    """Best-effort numeric cast (handles thousands separators / None)."""
    if val is None:
        return None
    try:
        return float(str(val).replace(",", ""))
    except (ValueError, TypeError):
        return None


def _filing_key_col(facts: pd.DataFrame) -> str | None:
    """Column that uniquely identifies a filing, for per-filing grouping."""
    for c in ("accession_number", "source_url"):
        if c in facts.columns and facts[c].notna().any():
            return c
    return None


def _dei_frame(facts: pd.DataFrame) -> pd.DataFrame:
    """Rows belonging to the SEC DEI (cover page) taxonomy."""
    return facts[facts["namespace"].fillna("").str.contains("/dei/")].copy()


def _discover_security_axes(fsub: pd.DataFrame) -> set[str]:
    """Security axes in play for one filing: the known axes plus whatever axis
    the filing actually tags its listing facts on, minus axes that describe
    listing *location* or *issuer* rather than security identity."""
    axes = set(_KNOWN_SECURITY_AXES)
    for dims in fsub.loc[fsub["concept"].isin(_LISTING_CONCEPTS), "dimensions"]:
        if isinstance(dims, dict):
            axes |= set(dims)
    return axes - _NON_SECURITY_AXES


def _security_member(dims, axes: set[str]) -> str | None:
    """The member of `dims` identifying the security: the canonical security
    axes take priority (so a subsidiary bond keys off the bond, not its issuer),
    then any other discovered security axis.  Returns None if none apply."""
    if not isinstance(dims, dict):
        return None
    for ax in _KNOWN_SECURITY_AXES:
        if ax in dims:
            return dims[ax]
    for ax in dims:
        if ax in axes:
            return dims[ax]
    return None


def _pivot_cover(sub: pd.DataFrame, rename: dict) -> dict:
    """First non-null value of each dei concept in `sub`, renamed to friendly
    column names where known."""
    out: dict = {}
    for concept, g in sub.groupby("concept"):
        vals = g["value"].dropna()
        out[rename.get(concept, concept)] = _clean_text(
            vals.iloc[0]) if len(vals) else None
    return out


def entity_facts(facts: pd.DataFrame) -> pd.DataFrame:
    """
    Collapse the cover page to ENTITY level — one row per filing.

    Takes the tidy fact table from ``parse_filings`` / ``fetch_and_parse`` and
    returns the un-dimensioned DEI cover facts (registrant name, incorporation
    state, principal office, phone, filer category, fiscal period, and the
    single-class common-shares total) pivoted wide, one row per filing.

    Company-specific and less common dei concepts are preserved under their raw
    XBRL names so nothing is dropped.
    """
    dei  = _dei_frame(facts)
    dei  = dei[~dei["is_dimensioned"]]                # entity = un-dimensioned
    dei  = dei[~dei["concept"].isin(_LISTING_CONCEPTS)]  # listing → security level
    key  = _filing_key_col(facts)
    carry = [c for c in _IDENTITY_FIELDS if c in facts.columns]

    groups = dei.groupby(key, sort=False) if key else [(None, dei)]
    rows = []
    for _, sub in groups:
        if sub.empty:
            continue
        ident = {c: sub[c].iloc[0] for c in carry}
        rows.append({**ident, **_pivot_cover(sub, _ENTITY_RENAME)})

    out = pd.DataFrame(rows)
    if out.empty:
        return out

    # Combine phone parts and coerce the share total to a real number.
    if {"phone_area_code", "phone_local"} <= set(out.columns):
        out["phone"] = out.apply(
            lambda r: f"({r['phone_area_code']}) {r['phone_local']}"
            if pd.notna(r.get("phone_area_code")) and pd.notna(r.get("phone_local"))
            else None, axis=1)
    if "common_shares_outstanding" in out.columns:
        out["common_shares_outstanding"] = out["common_shares_outstanding"].map(_num)

    # Order: identity → the most-used cover fields → everything else.
    preferred = carry + [
        "registrant_name", "document_type", "period_end",
        "fiscal_year_focus", "fiscal_period_focus", "fiscal_year_end",
        "incorporation_state", "tax_id", "file_number", "filer_category",
        "address_line1", "address_line2", "address_city", "address_state",
        "address_zip", "phone", "common_shares_outstanding",
        "shell_company", "small_business", "emerging_growth_company",
    ]
    cols = [c for c in preferred if c in out.columns]
    cols += [c for c in out.columns if c not in cols]
    return out[cols]


def security_facts(facts: pd.DataFrame) -> pd.DataFrame:
    """
    Collapse the cover page to SECURITY level — one row per registered trading
    line (each share class, listed note series, ADS, preferred, etc.).

    Columns: identity + security_member (raw axis member, None for a single
    un-dimensioned security), security_class, security_type
    (equity/debt/warrant/other), security_title, trading_symbol,
    no_trading_symbol_flag, exchange, shares_outstanding.

    Handles all three tagging shapes (see module comment above): multi-security
    filers dimensioned by the us-gaap or ifrs-full security axis; single-security
    filers whose listing facts are un-dimensioned; and share totals reported
    either per class or as one un-dimensioned company total.
    """
    dei   = _dei_frame(facts)
    key   = _filing_key_col(facts)
    carry = [c for c in _IDENTITY_FIELDS if c in facts.columns]

    groups = dei.groupby(key, sort=False) if key else [(None, dei)]
    rows = []
    for _, fsub in groups:
        ident = {c: fsub[c].iloc[0] for c in carry}
        axes  = _discover_security_axes(fsub)
        member_col = fsub["dimensions"].map(lambda d: _security_member(d, axes))

        # Un-dimensioned common-shares total (single-security filers report one).
        undim_shares = fsub[(fsub["concept"] == "EntityCommonStockSharesOutstanding")
                            & member_col.isna()]
        undim_total = (_num(undim_shares["value"].dropna().iloc[0])
                       if undim_shares["value"].notna().any() else None)

        # Dimensioned securities: one group per security-axis member.
        dimd = fsub[member_col.notna()].assign(_m=member_col[member_col.notna()])

        # A single un-dimensioned security exists only if listing identity
        # (title / ticker) is reported without any security axis — this avoids
        # turning a lone company-wide share total into a phantom security.
        undim_listing = fsub[member_col.isna()
                             & fsub["concept"].isin(_LISTING_CONCEPTS)]
        has_default = undim_listing["value"].notna().any()

        built = []  # (row, security_type, has_shares)
        for member, msub in dimd.groupby("_m", sort=False):
            vals   = _pivot_cover(msub, _SECURITY_RENAME)
            stype  = _classify_security(member, vals.get("security_title"))
            shares = _num(vals.get("shares_outstanding"))
            row = {**ident, "security_member": member,
                   "security_class": _member_label(member), "security_type": stype}
            _fill_security_row(row, vals, shares)
            built.append([row, stype, shares is not None])

        if has_default:
            default_sub = fsub[member_col.isna() & fsub["concept"].isin(
                _LISTING_CONCEPTS | {"EntityCommonStockSharesOutstanding"})]
            vals   = _pivot_cover(default_sub, _SECURITY_RENAME)
            title  = vals.get("security_title")
            stype  = _classify_security(None, title)
            shares = _num(vals.get("shares_outstanding"))
            if shares is None:
                shares = undim_total
            row = {**ident, "security_member": None,
                   "security_class": _default_class_label(title),
                   "security_type": stype}
            _fill_security_row(row, vals, shares)
            built.append([row, stype, shares is not None])

        # OTC / non-§12(b) issuers file no listing facts at all — only an
        # un-dimensioned share total.  Synthesise a minimal common-stock
        # security so that count is not lost.
        if not built and undim_total is not None:
            row = {**ident, "security_member": None,
                   "security_class": "CommonStock", "security_type": "equity"}
            _fill_security_row(row, {}, undim_total)
            built.append([row, "equity", True])

        # Attach the un-dimensioned common-shares total to the security it
        # describes — the unique common/ordinary equity line that lacks a figure
        # of its own.  Restricted to common stock so a preferred/other equity is
        # never credited with the common share count (e.g. Entergy's ETI/PR).
        if undim_total is not None:
            commons = [b for b in built
                       if b[1] == "equity" and not b[2] and _is_common_stock(b[0])]
            if len(commons) == 1:
                commons[0][0]["shares_outstanding"] = undim_total
                commons[0][2] = True

        rows.extend(b[0] for b in built)

    if not rows:
        # Stable schema even when a filing carries no cover securities (e.g. a
        # 40-F with no DEI cover facts), so downstream column access is safe.
        return pd.DataFrame(columns=carry + [
            "security_member", "security_class", "security_type",
            "security_title", "trading_symbol", "no_trading_symbol_flag",
            "exchange", "shares_outstanding"])
    return pd.DataFrame(rows)


def _is_common_stock(row: dict) -> bool:
    """True when a security row is the common/ordinary equity line — the one an
    un-dimensioned EntityCommonStockSharesOutstanding total describes.  Matches
    the several ways filers name it: CommonStockMember, CommonClass*Member
    (e.g. Morgan Stanley, Alphabet), CapitalClass*, ordinary shares, LP units."""
    member = (row.get("security_member") or "").lower()
    cls    = (row.get("security_class") or "").lower()
    member_keys = ("commonstock", "commonclass", "capitalclass", "capitalstock",
                   "ordinaryshares", "commonunit")
    return (any(k in member for k in member_keys)
            or cls in ("commonstock", "ordinaryshares", "capitalstock",
                       "commonunits", "commonunit"))


def _fill_security_row(row: dict, vals: dict, shares) -> None:
    """Populate the standard per-security fields on `row` from pivoted vals."""
    row["security_title"]         = vals.get("security_title")
    row["trading_symbol"]         = vals.get("trading_symbol")
    row["no_trading_symbol_flag"] = vals.get("no_trading_symbol_flag")
    row["exchange"]               = vals.get("exchange")
    row["shares_outstanding"]     = shares
    # Preserve any other per-security dei concepts not already mapped.
    for k, v in vals.items():
        row.setdefault(k, v)


def cover_pages(facts: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Convenience: return (entity_facts, security_facts) in one call."""
    return entity_facts(facts), security_facts(facts)
