# finalized/ — SEC EDGAR mirror + XBRL extraction

The settled core of the pipeline, packaged so it can be used on its own:
pull filings from EDGAR, store them in SQLite with full provenance, extract
XBRL facts tied back to the filing they came from, and structure the cover page
into clean entity and share-class tables.

| level | stage | command | produces |
|---|---|---|---|
| 1–2 | ingest | `bootstrap` / `sync` | `filings` |
| 3 | extract | `extract --run` | `xbrl_facts` |
| 3 fine-tune | field selection | `extract --facts <group>` | fewer, chosen facts |
| 3.5 | cover | `cover --build` | `entity_cover`, `security_cover` |
| 4 | headers | `headers --run` | `header_cover` |
| 4 | covertext | `covertext --run` | `cover_office` |
| 4 | resolve | `resolve --build` | `entity_resolved`, `entity_final`, `security_final` |

**Full run, in order.** Each stage reads only what the previous one wrote, so
you can stop anywhere and resume:

```bash
python -m finalized.cli bootstrap --start 2026-01-01 --end 2026-06-30
python -m finalized.cli extract --queue
python -m finalized.cli extract --run --facts cover     # level 3
python -m finalized.cli cover --build                   # level 3.5
python -m finalized.cli headers --run --limit 500       # level 4, cheap
python -m finalized.cli resolve --build                 # level 4, no network
python -m finalized.cli covertext --run --candidates    # level 4, expensive
python -m finalized.cli resolve --final                 # fold covers back in
python -m finalized.cli status
```

Only three stages touch the network: `bootstrap`/`sync`, `extract`, `headers`
and `covertext`. `cover --build` and `resolve` are pure database work and are
cheap to re-run whenever the logic changes.

**Scope.** This package stops at structured data. The local-LLM analysis layer,
the Flask viewer, country assignment and free-text HTML extraction all live in
the parent project and are deliberately *not* copied here.

---

## Install

```bash
pip install -r finalized/requirements.txt
```

Three packages — `requests`, `lxml`, `pandas`. Storage is stdlib `sqlite3`.
Nothing to host, no API key.

**Set your User-Agent before the first request.** The SEC requires automated
traffic to identify itself with a real contact address, and may block requests
that do not. Edit the top of [`core.py`](core.py):

```python
UA = {"User-Agent": "you@example.com"}
```

The other rule the SEC enforces is **≤ 10 requests/second**. This package paces
at ~8/s through a single fetcher and backs off on `429`; you do not have to
manage it, but you must not add a second HTTP path that bypasses it.

---

## The two ways in

This is the first decision, and both produce **identical `FilingRecord`
fields**, so everything downstream works the same either way.

| | by CIK | by date |
|---|---|---|
| module | `core.fetch_filings_for_ciks` | `ingest.bootstrap` / `run_incremental` |
| source | submissions API (`data.sec.gov`) | daily index (`master.YYYYMMDD.idx`) |
| grain | one request per **company** | one request per **published day** |
| answers | "give me Apple's 10-Ks" | "mirror every filing from A to B" |
| output | a list in memory | rows in SQLite |
| cost | 1 request per CIK | ~1 request per day + one ticker file |
| header fields | **included** (`isXBRL`, `primaryDocument`, `reportDate`) | **absent** — needs enrichment |

That last row is the one that catches people out. See
[Enrichment](#enrichment-the-step-between) below.

### By CIK — a watchlist

```python
from finalized import fetch_filings_for_ciks

filings = fetch_filings_for_ciks(ciks=["320193"], form_types=["10-K", "10-Q"])
f = filings[0]
f.form_type, f.filing_date, f.xbrl_instance_url
```

Use this when you know which companies you care about. It is one request per
CIK regardless of how many filings come back, and the response already contains
the header fields.

### By date — the whole market

```bash
# one-time backfill of a range you choose
python -m finalized.cli bootstrap --start 2026-01-01 --end 2026-06-30

# then keep it current; safe on a scheduler (cron / Task Scheduler)
python -m finalized.cli sync
```

```python
from finalized import bootstrap, run_incremental

bootstrap("2026-01-01", "2026-06-30")
run_incremental()                       # resumes from the watermark
```

Use this when you want coverage rather than a watchlist. Measured: **307
published days produced 1,590,488 filings in a 921 MB database in under four
minutes** — roughly 5,200 filings per published day at ~0.58 KB per row.

### Which should I use?

Fewer than a few thousand companies, and you know who they are → **by CIK**.
Market-wide work, or you don't know the CIKs up front → **by date**. They
compose: mirror by date, then use the CIK path to top up a specific company.

---

## The pipeline, stage by stage

```
  (a) CIK list ──► core.fetch_filings_for_ciks ──┐
                                                  ├──► FilingRecord
  (b) date range ► ingest.bootstrap ──────────────┘        │
                        │                                  │
                        ▼                                  ▼
                  filings table  ──► enrich ──► extract ──► xbrl_facts
                  idx_files                       ▲            │
                  ingest_runs                     │            │  level 3.5
                                            --facts group      ▼
                                           (what to store)  entity_cover
                                                            security_cover

                              everything is joined on (accession_number, cik)
```

### 1. Ingest — mirror the metadata

```bash
python -m finalized.cli bootstrap --start 2026-06-01 --end 2026-06-30
python -m finalized.cli sync --end 2026-07-31
python -m finalized.cli status
```

Three tables get written: `filings` (one row per filing), `idx_files` (the
watermark ledger of which published days are done), `ingest_runs` (the audit
log, including the exact User-Agent used).

The watermark is keyed on **index date, not filing date**, because EDGAR
re-disseminates old filings on later dates — a filing-date watermark would
silently skip them. This is why a 15-month ingest legitimately contains rows
dated 2002.

### 2. Enrichment — the step between

The daily index carries only five columns, so these fields arrive empty:
`report_date`, `primary_document`, `filing_url`, `act`, `file_number`,
`is_xbrl`, `xbrl_instance_url`.

**Two of those are exactly what you need to fetch a document**, so enrichment
is not optional if you plan to extract anything. It reads each filing's SGML
`<SEC-HEADER>` with a small HTTP range request:

```bash
python -m finalized.cli enrich --forms 10-K,20-F,40-F
python -m finalized.cli enrich --forms 10-Q --since 2026-01-01 --limit 5000
```

One request per filing, so **always scope it** with `--forms` / `--since` /
`--until` / `--limit`.

The XBRL instance URL cannot be guessed, which is why this step exists: EDGAR
derives it from the primary document (`aapl-20260430.htm` →
`aapl-20260430_htm.xml`) and only for *inline* XBRL filings. Older-style
filings ship the instance as a separately-named `EX-101.INS` exhibit that is
not derivable, so those return `None` rather than a wrong guess.

**You usually don't need to run this separately.** `extract` performs the same
header read inline for any filing whose instance URL is unknown, and writes the
result back to `filings`, so it is paid once ever rather than once per attempt.
Run `enrich` on its own when you want the header fields for their own sake.

#### Cheaper alternatives, if a request per filing is too much

| source | size | cost | gives you |
|---|---:|---|---|
| `full-index/YYYY/QTRn/xbrl.idx` | 1 MB | ~4 req/yr | which filings have XBRL (quarterly only — there is no daily `xbrl.idx`) |
| `bulkdata/submissions.zip` | 1,554 MB | 1 download | every header field, for every filing |
| `xbrl/companyfacts.zip` | 1,391 MB | 1 download | every XBRL fact, already parsed, stamped with its `accn` |

`companyfacts.zip` is worth knowing about: each fact carries the accession that
reported it, so joining `accn` → `filings.accession_number` gives filing-linked
financial data with no document fetching at all. It does not cover dimensional
detail, custom extension tags, or the DEI cover page — the per-filing path
still earns its keep for those.

### 3. Extract — XBRL facts, tied to the filing

```bash
python -m finalized.cli extract --queue           # select candidates (no network)
python -m finalized.cli status                    # check before committing
python -m finalized.cli extract --run --limit 100
python -m finalized.cli extract --run             # work the whole queue
python -m finalized.cli extract --run --retry-failed
```

```python
from finalized import queue_candidates, extract_xbrl

queue_candidates()                 # free and reversible
extract_xbrl(limit=100)
```

Queueing only copies keys out of `filings`, so it costs nothing and
`DELETE FROM extraction_queue` undoes it. **Check `status` before a long run.**

Per filing the driver: reads the header if the instance URL is unknown → marks
`no_xbrl` if there is no inline XBRL (never retried) → otherwise fetches and
parses the instance and stores the facts. That is two requests for a filing
with XBRL, one for a filing without. Expect roughly **30% `no_xbrl`** — most
8-Ks and all older filings.

#### What gets queued

Periodic reports, registration statements and prospectuses, **excluding
`424B2`**:

```sql
form_type IN ('10-K','10-K/A','20-F','20-F/A','40-F','40-F/A',
              '10-Q','10-Q/A','8-K','8-K/A')
OR form_type LIKE 'S-%'  OR form_type LIKE 'F-%'
OR form_type LIKE 'POS%' OR (form_type LIKE '424%' AND form_type <> '424B2')
```

`424B2` is 221,184 filings from ~775 bank filers — medium-term-note pricing
supplements with no company disclosure. Excluding it roughly halves the job.
`--include-424b2` puts it back.

#### Volume

A 10-Q yields ~500 facts, a 10-K ~1,500. Queue tens of thousands of filings and
`xbrl_facts` reaches tens of millions of rows and several GB. That is fine for
SQLite — it is why the table carries only two indexes — but run in chunks with
`--limit` rather than one multi-hour session.

Measured on this mirror: **20,437 filings → 20,709,005 facts → 38.6 GB.** If
that is more than you want, the next section is how to shrink it.

### 3b. Fact fine tuning — choosing which fields to store

`--facts` names a **field group**: a set of XBRL concepts. The run stores only
those, so the database grows at a fraction of the rate.

```bash
python -m finalized.cli facts --list                  # what groups exist
python -m finalized.cli facts --show headquarters     # what's in one
python -m finalized.cli facts --coverage headquarters # how often it's reported

python -m finalized.cli extract --run --facts all              # default
python -m finalized.cli extract --run --facts cover            # level 3.5 input
python -m finalized.cli extract --run --facts headquarters,shares
python -m finalized.cli extract --run --facts Assets,Liabilities
```

| group | concepts | what it covers |
|---|---:|---|
| `all` | — | every fact (**the default**) |
| `cover` | 43 | identity + headquarters + shares + document — what Level 3.5 needs |
| `identity` | 14 | registrant name, CIK, filer category, public float |
| `headquarters` | 10 | address block + state/country of incorporation |
| `shares` | 8 | share counts, tickers, exchanges, 12(b) titles |
| `document` | 11 | form type, fiscal period, amendment flags |
| `financials` | 13 | core statement lines (Assets, Revenues, NetIncomeLoss…) |

**Groups compose.** `--facts headquarters,shares` is the union. A bare CamelCase
concept name works too, for anything no group covers.

**Custom groups** live in the database and behave like built-ins:

```bash
python -m finalized.cli facts --define russell=EntityPublicFloat,EntityCommonStockSharesOutstanding \
                              --description "float + shares for index screens"
python -m finalized.cli extract --run --facts russell
python -m finalized.cli facts --delete russell
```

A custom group may not shadow a built-in one — otherwise the meaning of
`--facts cover` would depend on which database you opened.

#### Three things to know before you filter

**It saves storage, not requests.** The instance document arrives in one request
either way and is parsed in full; the filter only decides what gets written. A
lean run takes the same wall-clock time as a full one.

**What you skip, you cannot get back** without re-fetching the filing — and the
fetch is the expensive part. This is why `all` is the default. Filter when a run
feeds one known question; keep everything when you are still exploring.

**A lean run is recorded as lean.** The group is written to
`extraction_queue.fact_group`, so nothing later mistakes "we only kept ten
concepts" for "this filing does not report Assets":

```sql
SELECT COALESCE(fact_group,'all') AS stored, COUNT(*)
FROM extraction_queue WHERE status='done' GROUP BY stored;
```

`status` prints the same breakdown whenever more than one group is present.

Check coverage before trusting a group — a concept reported by 10% of filers is
not something to build an answer on:

```
  98.5%    27,754  EntityIncorporationStateCountryCode
  97.7%    27,551  EntityAddressAddressLine1
  10.5%     2,963  EntityAddressCountry        <- derive country, don't read it
```

### 3.5. Cover — entity and security tables

`xbrl_facts` is correct but shapeless: a company's identity and every one of its
share classes arrive as undifferentiated rows. Level 3.5 reshapes them.

```bash
python -m finalized.cli cover --build             # all companies (no network)
python -m finalized.cli cover --build --cik 1652044
python -m finalized.cli cover --summary
```

This reads only the database, so it is cheap to re-run whenever the transform
changes, and safe to interrupt.

| table | one row per | holds |
|---|---|---|
| `entity_cover` | `(accession_number, cik)` | name, incorporation, HQ address, filer category |
| `security_cover` | `(accession_number, cik, security_key)` | class, type, ticker, exchange, shares, `is_listed` |

**The rule these tables exist to make true:**

```sql
SELECT * FROM security_cover WHERE security_type = 'equity';
```

returns exactly the registrant's share classes — listed *and* unlisted — and
nothing else. Depositary receipts, preferred series, rights, SPAC units,
warrants and bonds each sit under their own `security_type`, because summing an
ADS alongside the ordinary shares it represents double-counts the company.

```
  Alphabet Inc.  (cik 1652044)
     equity  CommonClassA     GOOGL   listed     5,868,000,000
     equity  CapitalClassC    GOOG    listed     5,527,000,000
     equity  CommonClassB     -       UNLISTED     835,000,000   <- no ticker
     adr     DepositaryShares GOOGM   listed                 -   <- not equity
```

That unlisted Class B is the point. It carries shares but no ticker, so anything
keyed on "has a trading symbol" silently drops it — and with it, 835 million
shares of a company you thought you had measured.

#### Why it sources from periodic reports only

An 8-K cover lists *registered* securities, so it shows tickers but reports
**zero** share-class lines — verified across Alphabet, Meta, Coca-Cola, Apple and
Molson Coors. Unlisted voting classes appear only on a 10-K / 10-Q / 20-F / 40-F
cover. So `PERIODIC_FORMS` is the source, and the newest such filing per CIK
wins.

#### What it fixes, and what it declines to guess

A cover page can split one share class across two facts — the 12(b) listing
(title, ticker) and the share count — and filers split them two different ways:

- **undimensioned listing** (Meta): the classes carry counts, the ticker carries
  no class dimension at all;
- **separately dimensioned listing** (Ares): the ticker sits on its own axis
  member while the count sits on another.

Both are folded back together by reading the class **off the title the filer
printed** ("Class A common stock, par value $0.01 per share"), never by a
convention like "the listed one is always Class A" — which is wrong for Nike,
where Class B is the listed line.

When the filing genuinely does not say, it declines rather than guess. Currently
**76 rows of 13,087** decline: NIO's cover names a Class A its facts never tag,
Telesat's names a Class B alongside only a Class C count. A wrong merge would be
worse than two honest rows.

Current shape of the built tables:

```
  entity_cover      7,536      security_cover   13,087
      equity   9,121   debt      1,459   warrant   783   adr    572
      other      493   preferred   380   right     198   unit    81
  multi-class companies: 1,351  (18% report more than one share class)
```

### 4. Trusted current values

Level 3.5 gives a clean row per company **per filing**. Research needs the other
shape: for CIK X, what is the headquarters *now*, the incorporation *now*, and
how much should I trust each?

```bash
python -m finalized.cli resolve --build        # database only, no network
python -m finalized.cli resolve --summary
python -m finalized.cli resolve --conflicts    # the review queue
```

```sql
SELECT field, value, as_of, source, status
FROM entity_resolved WHERE cik = '86312';
```

#### Where the value comes from

| rank | source | what it is | coverage |
|---|---|---|---|
| 1 | XBRL cover of the latest filing of **any** in-scope form | the registrant's own statement in that filing | 8-K carries a city 99.7% of the time |
| 2 | `header_cover` (SGML) | EDGAR's **registered profile** for the filer | every filing, XBRL or not |

The important discovery: an **8-K cover carries the address and incorporation
code at 99.7%** while carrying **zero** share classes. That is why `cover --build`
excludes 8-K but `resolve` includes it — 8-Ks are filed constantly, so they are
the freshest statement a company makes about where it is, and the facts are
already in `xbrl_facts` at no network cost.

**XBRL always wins where it exists, regardless of which filing is newer.** The
header is a registered profile that filers update lazily, and it is often stale
even when its filing is more recent. Travelers is the proof — both sources dated
the same day:

```
cik 86312  TRAVELERS COMPANIES, INC.   address_city
   xbrl_cover   New York     8-K 2026-07-24   <- trusted
   sgml_header  SAINT PAUL       2026-07-24
```

So the header's real value is reaching companies XBRL cannot (**331 companies**
here) and casting a second vote — never overriding a company's own statement.

#### Disagreement is graded, not counted

Naive string comparison called 43 of 236 cross-checked fields "conflicts" when
most were spelling. `status` grades them:

| status | meaning | example |
|---|---|---|
| `agreed` | identical | — |
| `cosmetic` | case, punctuation, spacing or street abbreviation | `700 Universe Boulevard` / `700 UNIVERSE BLVD` |
| `contained` | one is a more precise form of the other | `Boadilla del Monte (Madrid)` / `MADRID`; `Z4` / `A1` |
| `conflict` | genuinely different — **review this** | IHG `X0` vs `DE` |
| `xbrl_only` / `header_only` | only one source had it | — |

Two normalisation rules earn their keep. Street lines expand abbreviations
(`Blvd`→`Boulevard`, `S.`→`South`), which moved 8 false conflicts out of the
queue. That expansion is **not** applied to city names, because in a city `St`
means Saint (`St. George`), not Street. And Canadian issuers tag either the
province (`A1`, British Columbia) or the federal code (`Z4`, Canada) for the same
company, so that pair is `contained` rather than a conflict.

This module does **not** decide a country — EDGAR codes are not ISO and
resolving them properly is a later level's job with the full code tables. It
only needs to know when two codes are compatible enough not to raise an alarm.

#### Shares are not resolved here

An 8-K cover reports no share counts at all (0% of 13,497 measured), so there is
no competing XBRL opinion to reconcile: `security_cover` already holds the latest
per-class counts, each with its own `shares_as_of`.

```sql
SELECT security_class, trading_symbol, shares_outstanding, shares_as_of
FROM security_cover WHERE cik = ? AND security_type = 'equity';
```

`shares_as_of` is the instant the count was measured, **not** the filing date,
and the gap is large enough to matter:

| form | avg lag as-of → filed | worst |
|---|---:|---:|
| 20-F/A | 161 days | 399 |
| 20-F | 104 days | 700 |
| 40-F | 69 days | 121 |
| 10-K | 13 days | 719 |
| 10-Q | 6 days | 903 |

Resolve "latest shares" on `shares_as_of`. Using `filing_date` misdates every
foreign private issuer by a full quarter.

#### The finalized answer: full names, never codes

`entity_final` is the wide, decoded conclusion — one row per company, with
incorporation and principal executive office as **full names**, each carrying an
as-of date, a source and a confidence.

```sql
SELECT incorporation_state, incorporation_country,
       hq_city, hq_state, hq_country,
       incorporation_conf, hq_conf, hq_conf_why
FROM entity_final WHERE cik = ?;
```

**Why full names and not codes.** EDGAR codes are not ISO, and worse, the cover
page uses *both systems in adjacent fields*:

| field | system | `CA` | `DE` |
|---|---|---|---|
| `EntityAddressStateOrProvince` | EDGAR | California | Delaware |
| `EntityAddressCountry` | **ISO 3166-1** | **Canada** | **Germany** |

Verified in this mirror: every filing tagging `EntityAddressCountry='CA'` is
Canadian (IMAX in Mississauga, Waste Connections in Woodbridge), and every `DE`
is European (SmartKem, Veraxa Biotech AG). Decoding the country field with the
EDGAR table would place IMAX in the United States — silently, with no error. So
[`jurisdiction.py`](jurisdiction.py) keeps two separate decoders with two
separate tables, and downstream reads names.

24 EDGAR codes collide with ISO this way. `CA` alone appears as an incorporation
code 60 times here — every one California.

**The state field is not one code system either.** `NL` appears with three
meanings in this data:

| company | `NL` means | resolved via |
|---|---|---|
| FORTIS (St. John's) | Newfoundland and Labrador | Canadian incorporation |
| FEMSA (Monterrey) | Nuevo León, **Mexico** | `EntityAddressCountry='MX'` |
| LAVA Therapeutics (Utrecht) | the **Netherlands** | `EntityAddressCountry='NL'` |

So a sub-national code is resolved only inside a country established
independently. Eight Canada Post abbreviations (`AB BC MB NB NS NT ON QC`)
collide with nothing and resolve alone — but are flagged **uncorroborated**, and
capped at medium confidence, because AEN Group tags `AB` with its office in
Zaoyang, China. Five (`NL NU PE SK YT`) collide with Netherlands, Niue, Peru,
Slovakia and Mayotte, and always require context.

#### Confidence

Every finalized value carries a level and the reason for it, so a reviewer sees
*why* rather than a bare score.

| level | when |
|---|---|
| `high` | two independent sources agree, or agree at different grain |
| `medium` | single source (the registrant's own XBRL cover); or stale; or an uncorroborated postal inference |
| `low` | sources conflict; the code is not in the table; or no value at all |

Ordered most-damaging first: a conflict outranks staleness, because a value two
sources disagree about is worse than an old value they agree on. An undecodable
code can never be `high`.

Most rows currently sit at `medium` — that is honest, not a defect: only 40
headers have been read, so almost nothing has a second source yet. A wider
`headers --run` moves corroborated values to `high`.

#### Shares

`security_final` is one row per company per share class — equity only, because
an ADS represents shares already counted on the ordinary line and summing both
double-counts the company.

```sql
SELECT security_class, trading_symbol, is_listed,
       shares_outstanding, shares_as_of, shares_conf
FROM security_final WHERE cik = ?;
```

Share confidence turns on attribution and currency, not transcription — the
number is never parsed from prose. A company whose cover carries a listing row
that could not be matched to a class has **every** class marked `low`, because
which class the ticker belongs to is unresolved for that filer (76 companies).

One correctness fix worth knowing: some filers tag a single class **twice**,
once dimensioned and once not. 3M reported `us-gaap:CommonStockMember` and a
plain `CommonStock` line both at 515,722,417 — summing its equity gave 1.03
billion, exactly double. `drop_duplicate_undimensioned` removes the
un-dimensioned twin only when the ticker *and* the count both match. Classes
that merely share a count survive: SQM's Series A/B, Petco's Class B-1/B-2 and
Goldman Sachs's Class S/D/T funds all report identical numbers on genuinely
distinct members.

#### The second principal executive office

XBRL tags exactly one address, so a company with two head offices is
indistinguishable in the structured data from one with a single office. The
second office exists only on the **printed cover page**.

```bash
python -m finalized.cli covertext --run --cik 24545        # one company
python -m finalized.cli covertext --run --candidates       # the flagged list
python -m finalized.cli covertext --summary
python -m finalized.cli resolve --final                    # fold results in
```

This fetches a **full document** per filing — orders of magnitude dearer than
the header read — so it refuses to run unscoped. Use `--cik` or `--candidates`.

**Why line-counting does not work.** The obvious rule ("more than one line
before *(Address of principal executive offices)*") fails on the commonest
layout. Apple prints:

```
One Apple Park Way
Cupertino, California          ← one address, two lines
```

Molson Coors prints:

```
P.O. Box 4030, BC555, Golden, Colorado, USA
111 Boulevard Robert-Bourassa, 9th Floor, Montréal, Québec, Canada
```

Two complete addresses. Counting lines cannot separate them.

What does separate them is the fields the SEC form makes **singular** — one
`(Zip Code)`, one telephone number. A filer with two offices is forced to double
them up. Four signals, any one sufficient:

| signal | example |
|---|---|
| explicit office labels | UEC: `(U.S. corporate headquarters)` + `(Canadian corporate headquarters)` |
| multiple `(Zip Code)` fields | UEC prints two |
| multiple postcodes | Molson Coors: `80401`, `H3C 2M1` |
| multiple labelled phones | Molson Coors: `(Colorado)`, `(Québec)` |

Confirmed results land in `entity_final.hq_dual_confirmed` with the other
address in `hq_second_office`:

```sql
SELECT entity_name, hq_city, hq_country, hq_second_office, hq_dual_reason
FROM entity_final WHERE hq_dual_confirmed = 1;
```

A cover read also **settles** a candidate in the negative — Travelers was
flagged by the stale-header disagreement and the cover confirmed one office.

**Two limits, stated plainly.** Combined annual reports (NatWest, IHG file the
UK annual report and the 20-F as one document) repeat "principal executive
offices" deep in the body — at character 177,611 for NatWest — where the
preceding text is prose, not an address. The search is bounded to the cover
region, so those **decline** rather than return nonsense. And some covers use a
layout the marker never matches; those are reported as `no cover marker`, not
silently skipped.

#### What is deliberately NOT built: non-XBRL share counts

Share counts for filings without XBRL would need text extraction. Measured
before building it: of 1,638 non-XBRL filings in the share-relevant forms
(10-K, 10-Q, 20-F, 40-F, 8-K, 6-K), **80% are structured-finance shells** — ABS
trusts, receivables and owner trusts with no share classes at all. The remaining
334 are overwhelmingly closed-end funds (Nuveen, Pioneer, PIMCO) and LPs, and 98
of those are CIKs already covered by an XBRL filing elsewhere.

That leaves ~236 filings, nearly all funds. A fuzzy parser for that population
would add risk to a table whose entire value is that its numbers are right, so
it is not built. If the need appears later, `covertext.to_text` and
`primary_document` are the reusable half.

#### Test suite

```bash
python -m finalized.tests_jurisdiction                 # offline logic
python -m finalized.tests_jurisdiction --db PATH       # + real-data checks
```

283 assertions across ten groups, every case drawn from something that actually
went wrong: ISO/EDGAR collisions, sub-national grain, offshore incorporation,
unknown codes, confidence ordering, cover-page layouts, and live-data
invariants. The cover-page group runs on stored fixtures, so the whole suite
works offline; `--db` adds the real-data checks.

Two traps the suite itself had to learn:

- **`<>` is NULL-blind in SQL.** `hq_state <> 'Ontario'` is NULL when the value
  is NULL, so the row is not counted — an earlier version passed while 19 `ON`
  rows sat unresolved. Any assertion whose point is to catch a *missing* value
  must use `IS NOT`.
- **A stale expectation is not a failure.** When the ISO table landed, FEMSA and
  LAVA started resolving to Mexico and the Netherlands. The tests said `None`;
  the code was right and the tests were out of date.

### 5. Backups

```bash
python -m finalized.cli backup                    # snapshot, then verify it
python -m finalized.cli backup --list
python -m finalized.cli backup --verify PATH
python -m finalized.cli backup --restore PATH     # keeps a pre-restore copy
python -m finalized.cli backup --prune 3
```

These use SQLite's **online backup API**, not a file copy. That matters: the
database runs in WAL mode, so committed state is split between the `.sqlite`
file and its `-wal` sidecar, and copying them separately during a run can
capture a torn pair that will not open. The backup API takes a transactionally
consistent snapshot **while a run is in progress** and produces one
self-contained file with no sidecars.

`backup` verifies what it just wrote (`PRAGMA integrity_check` plus a row
count), because a backup you have never opened is a hope, not a backup.

---

## Fallbacks and failure behaviour

The design assumption is that a long run *will* be interrupted. Every stage is
built so that re-running it is the correct response.

| situation | what happens | what you do |
|---|---|---|
| One malformed filing | Marked `failed` in the ledger with the error; the run continues | `--retry-failed` on the next run |
| One bad index date | That date marked `failed`; other dates unaffected | `sync` retries it automatically |
| Persistent HTTP 429 | `SECBlockedError` aborts the whole run cleanly, everything unprocessed stays queued | Wait ~10 minutes, re-run |
| Many consecutive failures | Circuit breaker stops the run (assumes a rate ban) | Investigate before re-running |
| Network drop / timeout | Retried with exponential back-off inside the fetcher | Nothing |
| Process killed mid-run | Each filing is marked as it completes; SQLite is transactional | Just re-run |
| Re-running a finished range | Idempotent — UPSERT on `(accession_number, cik)` refreshes rather than duplicates | Nothing |
| Filing has no inline XBRL | Marked `no_xbrl`, never retried | Nothing — this is normal, ~30% |
| Enriched values already present | A plain re-ingest will not clobber them (`COALESCE(NULLIF(...))`) | Nothing |

Two rules behind all of that:

**One rate-limited path.** Every SEC request goes through
`core._get_with_retry`. A second HTTP path would race the same IP past the cap
and earn a ban.

**`core.py` is the shared foundation.** Other modules import the fetcher, the
UA, `SECBlockedError` and `FilingRecord` from it; nothing edits it. Access
policy and record shape live in exactly one place.

---

## Data model

| table | one row per | key |
|---|---|---|
| `filings` | filing, per filer CIK | `(accession_number, cik)` |
| `idx_files` | daily index file | `idx_date` |
| `ingest_runs` | execution | `run_id` |
| `extraction_queue` | (filing, extractor) | `(accession_number, cik, extractor)` |
| `xbrl_facts` | reported fact | `id`, indexed on `(accession_number, cik)` |
| `entity_cover` | company-filing | `(accession_number, cik)` |
| `security_cover` | security line | `(accession_number, cik, security_key)` |
| `header_cover` | company-filing, from SGML | `(accession_number, cik)` |
| `cover_office` | company-filing, from the printed page | `(accession_number, cik)` |
| `entity_resolved` | company **field** | `(cik, field)` |
| `entity_final` | company | `cik` |
| `security_final` | company share class | `(cik, security_key)` |
| `fact_groups` | custom field group | `group_name` |

`entity_resolved` is long, not wide — one row per `(cik, field)` — so each value
carries its own source, as-of date and agreement status. That is what a review
UI needs in order to show *why* a value is what it is.

**Why the composite key.** The daily index lists a filing once per associated
CIK — a Form 4 appears under both the issuer and the reporting owner. Accession
alone is *not* unique. `(accession_number, cik)` is the true grain, and it is
the tie-back every extracted fact carries:

**Key on CIK, never on ticker.** `ticker` is a convenience label filled from
`company_tickers.json`, and it is missing far more often than people expect —
measured on a full 15-month mirror, **56.6% of filings carry no ticker at all**,
and 20.9% of annual reports have none. It is also not the ticker you may be
thinking of: that file maps a CIK to a single line, so Molson Coors' 125 filings
are stored under `TAP-A` (the Class A listing), not `TAP`. A query filtered on
ticker will silently return a subset. Filter on `cik`; display the ticker.

```sql
SELECT f.entity_name, f.form_type, f.filing_date, x.concept, x.value_num
FROM   xbrl_facts x
JOIN   filings f USING (accession_number, cik)
WHERE  x.concept = 'Assets' AND x.is_dimensioned = 0;
```

A cheap correctness check on extracted data — these should agree:

```sql
SELECT x.concept, x.value_num FROM xbrl_facts x
WHERE  x.accession_number = ?
  AND  x.concept IN ('Assets','LiabilitiesAndStockholdersEquity')
  AND  x.is_dimensioned = 0;
```

And nothing should ever be orphaned:

```sql
SELECT COUNT(*) FROM xbrl_facts x WHERE NOT EXISTS (
    SELECT 1 FROM filings f
     WHERE f.accession_number = x.accession_number AND f.cik = x.cik);  -- 0
```

---

## Worked example: one company, end to end

```bash
python -m finalized.cli bootstrap --start 2026-01-01 --end 2026-03-31
python -m finalized.cli extract --queue
python -m finalized.cli extract --run --limit 200 --facts cover
python -m finalized.cli cover --build
python -m finalized.cli status
python -m finalized.cli backup
```

```python
import sqlite3, pandas as pd
from finalized import DEFAULT_DB_PATH

con = sqlite3.connect(DEFAULT_DB_PATH)

pd.read_sql_query("""
    SELECT f.entity_name, f.form_type, f.filing_date, COUNT(*) AS facts
    FROM   xbrl_facts x
    JOIN   filings f USING (accession_number, cik)
    WHERE  f.cik = ?                       -- Apple; CIK, not ticker
    GROUP BY x.accession_number
    ORDER BY f.filing_date DESC""", con, params=("320193",))
```

To go from a ticker to a CIK once, look it up rather than filtering on it —
that way a blank or Class-A ticker cannot silently shrink the result:

```sql
SELECT DISTINCT cik, entity_name FROM filings
WHERE ticker = 'AAPL' OR entity_name LIKE 'Apple Inc%';
```

Once `cover --build` has run, `security_cover` is the better lookup, because it
knows which ticker is the *stock* rather than one of the bond series on the same
cover:

```sql
SELECT e.entity_name, s.security_class, s.trading_symbol,
       s.is_listed, s.shares_outstanding
FROM security_cover s
JOIN entity_cover e USING (accession_number, cik)
WHERE s.cik = '1652044' AND s.security_type = 'equity'
ORDER BY s.is_listed DESC;
```

Every share class a company reports, one row each — the shape research keys on:

```sql
SELECT cik, COUNT(*) AS classes, SUM(shares_outstanding) AS total_shares
FROM security_cover
WHERE security_type = 'equity'
GROUP BY cik HAVING classes > 1;
```

---

## Troubleshooting

**`SSLCertVerificationError: unable to get local issuer certificate` on every
request.** Local security software (Norton's Web/Mail Shield, corporate
proxies) is intercepting TLS and re-signing with a root Python does not trust —
the giveaway is that browsers work while every Python HTTPS call fails,
including PyPI. Either disable that product's HTTPS scanning, or export its
root and point `REQUESTS_CA_BUNDLE` at a bundle containing it.

**`SECBlockedError`.** You are rate-banned. Stop, wait ~10 minutes, re-run.
Nothing is lost. If it recurs, check nothing else on the network is also
hitting EDGAR.

**"Database has no watermark yet."** `sync` is meant to follow a `bootstrap`.
Either bootstrap a range first, or pass `sync --start YYYY-MM-DD` to seed it.

**Everything is `no_xbrl`.** Expected for 8-K-heavy selections. Inline XBRL is
mostly 10-K/10-Q/20-F; older filings have none at all.

**`database is locked`.** Another run is writing. WAL mode means readers never
block, so queries are fine — but two writers are not. Wait for the first to
finish; the 30-second busy timeout handles brief overlaps.

**The database is huge.** Facts dominate. Check the split:

```sql
SELECT (SELECT COUNT(*) FROM filings)    AS filings,
       (SELECT COUNT(*) FROM xbrl_facts) AS facts;
```

Then extract future runs with a field group — see
[fact fine tuning](#3b-fact-fine-tuning--choosing-which-fields-to-store). It
does not shrink what you already have; nothing here deletes facts.

**`cover --build` produced empty or near-empty tables.** Either no periodic
reports have been extracted yet (an 8-K-only selection yields no share-class
lines by design), or the extraction ran with a field group narrower than
`cover`. Check what was stored:

```sql
SELECT COALESCE(fact_group,'all') AS stored, COUNT(*)
FROM extraction_queue WHERE status='done' GROUP BY stored;
```

**A company shows more share classes than it has.** The extra row will have a
ticker and no shares while the real class has shares and no ticker — the merge
declined because the filing did not say which class the ticker belongs to. That
is deliberate; see
[what it declines to guess](#what-it-fixes-and-what-it-declines-to-guess).

**A share count looks wrong.** It is not parsed from text — it is
`EntityCommonStockSharesOutstanding` read by concept name, attributed by XBRL
dimension (95.4% of counts) or taken from the undimensioned single-security line
(4.6%). Compare against the filing itself:

```sql
SELECT value, segment, period_instant FROM xbrl_facts
WHERE accession_number = ? AND cik = ?
  AND concept = 'EntityCommonStockSharesOutstanding';
```

---

## Module reference

| module | what it owns |
|---|---|
| [`core.py`](core.py) | SEC access policy: the single rate-limited fetcher, `UA`, `SECBlockedError`, `FilingRecord`, URL builders, the CIK/submissions path |
| [`daily_index.py`](daily_index.py) | daily-index discovery, `master.idx` parsing, ticker resolution, SGML header enrichment |
| [`database.py`](database.py) | schema + all reads and writes (`FilingDB`) |
| [`ingest.py`](ingest.py) | orchestration: `bootstrap`, `run_incremental`, `enrich_backfill` |
| [`extract.py`](extract.py) | level 3 — candidate selection, the queue, XBRL enrichment + fact extraction |
| [`profiles.py`](profiles.py) | level 3 fine tuning — field groups, `resolve_fields`, coverage |
| [`cover.py`](cover.py) | level 3.5 — `entity_cover` + `security_cover`, security typing, share-class merge |
| [`sgml.py`](sgml.py) | level 4 — HQ + incorporation from any filing's SGML header, matched per CIK |
| [`covertext.py`](covertext.py) | level 4 — the printed cover page; the second principal executive office |
| [`resolve.py`](resolve.py) | level 4 — `entity_resolved` / `entity_final` / `security_final`: trusted values, decoded, with confidence |
| [`jurisdiction.py`](jurisdiction.py) | EDGAR **and** ISO code decoding, kept in separate tables on purpose |
| [`tests_jurisdiction.py`](tests_jurisdiction.py) | the Level 4 edge-case suite (259 assertions) |
| [`data/`](data/) | the mappings, as CSV: EDGAR codes, ISO codes, collisions, aliases, postal abbreviations |
| [`xbrl.py`](xbrl.py) | XBRL instance → tidy facts; `entity_facts()` / `security_facts()` |
| [`backup.py`](backup.py) | WAL-safe online snapshots, verify, restore, prune |
| [`cli.py`](cli.py) | one entry point: `bootstrap`, `sync`, `enrich`, `facts`, `extract`, `cover`, `status`, `backup` |

### How the layers depend on each other

```
core.py ◄── daily_index.py ◄── ingest.py
   ▲                              │
   └────────── extract.py ◄───────┘        cover.py ──► xbrl.py
                  │                            │
                  └──► profiles.py ◄───────────┘   (COVER_REQUIRED)
                            │
                        database.py  ◄── everything
```

`core.py` is the shared foundation and nothing edits it. `cover.py` never
touches the network — it reads facts that `extract.py` already stored, which is
why re-running it is cheap.

### Differences from the working tree

This copy is not a straight duplicate:

- **Five CLIs became one.** `bootstrap.py`, `run.py`, `enrich.py`, `status.py`
  and `extract.py`'s own parser each re-declared `--db`, `--delay` and an ISO
  date validator. Those now live once on a shared parent parser, with each
  stage as a subcommand.
- **Backups became a real module.** The inline `shutil.copy` helper was
  replaced with the online backup API, plus verify / restore / prune.
- **Field selection is one concept, not two.** The working tree grew a
  `fact_profiles` table with aliases, projection and materialisation aimed at
  the research layer. Here it is reduced to the part extraction actually
  needs — a named set of concepts — so `profiles.py` has no dependency on
  research code and `extract.py` stays independent of it.
- **`cover.py` imports `xbrl.py` directly** rather than reaching into
  `methods/sec_xbrl_extract`, so Level 3.5 travels with the package.
- **Imports are package-relative**, so `finalized/` is self-contained and can
  be moved or vendored on its own.
- **The per-module `sys.path` shims are gone** — unnecessary inside a package.

### Verification behind the Level 3.5 numbers

The share logic was not accepted on inspection. Every claim above is measured
against the 7,536-company mirror:

| check | result |
|---|---|
| rights / ADRs / SPAC units leaking into `equity` | 0 |
| security rows with no parent filing | 0 |
| cities with trailing punctuation | 0 (was 269) |
| unmerged listing rows | 76 of 13,087 (was 706) |
| share counts attributed by XBRL dimension | 95.4% |
| `entity_cover` rows with a city | 99.7% |

Hand-verified multi-class structures: Alphabet (A/B/C, B unlisted at 835M), Meta
(A/B, B unlisted), Berkshire (BRK.A 505,697 / BRK.B 1,398,308,677), Nike (Class
B listed, Class A unlisted — the inverse of the usual), Visa, Fox, Molson Coors
(TAP.A/TAP), Coca-Cola Consolidated, CGI (IFRS axis), Ares (listing on its own
axis member).
