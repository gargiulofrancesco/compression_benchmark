#!/bin/bash
# Reproduce every result in the paper, stage by stage.
#
#   ./reproduce.sh <stage>      run one stage (see list below)
#   ./reproduce.sh all          run everything in order
#
# Stages (wall-clock estimates from a Core Ultra 7 265K at a fixed 3.9 GHz):
#   build       deps + C++ build + full test suite              (~10 min)
#   datasets    download + materialise all datasets (~25 GB)    (1-3 h, network)
#   smoke       end-to-end functional check, synthetic data     (~5 min)
#   headline    main grid: 6 columns x 9 baseline configs       (~12 h; BPE-64K
#               training and Re-Pair grammar construction dominate)
#   access      point-access latency grid (Table on access)     (~1 h)
#   ablations   budget/threshold, capacity, seeds, block size,
#               query skew (A1-A7)                              (~8 h)
#   modelsize   RLZ reference-size sweep (model-size figure)    (~1 h)
#   synthetic   parametric synthetic sweep (synthetic figure)   (~1 h)
#   scale       N-sweep 10^5..10^8 rows, OnPair vs Re-Pair      (~6 h)
#   paper       regenerate paper tables + figures + claims audit (~1 min)
#
# Environment control (required for every timing stage):
#   performance governor on all CPUs AND turbo disabled. See README
#   "Environment control". Stages refuse to run otherwise (status=refused-env).
set -euo pipefail
cd "$(dirname "$0")"
VPY=.venv/bin/python

stage_build()     { make build && make ctest && make pytest; }
stage_datasets()  { make datasets; }
stage_smoke()     { make smoke; }
stage_headline()  { make bench; }
stage_access()    { PYTHONPATH=. $VPY -m experiments.ra_headline_driver; }
stage_ablations() { make a1 a2 a3 a4 a5 a6 a7; }
stage_modelsize() { PYTHONPATH=. $VPY -m experiments.rlz_sweep_driver; }
stage_synthetic() { $VPY tools/gen_vd_columns.py && PYTHONPATH=. $VPY -m experiments.f6_driver; }
stage_scale()     { PYTHONPATH=. $VPY -m experiments.nsweep_driver; }
stage_paper()     {
  PYTHONPATH=. $VPY -m tools.paper_tables
  PYTHONPATH=. $VPY -m tools.paper_figures
  PYTHONPATH=. $VPY -m tools.claims_audit
}

case "${1:-}" in
  build|datasets|smoke|headline|access|ablations|modelsize|synthetic|scale|paper)
    "stage_$1" ;;
  all)
    for s in build datasets smoke headline access ablations modelsize synthetic scale paper; do
      echo "=== reproduce: $s ==="
      "stage_$s"
    done ;;
  *)
    sed -n '2,23p' "$0"; exit 1 ;;
esac
