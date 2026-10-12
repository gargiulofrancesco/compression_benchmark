-- A1 ablation results store (adaptive vs fixed thresholds).
-- Wide-format paired schema: one row per (dataset, column, T) with both
-- Fixed{T} and matched-Dynamic{f} metric blocks side by side. Pairing is
-- guaranteed apples-to-apples by the protocol (both trainers see identical
-- shuffled-string order via mt19937_64(seed=42) and stop at the same byte
-- budget via sample_bytes_consumed-matched fraction).
--
-- This file is consumed by experiments/a1_driver.py at db init time;
-- build/a1_results.sqlite is NOT the headline results.sqlite. Fig. 6 (left)
-- is drawn from here.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS a1_results (
    run_id                       TEXT    NOT NULL,
    cell_id                      TEXT    NOT NULL,
    dataset                      TEXT    NOT NULL,
    column_name                  TEXT    NOT NULL,
    fixed_threshold_T            INTEGER NOT NULL,
    bits                         INTEGER NOT NULL,
    seed                         INTEGER NOT NULL,
    train_input_bytes            INTEGER NOT NULL,
    train_input_rows             INTEGER NOT NULL,
    heldout_input_bytes          INTEGER NOT NULL,
    heldout_input_rows           INTEGER NOT NULL,
    sample_bytes_consumed_fixed  INTEGER NOT NULL,
    matched_fraction             REAL    NOT NULL,
    -- Fixed-trained dict applied to heldout:
    dict_bytes_fixed             INTEGER NOT NULL,
    tokens_bytes_fixed           INTEGER NOT NULL,
    boundaries_bytes_fixed       INTEGER NOT NULL,
    heldout_output_bytes_fixed   INTEGER NOT NULL,
    heldout_ratio_fixed          REAL    NOT NULL,
    -- Matched-Dynamic-trained dict applied to heldout:
    dict_bytes_dyn               INTEGER NOT NULL,
    tokens_bytes_dyn             INTEGER NOT NULL,
    boundaries_bytes_dyn         INTEGER NOT NULL,
    heldout_output_bytes_dyn     INTEGER NOT NULL,
    heldout_ratio_dyn            REAL    NOT NULL,
    -- bookkeeping:
    status                       TEXT    NOT NULL CHECK (status IN ('ok','oom','crash','lossy','refused-env','dev-only','unsupported')),
    machine_id                   TEXT    NOT NULL,
    baseline_build_flags_sha     TEXT    NOT NULL,
    config_json                  TEXT    NOT NULL,
    notes                        TEXT,
    ts_utc                       TEXT    NOT NULL,
    PRIMARY KEY (run_id, cell_id)
);

CREATE INDEX IF NOT EXISTS idx_a1_dataset_column ON a1_results (dataset, column_name);
CREATE INDEX IF NOT EXISTS idx_a1_T              ON a1_results (fixed_threshold_T);
