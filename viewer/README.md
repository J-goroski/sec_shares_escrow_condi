# EDGAR Research Desk (viewer)

The Flask UI over the whole pipeline — filings reader **plus** the analyst
workflow around the locally derived facts (`analysis/`).

## Run

```bash
python viewer/app.py
```

Then open <http://127.0.0.1:5000>.

## The three views

**Overview (`/`)** — search any CIK/ticker, pipeline status (whether Ollama is
up and which model is the default), stats over the research store, the latest
derived facts, and per-company coverage.

**Company (`/cik/<cik>`)** — the two-pane workspace:

- **Left** — company core info, the **Derived facts** panel (ICB
  classification, votes-per-share by class, ADR ratios — each with confidence,
  review status, and expandable evidence + provenance), the XBRL cover-page
  **entity**/**security** facts, and the recent filings list with SEC.gov
  links.
- **Right** — a scrollable, isolated render of the selected filing's **cleaned
  HTML**, generated on demand and cached in `clean_html/`.

**Run analysis** on the company page executes the full pipeline
(`analysis.analyze.analyze_company`) in a background thread — the page
auto-refreshes until the facts land. Equivalent to:

```bash
python -m analysis.analyze <TICKER>
```

**Review queue (`/review`)** — every derived fact set waits here for a human
verdict: **Approve**, **Reject** (with an optional note), or **Edit** (fix the
JSON payload directly; the edited values then take precedence everywhere).
Verdicts persist in `analysis/research.sqlite` and survive pipeline re-runs —
a re-run only resets a verdict when the derived result actually changed.

## Notes

- First view of a CIK does a few SEC requests (metadata + one XBRL cover
  parse); first render of a filing downloads and cleans its full submission
  text. Both are cached, so repeat views are instant.
- `clean_html/` is a shared cache — the notebook, the analyze CLI and the
  viewer all reuse each other's files.
- The local LLM has two backends and the home page shows which one is live:
  an Ollama server, or the **embedded** in-process model whose weights sit at
  `analysis/models/*.gguf` (currently `gemma-2-2b-it-Q4_K_M.gguf`, ~1.7 GB —
  see `analysis/models/README.md`). With neither available the app still
  works: extraction falls back to the deterministic (regex) layers and ICB
  shows the SIC prior at industry level.
- A `SECBlockedError` banner means the SEC 10-minute rate-limit cool-off; wait
  and reload.
