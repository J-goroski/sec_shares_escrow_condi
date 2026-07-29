"""
sgml.py — Level 4 source A: read headquarters and state of incorporation out of
a filing's SGML ``<SEC-HEADER>``.

Why this exists
---------------
Level 3.5 can only describe a company that filed inline XBRL on a periodic
report.  Two gaps follow:

* **Companies XBRL never covers.**  A filer whose only recent filings are 8-K,
  6-K or a registration statement has no cover-page facts at all.
* **Staleness.**  ``entity_cover`` reflects the last 10-K/10-Q/20-F/40-F.  If a
  company moved its head office and said so on a later 8-K, Level 3.5 will not
  notice until the next periodic report — up to a year later.

Every filing, XBRL or not, carries an SGML header stating the filer's address
and state of incorporation.  That makes it a second opinion available on the
*latest* filing of any type, which is what change detection needs.

What the header does and does not give
--------------------------------------
It gives **name, state of incorporation, business address, fiscal year end**.
It does **not** give share counts — those are never in the header, so a
non-XBRL share count has to come from the document text (a separate, and much
less certain, extractor).

``STATE OF INCORPORATION`` is optional and genuinely absent on many filings —
verified: a 424B3 and an 8-K carry it, a Shift4 S-3ASR does not.  Absence is
normal and must not be read as "no longer incorporated anywhere".

The trap: one submission, many filers
-------------------------------------
A submission can contain **several ``FILER:`` blocks**, one per associated CIK,
and they are neither ordered to match the index nor identical.  Frontier Funds
files one 8-K under 8 CIKs; seven blocks say ``CITY: GOLDEN`` and the eighth
says ``DENVER``.  Reading "the first FILER block" would stamp one company's
address onto seven others.

So :func:`parse_header` **matches on CENTRAL INDEX KEY** — the same reason the
``filings`` primary key is ``(accession_number, cik)`` and not accession alone.

Trust
-----
This is EDGAR's *registered profile* for the filer, not what the company
printed on its cover page.  The two disagree, and the disagreement is signal:
Alibaba's registered state of incorporation is ``K3`` (Hong Kong) while its
filings say Cayman Islands.  So the header is a cross-check and a gap-filler,
never an override of an XBRL cover fact.

Usage
-----
    python -m finalized.cli headers --run --limit 200
    python -m finalized.cli headers --summary

...or from Python::

    from finalized.sgml import parse_header
    facts = parse_header(header_text, cik="1389124")
    facts.address_city, facts.incorporation_code
"""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from typing import Callable, Optional

import requests

from .core import UA, DEFAULT_DELAY, SECBlockedError
from .daily_index import _fetch_header_chunk
from .database import DEFAULT_DB_PATH, FilingDB, utc_now

# ── form scope ────────────────────────────────────────────────────────────────
#
# The "main" filings: annual, quarterly, event-driven and the primary
# registration statements.  Prospectuses (424*), S-8 employee plans and POS
# amendments are excluded on purpose — they are filed constantly, carry the
# same registered address as the parent registration, and would dominate any
# "latest filing" query without ever signalling a real change.
LEVEL4_BASE_FORMS = (
    "10-K", "10-Q",            # domestic periodic
    "20-F", "40-F", "6-K",     # foreign private issuers
    "8-K",                     # event driven
    "S-1", "S-3", "F-1", "F-3",  # primary registration statements
)

# Automatic-shelf variants belong to the S-3 / F-3 families.
_EXTRA_FORMS = ("S-3ASR", "F-3ASR")


def level4_where(alias: str = "") -> tuple[str, tuple]:
    """
    SQL predicate selecting the Level 4 form scope, plus its parameters.

    Matches each base form exactly, its ``/A`` amendments, and the ASR shelf
    variants.  ``LIKE 'X/%'`` rather than ``LIKE 'X%'`` matters: the loose form
    would drag ``10-K`` into ``10-KSB`` and ``S-1`` into ``S-11``, which are
    different forms with different meanings.
    """
    p = f"{alias}." if alias else ""
    forms = list(LEVEL4_BASE_FORMS) + list(_EXTRA_FORMS)
    exact = ",".join("?" * len(forms))
    amend = " OR ".join(f"{p}form_type LIKE ?" for _ in LEVEL4_BASE_FORMS)
    sql = f"({p}form_type IN ({exact}) OR {amend})"
    return sql, tuple(forms) + tuple(f"{f}/%" for f in LEVEL4_BASE_FORMS)


# ── the parser ────────────────────────────────────────────────────────────────

@dataclass
class HeaderFacts:
    """What one FILER block states. Every field is optional but ``cik``."""
    cik: str
    entity_name: Optional[str] = None
    incorporation_code: Optional[str] = None      # EDGAR code, NOT ISO
    fiscal_year_end: Optional[str] = None
    irs_number: Optional[str] = None
    sic: Optional[str] = None
    address_line1: Optional[str] = None
    address_line2: Optional[str] = None
    address_city: Optional[str] = None
    address_state: Optional[str] = None           # EDGAR code, NOT ISO
    address_postal: Optional[str] = None
    address_phone: Optional[str] = None
    address_kind: Optional[str] = None            # 'business' | 'mail'

    def as_dict(self) -> dict:
        return asdict(self)


# "LABEL:<tabs/spaces>VALUE" — the whole header is this one shape.
_FIELD = re.compile(r"^\s*([A-Z][A-Z0-9 /\-&.']*?):\s*(.*?)\s*$")

# Lines that open a new filer role.  FILED BY is a different party (the one
# submitting on someone's behalf) and must not be mistaken for the filer.
_ROLE = {"FILER", "FILED BY", "SUBJECT COMPANY", "REPORTING-OWNER", "ISSUER"}
# Sub-sections inside a filer block.  Both address blocks carry STREET 1 /
# CITY / STATE / ZIP, so the fields have to be scoped to the section they are
# under or the mail address overwrites the business one.
_SECTIONS = {"COMPANY DATA", "FILING VALUES", "BUSINESS ADDRESS",
             "MAIL ADDRESS", "FORMER COMPANY", "FORMER NAME",
             "FORMER CONFORMED NAME"}

_COMPANY_FIELDS = {
    "COMPANY CONFORMED NAME": "entity_name",
    "STATE OF INCORPORATION": "incorporation_code",
    "FISCAL YEAR END": "fiscal_year_end",
    "IRS NUMBER": "irs_number",
    "STANDARD INDUSTRIAL CLASSIFICATION": "sic",
}
_ADDRESS_FIELDS = {
    "STREET 1": "address_line1",
    "STREET 2": "address_line2",
    "CITY": "address_city",
    "STATE": "address_state",
    "ZIP": "address_postal",
    "BUSINESS PHONE": "address_phone",
}


def _norm_cik(value) -> Optional[str]:
    """'0001389124' and '1389124' are the same CIK; compare unpadded."""
    if value is None:
        return None
    digits = re.sub(r"\D", "", str(value))
    return str(int(digits)) if digits else None


def _split_filer_blocks(head: str) -> list[tuple[str, list[str]]]:
    """Cut the header into (role, lines) blocks, one per FILER / FILED BY."""
    blocks: list[tuple[str, list[str]]] = []
    current: Optional[tuple[str, list[str]]] = None
    for line in head.splitlines():
        m = _FIELD.match(line)
        label = m.group(1).strip() if m else None
        # A role line is a bare "FILER:" with no value on the same line.
        if label in _ROLE and m and not m.group(2):
            current = (label, [])
            blocks.append(current)
            continue
        if current is not None:
            current[1].append(line)
    return blocks


def _parse_block(lines: list[str]) -> dict:
    """Fields of one filer block, with address fields scoped to their section."""
    out: dict = {}
    section: Optional[str] = None
    seen_business = False
    for line in lines:
        m = _FIELD.match(line)
        if not m:
            continue
        label, value = m.group(1).strip(), m.group(2).strip()
        if label in _SECTIONS and not value:
            section = label
            continue
        if not value:
            continue

        if label == "CENTRAL INDEX KEY":
            out["cik"] = _norm_cik(value)
        elif label in _COMPANY_FIELDS and section in (None, "COMPANY DATA"):
            # FORMER COMPANY blocks also carry a name; only take the current one.
            out.setdefault(_COMPANY_FIELDS[label], value)
        elif label in _ADDRESS_FIELDS and section in ("BUSINESS ADDRESS",
                                                      "MAIL ADDRESS"):
            business = section == "BUSINESS ADDRESS"
            # Business address is the head office and wins.  Mail address is
            # taken only when there is no business block at all (some filers
            # register only a mailing address).
            if business and not seen_business:
                seen_business = True
                for f in _ADDRESS_FIELDS.values():
                    out.pop(f, None)          # drop any mail values already set
                out["address_kind"] = "business"
            if business or not seen_business:
                out[_ADDRESS_FIELDS[label]] = value
                out.setdefault("address_kind", "mail")
    return out


def parse_header(text: str, cik: str) -> Optional[HeaderFacts]:
    """
    The header facts for ONE cik, from a submission's ``<SEC-HEADER>``.

    ``cik`` is required and is matched against each block's CENTRAL INDEX KEY,
    because a submission can carry many filers whose addresses differ — see the
    module docstring.  Returns ``None`` when the header holds no block for that
    CIK, which is the honest answer: guessing from another filer's block is how
    one company's address ends up on another's record.
    """
    if not text:
        return None
    head = text.split("</SEC-HEADER>")[0]
    want = _norm_cik(cik)

    fallback: Optional[dict] = None
    for role, lines in _split_filer_blocks(head):
        parsed = _parse_block(lines)
        if not parsed.get("cik"):
            continue
        if parsed["cik"] == want:
            if role == "FILER":
                return _to_facts(parsed)
            fallback = fallback or parsed        # e.g. SUBJECT COMPANY
    return _to_facts(fallback) if fallback else None


def _to_facts(parsed: dict) -> HeaderFacts:
    known = {f for f in HeaderFacts.__dataclass_fields__}
    return HeaderFacts(**{k: v for k, v in parsed.items() if k in known})


# ── storage ───────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS header_cover (
    accession_number   TEXT NOT NULL,
    cik                TEXT NOT NULL,
    form_type          TEXT,
    filing_date        TEXT,
    entity_name        TEXT,
    incorporation_code TEXT,          -- EDGAR code, NOT ISO; often absent
    address_line1      TEXT,
    address_line2      TEXT,
    address_city       TEXT,
    address_state      TEXT,          -- EDGAR code, NOT ISO
    address_postal     TEXT,
    address_phone      TEXT,
    address_kind       TEXT,          -- 'business' (head office) or 'mail'
    fiscal_year_end    TEXT,
    sic                TEXT,
    built_utc          TEXT NOT NULL,
    PRIMARY KEY (accession_number, cik),
    FOREIGN KEY (accession_number, cik)
        REFERENCES filings(accession_number, cik)
);

CREATE INDEX IF NOT EXISTS ix_hdrcover_cik ON header_cover(cik);
"""

_INSERT = """
INSERT OR REPLACE INTO header_cover
  (accession_number, cik, form_type, filing_date, entity_name,
   incorporation_code, address_line1, address_line2, address_city,
   address_state, address_postal, address_phone, address_kind,
   fiscal_year_end, sic, built_utc)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""


def latest_level4_filings(
    db: FilingDB,
    cik: Optional[str] = None,
    only_missing: bool = True,
    limit: Optional[int] = None,
) -> list[dict]:
    """
    The newest in-scope filing per CIK — the one that could carry a change.

    Level 4 asks "has anything moved since the last periodic report?", so only
    the latest filing per company is worth reading; older ones describe a past
    already captured.  ``only_missing`` skips filings whose header is stored.
    """
    # The predicate is built twice: the inner "latest per cik" subquery has no
    # table alias, the outer select does.  Reusing the aliased form inside the
    # subquery fails with "no such column: f.form_type".
    outer, o_params = level4_where("f")
    inner, i_params = level4_where("")
    extra, more = "", []
    if cik:
        extra += " AND f.cik = ?"
        more.append(str(cik))
    if only_missing:
        extra += (" AND NOT EXISTS (SELECT 1 FROM header_cover h "
                  "WHERE h.accession_number = f.accession_number "
                  "AND h.cik = f.cik)")
    rows = db.conn.execute(f"""
        SELECT f.accession_number, f.cik, f.form_type, f.filing_date,
               f.submission_txt_url
        FROM filings f
        JOIN (SELECT cik, MAX(filing_date) md FROM filings
               WHERE {inner} GROUP BY cik) latest
          ON latest.cik = f.cik AND latest.md = f.filing_date
        WHERE {outer} AND f.submission_txt_url IS NOT NULL{extra}
        GROUP BY f.cik
        ORDER BY f.filing_date DESC
        {f'LIMIT {int(limit)}' if limit else ''}
        """, (*i_params, *o_params, *more)).fetchall()
    return [dict(r) for r in rows]


def extract_headers(
    db_path: str = DEFAULT_DB_PATH,
    cik: Optional[str] = None,
    limit: Optional[int] = None,
    delay: float = DEFAULT_DELAY,
    only_missing: bool = True,
    log: Callable[[str], None] = print,
) -> dict:
    """
    Read the SGML header of each company's latest in-scope filing.

    One small range request per filing — the same read enrichment already
    performs, so a filing enriched earlier costs one cheap repeat rather than a
    full document fetch.  Failures are isolated per filing; a persistent 429
    aborts cleanly with everything unwritten still selectable next run.
    """
    session = requests.Session()
    session.headers.update(UA)

    done = missed = failed = 0
    with FilingDB(db_path) as db:
        db.conn.executescript(SCHEMA)
        db.conn.commit()

        targets = latest_level4_filings(db, cik=cik, only_missing=only_missing,
                                        limit=limit)
        log(f"{len(targets):,} filing header(s) to read")
        now = utc_now()
        try:
            for i, t in enumerate(targets, 1):
                try:
                    text = _fetch_header_chunk(t["submission_txt_url"], session,
                                               delay, 12000)
                    facts = parse_header(text or "", t["cik"])
                    if facts is None:
                        # No block for this CIK in the window we read.  Escalate
                        # once: a many-filer submission can push the CIK's block
                        # past the first range.
                        text = _fetch_header_chunk(t["submission_txt_url"],
                                                   session, delay, 96000)
                        facts = parse_header(text or "", t["cik"])
                    if facts is None:
                        missed += 1
                        continue
                    with db._tx() as c:
                        c.execute(_INSERT, (
                            t["accession_number"], t["cik"], t["form_type"],
                            t["filing_date"], facts.entity_name,
                            facts.incorporation_code, facts.address_line1,
                            facts.address_line2, facts.address_city,
                            facts.address_state, facts.address_postal,
                            facts.address_phone, facts.address_kind,
                            facts.fiscal_year_end, facts.sic, now))
                    done += 1
                except SECBlockedError:
                    log("  ABORTED (rate ban); rerun to continue")
                    raise
                except Exception as exc:              # noqa: BLE001
                    failed += 1
                    log(f"  {t['cik']}: {type(exc).__name__}: {exc}")
                if i % 250 == 0:
                    log(f"  {i:,}/{len(targets):,} ...")
        except SECBlockedError:
            pass
        finally:
            session.close()

    log(f"header_cover +{done:,} | no block for cik {missed:,} | failed {failed:,}")
    return {"stored": done, "missing": missed, "failed": failed}


def header_summary(db_path: str = DEFAULT_DB_PATH) -> dict:
    """Shape of what has been read, for ``cli status``."""
    with FilingDB(db_path) as db:
        exists = db.conn.execute(
            "SELECT COUNT(*) n FROM sqlite_master WHERE type='table' "
            "AND name='header_cover'").fetchone()["n"]
        if not exists:
            return {"built": False}
        row = db.conn.execute(
            """SELECT COUNT(*) n, COUNT(DISTINCT cik) ciks,
                      SUM(incorporation_code IS NOT NULL) inc,
                      SUM(address_city IS NOT NULL) city,
                      SUM(address_kind = 'mail') mail
               FROM header_cover""").fetchone()
        return {"built": True, "rows": row["n"], "ciks": row["ciks"],
                "with_incorporation": row["inc"] or 0,
                "with_city": row["city"] or 0,
                "mail_only": row["mail"] or 0}
