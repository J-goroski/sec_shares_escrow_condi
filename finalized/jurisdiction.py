"""
jurisdiction.py — decode EDGAR state/country codes into full names.

The one thing to understand
---------------------------
**EDGAR codes are not ISO 3166.**  They look like ISO and they are not, and the
overlap is silent rather than loud:

    CA   EDGAR: California        ISO: Canada
    DE   EDGAR: Delaware          ISO: Germany
    CO   EDGAR: Colorado          ISO: Colombia
    ID   EDGAR: Idaho             ISO: Indonesia

24 codes collide this way.  ``CA`` alone appears as an incorporation code 60
times in this mirror — every one of them California, none of them Canada.  A
pipeline that hands an EDGAR code to an ISO library gets a plausible, wrong
country and no error.

Worse, both conventions live in the *same database*: a cover page tags
``EntityAddressStateOrProvince = 'CA'`` meaning California, while a segment fact
tags ``country:CA`` meaning Canada, because the ``country`` XBRL namespace *is*
ISO.  The two cannot be told apart by looking at the string, only by knowing
which field it came from — which is why decoding belongs in one place and every
consumer reads full names instead.

So this module never returns a code.  It returns ``state`` and ``country`` as
**full names**, separately, so "California" can never be mistaken for "Canada".

Three grains, one answer
------------------------
    state     52 US states/territories   -> country = United States
    province  11 Canadian provinces      -> country = Canada
    country   235 sovereign entries      -> country = itself

``A1`` is "British Columbia, Canada" — a province, so it yields
``state='British Columbia', country='Canada'``.  ``Z4`` is "Canada (Federal
Level)" — the same country at national grain, so it yields
``state=None, country='Canada'``.  That is why a filing tagging ``A1`` and one
tagging ``Z4`` are compatible rather than contradictory.

The tables are DATA, not code
-----------------------------
``data/edgar_state_country_codes.csv`` (309 codes), plus the collision list and
a small alias table for free text ("England and Wales" -> United Kingdom).
Editing a mapping is editing a CSV, never this file.

Usage
-----
    from finalized.jurisdiction import decode, country_of

    j = decode("A1")
    j.state, j.country          # ('British Columbia', 'Canada')
    country_of("CA")            # 'United States'  (California, NOT Canada)
    decode("ZZ").ok             # False - unknown code, never guessed
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

_CODES_CSV = os.path.join(_DATA, "edgar_state_country_codes.csv")
_COLLISIONS_CSV = os.path.join(_DATA, "state_country_collisions.csv")
_ALIASES_CSV = os.path.join(_DATA, "jurisdiction_aliases.csv")
_POSTAL_CSV = os.path.join(_DATA, "subnational_postal_codes.csv")
_ISO_CSV = os.path.join(_DATA, "iso_country_codes.csv")

UNITED_STATES = "United States"
CANADA = "Canada"


@dataclass(frozen=True)
class Jurisdiction:
    """A decoded EDGAR code. ``ok`` is False when the code is not in the table."""
    code: str
    ok: bool
    name: Optional[str] = None        # the table's full name, verbatim
    category: Optional[str] = None    # 'state' | 'province' | 'country'
    state: Optional[str] = None       # full state/province name, or None
    country: Optional[str] = None     # full country name
    iso_collision: Optional[str] = None   # what ISO would have said, if it differs

    @property
    def is_us(self) -> bool:
        return self.country == UNITED_STATES


@lru_cache(maxsize=1)
def _load_codes() -> dict[str, dict]:
    with open(_CODES_CSV, newline="", encoding="utf-8") as fh:
        return {r["code"].strip().upper(): r for r in csv.DictReader(fh)}


@lru_cache(maxsize=1)
def _load_collisions() -> dict[str, str]:
    try:
        with open(_COLLISIONS_CSV, newline="", encoding="utf-8") as fh:
            return {r["code"].strip().upper(): r["iso_country"]
                    for r in csv.DictReader(fh)}
    except FileNotFoundError:
        return {}


@lru_cache(maxsize=1)
def _load_aliases() -> dict[str, str]:
    try:
        with open(_ALIASES_CSV, newline="", encoding="utf-8") as fh:
            return {r["text"].strip().casefold(): r["country"]
                    for r in csv.DictReader(fh)}
    except FileNotFoundError:
        return {}


def _clean_country_name(name: str) -> str:
    """'Canada (Federal Level)' -> 'Canada'.

    The table spells the national grain explicitly so it can be told apart from
    the provinces; downstream only wants the country.
    """
    return name.split(" (Federal Level)")[0].strip()


@lru_cache(maxsize=4096)
def decode(code: Optional[str]) -> Jurisdiction:
    """
    Decode one EDGAR state/country code into full names.

    An unknown code returns ``ok=False`` with everything None rather than a
    guess — a wrong jurisdiction is far more damaging than a missing one, and
    the caller can see which it got.
    """
    if code is None:
        return Jurisdiction(code="", ok=False)
    key = str(code).strip().upper()
    if not key:
        return Jurisdiction(code="", ok=False)

    row = _load_codes().get(key)
    if row is None:
        return Jurisdiction(code=key, ok=False)

    name = (row.get("name") or "").strip()
    category = (row.get("category") or "").strip().lower()
    collision = _load_collisions().get(key)

    if category == "state":
        return Jurisdiction(key, True, name, "state", name, UNITED_STATES,
                            collision)
    if category == "province":
        # "British Columbia, Canada" -> state='British Columbia'
        state = name.split(",")[0].strip()
        return Jurisdiction(key, True, name, "province", state, CANADA,
                            collision)
    return Jurisdiction(key, True, name, "country", None,
                        _clean_country_name(name), collision)


def country_of(code: Optional[str]) -> Optional[str]:
    """Full country name for an EDGAR code, or None if it is not in the table."""
    return decode(code).country


def state_of(code: Optional[str]) -> Optional[str]:
    """Full state/province name, or None for a country-level code."""
    return decode(code).state


def country_from_text(text: Optional[str]) -> Optional[str]:
    """
    Map free text to a country name via the alias table.

    Covers the sub-jurisdiction and formal-name variants filers actually write
    ("England and Wales" -> United Kingdom, "Republic of China" -> Taiwan).
    Returns None when the text is not a known alias; it does not fuzzy-match,
    because "Georgia" is both a US state and a country and guessing is how that
    goes wrong.
    """
    if not text:
        return None
    key = str(text).strip().casefold()
    aliases = _load_aliases()
    if key in aliases:
        return aliases[key]
    # A full country name from the code table counts as its own alias.
    for row in _load_codes().values():
        if (row.get("category") or "").strip().lower() == "country":
            if _clean_country_name(row["name"]).casefold() == key:
                return _clean_country_name(row["name"])
    return None


def same_country(a: Optional[str], b: Optional[str]) -> Optional[bool]:
    """
    Do two EDGAR codes describe the same country?

    Returns None when either code is unknown, so "cannot tell" is never
    silently reported as "differs".  This is what makes ``A1`` (British
    Columbia) and ``Z4`` (Canada) compatible rather than a conflict.
    """
    ja, jb = decode(a), decode(b)
    if not ja.ok or not jb.ok:
        return None
    return ja.country == jb.country


@lru_cache(maxsize=1)
def _load_iso() -> dict[str, str]:
    try:
        with open(_ISO_CSV, newline="", encoding="utf-8") as fh:
            return {r["code"].strip().upper(): r["name"].strip()
                    for r in csv.DictReader(fh)}
    except FileNotFoundError:
        return {}


def country_from_iso(code: Optional[str]) -> Optional[str]:
    """
    Decode an **ISO 3166-1 alpha-2** country code to a full name.

    THIS IS NOT :func:`decode`.  The SEC cover page uses two different code
    systems in adjacent fields, and the same two letters mean different things
    in each:

        EntityAddressStateOrProvince   EDGAR    CA = California,  DE = Delaware
        EntityAddressCountry           ISO      CA = Canada,      DE = Germany

    Verified in this mirror: every filing tagging ``EntityAddressCountry='CA'``
    is Canadian (IMAX in Mississauga, Waste Connections in Woodbridge — both
    Ontario), and every ``'DE'`` is German or European (SmartKem, Veraxa
    Biotech AG).  Decoding that field with the EDGAR table would place IMAX in
    the United States, and would do so silently — no error, just a wrong
    country.

    So the two decoders are separate functions with separate tables on purpose.
    Passing an EDGAR code here, or an ISO code to :func:`decode`, is a bug that
    no exception will catch — only the field name tells you which to use.
    """
    if not code:
        return None
    return _load_iso().get(str(code).strip().upper())


@lru_cache(maxsize=1)
def _load_postal() -> dict[tuple[str, str], dict]:
    """(code, country) -> {name, needs_country_context} for postal abbreviations."""
    try:
        with open(_POSTAL_CSV, newline="", encoding="utf-8") as fh:
            return {(r["code"].strip().upper(), r["country"].strip()): {
                        "name": r["name"].strip(),
                        "needs_context": r.get("needs_country_context",
                                               "1").strip() == "1"}
                    for r in csv.DictReader(fh)}
    except FileNotFoundError:
        return {}


@dataclass(frozen=True)
class Office:
    """A resolved principal executive office location."""
    state_code: Optional[str] = None      # as filed, EDGAR code or postal abbr
    state: Optional[str] = None           # full name, or None if unresolvable
    country: Optional[str] = None         # full name, or None if unknown
    resolved: bool = False
    reason: str = ""
    # True when the country came from an EDGAR code or from stated evidence;
    # False when it was inferred from a postal abbreviation alone.  'AB' is
    # unique to Canada Post, but AEN Group tags 'AB' with its office in
    # Zaoyang, China -- so the inference is usually right and occasionally
    # nonsense, which is a confidence question, not a yes/no one.
    corroborated: bool = True


def resolve_office(
    state_code: Optional[str],
    country_text: Optional[str] = None,
    incorporation_code: Optional[str] = None,
) -> Office:
    """
    Resolve the office's state and country from what the cover actually tagged.

    ``EntityAddressStateOrProvince`` is not one code system.  US filers write an
    EDGAR state code; Canadian filers usually write the Canada Post
    abbreviation; and some filers write something else entirely.  ``NL`` alone
    appears three ways in this mirror:

        FORTIS INC.  St. John's, NL   -> Newfoundland and Labrador (Canada)
        FEMSA        Monterrey, NL    -> Nuevo Leon (MEXICO)
        LAVA Tx      Utrecht, NL      -> the NETHERLANDS, i.e. a country

    So a sub-national code cannot be decoded on its own.  This resolves it only
    inside a country that has been established independently, and otherwise
    declines — leaving ``state``/``country`` None with a reason, rather than
    inventing a province.

    Country evidence, strongest first:

    1. ``country_text`` — ``EntityAddressCountry`` as tagged (sparse but
       authoritative when present);
    2. the code itself being a US state in the EDGAR table;
    3. the *incorporation* country, used ONLY to admit a Canada Post
       abbreviation.  It is not general evidence of where the office is: a
       Cayman-incorporated company's office is almost never in the Caymans.
    """
    code = (state_code or "").strip().upper() or None
    # EntityAddressCountry is ISO, so it is decoded with the ISO table and
    # never the EDGAR one -- see country_from_iso for why that distinction is
    # load-bearing.  Free text ("England and Wales") falls back to the aliases.
    stated = (country_from_iso(country_text)
              or country_from_text(country_text)) if country_text else None

    if code is None:
        return Office(None, None, stated, bool(stated),
                      "no state code tagged" if not stated
                      else "country tagged, no state")

    j = decode(code)
    if j.ok and j.category in ("state", "province"):
        # An EDGAR sub-national code carries its own country unambiguously.
        return Office(code, j.state, j.country, True,
                      f"EDGAR {j.category} code")

    country = stated
    if country is None and incorporation_code:
        inc = decode(incorporation_code)
        # Canada only: the observed ambiguity is Canada-vs-Mexico-vs-Netherlands
        # and admitting any country here would re-open it.
        if inc.ok and inc.country == CANADA:
            country = CANADA

    if country:
        entry = _load_postal().get((code, country))
        if entry:
            return Office(code, entry["name"], country, True,
                          f"{country} postal abbreviation, country confirmed")
        if j.ok and j.category == "country":
            # "Utrecht, NL" - the filer put a country in the state field.
            return Office(code, None, j.country, True,
                          "country code tagged in the state field")
        iso_name = country_from_iso(code)
        if iso_name and iso_name == country:
            return Office(code, None, country, True,
                          "country repeated in the state field")
        return Office(code, None, country, True,
                      f"country known; {code!r} not a known {country} subdivision")

    # No country evidence.  A postal abbreviation that collides with nothing
    # else can still be read -- 'ON' is not an EDGAR code, not an ISO country,
    # and not a US state, so Ontario is the only thing it can denote.  But
    # nothing corroborates it, so it is flagged for a lower confidence.
    for (pcode, pcountry), entry in _load_postal().items():
        if pcode == code and not entry["needs_context"]:
            return Office(code, entry["name"], pcountry, True,
                          f"{pcountry} postal abbreviation, uncorroborated "
                          f"(no country tagged and issuer is not {pcountry})",
                          corroborated=False)

    if j.ok and j.category == "country":
        return Office(code, None, j.country, True,
                      "country code tagged in the state field")
    return Office(code, None, None, False,
                  f"{code!r} is not an EDGAR code and no country is known")


def collisions() -> dict[str, str]:
    """The 24 codes an ISO reader would mis-resolve, code -> ISO country."""
    return dict(_load_collisions())


def table_size() -> dict:
    """Row counts, for a startup sanity check."""
    codes = _load_codes()
    cats: dict[str, int] = {}
    for r in codes.values():
        c = (r.get("category") or "").strip().lower()
        cats[c] = cats.get(c, 0) + 1
    return {"codes": len(codes), "by_category": cats,
            "collisions": len(_load_collisions()),
            "aliases": len(_load_aliases())}
