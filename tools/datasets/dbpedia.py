"""DBpedia short-abstracts extractor — long-text (Wikipedia lead paragraphs).

Upstream: DBpedia databus short-abstracts (English), N-Triples (.ttl.bz2).
One row per abstract: the literal object between the first '"' and the trailing
'"@en'. Capped at `subset.limit` rows (default 1,000,000).
High-cardinality long text (L_avg ~337 B, ~99.8% distinct).
"""
from __future__ import annotations
import bz2
from pathlib import Path

from ._common import download_if_missing, write_rows

_DEFAULT_LIMIT = 1_000_000


def download(target_dir: Path, manifest_entry: dict) -> Path:
    target = target_dir / "short-abstracts_en.ttl.bz2"
    return download_if_missing(
        manifest_entry["source"]["url"], target,
        expected_sha256=manifest_entry["source"]["sha256"],
    )


def extract_columns(raw_path: Path, output_dir: Path, manifest_entry: dict,
                    subset_params: dict | None = None) -> dict[str, Path]:
    limit = int((subset_params or {}).get("limit", _DEFAULT_LIMIT))
    abstracts: list[bytes] = []
    with bz2.open(str(raw_path), mode="rt", encoding="utf-8") as f:
        for line in f:
            if line.startswith("#") or not line.strip():
                continue
            # N-Triples: <subj> <pred> "object"@en .
            start = line.find('"')
            end = line.rfind('"@en')
            if start != -1 and end != -1 and end > start:
                text = line[start + 1:end].strip()
                if text:
                    abstracts.append(text.encode("utf-8"))
                    if len(abstracts) >= limit:
                        break
    out = output_dir / "abstract.col"
    write_rows(out, abstracts)
    return {"abstract": out}
