"""
covertext.py — Level 4 source C: read the printed cover page when XBRL does not
say enough.

The one thing XBRL cannot tell you
----------------------------------
A company with two head offices tags only one.  Molson Coors prints both on its
10-K cover — Golden, Colorado *and* Montreal, Quebec, with two postcodes and two
telephone numbers — and its XBRL cover carries Golden alone.  So a registrant
with a genuine dual arrangement is indistinguishable, in the structured data,
from one with a single office.  The only place the second office exists is the
printed page.

This module reads that page.  It is deliberately narrow: it answers "does this
cover state more than one principal executive office, and what are they", and
nothing else.

Why line count is not the test
------------------------------
The obvious rule — "more than one line before *(Address of principal executive
offices)* means two offices" — is wrong, and wrong on the most common layout.
Apple's cover reads::

    One Apple Park Way
    Cupertino, California
    (Address of principal executive offices)

That is ONE address wrapped across two lines.  Molson Coors reads::

    P.O. Box 4030, BC555, Golden, Colorado, USA
    111 Boulevard Robert-Bourassa, 9th Floor, Montreal, Quebec, Canada
    (Address of principal executive offices)

Two complete addresses.  Counting lines cannot separate these.

What does separate them is the fields the SEC form makes **singular**: a cover
has one *(Zip Code)* and one *(Registrant's telephone number)*.  A filer with
two offices is forced to put two values in each — Molson Coors prints ``80401``
and ``H3C 2M1``, then ``303-279-6565 (Colorado)`` and ``514-521-1786
(Quebec)``.  Apple prints one of each.  So the postcode and telephone blocks
are the evidence, and the address lines are only read once that evidence says
to expect two.

Cost and scope
--------------
One full document fetch per filing — far more expensive than the header read,
so this runs over a **candidate list**, never the whole mirror.  Candidates come
from :func:`dual_candidates`: companies whose sources place the office in
different cities, plus any CIK asked for explicitly.

Usage
-----
    python -m finalized.cli covertext --run --cik 24545
    python -m finalized.cli covertext --run --candidates --limit 50
    python -m finalized.cli covertext --summary
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Optional

import requests
from lxml import html as lhtml

from .core import UA, DEFAULT_DELAY, SECBlockedError
from .database import DEFAULT_DB_PATH, FilingDB, utc_now

# Forms whose cover page carries the address block.  A prospectus repeats the
# registrant's address but is filed constantly and adds nothing.
COVER_FORMS = ("10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A",
               "10-Q", "10-Q/A")

# Tags after which a newline must be inserted.  Without this the document's
# text runs together and "...Colorado, USA" and "111 Boulevard..." become one
# line, which destroys exactly the structure this module reads.
_BLOCK_TAGS = {"p", "div", "tr", "td", "th", "br", "table", "li",
               "h1", "h2", "h3", "h4", "h5", "h6", "hr"}

_ADDRESS_MARK = re.compile(
    r"\(\s*address(?:es)?\s+of\s+principal\s+executive\s+offices?", re.I)
_INCORP_MARK = re.compile(
    r"\(\s*state\s+or\s+other\s+jurisdiction", re.I)
_ZIP_MARK = re.compile(r"\(\s*zip\s*code", re.I)
_PHONE_MARK = re.compile(r"\(\s*registrant.{0,4}s\s+telephone", re.I)
_IRS_MARK = re.compile(r"\(\s*i\.?r\.?s\.?\s+employer", re.I)

# An explicit second-office label.  Uranium Energy prints "(U.S. corporate
# headquarters)" and "(Canadian corporate headquarters)" on the same cover --
# the filer naming both offices outright, which is stronger evidence than any
# inference from formatting.
_HQ_LABEL = re.compile(
    r"\(\s*([A-Za-zÀ-ɏ.\s]{0,24}?)\s*"
    r"(?:corporate\s+(?:head\s?quarters|offices?)"
    r"|principal\s+(?:executive\s+)?offices?"
    r"|head\s+offices?)\s*\)", re.I)

# The SEC cover sits at the front of the document.  NatWest and IHG file
# COMBINED annual reports where the phrase "principal executive offices"
# also appears ~178,000 characters in, inside shareholder information -- and
# the text before it there is body copy, not an address.  Bounding the search
# to the cover region is what stops that being parsed as a headquarters.
_COVER_REGION = 60000

# US ZIP, Canadian postcode, UK-style outward code, and plain 4-6 digit codes.
_POSTCODE = re.compile(
    r"\b(?:\d{5}(?:-\d{4})?"                    # 90210 / 90210-1234
    r"|[A-Z]\d[A-Z]\s?\d[A-Z]\d"                # H3C 2M1
    r"|[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2})\b")   # SW1A 1AA
_PHONE = re.compile(r"(?:\+?\d[\d\-().\s]{7,}\d)")
# A telephone line labelled with a place is itself dual-office evidence:
# "303-279-6565 (Colorado)".
_PHONE_LABEL = re.compile(r"\(\s*([A-Za-zÀ-ɏ .'-]{3,24})\s*\)")

_NOISE = re.compile(
    r"^\s*(?:\(|_+$|N/?A\b|not applicable\b|\W*$)", re.I)
# An IRS Employer Identification Number, which sits beside the jurisdiction in
# the cover's two-column layout and is easily mistaken for it.
_EIN = re.compile(r"\d{2}-\d{7}")


@dataclass
class CoverPage:
    """What the printed cover states about where the registrant is."""
    incorporation_text: Optional[str] = None
    offices: list[str] = field(default_factory=list)
    postcodes: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    phone_labels: list[str] = field(default_factory=list)
    dual: bool = False
    reason: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.offices or self.incorporation_text)


def to_text(html_source: str) -> str:
    """
    Document HTML -> plain text with block boundaries preserved as newlines.

    lxml's ``text_content()`` concatenates without separators, so the two
    address lines of a dual-HQ cover arrive glued together ("...USA111
    Boulevard...").  Inserting a newline at each block tag is what makes the
    cover's line structure survive, and that structure is the whole signal.
    """
    if not html_source:
        return ""
    try:
        tree = lhtml.fromstring(html_source[:4_000_000])
    except Exception:                                   # noqa: BLE001
        return ""
    for bad in tree.xpath("//script|//style"):
        parent = bad.getparent()
        if parent is not None:
            parent.remove(bad)
    for el in tree.iter():
        if isinstance(el.tag, str) and el.tag.lower() in _BLOCK_TAGS:
            el.tail = "\n" + (el.tail or "")
            if el.text:
                el.text = "\n" + el.text
    text = tree.text_content()
    text = text.replace(" ", " ").replace("​", "")
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text)


_DOCUMENT_RE = re.compile(r"<DOCUMENT>(.*?)</DOCUMENT>", re.S | re.I)
_TEXT_RE = re.compile(r"<TEXT>(.*?)</TEXT>", re.S | re.I)
_FILENAME_RE = re.compile(r"<FILENAME>([^\r\n<]+)")
_TYPE_RE = re.compile(r"<TYPE>([^\r\n<]+)")


def primary_document(submission_text: str) -> Optional[str]:
    """
    The filing's main HTML document out of the multi-document submission.

    Prefers the document whose ``<TYPE>`` is the form itself (10-K), because a
    submission also carries exhibits, and an exhibit's first page is not the
    cover.  Falls back to the first HTML document.
    """
    best = None
    for block in _DOCUMENT_RE.findall(submission_text):
        fn = _FILENAME_RE.search(block)
        if not fn or not re.search(r"\.html?$", fn.group(1), re.I):
            continue
        body = _TEXT_RE.search(block)
        if not body:
            continue
        dtype = (_TYPE_RE.search(block) or [None, ""])[1].strip().upper()
        if dtype and not dtype.startswith("EX"):
            return body.group(1)
        best = best or body.group(1)
    return best


def _clean_lines(chunk: str) -> list[str]:
    out = []
    for raw in chunk.splitlines():
        line = raw.strip(" \t|")
        if not line or _NOISE.match(line):
            continue
        out.append(line)
    return out


def parse_cover(text: str) -> CoverPage:
    """
    Read the address block of a cover page.

    Returns what the page states, plus whether it states **two** principal
    executive offices.  The dual test is the postcode and telephone blocks, not
    the number of address lines — see the module docstring for why Apple's
    two-line single address would otherwise be read as two offices.
    """
    cp = CoverPage()
    if not text:
        cp.reason = "no document text"
        return cp
    # Only look at the cover region; see _COVER_REGION for why.
    region = text[:_COVER_REGION]
    m_addr = _ADDRESS_MARK.search(region)
    if not m_addr:
        cp.reason = ("no '(Address of principal executive offices)' marker on "
                     "the cover")
        return cp

    # The address lines sit between the incorporation (or IRS) marker and the
    # address marker.
    start = 0
    for mark in (_INCORP_MARK, _IRS_MARK):
        for m in mark.finditer(region, 0, m_addr.start()):
            start = max(start, m.end())
    start = max(start, m_addr.start() - 900)
    block = region[start:m_addr.start()]
    # Everything up to the first ")" belongs to the marker we started after.
    if start and ")" in block[:80]:
        block = block.split(")", 1)[1]

    # A bare postcode is its own field on the form, not an address line -- it
    # is collected separately below and would otherwise be reported as part of
    # the second office.
    cp.offices = [l for l in _clean_lines(block)
                  if len(l) > 6 and not _POSTCODE.fullmatch(l.strip())]

    m_inc = _INCORP_MARK.search(region)
    if m_inc:
        # The cover is a two-column table flattened to lines, so BOTH values can
        # precede BOTH labels:
        #     California / 94-2404110 / (State or other jurisdiction...) /
        #     (I.R.S. Employer Identification No.)
        # Taking the last line would return Apple's EIN, so skip anything that
        # is an employer number or otherwise has no letters in it.
        before = _clean_lines(region[max(0, m_inc.start() - 260):m_inc.start()])
        for line in reversed(before):
            if _EIN.fullmatch(line.strip()) or not re.search(r"[A-Za-z]", line):
                continue
            cp.incorporation_text = line.strip()
            break

    # Postcodes may sit inside the block (Uranium Energy prints one after each
    # address) or after the marker (Molson Coors prints both together).
    tail = region[m_addr.start():m_addr.start() + 900]
    scope = block + "\n" + tail
    m_zip = _ZIP_MARK.search(tail)
    cp.postcodes = list(dict.fromkeys(
        _POSTCODE.findall(block + "\n" + (tail[:m_zip.start()] if m_zip else ""))))

    m_phone = _PHONE_MARK.search(tail)
    if m_phone:
        seg = tail[m_zip.end() if m_zip else 0:m_phone.start()]
        cp.phones = [p.strip() for p in _PHONE.findall(seg)
                     if len(re.sub(r"\D", "", p)) >= 9]
        cp.phone_labels = [x.strip() for x in _PHONE_LABEL.findall(seg)
                           if not _POSTCODE.fullmatch(x.strip())]

    # Four independent signals, any one of which means the filer was forced to
    # state two offices in fields the form makes singular.
    signals = []
    hq_labels = [m.group(0).strip("() ") for m in _HQ_LABEL.finditer(scope)]
    hq_labels = [h for h in hq_labels if h.lower() != "address of principal "
                 "executive offices"]
    if len(hq_labels) > 1:
        signals.append(f"{len(hq_labels)} labelled offices "
                       f"({'; '.join(hq_labels[:2])})")
    n_zipmarks = len(_ZIP_MARK.findall(scope))
    if n_zipmarks > 1:
        signals.append(f"{n_zipmarks} separate (Zip Code) fields")
    if len(cp.postcodes) > 1:
        signals.append(f"{len(cp.postcodes)} postcodes "
                       f"({', '.join(cp.postcodes[:3])})")
    if len(cp.phones) > 1 and len(cp.phone_labels) > 1:
        signals.append(f"{len(cp.phones)} labelled telephone numbers "
                       f"({', '.join(cp.phone_labels[:3])})")

    if signals:
        cp.dual = True
        cp.reason = "cover states " + "; ".join(signals)
    elif cp.offices:
        cp.reason = "single office: one postcode and one telephone number"
    else:
        cp.reason = "address block not parsed"
    return cp


# ── storage ───────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS cover_office (
    accession_number   TEXT NOT NULL,
    cik                TEXT NOT NULL,
    form_type          TEXT,
    filing_date        TEXT,
    incorporation_text TEXT,       -- as printed, e.g. 'Delaware'
    office_count       INTEGER,    -- address lines found
    offices            TEXT,       -- the lines, newline separated
    postcodes          TEXT,       -- comma separated, as printed
    phones             TEXT,
    is_dual            INTEGER NOT NULL DEFAULT 0,
    dual_reason        TEXT,
    built_utc          TEXT NOT NULL,
    PRIMARY KEY (accession_number, cik)
);

CREATE INDEX IF NOT EXISTS ix_coveroffice_cik  ON cover_office(cik);
CREATE INDEX IF NOT EXISTS ix_coveroffice_dual ON cover_office(is_dual);
"""


def dual_candidates(db: FilingDB, limit: Optional[int] = None) -> list[str]:
    """CIKs worth spending a document fetch on."""
    have = db.conn.execute(
        "SELECT COUNT(*) n FROM sqlite_master WHERE type='table' "
        "AND name='entity_final'").fetchone()["n"]
    if not have:
        return []
    rows = db.conn.execute(
        f"""SELECT cik FROM entity_final WHERE hq_dual_candidate = 1
            ORDER BY cik {f'LIMIT {int(limit)}' if limit else ''}""").fetchall()
    return [r["cik"] for r in rows]


def _targets(db: FilingDB, cik: Optional[str], candidates: bool,
             limit: Optional[int], refresh: bool) -> list[dict]:
    forms = ",".join("?" * len(COVER_FORMS))
    params: list = list(COVER_FORMS)
    where = ""
    if cik:
        where += " AND f.cik = ?"
        params.append(str(cik))
    elif candidates:
        ciks = dual_candidates(db)
        if not ciks:
            return []
        where += f" AND f.cik IN ({','.join('?' * len(ciks))})"
        params.extend(ciks)
    if not refresh:
        where += (" AND NOT EXISTS (SELECT 1 FROM cover_office o "
                  "WHERE o.accession_number = f.accession_number "
                  "AND o.cik = f.cik)")
    rows = db.conn.execute(f"""
        SELECT f.accession_number, f.cik, f.form_type, f.filing_date,
               f.submission_txt_url
        FROM filings f
        WHERE f.form_type IN ({forms}) AND f.submission_txt_url IS NOT NULL
          {where}
        GROUP BY f.cik HAVING f.filing_date = MAX(f.filing_date)
        ORDER BY f.filing_date DESC
        {f'LIMIT {int(limit)}' if limit else ''}""", params).fetchall()
    return [dict(r) for r in rows]


def extract_cover_offices(
    db_path: str = DEFAULT_DB_PATH,
    cik: Optional[str] = None,
    candidates: bool = False,
    limit: Optional[int] = None,
    delay: float = DEFAULT_DELAY,
    refresh: bool = False,
    log: Callable[[str], None] = print,
) -> dict:
    """
    Read the printed cover of each target filing and record its offices.

    One FULL document fetch per filing — orders of magnitude dearer than the
    header read — so this is candidate-driven by design.  Pass ``--cik`` for a
    single company or ``--candidates`` to work the flagged list.
    """
    session = requests.Session()
    session.headers.update(UA)
    done = dual = skipped = failed = 0

    with FilingDB(db_path) as db:
        db.conn.executescript(SCHEMA)
        db.conn.commit()
        targets = _targets(db, cik, candidates, limit, refresh)
        log(f"{len(targets):,} cover page(s) to read")

        now = utc_now()
        try:
            for i, t in enumerate(targets, 1):
                try:
                    resp = session.get(t["submission_txt_url"], headers=UA,
                                       timeout=90)
                    if resp.status_code == 429:
                        raise SECBlockedError("HTTP 429 reading cover page")
                    resp.raise_for_status()
                    body = primary_document(
                        resp.content.decode("utf-8", "replace"))
                    cp = parse_cover(to_text(body or ""))
                    if not cp.ok:
                        skipped += 1
                        continue
                    with db._tx() as c:
                        c.execute(
                            """INSERT OR REPLACE INTO cover_office
                               (accession_number, cik, form_type, filing_date,
                                incorporation_text, office_count, offices,
                                postcodes, phones, is_dual, dual_reason,
                                built_utc)
                               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (t["accession_number"], t["cik"], t["form_type"],
                             t["filing_date"], cp.incorporation_text,
                             len(cp.offices), "\n".join(cp.offices),
                             ", ".join(cp.postcodes), ", ".join(cp.phones),
                             1 if cp.dual else 0, cp.reason, now))
                    done += 1
                    dual += bool(cp.dual)
                    if cp.dual:
                        log(f"  DUAL  {t['cik']:<9} {cp.reason}")
                except SECBlockedError as exc:
                    log(f"  ABORTED (rate ban): {exc}")
                    raise
                except Exception as exc:                  # noqa: BLE001
                    failed += 1
                    log(f"  {t['cik']}: {type(exc).__name__}: {exc}")
                if i % 25 == 0:
                    log(f"  {i:,}/{len(targets):,} ...")
        except SECBlockedError:
            pass
        finally:
            session.close()

    log(f"cover_office +{done:,} | dual {dual:,} | unparsed {skipped:,} | "
        f"failed {failed:,}")
    return {"stored": done, "dual": dual, "skipped": skipped, "failed": failed}


def covertext_summary(db_path: str = DEFAULT_DB_PATH) -> dict:
    with FilingDB(db_path) as db:
        have = db.conn.execute(
            "SELECT COUNT(*) n FROM sqlite_master WHERE type='table' "
            "AND name='cover_office'").fetchone()["n"]
        if not have:
            return {"built": False}
        r = db.conn.execute(
            """SELECT COUNT(*) n, COUNT(DISTINCT cik) ciks,
                      SUM(is_dual) dual FROM cover_office""").fetchone()
        return {"built": True, "rows": r["n"], "ciks": r["ciks"],
                "dual": r["dual"] or 0}
