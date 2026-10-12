"""Amazon Book titles extractor — the e-commerce headline column.

Upstream: Amazon Reviews 2023 (McAuley lab), Books category metadata
(meta_Books.jsonl.gz). One row per non-empty `title` field, kept in ORIGINAL
file order (datasets are never shuffled; the JSONL order is the natural
dump order): json per line, title.strip(), skip empties.

Stats (Table I): ~4.45M rows, 86.9% distinct, L_avg 52 B.
"""
from __future__ import annotations
import gzip
import json
from pathlib import Path

from ._common import download_if_missing, write_rows


def download(target_dir: Path, manifest_entry: dict) -> Path:
    target = target_dir / "meta_Books.jsonl.gz"
    return download_if_missing(
        manifest_entry["source"]["url"], target,
        expected_sha256=manifest_entry["source"]["sha256"],
    )


def extract_columns(raw_path: Path, output_dir: Path, manifest_entry: dict,
                    subset_params: dict | None = None) -> dict[str, Path]:
    rows: list[bytes] = []
    with gzip.open(str(raw_path), "rt", encoding="utf-8") as f:
        for line in f:
            try:
                title = json.loads(line).get("title", "").strip()
            except json.JSONDecodeError:
                continue
            if title:
                rows.append(title.encode("utf-8"))
    out = output_dir / "titles.col"
    write_rows(out, rows)
    return {"titles": out}
