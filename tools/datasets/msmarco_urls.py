"""MS MARCO document URLs extractor — high-cardinality navigational URLs.

Upstream: msmarco-docs.tsv.gz (docid <tab> url <tab> title <tab> body).
One row per document URL (field 1). ~3.21M rows, ~100% distinct, L_avg ~65 B,
high prefix redundancy (protocol/domain) — the URL standard for this workload.
Used at native size (no cap).
"""
from __future__ import annotations
import gzip
from pathlib import Path

from ._common import download_if_missing, write_rows


def download(target_dir: Path, manifest_entry: dict) -> Path:
    target = target_dir / "msmarco-docs.tsv.gz"
    return download_if_missing(
        manifest_entry["source"]["url"], target,
        expected_sha256=manifest_entry["source"]["sha256"],
    )


def extract_columns(raw_path: Path, output_dir: Path, manifest_entry: dict,
                    subset_params: dict | None = None) -> dict[str, Path]:
    urls: list[bytes] = []
    with gzip.open(str(raw_path), mode="rt", encoding="utf-8") as f:
        for line in f:
            parts = line.split("\t", 2)  # stop after the url field
            if len(parts) >= 2:
                url = parts[1]
                if url.startswith("http"):
                    urls.append(url.encode("utf-8"))
    out = output_dir / "url.col"
    write_rows(out, urls)
    return {"url": out}
