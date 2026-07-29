/*
=============================================================================
RIS INTERVIEW PREP — MSSQL SYNTAX & LOGIC PRACTICE
=============================================================================
Format: Each block has a comment framing the likely INTERVIEW QUESTION,
followed by the syntax/logic that answers it, followed by a short WHY note
explaining the reasoning (not just the syntax) — practice saying the WHY
out loud, not just reading the query.

Sample schema used throughout (create these to actually run the queries):

CREATE TABLE securities (
    security_id   VARCHAR(10)   PRIMARY KEY,
    sector        VARCHAR(50),
    market_cap    DECIMAL(18,2),
    pe_ratio      DECIMAL(10,2) NULL
);

CREATE TABLE factor_scores (
    security_id   VARCHAR(10),
    as_of_date    DATE,
    factor_value  DECIMAL(10,4),
    PRIMARY KEY (security_id, as_of_date)
);

CREATE TABLE vendor_a_data (
    security_id   VARCHAR(10),
    as_of_date    DATE,
    value         DECIMAL(18,4)
);

CREATE TABLE vendor_b_data (
    security_id   VARCHAR(10),
    as_of_date    DATE,
    value         DECIMAL(18,4)
);
=============================================================================
*/


-- =============================================================================
-- SECTION 1: BASIC FILTERING, NULL HANDLING, CASE LOGIC — likely warmup
-- =============================================================================

-- Q: "How would you handle NULLs in a WHERE clause or output column?"
SELECT
    security_id,
    pe_ratio,
    ISNULL(pe_ratio, 0)                        AS pe_ratio_zero_filled,
    COALESCE(pe_ratio, 0)                      AS pe_ratio_coalesced
FROM securities
WHERE pe_ratio IS NOT NULL;   -- NOTE: pe_ratio = NULL never matches — NULL requires IS NULL
-- WHY: ISNULL is SQL Server-specific and only takes 2 args; COALESCE is
-- ANSI-standard and takes N args, returning the first non-null — COALESCE
-- is generally the safer default to reach for.


-- Q: "Write a query that buckets securities by P/E ratio into readable tiers."
SELECT
    security_id,
    pe_ratio,
    CASE
        WHEN pe_ratio IS NULL      THEN 'Unknown'
        WHEN pe_ratio < 15         THEN 'Value'
        WHEN pe_ratio BETWEEN 15 AND 25 THEN 'Core'
        ELSE 'Growth'
    END AS pe_tier
FROM securities;
-- WHY: always handle the NULL case explicitly and first in a CASE chain —
-- silently falling through to ELSE for NULLs can misclassify missing data
-- as a real category, which is exactly the kind of silent-failure bug a
-- zero-manual-review pipeline can't tolerate.


-- Q: "How would you find duplicate security_id + as_of_date combinations
--     (a data integrity check before load)?"
SELECT security_id, as_of_date, COUNT(*) AS dupe_count
FROM factor_scores
GROUP BY security_id, as_of_date
HAVING COUNT(*) > 1;
-- WHY: HAVING filters on aggregated results (post-GROUP BY), whereas WHERE
-- filters rows before aggregation — a common conceptual gotcha question.


-- =============================================================================
-- SECTION 2: WINDOW FUNCTIONS — sector-neutral scoring, ranking (core to role)
-- =============================================================================

-- Q: "How would you rank securities within each sector by market cap?"
SELECT
    security_id,
    sector,
    market_cap,
    RANK()       OVER (PARTITION BY sector ORDER BY market_cap DESC) AS sector_rank,
    ROW_NUMBER() OVER (PARTITION BY sector ORDER BY market_cap DESC) AS sector_row_num,
    NTILE(5)     OVER (PARTITION BY sector ORDER BY market_cap DESC) AS sector_quintile
FROM securities;
-- WHY: RANK() leaves gaps after ties (1,1,3), ROW_NUMBER() never ties
-- (1,2,3 regardless), NTILE buckets into N groups regardless of ties —
-- choosing the wrong one silently changes factor index construction,
-- e.g., quintile-based long/short portfolios need NTILE, not RANK.


-- Q: "How would you compute a sector-neutral z-score in SQL?"
SELECT
    security_id,
    sector,
    pe_ratio,
    (pe_ratio - AVG(pe_ratio) OVER (PARTITION BY sector))
        / NULLIF(STDEV(pe_ratio) OVER (PARTITION BY sector), 0) AS sector_zscore
FROM securities;
-- WHY: NULLIF(x, 0) guards against divide-by-zero when a sector has zero
-- variance (e.g., only one security, or identical values) — this is the
-- SQL-level equivalent of the "fail loudly, don't crash silently" habit;
-- NULLIF returns NULL instead of throwing a divide-by-zero error, so the
-- downstream code can catch/flag it explicitly.


-- Q: "How would you compute a running/cumulative total or trailing average?"
SELECT
    security_id,
    as_of_date,
    factor_value,
    AVG(factor_value) OVER (
        PARTITION BY security_id
        ORDER BY as_of_date
        ROWS BETWEEN 2 PRECEDING AND CURRENT ROW
    ) AS rolling_avg_3d,
    SUM(factor_value) OVER (
        PARTITION BY security_id
        ORDER BY as_of_date
        ROWS UNBOUNDED PRECEDING
    ) AS running_total
FROM factor_scores;
-- WHY: ROWS BETWEEN ... PRECEDING AND CURRENT ROW is explicitly bounded to
-- the past — this is the SQL enforcement of point-in-time integrity
-- (no look-ahead bias), directly analogous to pandas/Polars rolling().


-- Q: "How would you get the previous period's value for a change/return
--     calculation (period-over-period)?"
SELECT
    security_id,
    as_of_date,
    factor_value,
    LAG(factor_value, 1) OVER (PARTITION BY security_id ORDER BY as_of_date) AS prior_value,
    factor_value - LAG(factor_value, 1) OVER (PARTITION BY security_id ORDER BY as_of_date) AS change
FROM factor_scores;
-- WHY: LAG/LEAD avoid a self-join for period-over-period comparisons —
-- cleaner and typically faster than joining the table to itself on a
-- date-offset condition.


-- =============================================================================
-- SECTION 3: POINT-IN-TIME / AS-OF-DATE PATTERNS (backtesting integrity)
-- =============================================================================

-- Q: "How would you retrieve the latest factor value as of a given
--     rebalance date, without leaking future data?"

-- Approach A: correlated subquery
SELECT f.security_id, f.factor_value, f.as_of_date
FROM factor_scores f
WHERE f.as_of_date = (
    SELECT MAX(f2.as_of_date)
    FROM factor_scores f2
    WHERE f2.security_id = f.security_id
      AND f2.as_of_date <= @RebalanceDate
);

-- Approach B: window function (often more efficient than a correlated subquery)
SELECT security_id, factor_value, as_of_date
FROM (
    SELECT
        security_id,
        factor_value,
        as_of_date,
        ROW_NUMBER() OVER (
            PARTITION BY security_id
            ORDER BY as_of_date DESC
        ) AS rn
    FROM factor_scores
    WHERE as_of_date <= @RebalanceDate
) ranked
WHERE rn = 1;
-- WHY: a correlated subquery re-executes per outer row (can be slow at
-- scale); the ROW_NUMBER version scans once and filters — worth being
-- ready to explain WHY you'd prefer Approach B for a large universe, even
-- though both are logically correct.


-- =============================================================================
-- SECTION 4: JOINS & CROSS-VENDOR VALIDATION (multi-vendor reconciliation)
-- =============================================================================

-- Q: "How would you compare two vendor sources and flag both value
--     mismatches AND securities missing from one side?"
SELECT
    COALESCE(a.security_id, b.security_id) AS security_id,
    a.value AS vendor_a_value,
    b.value AS vendor_b_value,
    CASE
        WHEN a.security_id IS NULL THEN 'Missing from Vendor A'
        WHEN b.security_id IS NULL THEN 'Missing from Vendor B'
        WHEN ABS(a.value - b.value) / NULLIF(a.value, 0) > 0.05 THEN 'Value Mismatch'
        ELSE 'OK'
    END AS validation_flag
FROM vendor_a_data a
FULL OUTER JOIN vendor_b_data b
    ON a.security_id = b.security_id
   AND a.as_of_date  = b.as_of_date
WHERE a.as_of_date = @RebalanceDate OR b.as_of_date = @RebalanceDate;
-- WHY: FULL OUTER JOIN surfaces every discrepancy type in one query —
-- an INNER JOIN would silently drop any security missing from either
-- vendor, which is exactly the kind of silent data loss a validation
-- layer exists to prevent.


-- Q: "What's the difference between INNER, LEFT, and FULL OUTER JOIN —
--     and when does the choice actually change your results here?"
/*
INNER JOIN      -> only rows matching in both tables (drops unmatched — risky for validation)
LEFT JOIN       -> all rows from left table, NULLs for unmatched right rows
FULL OUTER JOIN -> all rows from both tables, NULLs wherever there's no match
For cross-vendor validation specifically, FULL OUTER is usually correct —
you WANT to know about anything missing from either side, not just
matched pairs.
*/


-- =============================================================================
-- SECTION 5: CTEs vs TEMP TABLES vs SUBQUERIES
-- =============================================================================

-- Q: "When would you use a CTE vs a temp table vs a subquery?"
WITH sector_avg AS (
    SELECT sector, AVG(pe_ratio) AS avg_pe
    FROM securities
    GROUP BY sector
)
SELECT s.security_id, s.sector, s.pe_ratio, sa.avg_pe
FROM securities s
JOIN sector_avg sa ON s.sector = sa.sector;
-- WHY: CTEs are readable and scoped to a single statement — good for
-- breaking a complex query into logical steps. Temp tables (#temp) persist
-- for the session and can be indexed — better when the same intermediate
-- result is reused across multiple queries or is large enough that
-- materializing it once is worth the I/O. Subqueries are fine inline but
-- get unreadable once nested more than 1-2 levels — CTEs are usually the
-- more maintainable choice at that point.


-- Q: "Write a multi-step CTE that filters, scores, and flags anomalies
--     in one readable query — why chain CTEs instead of one giant nested
--     subquery?"
WITH filtered AS (
    SELECT * FROM securities WHERE market_cap > 0
),
scored AS (
    SELECT
        *,
        (pe_ratio - AVG(pe_ratio) OVER (PARTITION BY sector))
            / NULLIF(STDEV(pe_ratio) OVER (PARTITION BY sector), 0) AS sector_zscore
    FROM filtered
)
SELECT
    security_id,
    sector,
    sector_zscore,
    CASE WHEN ABS(sector_zscore) > 3 THEN 1 ELSE 0 END AS is_anomaly
FROM scored;
-- WHY: each CTE is a named, testable logical step (filter -> score -> flag)
-- — same reasoning as the class-based Python pipeline stages: readability
-- and the ability to reason about (or debug) one step at a time.


-- =============================================================================
-- SECTION 6: ERROR HANDLING & TRANSACTIONS (production reliability)
-- =============================================================================

-- Q: "How would you structure a data load so a failure doesn't leave the
--     table in a half-updated state — 'fail loudly,' not silently?"
BEGIN TRY
    BEGIN TRANSACTION;

    DELETE FROM factor_scores WHERE as_of_date = @RebalanceDate;

    INSERT INTO factor_scores (security_id, as_of_date, factor_value)
    SELECT security_id, @RebalanceDate, computed_value
    FROM staging_factor_scores;

    IF (SELECT COUNT(*) FROM factor_scores WHERE as_of_date = @RebalanceDate) = 0
        THROW 50001, 'No rows loaded for rebalance date — aborting.', 1;

    COMMIT TRANSACTION;
END TRY
BEGIN CATCH
    ROLLBACK TRANSACTION;
    THROW;   -- re-raise the original error with full context, don't swallow it
END CATCH;
-- WHY: TRY/CATCH + explicit ROLLBACK ensures a partial failure can't leave
-- the table in an inconsistent state; THROW (no args) inside CATCH
-- re-raises the original error rather than masking it — directly mirrors
-- the Python "raise ... from e" fail-loudly pattern.


-- =============================================================================
-- SECTION 7: MERGE (UPSERT) — common in daily pipeline loads
-- =============================================================================

-- Q: "How would you upsert daily factor scores — insert new securities,
--     update existing ones, in a single statement?"
MERGE INTO factor_scores AS target
USING staging_factor_scores AS source
    ON target.security_id = source.security_id
   AND target.as_of_date  = source.as_of_date
WHEN MATCHED THEN
    UPDATE SET target.factor_value = source.computed_value
WHEN NOT MATCHED BY TARGET THEN
    INSERT (security_id, as_of_date, factor_value)
    VALUES (source.security_id, source.as_of_date, source.computed_value);
-- WHY: MERGE avoids a separate DELETE+INSERT or manual existence-check
-- pattern — one atomic statement, easier to reason about and less prone
-- to race conditions in a scheduled daily job.


-- =============================================================================
-- SECTION 8: PIVOT (long -> wide, common before matrix-style analysis)
-- =============================================================================

-- Q: "How would you pivot long-format factor data into a wide
--     security x date matrix?"
SELECT security_id, [2026-01-01], [2026-01-02]
FROM (
    SELECT security_id, as_of_date, factor_value
    FROM factor_scores
) src
PIVOT (
    MAX(factor_value)
    FOR as_of_date IN ([2026-01-01], [2026-01-02])
) AS pvt;
-- WHY: PIVOT requires known column values up front (dates here), which is
-- awkward for dynamic date ranges — worth mentioning that dynamic SQL
-- (building the column list programmatically) is the usual real-world
-- fix, since PIVOT alone doesn't handle an unknown/variable date range.


-- =============================================================================
-- SECTION 9: LIKELY "EXPLAIN THE TRADEOFF" PROMPTS (talk-through only)
-- =============================================================================
/*
Q: "Index basics — why would a query on security_id be slow without one?"
A: Without an index, SQL Server does a full table scan (checks every row).
   A clustered or nonclustered index on security_id lets it seek directly
   (like a B-tree lookup) instead of scanning — the same conceptual O(1)/
   O(log n) lookup vs O(n) scan reasoning as a Python dict vs list.

Q: "Why might a correlated subquery be slower than a window function
    doing the same logical thing?"
A: A correlated subquery re-executes once per outer row (potentially N
   times); a window function computes over the partition in a single
   pass — same underlying reasoning as vectorization vs row-wise .apply()
   in pandas.

Q: "Why use NULLIF(x, 0) instead of just trusting x will never be zero?"
A: Financial ratios and z-score denominators can legitimately be zero
   (single-security sectors, zero variance) — silently trusting divide
   safety is exactly the kind of assumption a zero-manual-review pipeline
   can't afford; NULLIF converts a hard crash into a NULL you can detect
   and handle explicitly downstream.

Q: "When would you denormalize (duplicate data) instead of keeping things
    strictly normalized?"
A: Read-heavy analytical workloads (like daily factor computation across
   a large universe) often favor some denormalization/pre-aggregation for
   query performance, trading storage and write complexity for read speed
   — a classic OLTP vs OLAP tradeoff, relevant since this pipeline is
   analytical, not transactional.
*/

PRINT 'Interview prep file loaded. Read top to bottom, or run blocks individually.';
