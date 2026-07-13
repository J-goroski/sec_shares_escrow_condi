"""
country_assignment — assign & monitor each company's country of incorporation
and principal-executive-office (HQ) from SEC data.

Modules
-------
* ``codes``                 — EDGAR (non-ISO) state/country code tables, loaded
                              from CSVs in ``data/``; ``decode_code`` / ``country_of``.
* ``incorporation_validate``— cross-validate ONE filing's incorporation + HQ
                              across XBRL / SEC-HEADER / cover text (dual-HQ,
                              ISO-collision, XBRL-wrong detection).
* ``edgar_profile``         — pull the CURRENT authoritative registrant profile
                              from the EDGAR submissions API (the monthly source
                              of truth); per-CIK or bulk ``submissions.zip``.
* ``monthly_monitor``       — build a consolidated country assignment per CIK
                              (profile + latest-filing validation incl. 6-K),
                              snapshot monthly, and diff vs the prior month.
"""

from methods.country_assignment.codes import (
    US_STATES, CA_PROVINCES, EDGAR_COUNTRY, ISO_COLLISION,
    decode_code, country_of, text_to_country, reload_tables,
)
from methods.country_assignment.incorporation_validate import (
    IncorporationCheck, validate_filing, validate_filings,
    parse_sec_header, extract_cover_incorporation_address,
)
from methods.country_assignment.edgar_profile import (
    fetch_company_profile, fetch_profiles, profile_from_submissions,
    iter_bulk_profiles,
)
from methods.country_assignment.monthly_monitor import (
    CountryAssignment, build_assignment, run_monthly,
    diff_snapshots, load_latest_snapshot,
)

__all__ = [
    # codes
    "US_STATES", "CA_PROVINCES", "EDGAR_COUNTRY", "ISO_COLLISION",
    "decode_code", "country_of", "text_to_country", "reload_tables",
    # incorporation validation (filing-level)
    "IncorporationCheck", "validate_filing", "validate_filings",
    "parse_sec_header", "extract_cover_incorporation_address",
    # current-value pull
    "fetch_company_profile", "fetch_profiles", "profile_from_submissions",
    "iter_bulk_profiles",
    # monthly monitoring
    "CountryAssignment", "build_assignment", "run_monthly",
    "diff_snapshots", "load_latest_snapshot",
]
