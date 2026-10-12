"""Structural test of the headline cell generator.

Asserts experiments.gen_headline_cells expands the real-column × roster grid
correctly: 11 lossless baseline rows × every `headline: true` column in datasets.yaml, each
cell carrying dataset_kind=real, a materialized column_path, and a parseable
config_json. BPE contributes TWO size-matched rows: `bpe` (V=16384=2^14) and
`bpe_v65536` (V=65536=2^16, dictionary-size-matched to OnPair headline bits=16) —
both driven by the same bpe runner binary via RUNNER_ALIASES. The smoke doc must
be the documented lossless trio backed by the committed synthetic fixture.
"""
from __future__ import annotations
import json
from pathlib import Path

import yaml

from experiments import gen_headline_cells as G


REPO_ROOT = Path(__file__).resolve().parent.parent

# The paper's roster: includes the zstd level twins (zstd_l1/zstd_l9), not
# fsst_scalar (bit-identical to fsst on a machine without AVX-512).
EXPECTED_BASELINES = {
    "uncompressed", "zstd", "zstd_l1", "zstd_l9", "lz4", "fsst", "repair",
    "bpe", "bpe_v65536", "rlz", "onpair",
}


def _docs():
    datasets = yaml.safe_load((REPO_ROOT / "experiments" / "datasets.yaml").read_text())
    roster = yaml.safe_load((REPO_ROOT / "experiments" / "headline_roster.yaml").read_text())
    return datasets, roster


def _n_real_columns(datasets: dict) -> int:
    # Headline grid = only columns tagged `headline: true` (high-cardinality scope).
    return sum(1 for ds in datasets["datasets"] if ds.get("kind") == "real"
               for col in ds["columns"] if col.get("headline", False))


def test_full_grid_shape() -> None:
    datasets, roster = _docs()
    doc = G.build_full(datasets, roster)
    n_cols = _n_real_columns(datasets)
    n_base = len(EXPECTED_BASELINES)
    assert len(doc["cells"]) == n_cols * n_base, (
        f"expected {n_cols}×{n_base} cells, got {len(doc['cells'])}")
    assert doc["n_samples"] == 11
    assert doc["mem_cap_gb"] == 64


def test_roster_is_the_lossless_set_no_quarantine() -> None:
    _datasets, roster = _docs()
    names = {b["baseline"] for b in roster["baselines"]}
    assert names == EXPECTED_BASELINES
    assert "bpe" in names, "canonical BPE row must be present"
    assert "bpe_v65536" in names, "size-matched (V=2^16) BPE row must be present"
    assert "bpe-sentencepiece" not in names and "bpe-byte-level-hf" not in names, \
        "retired baseline names must not appear"


def test_every_cell_is_real_with_parseable_config() -> None:
    datasets, roster = _docs()
    doc = G.build_full(datasets, roster)
    for c in doc["cells"]:
        assert c["dataset_kind"] == "real"
        assert c["column_path"].startswith("datasets/materialized/")
        assert c["runner_path"].startswith("runners/")
        json.loads(c["config_json"])      # must parse
        assert c["baseline"] in EXPECTED_BASELINES


def test_cell_ids_unique() -> None:
    datasets, roster = _docs()
    doc = G.build_full(datasets, roster)
    ids = [c["cell_id"] for c in doc["cells"]]
    assert len(ids) == len(set(ids)), "duplicate headline cell_id"


def test_run_last_baselines_close_the_grid() -> None:
    datasets, roster = _docs()
    doc = G.build_full(datasets, roster)
    last = set(roster["run_last"])
    assert last == {"repair"}
    order = [c["baseline"] in last for c in doc["cells"]]
    n_last = sum(order)
    assert n_last == _n_real_columns(datasets) * len(last)
    assert order == [False] * (len(order) - n_last) + [True] * n_last


def test_parts_split_the_grid_at_run_last() -> None:
    datasets, roster = _docs()
    last = set(roster["run_last"])
    full = G.build_full(datasets, roster)["cells"]
    main = G.build_full(datasets, roster, "main")["cells"]
    tail = G.build_full(datasets, roster, "last")["cells"]
    assert main + tail == full
    assert main and all(c["baseline"] not in last for c in main)
    assert tail and all(c["baseline"] in last for c in tail)
    # both parts keep the headline protocol
    doc = G.build_full(datasets, roster, "last")
    assert doc["n_samples"] == 11 and doc["require_controlled_env"] is True


def test_smoke_doc_is_lossless_trio_on_committed_fixture() -> None:
    _datasets, roster = _docs()
    doc = G.build_smoke(roster)
    assert {c["baseline"] for c in doc["cells"]} == {"uncompressed", "zstd", "onpair"}
    for c in doc["cells"]:
        assert c["dataset_kind"] == "real"
        fixture = REPO_ROOT / c["column_path"]
        assert fixture.exists(), f"smoke fixture missing: {fixture}"
    assert doc["n_samples"] == 2


def test_generator_cli_writes_file(tmp_path: Path, monkeypatch) -> None:
    out = tmp_path / "hl.yaml"
    monkeypatch.setattr("sys.argv",
                        ["gen_headline_cells", "--out", str(out)])
    assert G.main() == 0
    reread = yaml.safe_load(out.read_text())
    datasets, roster = _docs()
    assert len(reread["cells"]) == _n_real_columns(datasets) * len(EXPECTED_BASELINES)


def test_selection_restricts_baselines_and_columns() -> None:
    datasets, roster = _docs()
    doc = G.build_full(datasets, roster, baselines=["onpair", "fsst"],
                       columns=["tpch/p_name"])
    assert [(c["dataset"], c["baseline"]) for c in doc["cells"]] == [
        ("tpch/p_name", "fsst"), ("tpch/p_name", "onpair")]      # roster order
    # a selected cell is the cell of the full grid, protocol included
    full = {c["cell_id"]: c for c in G.build_full(datasets, roster)["cells"]}
    assert all(full[c["cell_id"]] == c for c in doc["cells"])
    assert doc["n_samples"] == 11 and doc["require_controlled_env"] is True
    # the selection composes with the split at run_last
    last = G.build_full(datasets, roster, "last", baselines=["onpair", "repair"])
    assert {c["baseline"] for c in last["cells"]} == {"repair"}
    assert G.build_full(datasets, roster, "last", baselines=["onpair"])["cells"] == []


def test_selection_rejects_unknown_names() -> None:
    import pytest
    datasets, roster = _docs()
    with pytest.raises(SystemExit, match="unknown baseline zstd3; choose from: .*onpair"):
        G.build_full(datasets, roster, baselines=["zstd3"])
    with pytest.raises(SystemExit, match="unknown column tpch/name; choose from: .*tpch/p_name"):
        G.build_full(datasets, roster, columns=["tpch/name"])
