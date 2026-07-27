"""
profiles.py — "fact fine tuning": choose which XBRL concepts an extraction run
stores, by name, instead of storing all 342,054 of them.

Why this exists
---------------
Level 3 stores **everything** by default, and that is the right default: a
concept you did not store cannot be recovered without re-fetching the filing,
and the fetch is the expensive part.  But a full mirror is tens of millions of
rows, and most research questions want a handful of fields.  A run that only
feeds country assignment does not need 342,054 concepts — it needs nine.

A **field group** is a named set of concepts.  Passing one to ``extract`` keeps
just those facts, so the database grows at a fraction of the rate::

    all           store every fact                        (default)
    cover         the whole DEI cover page                 ~50 concepts
    headquarters  address + state/country of incorporation
    shares        share counts, tickers, exchanges, titles
    identity      registrant name, CIK, filer category
    document      form type, fiscal period, amendment flags
    financials    the core statement lines

Groups compose, so ``headquarters,shares`` is a legal spec, as is a bare
concept name for something no group covers.

What filtering does and does not save
-------------------------------------
It saves **storage**, not requests.  The instance document arrives in one
request either way and is parsed in full; the filter decides what is written.
So a lean run costs the same time as a full one and produces a much smaller
database — and re-running later with a wider group means re-fetching.

Which group Level 3.5 needs
---------------------------
``cover.py`` builds ``entity_cover`` and ``security_cover`` out of stored
facts, so it needs the ``cover`` group at minimum.  Extract with anything
narrower and the cover build will find nothing to work with.

Usage
-----
    python -m finalized.cli facts --list
    python -m finalized.cli facts --show headquarters
    python -m finalized.cli facts --coverage cover
    python -m finalized.cli extract --run --facts all
    python -m finalized.cli extract --run --facts headquarters,shares

...or from Python::

    from finalized.profiles import resolve_fields, FIELD_GROUPS
    keep = resolve_fields("headquarters,shares")     # -> set of concepts
    keep = resolve_fields("all")                     # -> None (store everything)
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from typing import Iterable, Optional

from .database import DEFAULT_DB_PATH

# The spec that means "no filtering".  Kept as a word rather than an empty
# value so that "store everything" is something a user types on purpose.
ALL = "all"

_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")


# ── the built-in groups ───────────────────────────────────────────────────────
#
# All of these are DEI (cover page) concepts except ``financials``.  Concept
# names are the XBRL local names exactly as they appear in ``xbrl_facts.concept``
# — no namespace prefix, because that is how the parser stores them.

_IDENTITY = (
    "EntityRegistrantName",
    "EntityCentralIndexKey",
    "EntityFilerCategory",
    "EntityTaxIdentificationNumber",
    "EntityFileNumber",
    "EntityCurrentReportingStatus",
    "EntityInteractiveDataCurrent",
    "EntityShellCompany",
    "EntitySmallBusiness",
    "EntityEmergingGrowthCompany",
    "EntityExTransitionPeriod",
    "EntityWellKnownSeasonedIssuer",
    "EntityVoluntaryFilers",
    "EntityPublicFloat",
)

# The address block plus incorporation.  NOTE: EntityAddressCountry is sparse —
# 21.8% of periodic-report covers and 10.5% of all extracted filings — because
# US registrants leave it blank and imply the country from the state code.  So
# country must be DERIVED from the state/incorporation codes, not read straight
# out of this field.  Those codes are EDGAR's own, NOT ISO: 'CA' is California
# here, and Canada elsewhere in the same database.
_HEADQUARTERS = (
    "EntityAddressAddressLine1",
    "EntityAddressAddressLine2",
    "EntityAddressAddressLine3",
    "EntityAddressCityOrTown",
    "EntityAddressStateOrProvince",
    "EntityAddressPostalZipCode",
    "EntityAddressCountry",
    "EntityIncorporationStateCountryCode",
    "CityAreaCode",
    "LocalPhoneNumber",
)

# Security-level facts.  These are the ones that arrive dimensioned by share
# class on a multi-class filer, which is what Level 3.5 reassembles.
_SHARES = (
    "EntityCommonStockSharesOutstanding",
    "Security12bTitle",
    "TradingSymbol",
    "NoTradingSymbolFlag",
    "SecurityExchangeName",
    "SecurityTradingCurrency",
    "EntityListingParValuePerShare",
    "EntityListingDepositoryReceiptRatio",
)

_DOCUMENT = (
    "DocumentType",
    "DocumentPeriodEndDate",
    "DocumentFiscalYearFocus",
    "DocumentFiscalPeriodFocus",
    "CurrentFiscalYearEndDate",
    "AmendmentFlag",
    "AmendmentDescription",
    "DocumentQuarterlyReport",
    "DocumentAnnualReport",
    "DocumentTransitionReport",
    "DocumentFinStmtErrorCorrectionFlag",
)

# Deliberately small: the handful of us-gaap lines that almost every filer
# reports, for sanity checks and coarse screens.  This is not a substitute for
# a full extraction if you want real financial analysis.
_FINANCIALS = (
    "Assets",
    "Liabilities",
    "StockholdersEquity",
    "LiabilitiesAndStockholdersEquity",
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "OperatingIncomeLoss",
    "NetIncomeLoss",
    "CashAndCashEquivalentsAtCarryingValue",
    "EarningsPerShareBasic",
    "EarningsPerShareDiluted",
    "CommonStockSharesIssued",
    "CommonStockSharesOutstanding",
)

FIELD_GROUPS: dict[str, tuple[str, tuple[str, ...]]] = {
    "identity": ("Registrant name, CIK, filer category, public float",
                 _IDENTITY),
    "headquarters": ("Principal executive office address + incorporation",
                     _HEADQUARTERS),
    "shares": ("Share counts, tickers, exchanges, 12(b) titles", _SHARES),
    "document": ("Form type, fiscal period, amendment flags", _DOCUMENT),
    "financials": ("Core statement lines (Assets, Revenues, NetIncomeLoss...)",
                   _FINANCIALS),
}

# The composite Level 3.5 depends on.  Declared from the parts so the two can
# never drift: widening 'shares' automatically widens 'cover'.
FIELD_GROUPS["cover"] = (
    "Everything Level 3.5 needs: identity + headquarters + shares + document",
    tuple(dict.fromkeys(_IDENTITY + _HEADQUARTERS + _SHARES + _DOCUMENT)),
)

# What ``cover.py`` cannot run without.  Used to warn before a build that would
# silently produce empty tables.
COVER_REQUIRED = frozenset(_HEADQUARTERS + _SHARES + ("EntityRegistrantName",
                                                      "EntityFilerCategory"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fact_groups (
    group_name  TEXT PRIMARY KEY,
    description TEXT,
    created_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fact_group_concepts (
    group_name TEXT NOT NULL,
    concept    TEXT NOT NULL,
    PRIMARY KEY (group_name, concept),
    FOREIGN KEY (group_name) REFERENCES fact_groups(group_name) ON DELETE CASCADE
);
"""


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


# ── custom groups ─────────────────────────────────────────────────────────────

def define_group(
    name: str,
    concepts: Iterable[str],
    description: Optional[str] = None,
    db_path: str = DEFAULT_DB_PATH,
) -> int:
    """
    Create or replace a user-defined field group, stored in the database.

    A custom group may not shadow a built-in one — otherwise the meaning of
    ``--facts cover`` would depend on which database you happened to open.
    """
    if not _NAME.match(name):
        raise ValueError(f"group name {name!r} must be lowercase [a-z0-9_-]")
    if name in FIELD_GROUPS or name == ALL:
        raise ValueError(f"{name!r} is a built-in group; pick another name")
    concepts = [c.strip() for c in concepts if c and c.strip()]
    if not concepts:
        raise ValueError("a group needs at least one concept")

    conn = _connect(db_path)
    try:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            "INSERT INTO fact_groups (group_name, description, created_utc) "
            "VALUES (?,?,?) ON CONFLICT(group_name) DO UPDATE SET "
            "description=excluded.description",
            (name, description, now))
        conn.execute("DELETE FROM fact_group_concepts WHERE group_name=?", (name,))
        conn.executemany(
            "INSERT INTO fact_group_concepts (group_name, concept) VALUES (?,?)",
            [(name, c) for c in concepts])
        conn.commit()
        return len(concepts)
    finally:
        conn.close()


def custom_groups(db_path: str = DEFAULT_DB_PATH) -> dict[str, tuple[str, tuple]]:
    """User-defined groups, in the same shape as :data:`FIELD_GROUPS`."""
    conn = _connect(db_path)
    try:
        out = {}
        for r in conn.execute("SELECT group_name, description FROM fact_groups"):
            concepts = tuple(
                x["concept"] for x in conn.execute(
                    "SELECT concept FROM fact_group_concepts WHERE group_name=? "
                    "ORDER BY concept", (r["group_name"],)))
            out[r["group_name"]] = (r["description"] or "(no description)",
                                    concepts)
        return out
    finally:
        conn.close()


def all_groups(db_path: str = DEFAULT_DB_PATH) -> dict[str, tuple[str, tuple]]:
    """Built-in groups plus any custom ones. Built-ins always win on a clash."""
    merged = dict(custom_groups(db_path))
    merged.update(FIELD_GROUPS)
    return merged


def delete_group(name: str, db_path: str = DEFAULT_DB_PATH) -> bool:
    """Remove a custom group. Built-ins cannot be deleted."""
    if name in FIELD_GROUPS:
        raise ValueError(f"{name!r} is a built-in group and cannot be deleted")
    conn = _connect(db_path)
    try:
        cur = conn.execute("DELETE FROM fact_groups WHERE group_name=?", (name,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ── resolving a spec ──────────────────────────────────────────────────────────

def resolve_fields(
    spec: Optional[str],
    db_path: str = DEFAULT_DB_PATH,
) -> Optional[set[str]]:
    """
    Turn a ``--facts`` spec into the concept set to keep.

    Returns ``None`` for "store everything" — ``None`` and the empty set mean
    opposite things here, and conflating them would silently store no facts at
    all, so an unknown name raises rather than resolving to nothing.

        resolve_fields(None)                  -> None   (default: everything)
        resolve_fields("all")                 -> None
        resolve_fields("headquarters")        -> {10 concepts}
        resolve_fields("headquarters,shares") -> {18 concepts}
        resolve_fields("Assets,Liabilities")  -> {'Assets', 'Liabilities'}
    """
    if spec is None:
        return None
    parts = [p.strip() for p in spec.split(",") if p.strip()]
    if not parts:
        return None
    if any(p.lower() == ALL for p in parts):
        if len(parts) > 1:
            raise ValueError("'all' cannot be combined with other groups")
        return None

    groups = all_groups(db_path)
    keep: set[str] = set()
    unknown: list[str] = []
    for p in parts:
        if p.lower() in groups:
            keep.update(groups[p.lower()][1])
        elif p[:1].isupper():
            keep.add(p)                 # a bare XBRL concept name
        else:
            unknown.append(p)
    if unknown:
        raise ValueError(
            f"unknown field group(s): {', '.join(unknown)}. "
            f"Known groups: {', '.join(sorted(groups))}, or 'all'. "
            f"A raw XBRL concept name must be CamelCase (e.g. Assets).")
    return keep


def describe(spec: Optional[str], db_path: str = DEFAULT_DB_PATH) -> str:
    """One-line human summary of what a spec will store, for run logs."""
    keep = resolve_fields(spec, db_path)
    if keep is None:
        return "all facts (no filter)"
    return f"{spec}: {len(keep)} concept(s)"


# ── coverage ──────────────────────────────────────────────────────────────────

def coverage(
    spec: str,
    db_path: str = DEFAULT_DB_PATH,
    limit_forms: Optional[Iterable[str]] = None,
) -> list[dict]:
    """
    Per concept, how many already-extracted filings actually report it.

    Run this before trusting a group: a concept sitting at 9% coverage is not
    something to build an answer on without a fallback.  Only meaningful once
    some facts exist — it reads ``xbrl_facts``, not EDGAR.
    """
    keep = resolve_fields(spec, db_path)
    if keep is None:
        raise ValueError("coverage needs a specific group, not 'all'")

    conn = _connect(db_path)
    try:
        where, params = "", []
        if limit_forms:
            forms = list(limit_forms)
            where = (" JOIN filings f USING (accession_number, cik) "
                     f"WHERE f.form_type IN ({','.join('?' * len(forms))})")
            params = forms
        total = conn.execute(
            "SELECT COUNT(DISTINCT accession_number || '|' || cik) n "
            f"FROM xbrl_facts{where}", params).fetchone()["n"]

        out = []
        for concept in sorted(keep):
            n = conn.execute(
                "SELECT COUNT(DISTINCT accession_number || '|' || cik) n "
                f"FROM xbrl_facts{where}"
                f"{' AND' if where else ' WHERE'} concept = ?",
                (*params, concept)).fetchone()["n"]
            out.append({"concept": concept, "filings": n, "total": total,
                        "pct": round(100 * n / total, 1) if total else 0.0})
        return sorted(out, key=lambda r: -r["pct"])
    finally:
        conn.close()
