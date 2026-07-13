"""
build_edgar_codes.py — (re)generate ``edgar_state_country_codes.csv`` from the
official SEC page so the mapping is reproducible data, not hand-typed.

    python methods/country_assignment/data/build_edgar_codes.py

Source: https://www.sec.gov/submit-filings/filer-support-resources/edgar-state-country-codes
The page is one table with section markers ("Canadian Provinces", "Other
Countries"); we categorise each code as state / province / country accordingly.
The companion ``state_country_collisions.csv`` (EDGAR state codes that a naive
ISO-3166 read would misread — CA→Canada, DE→Germany, …) is curated by hand.
"""
import csv
import io
import os
import re
import sys

# make the project importable so we reuse the shared UA / rate-limited policy
# (file is methods/country_assignment/data/ → 4 levels up is the project root)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))))
import requests
import pandas as pd
from methods.sec_filings_sync import UA

URL = ("https://www.sec.gov/submit-filings/filer-support-resources/"
       "edgar-state-country-codes")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "edgar_state_country_codes.csv")


def _title(s: str) -> str:
    out = str(s).strip().title()
    out = re.sub(r"\b(And|Of|The|De|Da|Le)\b", lambda m: m.group(1).lower(), out)
    return out.replace("’S", "’s").replace("'S", "'s")


def main() -> None:
    resp = requests.get(URL, headers=UA, timeout=30)
    resp.raise_for_status()
    table = pd.read_html(io.StringIO(resp.text))[0]
    table.columns = ["code", "name"]
    recs = [r for r in table.to_dict("records") if str(r["code"]) != "States"]

    rows, seen, section = [], set(), "state"
    for r in recs:
        code, name = str(r["code"]).strip(), str(r["name"]).strip()
        if code == "Canadian Provinces":
            section = "province"; continue
        if code == "Other Countries":
            section = "country"; continue
        if code == name:                       # any other section marker
            continue
        if section == "province":
            category = "province" if name.upper().endswith(", CANADA") else "country"
        else:
            category = section
        if code in seen:
            continue
        seen.add(code)
        rows.append({"code": code, "name": _title(name),
                     "category": category, "raw_name": name})

    with open(OUT, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["code", "name", "category", "raw_name"])
        w.writeheader()
        w.writerows(rows)

    from collections import Counter
    print(f"wrote {len(rows)} codes -> {OUT}")
    print("by category:", dict(Counter(r["category"] for r in rows)))


if __name__ == "__main__":
    main()
