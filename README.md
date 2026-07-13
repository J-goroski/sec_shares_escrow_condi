# SEC EDGAR Pipeline — Notes & Code Guide

A toolkit for turning a list of company **CIK codes** into clean, structured data
pulled from SEC EDGAR filings:

1. **filing metadata** (what a company filed, and the URLs to each document),
2. **structured XBRL facts** + clean "cover page" tables (entity & security),
3. **manual / unstructured extraction** from the raw filing text (clean HTML,
   geographic/revenue/segment tables, and free‑text "concentration" disclosures),
4. **country‑of‑incorporation + HQ assignment and monthly monitoring**.

Plus a small **Flask viewer** and a **Jupyter notebook** to drive it all.

> This file is the practical orientation. [`LOGIC.md`](LOGIC.md) has the deeper
> design rationale and the QA‑surfaced edge cases.

---

## Install & first run

```bash
pip install -r requirements.txt          # pandas, requests, lxml (+ flask for the viewer)
```

Everything talks to **live SEC endpoints**, so there is nothing to host. Two rules
the SEC enforces and this code obeys automatically:

- **≤ 10 requests/second** (this code paces at ~8/s and backs off on `429`).
- **A descriptive `User-Agent`** — set to your email in
  [`methods/sec_filings_sync.py`](methods/sec_filings_sync.py) (`UA = {...}`).
  Change it to your own email.

The fastest way to see it work is the notebook — open
[`sec_filings_notebook.ipynb`](sec_filings_notebook.ipynb), set `CIKS`, and run
top to bottom.

---

## The big picture (data flow)

```
 CIK list
    │
    ▼
 sec_filings_sync.fetch_filings_for_ciks()   ──►  FilingRecord per filing
    │   (form type, dates, and every EDGAR URL: primary doc, index, .txt, XBRL)
    │
    ├───────────────► xbrl_instance_url ─► sec_xbrl_extract
    │                    parse_filings()   → tidy fact table (one row per fact)
    │                    cover_pages()     → entity_facts + security_facts
    │
    ├───────────────► submission_txt_url ─► sec_filing_manual_extract
    │                    process_submission() → clean HTML  + tables + statements
    │
    └───────────────► (CIK) ─────────────► country_assignment
                         edgar_profile   → current incorporation + HQ (submissions API)
                         monthly_monitor → snapshot + month‑over‑month diff
```

Two ideas explain **why there are two extraction paths**:

- **XBRL** is the *structured* data the filer machine‑tagged (numbers, the cover
  page). Clean, but only present on inline‑XBRL filings (most 10‑K/10‑Q/20‑F; not
  6‑K, not older filings).
- The **full submission text** (`<accession>.txt`) is *everything* — the primary
  HTML document plus every exhibit. We parse it ourselves to get things XBRL
  doesn't carry (readable HTML, note tables, free‑text disclosures) and to handle
  forms without XBRL.

---

## Directory map

```
methods/
  sec_filings_sync.py          Stage 1: filing metadata.  ⚠ LOCKED — import, don't edit.
                               Owns the ONE rate-limited fetcher (_get_with_retry),
                               the UA header, SECBlockedError, and FilingRecord.
  sec_xbrl_extract.py          Stage 2: parse XBRL instance → tidy facts;
                               cover_pages() → entity_facts + security_facts.
  sec_filing_manual_extract.py Stage 3: work off the raw .txt → clean HTML,
                               tables, concentration statements.
  country_assignment/          Stage 4: incorporation + HQ country, monitoring.
    codes.py                     EDGAR (NOT ISO) code decoders, loaded from data/.
    incorporation_validate.py    Cross-validate ONE filing (XBRL/header/cover).
    edgar_profile.py             Pull CURRENT values from the submissions API.
    monthly_monitor.py           run_monthly() → snapshot + diff vs last month.
    data/                        The mappings — CSV, not hardcoded:
      edgar_state_country_codes.csv    official SEC list (309 codes)
      state_country_collisions.csv     ISO-misread state codes (CA→Canada…)
      jurisdiction_aliases.csv         England & Wales→UK, Republic of China→Taiwan…
      build_edgar_codes.py             regenerates the codes CSV from the SEC page

filing_database/               Daily-index ingestion into SQLite (see its README).
                               The date-driven sibling of sec_filings_sync: mirrors
                               EVERY filing over a date range, then stays current
                               incrementally.  CLIs: bootstrap.py / run.py /
                               enrich.py / status.py.  (DB file is gitignored.)
viewer/app.py                  Flask two-pane filing viewer (see viewer/README.md).
sec_filings_notebook.ipynb     The driver / demo (5 sections, runs top to bottom).
LOGIC.md                       Design rationale + QA edge cases.
requirements.txt               Dependencies.
```

---

## Each capability, with a quickstart

### 1. Filing metadata — `sec_filings_sync`

```python
from methods.sec_filings_sync import fetch_filings_for_ciks
filings = fetch_filings_for_ciks(ciks=["320193"], form_types=["10-K", "10-Q"])
f = filings[0]
f.form_type, f.filing_date, f.submission_txt_url, f.xbrl_instance_url
```

`FilingRecord` is a dataclass carrying the identity fields and **all** the EDGAR
URLs, computed from the accession number with no extra HTTP. This is the input to
every downstream stage.

### 2. Structured XBRL — `sec_xbrl_extract`

```python
from methods.sec_xbrl_extract import parse_filings, cover_pages
facts = parse_filings(filings)          # tidy: one row per reported fact
entity, security = cover_pages(facts)   # cover page split two ways
```

- **`parse_filings`** downloads each `xbrl_instance_url` and returns a long
  ("tidy") table — one row per fact, with period/unit/dimensions resolved into
  plain columns. Long format loses nothing; pivot later if you want a statement
  view (`pivot_concepts`).
- **`cover_pages`** splits the DEI cover page into **entity** facts (one row per
  filing: name, incorporation, address, filer category…) and **security** facts
  (one row per registered trading line: class, ticker, exchange, shares).

### 3. Manual extraction — `sec_filing_manual_extract`

```python
from methods.sec_filing_manual_extract import process_submission
res = process_submission(f, save_dir="clean_html")   # f = FilingRecord / sync_df row / .txt URL
res.clean_html      # readable HTML (inline-XBRL cruft stripped), saved gzip
res.catalog         # matched tables: geographic / revenue / segment / product
res.tables_for("geographic")           # the parsed DataFrames
res.statements      # "substantially all of our revenue is derived from the US" etc.
```

Downloads the full `.txt`, splits the SGML envelope, cleans the primary document,
then extracts note tables and geographic‑concentration sentences. Batch with
`process_filings(...)` + `statements_frame()` / `tables_frame()`.

### 4. Country assignment + monthly monitor — `country_assignment`

```python
from methods.country_assignment import (
    decode_code, fetch_company_profile, validate_filing, run_monthly,
)

decode_code("CA")                 # {'name': 'California', 'kind': 'us_state', ...}  (NOT Canada!)
fetch_company_profile("24545")    # current incorporation + HQ from the submissions API
validate_filing(filings[0])       # cross-check ONE filing (dual-HQ, mismatch, ISO trap)

snapshot, changes = run_monthly(  # the monthly job
    ["320193", "24545", ...],
    out_dir="country_assignment_snapshots",
)
```

See **"Monthly monitoring"** below for the how/why.

### The viewer — `viewer/app.py`

```bash
python viewer/app.py        # http://127.0.0.1:5000  → type a CIK or ticker
```

Left pane: company core info + XBRL entity/security facts + filing links.
Right pane: the cleaned filing HTML in a scrollable iframe. See
[`viewer/README.md`](viewer/README.md).

---

## Key concepts (the stuff that bites you)

**One rate‑limited path.** Every SEC request in the whole project goes through
`sec_filings_sync._get_with_retry`. Never add a second HTTP path — that would race
the same IP past the 10 req/s cap and earn a 10‑minute ban (`SECBlockedError`,
which you should treat as "stop and wait 10 minutes").

**`sec_filings_sync.py` is locked.** Other modules import from it; nothing edits
it. It's the shared foundation (fetcher, UA, `FilingRecord`).

**XBRL cover = entity vs security.** The DEI cover page mixes company‑level facts
(one per filing) with per‑security facts (one per share class / listed note). The
hard part — normalising the three ways filers tag securities — lives in
`cover_pages`; you just get two clean tables.

**EDGAR codes are NOT ISO — this is the #1 gotcha.** In EDGAR's scheme
`CA`=California, `DE`=Delaware, `IL`=Illinois, `KY`=Kentucky. A naive reader maps
these as ISO country codes (`CA`→Canada, `DE`→Germany, `IL`→Israel, `KY`→Cayman)
and gets it wrong. `country_assignment/codes.py` decodes with the **official SEC
table** and flags these "ISO‑collision" traps. Foreign codes are non‑obvious too:
`2M`=Germany, `E9`=Cayman, `K3`=Hong Kong, `L2`=Ireland, `X0`=UK, `Z4`=Canada,
`F5`=Taiwan. **All of these live in CSV files, not in code**, so you can refresh
or extend them.

**Three sources, cross‑validated.** A company's incorporation/HQ appears in three
places that don't always agree: the XBRL, the SGML `<SEC-HEADER>`, and the
human‑written cover page. The country‑assignment code compares all three and flags
disagreement — including cases where SEC's own profile is wrong (e.g. Alibaba's
profile says `K3`=Hong Kong, but it is Cayman‑incorporated).

---

## Monthly monitoring — recommended flow

Goal: track each company's **country of incorporation** and **HQ** month over
month, and catch changes/errors.

- **Primary source = the EDGAR *submissions* API**
  (`data.sec.gov/submissions/CIK…json`). It is the authoritative, real‑time
  registrant record — where a redomicile or HQ move actually shows up — and it's
  one request per CIK. `edgar_profile.fetch_profiles(ciks)` pulls it.
- **Validation = the latest cover‑bearing filing** (manual extraction), which
  covers **6‑K and other non‑XBRL** forms and catches profile errors.
  `monthly_monitor.build_assignment` does profile + validation together.
- **Change detection = snapshots + diff.** `run_monthly(ciks, out_dir)` writes
  `country_assignment_<YYYY-MM>.csv` and diffs it against the previous month into
  `changes_<YYYY-MM>.csv`. Section 5 of the notebook drives this.
- **Whole market?** Pull the nightly `submissions.zip` bulk file once and iterate
  with `edgar_profile.iter_bulk_profiles(...)`, or run `run_monthly(...,
  validate_with_filing=False)` for a fast profile‑only pass, then deep‑validate
  only the CIKs that changed.

You then compare each snapshot against your own database separately (out of scope
for this repo).

### Refreshing the mappings

The code tables are data files, so they're easy to keep current:

```bash
python methods/country_assignment/data/build_edgar_codes.py   # re-scrape the SEC codes page
```

Edit `state_country_collisions.csv` (new ISO‑ambiguous state codes) or
`jurisdiction_aliases.csv` (new spelled‑name → country aliases) by hand as needed.

---

## Gotchas & limitations (short list)

- **No XBRL → no `parse_filings`.** 6‑K, most 8‑K, and older filings have no
  inline XBRL; use the manual path (or the profile) instead.
- **40‑F wrappers** (some Canadian filers) put the real annual report in an
  exhibit, so the primary document — and its table count — can be thin.
- **Dual‑HQ** is detected from the **annual report** cover; 8‑K/6‑K covers are
  minimal, so the monitor validates against the latest annual when one exists.
- **Ticker currency** — `company_tickers.json` reflects the *current* ticker; a
  filing reflects the ticker *as filed*.
- Table extraction from filing HTML is best‑effort — filings use lots of layout
  cells; review the parsed frames.

More detail and the QA history are in [`LOGIC.md`](LOGIC.md).
