"""
=============================================================================
RIS INTERVIEW PREP — PYTHON / PANDAS / POLARS SYNTAX & LOGIC PRACTICE
=============================================================================
Format: Each block has a comment framing the likely INTERVIEW QUESTION,
followed by the syntax/logic that answers it, followed by a short WHY note
explaining the reasoning (not just the syntax) — practice saying the WHY
out loud, not just reading the code.

Run sections interactively or read top to bottom. Nothing here requires
external data — everything is self-contained with small sample DataFrames.
=============================================================================
"""

import pandas as pd
import numpy as np

try:
    import polars as pl
    HAS_POLARS = True
except ImportError:
    HAS_POLARS = False
    print("polars not installed — pip install polars to run those sections")


# =============================================================================
# SECTION 1: GENERAL PYTHON LOGIC (non-pandas) — likely warmup/sanity checks
# =============================================================================

# Q: "Walk me through how you'd deduplicate a list while preserving order."
def dedupe_preserve_order(items: list) -> list:
    seen = set()
    result = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result
# WHY: set lookup is O(1) avg vs O(n) for "if item not in result" — matters
# at scale. This is the kind of "why this data structure" question likely
# to come up rather than raw algorithm theory.


# Q: "How would you find duplicate security IDs in a dataset efficiently?"
def find_duplicates(items: list):
    seen = set()
    dupes = set()
    for item in items:
        if item in seen:
            dupes.add(item)
        seen.add(item)
    return dupes
# WHY: single pass, O(n) time, O(n) space — good contrast point vs a naive
# nested-loop O(n^2) approach. Worth being ready to explain the tradeoff.


# Q: "Explain list comprehension vs generator expression — when would you
#     use one over the other?"
squares_list = [x**2 for x in range(10)]         # materializes full list in memory
squares_gen = (x**2 for x in range(10))          # lazy, one value at a time
# WHY: generators matter when processing large datasets (e.g., streaming
# rows from a large factor universe) where you don't need everything in
# memory at once — same "why lazy evaluation" reasoning as Polars lazy mode.


# Q: "How would you group and count items without pandas, just core Python?"
def group_count(items: list) -> dict:
    counts = {}
    for item in items:
        counts[item] = counts.get(item, 0) + 1
    return counts
# WHY: dict.get(key, default) avoids KeyError without a try/except —
# a common "clean code" signal interviewers look for.


# Q: "What's the difference between a shallow copy and passing a mutable
#     default argument — why is this a common bug?"
def bad_default(item, bucket=[]):     # BUG: default list is shared across calls
    bucket.append(item)
    return bucket

def good_default(item, bucket=None):  # FIX: create new list each call
    if bucket is None:
        bucket = []
    bucket.append(item)
    return bucket
# WHY: mutable default arguments are evaluated once at function definition,
# not per call — classic "gotcha" question testing real Python fluency,
# not just syntax memorization.


# Q: "How would you handle an error in a pipeline stage without silently
#     swallowing it?" (ties directly to "zero manual review" philosophy)
def process_stage(value):
    try:
        return 100 / value
    except ZeroDivisionError as e:
        raise ValueError(f"Invalid input value=0 in process_stage: {e}") from e
    # NOTE: re-raising as a more specific, contextual error rather than
    # printing and returning None — "fail loudly" pattern.
# WHY: swallowing errors (bare except: pass) is exactly what an autonomous,
# zero-manual-review pipeline can't afford — errors need to surface with
# context, not disappear silently.


# Q: "Explain a decorator and give a use case relevant to a data pipeline."
import time
import functools

def timer(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        start = time.perf_counter()
        result = func(*args, **kwargs)
        elapsed = time.perf_counter() - start
        print(f"{func.__name__} took {elapsed:.4f}s")
        return result
    return wrapper

@timer
def slow_computation(n):
    return sum(i**2 for i in range(n))
# WHY: decorators are a clean way to add cross-cutting concerns (timing,
# logging, validation) to every pipeline stage without repeating code —
# directly relevant to a multi-stage production pipeline.


# =============================================================================
# SECTION 2: PANDAS — CORE SYNTAX & COMMON PATTERNS
# =============================================================================

sample_df = pd.DataFrame({
    "security_id": ["A1", "A2", "A3", "A4", "A5", "A6"],
    "sector": ["Tech", "Tech", "Financials", "Financials", "Energy", "Energy"],
    "market_cap": [500, 300, 800, 200, 150, 900],
    "pe_ratio": [25.0, np.nan, 15.0, 18.0, 40.0, 12.0],
    "return_pct": [0.05, -0.02, 0.01, 0.03, -0.10, 0.07],
})

# Q: "How would you check for and handle missing data in a factor input?"
null_counts = sample_df.isnull().sum()
null_pct = sample_df.isnull().mean()
df_dropped = sample_df.dropna(subset=["pe_ratio"])
df_filled = sample_df.fillna({"pe_ratio": sample_df["pe_ratio"].median()})
# WHY: median fill is more robust to outliers than mean for financial ratios
# (P/E can have extreme skew) — be ready to justify median over mean here.


# Q: "Why avoid .apply() with axis=1 for large DataFrames? Show the fix."
# ANTI-PATTERN (slow — row-by-row Python loop under the hood):
sample_df["capped_return_slow"] = sample_df.apply(
    lambda row: min(row["return_pct"], 0.05), axis=1
)
# VECTORIZED (fast — operates on the whole column at once via NumPy):
sample_df["capped_return_fast"] = sample_df["return_pct"].clip(upper=0.05)
# WHY: .apply(axis=1) essentially loops in Python and forfeits vectorization;
# clip/where/np.select operate at the C level across the whole array.


# Q: "How would you rank securities within each sector by a factor value?"
sample_df["sector_rank"] = (
    sample_df.groupby("sector")["market_cap"]
    .rank(ascending=False, method="min")
)
# WHY: groupby + rank is the pandas equivalent of the SQL PARTITION BY /
# RANK() OVER pattern — sector-neutral construction logic.


# Q: "How would you compute a z-score within groups (sector-neutral scoring)?"
sample_df["sector_zscore"] = sample_df.groupby("sector")["pe_ratio"].transform(
    lambda x: (x - x.mean()) / x.std()
)
# WHY: .transform() returns a same-length Series aligned back to the
# original index — different from .agg(), which collapses groups down.
# Knowing transform vs agg vs apply is a common conceptual gotcha.


# Q: "Explain the difference between .transform(), .agg(), and .apply()
#     in a groupby context."
agg_result = sample_df.groupby("sector")["market_cap"].agg(["mean", "sum", "count"])
# .agg()       -> collapses each group to a single row (summary stats)
# .transform() -> broadcasts a result back to original row shape
# .apply()     -> most flexible/slowest, can return scalar, Series, or DataFrame
# WHY: choosing the tightest-scoped method (agg over apply) is both a
# performance signal and a "do you actually understand the API" signal.


# Q: "How would you merge two vendor datasets and flag mismatches?"
vendor_a = pd.DataFrame({"security_id": ["A1", "A2", "A3"], "value_a": [100, 200, 300]})
vendor_b = pd.DataFrame({"security_id": ["A1", "A2", "A4"], "value_b": [101, 250, 400]})

merged = vendor_a.merge(vendor_b, on="security_id", how="outer", indicator=True)
merged["pct_diff"] = (merged["value_a"] - merged["value_b"]).abs() / merged["value_a"]
mismatches = merged[merged["pct_diff"] > 0.05]
unmatched = merged[merged["_merge"] != "both"]
# WHY: outer join + indicator=True surfaces BOTH value mismatches AND
# securities missing entirely from one vendor — a fail-loudly validation
# habit, not just an inner join that silently drops unmatched rows.


# Q: "How would you handle a rolling/trailing window calculation
#     (e.g., 3-period rolling average return)?"
ts_df = pd.DataFrame({
    "date": pd.date_range("2026-01-01", periods=6, freq="D"),
    "return_pct": [0.01, 0.02, -0.01, 0.03, 0.00, -0.02],
})
ts_df["rolling_avg_3d"] = ts_df["return_pct"].rolling(window=3).sum()
ts_df["rolling_vol_3d"] = ts_df["return_pct"].rolling(window=3).std()
# WHY: rolling() respects chronological order — critical for time series
# where using future data in a "rolling" window would be look-ahead bias.


# Q: "How would you pivot long-format factor data into a wide security x
#     date matrix?"
long_df = pd.DataFrame({
    "security_id": ["A1", "A1", "A2", "A2"],
    "date": ["2026-01-01", "2026-01-02", "2026-01-01", "2026-01-02"],
    "factor_value": [1.2, 1.3, 0.8, 0.9],
})
wide_df = long_df.pivot(index="date", columns="security_id", values="factor_value")
# WHY: pivot is common prep before matrix-style operations (correlation
# matrices, covariance for beta calc) — ties back to the alpha/beta material.


# Q: "How would you validate that a factor score conforms to expectations
#     before it's allowed downstream — fail loudly, not silently?"
def validate_factor_scores(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        raise ValueError("Factor score DataFrame is empty")
    if df["sector_zscore"].isnull().mean() > 0.05:
        raise ValueError("Null rate in sector_zscore exceeds 5% threshold")
    if not df["sector_zscore"].between(-5, 5).all():
        bad_ids = df.loc[~df["sector_zscore"].between(-5, 5), "security_id"].tolist()
        raise ValueError(f"Z-scores out of expected range for: {bad_ids}")
    return df
# WHY: this is the "confidence scoring is the control mechanism" philosophy
# expressed as code — validate structurally before anything downstream
# trusts the data, and raise with enough context to debug immediately.


# =============================================================================
# SECTION 3: POLARS — CORE SYNTAX, MIRRORING THE PANDAS PATTERNS ABOVE
# =============================================================================

if HAS_POLARS:

    pl_df = pl.DataFrame({
        "security_id": ["A1", "A2", "A3", "A4", "A5", "A6"],
        "sector": ["Tech", "Tech", "Financials", "Financials", "Energy", "Energy"],
        "market_cap": [500, 300, 800, 200, 150, 900],
        "pe_ratio": [25.0, None, 15.0, 18.0, 40.0, 12.0],
        "return_pct": [0.05, -0.02, 0.01, 0.03, -0.10, 0.07],
    })

    # Q: "Why is Polars considered faster than pandas — show it, don't just
    #     say it."
    # EAGER (pandas-style, executes immediately, no query optimization):
    eager_result = pl_df.filter(pl.col("market_cap") > 200).select(
        ["security_id", "market_cap"]
    )

    # LAZY (Polars-native — builds a query plan, optimizes, then executes):
    lazy_result = (
        pl_df.lazy()
        .filter(pl.col("market_cap") > 200)
        .select(["security_id", "market_cap"])
        .collect()
    )
    # WHY: .lazy() lets Polars apply predicate pushdown and projection
    # pushdown — e.g., it can push the filter down to the scan itself and
    # only read needed columns, rather than materializing everything first
    # then filtering. This is the substantive "why," not just "it's Rust."


    # Q: "How would you compute a sector-neutral z-score in Polars?"
    pl_df_z = pl_df.with_columns(
        ((pl.col("pe_ratio") - pl.col("pe_ratio").mean().over("sector"))
         / pl.col("pe_ratio").std().over("sector")).alias("sector_zscore")
    )
    # WHY: .over("sector") is Polars' equivalent of pandas groupby().transform()
    # — computes the aggregate per group but returns a column aligned to the
    # original row count, all within a single expression chain.


    # Q: "How would you rank within groups in Polars?"
    pl_df_rank = pl_df.with_columns(
        pl.col("market_cap").rank(method="min", descending=True).over("sector").alias("sector_rank")
    )
    # WHY: same .over() pattern — Polars' expression API composes rank(),
    # mean(), std(), sum() etc. uniformly with .over() for group-wise ops.


    # Q: "How would you handle nulls in Polars — show a couple of approaches."
    pl_null_handling = pl_df.with_columns(
        pl.col("pe_ratio").fill_null(pl.col("pe_ratio").median()).alias("pe_ratio_filled")
    )
    pl_dropped = pl_df.drop_nulls(subset=["pe_ratio"])
    # WHY: fill_null / drop_nulls mirror pandas fillna/dropna conceptually,
    # but are expressions — they compose inside a lazy query plan instead
    # of executing eagerly line by line.


    # Q: "How would you join two vendor datasets and flag mismatches in Polars?"
    pl_vendor_a = pl.DataFrame({"security_id": ["A1", "A2", "A3"], "value_a": [100, 200, 300]})
    pl_vendor_b = pl.DataFrame({"security_id": ["A1", "A2", "A4"], "value_b": [101, 250, 400]})

    pl_merged = pl_vendor_a.join(pl_vendor_b, on="security_id", how="full", coalesce=True)
    pl_merged = pl_merged.with_columns(
        ((pl.col("value_a") - pl.col("value_b")).abs() / pl.col("value_a")).alias("pct_diff")
    )
    pl_mismatches = pl_merged.filter(pl.col("pct_diff") > 0.05)
    # WHY: how="full" is Polars' outer join — same reasoning as pandas:
    # don't silently drop unmatched securities, surface them.


    # Q: "How would you do a rolling calculation in Polars?"
    pl_ts = pl.DataFrame({
        "date": pl.date_range(pl.date(2026, 1, 1), pl.date(2026, 1, 6), "1d", eager=True),
        "return_pct": [0.01, 0.02, -0.01, 0.03, 0.00, -0.02],
    })
    pl_ts = pl_ts.with_columns(
        pl.col("return_pct").rolling_sum(window_size=3).alias("rolling_sum_3d"),
        pl.col("return_pct").rolling_std(window_size=3).alias("rolling_vol_3d"),
    )
    # WHY: rolling_* functions are expressions too — composable with filter,
    # with_columns, group_by in the same lazy chain, unlike pandas where
    # rolling() returns an intermediate Rolling object you then aggregate.


    # Q: "Full end-to-end: build a lazy Polars pipeline that reads, filters,
    #     scores, and flags anomalies in one chain — why would you do this
    #     as one chain instead of step-by-step eager code?"
    def build_lazy_pipeline(lf: "pl.LazyFrame") -> "pl.LazyFrame":
        return (
            lf
            .filter(pl.col("market_cap") > 0)
            .with_columns(
                ((pl.col("pe_ratio") - pl.col("pe_ratio").mean().over("sector"))
                 / pl.col("pe_ratio").std().over("sector")).alias("sector_zscore")
            )
            .with_columns(
                (pl.col("sector_zscore").abs() > 3).alias("is_anomaly")
            )
        )

    result = build_lazy_pipeline(pl_df.lazy()).collect()
    # WHY: chaining as a single lazy pipeline lets the query optimizer see
    # the FULL plan before running anything — it can reorder filters,
    # skip unused columns, and avoid materializing intermediate results.
    # Eager step-by-step code forces full materialization at every line.


# =============================================================================
# SECTION 4: LIKELY "EXPLAIN THE TRADEOFF" PROMPTS (talk-through only, no code)
# =============================================================================
"""
Q: "When would you still reach for pandas instead of Polars?"
A: Small datasets where performance doesn't matter, heavy reliance on a
   pandas-only library/ecosystem integration (e.g., certain plotting or
   stats libraries expect pandas DataFrames), or team/codebase consistency
   during a migration — not everything needs to move at once.

Q: "What's a risk of blindly trusting a vectorized operation over a loop?"
A: Vectorization assumes the operation is well-defined across the whole
   array (e.g., division by zero, NaN propagation) — silent NaN/inf
   propagation can hide errors a loop with explicit checks might catch.
   This is why validation steps still matter even with vectorized code.

Q: "Dict vs list — when does the choice actually matter in this context?"
A: Dict for O(1) lookups by security_id (e.g., checking if an ID is in a
   monitored/exclusion list); list when order matters or when the full
   sequential lineage of items needs to be preserved (e.g., historical
   record of daily reconstitution changes).

Q: "Why does chronological-only cross-validation matter for time series,
    stated in your own words?"
A: Random-split CV lets the model see 'future' data during training,
   which never happens in live deployment — this inflates validation
   performance and hides a model that would actually fail out-of-sample.
"""

print("Interview prep file loaded. Read top to bottom, or step through in a REPL.")
