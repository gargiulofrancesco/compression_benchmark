#!/bin/bash
# Reproduce the paper "OnPair: Short String Compression for Fast Random Access",
# all of it or any part of it.
#
#   ./reproduce.sh all        everything, the way the paper's numbers were
#                             produced: build, datasets, three passes of the
#                             timing stages, the combined results, and the
#                             tables, figures and claims audit from them
#   ./reproduce.sh <stage>... one or more stages, in the order given (list below)
#   ./reproduce.sh results    tables, figures and audit from the measurements
#                             published with this repository (no datasets
#                             and no special machine needed)
#
# A part of the experiments, for example three baselines on one column:
#   BASELINES="onpair fsst zstd" COLS="tpch/p_name" ./reproduce.sh headline table
# `./reproduce.sh list` prints the baseline and column names.
#
# Stages:
#   build       dependencies, C++ build, test suites
#   datasets    download (about 14 GB) and materialise the six columns
#   smoke       end-to-end check on a committed synthetic column
#   env         how this machine compares with the paper's setup
#   list        names of the baselines and columns that can be selected
#   headline    main grid (Table II): compression ratio, compression and
#               decompression throughput; 6 columns x 11 configurations
#   access      point-access latency (Table III, Fig. 5)
#   decode      paired BPE/OnPair decode and construction phases
#   dictorder   decode speed by dictionary id order (needs decode)
#   bpedecoder  flat BPE decoder vs SentencePiece's own (needs decode)
#   prefix      compressed prefix filter vs raw scan
#   finalize    share of OnPair's training spent in the final sort
#   ablations   thresholds, budget, capacity, seeds, block size,
#               query skew (Figs. 6-7, Sections VIII-C to VIII-E)
#   modelsize   RLZ reference-size sweep (Fig. 7)
#   synthetic   parametric synthetic sweep (Fig. 8)
#   scale       10^5..10^8 rows, OnPair vs Re-Pair (Section VIII-F)
#   repair      Re-Pair's cells of headline and access, alone
#   combine     merge the passes into <out>/combined
#   table       print what has been measured so far, as plain text
#   paper       tables, figures, summary and claims audit
#
# `all` runs one full pass and then repeats the timing stages without Re-Pair,
# three passes in total by default. It is a long run, and most of that time
# goes to the Re-Pair experiments, which alone take longer than a full pass of
# all the others; they come last.
#
# Environment variables:
#   BASELINES="a b"  measure only these baselines (headline, access, repair)
#   COLS="x y"       measure only these columns, given as dataset/column
#                    (datasets, and every stage from headline to ablations;
#                    modelsize, synthetic and scale have columns of their own)
#   OUT=<dir>        where results are written and read (default: build; with
#                    BASELINES or COLS set: build/subset, so that a partial
#                    run never replaces the results of a full one). A stage
#                    replaces what an earlier run of it left in the directory.
#   PASSES=3         passes of the timing stages in `all` (1: a single pass)
#   PASS=<n>         with timing stages: write pass n (2, 3, ...) to
#                    <out>/pass<n>; Re-Pair's cells are left out of such passes
#   CPU=<n>          core every measurement is pinned to (default: the first
#                    performance core other than CPU 0; see `./reproduce.sh env`)
#   BUILD_ROOT       result directory read by `paper` and `table` (default:
#                    <out>/combined if it exists, otherwise <out>)
#   STRICT_ENV=1     refuse to measure unless the machine is set up as for the
#                    paper (see below)
#
# The paper's timings were taken with the performance governor on every CPU
# and turbo off (README, "The paper's setup"). The stages run on any x86-64
# Linux machine; elsewhere their timings are simply those of that machine.
# `./reproduce.sh env` compares the machine with the paper's.
set -euo pipefail
cd "$(dirname "$0")"
VPY=.venv/bin/python
PASSES=${PASSES:-3}
PASS=${PASS:-1}

# The six columns reported in the paper.
PAPER_COLUMNS=(dbpedia/abstract msmarco_urls/url msmarco_queries/query
               amazon/titles tpch/p_name tpch/c_comment)

# The configurations of the main grid (experiments/headline_roster.yaml).
ROSTER=(uncompressed lz4 zstd_l1 zstd zstd_l9 fsst bpe bpe_v65536 rlz repair onpair)

# Selection: BASELINES and COLS take names separated by spaces or commas.
# (Not COLUMNS: the shell sets that one itself, to the width of the terminal.)
BASELINES=${BASELINES:-}; SEL_BASELINES=(${BASELINES//,/ })
COLS=${COLS:-};           SEL_COLUMNS=(${COLS//,/ })
# $1: what is being named; $2: the name; the rest: the valid names.
known() {
  local what=$1 name=$2; shift 2
  case " $* " in
    *" $name "*) ;;
    *) echo "reproduce: unknown $what '$name'; choose from: $*" >&2; exit 1 ;;
  esac
}
for b in "${SEL_BASELINES[@]}"; do known baseline "$b" "${ROSTER[@]}"; done
for c in "${SEL_COLUMNS[@]}"; do known column "$c" "${PAPER_COLUMNS[@]}"; done
SUBSET=$(( ${#SEL_BASELINES[@]} + ${#SEL_COLUMNS[@]} ))
[ ${#SEL_COLUMNS[@]} -gt 0 ] || SEL_COLUMNS=("${PAPER_COLUMNS[@]}")
if [ -z "${OUT:-}" ]; then
  if [ "$SUBSET" -gt 0 ]; then OUT=build/subset; else OUT=build; fi
fi
selected() {              # is baseline $1 part of the selection?
  [ ${#SEL_BASELINES[@]} -eq 0 ] && return 0
  case " ${SEL_BASELINES[*]} " in *" $1 "*) return 0 ;; *) return 1 ;; esac
}
# The selected columns as `name=path` driver arguments, each preceded by $1.
column_args() {
  local flag=$1 c
  for c in "${SEL_COLUMNS[@]}"; do
    printf '%s%s=datasets/materialized/%s.col ' "$flag" "$c" "$c"
  done
}
column_names() {
  local flag=$1 c
  for c in "${SEL_COLUMNS[@]}"; do printf '%s%s ' "$flag" "$c"; done
}
# The selection as --baseline/--column options; $1 names baselines to drop
# because the driver does not measure them.
selection_args() {
  local b c
  for b in "${SEL_BASELINES[@]}"; do
    case " ${1:-} " in *" $b "*) ;; *) printf -- '--baseline %s ' "$b" ;; esac
  done
  [ "$SUBSET" -eq 0 ] || for c in "${SEL_COLUMNS[@]}"; do printf -- '--column %s ' "$c"; done
}

# Say so when stage $1 does not take the selection ($2: what it measures).
whole() {
  [ "$SUBSET" -eq 0 ] || echo "reproduce: $1 has no selection: it measures $2"
}
# The same for a stage that takes COLS but not BASELINES.
columns_only() {
  [ ${#SEL_BASELINES[@]} -eq 0 ] ||
    echo "reproduce: $1 takes COLS only: it always measures $2"
}

# Result directory of pass $1: <out> for the first, <out>/pass<n> afterwards.
root() { if [ "$1" -eq 1 ]; then echo "$OUT"; else echo "$OUT/pass$1"; fi; }
# Result directory that `paper` and `table` read.
reading_root() {
  if [ -n "${BUILD_ROOT:-}" ]; then echo "$BUILD_ROOT"
  elif [ -d "$OUT/combined" ]; then echo "$OUT/combined"
  else echo "$OUT"; fi
}

# The measurement core, resolved once the Python environment exists.
cpu() {
  if [ -z "${CPU:-}" ]; then
    CPU=$(PYTHONPATH=. $VPY -m tools.check_env --print-cpu) ||
      { echo "reproduce: no measurement core found; set CPU=<n>" >&2; exit 1; }
  fi
  echo "$CPU"
}
# Stages that measure time start here ($1: who asks). What the timer needs
# (an invariant TSC, a core the process may run on) is required; the paper's
# setup (performance governor, turbo off) is noted when absent, and required
# only with STRICT_ENV=1. `env` prints the same check in full.
need_clock() {
  if PYTHONPATH=. $VPY -m tools.check_env ${CPU:+--cpu "$CPU"} > /dev/null; then
    PYTHONPATH=. $VPY -m tools.check_env --note | sed "s/^/reproduce: $1: /"
    return 0
  fi
  echo "reproduce: $1 cannot be measured on this machine as it is:" >&2
  PYTHONPATH=. $VPY -m tools.check_env ${CPU:+--cpu "$CPU"} >&2 || true
  exit 1
}
# Run a measurement pinned to that core. Dataset generation is never pinned.
pin() {
  local c; c=$(cpu)
  PIN_CPUS=$c PYTHONHASHSEED=0 PYTHONPATH=. taskset -c "$c" "$@"
}

stage_build()    { make build && make ctest && make pytest; }
# With COLS, only the datasets of the selected columns are prepared.
stage_datasets() {
  local c only=()
  if [ -z "$COLS" ]; then make datasets; return; fi
  for c in "${SEL_COLUMNS[@]}"; do only+=(--dataset "${c%%/*}"); done
  make setup
  PYTHONPATH=. $VPY -m tools.datasets.prepare "${only[@]}" \
      --raw-dir datasets/raw --columns-dir datasets/materialized
}
stage_smoke()    { make smoke; }
stage_env()      { PYTHONPATH=. $VPY -m tools.check_env ${CPU:+--cpu "$CPU"}; }
stage_list()     { PYTHONPATH=. $VPY -m tools.show_results --list; }
stage_table()    { PYTHONPATH=. $VPY -m tools.show_results --build-root "$(reading_root)"; }

# Main grid. $2 selects the cells: all, main (without Re-Pair) or last (only
# Re-Pair, appended to the store that already holds the main part).
headline() {
  local r; r=$(root "$1"); mkdir -p "$r"
  PYTHONPATH=. $VPY -m experiments.gen_headline_cells --part "$2" \
      $(selection_args) --out "$r/headline_cells.$2.yaml"
  [ "$2" = last ] || rm -f "$r/results.sqlite" "$r/results.csv"
  pin $VPY -m orchestrator.orchestrate --build-root build \
      --cells "$r/headline_cells.$2.yaml" --modes experiments/baseline_modes.yaml \
      --needles testdata/g6_needles.txt --db "$r/results.sqlite" \
      --csv "$r/results.csv" --manifest "$r/manifest.$2.json" \
      --workdir "$r/headline_workdir"
}
# Point access. Extra arguments select the baselines (--skip repair, --only
# repair). Zstandard is measured at level 3 only, so the two other levels of
# the main grid are dropped from a selection. Like the main grid, the store
# starts empty unless Re-Pair's cells are being added to it.
access() {
  local r sel; r=$(root "$1"); shift; mkdir -p "$r"
  sel=$(selection_args "zstd_l1 zstd_l9")
  if [ ${#SEL_BASELINES[@]} -gt 0 ] && [[ $sel != *--baseline* ]]; then
    echo "reproduce: point access has no cell for ${SEL_BASELINES[*]}; nothing to do"
    return 0
  fi
  need_clock access
  [[ " $* " == *" --only "* ]] || rm -f "$r/ra_headline_results.sqlite"
  pin $VPY -m experiments.ra_headline_driver --db "$r/ra_headline_results.sqlite" \
      $sel "$@"
}
# Re-Pair has a single pass: later passes leave its cells out.
stage_headline() {
  if [ "$PASS" -eq 1 ]; then headline 1 all
  elif selected repair && [ ${#SEL_BASELINES[@]} -eq 1 ]; then
    echo "reproduce: Re-Pair is measured in the first pass only" >&2; exit 1
  else headline "$PASS" main; fi
}
stage_access() {
  if [ "$PASS" -eq 1 ]; then access 1; else access "$PASS" --skip repair; fi
}
stage_repair() {
  selected repair || { echo "reproduce: Re-Pair is not in BASELINES; nothing to do"; return 0; }
  headline 1 last; access 1 --only repair
}

stage_decode() {
  local r; r=$(root "$PASS")
  columns_only decode "BPE and OnPair"
  need_clock decode
  pin $VPY -m experiments.decode_driver $(column_args '--column ') --cpu "$(cpu)" \
      --workdir "$r/decode" --out-json "$r/decode/results.json" \
      --out-csv "$r/decode/observations.csv"
}
stage_dictorder() {
  local r; r=$(root "$PASS")
  columns_only dictorder "BPE and OnPair"
  need_clock dictorder
  pin $VPY -m experiments.dictionary_order_diagnostic_driver \
      $(column_args '--column ') --cpu "$(cpu)" --wire-root "$r/decode" \
      --workdir "$r/dictorder" --samples 11 --warmups 2
}
stage_bpedecoder() {
  local r; r=$(root 1)
  columns_only bpedecoder "BPE"
  need_clock bpedecoder
  pin $VPY -m experiments.bpe_decode_diagnostic_driver \
      $(column_args '--column ') --cpu "$(cpu)" --wire-root "$r/decode" \
      --workdir "$r/bpedecoder"
}
stage_prefix() {
  local r; r=$(root 1)
  columns_only prefix "OnPair against a scan of the raw column"
  need_clock prefix
  pin $VPY -m experiments.prefix_driver $(column_args '--column ') \
      --cpu "$(cpu)" --lengths 1,4,8,16,32 --workdir "$r/prefix" \
      --out-json "$r/prefix/results.json" --out-csv "$r/prefix/observations.csv"
}
stage_finalize() {
  local r; r=$(root 1)
  columns_only finalize "OnPair"
  need_clock finalize
  mkdir -p "$r/finalization"
  pin build/diagnostics/onpair_finalization_phase_probe "$(cpu)" 7 1 \
      $(column_args '') > "$r/finalization/results.csv"
}

# One ablation grid ($1 = a1..a7) into the result directory of pass $2. With
# COLS, the grid is cut to the selected columns among those it studies.
ablation() {
  local a=$1 r binary cells n; r=$(root "$2"); mkdir -p "$r"
  cells=experiments/${a}_cells.yaml
  rm -f "$r/${a}_results.sqlite"
  if [ -n "$COLS" ]; then
    n=$(PYTHONPATH=. $VPY -m tools.subset_cells "$cells" "$r/${a}_cells.yaml" \
          $(column_names '--column '))
    cells=$r/${a}_cells.yaml
    [ "$n" -gt 0 ] || { echo "reproduce: $a does not study ${SEL_COLUMNS[*]}; skipped"
                        rm -f "$cells"; return 0; }
  fi
  case $a in
    a5|a6) binary=(--build-root build) ;;
    *)     binary=(--runner-path build/runners/onpair/onpair_runner) ;;
  esac
  pin $VPY -m "experiments.${a}_driver" \
      --datasets-manifest experiments/datasets.yaml \
      "--$a-cells" "$cells" \
      --columns-dir datasets/materialized \
      --db "$r/${a}_results.sqlite" "${binary[@]}"
}
# a1, a2, a3 and a7 record compression ratios, which do not vary between
# passes; a4, a5 and a6 record timings and are repeated in every pass.
ablations_ratio()  { local a; for a in a1 a2 a3 a7; do ablation "$a" 1; done; }
ablations_timing() {
  local a
  need_clock "a4, a5 and a6"
  for a in a4 a5 a6; do ablation "$a" "$1"; done
}
stage_ablations()  {
  columns_only ablations "OnPair, and the block-based baselines of a5 and a6"
  [ "$PASS" -gt 1 ] || ablations_ratio; ablations_timing "$PASS"
}

stage_modelsize() {
  local r; r=$(root 1)
  whole modelsize "RLZ on the two columns of Fig. 7"
  rm -f "$r"/rlz_sweep2_results.{sqlite,csv}
  pin $VPY -m experiments.rlz_sweep_driver --out "$r"
}
stage_synthetic() {
  local r; r=$(root 1)
  whole synthetic "generated columns"
  PYTHONPATH=. $VPY tools/gen_vd_columns.py --set sweep
  rm -f "$r"/vd_results.{sqlite,csv}
  pin $VPY -m experiments.f6_driver --out "$r"
}
# In later passes only OnPair's flat throughput is of interest, so Re-Pair,
# which needs 40 minutes to time out at 300 MiB, is cut at 5 minutes.
stage_scale() {
  local r extra=(); r=$(root "$PASS"); mkdir -p "$r"
  whole scale "OnPair and Re-Pair on columns of 10^5 to 10^8 rows"
  need_clock scale
  [ "$PASS" -eq 1 ] || extra=(--op-timeout-s 300)
  PYTHONPATH=. $VPY tools/gen_vd_columns.py --set scale
  rm -f "$r/nsweep_results.sqlite"
  pin $VPY -m experiments.nsweep_driver --db "$r/nsweep_results.sqlite" "${extra[@]}"
}

stage_combine() {
  local p args=(--pass "$OUT")
  for p in $(seq 2 "$PASSES"); do
    [ -d "$OUT/pass$p" ] && args+=(--pass "$OUT/pass$p")
  done
  rm -rf "$OUT/combined"     # derived: always rebuilt from the passes
  PYTHONPATH=. $VPY -m tools.combine_passes "${args[@]}" --out "$OUT/combined"
}

# Tables, figures, the summary of the diagnostic stages and the claims audit,
# read from $1 and written to $2. Further arguments go to the audit.
paper() {
  local b=$1 o=$2; shift 2
  echo "reproduce: paper outputs from $b -> $o"
  PYTHONPATH=. $VPY -m tools.paper_tables --build-root "$b" --out "$o/tables"
  PYTHONPATH=. $VPY -m tools.paper_figures --build-root "$b" --out "$o/figures"
  PYTHONPATH=. $VPY -m tools.dataset_stats --out "$o/tables"
  PYTHONPATH=. $VPY -m tools.experiments_summary --build-root "$b" --out "$o"
  PYTHONPATH=. $VPY -m tools.claims_audit --build-root "$b" "$@"
}
# Measured here: sizes and ratios must match the paper's exactly; a timing
# within 10% of the printed one counts as reproduced, one outside is reported
# as different, which is what another machine gives.
stage_paper() { paper "$(reading_root)" "$OUT/paper" --timing-tolerance 10; }
# The same outputs from the measurements published in results/, every number
# checked to the printed digit.
stage_results() {
  make setup
  PYTHONPATH=. $VPY -m tools.published_results --unpack
  paper results/combined build/paper
}

# Banners carry the time of day, so that a log records how long a stage took,
# and the load average of the last minute: during a pass it is about 1, the
# measurement itself; much more means that other jobs share the machine.
banner() {
  local load; read -r load _ < /proc/loadavg 2>/dev/null || load="?"
  echo "=== reproduce: $* [$(date '+%F %T'), load $load] ==="
}
run() { banner "$1${2:+ (pass $2)}"; PASS=${2:-1} "stage_$1"; }

STAGES="build datasets smoke env list headline access repair decode dictorder bpedecoder prefix finalize ablations modelsize synthetic scale combine table paper results"
usage() { awk 'NR > 1 && !/^#/ { exit } NR > 1' "$0"; exit 1; }   # the header above
[ $# -gt 0 ] || usage
for s in "$@"; do
  case " $STAGES all " in
    *" $s "*) ;;
    *) echo "reproduce: unknown stage '$s'" >&2; usage ;;
  esac
  [ "$s" != all ] || [ $# -eq 1 ] ||
    { echo "reproduce: 'all' must be the only argument" >&2; exit 1; }
done

if [ "$1" != all ]; then
  for s in "$@"; do
    [ $# -eq 1 ] || banner "$s"
    "stage_$s"
  done
  [ $# -eq 1 ] || banner done
  exit 0
fi
[ "$SUBSET" -eq 0 ] || { echo "reproduce: 'all' is the paper's full procedure;" \
  "with BASELINES or COLS name the stages to run instead" >&2; exit 1; }

run build
# Stop here, before the downloads, if the timing stages would refuse to run.
run env
run datasets
run smoke
# First pass. Re-Pair's cells, by far the slowest, come last, so that
# everything else is on disk before they start.
banner "headline, without Re-Pair (pass 1)"; headline 1 main
banner "access, without Re-Pair (pass 1)";   access 1 --skip repair
for s in decode dictorder bpedecoder prefix finalize ablations modelsize synthetic scale; do
  run "$s" 1
done
for p in $(seq 2 "$PASSES"); do
  for s in headline access decode dictorder ablations scale; do run "$s" "$p"; done
done
run repair
[ "$PASSES" -le 1 ] || run combine
run paper
banner done
