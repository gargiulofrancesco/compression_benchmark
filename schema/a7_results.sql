-- Phase 5 step 8 — A7 ablation results store (shuffle-seed sensitivity sweep).
-- One row per (dataset, column, seed). For each cell, OnPair compresses the
-- FULL materialised column with the headline strategy held constant
-- (bits=16 = Q-OP1 default, DynamicThreshold{0.15}) and ONLY the training
-- shuffle seed sweeps over {42, 7, 1729, 0xDEAD, 0xBEEF} (plan §12.7 row A7,
-- §8.6, RQ3 / V-G). The single headline output is R (compression ratio =
-- input_bytes / output_bytes); T_c (compress time) is a secondary diagnostic.
--
-- The seed seeds the mt19937_64 string-shuffle that DynamicThreshold uses to
-- pick its training sample (orchestrator determinism lock: seed=42 is the
-- headline; A7 is the affirmative seed-insensitivity argument for V-G — R
-- should barely move across seeds). Datasets are deliberately narrow per the
-- plan (DBLP + ClickBench URL only): A7 is a robustness check, not a main
-- result.
--
-- DESIGN NOTE (same as A2, distinct from A1/A3): A7 adds NO new runner op and
-- NO upstream OnPair change. The `seed` knob already exists in the OnPair
-- runner cfg (parse_onpair_config, default 42); A7 reuses the existing
-- `compress` op and reads its sub-counters for R. Unlike A2, A7 is
-- compress-only (no bulk_decode) because its only y-axis is R — there is no
-- T_d in the A7 plan row.
--
-- Consumed by experiments/a7_driver.py at db init time. Separate file from the
-- headline results.sqlite AND from a1/a2/a3/a4/a5/a6_results.sqlite (each
-- ablation owns its own store, per the Phase 5 step 2 storage adjudication).
-- A7 plots (Phase 6) read from here.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS a7_results (
    run_id                       TEXT    NOT NULL,
    cell_id                      TEXT    NOT NULL,
    dataset                      TEXT    NOT NULL,
    column_name                  TEXT    NOT NULL,
    seed                         INTEGER NOT NULL,   -- the swept variable
    bits                         INTEGER NOT NULL,   -- held at 16 (Q-OP1 headline)
    sample_fraction              REAL    NOT NULL,   -- held at 0.15
    threshold_strategy           TEXT    NOT NULL,   -- held at 'dynamic'
    input_bytes                  INTEGER NOT NULL,
    input_rows                   INTEGER NOT NULL,
    -- Compress sub-counters (bit-tight: dict+tokens+boundaries=output):
    dict_bytes                   INTEGER NOT NULL,
    tokens_bytes                 INTEGER NOT NULL,
    boundaries_bytes             INTEGER NOT NULL,
    output_bytes                 INTEGER NOT NULL,
    ratio                        REAL    NOT NULL,   -- R = input_bytes / output_bytes
    -- Timing (nanoseconds; dispatch-loop t_op_ns over the whole compress op,
    -- incl. I/O, matching the §8.2 headline definition):
    compress_ns                  REAL    NOT NULL,   -- T_c (secondary diagnostic)
    -- bookkeeping:
    status                       TEXT    NOT NULL CHECK (status IN ('ok','oom','crash','lossy','refused-env','dev-only','unsupported')),
    machine_id                   TEXT    NOT NULL,
    baseline_build_flags_sha     TEXT    NOT NULL,
    config_json                  TEXT    NOT NULL,
    notes                        TEXT,
    ts_utc                       TEXT    NOT NULL,
    PRIMARY KEY (run_id, cell_id)
);

CREATE INDEX IF NOT EXISTS idx_a7_dataset_column ON a7_results (dataset, column_name);
CREATE INDEX IF NOT EXISTS idx_a7_seed           ON a7_results (seed);
