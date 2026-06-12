-- Phase 5 step 7 — A6 ablation results store (query-distribution sweep vs
-- random-access latency). Plan §12.7 row A6: query distribution ∈ {uniform,
-- Zipf(1.0), Zipf(1.5), Beta(2,5), Poisson(N/2)}, x = dist, y = L_ra
-- p50/p95/p99, datasets = ClickBench URL + TPC-H l_comment, RQ4 / V-I, V-J.
-- One row per (baseline, dataset, column, query_distribution, query_layout,
-- cache_state).
--
-- REUSE OF THE L_ra PRIMITIVE. A6 reuses the SAME harness `ra_latency` op
-- introduced for A5 (runners/{zstd,lz4,fsst,onpair}; runners/core/query_sampler.h
-- + latency_stats.h) — NOT an OnPair library change. A6 only varies the op's
-- query_distribution + query_layout; the timed region is byte-identical across
-- every baseline (the only difference between rows is the decode work, never the
-- query sequence). Unlike A5, block size is NOT swept here — it is fixed at the
-- headline 1 MB block budget for the block baselines (recorded in config_json).
--
-- ROSTER (user-adjudicated 2026-05-31). A6 sweeps the distribution over A5's
-- roster PLUS one per-string-native baseline besides OnPair:
--   block-based : zstd, lz4   (decode the enclosing block per point query →
--                              L_ra degrades when the hot set is scattered)
--   per-string  : fsst, onpair (decode each row independently, O(|s_i|) →
--                              flat under skew)
-- FSST is included as a FAIRNESS requirement: if only OnPair were flat under
-- skew the figure would imply a skew advantage that is actually a property of
-- the whole per-string class, not OnPair specifically. PFC was rejected as the
-- per-string companion — its front-coding buckets require a walk from the bucket
-- head, so it would NOT be flat and would conflate the per-string-class point.
--
-- COLUMN-STORE-NATIVE DEPENDENCY (flagged, not resolved here). The block-framing
-- lock restricts block framing to zstd + LZ4 only; the roster contains NO
-- distinct column-store-native BLOCK baseline. If one is later added to the
-- headline roster it MUST be added to A6 (its skew-response is what systems
-- reviewers care about). Recorded as an open dependency in the locks memory.
--
-- LAYOUT BRACKET (user-adjudicated 2026-05-31). Each skewed distribution is run
-- under BOTH layouts and reported as a bracket:
--   identity : index = popularity position → hot set physically CONTIGUOUS
--              (block baselines' best case: one hot block stays cached).
--   scramble : index = π(position) for a deterministic seeded permutation →
--              hot set SCATTERED (YCSB-realistic; block baselines' worst case).
-- The draw stream is identical for both layouts (same seed); scramble only
-- post-maps through π. Uniform is layout-invariant → recorded under identity
-- only. The honest claim the bracket supports: OnPair/FSST are flat across the
-- bracket, while zstd/lz4 degrade from identity → scramble — i.e. OnPair's
-- skew-response holds even under the layout most favourable to block formats.
--
-- query_layout: 'identity' | 'scramble' (uniform rows use 'identity').
-- n_blocks: realized block count for zstd/lz4; NULL for the per-string baselines
-- (fsst, onpair). cache_state ∈ {cold, warm}: the op's `warmup` flag controls
-- the in-process pre-pass; true FS-cache-cold (drop OS page cache) is a
-- measurement-rig concern handled by the `make a6` wrapper, not the op.
--
-- Consumed by experiments/a6_driver.py at db init time. Separate file from the
-- headline results.sqlite AND from a1..a5_results.sqlite (each ablation owns its
-- own store). A6 / F5 CDF plots (Phase 6) read from here.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS a6_results (
    run_id                       TEXT    NOT NULL,
    cell_id                      TEXT    NOT NULL,
    baseline                     TEXT    NOT NULL,   -- zstd | lz4 | fsst | onpair
    dataset                      TEXT    NOT NULL,
    column_name                  TEXT    NOT NULL,
    query_distribution           TEXT    NOT NULL,   -- uniform|zipf1.0|zipf1.5|beta2_5|poisson_n2
    query_layout                 TEXT    NOT NULL,   -- identity | scramble
    cache_state                  TEXT    NOT NULL,   -- cold | warm
    seed                         INTEGER NOT NULL,
    n_samples                    INTEGER NOT NULL,
    input_bytes                  INTEGER NOT NULL,
    input_rows                   INTEGER NOT NULL,
    n_blocks                     INTEGER,            -- block baselines; NULL = per-string (fsst/onpair)
    compressed_bytes             INTEGER NOT NULL,   -- output_bytes at the fixed headline block budget
    -- Random-access latency (nanoseconds; from the ra_latency op):
    l_ra_p50_ns                  REAL    NOT NULL,   -- headline y
    l_ra_p95_ns                  REAL    NOT NULL,   -- headline y
    l_ra_p99_ns                  REAL    NOT NULL,   -- headline y
    l_ra_mean_ns                 REAL    NOT NULL,   -- diagnostic
    l_ra_min_ns                  REAL    NOT NULL,   -- diagnostic
    l_ra_max_ns                  REAL    NOT NULL,   -- diagnostic
    -- bookkeeping:
    status                       TEXT    NOT NULL CHECK (status IN ('ok','oom','crash','lossy','refused-env','dev-only','unsupported')),
    machine_id                   TEXT    NOT NULL,
    baseline_build_flags_sha     TEXT    NOT NULL,
    config_json                  TEXT    NOT NULL,
    notes                        TEXT,
    ts_utc                       TEXT    NOT NULL,
    PRIMARY KEY (run_id, cell_id)
);

CREATE INDEX IF NOT EXISTS idx_a6_baseline_dist   ON a6_results (baseline, query_distribution);
CREATE INDEX IF NOT EXISTS idx_a6_dataset_column  ON a6_results (dataset, column_name);
