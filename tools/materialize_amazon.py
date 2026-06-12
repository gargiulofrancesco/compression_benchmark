"""One-shot: materialize amazon/titles.col from the downloaded meta_Books.jsonl.gz
(recipe: non-empty `title` per JSONL line, ORIGINAL file order, no
shuffle), write train/heldout splits (last 20%), and print the Table-1 stats
(N, MiB, unique%, L_avg, LCP_avg on the sorted permutation).
"""
from __future__ import annotations
import gzip
import json
import struct
import sys
from pathlib import Path

RAW = Path("/home/gargiulo/icde/datasets/raw/amazon/meta_Books.jsonl.gz")
OUT_DIR = Path("datasets/materialized_full/amazon")


def write_col(path: Path, rows: list[bytes]) -> None:
    total = sum(len(r) for r in rows)
    with open(path, "wb") as f:
        f.write(struct.pack("<II", len(rows), total))
        off = 0
        f.write(struct.pack("<I", 0))
        for r in rows:
            off += len(r)
            f.write(struct.pack("<I", off))
        for r in rows:
            f.write(r)
    print(f"{path}: {len(rows)} rows, {total/2**20:.0f} MiB", flush=True)


def main() -> int:
    rows: list[bytes] = []
    with gzip.open(str(RAW), "rt", encoding="utf-8") as f:
        for line in f:
            try:
                t = json.loads(line).get("title", "").strip()
            except json.JSONDecodeError:
                continue
            if t:
                rows.append(t.encode("utf-8"))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    write_col(OUT_DIR / "titles.col", rows)
    cut = len(rows) - len(rows) // 5
    write_col(OUT_DIR / "titles.train.col", rows[:cut])
    write_col(OUT_DIR / "titles.heldout.col", rows[cut:])
    # Table-1 stats
    n = len(rows)
    total = sum(len(r) for r in rows)
    uniq = len(set(rows))
    srt = sorted(rows)
    lcp = 0
    for a, b in zip(srt, srt[1:]):
        m = min(len(a), len(b))
        k = 0
        while k < m and a[k] == b[k]:
            k += 1
        lcp += k
    print(f"STATS N={n/1e6:.2f}M MiB={total/2**20:.0f} "
          f"unique={uniq/n*100:.1f}% Lavg={total/n:.0f} "
          f"LCPavg={lcp/(n-1):.0f}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
