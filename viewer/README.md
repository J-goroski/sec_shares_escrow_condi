# SEC Filing Viewer

A small Flask UI over the pipeline. Enter a **CIK or ticker** and get a
two-pane workspace:

- **Left** — company core info (name, tickers, exchange, SIC, incorporation,
  address), the XBRL cover-page **entity** and **security** facts (tracking
  whichever filing you're viewing), and the recent filings list with direct
  links to the real documents on SEC.gov (`index` / `doc` / `.txt`).
- **Right** — a scrollable, isolated render of the selected filing's **cleaned
  HTML**, generated on demand by `sec_filing_manual_extract.process_submission`
  and cached in `clean_html/` (the same folder the notebook writes to).

It only adds a UI — the pipeline modules are untouched, and all SEC traffic
still goes through the one rate-limited fetcher in `sec_filings_sync`.

## Run

```bash
python viewer/app.py
```

Then open <http://127.0.0.1:5000> and type a CIK (`320193`) or ticker (`AAPL`).

Requires `flask` (already installed in this environment). Click **View** on any
filing to render its cleaned document on the right; the entity/security facts
update to match. A green dot marks filings that carry inline XBRL.

## Notes

- First view of a CIK does a few SEC requests (metadata + one XBRL cover parse);
  first render of a filing downloads and cleans its full submission text. Both
  are cached, so repeat views are instant.
- `clean_html/` is a shared cache — files the notebook generated are reused here,
  and vice-versa.
- A `SECBlockedError` banner means the SEC 10-minute rate-limit cool-off; wait
  and reload.
