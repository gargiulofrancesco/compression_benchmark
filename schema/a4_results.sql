-- A4 ablation results store (capacity sweep).
-- One row per (dataset, column, capacity). For each cell, OnPair compresses the
-- FULL materialised column at dictionary capacity 2^b (DynamicThreshold{0.15},
-- seed=42 held constant — only the capacity sweeps) and then bulk-decodes it.
-- The main outputs are R (compression ratio), T_d
-- (decode time) and M_train (peak RSS during encode). |D| (dict_bytes) and T_c
-- (compress time) are recorded as secondary diagnostics.
--
-- DESIGN NOTE 1 — capacity ≡ 2^bits. OnPair has
-- NO independent dictionary-capacity knob: TrainingConfig (config.h) defines
-- "Max dictionary size = 2^bits entries", and the DynamicThresholdController is
-- constructed with capacity = 2^bits − 256. So A4's x-axis
-- capacity ∈ {2⁹, 2¹², 2¹⁴, 2¹⁶} maps 1:1 onto bits ∈ {9, 12, 14, 16}; we sweep
-- the existing `bits` knob and store BOTH `bits` and the derived `capacity`
-- (= 1<<bits) so the plot can use capacity directly. Decoupling capacity from
-- token width would require an upstream library change (a new max_dict_size
-- field), which is not made.
--
-- DESIGN NOTE 2 — like A2, A4 needs NO dedicated runner op and NO upstream OnPair
-- change. R / T_d / |D| / T_c all reuse the existing `compress` + `bulk_decode`
-- ops (dispatch-loop t_op_ns, whole op incl. I/O; not the codec-only timers
-- behind Table II). M_train (the one quantity A2 lacks) is obtained WITHOUT C++: the
-- driver runs the `compress` op in its OWN short-lived process and reads that
-- child's peak RSS via os.wait4().ru_maxrss after it exits, then runs
-- `bulk_decode` in a SEPARATE process for T_d (so decode RSS never contaminates
-- M_train). ru_maxrss is normalized to BYTES in the driver (macOS reports
-- bytes; Linux reports KiB) and the source platform is recorded in config_json.
-- M_train is therefore the whole compress-process high-water (incl. column I/O
-- + the train+encode+write region).
--
-- Consumed by experiments/a4_driver.py at db init time. Separate file from the
-- headline results.sqlite AND from a1/a2/a3_results.sqlite (each ablation owns
-- its own store). tools/plot/ablations.py reads from here.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS a4_results (
    run_id                       TEXT    NOT NULL,
    cell_id                      TEXT    NOT NULL,
    dataset                      TEXT    NOT NULL,
    column_name                  TEXT    NOT NULL,
    bits                         INTEGER NOT NULL,
    capacity                     INTEGER NOT NULL,   -- = 1<<bits (x-axis)
    sample_fraction              REAL    NOT NULL,
    seed                         INTEGER NOT NULL,
    threshold_strategy           TEXT    NOT NULL,
    input_bytes                  INTEGER NOT NULL,
    input_rows                   INTEGER NOT NULL,
    -- Compress sub-counters (bit-tight: dict+tokens+boundaries=output):
    dict_bytes                   INTEGER NOT NULL,   -- |D| (secondary diagnostic)
    tokens_bytes                 INTEGER NOT NULL,
    boundaries_bytes             INTEGER NOT NULL,
    output_bytes                 INTEGER NOT NULL,
    ratio                        REAL    NOT NULL,   -- R = input_bytes / output_bytes
    -- Timing (nanoseconds; dispatch-loop t_op_ns over the whole op, incl. I/O):
    compress_ns                  REAL    NOT NULL,   -- T_c (secondary diagnostic)
    decode_ns                    REAL    NOT NULL,   -- T_d (headline; bulk_decode)
    -- Memory (bytes; peak RSS of the compress-only process via os.wait4):
    m_train_bytes                INTEGER NOT NULL,   -- M_train (headline y-axis)
    -- bookkeeping:
    status                       TEXT    NOT NULL CHECK (status IN ('ok','oom','crash','lossy','refused-env','dev-only','unsupported')),
    machine_id                   TEXT    NOT NULL,
    baseline_build_flags_sha     TEXT    NOT NULL,
    config_json                  TEXT    NOT NULL,
    notes                        TEXT,
    ts_utc                       TEXT    NOT NULL,
    PRIMARY KEY (run_id, cell_id)
);

CREATE INDEX IF NOT EXISTS idx_a4_dataset_column ON a4_results (dataset, column_name);
CREATE INDEX IF NOT EXISTS idx_a4_capacity       ON a4_results (capacity);
