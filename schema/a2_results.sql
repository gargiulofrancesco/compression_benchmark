-- A2 ablation results store (bit-width sweep).
-- One row per (dataset, column, bits). For each cell, OnPair compresses the
-- FULL materialised column at bit-width b (DynamicThreshold{0.15}, seed=42 held
-- constant — only b sweeps) and then bulk-decodes it. The main outputs
-- are R (compression ratio = input_bytes / output_bytes),
-- T_d (decode time) and |D| (dictionary size, recorded as dict_bytes). T_c
-- (compress time) is recorded as a secondary diagnostic.
--
-- DESIGN NOTE (distinct from A1/A3): A2 needs NO dedicated runner op and NO upstream
-- OnPair change. Its three quantities all have direct headline-op equivalents,
-- so a2_driver.py reuses the existing `compress` + `bulk_decode` ops and reads
-- their dispatch-loop `t_op_ns` as T_c / T_d. t_op_ns covers the whole op,
-- including I/O, so these times are not the codec-only timers behind
-- Table II.  A1/A3 needed a bespoke composite
-- op (held_out_compress) only because held-out train/encode has no headline
-- equivalent; A2 does not.
--
-- Consumed by experiments/a2_driver.py at db init time. Separate file from the
-- headline results.sqlite AND from a1_results.sqlite / a3_results.sqlite (each
-- ablation owns its own store).
-- OnPair's curve in Fig. 7 is drawn from here.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS a2_results (
    run_id                       TEXT    NOT NULL,
    cell_id                      TEXT    NOT NULL,
    dataset                      TEXT    NOT NULL,
    column_name                  TEXT    NOT NULL,
    bits                         INTEGER NOT NULL,
    sample_fraction              REAL    NOT NULL,
    seed                         INTEGER NOT NULL,
    threshold_strategy           TEXT    NOT NULL,
    input_bytes                  INTEGER NOT NULL,
    input_rows                   INTEGER NOT NULL,
    -- Compress sub-counters (bit-tight: dict+tokens+boundaries=output):
    dict_bytes                   INTEGER NOT NULL,   -- |D| (dictionary size)
    tokens_bytes                 INTEGER NOT NULL,
    boundaries_bytes             INTEGER NOT NULL,
    output_bytes                 INTEGER NOT NULL,
    ratio                        REAL    NOT NULL,   -- R = input_bytes / output_bytes
    -- Timing (nanoseconds; dispatch-loop t_op_ns over the whole op, incl. I/O):
    compress_ns                  REAL    NOT NULL,   -- T_c (secondary diagnostic)
    decode_ns                    REAL    NOT NULL,   -- T_d (headline; bulk_decode)
    -- bookkeeping:
    status                       TEXT    NOT NULL CHECK (status IN ('ok','oom','crash','lossy','refused-env','dev-only','unsupported')),
    machine_id                   TEXT    NOT NULL,
    baseline_build_flags_sha     TEXT    NOT NULL,
    config_json                  TEXT    NOT NULL,
    notes                        TEXT,
    ts_utc                       TEXT    NOT NULL,
    PRIMARY KEY (run_id, cell_id)
);

CREATE INDEX IF NOT EXISTS idx_a2_dataset_column ON a2_results (dataset, column_name);
CREATE INDEX IF NOT EXISTS idx_a2_bits           ON a2_results (bits);
