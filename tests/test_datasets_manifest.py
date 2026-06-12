"""step 1 — datasets.yaml schema contract.

The manifest is the source of truth for experiment runners; bugs in
its schema or license-id cross-references are caught here before they bleed
into the per-dataset extractor tests or the experiment runners.
"""
from __future__ import annotations
import re
import sys
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = REPO_ROOT / "experiments" / "datasets.yaml"
LICENSING = REPO_ROOT / "LICENSING"


VALID_LICENSE_IDS = {
    "TPC-academic",
    "ODC-BY",                  # DBLP
    "CC-BY-SA-4.0",            # DBpedia abstracts
    "MS-MARCO-non-commercial", # MS MARCO URLs + queries
    "amazon-reviews-2023-research",  # Amazon Reviews 2023 (McAuley lab)
    "n/a",  # synthetic
}


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _load() -> dict:
    with open(MANIFEST) as f:
        return yaml.safe_load(f)


def test_manifest_loads() -> None:
    m = _load()
    assert isinstance(m, dict)
    assert "datasets" in m and isinstance(m["datasets"], list)


def test_six_real_plus_one_synthetic() -> None:
    # 6 real datasets (TPC-H, DBLP, Amazon, DBpedia, MS MARCO URLs,
    # MS MARCO queries) + 1 synthetic.
    m = _load()
    by_kind: dict[str, int] = {}
    for entry in m["datasets"]:
        by_kind[entry["kind"]] = by_kind.get(entry["kind"], 0) + 1
    assert by_kind.get("real") == 6, f"expected 6 real entries, got {by_kind}"
    assert by_kind.get("synthetic_vd") == 1, f"expected 1 synthetic_vd entry, got {by_kind}"


def test_required_fields_per_entry() -> None:
    m = _load()
    for entry in m["datasets"]:
        assert "name" in entry and isinstance(entry["name"], str)
        assert "kind" in entry
        src = entry.get("source") or {}
        assert "url" in src
        assert "sha256" in src
        assert "license_id" in src
        assert src["license_id"] in VALID_LICENSE_IDS, (
            f"{entry['name']}: unknown license_id {src['license_id']}"
        )
        if entry["kind"] == "real":
            assert "extractor" in entry
            assert "columns" in entry and isinstance(entry["columns"], list)
            assert "held_out" in entry
            assert "output_sha256" in entry
            # smoke_fixture optional: some datasets (dbpedia, msmarco_*)
            # have no per-dataset extractor fixtures; the smoke run uses the
            # synthetic V-D fixture (see headline_roster).


def test_sha256_field_is_valid_or_placeholder() -> None:
    m = _load()
    for entry in m["datasets"]:
        sha = entry["source"]["sha256"]
        if sha in ("n/a", "rolling", "recorded-at-fetch"):
            continue
        assert SHA256_RE.match(sha), (
            f"{entry['name']}: source.sha256 must be 64-hex or a documented placeholder; got {sha!r}"
        )
        # If sha256 is a hex digest, output_sha256 entries must be too (real only).
        if entry["kind"] == "real":
            for col, observed in entry["output_sha256"].items():
                assert SHA256_RE.match(observed), (
                    f"{entry['name']}/{col}: output_sha256 must be 64-hex; "
                    f"got {observed!r}"
                )


def test_license_ids_cross_referenced_in_LICENSING() -> None:
    """Every license_id used by a real-dataset entry must appear by name in
    the LICENSING file. Catches drift between manifest and license registry."""
    m = _load()
    licensing = LICENSING.read_text()
    used = {e["source"]["license_id"] for e in m["datasets"] if e["kind"] == "real"}
    for lic in used:
        assert lic in licensing, (
            f"license_id {lic!r} used in datasets.yaml is missing from {LICENSING}"
        )


def test_smoke_fixtures_committed() -> None:
    """Every smoke_fixture.columns entry points to an existing file.

    Exception: entries whose `smoke_fixture.sf_fixture` is set (TPC-H/TPC-DS)
    generate their fixtures via DuckDB at SF=0.01 — those are not committed
    and the corresponding per-dataset extractor test skips when the file is
    missing. They appear in the manifest as forward documentation."""
    m = _load()
    for entry in m["datasets"]:
        fix = entry.get("smoke_fixture") or {}
        is_duckdb_generated = "sf_fixture" in fix
        cols = fix.get("columns") or {}
        for col_name, rel_path in cols.items():
            p = REPO_ROOT / rel_path
            if is_duckdb_generated and not p.exists():
                continue  # DuckDB-gated; per-extractor test handles skip
            assert p.exists(), (
                f"{entry['name']}/{col_name}: smoke_fixture {rel_path} not committed"
            )
        # V-D entry uses a different structure.
        cells = fix.get("cells") or []
        for cell in cells:
            p = REPO_ROOT / cell["fixture"]
            assert p.exists(), f"synthetic_vd fixture missing: {cell['fixture']}"


def test_extractor_modules_importable() -> None:
    """Every real entry's `extractor` resolves to a Python module."""
    m = _load()
    sys.path.insert(0, str(REPO_ROOT))
    import importlib
    for entry in m["datasets"]:
        if entry["kind"] != "real":
            continue
        mod_name = entry["extractor"]
        mod = importlib.import_module(mod_name)
        assert hasattr(mod, "extract_columns"), (
            f"{entry['name']}: extractor module {mod_name} missing extract_columns"
        )
        assert hasattr(mod, "download"), (
            f"{entry['name']}: extractor module {mod_name} missing download"
        )


def test_held_out_fraction_in_open_interval() -> None:
    m = _load()
    for entry in m["datasets"]:
        if entry["kind"] != "real":
            continue
        ho = entry["held_out"]
        assert 0.0 < ho["fraction"] < 1.0
