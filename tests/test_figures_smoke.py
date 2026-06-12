"""— figure/table renderer smoke gate.

Fabricates minimal status='ok' stores matching each schema (so the L_ra figures
exercise too — on this non-x86 box the real a5/a6 smoke produces only
refused-env rows), runs every renderer, and asserts each emits its artifacts.
Also asserts render_all over a directory of all fabricated stores produces the
full set, and that renderers return [] (not raise) on absent stores.
"""
from __future__ import annotations
import sqlite3
from pathlib import Path

from tools.plot import headline, ablations, render_all


REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA = REPO_ROOT / "schema"


def _mk(db: Path, schema_file: str, table: str, rows: list[dict]) -> None:
    """Create `db` from a schema file and insert rows, filling NOT-NULL columns
    not given by the caller with type-appropriate defaults."""
    conn = sqlite3.connect(str(db))
    conn.executescript((SCHEMA / schema_file).read_text())
    info = conn.execute(f"PRAGMA table_info({table})").fetchall()
    names = [c[1] for c in info]
    types = {c[1]: c[2] for c in info}
    for i, r in enumerate(rows):
        vals = []
        for n in names:
            if n in r:
                vals.append(r[n])
            elif n == "status":
                vals.append("ok")
            elif n == "run_id":
                vals.append("r")
            elif n == "cell_id":
                vals.append(f"c{i}")          # unique per row → no PK collision
            elif types[n].upper().startswith(("INT", "REAL")):
                vals.append(0)
            else:
                vals.append("x")
        conn.execute(f"INSERT INTO {table} VALUES ({','.join(['?'] * len(names))})", vals)
    conn.commit()
    conn.close()


def _headline_db(db: Path) -> None:
    rows = []
    cfg = "{}"
    for b in ("zstd", "onpair"):
        ib = 1000.0
        ob = 500.0 if b == "zstd" else 800.0
        for op, metric, val in [
            ("compress", "input_bytes", ib),
            ("compress", "output_bytes", ob),
            ("compress", "t_op_ns", 2_000_000.0),
            ("bulk_decode", "t_op_ns", 1_000_000.0),
        ]:
            rows.append({"run_id": "r", "cell_id": f"c-{b}", "baseline": b, "op": op,
                         "metric_name": metric, "n_samples": 3, "median": val,
                         "mad": 0.0, "mean": val, "ci95_lo": val, "ci95_hi": val,
                         "config_json": cfg, "dataset": "msmarco_urls/url"})
    _mk(db, "results.sql", "results_agg", rows)


def test_headline_figures(tmp_path: Path) -> None:
    db = tmp_path / "results.sqlite"
    _headline_db(db)
    out = tmp_path / "figs"
    files = headline.render_all(db, out)
    names = {f.name for f in files}
    assert "F1_pareto.png" in names
    assert "T1_ratio.md" in names and "T1_ratio.csv" in names
    assert any("T2_" in n for n in names)
    # zstd ratio = 1000/500 = 2.0 appears in the T1 table.
    assert "2.000" in (out / "T1_ratio.md").read_text()


def test_a2(tmp_path: Path) -> None:
    db = tmp_path / "a2_results.sqlite"
    _mk(db, "a2_results.sql", "a2_results", [
        {"dataset": "dblp", "column_name": "titles", "bits": b, "ratio": 2.0 + b / 100,
         "dict_bytes": 1000 * b, "decode_ns": 5e6} for b in (9, 12, 16)])
    files = ablations.render_a2(db, tmp_path / "figs")
    assert files and files[0].name == "A2_bitwidth.png"


def test_a4(tmp_path: Path) -> None:
    db = tmp_path / "a4_results.sqlite"
    _mk(db, "a4_results.sql", "a4_results", [
        {"dataset": "dblp", "column_name": "titles", "capacity": c, "ratio": 2.0,
         "m_train_bytes": c * 100, "decode_ns": 5e6} for c in (512, 4096, 65536)])
    files = ablations.render_a4(db, tmp_path / "figs")
    assert files and files[0].name == "A4_capacity.png"


def test_a5(tmp_path: Path) -> None:
    db = tmp_path / "a5_results.sqlite"
    rows = []
    for state in ("cold", "warm"):
        for b, bs in [("zstd", 256), ("zstd", 4096), ("onpair", None)]:
            rows.append({"baseline": b, "dataset": "msmarco_urls", "column_name": "url",
                         "block_size_strings": bs, "cache_state": state,
                         "l_ra_p50_ns": 100.0, "l_ra_p95_ns": 200.0, "l_ra_p99_ns": 300.0})
    _mk(db, "a5_results.sql", "a5_results", rows)
    files = ablations.render_a5(db, tmp_path / "figs")
    assert files and files[0].name == "A5_blocksize.png"


def _a6_rows() -> list[dict]:
    rows = []
    for layout in ("identity", "scramble"):
        for dist in ("uniform", "zipf1.0", "poisson_n2"):
            for b in ("zstd", "onpair"):
                rows.append({"baseline": b, "dataset": "msmarco_urls", "column_name": "url",
                             "query_distribution": dist, "query_layout": layout,
                             "cache_state": "warm", "l_ra_min_ns": 10.0,
                             "l_ra_p50_ns": 40.0, "l_ra_p95_ns": 80.0,
                             "l_ra_p99_ns": 120.0, "l_ra_max_ns": 200.0})
    return rows


def test_a6_and_f5(tmp_path: Path) -> None:
    db = tmp_path / "a6_results.sqlite"
    _mk(db, "a6_results.sql", "a6_results", _a6_rows())
    a6 = ablations.render_a6(db, tmp_path / "figs")
    f5 = ablations.render_f5(db, tmp_path / "figs")
    assert a6 and a6[0].name == "A6_distribution.png"
    assert f5 and f5[0].name == "F5_latency_cdf.png"


def test_f4(tmp_path: Path) -> None:
    a1 = tmp_path / "a1_results.sqlite"
    _mk(a1, "a1_results.sql", "a1_results", [
        {"column_name": "titles", "heldout_ratio_fixed": 2.0, "heldout_ratio_dyn": 1.8}])
    a3 = tmp_path / "a3_results.sqlite"
    _mk(a3, "a3_results.sql", "a3_results", [
        {"column_name": "titles", "sample_fraction": f, "heldout_ratio": 2.0 + f}
        for f in (0.05, 0.15, 0.3)])
    a7 = tmp_path / "a7_results.sqlite"
    _mk(a7, "a7_results.sql", "a7_results", [
        {"dataset": "dblp", "column_name": "titles", "seed": s, "ratio": 2.0}
        for s in (42, 7, 1729)])
    files = ablations.render_f4(a1, a3, a7, tmp_path / "figs")
    assert files and files[0].name == "F4_threshold.png"


def test_t3(tmp_path: Path) -> None:
    a2 = tmp_path / "a2_results.sqlite"
    _mk(a2, "a2_results.sql", "a2_results", [
        {"dataset": "dblp", "column_name": "titles", "bits": 16, "ratio": 2.5,
         "dict_bytes": 100, "decode_ns": 1}])
    files = ablations.render_t3(a2, tmp_path / "missing_a4.sqlite",
                                tmp_path / "missing_a5.sqlite",
                                tmp_path / "missing_a7.sqlite", tmp_path / "figs")
    assert files and any(f.name == "T3_ablation_crosstab.md" for f in files)
    assert "titles" in (tmp_path / "figs" / "T3_ablation_crosstab.md").read_text()


def test_render_all_full_set(tmp_path: Path) -> None:
    b = tmp_path / "build"
    b.mkdir()
    _headline_db(b / "results.sqlite")
    _mk(b / "a2_results.sqlite", "a2_results.sql", "a2_results", [
        {"dataset": "dblp", "column_name": "titles", "bits": 16, "ratio": 2.5,
         "dict_bytes": 100, "decode_ns": 1}])
    _mk(b / "a6_results.sqlite", "a6_results.sql", "a6_results", _a6_rows())
    written = render_all.render_all(b, b / "figures")
    names = {f.name for f in written}
    assert "F1_pareto.png" in names
    assert "A2_bitwidth.png" in names
    assert "A6_distribution.png" in names
    assert "F5_latency_cdf.png" in names


def test_renderers_skip_absent_stores(tmp_path: Path) -> None:
    missing = tmp_path / "nope.sqlite"
    assert headline.render_all(missing, tmp_path / "f") == []
    assert ablations.render_a2(missing, tmp_path / "f") == []
    assert ablations.render_a6(missing, tmp_path / "f") == []
    assert render_all.render_all(tmp_path / "empty_build", tmp_path / "f") == []
