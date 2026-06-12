-- Phase 3 result schema. Long format: one row per (cell, baseline, op, metric, sample).
-- Long format absorbs per-baseline sub-counters (FSST natural split, OnPair dict_offsets_bytes)
-- without DDL changes. Per Phase 1 architecture lock + OPEN-Q12.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS results (
    run_id                   TEXT    NOT NULL,
    cell_id                  TEXT    NOT NULL,
    baseline                 TEXT    NOT NULL,
    op                       TEXT    NOT NULL CHECK (op IN ('compress','bulk_decode','random_decode','prefix_search')),
    metric_name              TEXT    NOT NULL,
    metric_value             REAL,
    status                   TEXT    NOT NULL CHECK (status IN ('ok','oom','timeout','refused-env','crash','lossy','unsupported','dev-only')),
    prefix_search_mode       TEXT             CHECK (prefix_search_mode IS NULL OR prefix_search_mode IN ('NativeOnCompressed','DecompressThenScan','Uncompressed')),
    config_json              TEXT    NOT NULL,
    dataset                  TEXT    NOT NULL,
    sample_idx               INTEGER NOT NULL,
    machine_id               TEXT    NOT NULL,
    baseline_build_flags_sha TEXT    NOT NULL,
    notes                    TEXT,
    ts_utc                   TEXT    NOT NULL,
    PRIMARY KEY (run_id, cell_id, baseline, op, metric_name, sample_idx)
);
CREATE INDEX IF NOT EXISTS idx_results_cell  ON results (cell_id, baseline, op);
CREATE INDEX IF NOT EXISTS idx_results_pmode ON results (op, prefix_search_mode);

CREATE TABLE IF NOT EXISTS results_agg (
    run_id                   TEXT    NOT NULL,
    cell_id                  TEXT    NOT NULL,
    baseline                 TEXT    NOT NULL,
    op                       TEXT    NOT NULL,
    metric_name              TEXT    NOT NULL,
    n_samples                INTEGER NOT NULL,
    median                   REAL,
    mad                      REAL,
    mean                     REAL,
    ci95_lo                  REAL,
    ci95_hi                  REAL,
    status                   TEXT    NOT NULL,
    prefix_search_mode       TEXT,
    config_json              TEXT    NOT NULL,
    dataset                  TEXT    NOT NULL,
    machine_id               TEXT    NOT NULL,
    baseline_build_flags_sha TEXT    NOT NULL,
    ts_utc                   TEXT    NOT NULL,
    PRIMARY KEY (run_id, cell_id, baseline, op, metric_name)
);
CREATE INDEX IF NOT EXISTS idx_agg_baseline ON results_agg (baseline, op);
