# Published measurements

`onpair_icde2027_measurements.tar.xz` holds every measurement behind the
paper's tables, figures and numbers: the result stores of the three passes
and the run logs. To turn them into the paper's tables and figures:

```sh
./reproduce.sh results
```

This extracts the archive here, merges the three passes into `combined/`
with `tools/combine_passes.py`, and writes the tables, the figures, the
summary and the claims audit to `build/paper/`. It needs Python only: no
datasets, no build, no special machine. Tables II and III and Figures 4 to 8
come out identical to the ones in the paper.

## Contents

| Path | What it is |
|---|---|
| `pass1/` | The full pass: every stage, all baselines. |
| `pass2/`, `pass3/` | The timing stages repeated without Re-Pair: main grid, point access, paired decode, id order, capacity sweep, block size, query skew, scale. |
| `superseded/` | Re-Pair's six point-access cells as first measured (see below). |
| `logs/` | Output of each pass, the scripts that drove them, and for pass 3 the clock, governor and thermal counters sampled every 10 seconds. |
| `combined/` | Created by `./reproduce.sh results`; not in the archive. |

Inside a pass:

| File | Stage | Used for |
|---|---|---|
| `results.sqlite` | `headline` | Table II, Fig. 4; one row per metric and repetition (11 repetitions per cell) |
| `ra_headline_results.sqlite` | `access` | Table III, Fig. 5; one row per cell with the latency percentiles |
| `decode/`, `dictorder/` | `decode`, `dictorder` | BPE and OnPair decode by id order, construction phases |
| `finalization/` | `finalize` | share of training spent in the final sort (Section VIII-B) |
| `bpedecoder/`, `prefix/` | `bpedecoder`, `prefix` | additional measurements that the paper does not report |
| `a1`, `a3`, `a7_results.sqlite` | `ablations` | thresholds, budget, seeds (Fig. 6, Section VIII-C) |
| `a2`, `a4_results.sqlite`, `rlz_sweep2_results.sqlite` | `ablations`, `modelsize` | capacity and model size (Fig. 7, Section VIII-D) |
| `a5`, `a6_results.sqlite` | `ablations` | block size and query skew (Section VIII-E) |
| `nsweep_results.sqlite` | `scale` | Section VIII-F |
| `vd_results.sqlite` | `synthetic` | Fig. 8, Section VIII-G |

The point-access stores keep the percentiles of each cell (median, p95,
p99, mean, minimum, maximum over 1,000,000 or 100,000 queries), not the
individual query times.

## How they were measured

- **Machine:** Intel Core Ultra 7 265K, 128 GB of RAM, Ubuntu 26.04, Linux
  7.0, GCC 15.2 with `-O3 -march=native`. Performance governor, turbo off,
  so the clock is fixed at 3.9 GHz. Every measurement ran single-threaded,
  pinned to CPU 1, a performance core.
- **Pass 1:** 9 October 2026, 09:49 to 23:09 (CEST). Re-Pair's cells ran
  last, from 14:23.
- **Pass 2:** 9 October 23:14 to 10 October 02:13.
- **Pass 3:** 10 October 08:14 to 11:15.
- **Code:** commit `6576404` of this repository for the three passes.

Every row has status `ok`, with two exceptions, both in the scale sweep:
Re-Pair did not finish the 300 MiB column within the time limit (`timeout`)
and cannot take the 3 GiB column (`unsupported`).

## Why three passes

Repetitions inside one process agree closely (typically within 0.2%), but a
few timings differ from one process to the next: Zstandard's level-9 decoder
ran at one of two speeds on two columns, and some point-access cells moved
by 10 to 20%. The paper therefore reports medians over three independent
passes: the main grid's 33 repetitions are pooled, and stores with one row
per cell take the median of the three passes. Re-Pair, whose cells take
almost nine hours, has one pass. Compression ratios do not vary at all.

## What you should know about these runs

- **Re-Pair's point-access cells were measured again.** After pass 1 its
  access index was changed from 64-bit to 32-bit positions (commit
  `643d0c7`), which made it 5 to 17% faster, and the six cells were
  re-measured on 10 October, 15:19 to 16:03. `pass1/` carries the
  re-measurement, which is what the paper reports; the first measurement is
  in `superseded/`.
- **Six threshold cells were added to pass 1 afterwards.** The fixed
  threshold T = 10 of the threshold study was measured on 9 October at
  23:14. These cells record compression ratios only.
- **The machine is shared, and other jobs ran during pass 1** on 9 October,
  12:30 to 12:40 and 14:20 to 14:30. The medians over three passes absorb
  the affected cells of the block-size, query-skew and scale sweeps. One
  single-pass cell was touched: five of Re-Pair's eleven compressions of
  `p_name` took about 76 s instead of 55 s; the reported median is one of
  the six undisturbed repetitions.
- **In passes 2 and 3 the scale sweep stops Re-Pair after 5 minutes**
  instead of 40, because only OnPair's numbers are taken from those passes.
- **Passes 1 and 2 have no hardware log.** Thermal throttling cannot be
  excluded from logs there; pass 3, which has one, shows none, and passes 1
  and 2 agree with it within 5% on 59 of 60 compression cells and 58 of 60
  decompression cells.

## Changes made for publication

The stores are the ones the runs wrote, with these changes and no others:

- each SQLite file was copied into a single-file database (no write-ahead
  log beside it);
- paths of the measurement machine inside the JSON files and logs were made
  relative to the repository;
- directory and experiment names follow the stage names used now (`decode`,
  `dictorder`, `bpedecoder`, `prefix`); they were different when the runs
  were made;
- CSV exports that duplicate a store were left out.

Not included: datasets, compressed columns and binaries.
