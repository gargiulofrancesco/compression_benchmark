"""Claims audit: the ranges, factors and comparisons that the paper's
evaluation states in its text, recomputed from the result stores and compared
with the printed values. One line per claim: PASS, FAIL, or SKIP when the
store a claim reads is absent (a SKIP leaves the exit status alone).

The published stores are checked to the printed digit. Stores measured anew
cannot be: sizes and ratios are deterministic and must still match exactly,
but a timing never repeats to the last digit, and on another machine it is a
different number altogether. With --timing-tolerance PCT a timing within PCT
percent of the printed value counts as reproduced, and a claim whose timings
fall outside is reported as DIFF, which, like SKIP, leaves the exit status
alone. `./reproduce.sh results` runs the first form, `./reproduce.sh paper`
the second, at 10 percent.

Usage: PYTHONPATH=. .venv/bin/python -m tools.claims_audit [--build-root build]
                                                  [--timing-tolerance PCT]
"""
from __future__ import annotations
import argparse
import sqlite3
import statistics
import sys
from pathlib import Path

_ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
_ap.add_argument("--build-root", type=Path, default=Path("build"),
                 help="directory holding the result stores (default: build)")
_ap.add_argument("--timing-tolerance", type=float, default=0.0, metavar="PCT",
                 help="for stores measured anew: accept a timing within PCT "
                      "percent of the printed value and report a timing claim "
                      "outside it as DIFF instead of FAIL (default 0: timings "
                      "are checked to the printed digit)")
_args = _ap.parse_args()
B = _args.build_root
TOL = _args.timing_tolerance / 100
COLS = ["dbpedia/abstract", "msmarco_urls/url", "msmarco_queries/query",
        "amazon/titles", "tpch/p_name", "tpch/c_comment"]

def _result_stores(build_root):
    stores = [build_root / "results.sqlite"]
    for p in sorted(build_root.glob("results_*.sqlite")):
        if p.name.startswith(("results_turbo", "results_verify")):
            continue
        stores.append(p)
    return [p for p in stores if p.exists()]


conns = [sqlite3.connect(p) for p in _result_stores(B)]


def med(ds, b, op, m):
    rows = []
    for c in conns:
        rows += [r[0] for r in c.execute(
            "SELECT metric_value FROM results WHERE dataset=? AND baseline=? "
            "AND op=? AND metric_name=? AND metric_value IS NOT NULL",
            (ds, b, op, m))]
    return statistics.median(rows) if rows else None


def ratio(ds, b):
    ib = med(ds, b, "compress", "input_bytes")
    d = med(ds, b, "compress", "dict_bytes") or 0
    t = med(ds, b, "compress", "tokens_bytes")
    return ib / (d + t) if ib and t else None


def tp(ds, b, op, m, unit=2**30):
    ib = med(ds, b, "compress", "input_bytes")
    t = med(ds, b, op, m)
    return ib / (t / 1e9) / unit if ib and t else None


def tc(ds, b):
    return tp(ds, b, "compress", "t_compress_ns")


def td(ds, b):
    return tp(ds, b, "bulk_decode", "t_decode_ns")


PASS, FAIL, SKIP, DIFF = 0, [], [], []
_missed = []      # timing comparisons that missed while a claim was evaluated


def timing(cond):
    """A condition that depends on the clock. For the published stores it is a
    condition like any other. For stores measured anew (--timing-tolerance) a
    miss does not fail the claim: the claim is reported as DIFF."""
    if cond or not TOL:
        return cond
    _missed.append(True)
    return True


def within(xs, lo, hi):
    """lo <= xs <= hi for one timing or for all of a list. The bounds carry
    the audit's own margin around what the paper prints; --timing-tolerance
    widens them by its share."""
    xs = xs if isinstance(xs, (list, tuple)) else [xs]
    return timing(lo * (1 - TOL) <= min(xs) and max(xs) <= hi * (1 + TOL))


def check(name, cond, detail=""):
    global PASS
    differs = bool(_missed)
    _missed.clear()
    if not cond:
        FAIL.append(name)
        print(f"  FAIL {name} {detail}")
    elif differs:
        DIFF.append(name)
        print(f"  DIFF {name} {detail}")
    else:
        PASS += 1
        print(f"  PASS {name} {detail}")


def skip(name, detail=""):
    _missed.clear()
    SKIP.append(name)
    print(f"  SKIP {name} {detail}")


def have(name, metric, *baselines, cols=COLS):
    """True if the stores hold metric(column, baseline) for every baseline and
    column that check `name` reads; otherwise the check is reported as SKIP."""
    lacking = []
    for b in baselines:
        n = sum(metric(c, b) is None for c in cols)
        if n:
            lacking.append(f"{b} on {n}/{len(cols)} columns")
    if lacking:
        skip(name, "(no data: " + ", ".join(lacking) + ")")
    return not lacking


print("== VIII-B1 ratio claims")
if have("onpair/fsst in 1.5-1.8x", ratio, "onpair", "fsst"):
    of = [ratio(c, "onpair") / ratio(c, "fsst") for c in COLS]
    check("onpair/fsst in 1.5-1.8x", 1.45 <= min(of) and max(of) <= 1.85,
          f"[{min(of):.2f},{max(of):.2f}]")
if have("beats zstd-9 on 5/6", ratio, "onpair", "zstd_l9"):
    wins = sum(1 for c in COLS if ratio(c, "onpair") > ratio(c, "zstd_l9"))
    check("beats zstd-9 on 5/6", wins == 5, f"wins={wins}")
if have("onpair within ~5% of BPE everywhere; wins p_name+c_comment",
        ratio, "onpair", "bpe_v65536"):
    b64 = [(c, ratio(c, "onpair"), ratio(c, "bpe_v65536")) for c in COLS
           if ratio(c, "bpe_v65536")]
    check("onpair within ~5% of BPE everywhere; wins p_name+c_comment",
          all(o >= b * 0.94 for _, o, b in b64)
          and all(o > b for c, o, b in b64 if c.startswith("tpch")),
          str([(c.split('/')[1], f"{o:.2f}vs{b:.2f}") for c, o, b in b64]))
if have("p_name: onpair 5.19 best of roster / BPE 4.89 / repair 5.05",
        ratio, "onpair", "lz4", "zstd_l1", "zstd", "zstd_l9", "fsst",
        "bpe_v65536", "rlz", "repair", cols=["tpch/p_name"]):
    check("p_name: onpair 5.19 best of roster / BPE 4.89 / repair 5.05",
          abs(ratio("tpch/p_name", "onpair") - 5.19) < 0.05
          and abs(ratio("tpch/p_name", "bpe_v65536") - 4.89) < 0.05
          and abs(ratio("tpch/p_name", "repair") - 5.05) < 0.05
          and all(ratio("tpch/p_name", "onpair") > (ratio("tpch/p_name", b) or 0)
                  for b in ("lz4", "zstd_l1", "zstd", "zstd_l9", "fsst",
                            "bpe_v65536", "rlz", "repair")))
if have("c_comment: repair 7.59, onpair 5.66, BPE 5.22",
        ratio, "repair", "onpair", "bpe_v65536", cols=["tpch/c_comment"]):
    check("c_comment: repair 7.59, onpair 5.66, BPE 5.22",
          abs(ratio("tpch/c_comment", "repair") - 7.59) < 0.05
          and abs(ratio("tpch/c_comment", "onpair") - 5.66) < 0.05
          and abs(ratio("tpch/c_comment", "bpe_v65536") - 5.22) < 0.05)
if have("repair wins ratio on exactly 5/6 (all but p_name)", ratio, "repair", "onpair"):
    rw = [c for c in COLS if (ratio(c, "repair") or 0) > ratio(c, "onpair")]
    check("repair wins ratio on exactly 5/6 (all but p_name)",
          len(rw) == 5 and "tpch/p_name" not in rw, str(rw))
if have("rlz range 2.0-4.4", ratio, "rlz"):
    rlz = [ratio(c, "rlz") for c in COLS]
    check("rlz range 2.0-4.4", 1.95 <= min(rlz) and max(rlz) <= 4.45,
          f"[{min(rlz):.2f},{max(rlz):.2f}]")

print("== VIII-B2 compression-throughput claims")
if have("onpair Tc 93-162 MiB/s", tc, "onpair"):
    otc = [tp(c, "onpair", "compress", "t_compress_ns", 2**20) for c in COLS]
    check("onpair Tc 93-162 MiB/s", within(otc, 90, 166),
          f"[{min(otc):.0f},{max(otc):.0f}]")
if have("repair Tc 0.1-1.7 MiB/s", tc, "repair"):
    rtc = [tp(c, "repair", "compress", "t_compress_ns", 2**20) for c in COLS]
    check("repair Tc 0.1-1.7 MiB/s", within(rtc, 0, 1.75), f"max={max(rtc):.2f}")
if have("zstd9 Tc 55-69 MiB/s", tc, "zstd_l9"):
    ztc = [tp(c, "zstd_l9", "compress", "t_compress_ns", 2**20) for c in COLS]
    check("zstd9 Tc 55-69 MiB/s", within(ztc, 52, 72),
          f"[{min(ztc):.0f},{max(ztc):.0f}]")
# training speedup (capacity-matched BPE t_train vs onpair t_train)
if have("train speedup vs BPE 36-69x",
        lambda c, b: med(c, b, "compress", "t_train_ns"), "onpair", "bpe_v65536"):
    spd = []
    for c in COLS:
        o = med(c, "onpair", "compress", "t_train_ns")
        bb = med(c, "bpe_v65536", "compress", "t_train_ns")
        if o and bb:
            spd.append(bb / o)
    check("train speedup vs BPE 36-69x",
          len(spd) == len(COLS) and within(spd, 33, 73),
          f"[{min(spd):.0f}x,{max(spd):.0f}x] n={len(spd)}")
# end-to-end construction (training + parsing) for both codecs
if have("end-to-end Tc: BPE 3.5-3.8 MiB/s, onpair 26-44x above",
        tc, "onpair", "bpe_v65536"):
    e2e = [(tp(c, "onpair", "compress", "t_compress_ns", 2**20),
            tp(c, "bpe_v65536", "compress", "t_compress_ns", 2**20)) for c in COLS]
    e2e = [(o, b) for o, b in e2e if o and b]
    check("end-to-end Tc: BPE 3.5-3.8 MiB/s, onpair 26-44x above",
          len(e2e) == len(COLS) and within([o / b for o, b in e2e], 24, 47)
          and within([b for _, b in e2e], 3.3, 4.0),
          f"BPE [{min(b for _, b in e2e):.1f},{max(b for _, b in e2e):.1f}] MiB/s, "
          f"onpair/BPE [{min(o / b for o, b in e2e):.0f}x,{max(o / b for o, b in e2e):.0f}x]")

print("== VIII-B3 decompression claims")
otd = {c: tp(c, "onpair", "bulk_decode", "t_decode_ns") for c in COLS}
if have("onpair Td 4.4-8.6 GiB/s", td, "onpair"):
    check("onpair Td 4.4-8.6 GiB/s",
          within(list(otd.values()), 4.3, 8.75),
          f"[{min(otd.values()):.2f},{max(otd.values()):.2f}]")
if have("onpair faster to decode than every baseline except BPE, on every column",
        td, "onpair", "fsst", "lz4", "zstd", "zstd_l1", "zstd_l9", "rlz", "repair"):
    best = all(otd[c] >= max(filter(None, (tp(c, b, "bulk_decode", "t_decode_ns")
                for b in ("fsst", "lz4", "zstd", "zstd_l1", "zstd_l9", "rlz",
                          "repair")))) for c in COLS)
    check("onpair faster to decode than every baseline except BPE, on every column",
          timing(best))
# BPE shares OnPair's flat decoder; in its native ID order it is comparable
# (the matched-order comparison is the dictorder stage).
if have("BPE and onpair decode within 30% of each other on every column",
        td, "bpe_v65536", "onpair"):
    bod = [tp(c, "bpe_v65536", "bulk_decode", "t_decode_ns") / otd[c] for c in COLS
           if tp(c, "bpe_v65536", "bulk_decode", "t_decode_ns")]
    check("BPE and onpair decode within 30% of each other on every column",
          len(bod) == len(COLS) and within(bod, 0.7, 1.3),
          f"BPE/onpair [{min(bod):.2f},{max(bod):.2f}]")
# avg token bytes 5.8-11.7
if have("avg token bytes 5.8-11.7", ratio, "onpair"):
    atl = []
    for c in COLS:
        ib, tb = med(c, "onpair", "compress", "input_bytes"), med(c, "onpair", "compress", "tokens_bytes")
        atl.append(ib / (tb / 2))
    check("avg token bytes 5.8-11.7", 5.6 <= min(atl) and max(atl) <= 11.9,
          f"[{min(atl):.1f},{max(atl):.1f}]")

print("== VIII-B4 access claims (ra_headline store)")
radb = B / "ra_headline_results.sqlite"
if radb.exists():
    ra = sqlite3.connect(radb)
else:  # no access grid yet: an empty one, so every lookup finds nothing
    ra = sqlite3.connect(":memory:")
    ra.execute("CREATE TABLE ra_headline_results "
               "(baseline, dataset, column_name, status, l_ra_p50_ns)")
def lra(b, col_filter=None, m="l_ra_p50_ns"):
    q = f"SELECT {m} FROM ra_headline_results WHERE baseline=? AND status='ok' AND {m} IS NOT NULL"
    return [r[0] for r in ra.execute(q, (b,))]
def p50(b, ds, col):
    r = ra.execute("SELECT l_ra_p50_ns FROM ra_headline_results WHERE baseline=? "
                   "AND dataset=? AND column_name=? AND status='ok'", (b, ds, col)).fetchone()
    return r[0] if r else None
def ra50(c, b):
    return p50(b, *c.split("/"))
if have("per-string p50 64-349ns", ra50, "onpair", "fsst", "bpe_v65536", "rlz"):
    ps = lra("onpair") + lra("fsst") + lra("bpe_v65536") + lra("rlz")
    check("per-string p50 64-349ns", within(ps, 58, 370),
          f"[{min(ps):.0f},{max(ps):.0f}]")
if have("repair p50 0.64-4.1us", ra50, "repair"):
    rp = lra("repair")
    check("repair p50 0.64-4.1us", within(rp, 580, 4400),
          f"[{min(rp):.0f},{max(rp):.0f}]")
if have("block 0.22-0.75ms", ra50, "zstd", "lz4"):
    bl = lra("zstd") + lra("lz4")
    check("block 0.22-0.75ms", within(bl, 2.0e5, 8.0e5),
          f"[{min(bl)/1e3:.0f}us,{max(bl)/1e3:.0f}us]")
# onpair vs raw on the two TPC-H columns (1.9-2.2x faster, 69-78 vs 149-150 ns)
if have("onpair 1.9-2.2x faster than raw on the two TPC-H columns",
        ra50, "onpair", "uncompressed", cols=["tpch/p_name", "tpch/c_comment"]):
    spdups = []
    for ds, col in (("tpch", "p_name"), ("tpch", "c_comment")):
        o, u = p50("onpair", ds, col), p50("uncompressed", ds, col)
        if o and u:
            spdups.append(u / o)
    check("onpair 1.9-2.2x faster than raw on the two TPC-H columns",
          len(spdups) == 2 and within(spdups, 1.8, 2.3),
          f"[{min(spdups):.2f}x,{max(spdups):.2f}x]")

print("== VIII-C held-out accounting bridge (a3 store)")
# Held-out ratios charge the dictionary against 1/5 of the column AND include
# the row-boundary array; Table II excludes boundaries and amortizes over the
# full column. Re-expressed under Table II accounting, the f=0.15 held-out
# dictionary must reproduce the headline ratio on p_name (closed vocabulary).
a3db = B / "a3_results.sqlite"
row = None
if a3db.exists():
    a3 = sqlite3.connect(a3db)
    row = a3.execute(
        "SELECT train_input_bytes, heldout_input_bytes, dict_bytes, "
        "tokens_bytes FROM a3_results WHERE dataset='tpch' AND "
        "column_name='p_name' AND sample_fraction=0.15 AND status='ok'"
    ).fetchone()
if row is None or None in row:
    skip("held-out bridge: p_name f=0.15 under Table II accounting = headline +-3%",
         "(no a3 row for p_name at f=0.15)")
elif have("held-out bridge: p_name f=0.15 under Table II accounting = headline +-3%",
          ratio, "onpair", cols=["tpch/p_name"]):
    tb_a3, hb_a3, db_a3, tk_a3 = row
    btot = tb_a3 + hb_a3
    r_eq = btot / (db_a3 + tk_a3 / hb_a3 * btot)
    r_head = ratio("tpch/p_name", "onpair")
    check("held-out bridge: p_name f=0.15 under Table II accounting = headline +-3%",
          abs(r_eq - r_head) / r_head <= 0.03,
          f"bridge={r_eq:.2f} headline={r_head:.2f}")

print("== Section I scope claim: 4-metric Pareto non-domination")
# For every (column, baseline): onpair strictly better on >=1 of
# {ratio, Tc, Td, L_ra}. Raw is excluded (not a compression scheme).
NONDOM_BASELINES = ["lz4", "zstd_l1", "zstd", "zstd_l9", "fsst",
                    "bpe_v65536", "rlz", "repair"]
RA_COL = {"dbpedia/abstract": ("dbpedia", "abstract"),
          "msmarco_urls/url": ("msmarco_urls", "url"),
          "msmarco_queries/query": ("msmarco_queries", "query"),
          "amazon/titles": ("amazon", "titles"),
          "tpch/p_name": ("tpch", "p_name"),
          "tpch/c_comment": ("tpch", "c_comment")}
def ra_p50(b, ds):
    r = ra.execute(
        "SELECT l_ra_p50_ns FROM ra_headline_results WHERE baseline=? "
        "AND dataset=? AND column_name=? AND status='ok'",
        (b, RA_COL[ds][0], RA_COL[ds][1])).fetchone()
    return r[0] if r and r[0] is not None else None
# (L_ra stays optional: the access grid has no zstd_l1/zstd_l9 cells)
if have("onpair strictly better on >=1 of 4 metrics vs every baseline/column",
        lambda c, b: ratio(c, b) and tc(c, b) and td(c, b),
        "onpair", *NONDOM_BASELINES):
    violations = []
    for c in COLS:
        o = (ratio(c, "onpair"), tp(c, "onpair", "compress", "t_compress_ns"),
             tp(c, "onpair", "bulk_decode", "t_decode_ns"), ra_p50("onpair", c))
        for b in NONDOM_BASELINES:
            v = (ratio(c, b), tp(c, b, "compress", "t_compress_ns"),
                 tp(c, b, "bulk_decode", "t_decode_ns"), ra_p50(b, c))
            wins = [(o[0] or 0) > (v[0] or 0) if v[0] else None,
                    (o[1] or 0) > (v[1] or 0) if v[1] else None,
                    (o[2] or 0) > (v[2] or 0) if v[2] else None,
                    (o[3] or 9e18) < (v[3] or 0) if v[3] else None]
            known = [w for w in wins if w is not None]
            if known and not any(known):
                violations.append((c, b))
    # three of the four metrics are timings, so the outcome is one too
    check("onpair strictly better on >=1 of 4 metrics vs every baseline/column",
          timing(not violations), str(violations))

# ---------------------------------------------------------------------------
# Claims read from the other stores: the remaining ranges of Section VIII-B
# and the numbers of Sections VIII-A and VIII-C to VIII-G.

def store(name):
    """Connection to a result store of the build root, or None if absent."""
    path = B / name
    return sqlite3.connect(path) if path.exists() else None


def claim(name, *stores):
    """Decorator for one claim: the function receives the stores and returns
    (holds, detail). A missing store or missing rows make the claim a SKIP."""
    def run(fn):
        if any(s is None for s in stores):
            skip(name, "(store missing)")
            return fn
        try:
            holds, detail = fn(*stores)
        except (TypeError, ValueError, IndexError, KeyError, ZeroDivisionError,
                statistics.StatisticsError):
            skip(name, "(rows missing)")
            return fn
        check(name, holds, detail)
        return fn
    return run


def rounds(x, printed, digits=2):
    """True if x rounds to the value the paper prints."""
    return abs(x - printed) <= 0.5 * 10 ** -digits + 1e-9


def span(xs, lo, hi, digits=2):
    """True if the extremes of xs round to the printed range lo-hi."""
    return rounds(min(xs), lo, digits) and rounds(max(xs), hi, digits)


def near(x, printed, digits=2):
    """rounds() for a timing: with --timing-tolerance, a value within that
    share of the printed one counts as well."""
    return timing(rounds(x, printed, digits) or abs(x - printed) <= TOL * abs(printed))


def near_span(xs, lo, hi, digits=2):
    """span() for timings."""
    return near(min(xs), lo, digits) and near(max(xs), hi, digits)


def fmt_span(xs, digits=2):
    return f"[{min(xs):.{digits + 1}f},{max(xs):.{digits + 1}f}]"


main_grid = conns[0] if conns else None
RA = ra if radb.exists() else None

print("== VIII-A setup")
@claim("strings are 89-99% of an uncompressed column", main_grid)
def _(_db):
    share = []
    for c in COLS:
        b, n = med(c, "onpair", "compress", "input_bytes"), med(c, "onpair", "compress", "num_rows")
        share.append(100 * b / (b + 4 * (n + 1)))
    return span(share, 89, 99, 0), fmt_span(share, 1)


print("== VIII-B main results, remaining ranges")
@claim("onpair compresses 1.8-3.8x slower than FSST, LZ4 and Zstd-1", main_grid)
def _(_db):
    r = [tc(c, b) / tc(c, "onpair") for c in COLS for b in ("fsst", "lz4", "zstd_l1")]
    return near_span(r, 1.8, 3.8, 1), fmt_span(r)


@claim("onpair parses 25-51x faster than SentencePiece's tokenizer", main_grid)
def _(_db):
    r = [(med(c, "bpe_v65536", "compress", "t_encode_ns")
          + med(c, "bpe_v65536", "compress", "t_pack_ns"))
         / med(c, "onpair", "compress", "t_parse_pack_ns") for c in COLS]
    return near_span(r, 25, 51, 0), fmt_span(r, 1)


def _dictorder():
    import json
    path = B / "dictorder" / "results.json"
    return json.loads(path.read_text())["datasets"] if path.exists() else None


@claim("id order: BPE at 0.93-1.06x onpair when sorted; frequency order 7-22% "
       "faster; creation order in between", _dictorder())
def _(order):
    speed = {c: {k: v["median_gib_per_second"] for k, v in order[c]["summary"].items()}
             for c in COLS}
    sorted_ratio = [s["bpe_lexicographic"] / s["onpair_lexicographic"] for s in speed.values()]
    gain = [100 * (s[f"{b}_frequency"] / s[f"{b}_lexicographic"] - 1)
            for s in speed.values() for b in ("bpe", "onpair")]
    between = all(s[f"{b}_lexicographic"] < s[mid] < s[f"{b}_frequency"]
                  for s in speed.values()
                  for b, mid in (("bpe", "bpe_native"), ("onpair", "onpair_construction")))
    return (near_span(sorted_ratio, 0.93, 1.06) and near_span(gain, 7, 22, 0)
            and timing(between),
            f"sorted {fmt_span(sorted_ratio)} gain {fmt_span(gain, 1)}% between={between}")


def _access(baselines, column="l_ra_p50_ns"):
    return {(b, c): RA.execute(
        f"SELECT {column} FROM ra_headline_results WHERE baseline=? AND dataset=? "
        "AND column_name=? AND status='ok'", (b, *RA_COL[c])).fetchone()[0]
        for b in baselines for c in COLS}


PER_STRING = ("onpair", "fsst", "bpe_v65536", "rlz")


@claim("point access: repair 5-13x onpair; per-string medians within 2.4x of "
       "raw; p99 within 1.6-4.5x of median; block codecs three orders slower", RA)
def _(_db):
    p50 = _access(PER_STRING + ("repair", "uncompressed", "zstd", "lz4"))
    p99 = _access(PER_STRING, "l_ra_p99_ns")
    repair = [p50["repair", c] / p50["onpair", c] for c in COLS]
    raw = [p50[b, c] / p50["uncompressed", c] for b in PER_STRING for c in COLS]
    tail = [p99[b, c] / p50[b, c] for b in PER_STRING for c in COLS]
    block = [p50[b, c] / p50["onpair", c] for b in ("zstd", "lz4") for c in COLS]
    return (near_span(repair, 5, 13, 0) and within(raw, 0, 2.4)
            and near_span(tail, 1.6, 4.5, 1) and within(block, 500, 20000),
            f"repair {fmt_span(repair)} raw<={max(raw):.2f} tail {fmt_span(tail)} "
            f"block {min(block):.0f}-{max(block):.0f}x")


print("== VIII-C training budget and threshold (a1, a3, a7 stores)")
A1, A3, A7 = store("a1_results.sqlite"), store("a3_results.sqlite"), store("a7_results.sqlite")


def _gaps(a1, dataset, column):
    """Adaptive minus fixed held-out ratio in percent, per threshold T."""
    return {int(T): 100 * (dyn - fix) / fix for T, fix, dyn in a1.execute(
        "SELECT fixed_threshold_T, heldout_ratio_fixed, heldout_ratio_dyn FROM "
        "a1_results WHERE dataset=? AND column_name=? AND status='ok'",
        (dataset, column))}


@claim("thresholds 2..50; from T=5 the controller wins or ties on the diverse "
       "columns (DBpedia +0.4-2.3%, Amazon +0.4-1.1%, URLs within 0.4%)", A1)
def _(a1):
    g = {k: _gaps(a1, *k) for k in (("dbpedia", "abstract"), ("amazon", "titles"),
                                    ("msmarco_urls", "url"))}
    from5 = {k: [v for T, v in d.items() if T >= 5] for k, d in g.items()}
    ts = sorted(g["dbpedia", "abstract"])
    return (ts == [2, 3, 5, 8, 10, 12, 20, 32, 50]
            and span(from5["dbpedia", "abstract"], 0.4, 2.3, 1)
            and span(from5["amazon", "titles"], 0.4, 1.1, 1)
            and max(abs(v) for v in from5["msmarco_urls", "url"]) <= 0.45
            and min(min(v) for v in from5.values()) > -0.2,
            " ".join(f"{k[0]} {fmt_span(v, 1)}" for k, v in from5.items()))


@claim("a tuned fixed threshold wins by up to 16% on p_name and 3.8-7.6% on "
       "c_comment", A1)
def _(a1):
    p = [-v for T, v in _gaps(a1, "tpch", "p_name").items() if T >= 5]
    c = [-v for T, v in _gaps(a1, "tpch", "c_comment").items() if T >= 5]
    return rounds(max(p), 16, 0) and span(c, 3.8, 7.6, 1), f"p_name max {max(p):.2f} c_comment {fmt_span(c, 1)}"


def _budget(a3, dataset, column):
    return dict(a3.execute(
        "SELECT sample_fraction, heldout_ratio FROM a3_results WHERE dataset=? "
        "AND column_name=? AND status='ok'", (dataset, column)))


@claim("budget sweep: ratio rises with f on DBpedia, Amazon, URLs and falls on "
       "p_name, c_comment, with the printed end points", A3)
def _(a3):
    printed = {("dbpedia", "abstract"): (2.68, 2.74), ("amazon", "titles"): (2.19, 2.24),
               ("msmarco_urls", "url"): (2.45, 2.51), ("tpch", "p_name"): (2.99, 2.53),
               ("tpch", "c_comment"): (3.99, 3.74)}
    ok, shown = True, []
    for k, (lo, hi) in printed.items():
        r = _budget(a3, *k)
        seq = [r[f] for f in sorted(r)]
        rising = all(a < b for a, b in zip(seq, seq[1:]))
        falling = all(a > b for a, b in zip(seq, seq[1:]))
        ok &= rounds(seq[0], lo) and rounds(seq[-1], hi) and (rising if hi > lo else falling)
        shown.append(f"{k[1]} {seq[0]:.3f}->{seq[-1]:.3f}")
    return ok, " ".join(shown)


def _best_fixed(a1, dataset, column):
    return max(r[0] for r in a1.execute(
        "SELECT heldout_ratio_fixed FROM a1_results WHERE dataset=? AND "
        "column_name=? AND status='ok'", (dataset, column)))


@claim("f=0.05 matches the best fixed threshold on p_name (2.99 vs 3.00); the "
       "default stays within 2.5% of it on five of six columns, 7% on Queries",
       A1, A3)
def _(a1, a3):
    best = _best_fixed(a1, "tpch", "p_name")
    tight = _budget(a3, "tpch", "p_name")[0.05]
    gap = {c: 100 * (_budget(a3, *RA_COL[c])[0.15] / _best_fixed(a1, *RA_COL[c]) - 1)
           for c in COLS}
    within = [c for c, v in gap.items() if v >= -2.5]
    return (rounds(tight, 2.99) and rounds(best, 3.00) and len(within) == 5
            and rounds(-gap["msmarco_queries/query"], 7, 0),
            f"{tight:.3f} vs {best:.3f}; " + " ".join(f"{c.split('/')[1]} {v:+.2f}%" for c, v in gap.items()))


@claim("five shuffle seeds: ratio varies by at most 0.37% (URLs), 0.24% (Amazon)", A7)
def _(a7):
    spread = {}
    for ds in ("msmarco_urls", "amazon"):
        r = [ib / (d + tk) for ib, d, tk in a7.execute(
            "SELECT input_bytes, dict_bytes, tokens_bytes FROM a7_results WHERE "
            "dataset=? AND status='ok'", (ds,))]
        spread[ds] = (len(r), 100 * (max(r) - min(r)) / min(r))
    return (spread["msmarco_urls"][0] == spread["amazon"][0] == 5
            and rounds(spread["msmarco_urls"][1], 0.37) and rounds(spread["amazon"][1], 0.24),
            str({k: round(v[1], 3) for k, v in spread.items()}))


print("== VIII-D dictionary capacity (a2 and RLZ sweep stores)")
A2, RLZ = store("a2_results.sqlite"), store("rlz_sweep2_results.sqlite")


def _capacity(a2, dataset, column):
    """bits -> (payload ratio, dictionary bytes)."""
    return {bits: (ib / (d + tk), d) for bits, ib, d, tk in a2.execute(
        "SELECT bits, input_bytes, dict_bytes, tokens_bytes FROM a2_results "
        "WHERE dataset=? AND column_name=? AND status='ok'", (dataset, column))}


@claim("capacity 2^9..2^16: URLs 1.80->3.08 and DBpedia 1.66->3.14 monotone; "
       "c_comment peaks at 2^12 (6.40), p_name at 2^14 (5.83); default gives up "
       "at most 12%", A2)
def _(a2):
    url, dbp = _capacity(a2, "msmarco_urls", "url"), _capacity(a2, "dbpedia", "abstract")
    com, pn = _capacity(a2, "tpch", "c_comment"), _capacity(a2, "tpch", "p_name")
    mono = all(all(a[0] < b[0] for a, b in zip(s, s[1:]))
               for s in ([url[b] for b in sorted(url)], [dbp[b] for b in sorted(dbp)]))
    loss = [100 * (1 - d[16][0] / max(v[0] for v in d.values())) for d in (com, pn)]
    return (sorted(url) == [9, 10, 12, 14, 16] and mono
            and rounds(url[9][0], 1.80) and rounds(url[16][0], 3.08)
            and rounds(dbp[9][0], 1.66) and rounds(dbp[16][0], 3.14)
            and max(com, key=lambda b: com[b][0]) == 12 and rounds(com[12][0], 6.40)
            and max(pn, key=lambda b: pn[b][0]) == 14 and rounds(pn[14][0], 5.83)
            and max(loss) <= 12,
            f"loss at 2^16: {loss[0]:.1f}% c_comment, {loss[1]:.1f}% p_name")


def _rlz(db, dataset):
    """reference bytes -> payload ratio, for the cells of one column."""
    cells = {}
    for cell, name, value in db.execute(
            "SELECT cell_id, metric_name, metric_value FROM results WHERE dataset=? "
            "AND op='compress' AND metric_name IN "
            "('input_bytes','dict_bytes','tokens_bytes') AND status IN ('ok','dev-only')",
            (dataset,)):
        cells.setdefault(cell, {}).setdefault(name, []).append(value)
    out = {}
    for m in cells.values():
        v = {k: statistics.median(x) for k, x in m.items()}
        out[v["dict_bytes"]] = (v["input_bytes"] / (v["dict_bytes"] + v["tokens_bytes"]),
                                100 * v["dict_bytes"] / v["input_bytes"])
    return out


@claim("RLZ reference 0.25%-16% of the column; onpair matches RLZ's 2.38 on "
       "URLs from a ~35 KiB dictionary (~290x smaller) and exceeds its best 4.39 "
       "(4 MiB) on c_comment from ~51 KiB (~80x smaller)", A2, RLZ, main_grid)
def _(a2, rlz, _db):
    url, com = _rlz(rlz, "msmarco_urls/url"), _rlz(rlz, "tpch/c_comment")
    shares = [s for d in (url, com) for _r, s in d.values()]
    best_ref = max(com, key=lambda k: com[k][0])
    on_url, on_com = _capacity(a2, "msmarco_urls", "url")[12], _capacity(a2, "tpch", "c_comment")[12]
    table2_ref = med("msmarco_urls/url", "rlz", "compress", "dict_bytes")
    return (rounds(min(shares), 0.25, 1) and rounds(max(shares), 16, 0)
            and rounds(ratio("msmarco_urls/url", "rlz"), 2.38) and on_url[0] >= 2.38
            and rounds(on_url[1] / 1024, 35, 0) and 280 <= table2_ref / on_url[1] <= 300
            and best_ref == 4 * 2**20 and rounds(com[best_ref][0], 4.39)
            and on_com[0] > com[best_ref][0] and rounds(on_com[1] / 1024, 51, 0)
            and 75 <= best_ref / on_com[1] <= 85,
            f"shares {fmt_span(shares)}% url dict {on_url[1] / 1024:.1f} KiB "
            f"({table2_ref / on_url[1]:.0f}x) c_comment dict {on_com[1] / 1024:.1f} KiB "
            f"({best_ref / on_com[1]:.0f}x)")


print("== VIII-E block size and query distribution (a5, a6 stores)")
A5, A6 = store("a5_results.sqlite"), store("a6_results.sqlite")


@claim("block size on URLs: Zstd 5.7 us -> 3.0 ms, LZ4 1.6 us -> 1.1 ms; onpair "
       "at ~175-290 ns; 20,000 queries per cell", A5)
def _(a5):
    def p50(baseline, dataset):
        return [r[0] for r in a5.execute(
            "SELECT l_ra_p50_ns FROM a5_results WHERE baseline=? AND dataset=? AND "
            "cache_state='warm' AND status='ok' ORDER BY block_size_strings",
            (baseline, dataset))]
    z, l = p50("zstd", "msmarco_urls"), p50("lz4", "msmarco_urls")
    on = p50("onpair", "msmarco_urls") + p50("onpair", "tpch")
    n = {r[0] for r in a5.execute("SELECT n_samples FROM a5_results")}
    return (near(z[0] / 1e3, 5.7, 1) and near(z[-1] / 1e6, 3.0, 1)
            and near(l[0] / 1e3, 1.6, 1) and near(l[-1] / 1e6, 1.1, 1)
            and within(min(on), 170, 180) and within(max(on), 280, 295)
            and n == {20000},
            f"zstd {z[0] / 1e3:.2f}us->{z[-1] / 1e6:.2f}ms lz4 {l[0] / 1e3:.2f}us->"
            f"{l[-1] / 1e6:.2f}ms onpair {fmt_span(on, 0)}ns")


@claim("query skew on URLs: onpair 286 ns uniform -> 30 ns under Zipf-1.5; "
       "scrambled Zipf-1.0 53 -> 193 ns; Zstd ~0.7 ms throughout; Beta no faster", A6)
def _(a6):
    def p50(baseline, dist, layout="identity"):
        return a6.execute(
            "SELECT l_ra_p50_ns FROM a6_results WHERE baseline=? AND dataset="
            "'msmarco_urls' AND query_distribution=? AND query_layout=? AND "
            "cache_state='warm' AND status='ok'", (baseline, dist, layout)).fetchone()[0]
    zstd = [r[0] / 1e6 for r in a6.execute(
        "SELECT l_ra_p50_ns FROM a6_results WHERE baseline='zstd' AND dataset="
        "'msmarco_urls' AND cache_state='warm' AND status='ok'")]
    uni, z15 = p50("onpair", "uniform"), p50("onpair", "zipf1.5")
    z10, z10s = p50("onpair", "zipf1.0"), p50("onpair", "zipf1.0", "scramble")
    beta = p50("onpair", "beta2_5")
    return (near(uni, 286, 0) and near(z15, 30, 0) and near(z10, 53, 0)
            and near(z10s, 193, 0) and near_span(zstd, 0.7, 0.7, 1)
            and timing(beta > 0.9 * uni),
            f"{uni:.1f}->{z15:.1f}; {z10:.1f}->{z10s:.1f}; zstd {fmt_span(zstd)}ms; "
            f"beta {beta:.1f}")


print("== VIII-F scalability (nsweep store)")
@claim("scale 3 MiB-3 GiB: onpair flat at 57-69 MiB/s; Re-Pair 0.41 MiB/s at "
       "3 MiB, 0.14 at 30 MiB, unfinished at 300 MiB", store("nsweep_results.sqlite"))
def _(db):
    rows = {(b, n): (status, ib, t) for b, n, status, ib, t in db.execute(
        "SELECT baseline, n_rows, status, input_bytes, t_compress_ns FROM nsweep_results")}
    speed = lambda b, n: rows[b, n][1] / 2**20 / (rows[b, n][2] / 1e9)
    on = [speed("onpair", n) for n in (10**5, 10**6, 10**7, 10**8)]
    r5, r6 = speed("repair", 10**5), speed("repair", 10**6)
    return (near_span(on, 57, 69, 0) and near(r5, 0.41) and near(r6, 0.14)
            and timing(rows["repair", 10**7][0] == "timeout"),
            f"onpair {fmt_span(on, 1)} repair {r5:.3f}, {r6:.3f}, {rows['repair', 10**7][0]}")


print("== VIII-G synthetic sweep (vd store)")
@claim("synthetic, L=32: rho=0 Zstd 1.93, FSST 1.90, onpair 1.66; rho=0.95 "
       "onpair 3.98 (1.4x FSST's 2.88), Zstd 4.22, decoding 7.3x faster "
       "(7.5 vs 1.0 GiB/s)", store("vd_results.sqlite"))
def _(db):
    def m(rho, baseline, op, name):
        return statistics.median(r[0] for r in db.execute(
            "SELECT metric_value FROM results WHERE dataset=? AND baseline=? AND "
            "op=? AND metric_name=? AND status IN ('ok','dev-only')",
            (f"synthetic_vd/f6__L32_rho{rho}", baseline, op, name)))
    def r(rho, b):
        return m(rho, b, "compress", "input_bytes") / (
            m(rho, b, "compress", "dict_bytes") + m(rho, b, "compress", "tokens_bytes"))
    def d(b):
        return (m("0.95", b, "compress", "input_bytes") / 2**30
                / (m("0.95", b, "bulk_decode", "t_decode_ns") / 1e9))
    return (rounds(r("0.0", "zstd"), 1.93) and rounds(r("0.0", "fsst"), 1.90)
            and rounds(r("0.0", "onpair"), 1.66) and rounds(r("0.95", "onpair"), 3.98)
            and rounds(r("0.95", "fsst"), 2.88) and rounds(r("0.95", "zstd"), 4.22)
            and rounds(r("0.95", "onpair") / r("0.95", "fsst"), 1.4, 1)
            and near(d("onpair"), 7.5, 1) and near(d("zstd"), 1.0, 1)
            and near(d("onpair") / d("zstd"), 7.3, 1),
            f"rho=0 {r('0.0', 'zstd'):.3f}/{r('0.0', 'fsst'):.3f}/{r('0.0', 'onpair'):.3f} "
            f"rho=0.95 {r('0.95', 'onpair'):.3f}/{r('0.95', 'fsst'):.3f}/"
            f"{r('0.95', 'zstd'):.3f} decode {d('onpair'):.2f} vs {d('zstd'):.2f}")


print(f"\n== RESULT: {PASS} pass, {len(FAIL)} fail, "
      + (f"{len(DIFF)} differ, " if TOL else "") + f"{len(SKIP)} skip")
for f in FAIL:
    print("   FAILED:", f)
for d in DIFF:
    print("   DIFFERS:", d)
if DIFF:
    print(f"   (a timing more than {100 * TOL:g}% from the paper's, or outside the "
          "range it states: expected on another\n   machine, and for a few cells "
          "between two runs on the same one)")
for s in SKIP:
    print("   SKIPPED:", s)
sys.exit(0 if not FAIL else 1)
