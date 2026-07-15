"""
icb_classify.py — assign ICB classifications (FTSE Russell, 2019+ structure).

The ICB hierarchy (11 industries -> 20 supersectors -> 45 sectors -> 173
subsectors) lives in ``data/icb_taxonomy.csv`` (regenerate with
``data/build_icb_taxonomy.py``).  Classification combines two signals:

1.  **SIC prior** (deterministic).  EDGAR reports every registrant's SIC code;
    ``data/sic_icb_prior.csv`` maps SIC ranges to the expected ICB *industry*.
    Coarse but honest — it can't tell a brewer from a tobacco firm, and SIC
    itself is often stale, which is exactly why the LLM layer exists.
2.  **Local LLM** (fine-grained).  The model reads the company's own business
    description (Item 1 of the 10-K / Item 4 of the 20-F) and picks a sector
    from the 45 valid choices — the JSON-schema enum makes an invalid answer
    impossible — then a subsector from that sector's children.

Confidence comes from agreement: LLM sector inside the SIC-prior industry ->
'high'; disagreement -> 'review' (an analyst should look); no LLM -> the SIC
prior alone at industry level, 'low'.

Usage
-----
    from analysis.icb_classify import classify_icb, business_description

    desc = business_description(annual_text, form_type="10-K")
    c = classify_icb(name="The Coca-Cola Company",
                     sic="2086", sic_desc="Bottled & Canned Soft Drinks",
                     description=desc)
    c.industry, c.sector, c.subsector, c.confidence
"""

from __future__ import annotations

import csv
import os
import re
from dataclasses import dataclass, field
from typing import Optional

from .ollama_client import (generate_json, is_available, pick_model,
                            OllamaUnavailable)

_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# Classification is a judgement call, not a literal extraction — bigger models
# earn their keep here (QA: gemma2:2b misplaced Apple in Software and Nucor in
# Construction; both 7-8B models got them right).  Extraction tasks elsewhere
# still default to the fast small model.
ICB_PREFERENCE = (
    "qwen2.5:14b-instruct", "qwen2.5:7b-instruct", "llama3.1:8b-instruct",
    "llama3.1:8b", "mistral:7b-instruct",
    "fluffy/l3-8b-stheno-v3.2", "dolphin-mistral", "gemma2:2b", "phi3:mini",
)

# ── Taxonomy ──────────────────────────────────────────────────────────────────

_taxonomy_cache: list[dict] | None = None


def load_taxonomy() -> list[dict]:
    """The full ICB table, one dict per subsector (cached)."""
    global _taxonomy_cache
    if _taxonomy_cache is None:
        with open(os.path.join(_DATA_DIR, "icb_taxonomy.csv"),
                  encoding="utf-8") as fh:
            _taxonomy_cache = list(csv.DictReader(fh))
    return _taxonomy_cache


def sectors() -> list[dict]:
    """The 45 sectors: sector_code, sector, supersector, industry(-_code)."""
    seen, out = set(), []
    for row in load_taxonomy():
        if row["sector_code"] not in seen:
            seen.add(row["sector_code"])
            out.append({k: row[k] for k in
                        ("industry_code", "industry", "supersector_code",
                         "supersector", "sector_code", "sector")})
    return out


def subsectors_of(sector_code: str) -> list[dict]:
    return [r for r in load_taxonomy() if r["sector_code"] == sector_code]


# ── SIC prior ─────────────────────────────────────────────────────────────────

_sic_map_cache: list[tuple[int, int, str, str, str]] | None = None


def _sic_map() -> list[tuple[int, int, str, str, str]]:
    global _sic_map_cache
    if _sic_map_cache is None:
        rows = []
        with open(os.path.join(_DATA_DIR, "sic_icb_prior.csv"),
                  encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                rows.append((int(r["sic_start"]), int(r["sic_end"]),
                             r["industry_code"], r.get("sector_code") or "",
                             r["note"]))
        _sic_map_cache = rows
    return _sic_map_cache


def sic_prior(sic: str | int | None) -> Optional[dict]:
    """Expected ICB industry for a SIC code (narrowest matching range wins).

    A few SIC codes are definitional and carry a sector_code hint too
    (6798 = REITs).  The hint is used as a same-industry tiebreaker, never to
    override a cross-industry LLM call (mortgage REITs belong to Financials
    in ICB and should surface as 'review', not be silently forced).
    """
    if sic in (None, ""):
        return None
    try:
        code = int(str(sic).strip())
    except ValueError:
        return None
    hits = [(end - start, start, end, ind, sec_hint, note)
            for start, end, ind, sec_hint, note in _sic_map()
            if start <= code <= end]
    if not hits:
        return None
    _, start, end, ind, sec_hint, note = min(hits)
    industry = next((r["industry"] for r in load_taxonomy()
                     if r["industry_code"] == ind), None)
    return {"industry_code": ind, "industry": industry,
            "sector_code_hint": sec_hint or None,
            "sic_range": f"{start:04d}-{end:04d}", "note": note}


# ── Business description from filing text ─────────────────────────────────────

def _find_span(text_low: str, start_pats: list[str], end_pats: list[str],
               min_len: int = 400) -> Optional[tuple[int, int]]:
    for sp in start_pats:
        for m in re.finditer(sp, text_low):
            start = m.end()
            ends = [e.start() for ep in end_pats
                    for e in [re.search(ep, text_low[start:start + 400000])] if e]
            end = start + (min(ends) if ends else 40000)
            if end - start >= min_len:
                return start, end
    return None


def business_description(text: str, form_type: str = "10-K",
                         max_chars: int = 4000) -> str:
    """The company's own description of its business, from the annual text.

    10-K: Item 1 (Business) up to Item 1A.  20-F: Item 4 (Information on the
    Company) up to Item 5.  Falls back to an early slice of the document
    (skipping the cover/TOC region) when the item structure isn't found.
    """
    low = text.lower()
    if form_type.startswith("20-F"):
        span = _find_span(low,
                          [r"item\s*4[.:]?\s*informat", r"business\s+overview"],
                          [r"item\s*4a[.:]", r"item\s*5[.:]?\s*operat"])
    else:
        span = _find_span(low,
                          [r"item\s*1[.:]?\s*business", r"item\s*1[.:]?\s*\n"],
                          [r"item\s*1a[.:]?\s*risk", r"item\s*2[.:]?\s*propert"])
    if span:
        chunk = text[span[0]:span[1]]
    else:
        chunk = text[1500:1500 + max_chars * 3]     # skip cover/TOC region
    chunk = re.sub(r"\s+", " ", chunk).strip()
    return chunk[:max_chars]


# ── Classification ────────────────────────────────────────────────────────────

@dataclass
class ICBClassification:
    industry_code: Optional[str] = None
    industry: Optional[str] = None
    supersector_code: Optional[str] = None
    supersector: Optional[str] = None
    sector_code: Optional[str] = None
    sector: Optional[str] = None
    subsector_code: Optional[str] = None
    subsector: Optional[str] = None
    method: Optional[str] = None          # llm+sic | llm | sic
    confidence: Optional[str] = None      # high | medium | review | low
    rationale: Optional[str] = None
    sic: Optional[str] = None
    sic_desc: Optional[str] = None
    sic_industry: Optional[str] = None    # the prior's industry (for display)
    evidence: Optional[str] = None        # description excerpt used

    @property
    def path_display(self) -> Optional[str]:
        parts = [p for p in (self.industry, self.supersector, self.sector,
                             self.subsector) if p]
        # collapse consecutive duplicates (e.g. Energy > Energy > Oil, Gas...)
        dedup = [p for i, p in enumerate(parts) if i == 0 or p != parts[i - 1]]
        return " > ".join(dedup) if dedup else None


_SECTOR_SYSTEM = (
    "You classify companies into the Industry Classification Benchmark (ICB). "
    "Pick the single sector that best matches the company's PRIMARY revenue "
    "source, based only on the provided description.")


def _llm_sector(name: str, sic_desc: str | None, description: str,
                model: str | None) -> Optional[dict]:
    """Stage 1: pick one of the 45 sectors.

    The enum is the sector NAMES, not the numeric codes: small local models
    reliably emit the semantically right name under constrained decoding but
    frequently pick an adjacent wrong 6-digit code (QA: a model whose stated
    rationale was "Beverages" returned the code for Tobacco, one line away).
    """
    secs = sectors()
    by_name = {s["sector"]: s for s in secs}   # 45 names, all unique
    listing = "\n".join(f"- {s['sector']}  [{s['industry']}]" for s in secs)
    schema = {"type": "object",
              "properties": {
                  "sector": {"type": "string", "enum": list(by_name)},
                  "reason": {"type": "string"}},
              "required": ["sector"]}
    prompt = (f"Company: {name}\n"
              + (f"SEC SIC description: {sic_desc}\n" if sic_desc else "")
              + f"\nBusiness description (from its own annual report):\n"
                f"\"{description}\"\n\n"
                f"ICB sectors (name [industry]):\n{listing}\n\n"
                "Choose the single best sector and briefly say why.")
    try:
        out = generate_json(prompt, schema, model=model, system=_SECTOR_SYSTEM)
    except (OllamaUnavailable, ValueError):
        return None
    hit = by_name.get(out.get("sector"))
    if hit:
        hit = dict(hit)
        hit["reason"] = out.get("reason")
    return hit


def _llm_subsector(sector_code: str, name: str, description: str,
                   model: str | None) -> Optional[dict]:
    """Stage 2: pick the subsector within the chosen sector."""
    subs = subsectors_of(sector_code)
    if len(subs) == 1:
        return subs[0]
    by_name = {s["subsector"]: s for s in subs}   # names, not codes (see above)
    schema = {"type": "object",
              "properties": {
                  "subsector": {"type": "string", "enum": list(by_name)}},
              "required": ["subsector"]}
    listing = "\n".join(f"- {s['subsector']}" for s in subs)
    prompt = (f"Company: {name}\n\nBusiness description:\n\"{description}\"\n\n"
              f"Subsectors of ICB sector {subs[0]['sector']}:\n{listing}\n\n"
              "Choose the single best subsector.")
    try:
        out = generate_json(prompt, schema, model=model, system=_SECTOR_SYSTEM)
    except (OllamaUnavailable, ValueError):
        return None
    return by_name.get(out.get("subsector"))


def classify_icb(
    name: str,
    sic: str | int | None = None,
    sic_desc: str | None = None,
    description: str | None = None,
    use_llm: bool = True,
    model: str | None = None,
) -> ICBClassification:
    """Classify one company into the ICB hierarchy.

    ``description`` should come from ``business_description(annual_text)``;
    without it the LLM still runs on name + SIC description alone (weaker).
    """
    prior = sic_prior(sic)
    out = ICBClassification(sic=str(sic) if sic not in (None, "") else None,
                            sic_desc=sic_desc,
                            sic_industry=prior["industry"] if prior else None,
                            evidence=(description or "")[:600] or None)

    sec = None
    if use_llm and is_available():
        model = model or pick_model(ICB_PREFERENCE)
        sec = _llm_sector(name, sic_desc, description or "(not available)",
                          model)
    if sec:
        # same-industry sector-hint tiebreak (e.g. SIC 6798 -> REITs sector)
        hint = prior.get("sector_code_hint") if prior else None
        if (hint and hint != sec["sector_code"]
                and prior["industry_code"] == sec["industry_code"]):
            corrected = next((s for s in sectors()
                              if s["sector_code"] == hint), None)
            if corrected:
                corrected = dict(corrected)
                corrected["reason"] = ((sec.get("reason") or "")
                                       + f" [sector corrected to "
                                         f"{corrected['sector']} by "
                                         f"definitional SIC {out.sic}]").strip()
                sec = corrected
        out.industry_code, out.industry = sec["industry_code"], sec["industry"]
        out.supersector_code, out.supersector = (sec["supersector_code"],
                                                 sec["supersector"])
        out.sector_code, out.sector = sec["sector_code"], sec["sector"]
        out.rationale = sec.get("reason")
        sub = _llm_subsector(sec["sector_code"], name,
                             description or "(not available)", model)
        if sub:
            out.subsector_code = sub["subsector_code"]
            out.subsector = sub["subsector"]
        if prior:
            agree = prior["industry_code"] == sec["industry_code"]
            out.method = "llm+sic"
            out.confidence = "high" if agree else "review"
            if not agree:
                out.rationale = ((out.rationale + " | ") if out.rationale
                                 else "") + \
                    f"SIC prior expects {prior['industry']} ({prior['note']})"
        else:
            out.method, out.confidence = "llm", "medium"
        return out

    # LLM unavailable — industry-level prior only
    if prior:
        out.industry_code, out.industry = prior["industry_code"], prior["industry"]
        out.method, out.confidence = "sic", "low"
        out.rationale = prior["note"]
    return out
