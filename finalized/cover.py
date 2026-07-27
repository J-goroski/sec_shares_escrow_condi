"""
cover.py — Level 3.5: turn stored XBRL facts into clean ENTITY and SECURITY
tables, one row per company-filing and one row per security line.

Why this exists
---------------
``xbrl_facts`` is correct but shapeless: a company's identity and every one of
its share classes arrive as undifferentiated rows.  Two things then go wrong
downstream:

* **Ticker is not one value.**  A cover page lists every security registered
  under Section 12(b), and most of them are bonds.  Coca-Cola's cover carries
  19 trading symbols, exactly one of which (``KO``) is its stock; Alphabet's
  carries 22 note series alongside GOOGL and GOOG.  Anything that reads "the
  TradingSymbol on this filing" picks a bond most of the time.
* **Share classes are lost.**  1,351 of 7,536 companies (18%) report more than
  one class on the cover.  Flattening to one row per filing drops the rest.

Four rules this module encodes
------------------------------
1. **Source from a periodic report, never an 8-K.**  An 8-K cover lists only
   *registered* securities, so it shows tickers but reports **zero** share-class
   lines — verified across Alphabet, Meta, Coca-Cola, Apple and Molson Coors.
   Unlisted voting classes (Alphabet Class B, Meta Class B) appear only on a
   10-K/10-Q/20-F/40-F cover, and those are exactly the lines you want.
2. **Rehydrate before parsing.**  SQLite has no boolean and no dict, so facts
   read back are ``int64`` and JSON text.  ``~df["is_dimensioned"]`` on int64 is
   *bitwise* NOT (``~0 == -1``), not logical NOT, which silently selects the
   wrong rows.  :func:`rehydrate` restores both types and must be applied to
   anything pulled out of ``xbrl_facts`` before the extractors touch it.
3. **Type the security before merging.**  A depositary receipt is not the share
   class it represents, so :func:`refine_security_type` runs first and the merge
   only ever joins ``equity`` rows.
4. **Decline when ambiguous.**  A listing row that names no class, or that could
   belong to two, is left as its own row rather than merged into the wrong one.
   76 rows out of 13,087 currently decline; every one of them is a filing that
   genuinely does not say which class its ticker belongs to.

Output
------
``entity_cover``    one row per (accession_number, cik) — identity, incorporation,
                    principal executive office address.
``security_cover``  one row per (accession_number, cik, security_key) — class,
                    type, ticker, exchange, shares.  ``is_listed`` separates
                    GOOGL from Alphabet's unlisted Class B, which carries
                    shares but no symbol.

The share-logic rule this table exists to make possible::

    SELECT * FROM security_cover WHERE security_type = 'equity'

returns exactly the registrant's share classes — listed and unlisted — and
nothing else.  Depositary receipts, preferred series, rights, SPAC units,
warrants and bonds all sit under their own type, because summing an ADS
alongside the ordinary shares it represents double-counts the company.

Requires the ``cover`` field group
----------------------------------
This reads facts that Level 3 already stored.  If the extraction ran with a
narrower ``--facts`` group the cover page will not be there; see
``profiles.COVER_REQUIRED``.

Usage
-----
    python -m finalized.cli cover --build
    python -m finalized.cli cover --build --cik 1652044      # one company
"""

from __future__ import annotations

import json
import re
from typing import Callable, Optional

import pandas as pd

from .database import DEFAULT_DB_PATH, FilingDB, utc_now
from .xbrl import entity_facts, security_facts, security_share_dates

# Covers that carry share-class lines.  8-K/6-K are excluded on purpose.
PERIODIC_FORMS = ("10-K", "10-K/A", "10-Q", "10-Q/A",
                  "20-F", "20-F/A", "40-F", "40-F/A")

# SQLite cannot store these types; they must be restored on the way out.
_BOOL_COLS = ("is_numeric", "is_nil", "is_dimensioned")

SCHEMA = """
CREATE TABLE IF NOT EXISTS entity_cover (
    accession_number   TEXT NOT NULL,
    cik                TEXT NOT NULL,
    form_type          TEXT,
    filing_date        TEXT,
    entity_name        TEXT,
    incorporation_code TEXT,          -- EDGAR code, NOT ISO (CA = California)
    address_line1      TEXT,
    address_line2      TEXT,
    address_city       TEXT,
    address_state      TEXT,          -- EDGAR code, NOT ISO
    address_postal     TEXT,
    address_country    TEXT,          -- sparse: only 21.8% of covers tag it;
                                      -- derive country from the state code
    filer_category     TEXT,
    built_utc          TEXT NOT NULL,
    PRIMARY KEY (accession_number, cik),
    FOREIGN KEY (accession_number, cik)
        REFERENCES filings(accession_number, cik)
);

CREATE TABLE IF NOT EXISTS security_cover (
    accession_number   TEXT NOT NULL,
    cik                TEXT NOT NULL,
    security_key       TEXT NOT NULL,  -- axis member, or '(single)' when untagged
    -- equity   = a share class of the registrant (what share counts belong to)
    -- adr      = a depositary receipt over those shares; never sum with equity
    -- unit     = a SPAC bundle whose share is counted on its own class line
    -- preferred, right, warrant, debt, other = not common equity
    security_type      TEXT,
    security_class     TEXT,           -- CommonClassA, CapitalClassC, ...
    title              TEXT,           -- the 12(b) title as printed
    trading_symbol     TEXT,
    exchange           TEXT,
    shares_outstanding REAL,
    -- The instant the count was measured, NOT the filing date.  A 20-F's count
    -- is ~104 days old at filing, so comparing counts across filers by
    -- filing_date compares figures a quarter apart.  Level 4 resolves "latest
    -- shares" on THIS column.
    shares_as_of       TEXT,
    is_listed          INTEGER,        -- 0 = unlisted voting class (no symbol)
    form_type          TEXT,
    filing_date        TEXT,
    built_utc          TEXT NOT NULL,
    PRIMARY KEY (accession_number, cik, security_key),
    FOREIGN KEY (accession_number, cik)
        REFERENCES filings(accession_number, cik)
);

CREATE INDEX IF NOT EXISTS ix_seccover_cik  ON security_cover(cik);
CREATE INDEX IF NOT EXISTS ix_seccover_sym  ON security_cover(trading_symbol);
CREATE INDEX IF NOT EXISTS ix_seccover_type ON security_cover(security_type);
CREATE INDEX IF NOT EXISTS ix_entcover_cik  ON entity_cover(cik);
"""


def rehydrate(df: pd.DataFrame) -> pd.DataFrame:
    """
    Restore the dtypes SQLite cannot hold.

    Without this the extractors misread the frame — see the module docstring.
    Applied to every frame pulled from ``xbrl_facts``.
    """
    out = df.copy()
    for col in _BOOL_COLS:
        if col in out.columns:
            out[col] = out[col].fillna(0).astype(bool)
    if "dimensions" in out.columns:
        out["dimensions"] = out["dimensions"].apply(
            lambda s: json.loads(s) if isinstance(s, str) and s.strip() else {})
    return out


_FACT_SELECT = """
    SELECT f.cik, f.entity_name, f.ticker, f.form_type, f.filing_date,
           f.report_date, f.accession_number,
           x.concept, x.namespace, x.value, x.value_num, x.is_numeric, x.is_nil,
           x.unit, x.decimals, x.period_type, x.period_start, x.period_end,
           x.period_instant, x.is_dimensioned, x.segment, x.dimensions,
           x.context_id
    FROM xbrl_facts x
    JOIN filings f
      ON f.accession_number = x.accession_number AND f.cik = x.cik
    WHERE x.accession_number = ? AND x.cik = ?
"""


def latest_cover_filings(db: FilingDB, cik: Optional[str] = None) -> list[dict]:
    """
    The newest periodic filing per CIK that actually has facts.

    One row per company: the cover we trust for entity and security structure.
    """
    forms = ",".join("?" * len(PERIODIC_FORMS))
    params: list = list(PERIODIC_FORMS)
    extra = ""
    if cik:
        extra = " AND x.cik = ?"
        params.append(str(cik))
    rows = db.conn.execute(f"""
        SELECT x.cik, x.accession_number, f.form_type, f.filing_date
        FROM xbrl_facts x
        JOIN filings f
          ON f.accession_number = x.accession_number AND f.cik = x.cik
        WHERE f.form_type IN ({forms}){extra}
        GROUP BY x.cik, x.accession_number
        HAVING f.filing_date = MAX(f.filing_date)
        """, params).fetchall()
    # One per CIK: keep the newest filing_date, break ties on accession.
    best: dict[str, dict] = {}
    for r in rows:
        cur = best.get(r["cik"])
        if cur is None or (r["filing_date"], r["accession_number"]) > \
                          (cur["filing_date"], cur["accession_number"]):
            best[r["cik"]] = dict(r)
    return list(best.values())


def _first(row, *names):
    for n in names:
        if n in row and pd.notna(row[n]):
            return row[n]
    return None


def _shares_as_of(as_of: dict, member, shares):
    """
    The instant belonging to this row's share count.

    ``security_facts`` attaches the company-wide *un-dimensioned* total to the
    sole common-stock line when only one exists, so that row carries a
    dimensioned member (``ifrs-full:OrdinarySharesMember``) while its number
    came from the un-dimensioned fact — which is why a plain member lookup
    misses and the ``None`` key is the fallback.  Southern Co is the clean
    example: the company total is un-dimensioned while every dimensioned member
    is a subsidiary on ``dei:LegalEntityAxis``.

    A row with no count gets no date; a date without a number is noise.
    """
    if shares is None or pd.isna(shares):
        return None
    return as_of.get(member, as_of.get(None))


def _clean(value):
    """Trim whitespace and a trailing comma left by cover-page line breaks.

    269 filings render the city as "Atlanta," because the cover prints
    "City, State" and the tag captures the comma.  Left in, it breaks any
    equality join on city.
    """
    if value is None or not isinstance(value, str):
        return value
    return value.strip().rstrip(",").strip() or None


# ── security typing ───────────────────────────────────────────────────────────
#
# The shared classifier in xbrl.py returns a broad "equity" for anything
# mentioning stock or shares.  Share logic needs finer types, because these are
# not the same thing and must never be summed together:
#
#   equity     an actual share class of the registrant (common / ordinary /
#              capital stock, LP common units).  This is what a share count
#              belongs to and what market cap is built from.
#   adr        a depositary receipt REPRESENTING those shares at some ratio.
#              SMFG, ING and Toyota list an ADS while the ordinary shares sit
#              on a separate line; counting both double-counts the company.
#   preferred  preferred stock and preferred series - senior, separately
#              listed, and not part of common equity.
#   right      a poison-pill "Preferred Stock Purchase Right", or a SPAC right
#              convertible into a fraction of a share.
#   unit       a SPAC unit — one share bundled with a right or warrant.  The
#              share inside it is already counted on its own class line, so a
#              unit counted as equity counts that share twice.
#
# Applied here rather than in the shared extractor, which other callers use.
_RIGHT_TITLE = re.compile(
    r"\brights?\s+to\s+purchase\b|\bpurchase\s+rights?\b"
    r"|^\s*rights?\b|\bright\s+entitling\b", re.I)
# Deliberately narrow.  An operating partnership's "Common Units" and "Units
# representing limited partner interests" ARE the registrant's equity, so this
# matches only the SPAC bundle phrasing and leaves 141 real LP unit lines alone.
_UNIT_TITLE = re.compile(r"\bunits?,\s+each\s+(?:consisting|comprising)\b", re.I)
# "American Depositary Shares" is title-cased on real cover pages, so the word
# half must be case-insensitive.  The acronyms stay case-sensitive via a scoped
# flag, so a lowercase "ads" in prose cannot be read as an ADS.
_ADR_TITLE = re.compile(
    r"(?i:\b(?:american\s+)?deposit(?:a|o)ry\b)|\bADRs?\b|\bADSs?\b")
_PREFERRED_TITLE = re.compile(r"\bpreferred\b|\bpreference\s+shares?\b", re.I)

# Axis-member labels are run-together CamelCase, so a \b-anchored pattern can
# never fire inside one: "SeriesBMandatoryConvertiblePreferredStock" has no word
# boundary before "Preferred".  Splitting the label back into words lets the
# same patterns read a member label and a printed title alike -- which matters
# because a preferred series often carries a share count but no 12(b) title.
_CAMEL_SPLIT = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _searchable(sec: pd.DataFrame) -> pd.Series:
    """Title + de-CamelCased member label, for classification by keyword."""
    title = (sec["security_title"].fillna("").astype(str)
             if "security_title" in sec.columns else "")
    label = (sec["security_class"].fillna("").astype(str)
             .map(lambda s: _CAMEL_SPLIT.sub(" ", s))
             if "security_class" in sec.columns else "")
    return title + " " + label


def refine_security_type(sec: pd.DataFrame) -> pd.DataFrame:
    """Split the broad 'equity' bucket into equity / adr / preferred / right / unit."""
    if sec.empty or "security_type" not in sec.columns:
        return sec
    out = sec.copy()
    text = _searchable(out)

    # Order matters.  A "Preferred Stock Purchase Right" is a right, not a
    # preferred; a SPAC unit names the share it contains, so it must be settled
    # before that share name is read; and an ADS over preference shares is a
    # depositary receipt, not a preferred.
    for pattern, label in ((_RIGHT_TITLE, "right"),
                           (_UNIT_TITLE, "unit"),
                           (_ADR_TITLE, "adr"),
                           (_PREFERRED_TITLE, "preferred")):
        equity = out["security_type"].eq("equity")
        out.loc[equity & text.str.contains(pattern), "security_type"] = label
    return out


# ── merging a split share class ───────────────────────────────────────────────

# The class as printed in a 12(b) title: "Class A", "Class B-1", "Class ER-D".
# {1,2} per segment is what stops "Class Common Stock" reading as class "co" —
# no word boundary follows a truncated word.
_CLASS_IN_TITLE = re.compile(r"\bclass\s+([a-z0-9]{1,2}(?:-[a-z0-9]{1,2})*)\b",
                             re.I)

# The class token inside an axis-member label.  Filers are inconsistent about
# the prefix and the suffix -- CommonClassA, ClassAOrdinaryShares,
# ClassASubordinateVotingShares, CommonClassANonVoting all mean class A -- so
# the token is extracted rather than the whole label string-matched.
#
# The token's end is found by CamelCase, not by a non-alphanumeric character:
# these labels are one run-together word, so in ClassASubordinateVotingShares
# the "A" is followed immediately by more letters and any `(?![a-z0-9])` guard
# can never match.  A capital starts the *next word* only when a lowercase
# letter follows it, which is what separates the two readings:
#
#   ClassAIssuedCapital -> A     ("I" begins "Issued")
#   CommonClassAI       -> AI    (nothing follows, so both letters are the class)
#   CommonClassER-D     -> ER-D  (hyphenated classes are distinct securities and
#                                 must not be truncated to a shared "ER", which
#                                 would make three classes collide)
_CLASS_IN_LABEL = re.compile(
    r"^(?:Common|Capital)?Class"
    r"(?:([A-Z][a-z])$"                          # ClassFa - ends in lowercase
    r"|([A-Z][A-Z0-9-]*?)(?=[A-Z][a-z]|$))")     # run, stopping before a word


def _class_letter(label) -> Optional[str]:
    """'ClassASubordinateVotingShares' -> 'a'; None when no class letter."""
    m = _CLASS_IN_LABEL.match(str(label or ""))
    if not m:
        return None
    return (m.group(1) or m.group(2)).lower()


def merge_listing_rows(sec: pd.DataFrame) -> pd.DataFrame:
    """
    Fold a listing-only line into the share class it names.

    A cover page can split one share class across two facts: the 12(b) listing
    (title, ticker, exchange) and the share count.  When the filer tags them
    differently the class arrives as two rows — one with a ticker and no shares,
    one with shares and no ticker — so a two-class company looks like three
    classes and the row holding the shares looks unlisted.

    Filers split them in two ways, and both are handled here:

    * **Undimensioned listing** (Meta).  ``CommonClassA``/``CommonClassB`` carry
      the counts; the META listing carries no class dimension at all.
    * **Separately dimensioned listing** (Ares).  The listing sits on its own
      member ``ClassACommonStockParValue0.01`` while the count sits on
      ``CommonClassA`` — *both* dimensioned, which is why this cannot key on
      "undimensioned".

    The donor's *title* states the class ("Class A common stock, par value
    $0.01 per share"), so the match is read off the filing rather than assumed
    from a convention like "the listed one is always Class A"; the donor's own
    member label is the fallback.  A donor that names no class, or that matches
    more than one candidate, is left alone rather than merged into the wrong
    line.

    Only ``equity`` rows take part.  :func:`refine_security_type` must run first
    so that a depositary receipt — "American Depositary Shares, each
    representing eight Class A ordinary shares" — is already typed ``adr`` and
    cannot be folded into the ordinary shares it merely represents.
    """
    if sec.empty or "security_class" not in sec.columns:
        return sec

    out = sec.copy()
    for col in ("trading_symbol", "shares_outstanding", "security_title"):
        if col not in out.columns:
            out[col] = None
    letters = out["security_class"].astype(str).map(_class_letter)
    is_equity = out.get("security_type", pd.Series(index=out.index)).eq("equity")

    has_symbol = out["trading_symbol"].notna()
    has_shares = out["shares_outstanding"].notna()
    # A donor states a listing but no count; a recipient holds a count but no
    # listing of its own.  A row with both is already complete and is neither.
    donors = out.index[is_equity & has_symbol & ~has_shares]
    recipients = is_equity & has_shares & ~has_symbol
    if not len(donors) or not recipients.any():
        return out

    used, drop = set(), []
    for idx in donors:
        title = str(out.at[idx, "security_title"] or "")
        want = (m.group(1).lower() if (m := _CLASS_IN_TITLE.search(title))
                else letters.get(idx))
        free = recipients & ~out.index.isin(used | {idx})
        target = pd.Index([])
        if want:
            target = out.index[free & letters.eq(want)]
        if not want or len(target) == 0:
            # A plain "Common Stock" title belongs to the base line — the one
            # class carrying no class letter at all.  Coca-Cola Consolidated
            # tags it coke:CommonClassUndefinedMember alongside a real
            # CommonClassB, so the base line is identifiable without assuming
            # which class happens to be listed.
            target = out.index[free & letters.isna()]
        if len(target) != 1:                 # ambiguous: leave both rows
            continue
        tgt = target[0]
        for col in ("trading_symbol", "exchange", "security_title"):
            if col in out.columns and pd.isna(out.at[tgt, col]):
                out.at[tgt, col] = out.at[idx, col]
        used.add(tgt)
        drop.append(idx)
    return out.drop(index=drop) if drop else out


# ── the build ─────────────────────────────────────────────────────────────────

def build_cover_tables(
    db_path: str = DEFAULT_DB_PATH,
    cik: Optional[str] = None,
    limit: Optional[int] = None,
    log: Callable[[str], None] = print,
) -> dict:
    """
    Rebuild ``entity_cover`` / ``security_cover`` from the stored facts.

    Reads only the database — no network — so it is cheap to re-run whenever
    the transform changes, and safe to interrupt.
    """
    with FilingDB(db_path) as db:
        db.conn.executescript(SCHEMA)
        db.conn.commit()

        targets = latest_cover_filings(db, cik=cik)
        if limit:
            targets = targets[:limit]
        log(f"{len(targets):,} company cover(s) to build")

        now = utc_now()
        n_ent = n_sec = n_skip = 0
        for i, t in enumerate(targets, 1):
            df = pd.read_sql_query(_FACT_SELECT, db.conn,
                                   params=(t["accession_number"], t["cik"]))
            if df.empty:
                n_skip += 1
                continue
            df = rehydrate(df)
            try:
                ent = entity_facts(df)
                # Refine first: the merge only joins 'equity' rows, so a
                # depositary receipt must already be typed 'adr' before it can
                # be mistaken for the ordinary shares it represents.
                sec = merge_listing_rows(refine_security_type(security_facts(df)))
                # Read off the raw facts, not the collapsed frame: the instant
                # is a property of the fact, and _pivot_cover keeps only values.
                as_of = security_share_dates(df)
            except Exception as exc:                      # noqa: BLE001
                log(f"  {t['cik']}: {type(exc).__name__}: {exc}")
                n_skip += 1
                continue

            with db._tx() as c:
                if len(ent):
                    r = ent.iloc[0]
                    c.execute(
                        # Every column is refreshed on conflict.  Updating only
                        # a subset would leave a rebuild half-applied — an
                        # address-cleaning fix, say, would land on city but not
                        # on state or postcode.
                        """INSERT INTO entity_cover
                           (accession_number, cik, form_type, filing_date,
                            entity_name, incorporation_code, address_line1,
                            address_line2, address_city, address_state,
                            address_postal, address_country, filer_category,
                            built_utc)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(accession_number, cik) DO UPDATE SET
                             form_type=excluded.form_type,
                             filing_date=excluded.filing_date,
                             entity_name=excluded.entity_name,
                             incorporation_code=excluded.incorporation_code,
                             address_line1=excluded.address_line1,
                             address_line2=excluded.address_line2,
                             address_city=excluded.address_city,
                             address_state=excluded.address_state,
                             address_postal=excluded.address_postal,
                             address_country=excluded.address_country,
                             filer_category=excluded.filer_category,
                             built_utc=excluded.built_utc""",
                        (t["accession_number"], t["cik"], t["form_type"],
                         t["filing_date"],
                         # Column names come from _ENTITY_RENAME in xbrl.py;
                         # concepts with no friendly alias (EntityAddressCountry)
                         # survive under their raw XBRL tag, so both are checked.
                         _first(r, "registrant_name", "entity_name"),
                         _first(r, "incorporation_state",
                                "EntityIncorporationStateCountryCode"),
                         _clean(_first(r, "address_line1",
                                       "EntityAddressAddressLine1")),
                         _clean(_first(r, "address_line2",
                                       "EntityAddressAddressLine2")),
                         _clean(_first(r, "address_city",
                                       "EntityAddressCityOrTown")),
                         _clean(_first(r, "address_state",
                                       "EntityAddressStateOrProvince")),
                         _clean(_first(r, "address_zip",
                                       "EntityAddressPostalZipCode")),
                         _clean(_first(r, "address_country",
                                       "EntityAddressCountry")),
                         _first(r, "filer_category", "EntityFilerCategory"),
                         now))
                    n_ent += 1

                c.execute("DELETE FROM security_cover WHERE accession_number=? "
                          "AND cik=?", (t["accession_number"], t["cik"]))
                for j, (_, s) in enumerate(sec.iterrows()):
                    sym = _first(s, "trading_symbol")
                    member = _first(s, "security_member")
                    key = str(member or _first(s, "security_class")
                              or f"(single){j}")
                    shares = _first(s, "shares_outstanding")
                    c.execute(
                        # Columns are NAMED, not positional.  A migrated table
                        # gets shares_as_of appended at the end while the
                        # CREATE above places it mid-table, so positional
                        # VALUES would write into the wrong columns on any
                        # database that predates the column.
                        """INSERT OR REPLACE INTO security_cover
                           (accession_number, cik, security_key, security_type,
                            security_class, title, trading_symbol, exchange,
                            shares_outstanding, shares_as_of, is_listed,
                            form_type, filing_date, built_utc)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (t["accession_number"], t["cik"], key,
                         _first(s, "security_type"),
                         _first(s, "security_class"),
                         _first(s, "security_title"),
                         sym, _first(s, "exchange"),
                         float(shares) if shares is not None
                         and pd.notna(shares) else None,
                         _shares_as_of(as_of, member, shares),
                         1 if sym else 0,
                         t["form_type"], t["filing_date"], now))
                    n_sec += 1

            if i % 500 == 0:
                log(f"  {i:,}/{len(targets):,} ...")

        log(f"entity_cover {n_ent:,} | security_cover {n_sec:,} | skipped {n_skip:,}")
        return {"entities": n_ent, "securities": n_sec, "skipped": n_skip}


def cover_summary(db_path: str = DEFAULT_DB_PATH) -> dict:
    """Shape of the built tables, for ``cli status`` and for sanity checks."""
    with FilingDB(db_path) as db:
        exists = db.conn.execute(
            "SELECT COUNT(*) n FROM sqlite_master WHERE type='table' "
            "AND name IN ('entity_cover','security_cover')").fetchone()["n"]
        if exists < 2:
            return {"built": False}
        by_type = {r["security_type"]: r["n"] for r in db.conn.execute(
            "SELECT security_type, COUNT(*) n FROM security_cover "
            "GROUP BY security_type")}
        multi = db.conn.execute(
            """SELECT COUNT(*) n FROM (SELECT cik FROM security_cover
               WHERE security_type='equity' GROUP BY cik HAVING COUNT(*) > 1)"""
        ).fetchone()["n"]
        return {
            "built": True,
            "entities": db.conn.execute(
                "SELECT COUNT(*) n FROM entity_cover").fetchone()["n"],
            "securities": db.conn.execute(
                "SELECT COUNT(*) n FROM security_cover").fetchone()["n"],
            "by_type": by_type,
            "multi_class_companies": multi,
        }
