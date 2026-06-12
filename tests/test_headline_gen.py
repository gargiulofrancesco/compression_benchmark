"""— headline cell-generator structural gate.

Asserts experiments.gen_headline_cells expands the real-column × roster grid
correctly: 10 lossless baseline rows × every `headline: true` column in datasets.yaml, each
cell carrying dataset_kind=real, a materialized column_path, and a parseable
config_json. BPE contributes TWO size-matched rows: `bpe` (V=16384=2^14) and
`bpe_v65536` (V=65536=2^16, dictionary-size-matched to OnPair headline bits=16) —
both driven by the same bpe runner binary via RUNNER_ALIASES. The smoke doc must
be the documented lossless trio backed by the committed V-D fixture.
"""
from __future__ import annotations
import json
from pathlib import Path

import yaml

from experiments import gen_headline_cells as G


REPO_ROOT = Path(__file__).resolve().parent.parent

# ICDE roster: zstd level twins added (zstd_l1/zstd_l9);
# fsst_scalar dropped (bit-identical to fsst on the AVX2-only measurement box).
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
