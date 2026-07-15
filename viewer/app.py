"""
viewer/app.py — the EDGAR Research Desk (Flask).

Three views over the pipeline:

  Home      — dashboard: search, pipeline status, stats over the research
              store, latest derived facts.
  Company   — the original two-pane workspace (core info, XBRL cover facts,
              filing list on the left; cleaned filing HTML on the right) plus
              the company's DERIVED FACTS (ICB / voting rights / ADR ratio)
              with evidence, confidence and review status.  "Run analysis"
              executes the full pipeline for the company in a background
              thread and the page live-refreshes until it lands.
  Review    — the analyst queue: approve / reject / edit every derived fact
              set; verdicts persist in analysis/research.sqlite and survive
              pipeline re-runs (unchanged results never reset a verdict).

Everything is built from the existing modules — this file adds UI + workflow,
it does not change the pipeline.  All SEC traffic flows through the one
rate-limited fetcher in ``sec_filings_sync``; all LLM calls flow through
``analysis.ollama_client`` (and the app degrades gracefully without Ollama).

Run
---
    python viewer/app.py           # then open http://127.0.0.1:5000
"""

from __future__ import annotations

import glob
import gzip
import json
import os
import re
import sys
import threading

# Make the project root importable whether run from root or from viewer/.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import requests
import pandas as pd
from flask import (Flask, request, redirect, render_template, Response,
                   flash, url_for)
from markupsafe import escape

from methods.sec_filings_sync import (
    UA, SECBlockedError, fetch_filings_for_ciks,
    _get_with_retry, _pad_cik, SUBMISSIONS_BASE,
)
from methods.sec_xbrl_extract import parse_filings, cover_pages
from methods.sec_filing_manual_extract import process_submission
from analysis.research_store import ResearchDB
from analysis.analyze import analyze_company
from analysis.ollama_client import backend_info

app = Flask(__name__)
app.secret_key = "edgar-research-desk-dev"      # local dev tool only

# ── Configuration ─────────────────────────────────────────────────────────────
CLEAN_DIR    = os.path.join(PROJECT_ROOT, "clean_html")
DEFAULT_FORMS = ["10-K", "10-Q", "20-F", "40-F", "8-K"]
COVER_FORMS   = {"10-K", "10-Q", "20-F", "40-F"}   # forms carrying an XBRL cover
ANNUAL_FORMS  = ["10-K", "20-F", "40-F"]           # richest doc for the viewer
MAX_FILINGS   = 60                                  # cap the filing list length
SAMPLES = [("AAPL", "320193"), ("SHEL", "1306965"), ("META", "1326801"),
           ("GOOGL", "1652044"), ("KO", "21344"), ("TSM", "1046179"),
           ("BRK-B", "1067983"), ("BABA", "1577552")]

# ── In-process caches (dev server, single user — plain dicts are fine) ─────────
_filings_cache: dict[str, list] = {}     # cik -> list[FilingRecord]
_header_cache:  dict[str, dict] = {}     # cik -> company header dict
_cover_cache:   dict[str, tuple] = {}    # cik -> (entity_df, security_df, label)
_html_cache:    dict[tuple, str] = {}    # (cik, accession) -> cleaned html
_ticker_map:    dict[str, str] = {}      # TICKER -> cik (lazy-loaded)
_analysis_running: dict[str, bool] = {}  # cik -> pipeline thread live

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
        if facts.empty:
            raise ValueError("no XBRL facts parsed")
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


# ── Research-store helpers ────────────────────────────────────────────────────

def _headline_for(kind: str, payload) -> str:
    """One-line summary of a derived payload for lists and cards."""
    try:
        if kind == "icb" and isinstance(payload, dict):
            parts = [payload.get("industry"), payload.get("sector"),
                     payload.get("subsector")]
            parts = [p for p in parts if p]
            dedup = [p for i, p in enumerate(parts)
                     if i == 0 or p != parts[i - 1]]
            return " › ".join(dedup) or "unclassified"
        if kind == "voting" and isinstance(payload, list):
            bits = []
            for r in payload:
                v = r.get("votes_per_share")
                bits.append(f"{r.get('class_label', '?')} "
                            f"{('%g' % v) if v is not None else '?'}")
            return " · ".join(bits) or "no classes"
        if kind == "adr" and isinstance(payload, list):
            return " · ".join(r.get("ratio_display") or "ratio unknown"
                              for r in payload) or "no ADS"
    except Exception:                                   # noqa: BLE001
        pass
    return "(unrenderable payload)"


def _factsets(rows: list[dict]) -> list[dict]:
    out = []
    for row in rows:
        payload = ResearchDB.payload(row)
        out.append({"row": row, "payload": payload,
                    "headline": _headline_for(row["kind"], payload),
                    "pretty": json.dumps(payload, indent=2,
                                         ensure_ascii=False)})
    return out


def _run_analysis_bg(cik: str) -> None:
    """Background pipeline run (own session + own DB connection)."""
    try:
        analyze_company(cik, clean_dir=CLEAN_DIR, verbose=False)
    except Exception:                                   # noqa: BLE001
        pass                    # errors surface as 'nothing derived'
    finally:
        _analysis_running.pop(cik, None)


@app.context_processor
def _inject_globals():
    try:
        db = ResearchDB()
        pending = db.stats().get("pending", 0)
        db.close()
    except Exception:                                   # noqa: BLE001
        pending = 0
    return {"pending_count": pending}


# ══════════════════════════════════════════════════════════════════════════════
#  Routes
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def home():
    db = ResearchDB()
    try:
        stats = db.stats()
        recent = _factsets(db.recent(14))
        recent = [{**f["row"], "headline": f["headline"]} for f in recent]
        companies = db.companies()
    finally:
        db.close()
    return render_template(
        "home.html", nav="home", stats=stats, recent=recent,
        companies=companies, samples=SAMPLES, llm=backend_info())


@app.route("/go")
def go():
    cik = _resolve_query(request.args.get("q", ""))
    if not cik:
        flash(f"Couldn't resolve '{request.args.get('q', '')}' to a CIK "
              "or ticker.", "error")
        return redirect(url_for("home"))
    return redirect(f"/cik/{cik}")


@app.route("/cik/<cik>")
def cik_view(cik):
    cik = str(int(cik)) if cik.isdigit() else cik
    try:
        header  = _company_header(cik)
        filings = _company_filings(cik)
    except SECBlockedError:
        flash("SEC rate limit hit (10-minute cool-off). Wait and retry.",
              "error")
        return redirect(url_for("home"))

    if not filings:
        flash(f"No {'/'.join(DEFAULT_FORMS)} filings found for CIK {cik}.",
              "error")
        return redirect(url_for("home"))

    filings = filings[:MAX_FILINGS]

    # Which filing is shown on the right? query ?acc=, else newest annual, else newest.
    acc = request.args.get("acc")
    if not acc or not _find_filing(filings, acc):
        annual = next((f for f in filings if f.form_type in ANNUAL_FORMS), None)
        acc = (annual or filings[0]).accession_number

    selected = _find_filing(filings, acc)
    entity, security, cover_label = _cover_facts(cik, filings, selected)

    db = ResearchDB()
    try:
        factsets = _factsets(db.for_company(cik))
    finally:
        db.close()

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

    return render_template(
        "company.html", nav=None, header=header, rows=rows, cik=cik,
        selected_acc=acc, cover_label=cover_label,
        entity_html=_entity_kv_html(entity),
        security_html=_security_table_html(security),
        factsets=factsets,
        analysis_running=cik in _analysis_running)


@app.route("/analyze/<cik>", methods=["POST"])
def analyze_route(cik):
    cik = str(int(cik)) if cik.isdigit() else cik
    if cik not in _analysis_running:
        _analysis_running[cik] = True
        threading.Thread(target=_run_analysis_bg, args=(cik,),
                         daemon=True).start()
        flash("Analysis started — ICB, voting rights and ADR ratios are "
              "being derived from the latest annual report.")
    return redirect(f"/cik/{cik}")


@app.route("/review")
def review():
    status = request.args.get("status", "pending")
    if status not in ("pending", "approved", "rejected", "edited"):
        status = "pending"
    db = ResearchDB()
    try:
        stats = db.stats()
        items = _factsets(db.queue(status=status))
    finally:
        db.close()
    tab_counts = [(s, stats.get(s, 0))
                  for s in ("pending", "approved", "rejected", "edited")]
    return render_template("review.html", nav="review", status=status,
                           items=items, tab_counts=tab_counts)


@app.route("/review/<int:analysis_id>", methods=["POST"])
def review_action(analysis_id):
    action = request.form.get("action", "")
    note = (request.form.get("note") or "").strip() or None
    back = request.form.get("back", "pending")
    db = ResearchDB()
    try:
        row = db.get(analysis_id)
        if row is None:
            flash("Unknown analysis id.", "error")
        elif action == "approve":
            db.review(analysis_id, "approved", note=note)
            flash(f"Approved {row['ticker'] or row['cik']} / {row['kind']}.")
        elif action == "reject":
            db.review(analysis_id, "rejected", note=note)
            flash(f"Rejected {row['ticker'] or row['cik']} / {row['kind']}.")
        elif action == "edit":
            try:
                edited = json.loads(request.form.get("edited_json", ""))
            except json.JSONDecodeError as exc:
                flash(f"Edit not saved — invalid JSON: {exc}", "error")
                return redirect(url_for("review", status=back))
            db.review(analysis_id, "edited", note=note, edited_result=edited)
            flash(f"Saved edited values for {row['ticker'] or row['cik']} / "
                  f"{row['kind']}.")
        else:
            flash("Unknown action.", "error")
    finally:
        db.close()
    return redirect(url_for("review", status=back))


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


if __name__ == "__main__":
    os.makedirs(CLEAN_DIR, exist_ok=True)
    print("EDGAR Research Desk - open http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
