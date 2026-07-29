"""
tests_jurisdiction.py — the edge-case suite for Level 4 incorporation and
principal-executive-office resolution.

Run it::

    python -m finalized.tests_jurisdiction              # offline logic only
    python -m finalized.tests_jurisdiction --db PATH    # + real-data checks

Why these cases
---------------
Every case here is one that has actually gone wrong, or that would go wrong
under an obvious-looking implementation.  They fall into six groups:

1. **ISO collisions.**  The whole reason this layer exists.  ``CA`` is
   California and ``DE`` is Delaware; any library that reads them as Canada and
   Germany produces confident nonsense, and 24 codes behave this way.
2. **Grain.**  ``A1`` (British Columbia) and ``Z4`` (Canada) are the same
   country described at different levels, so they must compare as compatible,
   while ``DE`` vs ``TX`` — two US states — never can be.
3. **Offshore incorporation.**  ``E9`` Cayman, ``D0`` Bermuda, ``D8`` BVI: the
   company is incorporated nowhere near its office, so incorporation country
   must never be used to infer office country.
4. **Unknown codes.**  Must return "cannot decode", never a guess.
5. **Confidence.**  A conflict must outrank staleness; an undecodable code must
   never be reported as high confidence.
6. **Live data.**  Codes actually present in the mirror must all decode, and
   named companies must resolve to their known real answers.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys

from .jurisdiction import (CANADA, UNITED_STATES, collisions, country_from_iso,
                           country_from_text, country_of, decode,
                           resolve_office, same_country, state_of, table_size)
from .covertext import parse_cover, to_text
from .resolve import compare, grade, grade_shares, normalise


class _Check:
    def __init__(self) -> None:
        self.passed = 0
        self.failed: list[str] = []

    def eq(self, got, want, label: str) -> None:
        if got == want:
            self.passed += 1
        else:
            self.failed.append(f"{label}\n       want {want!r}\n       got  {got!r}")

    def report(self, title: str) -> bool:
        n = self.passed + len(self.failed)
        print(f"\n{title}: {self.passed}/{n} passed")
        for f in self.failed:
            print(f"   FAIL {f}")
        return not self.failed


# ── 1. codes decode to full names ─────────────────────────────────────────────

def test_decoding() -> bool:
    c = _Check()
    # (code, state, country, note)
    CASES = [
        # The collisions.  Left column is what EDGAR means; an ISO reader would
        # say Canada / Germany / Colombia / Indonesia / Albania / Georgia.
        ("CA", "California", UNITED_STATES, "NOT Canada"),
        ("DE", "Delaware", UNITED_STATES, "NOT Germany"),
        ("CO", "Colorado", UNITED_STATES, "NOT Colombia"),
        ("ID", "Idaho", UNITED_STATES, "NOT Indonesia"),
        ("AL", "Alabama", UNITED_STATES, "NOT Albania"),
        ("GA", "Georgia", UNITED_STATES, "NOT the country Georgia"),
        # Plain states.
        ("NV", "Nevada", UNITED_STATES, ""),
        ("MD", "Maryland", UNITED_STATES, ""),
        ("NY", "New York", UNITED_STATES, ""),
        # Canadian grain: province carries a state, federal does not.
        ("A1", "British Columbia", CANADA, "province"),
        ("A6", "Ontario", CANADA, "province"),
        ("Z4", None, CANADA, "federal level -> no state"),
        # Offshore and foreign, country-level so state is None.
        ("E9", None, "Cayman Islands", ""),
        ("D0", None, "Bermuda", ""),
        ("D8", None, "Virgin Islands, British", ""),
        ("X0", None, "United Kingdom", ""),
        ("K3", None, "Hong Kong", ""),
        ("L3", None, "Israel", ""),
        ("F5", None, "Taiwan", ""),
    ]
    for code, state, country, note in CASES:
        j = decode(code)
        c.eq(j.ok, True, f"decode({code!r}) should be known")
        c.eq(j.state, state, f"decode({code!r}).state  {note}")
        c.eq(j.country, country, f"decode({code!r}).country  {note}")

    # Unknown codes: no guessing, and no crash.
    for bad in ("ZZ", "", None, "  ", "99", "XXX"):
        j = decode(bad)
        c.eq(j.ok, False, f"decode({bad!r}) must be ok=False")
        c.eq(j.country, None, f"decode({bad!r}) must not invent a country")

    # Case and whitespace tolerance -- filers are inconsistent.
    c.eq(decode(" de ").state, "Delaware", "decode tolerates spacing/case")
    c.eq(country_of("ca"), UNITED_STATES, "country_of is case-insensitive")
    c.eq(state_of("E9"), None, "a country code has no state")
    return c.report("1. code decoding")


# ── 2. compatibility, not string equality ─────────────────────────────────────

def test_same_country() -> bool:
    c = _Check()
    c.eq(same_country("A1", "Z4"), True, "British Columbia vs Canada federal")
    c.eq(same_country("A6", "A1"), True, "Ontario vs British Columbia")
    c.eq(same_country("DE", "TX"), True, "two US states share a country")
    c.eq(same_country("DE", "E9"), False, "Delaware vs Cayman")
    c.eq(same_country("CA", "Z4"), False,
         "California is NOT Canada -- the collision test")
    # Unknown -> None ("cannot tell"), never False ("differs").
    c.eq(same_country("ZZ", "DE"), None, "unknown code -> cannot tell")
    c.eq(same_country(None, "DE"), None, "missing code -> cannot tell")
    return c.report("2. country compatibility")


# ── 3. free-text aliases ──────────────────────────────────────────────────────

def test_iso_vs_edgar() -> bool:
    """
    The single most damaging confusion in this data: two adjacent cover fields
    use different code systems, and the overlap is silent.

        EntityAddressStateOrProvince  EDGAR   CA=California  DE=Delaware
        EntityAddressCountry          ISO     CA=Canada      DE=Germany
    """
    c = _Check()
    # Same string, opposite answers, depending only on which field it came from.
    for code, edgar_state, iso_country in (
            ("CA", "California", "Canada"),
            ("DE", "Delaware", "Germany"),
            ("CO", "Colorado", "Colombia"),
            ("ID", "Idaho", "Indonesia"),
            ("IL", "Illinois", "Israel"),
            ("IN", "Indiana", "India"),
            ("KY", "Kentucky", "Cayman Islands"),
            ("MD", "Maryland", "Moldova, Republic of"),
            ("MO", "Missouri", "Macao"),
            ("LA", "Louisiana", "Lao People's Democratic Republic"),
            ("VA", "Virginia", "Holy See"),
            ("PA", "Pennsylvania", "Panama")):
        c.eq(decode(code).state, edgar_state,
             f"EDGAR {code} (state field) is {edgar_state}")
        c.eq(country_from_iso(code), iso_country,
             f"ISO {code} (country field) is {iso_country}")
        c.eq(decode(code).country != country_from_iso(code), True,
             f"{code} must resolve differently in the two fields")

    # The country field, decoded correctly, wins over every other hint.
    o = resolve_office("ON", country_text="CA", incorporation_code="DE")
    c.eq((o.state, o.country), ("Ontario", CANADA),
         "IMAX case: country 'CA' is Canada, so ON is Ontario")
    o = resolve_office(None, country_text="DE")
    c.eq(o.country, "Germany", "SmartKem case: country 'DE' is Germany")
    o = resolve_office(None, country_text="KY")
    c.eq(o.country, "Cayman Islands", "country 'KY' is Cayman, not Kentucky")

    # Unknown / empty ISO input must not fall through to a guess.
    for bad in (None, "", "ZZZ", "  "):
        c.eq(country_from_iso(bad), None, f"country_from_iso({bad!r})")
    return c.report("0. ISO vs EDGAR (the silent collision)")


def test_office() -> bool:
    """The state field is not one code system. 'NL' proves it."""
    c = _Check()

    # US: an EDGAR state code is self-sufficient, no country hint needed.
    o = resolve_office("CA")
    c.eq((o.state, o.country, o.resolved),
         ("California", UNITED_STATES, True), "CA -> California, US")
    o = resolve_office("NY", incorporation_code="E9")
    c.eq((o.state, o.country), ("New York", UNITED_STATES),
         "a Cayman-incorporated company with a New York office")

    # The three meanings of NL, disambiguated only by country context.
    o = resolve_office("NL", incorporation_code="A4")   # Newfoundland -> Canada
    c.eq((o.state, o.country, o.resolved),
         ("Newfoundland and Labrador", CANADA, True), "FORTIS: NL in Canada")
    o = resolve_office("NL", incorporation_code="O5")   # Mexico
    c.eq((o.state, o.country), (None, None),
         "FEMSA: NL in Mexico must NOT become Newfoundland")
    c.eq(resolve_office("NL", incorporation_code="O5").resolved, False,
         "FEMSA: declines rather than guessing")
    o = resolve_office("NL", incorporation_code="P7")   # Netherlands
    c.eq(o.state, None, "LAVA: NL is not a Canadian province here")

    # Canada Post abbreviations, admitted only for Canadian companies.
    for code, name, inc in (("ON", "Ontario", "Z4"),
                            ("BC", "British Columbia", "A1"),
                            ("QC", "Quebec", "Z4"),
                            ("AB", "Alberta", "A0")):
        o = resolve_office(code, incorporation_code=inc)
        c.eq((o.state, o.country), (name, CANADA), f"{code} -> {name}")

    # A code unique to Canada Post resolves even with no country evidence --
    # 'ON' is not an EDGAR code, not an ISO country and not a US state -- but
    # it is marked uncorroborated so confidence can be capped.
    o = resolve_office("ON", incorporation_code="DE")
    c.eq((o.state, o.country, o.resolved), ("Ontario", CANADA, True),
         "ON is unique to Canada Post; a Delaware issuer can still sit there")
    c.eq(o.corroborated, False, "...but nothing confirms it")
    c.eq(resolve_office("BC", incorporation_code="NV").corroborated, False,
         "BC likewise unique but uncorroborated")
    c.eq(resolve_office("ON", country_text="CA").corroborated, True,
         "a tagged country corroborates it")
    c.eq(resolve_office("CA").corroborated, True,
         "an EDGAR code is self-corroborating")

    # The ambiguous half must still refuse without context.
    for code in ("NL", "SK", "PE", "NU", "YT"):
        c.eq(resolve_office(code, incorporation_code="DE").state, None,
             f"{code} collides with an ISO country; needs context")

    # An explicitly tagged country outranks everything.
    o = resolve_office("ON", country_text="Canada", incorporation_code="DE")
    c.eq((o.state, o.country), ("Ontario", CANADA),
         "tagged country beats the incorporation hint")

    # No state code at all.
    c.eq(resolve_office(None).resolved, False, "no code, no country")
    o = resolve_office(None, country_text="Cayman Islands")
    c.eq((o.country, o.resolved), ("Cayman Islands", True),
         "country tagged without a state is still resolved")

    # Every decline must say why.
    for args in (("NL", None, "O5"), ("ON", None, "DE"), ("ZZ", None, None)):
        c.eq(bool(resolve_office(*args).reason), True,
             f"resolve_office{args} must explain the decline")
    return c.report("3. office resolution (state field ambiguity)")


def test_text() -> bool:
    c = _Check()
    c.eq(country_from_text("England and Wales"), "United Kingdom", "UK sub")
    c.eq(country_from_text("SCOTLAND"), "United Kingdom", "case-insensitive")
    c.eq(country_from_text("Republic of China"), "Taiwan", "ROC is Taiwan")
    c.eq(country_from_text("People's Republic of China"), "China", "PRC")
    c.eq(country_from_text("Cayman Islands"), "Cayman Islands",
         "a country name is its own alias")
    c.eq(country_from_text("Neverland"), None, "unknown text -> None")
    c.eq(country_from_text(None), None, "None -> None")
    return c.report("3. free-text aliases")


# ── 4. the comparison tiers ───────────────────────────────────────────────────

def test_compare() -> bool:
    c = _Check()
    CASES = [
        ("address_city", "St. George", "ST GEORGE", "cosmetic"),
        ("address_city", "Hicksville", "Hicksville,", "cosmetic"),
        ("address_city", "Fort Lauderdale", "Fort    Lauderdale", "cosmetic"),
        ("address_city", "Boadilla del Monte (Madrid)", "MADRID", "contained"),
        ("address_city", "London", "EDINBURGH, SCOTLAND", "conflict"),
        ("address_city", "New York", "SAINT PAUL", "conflict"),
        ("address_line1", "700 Universe Boulevard", "700 UNIVERSE BLVD",
         "cosmetic"),
        ("address_line1", "855 South Mint Street", "855 S. MINT STREET",
         "cosmetic"),
        ("address_line1", "1 Apple Park Way", "2 Apple Park Way", "conflict"),
        ("incorporation_code", "Z4", "A1", "contained"),
        ("incorporation_code", "DE", "TX", "conflict"),
        ("incorporation_code", "DE", "DE", "agreed"),
        # "St" is Saint in a city and Street in an address line; the street
        # expansion must not leak into city comparison.
        ("address_city", "St. Louis", "Street Louis", "conflict"),
        ("address_state", "DE", "DEL", "conflict"),
        ("address_city", "Denver", None, "xbrl_only"),
        ("address_city", None, "GOLDEN", "header_only"),
        ("address_city", None, None, "missing"),
    ]
    for field, x, h, want in CASES:
        c.eq(compare(field, x, h), want, f"compare({field}, {x!r}, {h!r})")
    c.eq(normalise("Montré-al"), "montre al", "accents and punctuation folded")
    return c.report("4. comparison tiers")


# ── 5. confidence grading ─────────────────────────────────────────────────────

def test_confidence() -> bool:
    c = _Check()
    # (status, decoded_ok, age_days, has_value) -> level
    CASES = [
        (("agreed", True, 10, True), "high"),
        (("cosmetic", True, 10, True), "high"),
        (("contained", True, 10, True), "high"),
        (("xbrl_only", True, 10, True), "medium"),
        (("header_only", True, 10, True), "low"),
        (("conflict", True, 10, True), "low"),
        # A conflict outranks staleness: disagreement is worse than age.
        (("conflict", True, 5000, True), "low"),
        # Stale but agreed -> medium, not high.
        (("agreed", True, 5000, True), "medium"),
        # An undecodable code can never be high, whatever the status.
        (("agreed", False, 10, True), "low"),
        # No value at all.
        ((None, True, None, False), "low"),
        # Unknown age must not crash or upgrade confidence.
        (("xbrl_only", True, None, True), "medium"),
    ]
    for args, want in CASES:
        level, why = grade(*args)
        c.eq(level, want, f"grade{args}")
        c.eq(bool(why), True, f"grade{args} must explain itself")
    return c.report("5. confidence grading")


# ── 6. live data ──────────────────────────────────────────────────────────────

def test_live(db_path: str) -> bool:
    c = _Check()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    have = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "entity_final" not in have:
        print("\n6. live data: SKIPPED (run `cli resolve --final` first)")
        conn.close()
        return True

    # Every incorporation code in the mirror must decode.
    undecoded = [r["v"] for r in conn.execute(
        """SELECT DISTINCT incorporation_code v FROM entity_final
           WHERE incorporation_code IS NOT NULL""")
        if not decode(r["v"]).ok]
    c.eq(undecoded, [], "every incorporation_code in the data decodes")

    # hq_state_code is the value AS FILED, and the state field is not one code
    # system -- Canadian filers write 'ON', not the EDGAR 'A6'.  So the
    # invariant is not "every code is an EDGAR code"; it is "every code either
    # resolved to a full name, or was left NULL and marked low confidence".
    silent = conn.execute(
        """SELECT COUNT(*) n FROM entity_final
           WHERE hq_state_code IS NOT NULL
             AND hq_state IS NULL AND hq_country IS NULL
             AND hq_conf <> 'low'""").fetchone()["n"]
    c.eq(silent, 0, "an unresolved state code is never left at high confidence")

    # A full name must never appear without the country that licensed it.
    dangling = conn.execute(
        """SELECT COUNT(*) n FROM entity_final
           WHERE hq_state IS NOT NULL AND hq_country IS NULL""").fetchone()["n"]
    c.eq(dangling, 0, "no sub-national name without a country")

    # Canada Post abbreviations must resolve for Canadian companies...
    # NOTE: `hq_state <> ?` is NULL-blind -- an unresolved row has hq_state
    # NULL, `NULL <> 'Ontario'` evaluates to NULL, and the row is NOT counted.
    # An earlier version of this test passed while 19 'ON' rows sat unresolved.
    # SQLite's `IS NOT` is the null-safe comparison; use it for any assertion
    # whose whole point is to catch a missing value.
    # A Canada Post abbreviation resolves to its province UNLESS the filing
    # states a different country -- AEN Group tags 'AB' with its office in
    # Zaoyang and EntityAddressCountry='CN', and China must win over Alberta.
    for code, want in (("ON", "Ontario"), ("BC", "British Columbia"),
                       ("QC", "Quebec"), ("AB", "Alberta"),
                       ("SK", "Saskatchewan"), ("NS", "Nova Scotia"),
                       ("MB", "Manitoba")):
        bad = conn.execute(
            """SELECT COUNT(*) n FROM entity_final f
               WHERE f.hq_state_code=?
                 AND (f.hq_country IS NULL OR f.hq_country = ?)
                 AND (f.hq_state IS NOT ? OR f.hq_country IS NOT ?)""",
            (code, CANADA, want, CANADA)).fetchone()["n"]
        c.eq(bad, 0, f"every Canadian {code} office resolves to {want}")

    # ...and where a different country IS stated, the province must be dropped
    # rather than asserted anyway.
    wrong = conn.execute(
        """SELECT COUNT(*) n FROM entity_final
           WHERE hq_country IS NOT NULL AND hq_country <> ?
             AND hq_state IN ('Ontario','Alberta','British Columbia','Quebec',
                              'Saskatchewan','Nova Scotia','Manitoba')""",
        (CANADA,)).fetchone()["n"]
    c.eq(wrong, 0, "no Canadian province asserted outside Canada")

    # ...and the three-way NL case must be split correctly by country.
    #
    # The state is None for the last two on purpose.  FEMSA's NL is Nuevo Leon,
    # a Mexican state this package has no table for; LAVA's NL is the country
    # itself, tagged in the state field.  Both still get the right COUNTRY,
    # because EntityAddressCountry is tagged and decoded with the ISO table --
    # naming the subdivision is a separate, unmet need, and a None there is the
    # honest answer rather than a guess.
    for cik_like, want_state, want_country in (
            ("FORTIS%", "Newfoundland and Labrador", CANADA),
            ("%Mexican Economic%", None, "Mexico"),
            ("LAVA%", None, "Netherlands")):
        r = conn.execute(
            """SELECT hq_state, hq_country FROM entity_final
               WHERE hq_state_code='NL' AND entity_name LIKE ?""",
            (cik_like,)).fetchone()
        if r is None:
            continue
        c.eq(r["hq_state"], want_state, f"NL for {cik_like}: state")
        c.eq(r["hq_country"], want_country, f"NL for {cik_like}: country")

    # Country coverage: EntityAddressCountry is the only direct statement of
    # where the office is, so a row that has it must end up with a country.
    lost = conn.execute(
        """SELECT COUNT(*) n FROM entity_final f
           JOIN entity_resolved r ON r.cik = f.cik AND r.field='address_country'
           WHERE r.value IS NOT NULL AND f.hq_country IS NULL""").fetchone()["n"]
    c.eq(lost, 0, "a tagged EntityAddressCountry always yields an hq_country")

    # No row may carry a country without the code that produced it.
    orphan = conn.execute(
        """SELECT COUNT(*) n FROM entity_final
           WHERE incorporation_country IS NOT NULL
             AND incorporation_code IS NULL""").fetchone()["n"]
    c.eq(orphan, 0, "no country without a source code")

    # A US state code must never yield a non-US country, and vice versa.
    bad_us = conn.execute(
        """SELECT COUNT(*) n FROM entity_final
           WHERE incorporation_state IS NOT NULL
             AND incorporation_country NOT IN (?, ?)""",
        (UNITED_STATES, CANADA)).fetchone()["n"]
    c.eq(bad_us, 0, "a sub-national name implies US or Canada only")

    # Known companies, by CIK so a name collision cannot pick the wrong filer.
    KNOWN = [
        ("1652044", "Alphabet", "DE", "Delaware", UNITED_STATES,
         "Mountain View", UNITED_STATES),
        ("320193", "Apple", "CA", "California", UNITED_STATES,
         "Cupertino", UNITED_STATES),          # CA = California, not Canada
        ("1067983", "Berkshire", "DE", "Delaware", UNITED_STATES,
         "Omaha", UNITED_STATES),
        ("24545", "Molson Coors", "DE", "Delaware", UNITED_STATES,
         "Golden", UNITED_STATES),
    ]
    for cik, label, code, state, country, city, hq_country in KNOWN:
        r = conn.execute("SELECT * FROM entity_final WHERE cik=?",
                         (cik,)).fetchone()
        if r is None:
            c.failed.append(f"{label} (cik {cik}) missing from entity_final")
            continue
        c.eq(r["incorporation_code"], code, f"{label} incorporation code")
        c.eq(r["incorporation_state"], state, f"{label} incorporation state")
        c.eq(r["incorporation_country"], country, f"{label} incorporation country")
        c.eq(r["hq_city"], city, f"{label} hq city")
        c.eq(r["hq_country"], hq_country, f"{label} hq country")

    # The collision, measured: every CA-incorporated company must read as US.
    ca = conn.execute(
        """SELECT COUNT(*) n FROM entity_final
           WHERE incorporation_code='CA' AND incorporation_country <> ?""",
        (UNITED_STATES,)).fetchone()["n"]
    c.eq(ca, 0, "no CA-incorporated company resolved to Canada")

    conn.close()
    return c.report("6. live data")


def test_shares_grading() -> bool:
    """Confidence in a share count, offline."""
    c = _Check()
    # (shares, as_of, age, ambiguous, listed) -> level
    CASES = [
        ((5e9, "2026-07-15", 10, False, True), "high"),
        ((835e6, "2026-07-15", 10, False, False), "high"),   # unlisted class
        ((5e9, "2026-07-15", 10, True, True), "low"),        # attribution open
        ((5e9, None, None, False, True), "medium"),          # no measured date
        ((5e9, "2020-01-01", 2400, False, True), "medium"),  # stale
        ((None, None, None, False, True), "low"),            # no count
        # Ambiguity outranks everything: a number on the wrong class is worse
        # than a missing or old one.
        ((5e9, "2026-07-15", 10, True, False), "low"),
    ]
    for args, want in CASES:
        level, why = grade_shares(*args)
        c.eq(level, want, f"grade_shares{args}")
        c.eq(bool(why), True, "every share verdict explains itself")
    return c.report("7. share-count grading")


def test_shares_live(db_path: str) -> bool:
    c = _Check()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    have = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "security_final" not in have:
        print("\n8. shares live: SKIPPED (run `cli resolve --build` first)")
        conn.close()
        return True

    # Only the registrant's own equity may appear.  An ADS represents shares
    # that are ALSO counted on the ordinary line, so including it would
    # double-count the company -- the reason security_type exists at all.
    leaked = conn.execute(
        """SELECT COUNT(*) n FROM security_final f
           JOIN security_cover s ON s.cik=f.cik AND s.security_key=f.security_key
           WHERE s.security_type <> 'equity'""").fetchone()["n"]
    c.eq(leaked, 0, "no adr/preferred/right/unit/debt row reaches security_final")

    # Every count must carry the date it was measured.
    undated = conn.execute(
        """SELECT COUNT(*) n FROM security_final
           WHERE shares_outstanding IS NOT NULL
             AND shares_as_of IS NULL""").fetchone()["n"]
    c.eq(undated, 0, "every share count carries its as-of instant")

    # as_of must come from the fact's own period_instant, never be substituted
    # from filing_date.  Equality alone does NOT prove substitution -- BetterLife
    # and MingZhu genuinely tag an instant equal to their filing date -- so the
    # real check is that as_of matches the raw fact.
    mismatched = conn.execute(
        """SELECT COUNT(*) n FROM security_final f
           WHERE f.shares_as_of IS NOT NULL
             AND NOT EXISTS (
                 SELECT 1 FROM xbrl_facts x
                 WHERE x.accession_number = f.source_accession
                   AND x.cik = f.cik
                   AND x.concept = 'EntityCommonStockSharesOutstanding'
                   AND x.period_instant = f.shares_as_of)""").fetchone()["n"]
    c.eq(mismatched, 0, "every as-of date is a real period_instant from the facts")

    # 20-F counts should still be measurably older than their filing on average,
    # which is the whole reason the column exists.
    lag = conn.execute(
        """SELECT AVG(julianday(filing_date) - julianday(shares_as_of)) d
           FROM security_final WHERE source_form='20-F'
             AND shares_as_of IS NOT NULL""").fetchone()["d"]
    c.eq(lag is not None and lag > 30, True,
         f"20-F counts lag their filing by weeks (got {lag:.0f}d)")

    # Known multi-class structures, keyed on CIK.
    KNOWN = [
        ("1652044", 3, {"GOOGL", "GOOG"}, "Alphabet: A, B unlisted, C"),
        ("1326801", 2, {"META"}, "Meta: A listed, B unlisted"),
        ("1067983", 2, {"BRK.A", "BRK.B"}, "Berkshire: both listed"),
        ("317540", 2, {"COKE"}, "Coca-Cola Consolidated: B unlisted"),
    ]
    for cik, n_classes, tickers, label in KNOWN:
        rows = conn.execute(
            "SELECT * FROM security_final WHERE cik=?", (cik,)).fetchall()
        c.eq(len(rows), n_classes, f"{label}: class count")
        got = {r["trading_symbol"] for r in rows if r["trading_symbol"]}
        c.eq(got, tickers, f"{label}: listed tickers")
        c.eq(all(r["shares_outstanding"] for r in rows), True,
             f"{label}: every class has a count")
        # The unlisted class is the whole point -- it must survive.
        if n_classes > len(tickers):
            c.eq(any(not r["is_listed"] for r in rows), True,
                 f"{label}: the unlisted class is retained")

    # The same class must not be counted twice.  A shared ticker alone is not
    # proof -- Telesat lists two classes under TSAT -- so the test is the
    # damaging combination: same ticker AND same share count, which is one
    # class tagged both dimensioned and un-dimensioned (the 3M case, where the
    # company's shares would otherwise double).
    dupe = conn.execute(
        """SELECT COUNT(*) n FROM (
           SELECT cik, trading_symbol, shares_outstanding
           FROM security_final
           WHERE trading_symbol IS NOT NULL AND shares_outstanding IS NOT NULL
           GROUP BY cik, trading_symbol, shares_outstanding
           HAVING COUNT(*) > 1)""").fetchone()["n"]
    c.eq(dupe, 0, "no class counted twice under one ticker (the 3M double-count)")

    # 3M specifically: exactly one common-stock row, at the real figure.
    mmm = conn.execute(
        "SELECT * FROM security_final WHERE cik='66740'").fetchall()
    if mmm:
        c.eq(len(mmm), 1, "3M has exactly one share class")
        c.eq(mmm[0]["shares_outstanding"], 515722417.0, "3M share count")

    # Classes that legitimately share a count must NOT have been collapsed.
    # Petco reports Class A (WOOF) plus paired Class B-1 and B-2 that carry
    # identical counts by design -- 3 rows, not 2, and none of them a duplicate.
    for cik, label, n in (("909037", "SQM Series A/B", 2),
                          ("1826470", "Petco Class A + B-1 + B-2", 3)):
        rows = conn.execute(
            "SELECT COUNT(*) n FROM security_final WHERE cik=?",
            (cik,)).fetchone()["n"]
        if rows:
            c.eq(rows, n, f"{label}: distinct classes preserved")

    conn.close()
    return c.report("8. shares live")


# Real cover-page text, reduced to the address block.  Kept as fixtures so the
# suite runs offline and so a regression is reproducible without a fetch.
_COVER_MOLSON = """Commission File Number: 1-14829
Molson Coors Beverage Company
(Exact name of registrant as specified in its charter)
Delaware
(State or other jurisdiction of incorporation or organization)
P.O. Box 4030, BC555, Golden, Colorado, USA
111 Boulevard Robert-Bourassa, 9th Floor, Montreal, Quebec, Canada
(Address of principal executive offices)
84-0178360
(I.R.S. Employer Identification No.)
80401
H3C 2M1
(Zip Code)
303-279-6565 (Colorado)
514-521-1786 (Quebec)
(Registrant's telephone number, including area code)
"""

# Apple: ONE address wrapped across two lines -- the case that breaks any
# "more than one line means two offices" rule.
_COVER_APPLE = """Commission File Number: 001-36743
Apple Inc.
(Exact name of Registrant as specified in its charter)
California
94-2404110
(State or other jurisdiction
of incorporation or organization)
(I.R.S. Employer Identification No.)
One Apple Park Way
Cupertino, California
95014
(Address of principal executive offices)
(Zip Code)
(408) 996-1010
(Registrant's telephone number, including area code)
"""

# Uranium Energy: the filer LABELS both offices and prints two (Zip Code)
# fields, but only one telephone number.
_COVER_UEC = """Commission File Number: 001-33706
URANIUM ENERGY CORP.
(Exact name of registrant as specified in its charter)
Nevada
98-0399476
(State or other jurisdiction of incorporation of organization)
(I.R.S. Employer Identification No.)
500 North Shoreline, Ste. 800, Corpus Christi, Texas, U.S.A.
78401
(U.S. corporate headquarters)
(Zip Code)
1830 - 1188 West Georgia Street
Vancouver, British Columbia, Canada
(Canadian corporate headquarters)
V6E 4A2
(Zip Code)
(Address of principal executive offices)
(361) 888-8235
(Registrant's telephone number, including area code)
"""

# A combined annual report: the phrase appears deep in the body, not on a
# cover.  Parsing the text before it yields prose, not an address.
_COVER_COMBINED = ("Annual Report 2025\n" + ("Our strategy delivered. " * 400)
                   + "\nprincipal executive offices)\n"
                     "Gary Moore, Chief Governance Officer\n")


def test_covertext() -> bool:
    c = _Check()

    m = parse_cover(_COVER_MOLSON)
    c.eq(m.dual, True, "Molson Coors: two offices detected")
    c.eq(len(m.offices), 2, "Molson Coors: two address lines")
    c.eq(m.postcodes, ["80401", "H3C 2M1"], "Molson Coors: both postcodes")
    c.eq(m.incorporation_text, "Delaware", "Molson Coors: incorporation")
    c.eq("Montreal" in m.offices[1], True, "Molson Coors: second office kept")

    a = parse_cover(_COVER_APPLE)
    c.eq(a.dual, False,
         "Apple: two LINES but one address -- must not read as dual")
    c.eq(len(a.offices), 2, "Apple: both lines still captured")
    c.eq(a.postcodes, ["95014"], "Apple: one postcode")
    c.eq(a.incorporation_text, "California", "Apple: incorporation")

    u = parse_cover(_COVER_UEC)
    c.eq(u.dual, True, "UEC: labelled offices detected")
    c.eq("labelled offices" in u.reason or "Zip Code" in u.reason, True,
         f"UEC: reason names the evidence (got {u.reason!r})")
    # A bare postcode is a field of its own, not an address line.
    c.eq(any(x.strip() in ("V6E 4A2", "78401") for x in u.offices), False,
         "UEC: postcodes excluded from the address lines")
    c.eq(any("Vancouver" in x for x in u.offices), True,
         "UEC: the Canadian office is captured")

    k = parse_cover(_COVER_COMBINED)
    c.eq(k.dual, False, "combined annual report: not read as dual")
    c.eq(k.ok, False, "combined annual report: declines rather than guessing")
    c.eq("cover" in k.reason, True, "combined report: reason mentions the cover")

    # Degenerate inputs must not raise.
    for bad in ("", "no markers here at all", "(Zip Code)"):
        r = parse_cover(bad)
        c.eq(r.dual, False, f"parse_cover({bad[:18]!r}) is not dual")
        c.eq(bool(r.reason), True, f"parse_cover({bad[:18]!r}) explains itself")

    # Block-aware text conversion is what preserves the line structure.
    t = to_text("<div>Golden, Colorado, USA</div><div>Montreal, Canada</div>")
    c.eq(len([x for x in t.splitlines() if x.strip()]), 2,
         "to_text keeps block boundaries as separate lines")
    c.eq(to_text(""), "", "to_text('') is empty, not an error")
    return c.report("9. cover-page text (dual office)")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m finalized.tests_jurisdiction")
    p.add_argument("--db", default=None,
                   help="Also run checks against a real database.")
    a = p.parse_args(argv)

    t = table_size()
    print(f"tables: {t['codes']} codes {t['by_category']}, "
          f"{t['collisions']} ISO collisions, {t['aliases']} aliases")
    print(f"collisions guarded: {', '.join(sorted(collisions())[:12])} ...")

    ok = all([test_iso_vs_edgar(), test_decoding(), test_same_country(),
              test_office(), test_text(), test_compare(), test_confidence(),
              test_shares_grading(), test_covertext()])
    if a.db:
        ok = all([test_live(a.db), test_shares_live(a.db)]) and ok
    print("\n" + ("ALL PASS" if ok else "FAILURES ABOVE"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
