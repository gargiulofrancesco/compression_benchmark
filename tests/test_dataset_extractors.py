"""Per-dataset extractor bytewise-identity oracles.

For each real dataset, run the extractor against the committed raw sample
under testdata/datasets/ and assert the output column files match the
committed .fixture.col bytes exactly. Catches drift in extractor logic
(e.g. field-index changes, character-encoding changes) at smoke speed
without network or duckdb.

The TPC-H extractor is tested only when (a) duckdb is installed AND
(b) an SF=0.01 fixture file exists (none is committed). Otherwise the test
skips cleanly with an explicit skip reason — never a silent pass.
"""
from __future__ import annotations
import importlib
from pathlib import Path
from unittest.mock import patch

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
            "TPC-H SF=0.01 fixture (testdata/datasets/tpch__l_comment.fixture.col) is not "
            "committed; this regression oracle runs only where one has been generated"
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



@pytest.mark.parametrize("marker", [
    "TBD-AT-INTEGRATION", "recorded-at-fetch", "rolling", "n/a"])
def test_download_accepts_documented_non_digest_markers(
        tmp_path: Path, marker: str) -> None:
    from tools.datasets._common import download_if_missing

    target = tmp_path / "archive.bin"
    target.write_bytes(b"fixture")
    # Existing targets do not open the network; the patch makes that invariant
    # explicit so this test also guards against accidental external I/O.
    with patch("tools.datasets._common._urlopen_retry") as urlopen:
        assert download_if_missing(
            "https://invalid.example/archive", target,
            expected_sha256=marker) == target
        urlopen.assert_not_called()


def test_recorded_checksums_gate_headline_columns_only(capsys) -> None:
    from tools.datasets.prepare import check_recorded_checksums
    good, other = "a" * 64, "b" * 64
    entry = {"name": "d",
             "columns": [{"name": "h", "headline": True}, {"name": "x"}, {"name": "new"}],
             "output_sha256": {"h": good, "x": good, "new": "TBD-AT-INTEGRATION"}}
    assert check_recorded_checksums(entry, {"h": good, "x": good, "new": other}) == []
    # a non-headline column may drift (rolling dump); a headline column may not
    assert check_recorded_checksums(entry, {"h": good, "x": other}) == []
    assert check_recorded_checksums(entry, {"h": other, "x": good}) == ["d/h"]
    assert "differs from the recorded" in capsys.readouterr().out


def test_paper_columns_keeps_only_reported_columns() -> None:
    from tools.datasets.prepare import paper_columns
    entry = {"name": "d", "columns": [{"name": "h", "headline": True}, {"name": "x"}]}
    assert paper_columns(entry)["columns"] == [{"name": "h", "headline": True}]
    assert entry["columns"] == [{"name": "h", "headline": True}, {"name": "x"}]   # not modified
    assert paper_columns({"name": "rolling", "columns": [{"name": "x"}]})["columns"] == []
