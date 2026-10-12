-- A3 ablation results store (sample_fraction sweep).
-- One row per (dataset, column, sample_fraction). For each cell, OnPair trains
-- a single DynamicThreshold{sample_fraction} dictionary on the column's train
-- split (seed=42, bits=16) and encodes the held-out split with it. The main
-- outputs are R (held-out ratio) and T_c (compression time,
-- decomposed here into train_ns + encode_ns); |D| (num_dict_tokens) and the
-- effective_fraction actually consumed are recorded as diagnostics.
--
-- Consumed by experiments/a3_driver.py at db init time. Separate file from the
-- headline results.sqlite AND from a1_results.sqlite (each ablation owns its
-- own store). Fig. 6 (right) is drawn from here.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS a3_results (
    run_id                       TEXT    NOT NULL,
    cell_id                      TEXT    NOT NULL,
    dataset                      TEXT    NOT NULL,
    column_name                  TEXT    NOT NULL,
    sample_fraction              REAL    NOT NULL,
    bits                         INTEGER NOT NULL,
    seed                         INTEGER NOT NULL,
    threshold_strategy           TEXT    NOT NULL,
    train_input_bytes            INTEGER NOT NULL,
    train_input_rows             INTEGER NOT NULL,
    heldout_input_bytes          INTEGER NOT NULL,
    heldout_input_rows           INTEGER NOT NULL,
    -- Training observables:
    sample_bytes_consumed        INTEGER NOT NULL,
    effective_fraction           REAL    NOT NULL,
    num_dict_tokens              INTEGER NOT NULL,
    -- Held-out encode sub-counters (bit-tight: dict+tokens+boundaries=output):
    dict_bytes                   INTEGER NOT NULL,
    tokens_bytes                 INTEGER NOT NULL,
    boundaries_bytes             INTEGER NOT NULL,
    heldout_output_bytes         INTEGER NOT NULL,
    heldout_ratio                REAL    NOT NULL,
    -- T_c decomposition (nanoseconds, in-op steady_clock):
    train_ns                     REAL    NOT NULL,
    encode_ns                    REAL    NOT NULL,
    -- bookkeeping:
    status                       TEXT    NOT NULL CHECK (status IN ('ok','oom','crash','lossy','refused-env','dev-only','unsupported')),
    machine_id                   TEXT    NOT NULL,
    baseline_build_flags_sha     TEXT    NOT NULL,
    config_json                  TEXT    NOT NULL,
    notes                        TEXT,
    ts_utc                       TEXT    NOT NULL,
    PRIMARY KEY (run_id, cell_id)
);

CREATE INDEX IF NOT EXISTS idx_a3_dataset_column ON a3_results (dataset, column_name);
CREATE INDEX IF NOT EXISTS idx_a3_fraction       ON a3_results (sample_fraction);
