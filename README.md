# OnPair benchmark suite

The benchmark behind the paper **OnPair: Short String Compression for Fast
Random Access** (F. Gargiulo and R. Venturini, ICDE 2027). Every table,
figure and number in the paper's evaluation is produced by this repository:
each measurement runs a compressor through the same subprocess protocol and
is stored in an SQLite file, and scripts generate the paper's tables and
figures from those files.

The OnPair library itself lives at
[gargiulofrancesco/onpair_cpp](https://github.com/gargiulofrancesco/onpair_cpp);
this repository fetches it at a pinned commit.

## Four ways to use it

| You want to | Run | Needs |
|---|---|---|
| Check the paper against the published measurements | `./reproduce.sh results` | Python |
| Check that everything builds and runs | `make smoke` | compiler, network for three source fetches |
| Measure a part: some baselines, some columns, one study | `BASELINES="onpair fsst zstd" COLS="tpch/p_name" ./reproduce.sh build datasets headline table` | a Linux x86-64 machine; no download for the TPC-H columns |
| Reproduce all the measurements | `./reproduce.sh all` | the same machine, 14 GB of downloads |

The third and fourth are described under
[Reproduce the measurements](#reproduce-the-measurements): every study of the
paper is a stage that can be run on its own, on all the baselines and columns
or on the ones you name.

## Requirements

- Linux on x86-64. Point-access latency is timed with `rdtscp` and needs an
  invariant TSC.
- GCC with C++20 (the paper used GCC 15.2), CMake 3.20 or later, Python 3.12
  or later (the paper used 3.14), `make`, `git`, and `taskset` (util-linux).
- Network access for the datasets and for three source fetches at build time:
  the OnPair library, its Boost.Unordered dependency, and the Abseil release
  that the vendored SentencePiece builds against. Every other baseline is
  vendored under `third_party/`.
- About 50 GB of free disk for a full reproduction: 14 GB of downloads, 6 GB
  of materialised and synthetic columns, and up to 20 GB of working files.
- Memory: the largest peak recorded is 10.4 GiB (the 3 GiB column of the scale
  sweep). Re-Pair's peak on the real columns was not recorded; the paper's
  machine has 128 GB.

Python packages are installed into a local `.venv` by `make setup`, which
every other target runs first. Only DuckDB is pinned (1.5.6: it generates the
TPC-H columns, whose bytes are checksummed). The others take the current
release; this repository was last tested with PyYAML 6.0.3, NumPy 2.5.3,
SciPy 1.18.1, Matplotlib 3.11.2 and pytest 9.1.1.

## Check the paper against the published measurements

```sh
./reproduce.sh results
```

`results/` holds the result stores of the three passes that produced the
paper's numbers, 1.1 MB compressed. The command extracts them, merges the
passes, and writes to `build/paper/`:

| Output | Paper |
|---|---|
| `tables/T2_throughput.tex` | Table II (ratio, compression and decompression throughput) |
| `tables/T3_access.tex` | Table III (point-access latency) |
| `figures/F1_pareto.pdf` | Fig. 4 |
| `figures/F5_latency.pdf` | Fig. 5 |
| `figures/F4_threshold.pdf` | Fig. 6 |
| `figures/F7_modelsize.pdf` | Fig. 7 |
| `figures/F6_synthetic.pdf` | Fig. 8 |
| `tables/T0_datasets.tex` | Table I (written only when the datasets are present) |
| `tables/T1`, `T4`, `T5`, `T6` | more detail than the paper prints: ratios alone, construction time in seconds, ratios with row offsets counted, decode speed by dictionary id order |
| `tables/claims_ledger.json`, `experiments_summary.json` | the numbers behind the tables and the text |

The two tables and five figures come out identical to the ones in the paper.
The command ends with `tools/claims_audit.py`, which recomputes 38 ranges,
factors and comparisons stated in the text of Section VIII from the stores
and prints PASS or FAIL for each.

[results/README.md](results/README.md) describes every file, when and how it
was measured, and what is known about those runs.

## Reproduce the measurements

### The paper's setup

The stages run on any x86-64 Linux machine as it is. Nothing has to be
configured, and no root access is needed: each measurement is pinned to one
core by the script itself (the first performance core other than CPU 0;
`CPU=<n> ./reproduce.sh ...` chooses another), and the only thing required of
the hardware is an invariant TSC, which the latency timer reads.

The paper's own timings were taken at a fixed clock, with the `performance`
frequency governor on every CPU and turbo off:

```sh
sudo cpupower frequency-set -g performance
echo 1 | sudo tee /sys/devices/system/cpu/intel_pstate/no_turbo   # Intel
echo 0 | sudo tee /sys/devices/system/cpu/cpufreq/boost            # AMD
./reproduce.sh env        # compares this machine with the paper's
```

This matters only if you want timings taken the way the paper's were. Without
it the timings are those of your machine as it is: turbo and frequency
scaling change them and make them vary more from run to run, and compression
ratios are not affected at all. What is recorded says which case it was: the
main grid labels its rows `dev-only` when the machine is not in the paper's
setup, the diagnostic stages store the governor, the turbo state, the
frequency limit and the clock held under load in their result files, and each
stage prints a one-line note.

`STRICT_ENV=1` turns the paper's setup into a requirement: the timing stages
then refuse to run on a machine that is not in it (and `decode`, `dictorder`,
`bpedecoder` and `prefix` also verify, where the kernel exposes the base
frequency, that the core is limited to it and holds it within 1% under load).
That is how the published measurements were taken.

Nothing checks what else is running, and no core is isolated: other
processes can still use the caches, the memory and the sibling of the
measurement core. A quiet machine matters, most of all for Re-Pair and for
the largest columns. Every stage is announced with the load average of the
last minute, which is about 1 during a pass (the measurement itself); much
more means that other jobs share the machine, and the timings taken then
should not be trusted.

### One command

```sh
./reproduce.sh all
```

This does what produced the paper's numbers:

1. builds everything and runs the tests, reports how the machine compares
   with the paper's, downloads and materialises the datasets, and runs the
   smoke test;
2. runs every stage once, except Re-Pair's cells;
3. repeats the timing stages twice more, again without Re-Pair;
4. runs Re-Pair's cells, by far the slowest, last;
5. merges the three passes into `build/combined` and writes the tables,
   figures and audit to `build/paper/`.

Run it in `tmux` or `screen`: it is a long run, and most of that time goes to
the Re-Pair experiments, which alone take longer than a full pass of all the
others. `PASSES=1 ./reproduce.sh all` does a single pass. Every stage is
announced with the time of day and the load average, so the log of a run
records how long each stage took and whether the machine was quiet.

### Stage by stage

`./reproduce.sh <stage>...` runs one or more stages in the order given.

| Stage | What it measures | Paper | Result (under `build/`) |
|---|---|---|---|
| `build` | dependencies, C++ build, test suites | | |
| `datasets` | download and materialise the six columns | Table I | `../datasets/` |
| `smoke` | end-to-end run on a committed synthetic column | | |
| `headline` | main grid: 6 columns × 11 configurations | Table II, Fig. 4 | `results.sqlite` |
| `access` | point-access latency | Table III, Fig. 5 | `ra_headline_results.sqlite` |
| `decode` | BPE and OnPair compressed once and decoded in one paired run | feeds `dictorder`; not reported itself | `decode/` |
| `dictorder` | decode speed by dictionary id order | Table II (D of BPE), Fig. 4 (hollow marks), Section VIII-B3 | `dictorder/` |
| `bpedecoder` | flat BPE decoder against SentencePiece's own | not reported | `bpedecoder/` |
| `prefix` | compressed prefix filter against a raw scan | not reported | `prefix/` |
| `finalize` | share of OnPair's training spent in the final sort | Section VIII-B2 | `finalization/` |
| `ablations` | thresholds, budget, seeds; capacity; block size, query skew | Figs. 6, 7; Sections VIII-C to E | `a1` … `a7_results.sqlite` |
| `modelsize` | RLZ reference-size sweep | Fig. 7 | `rlz_sweep2_results.sqlite` |
| `synthetic` | parametric synthetic sweep, on nine generated columns | Fig. 8 | `vd_results.sqlite` |
| `scale` | 10⁵ to 10⁸ rows, OnPair and Re-Pair, on four generated columns | Section VIII-F | `nsweep_results.sqlite` |
| `repair` | Re-Pair's cells of `headline` and `access`, alone | | appended to both stores |
| `combine` | merge the passes | | `combined/` |
| `table` | print what has been measured so far, as plain text: the main grid, point access, and a block for each ablation and sweep | | |
| `paper` | tables, figures, summary, audit | | `paper/` |

Two more stages measure nothing: `env` compares the machine with the paper's
setup, and `list` prints the names of the baselines and columns.

The eleven configurations of the main grid are the nine rows of Table II,
the uncompressed column, and a second BPE with a 2¹⁴ vocabulary that the
paper does not report.
`decode` must run before `dictorder` and `bpedecoder`, which reuse its
compressed columns; the other measuring stages are independent once `build`
and `datasets` have run. A stage starts from an empty store: running it again
replaces what its previous run left. `PASS=2 ./reproduce.sh headline access`
writes a second pass of those stages to `build/pass2/`, without Re-Pair's
cells. `paper` and `table` read `build/combined` if it exists and `build`
otherwise; `BUILD_ROOT=<dir>` makes them read any other directory of stores.

Re-Pair's cells are slow because Re-Pair is: its exhaustive grammar
construction, repeated for each of the 11 repetitions, is one of the paper's
measured results, not harness overhead.

### A part of the experiments

`BASELINES` and `COLS` narrow a stage to the baselines and columns named,
separated by spaces or commas; `./reproduce.sh list` prints the names. To
measure the compression ratio, compression throughput and decompression
throughput of three baselines on one column, and nothing else:

```sh
BASELINES="onpair fsst zstd" COLS="tpch/p_name" ./reproduce.sh headline table
```

It ends with:

```
tpch/p_name  (62 MiB)
  baseline        ratio   compress  decompress  runs
                             MiB/s       GiB/s
  zstd             3.87      304.6        1.45    11
  fsst             2.97      322.9        6.46    11
  onpair           5.19      161.8        8.62    11
```

The cells are those of the full grid, measured the same way: 11 repetitions,
the same core, the same correctness checks. Adding `access` to the stages
adds the median and 99th-percentile point-access latency to the table.
`table` prints a block for every other study whose results it finds as well:
`COLS="msmarco_urls/url" ./reproduce.sh ablations table` ends with the
threshold, budget, capacity, seed, block-size and query-skew numbers of that
column.

- `BASELINES` applies to `headline`, `access` and `repair`. The other studies
  measure fixed schemes (for instance `decode`: BPE and OnPair) and say so
  when the variable is set.
- `COLS` applies to `datasets` and to every measuring stage from `headline`
  to `ablations`. With it, `datasets` prepares only the sources of the
  columns named: the two TPC-H columns are generated locally and need no
  download. An ablation grid that does not study a named column
  is skipped. `modelsize`, `synthetic` and `scale` have columns of their own.
  (The variable is not called `COLUMNS` because the shell sets that one
  itself, to the width of the terminal.)
- With either variable set, results go to `build/subset/` instead of
  `build/`, so that a partial run never replaces the results of a full one;
  `OUT=<dir>` chooses another directory. Later commands on those results
  take the same selection or the same `OUT`:
  `OUT=build/subset ./reproduce.sh table`.
- Passes work as for the full grid: `PASS=2` with the same selection writes
  to `build/subset/pass2/`, and `combine` pools the passes.
- `paper` on a partial directory writes the tables with `--` in the cells
  that were not measured, draws the figures whose stores are present, and
  marks as skipped the audited claims whose numbers are missing.
- `all` is the paper's full procedure and refuses a selection.

More examples:

```sh
BASELINES="onpair fsst" ./reproduce.sh headline access table      # two baselines, six columns
COLS="tpch/p_name tpch/c_comment" ./reproduce.sh decode dictorder # id-order study on two columns
COLS="msmarco_urls/url" ./reproduce.sh ablations                  # every ablation on one column
OUT=build/again ./reproduce.sh modelsize                          # one study, kept apart from build/
```

### What to expect

- **Compression ratios reproduce exactly**, whatever the machine and its
  load: the datasets are verified by checksum and every compressor is
  deterministic. They depend on the C++ standard library in one respect:
  OnPair, BPE and RLZ draw their training sample with `std::shuffle` or
  `std::uniform_int_distribution`, whose output the C++ standard leaves to
  each library. With GCC's libstdc++, which the requirements ask for, the
  ratios are the paper's (GCC 14.2 and 15.2 print the same ones). LLVM's
  libc++ would draw another sample and move them by about what a change of
  seed does, at most 0.4% for OnPair in the paper's seed study.
- **The audit at the end of `paper` separates the two kinds of number.** A
  claim about sizes or ratios must match the paper exactly, on any machine,
  and is reported as FAIL if it does not. A timing within 10% of the printed
  value counts as reproduced (PASS); a claim whose timings fall outside is
  reported as DIFF, with the measured values, and does not make the command
  fail. On another machine most of the timing claims are DIFF: that is the
  machine, not an error.
- **Timings scale with the machine.** In the paper's setup, repetitions
  inside one process agree closely (typically within 0.2%), but a few cells
  differ from one process to the next: Zstandard's level-9 decoder ran at
  one of two speeds on two columns, and some point-access cells moved by 10
  to 20%. This is why the paper reports medians over three passes.
- FSST has an AVX-512 encoder path, used only where the CPU provides it. The
  paper's machine has AVX2 only, so there FSST encodes with its scalar path;
  on an AVX-512 machine FSST's compression throughput would be higher and no
  other number would change. `./reproduce.sh env` shows which path is built.

## Datasets

`./reproduce.sh datasets` downloads each source, verifies it, extracts the
column and checks the result against the SHA-256 recorded in
`experiments/datasets.yaml`. A column that differs fails the stage.

| Column | Source | Download |
|---|---|---|
| DBpedia abstracts | DBpedia short abstracts, 2022.12.01 dump; first 1,000,000 abstracts | 617 MB |
| MS MARCO URLs | MS MARCO document ranking, `msmarco-docs.tsv.gz` | 8.4 GB |
| MS MARCO queries | MS MARCO document ranking, `queries.tar.gz` | 19 MB |
| Amazon book titles | Amazon Reviews 2023 (McAuley Lab), Books metadata | 4.9 GB |
| TPC-H `p_name`, `c_comment` | generated locally with DuckDB's `tpch` extension, scale factor 10 | none |

Columns are kept in the order of their source and never sorted or shuffled;
DBpedia's cap is the only subsetting. The raw downloads of DBpedia and MS
MARCO are also verified against recorded checksums; for Amazon and TPC-H only
the extracted columns are. `--extra-columns` (see
`tools/datasets/prepare.py`) also materialises columns that the manifest
declares and no experiment of the paper reads. The licences of the datasets
are listed in `LICENSING`.

## How things are measured

- Everything is single-threaded: no runner starts a thread, the runners that
  take a thread count (OnPair, BPE, RLZ) reject any value other than one,
  SentencePiece is called with one thread, and Zstandard is built without
  its multi-threaded code.
- Compression time includes training or construction for every scheme that
  has one (OnPair, BPE, FSST, RLZ, Re-Pair), and OnPair's final dictionary
  sort.
- Compression ratio counts the payload: dictionary, token stream, blocks or
  grammar. The per-row offsets, which every scheme needs in some form, are
  left out for all of them. `T5_complete.tex` gives the ratios with them
  counted.
- Decompression times the decode of the payload into a pre-allocated
  buffer; file I/O and allocation are outside the timer.
- Point access times 1,000,000 uniform queries per per-string scheme and
  100,000 per block scheme, one by one, after a warm-up pass. A block scheme
  decodes the enclosing 1 MiB block on every query. Re-Pair reaches a row by
  descending its grammar through an index built when the column is loaded;
  that index is counted in neither its ratio nor its compression time.
- In the main grid, every cell must decode back to the input byte for byte,
  and return the right row for a point-access probe, before its measurements
  are recorded.
- BPE is SentencePiece's: it trains the vocabulary on the same 15% sample as
  OnPair and tokenizes the column. Decoding uses a flat decoder of the same
  design as OnPair's, written in this repository.

The per-baseline configuration and the reasons for it are in
`docs/benchmark/fairness/`.

## Baselines

| Baseline | Version | Where |
|---|---|---|
| OnPair | commit `f8ecc64` of `onpair_cpp`, with `external/onpair_local_shims.patch` | fetched at build time |
| Zstandard | 1.5.6 | `third_party/zstd` |
| LZ4 | 1.10.0 | `third_party/lz4` |
| FSST | upstream commit `e638d4c` | `third_party/fsst` |
| BPE | SentencePiece 0.2.2 | `third_party/sentencepiece` |
| Re-Pair | libCSD, commit `568bf8a` | `third_party/libcsd` |
| RLZ | implemented here after Hoobin et al., on libdivsufsort 2.0.1 | `runners/rlz`, `third_party/libdivsufsort` |

The patch to the OnPair library does not touch the algorithm. It exposes the
number of sample bytes the trainer consumed and the dictionary's two size
components, and adds a switch, off by default, that keeps the dictionary in
construction order for the id-order study.

## Layout

| Path | Role |
|---|---|
| `reproduce.sh` | the stages, and `all` |
| `runners/` | one C++ runner per baseline, speaking a line-based JSON protocol over stdio |
| `orchestrator/` | drives the runners: cells, environment probe, correctness gate, SQLite store |
| `experiments/` | grid definitions (YAML) and one driver per study |
| `tools/` | dataset extractors, table and figure generators, pass merging, claims audit |
| `results/` | the published measurements |
| `schema/` | result-store definitions |
| `tests/` | C++ (ctest) and Python (pytest) suites |
| `third_party/` | vendored baselines, each with a `PROVENANCE.txt` (URL, commit, licence) |
| `external/` | the pin and the patch for the OnPair library |
| `docs/benchmark/` | architecture and per-baseline configuration notes |

## Citing

```bibtex
@inproceedings{gargiulo2027onpair,
  author    = {Francesco Gargiulo and Rossano Venturini},
  title     = {{OnPair}: Short String Compression for Fast Random Access},
  booktitle = {IEEE International Conference on Data Engineering (ICDE)},
  year      = {2027}
}
```

## Licences

The code of this repository is under the MIT licence (`LICENSE`). Each
runner binary inherits the licences of the libraries it links, and the
datasets have their own terms: see `LICENSING` and
`third_party/*/PROVENANCE.txt`.
