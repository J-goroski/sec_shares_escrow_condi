"""
build_icb_taxonomy.py — regenerate ``icb_taxonomy.csv`` from Wikipedia.

The Industry Classification Benchmark (ICB, FTSE Russell, 2019+ structure) is a
4-level hierarchy encoded in the 8-digit subsector code itself:

    digits 1-2  industry      (11 of them,  e.g. 10 = Technology)
    digits 1-4  supersector   (20,          e.g. 1010)
    digits 1-6  sector        (45,          e.g. 101010 = Software and Computer Services)
    digits 1-8  subsector     (173,         e.g. 10101015 = Software)

The official FTSE Russell "ICB codes and descriptions" spreadsheet is behind a
form, but Wikipedia's ICB article carries the full tree (sourced from that
spreadsheet).  This script parses the article's wikitext tree and writes the
flat CSV used by ``analysis.icb_classify``.

Run:  python analysis/data/build_icb_taxonomy.py
"""

from __future__ import annotations

import csv
import os
import re
import sys

WIKI_RAW_URL = ("https://en.wikipedia.org/w/index.php"
                "?title=Industry_Classification_Benchmark&action=raw")
OUT_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "icb_taxonomy.csv")

_LEAF_RE = re.compile(r"^\*{5}\s*(.+?)\s*\((\d{8})\)\s*$")
_LEVEL_RE = re.compile(r"^(\*{2,4})\s*(.+?)\s*$")

EXPECTED = {"industries": 11, "supersectors": 20, "sectors": 45, "subsectors": 173}


def parse_wikitext(text: str) -> list[dict]:
    """Parse the ICB tree in the article wikitext into flat subsector rows."""
    industry = supersector = sector = None
    rows: list[dict] = []
    for line in text.splitlines():
        m = _LEAF_RE.match(line)
        if m:
            name, code = m.group(1), m.group(2)
            if not (industry and supersector and sector):
                continue
            rows.append({
                "industry_code":    code[:2],
                "industry":         industry,
                "supersector_code": code[:4],
                "supersector":      supersector,
                "sector_code":      code[:6],
                "sector":           sector,
                "subsector_code":   code,
                "subsector":        name,
            })
            continue
        m = _LEVEL_RE.match(line)
        if m:
            stars, name = len(m.group(1)), m.group(2)
            if stars == 2:
                industry = name
            elif stars == 3:
                supersector = name
            elif stars == 4:
                sector = name
    return rows


def validate(rows: list[dict]) -> None:
    counts = {
        "industries":   len({r["industry_code"] for r in rows}),
        "supersectors": len({r["supersector_code"] for r in rows}),
        "sectors":      len({r["sector_code"] for r in rows}),
        "subsectors":   len({r["subsector_code"] for r in rows}),
    }
    for k, want in EXPECTED.items():
        got = counts[k]
        status = "ok" if got == want else "MISMATCH"
        print(f"  {k:<13} {got:>4}  (expected {want})  {status}")
    if counts != EXPECTED:
        print("WARNING: counts differ from the published ICB structure -- "
              "check the article for edits before trusting the CSV.")


def main() -> int:
    import requests
    print(f"Fetching {WIKI_RAW_URL}")
    text = requests.get(WIKI_RAW_URL, timeout=30,
                        headers={"User-Agent": "icb-taxonomy-build"}).text
    rows = parse_wikitext(text)
    if not rows:
        print("ERROR: no subsector rows parsed -- article format changed?")
        return 1
    validate(rows)
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"Wrote {len(rows)} subsector rows to {OUT_CSV}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
