"""Summarise the experiment drivers' outputs for the paper.

Reads whichever of these exist under --build-root and skips the rest:

  decode/results.json            paired BPE vs OnPair bulk decode,
                                          construction phases, serialized sizes
  dictorder/results.json  decode speed by dictionary ID order
  bpedecoder/results.json
                                          flat BPE decoder vs SentencePiece's
                                          own detokenizer
  prefix/results.json            compressed prefix filter vs raw scan
  finalization/results.csv                OnPair training finalization share

Outputs (default --out build/paper):
  tables/T6_dictorder.tex     decode throughput by vocabulary and ID order
  experiments_summary.json    every number printed below, keyed by claim id

The ranges, medians and geometric means printed to stdout are the ones the
paper quotes in prose.

Usage:
  PYTHONPATH=. .venv/bin/python -m tools.experiments_summary \
      [--build-root build] [--out build/paper]
"""
from __future__ import annotations
import argparse
import csv
import json
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

COLUMNS = [
    ("dbpedia/abstract", "DBpedia"),
    ("msmarco_urls/url", "URLs"),
    ("msmarco_queries/query", "Queries"),
    ("amazon/titles", "Amazon"),
    ("tpch/p_name", "p\\_name"),
    ("tpch/c_comment", "c\\_comm."),
]


def geomean(values):
    return math.exp(sum(math.log(v) for v in values) / len(values))


def span(values, digits=2):
    return f"{min(values):.{digits}f}-{max(values):.{digits}f}"


def load_json(path):
    if not path.exists():
        print(f"-- {path.name} not found under {path.parent}; skipped")
        return None
    return json.loads(path.read_text())


def decode(build_root, ledger):
    data = load_json(build_root / "decode" / "results.json")
    if data is None:
        return None
    print("\n== Paired bulk decode, construction and sizes (decode stage)")
    ratios, train_x, total_x = [], [], []
    fastest_decode_ns = {}
    for ds, _ in COLUMNS:
        entry = data["datasets"].get(ds)
        if entry is None:
            continue
        s, pm, sp = entry["summary"], entry["phase_metrics"], entry["space"]
        b, o = s["bpe_v65536"], s["onpair"]
        ratio = b["median_gib_per_second"] / o["median_gib_per_second"]
        ratios.append(ratio)
        fastest_decode_ns[ds] = min(b["median_decode_ns"], o["median_decode_ns"])
        bp, op = pm["bpe_v65536"], pm["onpair"]
        train_x.append(bp["t_train_ns"] / op["t_train_ns"])
        total_x.append(bp["t_codec_first_use_ns"] / op["t_codec_first_use_ns"])
        for name, value in (
                ("decode_gibs/bpe", b["median_gib_per_second"]),
                ("decode_gibs/onpair", o["median_gib_per_second"]),
                ("decode_ratio_bpe_over_onpair", ratio),
                ("train_s/bpe", bp["t_train_ns"] / 1e9),
                ("encode_s/bpe", (bp["t_encode_ns"] + bp["t_pack_ns"]) / 1e9),
                ("construct_s/bpe", bp["t_codec_first_use_ns"] / 1e9),
                ("train_s/onpair", op["t_train_ns"] / 1e9),
                ("parse_s/onpair", op["t_parse_pack_ns"] / 1e9),
                ("construct_s/onpair", op["t_codec_first_use_ns"] / 1e9),
                ("complete_ratio/bpe", sp["bpe_v65536"]["full_accounted_ratio"]),
                ("complete_ratio/onpair", sp["onpair"]["full_accounted_ratio"]),
                ("wire_ratio/bpe", sp["bpe_v65536"]["wire_ratio"]),
                ("wire_ratio/onpair", sp["onpair"]["wire_ratio"])):
            ledger[f"paired/{name}/{ds}"] = value
        print(f"  {ds:24} decode BPE {b['median_gib_per_second']:.2f} / OnPair "
              f"{o['median_gib_per_second']:.2f} GiB/s (ratio {ratio:.2f}); "
              f"construct BPE {bp['t_codec_first_use_ns'] / 1e9:.2f} s / OnPair "
              f"{op['t_codec_first_use_ns'] / 1e9:.2f} s; complete ratio "
              f"{sp['bpe_v65536']['full_accounted_ratio']:.3f} / "
              f"{sp['onpair']['full_accounted_ratio']:.3f}")
    if ratios:
        print(f"  BPE/OnPair decode ratio {span(ratios)}; OnPair faster to train "
              f"by {span(train_x, 0)}x and to construct by {span(total_x, 0)}x")
        ledger["paired/decode_ratio_range"] = [min(ratios), max(ratios)]
        ledger["paired/train_speedup_range"] = [min(train_x), max(train_x)]
        ledger["paired/construct_speedup_range"] = [min(total_x), max(total_x)]
    return fastest_decode_ns


def dictorder(build_root, out_dir, ledger):
    data = load_json(build_root / "dictorder" / "results.json")
    if data is None:
        return
    print("\n== Decode speed by dictionary ID order (dictorder stage)")
    layouts = ["bpe_native", "bpe_lexicographic", "bpe_frequency",
               "onpair_lexicographic", "onpair_frequency"]
    rows, lex, freq, gain = [], [], [], defaultdict(list)
    for ds, label in COLUMNS:
        entry = data["datasets"].get(ds)
        if entry is None:
            continue
        v = {k: entry["summary"][k]["median_gib_per_second"] for k in layouts}
        for k in layouts:
            ledger[f"dictorder/{k}/{ds}"] = v[k]
        rows.append(f"    {label} & " + " & ".join(f"{v[k]:.2f}" for k in layouts)
                    + " \\\\")
        lex.append(v["bpe_lexicographic"] / v["onpair_lexicographic"])
        freq.append(v["bpe_frequency"] / v["onpair_frequency"])
        gain["bpe"].append(v["bpe_frequency"] / v["bpe_lexicographic"] - 1)
        gain["onpair"].append(v["onpair_frequency"] / v["onpair_lexicographic"] - 1)
    if not rows:
        return
    print(f"  BPE/OnPair at lexicographic IDs {span(lex)} (geomean "
          f"{geomean(lex):.2f}); at frequency IDs {span(freq)} (geomean "
          f"{geomean(freq):.2f})")
    for codec in ("bpe", "onpair"):
        g = [100 * x for x in gain[codec]]
        print(f"  frequency order faster than lexicographic for {codec}: "
              f"{min(g):.1f}-{max(g):.1f}%")
        ledger[f"dictorder/frequency_gain_pct/{codec}"] = [min(g), max(g)]
    ledger["dictorder/matched_lex_ratio_range"] = [min(lex), max(lex)]
    ledger["dictorder/matched_freq_ratio_range"] = [min(freq), max(freq)]
    lines = [
        "% AUTO-GENERATED by tools/experiments_summary.py — do not edit by hand.",
        "\\begin{table}[t]",
        "  \\centering",
        "  \\caption{Bulk decompression throughput (GiB/s) by vocabulary and"
        " dictionary ID order}",
        "  \\label{tab:dictorder}",
        "  \\small\\setlength{\\tabcolsep}{3.4pt}",
        "  \\begin{tabular}{l@{\\hskip 9pt}rrr@{\\hskip 9pt}rr}",
        "    \\toprule",
        "    & \\multicolumn{3}{c}{\\compressor{BPE}}"
        " & \\multicolumn{2}{c}{\\onpair} \\\\",
        "    \\cmidrule(lr){2-4}\\cmidrule(lr){5-6}",
        "    & native & lex. & freq. & lex. & freq. \\\\",
        "    \\midrule",
        *rows,
        "    \\bottomrule", "  \\end{tabular}", "\\end{table}", "",
    ]
    tables = out_dir / "tables"
    tables.mkdir(parents=True, exist_ok=True)
    (tables / "T6_dictorder.tex").write_text("\n".join(lines))
    print(f"  wrote {tables / 'T6_dictorder.tex'}")


def bpe_decoder(build_root, ledger):
    """The headline BPE decode number uses a flat pre-expanded table of the
    same design as OnPair's. SentencePiece's own Decode is a text detokenizer;
    `complete` adds the ID unpacking and the byte<->unicode inversion that the
    harness would need around it to return the original bytes."""
    data = load_json(build_root / "bpedecoder" / "results.json")
    if data is None:
        return
    print("\n== Flat BPE decoder vs SentencePiece's own detokenizer (bpedecoder stage)")
    api, complete = [], []
    for ds, _ in COLUMNS:
        entry = data["datasets"].get(ds)
        if entry is None:
            continue
        s = entry["summary"]
        flat = s["bpe_flat"]["median_gib_per_second"]
        native = max(s[k]["median_gib_per_second"]
                     for k in ("sentencepiece_rows", "sentencepiece_column"))
        full = max(s[k]["median_complete_adapted_gib_per_second"]
                   for k in ("sentencepiece_rows", "sentencepiece_column"))
        api.append(flat / native)
        complete.append(flat / full)
        for name, value in (("flat_gibs", flat), ("sentencepiece_api_gibs", native),
                            ("sentencepiece_complete_gibs", full)):
            ledger[f"bpedecoder/{name}/{ds}"] = value
        print(f"  {ds:24} flat {flat:.2f} GiB/s; SentencePiece Decode {native:.3f} "
              f"GiB/s (API only), {full:.3f} GiB/s (complete path)")
    if api:
        print(f"  flat decoder faster by {span(api, 0)}x (API only), "
              f"{span(complete, 0)}x (complete path)")
        ledger["bpedecoder/flat_over_api_range"] = [min(api), max(api)]
        ledger["bpedecoder/flat_over_complete_range"] = [min(complete), max(complete)]


def prefix(build_root, fastest_decode_ns, ledger):
    data = load_json(build_root / "prefix" / "results.json")
    if data is None:
        return
    print("\n== Compressed prefix filter vs raw scan (prefix stage)")
    by_method = defaultdict(dict)
    for row in data["summaries"]:
        by_method[row["method"]][(row["dataset"], row["query_id"])] = row
    native, raw = by_method["onpair_native"], by_method["uncompressed_scan"]
    speedups = {k: r["speedup_vs_uncompressed"] for k, r in native.items()}
    values = list(speedups.values())
    wins = sum(1 for v in values if v > 1)
    print(f"  {len(values)} queries; OnPair beats the raw scan on {wins}; speedup "
          f"{span(values)}x (median {statistics.median(values):.2f}, geomean "
          f"{geomean(values):.2f})")
    ledger["prefix/queries"] = len(values)
    ledger["prefix/wins_vs_raw"] = wins
    ledger["prefix/speedup_vs_raw"] = {
        "min": min(values), "max": max(values),
        "median": statistics.median(values), "geomean": geomean(values)}
    by_length = defaultdict(list)
    for key, row in native.items():
        by_length[row["needle_bytes"]].append(speedups[key])
    for length in sorted(by_length):
        v = by_length[length]
        print(f"    {length:2d}-byte prefixes: {len(v)} queries, {span(v)}x "
              f"(median {statistics.median(v):.2f}, geomean {geomean(v):.2f})")
        ledger[f"prefix/speedup_vs_raw/length_{length}"] = {
            "n": len(v), "min": min(v), "max": max(v),
            "median": statistics.median(v), "geomean": geomean(v)}
    for key, v in sorted(speedups.items()):
        if v <= 1:
            row = native[key]
            print(f"    loss: {key[0]} {key[1]} ({row['needle_bytes']} bytes, "
                  f"selectivity {row['selectivity']:.3g}): {v:.2f}x")
    # Decode-then-scan with the baselines the driver measured itself.
    lower = {}
    for method, rows in by_method.items():
        if method.endswith("_decode_plus_raw_scan_lower_bound"):
            for key, row in rows.items():
                lower[key] = min(lower.get(key, math.inf), row["median_ns"])
    if lower:
        v = [lower[k] / native[k]["median_ns"] for k in native if k in lower]
        print(f"  vs decode-then-scan with the fastest of FSST/LZ4/Zstd: "
              f"{span(v)}x (geomean {geomean(v):.2f})")
        ledger["prefix/speedup_vs_decode_then_scan_block_or_fsst"] = {
            "min": min(v), "max": max(v), "geomean": geomean(v)}
    # Cross-run estimate with the fastest flat decoder (BPE or OnPair payload
    # decode from the decode stage) followed by the raw scan.
    if fastest_decode_ns:
        v = [(fastest_decode_ns[k[0]] + raw[k]["median_ns"]) / native[k]["median_ns"]
             for k in native if k[0] in fastest_decode_ns and k in raw]
        if v:
            print(f"  vs decode-then-scan with the fastest flat decoder "
                  f"(cross-run estimate): {span(v)}x (geomean {geomean(v):.2f})")
            ledger["prefix/speedup_vs_decode_then_scan_flat_estimate"] = {
                "min": min(v), "max": max(v), "geomean": geomean(v)}


def finalization(build_root, ledger):
    path = build_root / "finalization" / "results.csv"
    if not path.exists():
        print(f"-- {path.name} not found under {path.parent}; skipped")
        return
    print("\n== OnPair training finalization (finalize stage)")
    per_dataset = defaultdict(lambda: defaultdict(list))
    with path.open() as fh:
        for row in csv.DictReader(fh):
            for key in ("finalization_ns", "finalization_pct_train",
                        "finalization_pct_total"):
                per_dataset[row["dataset"]][key].append(float(row[key]))
    ms, pct_train, pct_total = [], [], []
    for ds, cols in per_dataset.items():
        m = {k: statistics.median(v) for k, v in cols.items()}
        ms.append(m["finalization_ns"] / 1e6)
        pct_train.append(m["finalization_pct_train"])
        pct_total.append(m["finalization_pct_total"])
        ledger[f"finalization/ms/{ds}"] = ms[-1]
        ledger[f"finalization/pct_train/{ds}"] = pct_train[-1]
        ledger[f"finalization/pct_total/{ds}"] = pct_total[-1]
    print(f"  {span(ms)} ms; {span(pct_train)}% of training; {span(pct_total)}% "
          f"of training plus parsing (below 2% on "
          f"{sum(1 for p in pct_total if p < 2)} of {len(pct_total)} columns)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build-root", type=Path, default=REPO_ROOT / "build")
    ap.add_argument("--out", type=Path, default=REPO_ROOT / "build" / "paper")
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    ledger: dict = {}
    fastest_decode_ns = decode(a.build_root, ledger)
    dictorder(a.build_root, a.out, ledger)
    bpe_decoder(a.build_root, ledger)
    prefix(a.build_root, fastest_decode_ns, ledger)
    finalization(a.build_root, ledger)
    (a.out / "experiments_summary.json").write_text(
        json.dumps(ledger, indent=1, default=float))
    print(f"\nexperiments_summary: {len(ledger)} numbers written to "
          f"{a.out / 'experiments_summary.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
