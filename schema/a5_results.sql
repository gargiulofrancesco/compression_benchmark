-- Phase 5 step 6 — A5 ablation results store (block-size sweep vs random-access
-- latency). Plan §12.7 row A5: x = block size ∈ {64,256,1K,4K,16K,64K} strings,
-- y = L_ra p50/p95/p99, datasets = ClickBench URL + TPC-H l_comment, cold+warm,
-- RQ4 / V-I. One row per (baseline, dataset, column, block_size, cache_state).
--
-- DESIGN NOTE — cross-baseline reframe (user-adjudicated 2026-05-31). OnPair's
-- implemented format has NO block knob: decompress(idx) seeks directly per
-- string via the global boundaries (§5.4 O(|s_i|)), so block size cannot affect
-- OnPair's L_ra. A5 is therefore NOT an OnPair-internal sweep. Instead it sweeps
-- block size on the BLOCK-BASED baselines (zstd, lz4 — both store the column as
-- byte/string-budgeted blocks and must decode the enclosing block to answer one
-- point query), measuring how THEIR L_ra degrades as block size grows, and plots
-- OnPair as a single FLAT horizontal reference line (block_size_strings = NULL),
-- block-independent because it has no blocks. The figure's thesis: block-based
-- baselines are forced to trade compression ratio (small blocks = worse ratio,
-- captured in compressed_bytes) against random-access latency via block size,
-- while OnPair escapes the trade-off entirely. (RLZ is excluded: its block_size
-- is locked to 16384 by the Q-RLZ6 lock and cannot sweep.)
--
-- L_ra MEASUREMENT — new harness C++ op `ra_latency` (NOT an OnPair library
-- change; harness runner code, like A1's held_out_compress). The op reads the
-- compressed column once, optionally does one warm pre-pass (cache_state), then
-- times n_samples (~10⁶) uniform-random single-string accesses with NO file I/O
-- inside the timed loop, and reduces them to p50/p95/p99 (+ mean/min/max) via
-- runners/core/latency_stats.h (nearest-rank). For zstd/lz4 each timed access
-- decodes the enclosing block; for OnPair it is a per-string decompress.
-- query_distribution is 'uniform' for A5; A6 will extend the op's distribution
-- enum (Zipf/Beta/Poisson) reusing the same primitive.
--
-- block_size_strings: the swept block size for zstd/lz4; NULL for OnPair (flat,
-- block-independent). n_blocks: realized block count for zstd/lz4; NULL for
-- OnPair. cache_state ∈ {cold, warm}: the op's `warmup` flag controls the
-- in-process pre-pass; true FS-cache-cold (drop OS page cache) is a measurement-
-- rig concern handled by the `make a5` wrapper, not the op.
--
-- Consumed by experiments/a5_driver.py at db init time. Separate file from the
-- headline results.sqlite AND from a1..a4_results.sqlite (each ablation owns its
-- own store, per the Phase 5 step 2 storage adjudication). A5 / F5 plots
-- (Phase 6) read from here.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS a5_results (
    run_id                       TEXT    NOT NULL,
    cell_id                      TEXT    NOT NULL,
    baseline                     TEXT    NOT NULL,   -- zstd | lz4 | onpair
    dataset                      TEXT    NOT NULL,
    column_name                  TEXT    NOT NULL,
    block_size_strings           INTEGER,            -- swept (zstd/lz4); NULL = OnPair flat line
    cache_state                  TEXT    NOT NULL,   -- cold | warm
    query_distribution           TEXT    NOT NULL,   -- 'uniform' for A5
    seed                         INTEGER NOT NULL,
    n_samples                    INTEGER NOT NULL,
    input_bytes                  INTEGER NOT NULL,
    input_rows                   INTEGER NOT NULL,
    n_blocks                     INTEGER,            -- realized blocks (zstd/lz4); NULL = OnPair
    compressed_bytes             INTEGER NOT NULL,   -- output_bytes at this block size (ratio cost axis)
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

CREATE INDEX IF NOT EXISTS idx_a5_baseline_block  ON a5_results (baseline, block_size_strings);
CREATE INDEX IF NOT EXISTS idx_a5_dataset_column  ON a5_results (dataset, column_name);
