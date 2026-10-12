# Harness architecture

This document describes the architecture implemented in `runners/`,
`orchestrator/`, `schema/`, and `experiments/`.

## Data flow

```
synthetic_gen ──► Column on disk ──► runner subprocess (NDJSON over stdio)
                                            │
                                            ▼
                                  result envelopes ──► orchestrator
                                                            │
                            ┌───────────────────────────────┴───────────┐
                            ▼                                           ▼
                   results.sqlite + results.csv               manifest.json
                            │
                            ▼
                       results_agg (median/MAD/mean/95%-CI)
                            │
                            ▼
                  tools/plot/figure_template.py → PNG/PDF
```

## Runner ABC

```cpp
class Runner {
    virtual const char*       name()               const = 0;
    virtual const char*       version()            const = 0;
    virtual PrefixSearchMode  prefix_search_mode() const = 0;
    virtual OpResult compress(in_path, out_path, config_json)        = 0;
    virtual OpResult bulk_decode(in_path, out_path, config_json)     = 0;
    virtual OpResult random_decode(in_path, idx, out_path, cfg_json) = 0;
    virtual OpResult prefix_search(in_path, needle, out_path, cfg)   = 0;
};
```

Two further ops have default implementations that answer `unsupported`:
`ra_latency` (point-access latency, implemented by every runner) and
`held_out_compress` (OnPair only, for the threshold and budget ablations).
`OpResult` carries a vector of `(metric_name, value)`
pairs — long-format absorbs per-baseline sub-counters (FSST natural split, OnPair
`dict_offsets_bytes` diagnostic) with no DDL change.

The dispatch loop in `runners/core/runner_protocol.cpp` owns NDJSON parsing
and the whole-op `steady_clock` timing: it injects `t_op_ns` into every result.
Runners receive file paths, read and write columns with the shared
`read_column` / `write_column`, and report their own codec-only timers
(`t_compress_ns`, `t_decode_ns`), which exclude file I/O.

## PrefixSearchMode enum

```
NativeOnCompressed  — OnPair only
DecompressThenScan  — every other compressed baseline (zstd, LZ4, FSST, Re-Pair, BPE, RLZ)
Uncompressed        — the uncompressed reference
```

The mode check (`orchestrator/modes.py::check_g7`) asserts each runner's
declared mode matches `experiments/baseline_modes.yaml`. The prefix-search
check (`orchestrator/orchestrate.py::_verify_g6`) asserts, for every cell,
that the baseline's `prefix_search` hit list for the first needle of
`testdata/g6_needles.txt` equals a reference scan of the input column; its
outcome is stored as the metric `g6_passed`. The C++ tests
(`tests/*_prefix_search_test.cpp`) compare all 16 needles against the
canonical raw-byte scan in `runners/core/scan_prefix.cpp`.

## Result schema

Long format — one row per `(run_id, cell_id, baseline, op, metric_name, sample_idx)`.
See `schema/results.sql`. Status column is one of eight values:

```
ok | oom | timeout | refused-env | crash | lossy | unsupported | dev-only
```

The `prefix_search_mode` column is denormalised: NULL for
non-prefix_search rows, so prefix-search rows can be sliced by mode in SQL.

Aggregated table `results_agg` carries `median / mad / mean / ci95_lo / ci95_hi`
populated by `orchestrator/stats.py`. The diagnostic plot scripts under
`tools/plot/` read `results_agg`; the paper's tables and figures
(`tools/paper_tables.py`, `tools/paper_figures.py`) take their medians from
`results` directly, pooling the repetitions of every pass.

## Ratio formula

```
R = Σ|s_i| / (dict_bytes + tokens_bytes)
```

The per-row offsets (`boundaries_bytes`), which every baseline stores in some
form, are left out for all of them; `output_bytes`, which includes them, is
recorded as well, and `T5_complete.tex` gives the ratios with the offsets
counted.

Sub-counters per baseline:

- **Uncompressed**: `dict_bytes=0`, `tokens_bytes=total payload`, `boundaries_bytes=(N+1)·4`.
- **OnPair**: `dict_bytes = Dictionary::bytes_used()`; the diagnostic
  `dict_offsets_bytes = (num_tokens+1)·4` is reported next to it and is
  contained in `dict_bytes`.
- Per-baseline split documented in docs/benchmark/fairness/.

## Statistical engine

- **Primary**: median ± MAD (`orchestrator/stats.median_mad`).
- **Secondary**: arithmetic mean + 95%-CI from Student-t (`mean_ci95`).
- `n_samples = 2` for smoke; `n_samples = 11` for the main grid.

## Environment capture

`orchestrator/env_probe.py` records `uname / cpu_model / compiler / python_version`,
derives `machine_id = sha256(uname|cpu_model|compiler)[:16]`, and computes
`baseline_build_flags_sha`, the SHA-256 of the sorted build flags
(`DEV_BUILD_FLAGS`). The same canonical
flag list lives in `CMakeLists.txt` (`add_compile_options(...)`) — these two
sources MUST stay in lockstep.

The paper's timings were taken with the performance governor and turbo off.
Rows measured on a machine set up otherwise are annotated
`status='dev-only'`: their sizes and ratios are exact, their timings are those
of that machine as it was. Rows measured in the paper's setup are
`status='ok'`. `STRICT_ENV=1` makes the timing stages refuse to run outside
that setup.

## Failure-as-data

A failed cell is recorded as a row, not raised. Each op's status comes from
the runner's result envelope: a runner self-declares `unsupported` for an op
it cannot handle on a given input, `refused-env` when a measurement's
preconditions are not met, `oom` on an allocation failure. The orchestrator
adds `lossy` (a round trip that does not reproduce the input), `crash` (the
runner died mid-protocol) and `dev-only`. `orchestrator/status.py::classify`
maps a process outcome `(exit_code, signal, peak_rss, wall_s)` to a status
(exit 78 → `refused-env`; SIGKILL + RSS ≥ 85% of `mem_cap_gb` → `oom`); it is
covered by `tests/test_status.py`.

## Orchestration entry points

```
make smoke           end-to-end check on the synthetic generator (≤ 5 min)
make build           setup venv + cpp-build
make cpp-build       cmake configure + build
make ctest           C++ unit tests
make pytest          Python tests
make plot            render figure template from results.sqlite
make lint            source grep audits
```

`./reproduce.sh` (see the README) runs the paper's stages on top of these.

## Stability surfaces

These contracts are stable; baseline integrations
plug into them without modification:

- `runners/core/runner_protocol.h` ABC + NDJSON envelope shape.
- `runners/core/scan_prefix.h` canonical prefix scanner (reference for the prefix-search check).
- `schema/results.sql` DDL.
- `experiments/baseline_modes.yaml` schema (rows append per baseline).
- `testdata/g6_needles.txt` 16-needle list.
- `orchestrator/status.py` status set and classifier.
- `orchestrator/stats.py` median+MAD+mean+95%-CI shapes.
