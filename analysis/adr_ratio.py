"""
adr_ratio.py — derive ADR/ADS ratios (underlying shares per ADS).

Foreign private issuers list American Depositary Shares (ADSs) on US exchanges;
each ADS represents a fixed number of underlying home-market shares (the "ADR
ratio").  The XBRL cover page has **no structured field** for this ratio — it
lives as free text, usually inside the registered security title itself:

    "American Depositary Shares, each representing two (2) ordinary shares
     with a nominal value of EUR0.07 each"                          (SHEL -> 2)
    "American Depositary Shares (Each Depositary Share Represents One Share
     of Common Stock)"                                              (SONY -> 1)
    "American depositary shares, each representing one-half of one ordinary
     share"                                                         (-> 0.5)

Extraction is tiered, cheapest first, and every result carries its method and
evidence so an analyst can audit it:

    1. **regex on the security title**   (method='regex-title',  confidence high)
    2. **regex on the filing text**      (method='regex-text',   confidence high)
       - for titles that just say "American Depositary Shares"; annual reports
         state the ratio in prose ("Each ADS represents five equity shares").
    3. **local LLM on candidate text**   (method='llm',          confidence medium)
       - only sees the same sentences, never answers from memory.

Usage
-----
    from analysis.adr_ratio import extract_adr_ratio, adr_ratios_for_securities

    r = extract_adr_ratio(title="American Depositary Shares, each representing "
                                "eight Ordinary Shares")
    r.shares_per_ads          # 8.0
    r.ratio_display           # '1 ADS : 8 ordinary shares'

    # over a cover-page security_facts frame (one row per ADS line kept):
    df = adr_ratios_for_securities(security_df)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from .ollama_client import generate_json, is_available, OllamaUnavailable

# ── Result model ──────────────────────────────────────────────────────────────

@dataclass
class ADRRatio:
    """One derived ADR ratio with its provenance."""
    is_ads: bool
    shares_per_ads: Optional[float] = None   # underlying shares in ONE ADS
    underlying: Optional[str] = None         # e.g. "ordinary shares"
    method: Optional[str] = None             # regex-title | regex-text | llm
    confidence: Optional[str] = None         # high | medium | low
    evidence: Optional[str] = None           # exact text the ratio came from
    notes: list[str] = field(default_factory=list)

    @property
    def ads_per_share(self) -> Optional[float]:
        if self.shares_per_ads:
            return 1.0 / self.shares_per_ads
        return None

    @property
    def ratio_display(self) -> Optional[str]:
        """Human form, ADS side normalised to 1 where possible."""
        if not self.is_ads or self.shares_per_ads is None:
            return None
        n = self.shares_per_ads
        under = self.underlying or "underlying shares"
        if n >= 1:
            num = int(n) if float(n).is_integer() else n
            return f"1 ADS : {num} {under}"
        # fractional -> N ADS per share reads better (e.g. 0.5 -> 2 ADS : 1)
        inv = 1.0 / n
        if float(inv).is_integer():
            return f"{int(inv)} ADS : 1 {under.rstrip('s')}"
        return f"1 ADS : {n} {under}"


# ── Quantity parsing (words, digits, fractions, parentheticals) ───────────────

_UNITS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
          "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
          "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
          "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
          "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
          "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
          "hundred": 100, "an": 1, "a": 1}

_DENOMS = {"half": 2, "halves": 2, "third": 3, "thirds": 3,
           "quarter": 4, "quarters": 4, "fourth": 4, "fourths": 4,
           "fifth": 5, "fifths": 5, "sixth": 6, "sixths": 6,
           "seventh": 7, "sevenths": 7, "eighth": 8, "eighths": 8,
           "ninth": 9, "ninths": 9, "tenth": 10, "tenths": 10,
           "twelfth": 12, "twelfths": 12, "fifteenth": 15,
           "twentieth": 20, "twentieths": 20, "twenty-fifth": 25,
           "fiftieth": 50, "hundredth": 100, "hundredths": 100,
           "thousandth": 1000, "ten-thousandth": 10000}

_NUM_TOKEN = re.compile(r"\d+(?:[.,]\d+)?")


def _word_fraction(phrase: str) -> Optional[float]:
    """'one-half' -> 0.5, 'three-fourths' -> 0.75, 'one-tenth' -> 0.1 ..."""
    m = re.match(r"\s*([a-z]+)[-\s]+([a-z-]+)", phrase)
    if not m:
        return None
    num = _UNITS.get(m.group(1))
    den = _DENOMS.get(m.group(2))
    if num and den:
        return num / den
    return None


def parse_quantity(phrase: str) -> Optional[float]:
    """Parse the leading quantity of a ratio phrase.

    Handles: '2', '0.5', 'two (2)', 'one-half', 'one half of one', 'ten',
    '1/2', 'twenty-five (25)' and similar cover-title constructions.
    """
    p = phrase.strip().lower().replace(" ", " ")
    p = re.sub(r"^(?:the\s+right\s+to\s+receive\s+|an\s+interest\s+in\s+)", "", p)

    # explicit fraction 1/2
    m = re.match(r"\s*(\d+)\s*/\s*(\d+)", p)
    if m and int(m.group(2)):
        return int(m.group(1)) / int(m.group(2))

    # leading digits (possibly followed by a confirming parenthetical)
    m = re.match(r"\s*(\d+(?:[.,]\d+)?)", p)
    if m:
        return float(m.group(1).replace(",", ""))

    # word fraction: 'one-half [of one/of a]', 'three-quarters', ...
    frac = _word_fraction(p)
    if frac is not None:
        return frac

    # word number, optionally 'twenty five' or with parenthetical 'two (2)'
    words = re.findall(r"[a-z-]+|\(\s*\d+(?:\.\d+)?\s*\)", p)
    total, seen = 0.0, False
    for w in words:
        if w.startswith("("):                       # parenthetical confirms
            val = float(_NUM_TOKEN.search(w).group().replace(",", ""))
            if seen and total != val:
                return val                          # trust the numeral
            return val if not seen else total
        w2 = w.replace("-", " ").split()
        matched = False
        for part in w2:
            if part in _UNITS:
                # 'twenty five' style addition ('hundred' multiplication is
                # absent from real cover titles, so simple addition suffices)
                total += _UNITS[part]
                seen, matched = True, True
        if not matched:
            break                                    # quantity phrase ended
    return total if seen else None


# ── Regex layer ───────────────────────────────────────────────────────────────

_ADS_HINT = re.compile(r"american\s+depositar[yi]", re.I)

# Inline-XBRL cover text sometimes drops the space between text runs
# ("ordinary shareswith a nominal value" — Shell).  Re-insert a space before
# glued function words; \b keeps words like "notwithstanding" intact.
_GLUE_RE = re.compile(
    r"(?<=[a-z])(?=(?:with|having|representing|represents|each|nominal|par)\b)")


def _normalize(text: str) -> str:
    return _GLUE_RE.sub(" ", text)


# "... each|every|Shares ... represent(s|ing) <phrase> share(s) [of <type>]"
# NB the qty phrase forbids . ; : so it cannot cross a sentence boundary, but
# a dot BETWEEN digits is allowed so decimals like "0.25" still parse.
# The anchor includes bare "Shares"/"ADSs" because some titles skip "each"
# ("American Depositary Shares representing two ordinary shares" — Shell).
_SAFE = r"(?:[^.;:]|(?<=\d)\.(?=\d))"
_REP_RE = re.compile(
    r"(?:each|every|per|shares?|adss?|receipts?\)?)" + _SAFE + r"{0,80}?"
    r"represent(?:ing|s)?\s+"
    r"(?:the\s+right\s+to\s+receive\s+|an?\s+interest\s+in\s+)?"
    r"(?P<qty>" + _SAFE + r"{1,90}?)\s*"
    r"(?P<share>shares?|units?)\b"
    r"(?:\s+of\s+(?P<of>[^.;:,(]{1,60}))?",
    re.I)

# Prose form for FILING TEXT — stricter than the title form: the thing doing
# the representing must explicitly be an ADS/ADR ("Each ADS represents six
# ordinary shares").  The loose title anchor ("Shares ... representing") is
# safe on a 100-char security title but false-positives on 200 pages of prose
# (e.g. "percentage of shares held by each individual ... represents").
_PROSE_RE = re.compile(
    r"(?:each|every|one|1|an)\s+"
    r"(?:american\s+depositar[yi]\s+(?:share|receipt)s?|adss?|adrs?)\b"
    + _SAFE + r"{0,60}?"
    r"represent(?:ing|s)?\s+"
    r"(?:the\s+right\s+to\s+receive\s+|an?\s+interest\s+in\s+)?"
    r"(?P<qty>" + _SAFE + r"{1,90}?)\s*"
    r"(?P<share>shares?|units?)\b"
    r"(?:\s+of\s+(?P<of>[^.;:,(]{1,60}))?",
    re.I)

# subject-first variant: "American Depositary Receipts ..., each representing
# two Common Shares" (Petrobras).  Still anchored on an explicit ADS token.
_PROSE2_RE = re.compile(
    r"(?:american\s+depositar[yi]\s+(?:share|receipt)s?|adss?|adrs?)\b"
    r"[^.;:]{0,60}?,?\s*each(?:\s+of\s+which)?\s+"
    r"represent(?:ing|s)?\s+"
    r"(?:the\s+right\s+to\s+receive\s+|an?\s+interest\s+in\s+)?"
    r"(?P<qty>" + _SAFE + r"{1,90}?)\s*"
    r"(?P<share>shares?|units?)\b"
    r"(?:\s+of\s+(?P<of>[^.;:,(]{1,60}))?",
    re.I)

# reverse form: "<N> American Depositary Shares represent <M> ordinary shares"
_REV_RE = re.compile(
    r"(?P<n>\d+(?:\.\d+)?|[a-z-]+)\s+"
    r"(?:american\s+depositar[yi]\s+shares?|adss?)"
    r"[^.;:]{0,40}?represent(?:ing|s)?\s+"
    r"(?P<qty>[^.;:]{1,60}?)\s*(?:shares?|units?)\b",
    re.I)

_UNDERLYING_NOISE = re.compile(
    r"\b(?:with|having|par|nominal|value|each|per|of\s+the\s+company|issued"
    r"|fully|paid|credited|no\s+par)\b.*$", re.I)


def _clean_underlying(qty_phrase: str, of_phrase: str | None) -> Optional[str]:
    """Turn the captured phrases into a short underlying-share label."""
    if of_phrase:                                          # "shares of X"
        label = of_phrase.strip()
    else:
        # Skip the leading quantity tokens ("two", "(2)", "0.5", "one-half",
        # "of", "an") — whatever remains is the share-type phrase.
        toks = qty_phrase.strip().split()
        i = 0
        while i < len(toks):
            t = toks[i].strip("(),").lower()
            if (not t or _NUM_TOKEN.fullmatch(t) or t in _UNITS or t in _DENOMS
                    or t in ("of",) or re.fullmatch(r"\d+/\d+", t)
                    or _word_fraction(t) is not None):
                i += 1
                continue
            break
        rest = " ".join(toks[i:]).strip()
        label = (rest + " shares") if rest else None
    if not label:
        return None
    label = _UNDERLYING_NOISE.sub("", label).strip(" ,;-()")
    label = re.sub(r"\s{2,}", " ", label).lower()
    # drop leftover pure-number junk like "one" that survived
    if not re.search(r"(share|stock|unit|class|ordinary|common|preferred|"
                     r"preference|equity)", label):
        return None
    return label or None


def parse_ratio_text(text: str, prose: bool = False
                     ) -> Optional[tuple[float, Optional[str], str]]:
    """Find '(each ...) represents N <type> shares' in text.

    ``prose=True`` (filing text) uses only the strict pattern where the ADS is
    the subject of "represents"; the loose pattern is title-only.

    Returns (shares_per_ads, underlying, evidence_snippet) or None.
    Tries the forward form first, then the reverse 'N ADSs represent M shares'.
    """
    text = _normalize(text)
    patterns = (_PROSE_RE, _PROSE2_RE) if prose else (_REP_RE, _PROSE_RE)
    for pattern in patterns:
        for m in pattern.finditer(text):
            if "%" in m.group("qty"):
                continue                       # percentages are never ratios
            qty = parse_quantity(m.group("qty"))
            if qty is None or qty <= 0 or qty > 100000:
                continue
            # the phrase must be about depositary shares (near the match)
            window = text[max(0, m.start() - 120):m.end() + 20]
            if not (_ADS_HINT.search(window) or re.search(r"\bADS|\bADR", window)):
                continue
            underlying = _clean_underlying(m.group("qty"), m.group("of"))
            snippet = re.sub(r"\s+", " ", m.group(0)).strip()
            return qty, underlying, snippet

    for m in _REV_RE.finditer(text):
        n_raw = m.group("n")
        n = parse_quantity(n_raw)
        qty = parse_quantity(m.group("qty"))
        if n and qty and n > 0:
            snippet = re.sub(r"\s+", " ", m.group(0)).strip()
            return qty / n, None, snippet
    return None


def parse_adr_title(title: str | None) -> ADRRatio:
    """Regex-only parse of one registered security title."""
    if not title or not _ADS_HINT.search(str(title)):
        return ADRRatio(is_ads=False)
    hit = parse_ratio_text(str(title))
    if hit:
        qty, underlying, snippet = hit
        return ADRRatio(True, qty, underlying, "regex-title", "high", snippet)
    return ADRRatio(True, None, None, None, "low", str(title),
                    notes=["title does not state the ratio"])


# ── Filing-text layer ─────────────────────────────────────────────────────────

_SENT_HINT = re.compile(
    r"(american\s+depositar[yi]|each\s+ads\b|per\s+ads\b|\bADSs?\b)", re.I)


def find_ratio_sentences(text: str, limit: int = 12) -> list[str]:
    """Sentences in filing text likely to state the ADS ratio.

    Splits on sentence enders and newlines (the HTML-to-text step inserts
    newlines at block boundaries, so headings/tables become their own chunks).
    Over-long runs are windowed around 'represent' instead of being dropped.
    """
    out = []

    def _add(chunk: str) -> bool:
        out.append(re.sub(r"\s+", " ", chunk).strip())
        return len(out) >= limit

    for raw in re.split(r"(?<=[.;])\s+|\n+", text):
        if len(raw) < 20:
            continue
        if not (_SENT_HINT.search(raw) and re.search(r"represent", raw, re.I)):
            continue
        if len(raw) <= 600:
            if _add(raw):
                break
        else:  # long run (e.g. cover block without periods) — window it
            for m in re.finditer(r"represent", raw, re.I):
                window = raw[max(0, m.start() - 300):m.end() + 300]
                if _SENT_HINT.search(window) and _add(window):
                    return out
    return out


def ratio_from_filing_text(text: str) -> Optional[ADRRatio]:
    """Scan filing prose for the ratio ('Each ADS represents five ...')."""
    for sent in find_ratio_sentences(text):
        hit = parse_ratio_text(sent, prose=True)
        if hit:
            qty, underlying, _ = hit
            return ADRRatio(True, qty, underlying, "regex-text", "high", sent)
    return None


# ── LLM layer ─────────────────────────────────────────────────────────────────

_LLM_SCHEMA = {
    "type": "object",
    "properties": {
        "is_ads": {"type": "boolean"},
        "shares_per_ads": {"type": ["number", "null"]},
        "underlying": {"type": ["string", "null"]},
    },
    "required": ["is_ads", "shares_per_ads"],
}

_LLM_SYSTEM = (
    "You extract American Depositary Share (ADS/ADR) ratios from SEC filing "
    "text. Answer ONLY from the text given. shares_per_ads is how many "
    "underlying shares ONE ADS represents (fractions allowed, e.g. 0.5). "
    "If the text does not state a ratio, use null.")


def ratio_via_llm(evidence: str, model: str | None = None) -> Optional[ADRRatio]:
    """Ask the local model to read the evidence text; None when unavailable."""
    if not is_available():
        return None
    try:
        out = generate_json(
            "Text from an SEC filing about a listed security:\n\n"
            f"\"{evidence}\"\n\n"
            "If this describes American Depositary Shares/Receipts, extract "
            "how many underlying shares ONE ADS represents and the underlying "
            "share type.",
            _LLM_SCHEMA, model=model, system=_LLM_SYSTEM)
    except (OllamaUnavailable, ValueError):
        return None
    if not out.get("is_ads"):
        return None
    qty = out.get("shares_per_ads")
    if qty is None or not isinstance(qty, (int, float)) or qty <= 0:
        return None
    # Plausibility cap: real ADS ratios run from tiny fractions to ~100
    # underlying shares.  Values beyond that are the model misreading holder
    # counts or share totals, not a ratio.
    if not (0.0001 <= float(qty) <= 100):
        return None
    return ADRRatio(True, float(qty), out.get("underlying"),
                    "llm", "medium", evidence)


# Sentences that talk about holder counts / ownership percentages routinely
# contain "ADSs, representing 18.3% of ..." — never a ratio.  Filter them out
# of the LLM candidate pool.
_STATS_RE = re.compile(
    r"(?i)holders?\s+of\b|represent\w*\s+(?:approximately\s+)?[\d.,]+\s*%")


# ── Public tiered entry points ────────────────────────────────────────────────

def extract_adr_ratio(
    title: str | None = None,
    filing_text: str | None = None,
    use_llm: bool = True,
    model: str | None = None,
) -> ADRRatio:
    """Derive the ADR ratio from whatever sources are supplied, cheapest first.

    ``title`` is the cover-page security title; ``filing_text`` is optional
    plain text of the annual report (used when the title lacks the ratio).
    """
    res = parse_adr_title(title) if title else ADRRatio(is_ads=False)
    if res.is_ads and res.shares_per_ads is not None:
        return res

    ads_seen = res.is_ads
    if filing_text:
        text_hit = ratio_from_filing_text(filing_text)
        if text_hit:
            return text_hit
        if _ADS_HINT.search(filing_text):
            ads_seen = True

    if use_llm and ads_seen:
        candidates = ([str(title)] if title else []) + \
                     (find_ratio_sentences(filing_text) if filing_text else [])
        candidates = [c for c in candidates if not _STATS_RE.search(c)]
        for ev in candidates[:5]:
            llm_hit = ratio_via_llm(ev, model=model)
            if llm_hit:
                return llm_hit

    if ads_seen:
        res.is_ads = True
        if not res.notes:
            res.notes = ["ADS detected but no ratio found in available text"]
    return res


def adr_ratios_for_securities(
    security_df: pd.DataFrame,
    filing_texts: dict | None = None,
    use_llm: bool = True,
    model: str | None = None,
) -> pd.DataFrame:
    """Derive ratios for every ADS line in a ``security_facts`` frame.

    ``filing_texts`` optionally maps accession_number -> plain filing text for
    the text-layer fallback.  Non-ADS securities are dropped; returns one row
    per ADS with ratio columns appended.
    """
    if security_df is None or security_df.empty:
        return pd.DataFrame()
    rows = []
    for _, sec in security_df.iterrows():
        title = sec.get("security_title")
        if title is None or (isinstance(title, float) and pd.isna(title)):
            title = None
        # An ADS line is recognised by its title OR by its axis member name
        # (some filers tag the member AmericanDepositaryShares with a bare
        # or missing title).
        is_ads_row = bool(title and _ADS_HINT.search(str(title))) or \
            "americandepositary" in str(sec.get("security_class") or "").lower()
        if not is_ads_row:
            continue
        text = (filing_texts or {}).get(sec.get("accession_number"))
        r = extract_adr_ratio(title=title, filing_text=text,
                              use_llm=use_llm, model=model)
        rows.append({
            **{k: sec.get(k) for k in ("cik", "entity_name", "ticker",
                                       "form_type", "filing_date",
                                       "accession_number", "security_class",
                                       "trading_symbol", "exchange") if k in sec},
            "security_title":  title,
            "shares_per_ads":  r.shares_per_ads,
            "ratio_display":   r.ratio_display,
            "underlying":      r.underlying,
            "method":          r.method,
            "confidence":      r.confidence,
            "evidence":        r.evidence,
        })
    return pd.DataFrame(rows)
