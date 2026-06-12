# `make smoke` is the end-to-end functional gate.

PYTHON       ?= python3
VENV         := .venv
VPY          := $(VENV)/bin/python
VPIP         := $(VENV)/bin/pip
VPYTEST      := $(VENV)/bin/pytest
BUILD        := build
RUNNER       := $(BUILD)/runners/uncompressed/uncompressed_runner
DB           := $(BUILD)/results.sqlite
CSV          := $(BUILD)/results.csv
# smoke uses its own store so it can never clobber a real measurement run
SMOKE_DB     := $(BUILD)/smoke_results.sqlite
SMOKE_CSV    := $(BUILD)/smoke_results.csv
MANIFEST     := $(BUILD)/manifest.json
WORKDIR      := $(BUILD)/smoke_workdir
NEEDLES      := testdata/g6_needles.txt
MODES        := experiments/baseline_modes.yaml
CELLS        := experiments/smoke_cells.yaml
FIG_PNG      := $(BUILD)/figure_template.png

.PHONY: smoke setup build cpp-build pytest ctest plot lint clean reproduce-all help datasets a1-smoke a1 a2-smoke a2 a3-smoke a3 a4-smoke a4 a5-smoke a5 a6-smoke a6 a7-smoke a7 bench-smoke bench figures fingerprint install-hooks repo-size

help:
	@echo "Targets:"
	@echo "  setup         create .venv and install requirements.txt"
	@echo "  cpp-build     configure + build runners/ via CMake"
	@echo "  build         setup + cpp-build"
	@echo "  smoke         end-to-end functional smoke"
	@echo "  datasets      materialise the 7 real datasets per experiments/"
	@echo "                datasets.yaml. NOT part of smoke. Requires ~5 GB"
	@echo "                disk + bandwidth + duckdb for TPC. Measurement"
	@echo "                rig only; do NOT run on the dev box without intent."
	@echo "  a1-smoke      A1 ablation smoke: 1 paired probe"
	@echo "                against a committed V-D fixture; <1 sec wall."
	@echo "  a1            A1 headline grid (Fixed{T} × 15 cols)."
	@echo "                Requires 'make datasets' first. Measurement rig only."
	@echo "  a2-smoke      A2 ablation smoke: 1 bit-width probe"
	@echo "                (compress+decode) against a committed V-D fixture;"
	@echo "                <1 sec wall."
	@echo "  a2            A2 grid (bit-width sweep × 15 cols)."
	@echo "                Requires 'make datasets' first. Rig only."
	@echo "  a3-smoke      A3 ablation smoke: 1 sample_fraction"
	@echo "                probe against a committed V-D fixture; <1 sec wall."
	@echo "  a3            A3 grid (sample_fraction sweep × 15"
	@echo "                cols). Requires 'make datasets' first. Rig only."
	@echo "  a4-smoke      A4 ablation smoke: 1 capacity probe"
	@echo "                (compress+decode, peak-RSS M_train) against a"
	@echo "                committed V-D fixture; <1 sec wall."
	@echo "  a4            A4 grid (capacity sweep × 15 cols)."
	@echo "                Requires 'make datasets' first. Rig only."
	@echo "  a5-smoke      A5 ablation smoke: one zstd block cell"
	@echo "                + one OnPair flat cell (ra_latency L_ra) against a"
	@echo "                committed V-D fixture; <1 sec wall."
	@echo "  a5            A5 grid (block-size sweep on zstd/lz4 vs"
	@echo "                L_ra p50/p95/p99, OnPair flat line; 2 cols, cold+warm)."
	@echo "                Requires 'make datasets' first. Rig only."
	@echo "  a6-smoke      A6 ablation smoke: zstd + fsst + onpair,"
	@echo "                Zipf(1.0) × {identity,scramble} (ra_latency L_ra) against"
	@echo "                a committed V-D fixture; <1 sec wall."
	@echo "  a6            A6 grid (query-distribution sweep + layout"
	@echo "                bracket vs L_ra; zstd/lz4/fsst/onpair, 2 cols, cold+warm)."
	@echo "                Requires 'make datasets' first. Rig only."
	@echo "  a7-smoke      A7 ablation smoke: 1 seed probe (seed=7,"
	@echo "                compress-only) against a committed V-D fixture; <1 sec."
	@echo "  a7            A7 grid (shuffle-seed sweep × DBLP titles"
	@echo "                + ClickBench URL; seed-insensitivity of R for V-G)."
	@echo "                Requires 'make datasets' first. Rig only."
	@echo "  bench-smoke   Phase 6 headline smoke: lossless trio (uncompressed/"
	@echo "                zstd/onpair) on a committed fixture via the canonical"
	@echo "                orchestrator; verifies the headline wiring. <~30 s."
	@echo "  bench         Phase 6 headline run (§8.2/F1/T1/T2): 15 real cols × 11"
	@echo "                lossless baselines → results.sqlite. Requires 'make"
	@echo "                datasets' first. Rig only."
	@echo "  figures       Phase 6 figures: render all F/T/A figures from the"
	@echo "                *_results.sqlite stores into build/figures/."
	@echo "  install-hooks Install the versioned git pre-commit guard"
	@echo "                (core.hooksPath=tools/git-hooks) that blocks committing"
	@echo "                build/ .venv/ datasets/ or files >10 MiB. Run after 'git init'."
	@echo "  repo-size     Report what git would actually track + the 10 largest"
	@echo "                tracked files (sanity-check before pushing)."
	@echo "  pytest        run Python tests"
	@echo "  ctest         run C++ unit tests (excludes LABELS=stress)"
	@echo "  plot          render figure template from \$$DB"
	@echo "  lint          run CI grep audits (FCSD-skip etc.)"
	@echo "  clean         remove build/ and .venv/"

$(VENV)/.installed: requirements.txt
	$(PYTHON) -m venv $(VENV)
	$(VPIP) install --upgrade pip >/dev/null
	$(VPIP) install -r requirements.txt
	@touch $@

setup: $(VENV)/.installed

cpp-build:
	cmake -S . -B $(BUILD) -DCMAKE_BUILD_TYPE=Release
	cmake --build $(BUILD) -j

build: setup cpp-build

ctest: cpp-build
	# C++ unit tests. `-LE stress` is retained as a harmless generic exclusion;
	# the OOM/resource-exhaustion stress probes were removed (out of
	# scope — target columns are hundreds-of-MB to a few GB). The status='oom'
	# machinery stays compiled in and is unit-tested by tests/test_status.py.
	ctest --test-dir $(BUILD) --output-on-failure -LE stress

pytest: setup
	PYTHONPATH=. $(VPYTEST) -q tests

lint:
	# bpe/rlz scope-audits removed — brittle source-grep proxies that
	# false-positive on legit code (bpe: IdToPiece decode-table build; rlz:
	# chrono .count()). The "no compressed-domain fast path" property is enforced
	# by baseline_modes (DecompressThenScan) + the round-trip tests instead.
	bash tools/lint/fcsd_skip_audit.sh
	bash tools/lint/onpair_scope_audit.sh

plot: setup
	$(VPY) tools/plot/figure_template.py --db $(SMOKE_DB) --out-png $(FIG_PNG)

smoke: build lint
	@rm -rf $(WORKDIR) $(SMOKE_DB) $(SMOKE_CSV)
	@mkdir -p $(WORKDIR)
	PYTHONHASHSEED=0 $(VPY) -m orchestrator.orchestrate \
	    --build-root $(BUILD) \
	    --cells    $(CELLS) \
	    --modes    $(MODES) \
	    --needles  $(NEEDLES) \
	    --db       $(SMOKE_DB) \
	    --csv      $(SMOKE_CSV) \
	    --manifest $(MANIFEST) \
	    --workdir  $(WORKDIR)
	$(MAKE) ctest
	$(MAKE) pytest
	$(MAKE) plot
	@echo "SMOKE OK"

datasets: setup
	# — materialise the 7 real datasets per experiments/
	# datasets.yaml. Network IO + duckdb (TPC); NOT part of `make smoke`.
	# Smoke uses committed fixtures under testdata/datasets/ instead.
	PYTHONPATH=. $(VPY) -m tools.datasets.prepare --all \
	    --raw-dir datasets/raw --columns-dir datasets/materialized \
	    --update-checksums

a1-smoke: build
	# A1 ablation smoke gate. Single paired probe
	# against the committed V-D fixture; runs in <1 sec wall.
	# NOT wired into `make smoke` — A1 is a separate experiment runner.
	@mkdir -p $(BUILD)
	PYTHONPATH=. $(VPY) -m experiments.a1_driver \
	    --smoke \
	    --db $(BUILD)/a1_results.sqlite \
	    --runner-path $(BUILD)/runners/onpair/onpair_runner

a1: build
	# A1 headline grid. Requires `make datasets` first.
	# NOT part of `make smoke`. Measurement rig only.
	PYTHONPATH=. $(VPY) -m experiments.a1_driver \
	    --datasets-manifest experiments/datasets.yaml \
	    --a1-cells experiments/a1_cells.yaml \
	    --columns-dir datasets/materialized \
	    --db $(BUILD)/a1_results.sqlite \
	    --runner-path $(BUILD)/runners/onpair/onpair_runner

a2-smoke: build
	# A2 ablation smoke gate. Single bit-width probe
	# (compress+bulk_decode) against the committed V-D fixture; <1 sec wall.
	# NOT wired into `make smoke` — A2 is a separate experiment runner.
	@mkdir -p $(BUILD)
	PYTHONPATH=. $(VPY) -m experiments.a2_driver \
	    --smoke \
	    --db $(BUILD)/a2_results.sqlite \
	    --runner-path $(BUILD)/runners/onpair/onpair_runner

a2: build
	# A2 bit-width sweep. Requires `make datasets` first.
	# NOT part of `make smoke`. Measurement rig only.
	PYTHONPATH=. $(VPY) -m experiments.a2_driver \
	    --datasets-manifest experiments/datasets.yaml \
	    --a2-cells experiments/a2_cells.yaml \
	    --columns-dir datasets/materialized \
	    --db $(BUILD)/a2_results.sqlite \
	    --runner-path $(BUILD)/runners/onpair/onpair_runner

a3-smoke: build
	# A3 ablation smoke gate. Single sample_fraction probe
	# against the committed V-D fixture; runs in <1 sec wall.
	# NOT wired into `make smoke` — A3 is a separate experiment runner.
	@mkdir -p $(BUILD)
	PYTHONPATH=. $(VPY) -m experiments.a3_driver \
	    --smoke \
	    --db $(BUILD)/a3_results.sqlite \
	    --runner-path $(BUILD)/runners/onpair/onpair_runner

a3: build
	# A3 sample_fraction sweep. Requires `make datasets` first.
	# NOT part of `make smoke`. Measurement rig only.
	PYTHONPATH=. $(VPY) -m experiments.a3_driver \
	    --datasets-manifest experiments/datasets.yaml \
	    --a3-cells experiments/a3_cells.yaml \
	    --columns-dir datasets/materialized \
	    --db $(BUILD)/a3_results.sqlite \
	    --runner-path $(BUILD)/runners/onpair/onpair_runner

a4-smoke: build
	# A4 ablation smoke gate. Single capacity probe
	# (compress+bulk_decode, peak-RSS M_train) against the committed V-D
	# fixture; <1 sec wall. NOT wired into `make smoke` — A4 is a separate
	# experiment runner.
	@mkdir -p $(BUILD)
	PYTHONPATH=. $(VPY) -m experiments.a4_driver \
	    --smoke \
	    --db $(BUILD)/a4_results.sqlite \
	    --runner-path $(BUILD)/runners/onpair/onpair_runner

a4: build
	# A4 capacity sweep. Requires `make datasets` first.
	# NOT part of `make smoke`. Measurement rig only.
	PYTHONPATH=. $(VPY) -m experiments.a4_driver \
	    --datasets-manifest experiments/datasets.yaml \
	    --a4-cells experiments/a4_cells.yaml \
	    --columns-dir datasets/materialized \
	    --db $(BUILD)/a4_results.sqlite \
	    --runner-path $(BUILD)/runners/onpair/onpair_runner

a5-smoke: build
	# A5 ablation smoke gate. One zstd block-decode cell + one
	# OnPair flat-line cell (the new `ra_latency` op) against the committed V-D
	# fixture; <1 sec wall. NOT wired into `make smoke` — A5 is a separate
	# experiment runner. Drives the zstd + onpair runners via --build-root.
	@mkdir -p $(BUILD)
	PYTHONPATH=. $(VPY) -m experiments.a5_driver \
	    --smoke \
	    --db $(BUILD)/a5_results.sqlite \
	    --build-root $(BUILD)

a5: build
	# A5 block-size sweep (zstd/lz4 L_ra degrade vs block size;
	# OnPair flat). Requires `make datasets` first. NOT part of `make smoke`.
	# Measurement rig only. NOTE: true FS-cache-cold cells need an OS page-cache
	# drop between runs (macOS `sudo purge` / Linux `echo 3 > drop_caches`); the
	# op's warmup flag only controls the in-process pre-pass.
	PYTHONPATH=. $(VPY) -m experiments.a5_driver \
	    --datasets-manifest experiments/datasets.yaml \
	    --a5-cells experiments/a5_cells.yaml \
	    --columns-dir datasets/materialized \
	    --db $(BUILD)/a5_results.sqlite \
	    --build-root $(BUILD)

a6-smoke: build
	# A6 ablation smoke gate. zstd (block) + fsst + onpair
	# (per-string) under Zipf(1.0) × {identity, scramble} via the reused
	# `ra_latency` op (runners/core/query_sampler.h) against the committed V-D
	# fixture; <1 sec wall. NOT wired into `make smoke`. Drives 3 runners via
	# --build-root.
	@mkdir -p $(BUILD)
	PYTHONPATH=. $(VPY) -m experiments.a6_driver \
	    --smoke \
	    --db $(BUILD)/a6_results.sqlite \
	    --build-root $(BUILD)

a6: build
	# A6 query-distribution sweep + layout bracket vs L_ra
	# (zstd/lz4 block baselines degrade under scramble; fsst/onpair per-string
	# flat). Block size fixed at the headline 1 MB budget. Requires
	# `make datasets` first. NOT part of `make smoke`. Measurement rig only.
	# NOTE: true FS-cache-cold cells need an OS page-cache drop between runs
	# (macOS `sudo purge` / Linux `echo 3 > drop_caches`); the op's warmup flag
	# only controls the in-process pre-pass. ~144 cells (4 baselines × 2 cols ×
	# 2 cache × 9 dist/layout) — confirm wall-time before running; the cells
	# yaml carries a fallback_distributions subset that KEEPS the layout bracket
	# + per-string baseline if the budget is exceeded.
	PYTHONPATH=. $(VPY) -m experiments.a6_driver \
	    --datasets-manifest experiments/datasets.yaml \
	    --a6-cells experiments/a6_cells.yaml \
	    --columns-dir datasets/materialized \
	    --db $(BUILD)/a6_results.sqlite \
	    --build-root $(BUILD)

a7-smoke: build
	# A7 ablation smoke gate. Single seed probe (seed=7,
	# compress-only) against the committed V-D fixture; <1 sec wall.
	# NOT wired into `make smoke` — A7 is a separate experiment runner.
	@mkdir -p $(BUILD)
	PYTHONPATH=. $(VPY) -m experiments.a7_driver \
	    --smoke \
	    --db $(BUILD)/a7_results.sqlite \
	    --runner-path $(BUILD)/runners/onpair/onpair_runner

a7: build
	# A7 shuffle-seed sweep (seed-insensitivity of R for V-G;
	# DBLP titles + ClickBench URL only). Requires `make datasets` first.
	# NOT part of `make smoke`. Measurement rig only.
	PYTHONPATH=. $(VPY) -m experiments.a7_driver \
	    --datasets-manifest experiments/datasets.yaml \
	    --a7-cells experiments/a7_cells.yaml \
	    --columns-dir datasets/materialized \
	    --db $(BUILD)/a7_results.sqlite \
	    --runner-path $(BUILD)/runners/onpair/onpair_runner

bench-smoke: build
	# Phase 6 headline smoke gate. Generates a 3-baseline cells doc backed by
	# the committed V-D 1000-row fixture (dataset_kind=real) and drives it through
	# the canonical orchestrator — verifies the full headline wiring on committed
	# data. NOT wired into `make smoke`. Writes a throwaway DB.
	@mkdir -p $(BUILD)
	PYTHONHASHSEED=0 $(VPY) -m experiments.gen_headline_cells \
	    --smoke --out $(BUILD)/headline_cells.smoke.yaml
	PYTHONHASHSEED=0 $(VPY) -m orchestrator.orchestrate \
	    --build-root $(BUILD) \
	    --cells    $(BUILD)/headline_cells.smoke.yaml \
	    --modes    $(MODES) \
	    --needles  $(NEEDLES) \
	    --db       $(BUILD)/headline_smoke.sqlite \
	    --csv      $(BUILD)/headline_smoke.csv \
	    --workdir  $(BUILD)/headline_smoke_workdir
	@echo "bench-smoke: OK"

bench: build
	# Phase 6 headline run (§8.2 / F1 / T1 / T2): full grid = every real column
	# in datasets.yaml × the 11 lossless baselines in headline_roster.yaml,
	# driven through the canonical orchestrator (G6 + round-trip verified) into
	# the headline results.sqlite. Requires `make datasets` first. NOT part of
	# `make smoke`. Measurement rig only.
	# Start from a clean store: the canonical headline grid is a fresh run.
	# a*_results.sqlite are separate stores; smoke uses smoke_results.sqlite.
	@rm -f $(DB) $(CSV)
	PYTHONHASHSEED=0 $(VPY) -m experiments.gen_headline_cells \
	    --out $(BUILD)/headline_cells.yaml
	PYTHONHASHSEED=0 $(VPY) -m orchestrator.orchestrate \
	    --build-root $(BUILD) \
	    --cells    $(BUILD)/headline_cells.yaml \
	    --modes    $(MODES) \
	    --needles  $(NEEDLES) \
	    --db       $(DB) \
	    --csv      $(CSV) \
	    --manifest $(MANIFEST) \
	    --workdir  $(BUILD)/headline_workdir

fingerprint: setup
	# MIGRATION §3 — write build/env_fingerprint.json (AVX-512 / FSST-dispatch
	# fairness record + L_ra timing prereqs). Needs cpp-build for the authoritative
	# FSST-object disassembly; run after 'make build'. Safe to run standalone.
	PYTHONPATH=. $(VPY) -m tools.env_fingerprint --build-root $(BUILD)

figures: setup
	# Phase 6 — render every paper figure/table from whichever *_results.sqlite
	# stores are present (headline results.sqlite + a*_results.sqlite). Missing
	# stores are skipped with a note; safe to run after a partial set of runs.
	$(VPY) -m tools.plot.render_all --build-root $(BUILD) --out $(BUILD)/figures

install-hooks:
	# Activate the versioned pre-commit guard (tools/git-hooks/pre-commit):
	# blocks committing build/ .venv/ datasets/ and any file > 10 MiB. Run this
	# once after `git init` (and once per fresh clone).
	@git rev-parse --is-inside-work-tree >/dev/null 2>&1 || \
	    { echo "install-hooks: not a git repo yet — run 'git init' first."; exit 1; }
	@chmod +x tools/git-hooks/* 2>/dev/null || true
	git config core.hooksPath tools/git-hooks
	@echo "install-hooks: core.hooksPath → tools/git-hooks (pre-commit guard active)."

repo-size:
	# What WOULD git track, and the heaviest tracked files. Use before pushing.
	@git rev-parse --is-inside-work-tree >/dev/null 2>&1 || \
	    { echo "repo-size: not a git repo yet — run 'git init' first."; exit 1; }
	@echo "tracked files: $$(git ls-files | wc -l | tr -d ' ')"
	@git ls-files -z | xargs -0 du -ch 2>/dev/null | tail -1 | sed 's/^/total tracked: /'
	@echo "largest tracked files:"
	@git ls-files -z | xargs -0 du -h 2>/dev/null | sort -rh | head -10

reproduce-all:
	./reproduce.sh all

clean:
	rm -rf $(BUILD) $(VENV) __pycache__ */__pycache__ */*/__pycache__
