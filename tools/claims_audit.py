"""ICDE — final claims audit: every quantitative claim in the paper
prose, re-derived from the result stores and checked. Run AFTER the final
regeneration; ship only on all-PASS (or consciously waived items).

Usage: PYTHONPATH=. .venv/bin/python -m tools.claims_audit
"""
from __future__ import annotations
import sqlite3
import statistics
import sys
from pathlib import Path

B = Path("build")
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


PASS, FAIL = 0, []


def check(name, cond, detail=""):
    global PASS
    if cond:
        PASS += 1
        print(f"  PASS {name} {detail}")
    else:
        FAIL.append(name)
        print(f"  FAIL {name} {detail}")


print("== §8.4.1 ratio claims")
of = [ratio(c, "onpair") / ratio(c, "fsst") for c in COLS]
check("onpair/fsst in 1.5-1.8x", 1.45 <= min(of) and max(of) <= 1.85,
      f"[{min(of):.2f},{max(of):.2f}]")
wins = sum(1 for c in COLS if ratio(c, "onpair") > ratio(c, "zstd_l9"))
check("beats zstd-9 on 5/6", wins == 5, f"wins={wins}")
b64 = [(c, ratio(c, "onpair"), ratio(c, "bpe_v65536")) for c in COLS
       if ratio(c, "bpe_v65536")]
check("onpair within ~5% of bpe64 everywhere; wins p_name+c_comment",
      all(o >= b * 0.94 for _, o, b in b64)
      and all(o > b for c, o, b in b64 if c.startswith("tpch")),
      str([(c.split('/')[1], f"{o:.2f}vs{b:.2f}") for c, o, b in b64]))
check("p_name: onpair 5.19 best of roster / bpe64 4.87 / repair 5.05",
      abs(ratio("tpch/p_name", "onpair") - 5.19) < 0.05
      and abs(ratio("tpch/p_name", "bpe_v65536") - 4.87) < 0.05
      and abs(ratio("tpch/p_name", "repair") - 5.05) < 0.05
      and all(ratio("tpch/p_name", "onpair") > (ratio("tpch/p_name", b) or 0)
              for b in ("lz4", "zstd_l1", "zstd", "zstd_l9", "fsst",
                        "bpe_v65536", "rlz", "repair")))
check("c_comment: repair 7.59, onpair 5.66, bpe64 5.22",
      abs(ratio("tpch/c_comment", "repair") - 7.59) < 0.05
      and abs(ratio("tpch/c_comment", "onpair") - 5.66) < 0.05
      and abs(ratio("tpch/c_comment", "bpe_v65536") - 5.22) < 0.05)
rw = [c for c in COLS if (ratio(c, "repair") or 0) > ratio(c, "onpair")]
check("repair wins ratio on exactly 5/6 (all but p_name)",
      len(rw) == 5 and "tpch/p_name" not in rw, str(rw))
rlz = [ratio(c, "rlz") for c in COLS]
check("rlz range 2.0-4.4", 1.95 <= min(rlz) and max(rlz) <= 4.45,
      f"[{min(rlz):.2f},{max(rlz):.2f}]")

print("== §8.4.2 compression-throughput claims")
otc = [tp(c, "onpair", "compress", "t_compress_ns", 2**20) for c in COLS]
check("onpair Tc 81-153 MiB/s", 78 <= min(otc) and max(otc) <= 157,
      f"[{min(otc):.0f},{max(otc):.0f}]")
rtc = [tp(c, "repair", "compress", "t_compress_ns", 2**20) for c in COLS]
check("repair Tc 0.1-1.5 MiB/s", max(rtc) <= 1.6, f"max={max(rtc):.2f}")
ztc = [tp(c, "zstd_l9", "compress", "t_compress_ns", 2**20) for c in COLS]
check("zstd9 Tc 53-66 MiB/s", 50 <= min(ztc) and max(ztc) <= 70,
      f"[{min(ztc):.0f},{max(ztc):.0f}]")
# training speedup (size-matched bpe64 t_train vs onpair t_train)
spd = []
for c in COLS:
    o = med(c, "onpair", "compress", "t_train_ns")
    bb = med(c, "bpe_v65536", "compress", "t_train_ns")
    if o and bb:
        spd.append(bb / o)
check("train speedup vs BPE-64K: prose 380-3500x, >=100x everywhere",
      len(spd) >= 5 and min(spd) >= 350 and max(spd) <= 3600,
      f"[{min(spd):.0f}x,{max(spd):.0f}x] n={len(spd)}")
b64tc = [tp(c, "bpe_v65536", "compress", "t_compress_ns", 2**20) for c in COLS
         if tp(c, "bpe_v65536", "compress", "t_compress_ns")]
check("bpe64 end-to-end Tc 2.8-4.6 MiB/s",
      2.7 <= min(b64tc) and max(b64tc) <= 4.7,
      f"[{min(b64tc):.1f},{max(b64tc):.1f}] n={len(b64tc)}")

print("== §8.4.3 decompression claims")
otd = {c: tp(c, "onpair", "bulk_decode", "t_decode_ns") for c in COLS}
check("onpair Td 4.4-8.5 GiB/s", 4.3 <= min(otd.values()) and max(otd.values()) <= 8.6,
      f"[{min(otd.values()):.2f},{max(otd.values()):.2f}]")
best = all(otd[c] >= max(filter(None, (tp(c, b, "bulk_decode", "t_decode_ns")
            for b in ("fsst", "lz4", "zstd", "zstd_l1", "zstd_l9", "rlz",
                      "repair", "bpe_v65536")))) for c in COLS)
check("onpair best Td on EVERY column", best)
# avg token bytes 5.8-11.7
atl = []
for c in COLS:
    ib, tb = med(c, "onpair", "compress", "input_bytes"), med(c, "onpair", "compress", "tokens_bytes")
    atl.append(ib / (tb / 2))
check("avg token bytes 5.8-11.7", 5.6 <= min(atl) and max(atl) <= 11.9,
      f"[{min(atl):.1f},{max(atl):.1f}]")

print("== §8.4.4 access claims (ra_headline store)")
ra = sqlite3.connect(B / "ra_headline_results.sqlite")
def lra(b, col_filter=None, m="l_ra_p50_ns"):
    q = f"SELECT {m} FROM ra_headline_results WHERE baseline=? AND status='ok' AND {m} IS NOT NULL"
    return [r[0] for r in ra.execute(q, (b,))]
ps = lra("onpair") + lra("fsst") + lra("bpe_v65536") + lra("rlz")
check("per-string p50 60-440ns", 55 <= min(ps) and max(ps) <= 445,
      f"[{min(ps):.0f},{max(ps):.0f}]")
rp = lra("repair")
check("repair p50 0.8-5.2us", 700 <= min(rp) and max(rp) <= 5400,
      f"[{min(rp):.0f},{max(rp):.0f}]")
bl = lra("zstd") + lra("lz4")
check("block 0.22-0.76ms", 2.0e5 <= min(bl) and max(bl) <= 8.0e5,
      f"[{min(bl)/1e3:.0f}us,{max(bl)/1e3:.0f}us]")
# onpair vs raw on cache-resident columns (1.8-3.7x faster, 69-91 vs 145-252)
def p50(b, ds, col):
    r = ra.execute("SELECT l_ra_p50_ns FROM ra_headline_results WHERE baseline=? "
                   "AND dataset=? AND column_name=? AND status='ok'", (b, ds, col)).fetchone()
    return r[0] if r else None
spdups = []
for ds, col in (("msmarco_queries", "query"), ("tpch", "p_name"), ("tpch", "c_comment")):
    o, u = p50("onpair", ds, col), p50("uncompressed", ds, col)
    if o and u:
        spdups.append(u / o)
check("onpair 1.8-3.7x faster than raw on 3 cache-resident cols",
      len(spdups) == 3 and 1.75 <= min(spdups) and max(spdups) <= 3.75,
      f"[{min(spdups):.2f}x,{max(spdups):.2f}x]")

print("== §8.1 turbo bridge claims (results_turbo store)")
tdb = B / "results_turbo.sqlite"
if tdb.exists():
    tconn = sqlite3.connect(tdb)
    TCOLS = [c for c in COLS if c != "amazon/titles"]
    def tmed(ds, b, op, m):
        rs = [r[0] for r in tconn.execute(
            "SELECT metric_value FROM results WHERE dataset=? AND baseline=? "
            "AND op=? AND metric_name=? AND metric_value IS NOT NULL", (ds, b, op, m))]
        return statistics.median(rs) if rs else None
    # Per-codec MEDIAN scaling (prose: "1.27-1.39x per codec"). Cell-level
    # ratios are noisier (RLZ's reference allocation is bimodal across
    # processes; see results_turbo_rlz.sqlite for the re-run resolving it).
    percodec = []
    for b in ("lz4", "zstd", "fsst", "rlz", "onpair"):
        sca = []
        for c in TCOLS:
            for op, m in (("bulk_decode", "t_decode_ns"), ("compress", "t_compress_ns")):
                bm, tm = med(c, b, op, m), tmed(c, b, op, m)
                if bm and tm:
                    sca.append(bm / tm)
        if sca:
            percodec.append(statistics.median(sca))
    check("turbo speedup 1.2-1.4x per codec (median over cells)",
          1.15 <= min(percodec) and max(percodec) <= 1.45,
          f"[{min(percodec):.2f},{max(percodec):.2f}]")
    tbest = all((tmed(c, "onpair", "bulk_decode", "t_decode_ns") or 9e18) <=
                min(filter(None, (tmed(c, b, "bulk_decode", "t_decode_ns")
                    for b in ("fsst", "lz4", "zstd", "zstd_l1", "zstd_l9", "rlz"))))
                for c in TCOLS)
    check("onpair decode lead holds under turbo on all columns", tbest)

print("== §8.5 held-out accounting bridge (a3 store)")
# Held-out ratios charge the dictionary against 1/5 of the column AND include
# the row-boundary array; Table II excludes boundaries and amortizes over the
# full column. Re-expressed under Table II accounting, the f=0.15 held-out
# dictionary must reproduce the headline ratio on p_name (closed vocabulary).
a3db = B / "a3_results.sqlite"
if a3db.exists():
    a3 = sqlite3.connect(a3db)
    row = a3.execute(
        "SELECT train_input_bytes, heldout_input_bytes, dict_bytes, "
        "tokens_bytes FROM a3_results WHERE dataset='tpch' AND "
        "column_name='p_name' AND sample_fraction=0.15 AND status='ok'"
    ).fetchone()
    tb_a3, hb_a3, db_a3, tk_a3 = row
    btot = tb_a3 + hb_a3
    r_eq = btot / (db_a3 + tk_a3 / hb_a3 * btot)
    r_head = ratio("tpch/p_name", "onpair")
    check("held-out bridge: p_name f=0.15 under Table II accounting = headline +-3%",
          abs(r_eq - r_head) / r_head <= 0.03,
          f"bridge={r_eq:.2f} headline={r_head:.2f}")
else:
    check("held-out bridge: p_name f=0.15 under Table II accounting = headline +-3%",
          False, "a3_results.sqlite missing")

print("== §1 scope claim: 4-metric Pareto non-domination")
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
check("onpair strictly better on >=1 of 4 metrics vs every baseline/column",
      not violations, str(violations))

print(f"\n== RESULT: {PASS} pass, {len(FAIL)} fail")
for f in FAIL:
    print("   FAILED:", f)
sys.exit(0 if not FAIL else 1)
