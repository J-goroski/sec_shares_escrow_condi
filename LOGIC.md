# SEC EDGAR Pipeline — Logic & Design Notes

A short guide to how the pieces fit together, and **why** each part exists.
The pipeline turns a list of company CIK codes into (a) filing metadata,
(b) a tidy table of every XBRL fact, and (c) two clean "cover page" tables —
one per **entity**, one per **security**.

```
company_tickers.json / CIK list
          │
          ▼
  sec_filings_sync.py        ── filing metadata  (FilingRecord: URLs, dates, XBRL flag)
          │  xbrl_instance_url
          ▼
  sec_xbrl_extract.py        ── parse XBRL instance → tidy facts
          │
          ├─ parse_filings()          one row per reported fact
          └─ cover_pages()            entity_facts + security_facts
          │
          ▼
  sec_filings_notebook.ipynb  ── the driver / UI (pandas DataFrames)
```

Everything runs against live SEC endpoints, so the whole design is shaped by the
SEC **fair-access policy** (≤10 requests/second, descriptive `User-Agent`
required). Breaking that gets the IP a 10-minute ban.

---

## 1. `methods/sec_filings_sync.py` — metadata fetcher

**Job:** for each CIK, hit the EDGAR *submissions* API and return a
`FilingRecord` per filing, filtered by form type and date.

> ⚠️ **This file is treated as read-only / locked.** Other modules import from
> it; they do not modify it.

Key parts and why they exist:

| Part | Why |
|---|---|
| `UA` (User-Agent = email) | SEC blocks requests without a descriptive UA. |
| `_get_with_retry()` | The **single** rate-limited HTTP path. 429 → exponential back-off; still-429 after retries → `SECBlockedError` (means a 10-min ban — stop). Having one choke point is what keeps us under the rate cap. |
| `SECBlockedError` | Lets callers distinguish "SEC banned us, stop everything" from an ordinary HTTP error. |
| `FilingRecord` (dataclass) | Self-describing metadata row. Carries the several EDGAR URLs so downstream code never has to reconstruct them. |
| `_build_urls()` / `_build_xbrl_instance_url()` | EDGAR URLs are derivable from the accession number + primary document, so we compute them with **no extra HTTP calls**. The XBRL instance URL only exists for **inline** XBRL filings, so it's gated on `isInlineXBRL` and returns `None` otherwise. |
| Early-exit in `_parse_recent_filings()` | Filings come back newest-first; once a date is before `start_date` we stop — avoids scanning a huge filer's full history. |

**Output that matters downstream:** `FilingRecord.xbrl_instance_url` — the input
to stage 2. It is `None` for filings without inline XBRL (most 8-Ks, older
filings), which is why those get skipped later.

---

## 2. `methods/sec_xbrl_extract.py` — XBRL → tidy tables

This module reuses `sec_filings_sync`'s rate-limited fetcher (imported, not
re-implemented) so **all** SEC traffic — metadata and documents — shares one
polite request path. It has three layers.

### 2a. Fact parser → tidy long format

`parse_instance_bytes()` (pure, no network) / `fetch_and_parse()` (one URL) /
`parse_filings()` (a whole `FilingRecord` list or `sync_df`).

A raw XBRL instance stores three things **apart** from each other:

- **facts** — the numbers/strings, each pointing at a context + unit *by ref*
- **contexts** — period (instant vs. duration), entity CIK, and any dimensions
- **units** — USD, shares, USD/shares, pure

A fact like `<us-gaap:Revenues contextRef="c-3" unitRef="usd">…` is meaningless
alone. "Cleaning" XBRL = **resolving those refs** so every fact becomes one
self-describing row. We emit **long ("tidy") format** — one row per fact — with
period, unit, and dimensions resolved into plain columns. Long format is the
right primitive: nothing is lost and you can always pivot it later
(`pivot_concepts()` is a convenience for that).

Why the notable bits exist:
- **lxml with `recover=True`** — tolerates the occasional malformed entity in
  older filings instead of failing the whole document.
- **Match namespaces by URI, not prefix** — prefixes vary filing to filing; the
  XBRL spec fixes the URIs.
- **`is_numeric` = "has a unit"** — separates real numbers (monetary/shares)
  from text facts (dei strings) that might coincidentally parse as numbers.
- **`dimensions` kept as a dict** *and* flattened to a `segment` string — dict
  for precise filtering, string for eyeballing. `is_dimensioned == False` gives
  you the consolidated top-line figures.

### 2b. Cover-page transform → entity + security levels

The DEI ("Document & Entity Information") cover page mixes two things, split by
whether a fact is dimensioned on a **security axis**:

- **`entity_facts()`** — un-dimensioned cover facts → **one row per filing**:
  registrant name, incorporation state, principal office, phone, filer category,
  fiscal period, single-class share total. Listing fields are excluded (they
  belong to securities); uncommon dei concepts are preserved under their raw
  XBRL names so nothing is dropped.
- **`security_facts()`** — one row per registered trading line (share class,
  listed note series, ADS, preferred…): `security_class`, `security_type`,
  `trading_symbol`, `exchange`, `shares_outstanding`, `security_title`.
- **`cover_pages()`** — returns both in one call.

**The hard part is that filers tag securities three different ways**, and the
code normalises all three to the same output (this is where most of the QA-driven
complexity lives):

1. **Multiple securities** (Apple, Alphabet, BBVA) — each dimensioned on a
   *security axis*: `us-gaap:StatementClassOfStockAxis` for domestic filers,
   `ifrs-full:ClassesOfShareCapitalAxis` for IFRS/foreign (20-F) filers. Both are
   recognised, plus any axis discovered dynamically from the listing facts.
2. **Single security** (Tesla, most companies) — *no axis at all*; title/ticker/
   shares are un-dimensioned. We synthesise one "default" security from those
   facts — but only when real listing identity (title/ticker) is present, so a
   lone company-wide share total doesn't create a phantom security.
3. **OTC issuers** (no §12(b) listing) — only an un-dimensioned share total,
   no listing facts. We synthesise a minimal common-stock row so the count is
   not lost (ticker will be `None` — the filing has none).

**Share-total attachment.** `EntityCommonStockSharesOutstanding` is reported
either per class (multi-class) or as one un-dimensioned company total
(single-class). The total is attached to the **unique common/ordinary** security
(`_is_common_stock`), never to a preferred/other equity — so e.g. Entergy's
`ETI/PR` preferred is not credited with the common count.

**Axis exclusions.** `EntityListingsExchangeAxis` (same security on multiple
exchanges) and `LegalEntityAxis` (which subsidiary issued it) are *not* security
identity — treating them as such would split one security into several rows
(Entergy). They're excluded, and the canonical security axes take priority when
a fact is dimensioned on several axes at once.

**Classification** (`_classify_security`): debt (note/debenture/bond) →
warrant → equity (stock/class/ordinary/share/preferred/depositary/capital/unit)
→ other. "unit" is included so LP/LLC common units and SPAC units read as equity.

**Text hygiene** (`_clean_text`): normalises non-breaking spaces, strips
zero-width / directional Unicode format characters (common in ADR titles), and
collapses whitespace. This is normalisation of values we already pull — **not**
scraping.

### 2c. What is deliberately *not* done

- **No trading volume** — it isn't in filings (market data, not XBRL).
- **No ADR-ratio parsing** — there is **no structured dei field** for the ratio;
  it lives only as free text in `security_title` (e.g. Shell: "…representing two
  ordinary shares"). Some filers (TSM, Toyota) omit it entirely. The title text
  is surfaced as-is for the caller to parse if wanted.

---

## 3. `sec_filings_notebook.ipynb` — driver

Set `CIKS`, `FORM_TYPES`, date range, and delay; run cells top to bottom. It
calls `fetch_filings_for_ciks()`, shows the `FilingRecord`s as a DataFrame, then
(optionally) `parse_filings()` → `cover_pages()` for the XBRL cover tables.

---

## 4. `methods/sec_filing_manual_extract.py` — manual / unstructured extraction

Works off the raw **full submission text** (`submission_txt_url`), not XBRL.
`process_submission()` splits the SGML envelope, picks the primary HTML doc,
strips inline-XBRL cruft into a clean gzip-saved HTML render, pulls
geographic/revenue/segment/product tables (`pd.read_html`), and extracts
geographic-**concentration statements** ("substantially all of our revenue is
derived from the United States"). Batch: `process_filings` + `statements_frame`
/ `tables_frame`.

## 5. `viewer/app.py` — Flask viewer

`python viewer/app.py` → a two-pane UI: left = company core info + XBRL
entity/security cover facts + filing links; right = the cleaned filing HTML in a
scrollable iframe. Additive; reuses the shared fetcher.

## 6. `methods/country_assignment/` — country of incorporation + HQ, and monthly monitoring

Assigns and monitors each company's **country of incorporation** and
**principal-executive-office (HQ)** country, and flags data-quality problems.

- **`codes.py`** — EDGAR State/Country codes are **NOT ISO** (`CA`=California,
  `DE`=Delaware, `IL`=Illinois, `A1`=BC, `2M`=Germany, `E9`=Cayman, `K3`=HongKong,
  `L2`=Ireland, `X0`=UK, `Z4`=Canada-federal, `F5`=Taiwan). `decode_code` /
  `country_of` / `text_to_country`. **All mappings are DATA** in `data/`:
  `edgar_state_country_codes.csv` (309 codes, regenerate with
  `python methods/country_assignment/data/build_edgar_codes.py`),
  `state_country_collisions.csv` (US-state codes a naive ISO read misreads —
  CA→Canada, DE→Germany, IL→Israel, KY→Cayman…), and `jurisdiction_aliases.csv`
  (England & Wales→UK, Republic of China→Taiwan…).
- **`incorporation_validate.py`** — cross-validate ONE filing across XBRL /
  SEC-HEADER / cover text: dual-HQ (TAP, UEC), XBRL-wrong (BABA `E9` vs `K3`),
  ISO-collision traps (informational; only forces review when uncorroborated).
- **`edgar_profile.py`** — pull the **current authoritative** registrant record
  from the EDGAR *submissions* API (`data.sec.gov/submissions/CIK…json`). This is
  the monthly source of truth: real-time `stateOfIncorporation` + business/mailing
  address, plus the latest **cover-bearing** filing (prefers the annual report for
  the fullest cover; falls back to the latest 10-Q/8-K/**6-K**). `fetch_profiles`
  for a watchlist; `iter_bulk_profiles(submissions.zip)` for the whole market.
- **`monthly_monitor.py`** — `run_monthly(ciks, out_dir)` builds one
  `CountryAssignment` per CIK (profile **+** manual validation of the latest
  cover, so **6-K / non-XBRL** forms and *principal-office-only* covers are
  handled — incorporation falls back to the filing header/cover, then profile),
  writes a timestamped **snapshot** (`country_assignment_<YYYY-MM>.csv`), and
  **diffs** it against last month's snapshot (`changes_<YYYY-MM>.csv`) on
  incorporation code/country, HQ country, HQ address, and dual-HQ. (You then
  compare the snapshot against your own database separately.)

**Monthly pull, most-accurate recipe.** The submissions JSON is authoritative and
real-time (where a redomicile / HQ move shows up) — it is the primary pull. Cross-
validate it against the latest filing's cover (manual extraction) to catch profile
errors (e.g. BABA's profile itself says `K3`=Hong Kong while the 20-F cover says
Cayman Islands) and to cover forms that carry only a principal office. For a whole-
market run, download `submissions.zip` once/month and build assignments
profile-only for speed, then deep-validate just the CIKs that changed.

---

## 7. `analysis/` — derived facts with local LLMs (+ the Research Desk)

The rule that organises the whole package: **the LLM interprets text it is
shown; it never answers from memory.** Every prompt embeds the filing text,
every reply is JSON-schema-constrained (Ollama structured outputs), and every
stored fact carries method, model, confidence and the evidence sentence. The
deterministic layer always runs first and always works without Ollama.

**Two LLM backends, one gateway.** `ollama_client` routes every call to
either the Ollama server or the EMBEDDED backend (`local_llm.py`:
llama-cpp-python loads a `.gguf` from `analysis/models/` in-process — no
server, nothing to connect to; env `LLM_BACKEND` forces a choice, `auto`
prefers the server). Both give schema-constrained JSON. The embedded
gemma-2-2b matched the served model 4/4 on the extraction benchmark on CPU
(~1–3 s/call). Gotcha: gemma-2's chat template raises "System role not
supported", so the embedded backend folds any system prompt into the user
turn — equivalent for instruction-following extraction.

**Model selection is empirical, per task.** A 4-task benchmark over the
installed models showed schema-constrained decoding makes small models strong
*extractors* — gemma2:2b went 4/4 (including parsing "one-half of one ordinary
share" → 0.5) and was fastest, while llama3.2:1b failed 3/4. But
*classification* is a judgement call: gemma2:2b misplaced Apple (Software) and
Nucor (Construction); both 7–8B models placed them correctly, so
`icb_classify` prefers the largest installed model while extraction keeps the
small fast default. Preference lists live in code and skip models that aren't
installed, so pulling a better model upgrades the pipeline with no change.

**Enum by name, not code.** The single biggest ICB accuracy lever: with the 45
sector *codes* as the enum, models returned the right rationale and the wrong
adjacent code ("Beverages" reasoning → Tobacco's code) — 4/10. Switching the
enum to sector *names* (mapped back to codes in Python) → 8/10, larger model →
9/10, definitional SIC hint (6798=REITs, same-industry tiebreak only) → 10/10.

**Prose needs stricter patterns than titles.** The loose ratio regex that is
safe on a 100-char security title false-positives on 200 pages of prose (TSM:
"percentage of shares held by each individual … represents" happened to yield
the *correct* 5.0 — luck, not extraction; AZN: holder statistics yielded
18.3). Filing-text patterns therefore require the ADS to be the grammatical
subject of "represents", quantities reject `%`, LLM candidates filter out
holder-count sentences, and LLM ratios face a plausibility cap (0.0001–100).

**A missing ratio can be the right answer.** AZN's 20-F contains no ADS ratio
because AstraZeneca terminated its ADR programme on 2 Feb 2026 and
direct-listed its Ordinary Shares on the NYSE. The extractor returning `None`
for AZN is a **pass**, and a hallucinated value would have been the failure —
which is exactly what the caps/filters prevented.

**Voting rights: two passes, one sentence pool.** Rules (class-mention ↔
votes-expression pairing, non-voting, relative-rights) and the LLM read the
same candidate sentences; agreement → `high`, disagreement → `review`, an
abstention is not a conflict. Pairing associates each votes-expression with
the nearest *preceding* class, except the "one vote for each share of Common
Stock" form where the class follows the expression (Coca-Cola's proxy — the
class is captured inside the per-share tail; a general forward-fallback would
mispair "Share units do not have voting rights … Common Stock"). Relative
rights resolve against the referenced class (Berkshire B = 1/10,000 × A = 
0.0001). Source fallback: annual report → DEF 14A (KO states voting only in
the proxy).

**QA scorecards (live EDGAR):** ADR 15/15 (SHEL 2, BABA 8, TSM 5 prose-only,
SONY 1, TM 10, BP 6, VALE 1, PBR 2, UL 1, SAP 1, NVO 1, HSBC 5, RIO 1, INFY 1,
AZN None-correct); voting META/GOOGL/KO/BRK-B all correct; ICB 10/10 sectors
(and spot-on subsectors: Computer Hardware, Integrated Oil and Gas, Soft
Drinks, Iron and Steel, Industrial REITs…). FTSE really does classify Meta
under Consumer Digital Services — the pipeline reproduced that independently.

**Review workflow.** `research_store` keeps one row per (company, kind, source
accession). Verdicts (approve/reject/edit) survive re-runs; only a *changed*
result resets a row to `pending` with a supersede note. Edited JSON takes
precedence over the model output everywhere (`ResearchDB.payload`).

**Data files:** `data/icb_taxonomy.csv` (validated 11/20/45/173 against the
published structure; rebuild via `build_icb_taxonomy.py`) and
`data/sic_icb_prior.csv` (SIC ranges → expected industry, narrowest range
wins, optional definitional sector hint) — mappings are data, not code.

---

## Design principles

1. **One rate-limited path.** All SEC traffic goes through
   `_get_with_retry`; no second limiter competes for the 10 req/s budget.
2. **`sec_filings_sync.py` is read-only.** Extraction imports from it.
3. **No extra HTTP for derivable data.** URLs are computed from the accession
   number; cover data comes from the one instance download.
4. **Long/tidy first, wide later.** Facts are stored one-per-row; wide/pivoted
   views are derived on demand.
5. **Extract what the filing says; don't invent.** Missing ticker/shares/ratio
   stay `None` rather than being scraped or guessed.
6. **Degrade gracefully.** One bad filing is logged and skipped, not fatal;
   empty inputs return stable, correctly-columned frames.

---

## Known limitations (surfaced by QA)

Validated against a stratified random sample from `company_tickers.json`
(market-cap ranked, ~9,300 tickers). Cover-bearing filers extract correctly
across the full size range; the residual mismatches are **data**, not bugs:

- **OTC / non-§12(b) issuers** report no ticker in the cover XBRL → `trading_symbol`
  is `None` (correct — there's nothing to extract).
- **Ticker currency** — `company_tickers.json` reflects the *current* ticker; a
  filing reflects the ticker *as filed* (e.g. BNY Mellon still tags `BK`).
- **Some ADRs/FPIs** tag only the ordinary-share line (with `NoTradingSymbolFlag`)
  and put the ADS ticker only on debt lines (AstraZeneca) — so the equity ticker
  can be absent from structured data.
- **Non-periodic / non-corporate filers are skipped** — registered funds
  (N-CSR/NPORT), Form-D-only issuers, F-6 ADR-registration shells, and 40-F
  filers with no DEI cover — none carry an XBRL cover to process.
- **Complex multi-entity registrants** (parent + subsidiary issuers) are best-effort.

**Country assignment specifics:**
- The **EDGAR profile can itself be wrong** (e.g. Alibaba's `stateOfIncorporation`
  is `K3`=Hong Kong, but it is Cayman-incorporated) — which is exactly why the
  monitor cross-validates the profile against the latest filing's cover text.
- **Dual-HQ** is detected from the **annual report** cover (two countries or two
  "headquarters" markers); 8-K/6-K covers are minimal, so the monitor validates
  against the latest annual when one exists.
- **EDGAR codes are not ISO** — never map a 2-letter incorporation code as ISO
  (`CA`→Canada is the classic error; it is California). Decode via the CSV tables.
- **Refresh cadence** — re-run `build_edgar_codes.py` if the SEC updates its code
  list; extend `state_country_collisions.csv` / `jurisdiction_aliases.csv` as new
  ambiguities/aliases surface.
