"""
viewer/app.py — a small Flask UI over the SEC pipeline.

Enter a CIK (or ticker) and get a two-pane workspace:

  LEFT  — the company's core info (name, tickers, exchange, SIC, address),
          the XBRL cover-page **entity** and **security** facts, and the list
          of recent filings with direct links to the real documents on SEC.gov.
  RIGHT — a scrollable, isolated render of the selected filing's **cleaned HTML**
          (produced on demand by ``sec_filing_manual_extract.process_submission``
          and cached in ``clean_html/`` alongside the notebook's output).

Everything is built from the existing modules — this file adds a UI, it does not
change the pipeline.  All SEC traffic still flows through the one rate-limited
fetcher in ``sec_filings_sync``.

Run
---
    python viewer/app.py           # then open http://127.0.0.1:5000
"""

from __future__ import annotations

import glob
import gzip
import os
import re
import sys

# Make the project root importable whether run from root or from viewer/.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import requests
import pandas as pd
from flask import Flask, request, redirect, render_template_string, Response
from markupsafe import escape

from methods.sec_filings_sync import (
    UA, SECBlockedError, fetch_filings_for_ciks,
    _get_with_retry, _pad_cik, SUBMISSIONS_BASE,
)
from methods.sec_xbrl_extract import parse_filings, cover_pages
from methods.sec_filing_manual_extract import process_submission

app = Flask(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────
CLEAN_DIR    = os.path.join(PROJECT_ROOT, "clean_html")
DEFAULT_FORMS = ["10-K", "10-Q", "20-F", "40-F", "8-K"]
COVER_FORMS   = {"10-K", "10-Q", "20-F", "40-F"}   # forms carrying an XBRL cover
ANNUAL_FORMS  = ["10-K", "20-F", "40-F"]           # richest doc for the viewer
MAX_FILINGS   = 60                                  # cap the filing list length

# ── In-process caches (dev server, single user — plain dicts are fine) ─────────
_filings_cache: dict[str, list] = {}     # cik -> list[FilingRecord]
_header_cache:  dict[str, dict] = {}     # cik -> company header dict
_cover_cache:   dict[str, tuple] = {}    # cik -> (entity_df, security_df, label)
_html_cache:    dict[tuple, str] = {}    # (cik, accession) -> cleaned html
_ticker_map:    dict[str, str] = {}      # TICKER -> cik (lazy-loaded)

_SESSION = requests.Session()
_SESSION.headers.update(UA)


# ══════════════════════════════════════════════════════════════════════════════
#  Data helpers (all reuse the shared rate-limited fetcher)
# ══════════════════════════════════════════════════════════════════════════════

def _company_header(cik: str) -> dict:
    """Core company info from the EDGAR submissions header (cached)."""
    if cik in _header_cache:
        return _header_cache[cik]
    url = f"{SUBMISSIONS_BASE}/submissions/CIK{_pad_cik(cik)}.json"
    try:
        data = _get_with_retry(url, _SESSION, 0.12).json()
    except Exception:
        data = {}
    addr = (data.get("addresses") or {}).get("business") or {}
    header = {
        "cik":          str(int(cik)) if str(cik).isdigit() else cik,
        "name":         data.get("name", "Unknown"),
        "tickers":      data.get("tickers", []) or [],
        "exchanges":    data.get("exchanges", []) or [],
        "sic":          data.get("sicDescription") or data.get("sic"),
        "category":     data.get("category"),
        "entity_type":  data.get("entityType"),
        "incorporation": data.get("stateOfIncorporationDescription"),
        "fiscal_year_end": data.get("fiscalYearEnd"),
        "ein":          data.get("ein"),
        "address":      ", ".join(
            p for p in [addr.get("street1"), addr.get("street2"),
                        addr.get("city"), addr.get("stateOrCountry"),
                        addr.get("zipCode")] if p),
        "phone":        data.get("phone"),
    }
    _header_cache[cik] = header
    return header


def _company_filings(cik: str) -> list:
    """Recent filings for a CIK across the viewer's default form set (cached)."""
    if cik in _filings_cache:
        return _filings_cache[cik]
    filings = fetch_filings_for_ciks([cik], form_types=DEFAULT_FORMS)
    _filings_cache[cik] = filings
    return filings


def _cover_facts(cik: str, filings: list, selected=None) -> tuple:
    """
    (entity_df, security_df, source_label) for the cover page.

    Uses the *selected* filing's cover when it carries XBRL (so the facts on the
    left match the document on the right); otherwise falls back to the most
    recent XBRL cover filing, giving a stable company view for e.g. 8-Ks.
    """
    src = None
    if selected is not None and selected.form_type in COVER_FORMS \
            and selected.xbrl_instance_url:
        src = selected
    else:
        src = next((f for f in filings
                    if f.form_type in COVER_FORMS and f.xbrl_instance_url), None)
    if src is None:
        return (pd.DataFrame(), pd.DataFrame(), None)

    key = (cik, src.accession_number)
    if key in _cover_cache:
        return _cover_cache[key]
    try:
        facts = parse_filings([src], verbose=False)
        entity, security = cover_pages(facts)
        result = (entity, security, f"{src.form_type} filed {src.filing_date}")
    except Exception as exc:                            # noqa: BLE001
        result = (pd.DataFrame(), pd.DataFrame(), f"error: {exc}")
    _cover_cache[key] = result
    return result


def _find_filing(filings: list, accession: str):
    return next((f for f in filings if f.accession_number == accession), None)


def _disk_path_for(accession: str) -> str | None:
    """An existing cleaned-HTML file for this accession (from any prior run)."""
    hits = glob.glob(os.path.join(CLEAN_DIR, f"*{accession}*.clean.html*"))
    return hits[0] if hits else None


def _clean_html_for(cik: str, filing) -> str:
    """Cleaned HTML for a filing — memory cache → disk cache → generate."""
    key = (cik, filing.accession_number)
    if key in _html_cache:
        return _html_cache[key]

    path = _disk_path_for(filing.accession_number)
    if path:
        try:
            opener = gzip.open if path.endswith(".gz") else open
            with opener(path, "rt", encoding="utf-8") as fh:
                html = fh.read()
            _html_cache[key] = html
            return html
        except Exception:
            pass  # fall through to regenerate

    # Generate (and persist to clean_html/) — no need for tables/statements here.
    res = process_submission(
        filing, session=_SESSION, save_dir=CLEAN_DIR, compress=True,
        want_tables=False, want_statements=False)
    html = res.clean_html or "<p style='padding:2rem;font:14px sans-serif'>" \
                             "No HTML primary document in this submission.</p>"
    _html_cache[key] = html
    return html


_HEAD_RE = re.compile(r"(?i)<head[^>]*>")


def _inject_base(html: str, base_href: str) -> str:
    """Insert a <base> so the filing's relative images resolve against SEC.gov."""
    tag = f'<base href="{escape(base_href)}">'
    m = _HEAD_RE.search(html)
    if m:
        return html[:m.end()] + tag + html[m.end():]
    return f"<head>{tag}</head>" + html


def _resolve_query(q: str) -> str | None:
    """Turn a search box value (CIK number or ticker) into a numeric CIK."""
    q = (q or "").strip()
    if not q:
        return None
    if q.isdigit():
        return str(int(q))
    # ticker lookup (lazy-load the ranked ticker file once)
    global _ticker_map
    if not _ticker_map:
        try:
            data = _get_with_retry(
                "https://www.sec.gov/files/company_tickers.json",
                _SESSION, 0.12).json()
            _ticker_map = {v["ticker"].upper(): str(v["cik_str"])
                           for v in data.values()}
        except Exception:
            _ticker_map = {}
    return _ticker_map.get(q.upper())


# ── DataFrame → HTML rendering ────────────────────────────────────────────────

def _entity_kv_html(entity: pd.DataFrame) -> str:
    """Render the 1-row entity facts as a field/value table (non-null only)."""
    if entity is None or entity.empty:
        return "<p class='muted'>No XBRL cover facts for this company.</p>"
    row = entity.iloc[0]
    hide = {"cik", "entity_name", "ticker", "form_type", "filing_date",
            "report_date", "accession_number", "phone_area_code", "phone_local"}
    parts = ["<table class='kv'>"]
    for col, val in row.items():
        if col in hide or pd.isna(val) or val in (None, ""):
            continue
        parts.append(
            f"<tr><th>{escape(col)}</th><td>{escape(str(val))}</td></tr>")
    parts.append("</table>")
    return "".join(parts)


def _security_table_html(security: pd.DataFrame) -> str:
    """Render the security facts as a compact table (curated columns)."""
    if security is None or security.empty:
        return "<p class='muted'>No registered securities in the cover page.</p>"
    cols = [c for c in ["security_class", "security_type", "security_title",
                        "trading_symbol", "exchange", "shares_outstanding"]
            if c in security.columns]
    df = security[cols].copy()
    if "shares_outstanding" in df.columns:
        df["shares_outstanding"] = df["shares_outstanding"].map(
            lambda v: f"{v:,.0f}" if pd.notna(v) else "")
    return df.to_html(index=False, na_rep="", border=0,
                      classes="tbl", justify="left")


# ══════════════════════════════════════════════════════════════════════════════
#  Routes
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template_string(PAGE_HTML, view=None)


@app.route("/go")
def go():
    cik = _resolve_query(request.args.get("q", ""))
    if not cik:
        return render_template_string(
            PAGE_HTML, view=None,
            error=f"Couldn't resolve '{escape(request.args.get('q',''))}' "
                  "to a CIK or ticker.")
    return redirect(f"/cik/{cik}")


@app.route("/cik/<cik>")
def cik_view(cik):
    cik = str(int(cik)) if cik.isdigit() else cik
    try:
        header  = _company_header(cik)
        filings = _company_filings(cik)
    except SECBlockedError:
        return render_template_string(
            PAGE_HTML, view=None,
            error="SEC rate limit hit (10-minute cool-off). Wait and retry.")

    if not filings:
        return render_template_string(
            PAGE_HTML, view=None,
            error=f"No {'/'.join(DEFAULT_FORMS)} filings found for CIK {escape(cik)}.")

    filings = filings[:MAX_FILINGS]

    # Which filing is shown on the right? query ?acc=, else newest annual, else newest.
    acc = request.args.get("acc")
    if not acc or not _find_filing(filings, acc):
        annual = next((f for f in filings if f.form_type in ANNUAL_FORMS), None)
        acc = (annual or filings[0]).accession_number

    selected = _find_filing(filings, acc)
    entity, security, cover_label = _cover_facts(cik, filings, selected)

    rows = [{
        "form":        f.form_type,
        "filing_date": f.filing_date,
        "report_date": f.report_date or "",
        "accession":   f.accession_number,
        "detail_url":  f.filing_detail_url,
        "filing_url":  f.filing_url,
        "txt_url":     f.submission_txt_url,
        "has_xbrl":    bool(f.xbrl_instance_url),
        "selected":    f.accession_number == acc,
    } for f in filings]

    return render_template_string(
        PAGE_HTML, view=True, header=header, rows=rows, cik=cik,
        selected_acc=acc, cover_label=cover_label,
        entity_html=_entity_kv_html(entity),
        security_html=_security_table_html(security),
    )


@app.route("/html/<cik>/<accession>")
def filing_html(cik, accession):
    """The isolated cleaned-HTML document loaded by the right-pane iframe."""
    cik = str(int(cik)) if cik.isdigit() else cik
    filings = _filings_cache.get(cik) or _company_filings(cik)
    filing = _find_filing(filings, accession)
    if filing is None:
        return Response("<p>Unknown filing.</p>", mimetype="text/html")
    try:
        html = _clean_html_for(cik, filing)
    except SECBlockedError:
        return Response("<p style='padding:2rem;font:14px sans-serif'>SEC rate "
                        "limit hit — wait 10 minutes and reload.</p>",
                        mimetype="text/html")
    except Exception as exc:                            # noqa: BLE001
        return Response(f"<p style='padding:2rem;font:14px sans-serif'>"
                        f"Could not build clean HTML: {escape(str(exc))}</p>",
                        mimetype="text/html")
    return Response(_inject_base(html, filing.index_url), mimetype="text/html")


# ── Template ──────────────────────────────────────────────────────────────────

PAGE_HTML = r"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{% if view %}{{ header.name }}{% else %}SEC Filing Viewer{% endif %}</title>
<style>
  :root { --bg:#f4f6f8; --card:#fff; --line:#e2e6ea; --ink:#1c2430; --muted:#6b7684;
          --accent:#2563eb; --accent-soft:#eaf1ff; --slate:#1f2937; }
  * { box-sizing:border-box; }
  html,body { margin:0; height:100%; }
  body { font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
         color:var(--ink); background:var(--bg); }
  a { color:var(--accent); text-decoration:none; } a:hover { text-decoration:underline; }
  .muted { color:var(--muted); }
  code, .mono { font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }

  /* top bar */
  .topbar { display:flex; align-items:center; gap:1rem; padding:.6rem 1rem;
            background:var(--slate); color:#fff; }
  .topbar .brand { font-weight:700; letter-spacing:.02em; white-space:nowrap; }
  .topbar form { margin-left:auto; display:flex; gap:.4rem; }
  .topbar input { padding:.4rem .6rem; border:1px solid #33415522; border-radius:6px;
                  width:220px; font-size:13px; }
  .topbar button { padding:.4rem .8rem; border:0; border-radius:6px;
                   background:var(--accent); color:#fff; cursor:pointer; font-size:13px; }

  /* layout */
  .split { display:flex; height:calc(100vh - 48px); }
  .left { width:44%; max-width:640px; min-width:360px; overflow-y:auto; padding:1rem;
          border-right:1px solid var(--line); }
  .right { flex:1; background:#fff; }
  .right iframe { width:100%; height:100%; border:0; }

  .card { background:var(--card); border:1px solid var(--line); border-radius:10px;
          padding:.9rem 1rem; margin-bottom:1rem; }
  .card h2 { font-size:12px; text-transform:uppercase; letter-spacing:.06em;
             color:var(--muted); margin:0 0 .6rem; }
  .company h1 { font-size:20px; margin:0 0 .3rem; }
  .pill { display:inline-block; padding:.1rem .5rem; border-radius:999px; font-size:12px;
          font-weight:600; background:var(--accent-soft); color:var(--accent);
          margin:0 .3rem .3rem 0; }
  .meta { display:grid; grid-template-columns:auto 1fr; gap:.15rem .8rem;
          font-size:13px; margin-top:.5rem; }
  .meta div:nth-child(odd) { color:var(--muted); }

  table.kv { width:100%; border-collapse:collapse; font-size:13px; }
  table.kv th { text-align:left; color:var(--muted); font-weight:500; padding:.2rem .6rem .2rem 0;
                vertical-align:top; white-space:nowrap; width:1%; }
  table.kv td { padding:.2rem 0; }
  table.tbl { width:100%; border-collapse:collapse; font-size:12.5px; }
  table.tbl th { text-align:left; background:#f7f9fb; border-bottom:2px solid var(--line);
                 padding:.35rem .5rem; }
  table.tbl td { border-bottom:1px solid var(--line); padding:.35rem .5rem; }
  table.tbl tr:hover td { background:#f9fbff; }

  /* filing list */
  .filing { display:flex; align-items:center; gap:.6rem; padding:.5rem .6rem;
            border:1px solid var(--line); border-radius:8px; margin-bottom:.45rem; }
  .filing.sel { border-color:var(--accent); background:var(--accent-soft); }
  .filing .form { font-weight:700; width:52px; }
  .filing .dates { font-size:12px; color:var(--muted); flex:1; }
  .filing .links { display:flex; gap:.5rem; font-size:12px; white-space:nowrap; }
  .filing .view { padding:.25rem .6rem; border-radius:6px; background:var(--accent);
                  color:#fff; font-size:12px; font-weight:600; }
  .filing .view:hover { text-decoration:none; opacity:.9; }
  .xbrl-dot { width:7px; height:7px; border-radius:50%; background:#22c55e; display:inline-block; }
  .empty { max-width:520px; margin:12vh auto; text-align:center; }
  .err { background:#fef2f2; border:1px solid #f6c9c9; color:#9b1c1c;
         padding:.6rem .9rem; border-radius:8px; margin:1rem; }
</style>
</head>
<body>
  <div class="topbar">
    <span class="brand">SEC Filing Viewer</span>
    <form action="/go" method="get">
      <input name="q" placeholder="CIK or ticker (e.g. 320193 or AAPL)" autofocus>
      <button type="submit">Load</button>
    </form>
  </div>

  {% if error %}<div class="err">{{ error }}</div>{% endif %}

  {% if not view %}
    <div class="empty">
      <h1>SEC Filing Viewer</h1>
      <p class="muted">Enter a CIK number or ticker above to load a company's
      core info, XBRL cover facts, filing links, and cleaned filing documents.</p>
      <p class="muted">Try
        <a href="/cik/320193">Apple (320193)</a> ·
        <a href="/cik/789019">Microsoft (789019)</a> ·
        <a href="/cik/1577552">Alibaba (1577552)</a></p>
    </div>
  {% else %}
  <div class="split">
    <div class="left">

      <div class="card company">
        <h1>{{ header.name }}</h1>
        <div>
          {% for t in header.tickers %}<span class="pill">{{ t }}</span>{% endfor %}
          {% if not header.tickers %}<span class="muted">No ticker on file</span>{% endif %}
        </div>
        <div class="meta">
          <div>CIK</div><div class="mono">{{ header.cik }}</div>
          {% if header.exchanges %}<div>Exchange</div><div>{{ header.exchanges|join(', ') }}</div>{% endif %}
          {% if header.sic %}<div>Industry</div><div>{{ header.sic }}</div>{% endif %}
          {% if header.category %}<div>Filer</div><div>{{ header.category }}</div>{% endif %}
          {% if header.incorporation %}<div>Incorporated</div><div>{{ header.incorporation }}</div>{% endif %}
          {% if header.fiscal_year_end %}<div>FY end</div><div>{{ header.fiscal_year_end }}</div>{% endif %}
          {% if header.address %}<div>Address</div><div>{{ header.address }}</div>{% endif %}
          {% if header.phone %}<div>Phone</div><div>{{ header.phone }}</div>{% endif %}
        </div>
      </div>

      <div class="card">
        <h2>Entity facts {% if cover_label %}<span class="muted">— {{ cover_label }}</span>{% endif %}</h2>
        {{ entity_html|safe }}
      </div>

      <div class="card">
        <h2>Security facts</h2>
        {{ security_html|safe }}
      </div>

      <div class="card">
        <h2>Filings <span class="muted">— click “View” to render the cleaned document →</span></h2>
        {% for f in rows %}
          <div class="filing {{ 'sel' if f.selected else '' }}">
            <span class="form pill">{{ f.form }}</span>
            <span class="dates">
              {{ f.filing_date }}{% if f.report_date %} · period {{ f.report_date }}{% endif %}
              {% if f.has_xbrl %}<span class="xbrl-dot" title="inline XBRL"></span>{% endif %}
            </span>
            <span class="links">
              <a href="{{ f.detail_url }}" target="_blank" title="EDGAR filing index">index</a>
              <a href="{{ f.filing_url }}" target="_blank" title="Primary document on SEC.gov">doc</a>
              <a href="{{ f.txt_url }}" target="_blank" title="Full submission .txt">.txt</a>
              <a class="view" href="/cik/{{ cik }}?acc={{ f.accession }}">View</a>
            </span>
          </div>
        {% endfor %}
      </div>

    </div>
    <div class="right">
      <iframe src="/html/{{ cik }}/{{ selected_acc }}" title="cleaned filing"></iframe>
    </div>
  </div>
  {% endif %}
</body>
</html>
"""


if __name__ == "__main__":
    os.makedirs(CLEAN_DIR, exist_ok=True)
    print("SEC Filing Viewer — open http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
