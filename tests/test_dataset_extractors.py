"""step 1 — per-dataset extractor bytewise-identity oracles.

For each real dataset, run the extractor against the committed raw sample
under testdata/datasets/ and assert the output column files match the
committed .fixture.col bytes exactly. Catches drift in extractor logic
(e.g. field-index changes, character-encoding changes) at smoke speed
without network or duckdb.

TPC-H / TPC-DS extractors are tested only when (a) duckdb is installed AND
(b) a previously-pinned SF=0.01 fixture file exists. Otherwise the tests
skip cleanly with an explicit skip reason — never a silent pass.
"""
from __future__ import annotations
import importlib
from pathlib import Path

import pytest

from tools.datasets import dblp


REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = REPO_ROOT / "testdata" / "datasets"


def _bytes(p: Path) -> bytes:
    return p.read_bytes()


def test_dblp_extractor_bytewise(tmp_path: Path) -> None:
    raw = FIXTURE_DIR / "dblp__dump.raw.fixture.xml"
    cols = dblp.extract_columns(
        raw, tmp_path,
        {"columns": [{"name": "titles"}, {"name": "authors"}]},
    )
    for col_name, fixture_name in (
        ("titles",  "dblp__titles.fixture.col"),
        ("authors", "dblp__authors.fixture.col"),
    ):
        assert _bytes(cols[col_name]) == _bytes(FIXTURE_DIR / fixture_name)


def _have_duckdb() -> bool:
    try:
        importlib.import_module("duckdb")
        return True
    except ImportError:
        return False


@pytest.mark.skipif(not _have_duckdb(), reason="duckdb not installed; TPC-H extractor untestable")
def test_tpch_extractor_bytewise(tmp_path: Path) -> None:
    fixture = FIXTURE_DIR / "tpch__l_comment.fixture.col"
    if not fixture.exists():
        pytest.skip(
            "TPC-H SF=0.01 fixture not generated yet; run `python -m tools.datasets.tpch "
            "--regenerate-fixture --sf 0.01` to materialise it for this regression oracle"
        )
    # If we have a fixture + duckdb, generate at SF=0.01 + compare.
    from tools.datasets import tpch
    raw_dir = tmp_path / "raw"
    out_dir = tmp_path / "out"
    raw_dir.mkdir(); out_dir.mkdir()
    manifest_entry = {
        "subset": {"sf": 0.01},
        "columns": [{"name": "l_comment"}],
    }
    raw = tpch.download(raw_dir, manifest_entry)
    cols = tpch.extract_columns(raw, out_dir, manifest_entry)
    assert _bytes(cols["l_comment"]) == _bytes(fixture)

# TPC-DS extractor test removed (rev-2b): TPC-DS dropped from the
# roster (phantom columns cp_url/cs_comment + no large string columns at SF=10).
