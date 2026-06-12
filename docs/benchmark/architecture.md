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

The four ops match.4. `OpResult` carries a vector of `(metric_name, value)`
pairs — long-format absorbs per-baseline sub-counters (FSST natural split, OnPair
`dict_offsets_bytes` diagnostic) with no DDL change.

The dispatch loop in `runners/core/runner_protocol.cpp` owns NDJSON parsing,
file I/O, and `steady_clock` timing. It injects `t_op_ns` into every result.

## PrefixSearchMode enum (architecture addendum)

```
NativeOnCompressed  — OnPair only (OnPair-integration)
DecompressThenScan  — every other compressed baseline (zstd, LZ4, FSST, RPDAC, PFC, HTFC, BigRePair, BPE, RLZ)
Uncompressed        — §8.10 floor reference (stub baseline)
```

The G7 smoke gate (`orchestrator/modes.py::check_g7`) asserts each runner's
declared mode matches `experiments/baseline_modes.yaml`. The G6 gate
(`orchestrator/orchestrate.py::_verify_g6`) asserts every baseline's
`prefix_search` hit-set is bytewise identical to the canonical raw-byte scan
in `runners/core/scan_prefix.cpp` on the 16 fixed needles at
`testdata/g6_needles.txt`.

## Result schema

Long format — one row per `(run_id, cell_id, baseline, op, metric_name, sample_idx)`.
See `schema/results.sql`. Status column is one of seven values:

```
ok | oom | timeout | refused-env | crash | unsupported | dev-only
```

The `prefix_search_mode` column is denormalised per — NULL for
non-prefix_search rows, powers §8.10 SQL slicing.

Aggregated table `results_agg` carries `median / mad / mean / ci95_lo / ci95_hi`
populated by `orchestrator/stats.py`. Plot scripts read `results_agg`, not `results`.

## Ratio formula §12.6

```
R = Σ|s_i| / (dict_bytes + tokens_bytes + boundaries_bytes)
```

Sub-counters per baseline:

- **Uncompressed**: `dict_bytes=0`, `tokens_bytes=total payload`, `boundaries_bytes=(N+1)·4`.
- **OnPair**: `dict_bytes = Dictionary::bytes_used` (diagnostic sub-counter);
  additional `dict_offsets_bytes = (num_tokens+1)·4` diagnostic — NOT in the
  ratio numerator, surfaced for V-B reviewer transparency.
- Per-baseline split documented in docs/benchmark/fairness/.

## Statistical engine (.5)

- **Primary**: median ± MAD (`orchestrator/stats.median_mad`).
- **Secondary**: arithmetic mean + 95%-CI from Student-t (`mean_ci95`).
- `n_samples = 5` for smoke; `n_samples = 30` for production.

## Environment capture

`orchestrator/env_probe.py` records `uname / cpu_model / compiler / python_version`,
derives `machine_id = sha256(uname|cpu_model|compiler)[:16]`, and computes
`baseline_build_flags_sha = sha256(joined-sorted-dev-flags)`. The same canonical
flag list lives in `CMakeLists.txt` (`add_compile_options(...)`) — these two
sources MUST stay in lockstep (will share a single source of truth).

Rows measured in an uncontrolled environment (governor != performance or
turbo on, where the grid does not refuse outright) are annotated
`status='dev-only'`: metric values are recorded but flagged as not
publication-grade. Controlled-environment rows are `status='ok'`.

## Failure-as-data

`orchestrator/status.py::classify` maps `(exit_code, signal, peak_rss, wall_s)`
to one of the seven statuses. Reserved sentinel: exit 78 → `refused-env`
(RLZ-style "refuse to run on this hardware"). SIGKILL + RSS ≥ 85% of `mem_cap_gb`
→ `oom`. Runners may self-declare `status='unsupported'` for ops they cannot
handle on a given input.

## Orchestration entry points

```
make smoke           exit gate — end-to-end on synthetic generator (≤ 5 min)
make build           setup venv + cpp-build
make cpp-build       cmake configure + build
make ctest           C++ unit tests
make pytest          Python tests
make plot            render figure template from results.sqlite
make lint            CI grep audits
```

## Stability surfaces

These contracts are stable; baseline integrations
plug into them without modification:

- `runners/core/runner_protocol.h` ABC + NDJSON envelope shape.
- `runners/core/scan_prefix.h` canonical prefix scanner (G6 reference).
- `schema/results.sql` DDL.
- `experiments/baseline_modes.yaml` schema (rows append per baseline).
- `testdata/g6_needles.txt` 16-needle binding list (immutable).
- `orchestrator/status.py` seven-status classifier.
- `orchestrator/stats.py` median+MAD+mean+95%-CI shapes.
