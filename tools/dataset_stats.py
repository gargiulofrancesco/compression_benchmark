"""Compute the paper's dataset-statistics table (T-datasets) from the
materialized headline columns: N, distinct %, L_avg, |Sigma|, LCP_avg
(average longest common prefix between lexicographically adjacent rows).

Usage: .venv/bin/python tools/dataset_stats.py [--columns-dir datasets/materialized]
Writes a markdown + LaTeX-ready CSV to build/dataset_stats.{md,csv}.
"""
from __future__ import annotations
import argparse
import csv
import os
import struct
import sys

HEADLINE = [
    ("dbpedia", "abstract.col", "DBpedia"),
    ("msmarco_urls", "url.col", "URLs"),
    ("msmarco_queries", "query.col", "Query Logs"),
    ("amazon", "titles.col", "Amazon Titles"),
    ("tpch", "p_name.col", "TPC-H p\\_name"),
    ("tpch", "c_comment.col", "TPC-H c\\_comment"),
]


def stats(path: str):
    with open(path, "rb") as f:
        n, total = struct.unpack("<II", f.read(8))
        offsets = struct.unpack(f"<{n + 1}I", f.read((n + 1) * 4))
        data = f.read(total)
    rows = [data[offsets[i]:offsets[i + 1]] for i in range(n)]
    alphabet = len(set(data))
    l_avg = total / n
    distinct = len(set(rows))
    srt = sorted(rows)
    lcp_sum = 0
    for a, b in zip(srt, srt[1:]):
        m = min(len(a), len(b))
        k = 0
        while k < m and a[k] == b[k]:
            k += 1
        lcp_sum += k
    lcp_avg = lcp_sum / (n - 1) if n > 1 else 0.0
    return dict(n=n, total=total, unique_pct=100.0 * distinct / n,
                l_avg=l_avg, alphabet=alphabet, lcp_avg=lcp_avg)


def fmt_n(n: float) -> str:
    return f"{n/1e6:.2f}M"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--columns-dir", default="datasets/materialized")
    a = ap.parse_args()
    out_rows = []
    for ds, fn, label in HEADLINE:
        p = os.path.join(a.columns_dir, ds, fn)
        s = stats(p)
        out_rows.append([label, fmt_n(s["n"]), f"{s['total']/2**20:.0f}",
                         f"{s['unique_pct']:.1f}", f"{s['l_avg']:.0f}",
                         str(s["alphabet"]), f"{s['lcp_avg']:.0f}"])
        print(label, out_rows[-1], flush=True)
    os.makedirs("build", exist_ok=True)
    hdr = ["Dataset", "N", "MiB", "Unique%", "Lavg", "Sigma", "LCPavg"]
    with open("build/dataset_stats.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(hdr)
        w.writerows(out_rows)
    with open("build/dataset_stats.md", "w") as f:
        f.write("| " + " | ".join(hdr) + " |\n")
        f.write("|" + "---|" * len(hdr) + "\n")
        for r in out_rows:
            f.write("| " + " | ".join(r) + " |\n")
    print("wrote build/dataset_stats.{csv,md}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
