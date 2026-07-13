"""
codes.py — EDGAR State / Country code tables (NOT ISO — this is the whole point).

The mappings are DATA, not code: they load from CSVs under ``country_assignment/
data/`` so they can be refreshed/extended without touching any module.

  * ``edgar_state_country_codes.csv`` — the official SEC EDGAR "State and Country
    Codes" list (code, name, category ∈ {state, province, country}), scraped from
    https://www.sec.gov/submit-filings/filer-support-resources/edgar-state-country-codes
    Rebuild it any time with ``python methods/country_assignment/data/build_edgar_codes.py``.
  * ``state_country_collisions.csv`` — the curated ambiguity map: EDGAR *state*
    codes that a naive ISO-3166 read would turn into a *different* country
    (CA→Canada, DE→Germany, IL→Israel, KY→Cayman Islands, …).

In EDGAR's scheme ``CA``=California, ``DE``=Delaware, ``IL``=Illinois,
``A1``=British Columbia, ``2M``=Germany, ``E9``=Cayman Islands, ``K3``=Hong Kong,
``L2``=Ireland, ``X0``=United Kingdom, ``Z4``=Canada (federal), ``F5``=Taiwan.
"""

from __future__ import annotations

import csv
import os
import re
import warnings

__all__ = [
    "US_STATES", "CA_PROVINCES", "EDGAR_COUNTRY", "ISO_COLLISION",
    "decode_code", "country_of", "text_to_country", "reload_tables",
]

_DATA_DIR       = os.path.join(os.path.dirname(__file__), "data")
_CODES_CSV      = os.path.join(_DATA_DIR, "edgar_state_country_codes.csv")
_COLLISIONS_CSV = os.path.join(_DATA_DIR, "state_country_collisions.csv")
_ALIASES_CSV    = os.path.join(_DATA_DIR, "jurisdiction_aliases.csv")


def _load_codes(path: str = _CODES_CSV) -> tuple[dict, dict, dict]:
    """Load the official EDGAR codes CSV → (states, provinces, countries) dicts."""
    states, provinces, countries = {}, {}, {}
    try:
        with open(path, encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                code = (row.get("code") or "").strip().upper()
                name = (row.get("name") or "").strip()
                cat  = (row.get("category") or "").strip().lower()
                if not code:
                    continue
                if cat == "state":
                    states[code] = name
                elif cat == "province":
                    provinces[code] = name
                else:
                    countries[code] = name
    except FileNotFoundError:
        warnings.warn(f"EDGAR code table missing: {path} — codes won't decode. "
                      "Rebuild it with data/build_edgar_codes.py.")
    return states, provinces, countries


def _load_collisions(path: str = _COLLISIONS_CSV) -> dict:
    """Load the curated ISO-collision CSV → {edgar_state_code: iso_country}."""
    out: dict = {}
    try:
        with open(path, encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                code = (row.get("code") or "").strip().upper()
                iso  = (row.get("iso_country") or "").strip()
                if code and iso:
                    out[code] = iso
    except FileNotFoundError:
        warnings.warn(f"Collision table missing: {path}")
    return out


def _load_aliases(path: str = _ALIASES_CSV) -> dict:
    """Load spelled-jurisdiction → country aliases (England and Wales → UK, …)."""
    out: dict = {}
    try:
        with open(path, encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                text = (row.get("text") or "").strip().lower()
                country = (row.get("country") or "").strip()
                if text and country:
                    out[text] = country
    except FileNotFoundError:
        pass
    return out


# Loaded once at import; call reload_tables() after editing the CSVs.
US_STATES, CA_PROVINCES, EDGAR_COUNTRY = _load_codes()
ISO_COLLISION = _load_collisions()
_ALIASES = _load_aliases()
# name(lower) → country, longest-first, for resolving free-text jurisdictions.
_NAME_TO_COUNTRY: list = []


def _rebuild_name_index() -> None:
    global _NAME_TO_COUNTRY
    pairs = []
    for name in US_STATES.values():
        pairs.append((name.lower(), "United States"))
    for name in CA_PROVINCES.values():
        pairs.append((name.split(",")[0].lower(), "Canada"))
    for name in EDGAR_COUNTRY.values():
        pairs.append((re.sub(r"\s*\(.*?\)", "", name).strip().lower(),
                      re.sub(r"\s*\(.*?\)", "", name).strip()))
    for text, country in _ALIASES.items():
        pairs.append((text, country))
    # longest names first so "united states" beats "states"
    _NAME_TO_COUNTRY = sorted(set(pairs), key=lambda p: -len(p[0]))


_rebuild_name_index()


def reload_tables() -> None:
    """Re-read the CSVs (e.g. after refreshing them mid-session)."""
    global US_STATES, CA_PROVINCES, EDGAR_COUNTRY, ISO_COLLISION, _ALIASES
    US_STATES, CA_PROVINCES, EDGAR_COUNTRY = _load_codes()
    ISO_COLLISION = _load_collisions()
    _ALIASES = _load_aliases()
    _rebuild_name_index()


def text_to_country(text: str | None) -> str | None:
    """
    Resolve a free-text jurisdiction ("Delaware", "England and Wales", "Cayman
    Islands", "Republic of China") to a country, so two differently-worded names
    can be compared at country level.  Aliases (data file) take priority, then a
    longest-match scan of the EDGAR state/province/country names.  None if unknown.
    """
    if not text:
        return None
    t = re.sub(r"[^a-z ]", " ", text.lower())
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        return None
    if t in _ALIASES:
        return _ALIASES[t]
    for name, country in _NAME_TO_COUNTRY:
        if name and re.search(rf"\b{re.escape(name)}\b", t):
            return country
    return None


def decode_code(code: str | None) -> dict:
    """Decode an EDGAR state/country code → {code, name, kind}.

    kind ∈ {us_state, ca_province, country, unknown, missing}.
    """
    c = (code or "").strip().upper()
    if not c:
        return {"code": None, "name": None, "kind": "missing"}
    if c in US_STATES:
        return {"code": c, "name": US_STATES[c], "kind": "us_state"}
    if c in CA_PROVINCES:
        return {"code": c, "name": CA_PROVINCES[c], "kind": "ca_province"}
    if c in EDGAR_COUNTRY:
        return {"code": c, "name": EDGAR_COUNTRY[c], "kind": "country"}
    return {"code": c, "name": None, "kind": "unknown"}


def country_of(code: str | None) -> str | None:
    """Country implied by an EDGAR state/country code (for address comparison)."""
    d = decode_code(code)
    if d["kind"] == "us_state":          # includes DC + US territories
        return "United States"
    if d["kind"] == "ca_province":
        return "Canada"
    if d["kind"] == "country":
        if not d["name"]:
            return None
        # normalise "Canada (Federal Level)" → "Canada" so it matches a province
        return re.sub(r"\s*\(.*?\)", "", d["name"]).strip()
    return None
