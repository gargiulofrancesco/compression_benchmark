"""Shared helpers for tools/datasets/<name>.py extractor modules."""
from __future__ import annotations
import hashlib
import struct
import time
import urllib.error
import urllib.request
from pathlib import Path

from orchestrator.column_io import Column, write_column


def _urlopen_retry(url: str, *, retries: int = 5, backoff: float = 3.0):
    """Open `url` with a descriptive UA and retry transient failures.

    Dataset mirrors (dblp.org, for one) intermittently 503 / throttle large
    fetches; a bare urlopen aborts the whole pipeline. Retry on 5xx + 429 +
    URLError with exponential backoff. Some CDNs also
    403 the default "Python-urllib/x.y" UA, so send our own (verified 200,
    honest, non-impersonating, accepted by all dataset hosts)."""
    req = urllib.request.Request(
        url, headers={"User-Agent": "onpair_bench-datasets/1.0"})
    last: Exception | None = None
    for attempt in range(retries):
        try:
            return urllib.request.urlopen(req)  # noqa: S310
        except urllib.error.HTTPError as e:
            last = e
            if e.code not in (429, 500, 502, 503, 504):
                raise
        except urllib.error.URLError as e:
            last = e
        wait = backoff * (2 ** attempt)
        print(f"    download retry {attempt + 1}/{retries} for {url} "
              f"after {type(last).__name__}: {last}; sleeping {wait:.0f}s")
        time.sleep(wait)
    assert last is not None
    raise last


def sha256_of(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            blob = f.read(chunk)
            if not blob:
                break
            h.update(blob)
    return h.hexdigest()


def download_if_missing(url: str, target: Path, *, expected_sha256: str | None = None) -> Path:
    """Idempotent: skip if target exists. Verify SHA-256 when given (and not one
    of the non-digest markers below); raise on mismatch.

    Network IO; never invoked by smoke.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        # Download to a .part file then atomically rename, so an interrupted
        # transfer never leaves a truncated target that the idempotent skip
        # would later treat as complete.
        part = target.with_suffix(target.suffix + ".part")
        with _urlopen_retry(url) as resp, open(part, "wb") as out:
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
        part.rename(target)
    # Several rolling/locally generated sources intentionally carry a manifest
    # marker instead of a digest. Final materialized-column hashes remain pinned.
    non_digests = {"TBD-AT-INTEGRATION", "recorded-at-fetch", "rolling", "n/a"}
    if expected_sha256 and expected_sha256 not in non_digests:
        if len(expected_sha256) != 64:
            raise ValueError(f"invalid expected SHA-256 marker/digest: {expected_sha256!r}")
        observed = sha256_of(target)
        if observed != expected_sha256:
            raise IOError(
                f"sha256 mismatch on {target}: expected {expected_sha256}, got {observed}"
            )
    return target


def rows_to_column(rows: list[bytes]) -> Column:
    """Pack `rows` into the harness binary column format."""
    offsets = [0]
    for r in rows:
        offsets.append(offsets[-1] + len(r))
    return Column(bytes_=b"".join(rows), offsets=offsets)


def write_rows(out_path: Path, rows: list[bytes]) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    write_column(str(out_path), rows_to_column(rows))
    return out_path


def cardinality(col_path: Path) -> tuple[int, int]:
    """(N, U) = total rows and distinct rows of a materialised .col.

    The high-cardinality scope is REPORTED (Table I, Uniq. %), not gated: this
    statistic feeds the dataset table and a sanity check that headline columns
    are genuinely high-cardinality. Reads the [N][total][offsets][bytes] format.
    """
    with open(col_path, "rb") as f:
        n, total = struct.unpack("<II", f.read(8))
        offs = struct.unpack(f"<{n + 1}I", f.read((n + 1) * 4))
        data = f.read(total)
    seen = {data[offs[i]:offs[i + 1]] for i in range(n)}
    return n, len(seen)
