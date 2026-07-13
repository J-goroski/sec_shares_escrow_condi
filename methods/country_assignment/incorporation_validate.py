"""
incorporation_validate.py — Cross-validate a filing's incorporation
jurisdiction and principal-executive-office address across three independent
sources, to catch bad/ambiguous XBRL and dual-headquarters filers.

The problem
-----------
The DEI XBRL fact ``EntityIncorporationStateCountryCode`` (and the address
``EntityAddressStateOrProvince``) carry an **EDGAR** state/country code — NOT an
ISO code.  In EDGAR's scheme ``CA``=California, ``DE``=Delaware, ``IL``=Illinois,
``A1``=British Columbia, ``2M``=Germany, ``E9``=Cayman Islands, ``K3``=Hong Kong.
Two failure modes bite downstream consumers:

  1. **ISO-collision misreads.**  A naive consumer treats the 2-letter code as
     ISO-3166 and turns ``CA``→Canada, ``DE``→Germany, ``IL``→Israel,
     ``KY``→Cayman, ``PA``→Panama, ``GA``→Georgia(country)…  The code was right;
     the *reading* was wrong.
  2. **The XBRL is simply wrong / inconsistent** — e.g. the header says the
     registrant is incorporated in one place and the XBRL says another (Alibaba:
     XBRL ``E9``=Cayman vs header ``K3``=Hong Kong), or an address province is
     tagged with a postal code (``BC``) instead of the EDGAR code (``A1``).

  3. **Dual headquarters.**  Some filers (Molson Coors/TAP, Uranium Energy/UEC,
     …) print **two** principal-executive-office addresses on the cover — a US
     one and a foreign one — so any single "the HQ is X" value is incomplete.

The defense
-----------
Compare three independent sources and flag disagreement:

  * **XBRL** — ``EntityIncorporationStateCountryCode`` + the DEI address facts.
  * **SEC-HEADER** — the SGML ``<SEC-HEADER>`` block of the full submission text,
    which carries EDGAR's own ``STATE OF INCORPORATION`` and the business/mail
    address (same EDGAR code scheme as the XBRL, so codes compare directly).
  * **Cover text** — the spelled-out jurisdiction ("Delaware", "Cayman Islands")
    and the address block on the cover page (authoritative, human-readable).

This module is additive — it reuses the shared rate-limited fetcher and the
existing XBRL / manual-extract helpers, and changes no existing file.

Usage
-----
    from methods.sec_filings_sync import fetch_filings_for_ciks
    from methods.country_assignment import validate_filing, validate_filings

    filings = fetch_filings_for_ciks(["24545"], form_types=["10-K"])  # Molson Coors
    check = validate_filing(filings[0])
    print(check.status, check.issues)

    df = validate_filings(filings)          # one row per filing, flags + evidence
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from methods.sec_filings_sync import UA, DEFAULT_DELAY, SECBlockedError
from methods.sec_xbrl_extract import parse_filings, entity_facts
from methods.sec_filing_manual_extract import (
    fetch_submission_text, clean_submission_html, _html_to_text, _record_to_dict,
)
# Code tables + decoders live in .codes (loaded from the CSV data files).
from methods.country_assignment.codes import (
    US_STATES, CA_PROVINCES, EDGAR_COUNTRY, ISO_COLLISION,
    decode_code, country_of,
)

import requests

__all__ = [
    "IncorporationCheck",
    "decode_code", "country_of", "parse_sec_header",
    "extract_cover_incorporation_address",
    "validate_filing", "validate_filings",
    "US_STATES", "CA_PROVINCES", "EDGAR_COUNTRY", "ISO_COLLISION",
]

_IDENTITY_FIELDS = [
    "cik", "entity_name", "ticker", "form_type",
    "filing_date", "report_date", "accession_number",
]


# ══════════════════════════════════════════════════════════════════════════════
#  SEC-HEADER parsing
# ══════════════════════════════════════════════════════════════════════════════

_ADDR_FIELDS = ["STREET 1", "STREET 2", "CITY", "STATE", "ZIP", "BUSINESS PHONE"]


def _header_block(text: str) -> str:
    m = re.search(r"<SEC-HEADER>(.*?)</SEC-HEADER>", text, re.S | re.I)
    if m:
        return m.group(1)
    i = text.find("<DOCUMENT>")
    return text[:i] if i > 0 else text[:8000]


def _hfield(block: str, name: str) -> Optional[str]:
    m = re.search(rf"{re.escape(name)}:\s*([^\r\n]+)", block, re.I)
    return m.group(1).strip() if m else None


def _address_at(block: str, start: int) -> dict:
    """Read the address sub-block fields that follow position `start`."""
    window = block[start:start + 600]
    out = {}
    for f in _ADDR_FIELDS:
        m = re.search(rf"{re.escape(f)}:\s*([^\r\n]+)", window, re.I)
        out[f.lower().replace(" ", "_")] = m.group(1).strip() if m else None
    return out


def parse_sec_header(text: str) -> dict:
    """
    Pull the registrant's authoritative EDGAR filer-database values from the
    ``<SEC-HEADER>`` block: state of incorporation and business/mail addresses.
    Captures the count of FILER blocks (co-registrants) too.
    """
    block = _header_block(text)
    incorp = _hfield(block, "STATE OF INCORPORATION")

    bm = re.search(r"BUSINESS ADDRESS:", block, re.I)
    mm = re.search(r"MAIL ADDRESS:", block, re.I)
    business = _address_at(block, bm.end()) if bm else {}
    mail     = _address_at(block, mm.end()) if mm else {}

    n_filers = len(re.findall(r"^\s*FILER:", block, re.M)) or \
        (1 if re.search(r"COMPANY CONFORMED NAME", block, re.I) else 0)

    return {
        "incorporation_code": incorp,
        "business_address": business,
        "mail_address": mail,
        "n_filers": n_filers,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  Cover-page parsing  (jurisdiction + principal office address, dual-HQ)
# ══════════════════════════════════════════════════════════════════════════════

# Country / "headquarters" cues used to anchor an address line on the cover.
_COUNTRY_CUES = re.compile(
    r"\b(U\.?S\.?A\.?|United States|Canada|Québec|Quebec|Ontario|British Columbia|"
    r"Cayman|Bermuda|Ireland|Germany|Israel|China|Hong Kong|United Kingdom|"
    r"England|Netherlands|Switzerland|Luxembourg|Singapore|Australia|Japan|"
    r"Brazil|Mexico|France|Panama|Marshall Islands|Jersey|Guernsey)\b", re.I)
_HQ_MARKER = re.compile(
    r"\b(corporate headquarters|principal (?:executive )?office|head office|"
    r"registered office)\b", re.I)
_INCORP_LABEL = re.compile(
    r"(?:state|jurisdiction|country|province)[^)]*incorporat|incorporat\w*\s+"
    r"(?:or|of)\s+organization", re.I)
_ADDR_LABEL = re.compile(r"address of principal executive office", re.I)
_END_OF_COVER = re.compile(r"securities registered pursuant to section 12", re.I)


def _is_label_or_number(line: str) -> bool:
    """True for cover lines that are parenthetical labels, IRS numbers, or zips."""
    l = line.strip()
    if not l:
        return True
    if l.lower() in ("n/a", "na", "none", "not applicable", "—", "-", "not applicable."):
        return True
    if l.startswith("(") or l.endswith(")"):
        return True
    if re.fullmatch(r"\d{2}-\d{7}", l):                 # IRS EIN
        return True
    if re.fullmatch(r"[\d\-\s]+", l):                   # bare number / US zip
        return True
    if re.search(r"i\.?r\.?s\.?|identification|zip code|employer|exact name|"
                 r"telephone|commission|file number|registrant|mark one|"
                 r"form 10-|form 20-|annual report|securities exchange act", l, re.I):
        return True
    return False


def extract_cover_incorporation_address(cover_text: str) -> dict:
    """
    From the cover-page text, extract the spelled-out incorporation jurisdiction,
    the principal-office address line(s), and dual-HQ evidence.

    Returns {incorporation_text, addresses:[...], dual_hq:bool, dual_hq_evidence,
             countries:[...]}.
    """
    lines = [l.strip() for l in cover_text.split("\n") if l.strip()]
    end = next((i for i, l in enumerate(lines) if _END_OF_COVER.search(l)),
               min(len(lines), 70))
    region = lines[:end]

    # --- incorporation jurisdiction: value line just before the label ---
    incorp_text = None
    inc_idx = next((i for i, l in enumerate(region) if _INCORP_LABEL.search(l)), None)
    if inc_idx is not None:
        for j in range(inc_idx - 1, max(-1, inc_idx - 6), -1):
            if not _is_label_or_number(region[j]):
                incorp_text = region[j]
                break

    # --- principal-office address anchors + dual-HQ evidence ---
    # Address "anchor" = a line naming a city with a country/US-state/province.
    anchors, countries = [], []
    for l in region:
        if _is_label_or_number(l):
            continue
        if "," in l and _COUNTRY_CUES.search(l) and len(l) < 120:
            if l not in anchors:
                anchors.append(l)
            m = _COUNTRY_CUES.search(l)
            c = _canon_country(m.group(0))
            if c and c not in countries:
                countries.append(c)

    hq_markers = [l for l in region if _HQ_MARKER.search(l) and l.startswith("(")]

    # Dual HQ needs a genuine second office: two distinct COUNTRIES among the
    # cover addresses, or two explicit "headquarters/office" markers.  (Counting
    # raw anchor lines over-fires when one address wraps across two lines, e.g.
    # "British Columbia, Canada" / "Vancouver, British Columbia V6C 3R8".)
    dual = False
    evidence = []
    if len(countries) >= 2:
        dual = True
        evidence.append("addresses in multiple countries: " + ", ".join(countries))
    if len(hq_markers) >= 2:
        dual = True
        evidence.append(f"{len(hq_markers)} 'headquarters/office' markers: "
                        + "; ".join(m.strip('()') for m in hq_markers))

    return {
        "incorporation_text": incorp_text,
        "addresses": anchors,
        "countries": countries,
        "dual_hq": dual,
        "dual_hq_evidence": "; ".join(evidence) or None,
    }


def _canon_country(token: str) -> str:
    t = token.lower().replace(".", "")
    if t in ("usa", "us", "united states"):
        return "United States"
    if t in ("québec", "quebec", "ontario", "british columbia", "canada"):
        return "Canada"
    return token.title()


# ══════════════════════════════════════════════════════════════════════════════
#  Cross-validation
# ══════════════════════════════════════════════════════════════════════════════

def _norm_name(s: str | None) -> str:
    """Normalize a jurisdiction name for comparison."""
    if not s:
        return ""
    s = s.lower()
    s = re.sub(r"\b(the )?(state|commonwealth|province|country)( of| or province of)?\b",
               " ", s)
    s = re.sub(r"\b(federal republic|republic|kingdom|people s republic|"
               r"islands?) of\b", " ", s)
    s = re.sub(r"[^a-z ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _same_name(a: str, b: str) -> bool:
    """Two normalized jurisdiction names refer to the same place.

    Tolerant of substrings ("Germany" ⊂ "Federal Republic of Germany") and of
    word order / comma variants ("British Virgin Islands" vs "Virgin Islands,
    British") — but not of sub/superset token overlap (so Virginia ≠ West
    Virginia)."""
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    return set(a.split()) == set(b.split())


@dataclass
class IncorporationCheck:
    """Result of cross-validating one filing's incorporation + HQ address."""
    identity: dict

    # incorporation
    xbrl_incorp_code: Optional[str] = None
    xbrl_incorp_name: Optional[str] = None
    xbrl_incorp_kind: Optional[str] = None
    header_incorp_code: Optional[str] = None
    header_incorp_name: Optional[str] = None
    text_incorp: Optional[str] = None
    iso_trap: Optional[str] = None
    incorp_status: str = "unknown"       # agree | mismatch | review | unknown

    # address / HQ
    xbrl_address: Optional[str] = None
    header_business_address: Optional[str] = None
    header_mail_address: Optional[str] = None
    cover_addresses: list = field(default_factory=list)
    xbrl_country: Optional[str] = None
    header_country: Optional[str] = None
    address_status: str = "unknown"      # agree | mismatch | dual_hq | unknown
    dual_hq: bool = False
    dual_hq_evidence: Optional[str] = None

    issues: list = field(default_factory=list)
    status: str = "ok"                   # ok | review

    def as_row(self) -> dict:
        row = {k: self.identity.get(k) for k in _IDENTITY_FIELDS}
        row.update({
            "xbrl_incorp_code": self.xbrl_incorp_code,
            "xbrl_incorp_name": self.xbrl_incorp_name,
            "header_incorp_code": self.header_incorp_code,
            "text_incorp": self.text_incorp,
            "incorp_status": self.incorp_status,
            "iso_trap": self.iso_trap,
            "xbrl_country": self.xbrl_country,
            "header_country": self.header_country,
            "address_status": self.address_status,
            "dual_hq": self.dual_hq,
            "dual_hq_evidence": self.dual_hq_evidence,
            "xbrl_address": self.xbrl_address,
            "header_business_address": self.header_business_address,
            "cover_addresses": " || ".join(self.cover_addresses) or None,
            "status": self.status,
            "issues": "; ".join(self.issues) or None,
        })
        return row


def _fmt_addr(d: dict) -> Optional[str]:
    parts = [d.get("street_1"), d.get("street_2"), d.get("city"),
             d.get("state"), d.get("zip")]
    s = ", ".join(str(p) for p in parts if p)
    return s or None


def _check_incorporation(chk: IncorporationCheck) -> None:
    """Populate incorporation fields/flags on `chk` and append any issues."""
    xd = decode_code(chk.xbrl_incorp_code)
    chk.xbrl_incorp_name = xd["name"]
    chk.xbrl_incorp_kind = xd["kind"]
    hd = decode_code(chk.header_incorp_code)
    chk.header_incorp_name = hd["name"]

    # ISO-collision warning (informational): code decodes to a US state/province
    # but a naive ISO read would say a different country.
    code = (chk.xbrl_incorp_code or "").strip().upper()
    if code in ISO_COLLISION and xd["kind"] in ("us_state", "ca_province"):
        chk.iso_trap = (f"code '{code}' = {xd['name']} ({xd['kind']}); "
                        f"a naive ISO read would wrongly say {ISO_COLLISION[code]}")

    names = {
        "xbrl":   _norm_name(xd["name"]),
        "header": _norm_name(hd["name"]),
        "text":   _norm_name(chk.text_incorp),
    }
    present = {k: v for k, v in names.items() if v}
    hcode = (chk.header_incorp_code or "").strip().upper()
    # Two authoritative EDGAR codes (XBRL + header) agreeing is the strongest
    # signal; a differently-worded cover text ("England and Wales" vs code
    # X0=United Kingdom) is then just granularity, not an error.
    header_corroborates = bool(code and hcode and code == hcode)

    # 1. XBRL vs header code (same EDGAR scheme — compare codes directly).
    if code and hcode and code != hcode:
        xn = f" ({xd['name']})" if xd["name"] else ""
        hn = f" ({hd['name']})" if hd["name"] else ""
        chk.incorp_status = "mismatch"
        chk.issues.append(
            f"XBRL incorporation {code}{xn} != header {hcode}{hn}")

    # 2. Cover text vs XBRL-decoded name.
    if chk.incorp_status != "mismatch" and names["text"] and names["xbrl"] \
            and not _same_name(names["text"], names["xbrl"]):
        if header_corroborates:
            chk.issues.append(
                f"note: cover text says '{chk.text_incorp}' "
                f"(code {code} = {xd['name']})")
        else:
            chk.incorp_status = "mismatch"
            chk.issues.append(
                f"cover text says '{chk.text_incorp}' but XBRL code {code} "
                f"decodes to '{xd['name'] or '?'}'")

    # 3. Overall corroboration status.
    if chk.incorp_status != "mismatch":
        vals = list(present.values())
        if not vals:
            chk.incorp_status = "unknown"
        elif header_corroborates:
            chk.incorp_status = "agree"          # XBRL + header codes agree
        elif len(vals) == 1:
            chk.incorp_status = "unconfirmed"    # only one source — no corroboration
        elif all(_same_name(a, b) for a in vals for b in vals):
            chk.incorp_status = "agree"
        else:
            chk.incorp_status = "review"
    if code and xd["kind"] == "unknown":
        chk.issues.append(f"XBRL incorporation code '{code}' not in EDGAR table")


def _check_address(chk: IncorporationCheck, xf: dict, header: dict,
                   cover: dict) -> None:
    """Populate address/HQ fields/flags on `chk` and append any issues."""
    chk.xbrl_address = ", ".join(str(xf.get(k)) for k in
        ["address_line1", "address_line2", "address_city",
         "address_state", "address_zip"] if xf.get(k)) or None
    chk.header_business_address = _fmt_addr(header.get("business_address", {}))
    chk.header_mail_address = _fmt_addr(header.get("mail_address", {}))
    chk.cover_addresses = cover.get("addresses", [])

    chk.xbrl_country = country_of(xf.get("address_state"))
    hb = header.get("business_address", {})
    chk.header_country = country_of(hb.get("state"))

    chk.dual_hq = cover.get("dual_hq", False)
    chk.dual_hq_evidence = cover.get("dual_hq_evidence")

    # XBRL vs header principal-office comparison keys on CITY and COUNTRY — the
    # strong discriminators.  Zip alone is skipped: zip+4 vs zip5 and PO-box vs
    # street zips differ harmlessly for the same office (false positives).
    norm = lambda v: re.sub(r"[^a-z0-9]", "", (v or "").lower())
    xcity, hcity = norm(xf.get("address_city")), norm(hb.get("city"))
    mismatch = False
    # substring-tolerant: "Maranello (MO)"~"Maranello", "Dublin 4"~"Dublin".
    if xcity and hcity and xcity != hcity \
            and xcity not in hcity and hcity not in xcity:
        mismatch = True
        chk.issues.append(
            f"XBRL office city ({xf.get('address_city')}) != header city "
            f"({hb.get('city')})  [XBRL: {chk.xbrl_address} | HDR: "
            f"{chk.header_business_address}]")
    if chk.xbrl_country and chk.header_country and \
            chk.xbrl_country != chk.header_country:
        mismatch = True
        chk.issues.append(
            f"XBRL office country ({chk.xbrl_country}) != header country "
            f"({chk.header_country})")

    if chk.dual_hq:
        chk.address_status = "dual_hq"
        chk.issues.append(f"dual headquarters — {chk.dual_hq_evidence}")
    elif mismatch:
        chk.address_status = "mismatch"
    elif xcity or hcity:
        chk.address_status = "agree"
    else:
        chk.address_status = "unknown"


def validate_filing(
    filing,
    session: requests.Session | None = None,
    delay: float = DEFAULT_DELAY,
    submission_text: str | None = None,
    entity_row: dict | None = None,
) -> IncorporationCheck:
    """
    Cross-validate one filing's incorporation + principal-office address.

    Pulls the XBRL entity facts (unless ``entity_row`` is supplied) and the full
    submission text (unless ``submission_text`` is supplied), parses the
    SEC-HEADER and the cover page, and compares all three sources.

    Parameters
    ----------
    filing : FilingRecord | dict | sync_df row
        Must carry ``submission_txt_url`` and (for XBRL) ``xbrl_instance_url``.
    submission_text, entity_row :
        Optional pre-fetched inputs (handy for batch/testing to avoid re-fetch).
    """
    rec = _record_to_dict(filing)
    identity = {k: rec.get(k) for k in _IDENTITY_FIELDS}
    chk = IncorporationCheck(identity=identity)

    # --- XBRL entity facts ---
    xf: dict = entity_row or {}
    if not xf and rec.get("xbrl_instance_url"):
        try:
            facts = parse_filings([rec], session=session, delay=delay, verbose=False)
            ent = entity_facts(facts)
            if len(ent):
                xf = ent.iloc[0].to_dict()
        except SECBlockedError:
            raise
        except Exception as exc:                        # noqa: BLE001
            chk.issues.append(f"XBRL parse failed: {exc}")
    chk.xbrl_incorp_code = (xf.get("incorporation_state") or None)

    # --- full submission text → header + cover ---
    if submission_text is None:
        submission_text = fetch_submission_text(
            rec["submission_txt_url"], session=session, delay=delay)
    header = parse_sec_header(submission_text)
    chk.header_incorp_code = header.get("incorporation_code")

    clean, _, _ = clean_submission_html(submission_text, rec.get("primary_document"))
    cover_text = _html_to_text(clean)[:8000] if clean else ""
    cover = extract_cover_incorporation_address(cover_text)
    chk.text_incorp = cover.get("incorporation_text")

    # --- cross-validate ---
    _check_incorporation(chk)
    _check_address(chk, xf, header, cover)

    # The ISO-collision trap only warrants review when the reading is NOT
    # corroborated — if all sources agree it's Delaware, the trap is resolved.
    chk.status = "review" if (
        chk.incorp_status in ("mismatch", "review")
        or chk.address_status in ("mismatch", "dual_hq")
        or (chk.iso_trap and chk.incorp_status != "agree")
    ) else "ok"
    return chk


def validate_filings(
    filings,
    session: requests.Session | None = None,
    delay: float = DEFAULT_DELAY,
    verbose: bool = True,
) -> pd.DataFrame:
    """
    Run ``validate_filing`` over many filings → one tidy row per filing.

    Accepts a ``sync_df`` DataFrame, a list of ``FilingRecord``, or dicts. A
    single bad filing is logged and skipped; ``SECBlockedError`` propagates.
    """
    if isinstance(filings, pd.DataFrame):
        records = filings.to_dict("records")
    else:
        records = list(filings)

    own = session is None
    sess = session or requests.Session()
    if own:
        sess.headers.update(UA)

    rows = []
    try:
        for rec in records:
            rd = _record_to_dict(rec)
            label = (f"{rd.get('ticker') or rd.get('cik')} "
                     f"{rd.get('form_type')} {rd.get('filing_date')}")
            if not rd.get("submission_txt_url"):
                continue
            try:
                chk = validate_filing(rd, session=sess, delay=delay)
            except SECBlockedError:
                raise
            except Exception as exc:                    # noqa: BLE001
                if verbose:
                    print(f"[incorp] {label:<26} ERROR: {exc}")
                continue
            rows.append(chk.as_row())
            if verbose:
                flag = "OK " if chk.status == "ok" else "REVIEW"
                print(f"[incorp] {label:<26} {flag}  "
                      f"inc={chk.incorp_status} addr={chk.address_status}"
                      f"{'  DUAL-HQ' if chk.dual_hq else ''}"
                      f"{'  ISO-TRAP' if chk.iso_trap else ''}")
    finally:
        if own:
            sess.close()

    cols = _IDENTITY_FIELDS + [
        "xbrl_incorp_code", "xbrl_incorp_name", "header_incorp_code",
        "text_incorp", "incorp_status", "iso_trap",
        "xbrl_country", "header_country", "address_status", "dual_hq",
        "dual_hq_evidence", "xbrl_address", "header_business_address",
        "cover_addresses", "status", "issues"]
    return pd.DataFrame(rows, columns=cols)
