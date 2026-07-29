"""
resolve.py — Level 4: one trusted current value per company per field, with the
evidence that produced it.

The question this answers
-------------------------
Level 3.5 gives a clean row per company *per filing*.  Research needs the
opposite shape: for CIK X, what is the headquarters **now**, what is the state
of incorporation **now**, and how much should I trust each?

Three sources, ranked by what they actually are
-----------------------------------------------
1. **XBRL cover of the latest filing of any form** — the registrant's own
   statement, made in that filing.  Measured on this mirror, an 8-K's cover
   carries a city 99.7% of the time and an incorporation code 99.7%, and 8-Ks
   are filed constantly — so this is both the freshest and the most
   authoritative source, and it costs nothing because the facts are already in
   ``xbrl_facts``.  Note ``entity_cover`` deliberately does NOT include 8-K
   (an 8-K reports zero share classes), so this module re-reads the facts
   rather than reusing that table.
2. **SGML header** (``header_cover``) — EDGAR's *registered profile* for the
   filer.  Available on every filing including non-XBRL ones, which is its
   value; but it is maintained lazily and is frequently **staler** than the
   XBRL cover even when it comes from a later filing.  Travelers is the worked
   example: the header still said ``SAINT PAUL`` while the company's own cover
   said ``New York``.
3. Filing text — not implemented here; see the shares note below.

Hence the precedence rule: **XBRL wins wherever it exists, regardless of which
source's filing is newer.**  The header fills gaps and casts a vote; it never
overrides a statement the company made itself.

Disagreement is graded, not counted
-----------------------------------
Naive string comparison called 7 of 32 companies "changed" when almost none
had.  Real disagreements fall into four tiers:

    agreed      normalised values match
    cosmetic    differ only by case, punctuation or spacing
                ("ST GEORGE" vs "St. George", "Hicksville," vs "Hicksville")
    contained   one is a more precise form of the other
                ("Boadilla del Monte (Madrid)" vs "MADRID")
    conflict    genuinely different -> a human should look
                (InterContinental Hotels: header DE, cover X0)

Canadian issuers add a systematic case: a province code and the federal code
describe the same country at different grain (Teck ``A1`` British Columbia vs
``Z4`` Canada), so that pair is ``contained``, not a conflict.

What this does NOT do
---------------------
It does not decide a *country*.  EDGAR codes are not ISO and resolving them
properly is Level 5A's job, with the full code tables.  This module only needs
to know when two codes are compatible enough not to raise an alarm.

Shares are not resolved here
----------------------------
An 8-K cover reports no share counts at all (0% of 13,497 measured), so there
is no second XBRL opinion to reconcile: ``security_cover`` already holds the
latest per-class counts with their own ``shares_as_of``.  Non-XBRL share counts
would have to come from filing text, which is a separate extractor.

Usage
-----
    python -m finalized.cli resolve --build
    python -m finalized.cli resolve --conflicts
"""

from __future__ import annotations

import re
import unicodedata
from typing import Callable, Optional

from .database import DEFAULT_DB_PATH, FilingDB, utc_now
from .jurisdiction import Jurisdiction, Office, decode, resolve_office
from .sgml import level4_where

# DEI concepts -> the field names this module resolves.  Same aliases
# entity_cover uses, so the two tables join on meaning, not on spelling.
_FIELD_CONCEPTS = {
    "entity_name":        "EntityRegistrantName",
    "incorporation_code": "EntityIncorporationStateCountryCode",
    "address_line1":      "EntityAddressAddressLine1",
    "address_line2":      "EntityAddressAddressLine2",
    "address_city":       "EntityAddressCityOrTown",
    "address_state":      "EntityAddressStateOrProvince",
    "address_postal":     "EntityAddressPostalZipCode",
    # Sparse (21.8% of covers) but authoritative when present, and the only
    # direct statement of the office's country -- without it a foreign office
    # whose state field is blank cannot be placed at all.
    "address_country":    "EntityAddressCountry",
}
# Fields the SGML header can also supply, for cross-checking.  The header has
# no country line at all, so address_country is XBRL-only by construction.
_HEADER_COLUMNS = {
    "entity_name": "entity_name",
    "incorporation_code": "incorporation_code",
    "address_line1": "address_line1",
    "address_line2": "address_line2",
    "address_city": "address_city",
    "address_state": "address_state",
    "address_postal": "address_postal",
    "address_country": None,
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS entity_resolved (
    cik              TEXT NOT NULL,
    field            TEXT NOT NULL,   -- address_city, incorporation_code, ...
    value            TEXT,            -- the trusted current value
    as_of            TEXT,            -- filing_date of the filing it came from
    source           TEXT,            -- 'xbrl_cover' | 'sgml_header'
    source_form      TEXT,
    source_accession TEXT,
    -- agreed | cosmetic | contained | conflict | xbrl_only | header_only
    status           TEXT NOT NULL,
    alt_value        TEXT,            -- what the other source said
    alt_source       TEXT,
    alt_as_of        TEXT,
    built_utc        TEXT NOT NULL,
    PRIMARY KEY (cik, field)
);

CREATE INDEX IF NOT EXISTS ix_resolved_status ON entity_resolved(status);
CREATE INDEX IF NOT EXISTS ix_resolved_field  ON entity_resolved(field);

-- One row per company: the finalized answer to "where is it incorporated and
-- where is its principal executive office", in FULL NAMES.  entity_resolved is
-- the long, per-field evidence; this is the wide, decoded conclusion.
CREATE TABLE IF NOT EXISTS entity_final (
    cik                     TEXT PRIMARY KEY,
    entity_name             TEXT,

    -- Incorporation.  The code is kept for audit; research reads the names.
    incorporation_code      TEXT,      -- EDGAR code, NOT ISO
    incorporation_state     TEXT,      -- full name, NULL for country-level
    incorporation_country   TEXT,      -- full name
    incorporation_as_of     TEXT,
    incorporation_source    TEXT,
    incorporation_status    TEXT,      -- agreed | conflict | xbrl_only | ...
    incorporation_conf      TEXT,      -- high | medium | low
    incorporation_conf_why  TEXT,

    -- Principal executive office.
    hq_line1                TEXT,
    hq_line2                TEXT,
    hq_city                 TEXT,
    hq_state_code           TEXT,      -- EDGAR code, NOT ISO
    hq_state                TEXT,      -- full name
    hq_country              TEXT,      -- full name
    hq_postal               TEXT,
    hq_as_of                TEXT,
    hq_source               TEXT,
    hq_status               TEXT,
    hq_conf                 TEXT,
    hq_conf_why             TEXT,

    -- A second principal office is asserted on the cover PAGE but is never
    -- tagged in XBRL (Molson Coors prints Golden CO and Montreal, tags only
    -- Golden).  'candidate' is the cheap signal from disagreeing sources;
    -- 'confirmed' means covertext.py read the printed cover and found the
    -- filer stating two offices outright.
    hq_dual_candidate       INTEGER NOT NULL DEFAULT 0,
    hq_dual_confirmed       INTEGER NOT NULL DEFAULT 0,
    hq_dual_reason          TEXT,
    hq_second_office        TEXT,      -- the other address, as printed

    built_utc               TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_final_country ON entity_final(incorporation_country);
CREATE INDEX IF NOT EXISTS ix_final_hqctry  ON entity_final(hq_country);
CREATE INDEX IF NOT EXISTS ix_final_conf    ON entity_final(incorporation_conf);

-- One row per company per SHARE CLASS: the current count, when it was counted,
-- and how much to trust it.  Sourced from security_cover, which already holds
-- the latest periodic cover per company; this adds the confidence grading and
-- drops everything that is not the registrant's own equity.
CREATE TABLE IF NOT EXISTS security_final (
    cik                TEXT NOT NULL,
    security_key       TEXT NOT NULL,
    security_class     TEXT,
    trading_symbol     TEXT,
    exchange           TEXT,
    is_listed          INTEGER,        -- 0 = unlisted voting class (real, common)
    shares_outstanding REAL,
    shares_as_of       TEXT,           -- the instant counted, NOT filing_date
    source_form        TEXT,
    source_accession   TEXT,
    filing_date        TEXT,
    shares_conf        TEXT,           -- high | medium | low
    shares_conf_why    TEXT,
    built_utc          TEXT NOT NULL,
    PRIMARY KEY (cik, security_key)
);

CREATE INDEX IF NOT EXISTS ix_secfinal_cik  ON security_final(cik);
CREATE INDEX IF NOT EXISTS ix_secfinal_sym  ON security_final(trading_symbol);
CREATE INDEX IF NOT EXISTS ix_secfinal_conf ON security_final(shares_conf);
"""


# ── normalisation and comparison ──────────────────────────────────────────────

_PUNCT = re.compile(r"[.,''`\"()\[\]/\\-]+")
_WS = re.compile(r"\s+")


def normalise(value: Optional[str]) -> Optional[str]:
    """
    Casefold, strip accents and punctuation, collapse spacing.

    This is what turns "ST GEORGE" / "St. George" and "Hicksville," /
    "Hicksville" into the same string.  Accents are folded because a filer may
    write "Boadilla del Monte" one quarter and "Boadilla del Monté" the next.
    """
    if value is None:
        return None
    text = unicodedata.normalize("NFKD", str(value))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = _PUNCT.sub(" ", text)
    text = _WS.sub(" ", text).strip().casefold()
    return text or None


# Street-suffix and direction abbreviations.  Filers write the same address two
# ways in the same week -- NextEra tags "700 Universe Boulevard" on its 8-K
# cover while its registered header says "700 UNIVERSE BLVD", and Honeywell
# "855 South Mint Street" vs "855 S. MINT STREET".  Without this both land in
# the review queue as conflicts, which is exactly the noise the tiers exist to
# remove.  Applied ONLY to street lines: in a city name "st" means Saint
# ("St. George"), not Street, so expanding it there would corrupt the compare.
_STREET_TOKENS = {
    "st": "street", "str": "street", "ave": "avenue", "av": "avenue",
    "blvd": "boulevard", "blv": "boulevard", "rd": "road", "dr": "drive",
    "ln": "lane", "ct": "court", "pl": "place", "sq": "square",
    "cir": "circle", "pkwy": "parkway", "pky": "parkway", "hwy": "highway",
    "ste": "suite", "fl": "floor", "rm": "room", "bldg": "building",
    "n": "north", "s": "south", "e": "east", "w": "west",
    "ne": "northeast", "nw": "northwest", "se": "southeast",
    "sw": "southwest",
}


def _expand_street(text: Optional[str]) -> Optional[str]:
    """Expand street abbreviations in an already-normalised address line."""
    if not text:
        return text
    return " ".join(_STREET_TOKENS.get(tok, tok) for tok in text.split())


# EDGAR sub-national codes that describe the same country as a national code.
# Only Canada actually shows this duality in the data — a filer may give the
# province (Teck: A1, British Columbia) where another gives the federal code
# (Z4, Canada).  US filers always use a state code and never a national one, so
# "DE vs TX" is a real conflict, not a grain difference.
_CA_PROVINCES = {"A0", "A1", "A2", "A3", "A4", "A5", "A6",
                 "A7", "A8", "A9", "B0"}
_NATIONAL_OF = {p: "Z4" for p in _CA_PROVINCES}


def _same_jurisdiction_family(a: Optional[str], b: Optional[str]) -> bool:
    """True when two codes name the same country at different grain."""
    if not a or not b:
        return False
    x, y = a.strip().upper(), b.strip().upper()
    return _NATIONAL_OF.get(x) == y or _NATIONAL_OF.get(y) == x


def compare(field: str, xbrl: Optional[str], header: Optional[str]) -> str:
    """
    Grade the relationship between the two sources for one field.

    Returns one of: agreed, cosmetic, contained, conflict, xbrl_only,
    header_only, or 'missing' when neither source has anything.
    """
    if xbrl and not header:
        return "xbrl_only"
    if header and not xbrl:
        return "header_only"
    if not xbrl and not header:
        return "missing"

    if str(xbrl).strip() == str(header).strip():
        return "agreed"

    nx, nh = normalise(xbrl), normalise(header)
    if nx == nh:
        return "cosmetic"

    if field.startswith("address_line"):
        # "700 universe blvd" == "700 universe boulevard"
        if _expand_street(nx) == _expand_street(nh):
            return "cosmetic"

    if field in ("incorporation_code", "address_state"):
        # Codes are short; substring logic would call "DE" and "DEL" a match.
        return ("contained" if _same_jurisdiction_family(xbrl, header)
                else "conflict")

    if nx and nh and (f" {nh} " in f" {nx} " or f" {nx} " in f" {nh} "):
        # "boadilla del monte madrid" contains "madrid".
        return "contained"
    return "conflict"


# ── the two sources ───────────────────────────────────────────────────────────

def _xbrl_latest(db: FilingDB, cik: Optional[str] = None) -> dict:
    """
    Per CIK, the cover fields of the newest in-scope filing that reports them.

    Resolved per *field*, not per filing: a filing may tag a city but omit the
    postcode, and falling back to the last filing that did state one beats
    leaving the field empty.  ROW_NUMBER picks the newest filing per field.
    """
    where, params = level4_where("f")
    extra, more = "", []
    if cik:
        extra = " AND f.cik = ?"
        more.append(str(cik))
    concepts = tuple(_FIELD_CONCEPTS.values())
    qs = ",".join("?" * len(concepts))
    rows = db.conn.execute(f"""
        WITH picked AS (
            SELECT f.cik, x.concept, x.value, f.filing_date, f.form_type,
                   f.accession_number,
                   ROW_NUMBER() OVER (
                       PARTITION BY f.cik, x.concept
                       ORDER BY f.filing_date DESC, x.id DESC) rn
            FROM xbrl_facts x
            JOIN filings f
              ON f.accession_number = x.accession_number AND f.cik = x.cik
            WHERE x.concept IN ({qs})
              AND x.is_dimensioned = 0
              AND {where}{extra}
        )
        SELECT cik, concept, value, filing_date, form_type, accession_number
        FROM picked WHERE rn = 1
        """, (*concepts, *params, *more)).fetchall()

    by_concept = {v: k for k, v in _FIELD_CONCEPTS.items()}
    out: dict = {}
    for r in rows:
        out.setdefault(r["cik"], {})[by_concept[r["concept"]]] = {
            "value": r["value"], "as_of": r["filing_date"],
            "form": r["form_type"], "accession": r["accession_number"]}
    return out


def _header_latest(db: FilingDB, cik: Optional[str] = None) -> dict:
    """Per CIK, the newest stored SGML header."""
    exists = db.conn.execute(
        "SELECT COUNT(*) n FROM sqlite_master WHERE type='table' "
        "AND name='header_cover'").fetchone()["n"]
    if not exists:
        return {}
    extra, more = "", []
    if cik:
        extra = " WHERE cik = ?"
        more.append(str(cik))
    rows = db.conn.execute(f"""
        SELECT * FROM header_cover h{extra}
        GROUP BY cik HAVING filing_date = MAX(filing_date)""", more).fetchall()
    return {r["cik"]: dict(r) for r in rows}


# ── the build ─────────────────────────────────────────────────────────────────

def build_resolved(
    db_path: str = DEFAULT_DB_PATH,
    cik: Optional[str] = None,
    log: Callable[[str], None] = print,
) -> dict:
    """
    Resolve every company's current entity fields. Reads the database only.
    """
    with FilingDB(db_path) as db:
        db.conn.executescript(SCHEMA)
        db.conn.commit()

        xbrl = _xbrl_latest(db, cik)
        header = _header_latest(db, cik)
        log(f"{len(xbrl):,} companies with XBRL cover facts | "
            f"{len(header):,} with a stored header")

        now = utc_now()
        counts: dict[str, int] = {}
        n = 0
        with db._tx() as c:
            if cik:
                c.execute("DELETE FROM entity_resolved WHERE cik = ?", (str(cik),))
            else:
                c.execute("DELETE FROM entity_resolved")

            for key in sorted(set(xbrl) | set(header)):
                x_fields = xbrl.get(key, {})
                h_row = header.get(key, {})
                for field, hcol in _HEADER_COLUMNS.items():
                    x = x_fields.get(field) or {}
                    x_val = x.get("value")
                    # hcol None => the header cannot supply this field, so it
                    # is XBRL-only rather than "the header disagreed".
                    h_val = h_row.get(hcol) if hcol else None
                    status = compare(field, x_val, h_val)
                    if status == "missing":
                        continue
                    # XBRL is the registrant's own statement and always wins;
                    # the header only fills a gap.  See the module docstring.
                    use_xbrl = x_val is not None
                    c.execute(
                        """INSERT OR REPLACE INTO entity_resolved
                           (cik, field, value, as_of, source, source_form,
                            source_accession, status, alt_value, alt_source,
                            alt_as_of, built_utc)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (key, field,
                         x_val if use_xbrl else h_val,
                         (x.get("as_of") if use_xbrl
                          else h_row.get("filing_date")),
                         "xbrl_cover" if use_xbrl else "sgml_header",
                         (x.get("form") if use_xbrl else h_row.get("form_type")),
                         (x.get("accession") if use_xbrl
                          else h_row.get("accession_number")),
                         status,
                         h_val if use_xbrl else x_val,
                         "sgml_header" if use_xbrl else "xbrl_cover",
                         (h_row.get("filing_date") if use_xbrl
                          else x.get("as_of")),
                         now))
                    counts[status] = counts.get(status, 0) + 1
                    n += 1

        log(f"entity_resolved {n:,} field(s) across "
            f"{len(set(xbrl) | set(header)):,} companies")
        for s, k in sorted(counts.items(), key=lambda kv: -kv[1]):
            log(f"    {s:<12} {k:,}")
        return {"rows": n, "by_status": counts}


# ── confidence ────────────────────────────────────────────────────────────────
#
# Confidence answers "how much human attention does this value need", so it is
# derived from things that are actually observable — did two independent
# sources agree, did the code decode, how old is the statement — and never from
# a feeling about the filer.  Each verdict carries its reason so a reviewer can
# see why, and so a rule change is visible in the data rather than buried.

# A value older than this is reported as stale.  Two years: a company files a
# periodic report at least annually, so nothing current should be older, and a
# gap this size usually means the filer went quiet.
_STALE_DAYS = 730


def _age_days(as_of: Optional[str], today: Optional[str]) -> Optional[int]:
    if not as_of or not today:
        return None
    try:
        from datetime import date
        return (date.fromisoformat(today[:10])
                - date.fromisoformat(as_of[:10])).days
    except ValueError:
        return None


def grade(status: Optional[str], decoded_ok: bool, age: Optional[int],
          has_value: bool) -> tuple[str, str]:
    """
    Confidence in one finalized value, plus the reason for it.

    Ordered most-damaging first: a conflict outranks staleness, because a value
    two sources disagree about is a worse problem than an old value they agree
    on.
    """
    if not has_value:
        return "low", "no value from any source"
    if not decoded_ok:
        # A code absent from the table is the one case where the value exists
        # but cannot be trusted to mean anything.
        return "low", "code not in the EDGAR table; not decoded"
    if status == "conflict":
        return "low", "sources disagree; needs review"
    if age is not None and age > _STALE_DAYS:
        return "medium", f"stale: newest statement is {age} days old"
    if status in ("agreed", "cosmetic"):
        return "high", "two independent sources agree"
    if status == "contained":
        return "high", "sources agree at different grain"
    if status == "xbrl_only":
        return "medium", "single source: the registrant's own XBRL cover"
    if status == "header_only":
        return "low", "single source: EDGAR registered profile, no XBRL cover"
    return "medium", f"status={status}"


def conflicts(db_path: str = DEFAULT_DB_PATH, limit: int = 50) -> list[dict]:
    """Fields where the two sources genuinely disagree — the review queue."""
    with FilingDB(db_path) as db:
        exists = db.conn.execute(
            "SELECT COUNT(*) n FROM sqlite_master WHERE type='table' "
            "AND name='entity_resolved'").fetchone()["n"]
        if not exists:
            return []
        # The company name is a ROW in this long table (field='entity_name'),
        # not a column, so it is looked up as one.
        return [dict(r) for r in db.conn.execute(
            """SELECT r.*, (SELECT e.value FROM entity_resolved e
                            WHERE e.cik = r.cik AND e.field = 'entity_name')
                           AS name
               FROM entity_resolved r WHERE r.status = 'conflict'
               ORDER BY r.field, r.cik LIMIT ?""", (int(limit),))]


def build_final(
    db_path: str = DEFAULT_DB_PATH,
    today: Optional[str] = None,
    log: Callable[[str], None] = print,
) -> dict:
    """
    Collapse ``entity_resolved`` into one decoded row per company.

    Reads the long evidence table and writes the wide conclusion: incorporation
    and principal executive office as **full names**, each with its as-of date,
    source, agreement status and a graded confidence.

    ``today`` (ISO date) anchors the staleness test; defaults to the newest
    filing date in the database rather than the wall clock, so a rebuild of an
    old mirror does not mark everything stale.
    """
    with FilingDB(db_path) as db:
        db.conn.executescript(SCHEMA)
        db.conn.commit()

        if today is None:
            row = db.conn.execute(
                "SELECT MAX(filing_date) d FROM filings").fetchone()
            today = row["d"] if row and row["d"] else None

        # Confirmed dual offices, where covertext.py has read the printed page.
        cover: dict[str, dict] = {}
        if db.conn.execute(
                "SELECT COUNT(*) n FROM sqlite_master WHERE type='table' "
                "AND name='cover_office'").fetchone()["n"]:
            cover = {r["cik"]: dict(r) for r in db.conn.execute(
                "SELECT * FROM cover_office GROUP BY cik "
                "HAVING filing_date = MAX(filing_date)")}

        rows = db.conn.execute(
            """SELECT cik, field, value, as_of, source, status, alt_value
               FROM entity_resolved""").fetchall()
        by_cik: dict[str, dict] = {}
        for r in rows:
            by_cik.setdefault(r["cik"], {})[r["field"]] = dict(r)
        log(f"{len(by_cik):,} companies to finalize")

        now = utc_now()
        conf_counts: dict[str, int] = {}
        dual = 0
        with db._tx() as c:
            c.execute("DELETE FROM entity_final")
            for cik, f in by_cik.items():
                inc = f.get("incorporation_code", {})
                inc_code = inc.get("value")
                j = decode(inc_code)
                inc_conf, inc_why = grade(
                    inc.get("status"), j.ok or inc_code is None,
                    _age_days(inc.get("as_of"), today), inc_code is not None)

                city = f.get("address_city", {})
                st = f.get("address_state", {})
                st_code = st.get("value")
                # The state field is not one code system -- a US filer writes an
                # EDGAR code, a Canadian one writes 'ON'.  resolve_office decodes
                # it only inside a country it can establish, and declines rather
                # than guess; see its docstring for the three meanings of 'NL'.
                office = resolve_office(
                    st_code,
                    country_text=(f.get("address_country") or {}).get("value"),
                    incorporation_code=inc_code)
                hq_status = _worst(city.get("status"), st.get("status"))
                hq_conf, hq_why = grade(
                    hq_status, office.resolved or st_code is None,
                    _age_days(city.get("as_of"), today),
                    city.get("value") is not None)
                if not office.resolved and st_code:
                    hq_conf, hq_why = "low", office.reason
                elif not office.corroborated and hq_conf == "high":
                    # The location was inferred from a postal abbreviation with
                    # nothing confirming the country; never let that read as
                    # well-established.
                    hq_conf, hq_why = "medium", office.reason

                is_dual, dual_why = _dual_hq_signal(f, office, j)
                # A read of the printed cover outranks the cheap signal in both
                # directions: it can confirm a dual the sources hinted at, and
                # it can settle one they merely disagreed about.
                cv = cover.get(cik)
                confirmed = bool(cv and cv["is_dual"])
                second = None
                if confirmed:
                    is_dual, dual_why = True, cv["dual_reason"]
                    lines = [l for l in (cv["offices"] or "").split("\n") if l]
                    second = "\n".join(lines[1:]) or None
                elif cv:
                    is_dual, dual_why = False, (
                        f"cover page read: {cv['dual_reason']}")
                dual += bool(is_dual)

                c.execute(
                    # NAMED, not positional: a database that predates
                    # hq_dual_confirmed gets it appended at the END by the
                    # migration, while the CREATE above places it mid-table, so
                    # positional VALUES would write into the wrong columns.
                    """INSERT OR REPLACE INTO entity_final
                       (cik, entity_name,
                        incorporation_code, incorporation_state,
                        incorporation_country, incorporation_as_of,
                        incorporation_source, incorporation_status,
                        incorporation_conf, incorporation_conf_why,
                        hq_line1, hq_line2, hq_city, hq_state_code, hq_state,
                        hq_country, hq_postal, hq_as_of, hq_source, hq_status,
                        hq_conf, hq_conf_why,
                        hq_dual_candidate, hq_dual_confirmed, hq_dual_reason,
                        hq_second_office, built_utc)
                       VALUES (?,?,?,?,?,?,?,?,?,?,
                               ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (cik, (f.get("entity_name") or {}).get("value"),
                     inc_code, j.state, j.country,
                     inc.get("as_of"), inc.get("source"), inc.get("status"),
                     inc_conf, inc_why,
                     (f.get("address_line1") or {}).get("value"),
                     (f.get("address_line2") or {}).get("value"),
                     city.get("value"), st_code, office.state, office.country,
                     (f.get("address_postal") or {}).get("value"),
                     city.get("as_of"), city.get("source"), hq_status,
                     hq_conf, hq_why,
                     1 if is_dual else 0, 1 if confirmed else 0, dual_why,
                     second, now))
                key = f"inc={inc_conf}"
                conf_counts[key] = conf_counts.get(key, 0) + 1
                key = f"hq={hq_conf}"
                conf_counts[key] = conf_counts.get(key, 0) + 1

        log(f"entity_final {len(by_cik):,} row(s) | staleness anchored on {today}")
        for k, v in sorted(conf_counts.items()):
            log(f"    {k:<12} {v:,}")
        log(f"    dual-HQ candidates {dual:,}")
        return {"rows": len(by_cik), "confidence": conf_counts,
                "dual_candidates": dual}


def grade_shares(shares, as_of: Optional[str], age: Optional[int],
                 ambiguous: bool, listed: bool) -> tuple[str, str]:
    """
    Confidence in one share count.

    The count itself is never parsed from prose — it is
    ``EntityCommonStockSharesOutstanding`` read by concept name and attributed
    by XBRL dimension — so the risk is not "is this number transcribed right"
    but "does it belong to the class it is filed under, and is it current".
    """
    if shares is None:
        return "low", "no share count on the latest periodic cover"
    if ambiguous:
        # The filing carries a listing row this class could not be matched to,
        # so which class the ticker belongs to is unresolved for this company.
        return "low", "company has an unmatched listing row; class attribution unresolved"
    if as_of is None:
        return "medium", "count present but the filing tagged no measurement date"
    if age is not None and age > _STALE_DAYS:
        return "medium", f"stale: counted {age} days ago"
    if not listed:
        # An unlisted voting class is normal and real (Alphabet Class B), but
        # it has no ticker to corroborate it against.
        return "high", "counted on the latest periodic cover; unlisted class"
    return "high", "counted on the latest periodic cover"


def build_security_final(
    db_path: str = DEFAULT_DB_PATH,
    today: Optional[str] = None,
    log: Callable[[str], None] = print,
) -> dict:
    """
    Finalize share counts per company per class, with confidence.

    Only ``security_type='equity'`` rows survive: an ADS, a preferred series, a
    SPAC unit and a bond are not the registrant's share classes, and summing
    them with the common stock double-counts the company.
    """
    with FilingDB(db_path) as db:
        db.conn.executescript(SCHEMA)
        db.conn.commit()

        have = db.conn.execute(
            "SELECT COUNT(*) n FROM sqlite_master WHERE type='table' "
            "AND name='security_cover'").fetchone()["n"]
        if not have:
            log("security_cover not built - run: cli cover --build")
            return {"rows": 0}

        if today is None:
            r = db.conn.execute("SELECT MAX(filing_date) d FROM filings").fetchone()
            today = r["d"] if r and r["d"] else None

        # A company whose cover carries a listing row that could not be matched
        # to a class has unresolved attribution -- flag every class it owns.
        ambiguous = {r["cik"] for r in db.conn.execute(
            """SELECT DISTINCT s.cik FROM security_cover s
               WHERE s.security_type='equity' AND s.trading_symbol IS NOT NULL
                 AND s.shares_outstanding IS NULL
                 AND EXISTS (SELECT 1 FROM security_cover s2
                             WHERE s2.cik=s.cik
                               AND s2.accession_number=s.accession_number
                               AND s2.security_type='equity'
                               AND s2.shares_outstanding IS NOT NULL
                               AND s2.trading_symbol IS NULL)""")}

        rows = db.conn.execute(
            """SELECT * FROM security_cover WHERE security_type='equity'"""
        ).fetchall()

        now = utc_now()
        counts: dict[str, int] = {}
        with db._tx() as c:
            c.execute("DELETE FROM security_final")
            for r in rows:
                amb = r["cik"] in ambiguous
                conf, why = grade_shares(
                    r["shares_outstanding"], r["shares_as_of"],
                    _age_days(r["shares_as_of"], today), amb,
                    bool(r["is_listed"]))
                c.execute(
                    """INSERT OR REPLACE INTO security_final
                       (cik, security_key, security_class, trading_symbol,
                        exchange, is_listed, shares_outstanding, shares_as_of,
                        source_form, source_accession, filing_date,
                        shares_conf, shares_conf_why, built_utc)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (r["cik"], r["security_key"], r["security_class"],
                     r["trading_symbol"], r["exchange"], r["is_listed"],
                     r["shares_outstanding"], r["shares_as_of"],
                     r["form_type"], r["accession_number"], r["filing_date"],
                     conf, why, now))
                counts[conf] = counts.get(conf, 0) + 1

        n = sum(counts.values())
        log(f"security_final {n:,} share class(es) across "
            f"{len({r['cik'] for r in rows}):,} companies")
        for k, v in sorted(counts.items()):
            log(f"    {k:<8} {v:,}")
        if ambiguous:
            log(f"    {len(ambiguous):,} company(ies) flagged: unmatched listing row")
        return {"rows": n, "by_conf": counts, "ambiguous": len(ambiguous)}


_STATUS_RANK = {"conflict": 0, "header_only": 1, "xbrl_only": 2,
                "contained": 3, "cosmetic": 4, "agreed": 5}


def _worst(*statuses) -> Optional[str]:
    """The least-confident status among several fields of one logical value."""
    present = [s for s in statuses if s]
    if not present:
        return None
    return min(present, key=lambda s: _STATUS_RANK.get(s, 2))


def _dual_hq_signal(fields: dict, hq: "Office",
                    inc: "Jurisdiction") -> tuple[bool, Optional[str]]:
    """
    Flag companies whose principal office is plausibly split across two places.

    XBRL tags exactly one address block, so a genuine second head office is
    invisible here — Molson Coors prints Golden, Colorado *and* Montreal on its
    cover and tags only Golden.  What IS visible is the cheap signal: the two
    sources place the office in different countries, which is what a real dual
    arrangement looks like from this distance.  This marks candidates for the
    text extractor and for human review; it does not resolve them.
    """
    city = fields.get("address_city") or {}
    if city.get("status") == "conflict" and city.get("alt_value"):
        return True, "sources place the office in different cities"
    # A US-incorporated company whose office country is not the US is not dual
    # by itself, so incorporation is deliberately NOT used as evidence here.
    return False, None


def resolved_summary(db_path: str = DEFAULT_DB_PATH) -> dict:
    """Shape of the resolved table, for ``cli status``."""
    with FilingDB(db_path) as db:
        exists = db.conn.execute(
            "SELECT COUNT(*) n FROM sqlite_master WHERE type='table' "
            "AND name='entity_resolved'").fetchone()["n"]
        if not exists:
            return {"built": False}
        by_status = {r["status"]: r["n"] for r in db.conn.execute(
            "SELECT status, COUNT(*) n FROM entity_resolved GROUP BY status")}
        return {
            "built": True,
            "rows": db.conn.execute(
                "SELECT COUNT(*) n FROM entity_resolved").fetchone()["n"],
            "companies": db.conn.execute(
                "SELECT COUNT(DISTINCT cik) n FROM entity_resolved"
            ).fetchone()["n"],
            "by_status": by_status,
        }
