"""Paper tools test: main table, ratio-vs-decode scatter, claims audit.

Fabricates a small headline store (every baseline compresses 100 MiB to half
its size in 1 s and decodes it in 20 ms), optionally with the dictorder stage's
results.json and optionally without Re-Pair, and asserts that
  - T2_throughput (Table II) splits the D cell of BPE and OnPair by dictionary ID order when the JSON
    exists and prints the store's single number when it does not;
  - F1_pareto (Fig. 4) draws the second, hollow mark under the same condition, keeps it inside
    the axis limits, and skips a baseline that has no rows for a column;
  - the claims audit reports a check without data as SKIP instead of crashing,
    keeps PASS/FAIL and the exit status for the others, and exits 0 on SKIPs
    alone.
"""
from __future__ import annotations
import ast
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

matplotlib = pytest.importorskip("matplotlib")
from tools import paper_figures, paper_tables  # noqa: E402


REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA = REPO_ROOT / "schema"
COLUMNS = [ds for ds, _ in paper_tables.COLUMNS]
BASELINES = [b for b, _ in paper_tables.BASELINES]
MIB = 2**20
STORE_TD_GIBS = 100 * MIB / 0.02 / 2**30            # 4.88
DICTORDER = {"bpe_lexicographic": 5.52, "bpe_frequency": 6.14,
             "onpair_lexicographic": 5.44, "onpair_frequency": 6.06}


def _store(build: Path, skip: tuple = ()) -> None:
    """build/results.sqlite, without the (dataset, baseline) cells in `skip`."""
    build.mkdir(exist_ok=True)
    conn = sqlite3.connect(str(build / "results.sqlite"))
    conn.executescript((SCHEMA / "results.sql").read_text())
    metrics = [("compress", "input_bytes", 100 * MIB), ("compress", "dict_bytes", 0),
               ("compress", "tokens_bytes", 50 * MIB), ("compress", "t_compress_ns", 1e9),
               ("compress", "t_train_ns", 1e8), ("bulk_decode", "t_decode_ns", 2e7)]
    for ds in COLUMNS:
        for b in BASELINES:
            if (ds, b) in skip:
                continue
            for op, name, value in metrics:
                conn.execute(
                    "INSERT INTO results (run_id, cell_id, baseline, op, metric_name, "
                    "metric_value, status, config_json, dataset, sample_idx, machine_id, "
                    "baseline_build_flags_sha, ts_utc) VALUES "
                    "('r', ?, ?, ?, ?, ?, 'ok', '{}', ?, 0, 'm', 's', 't')",
                    (f"{ds}-{b}", b, op, name, value, ds))
    conn.commit()
    conn.close()


def _dictorder(build: Path) -> None:
    path = build / "dictorder" / "results.json"
    path.parent.mkdir()
    summary = {k: {"median_gib_per_second": v} for k, v in DICTORDER.items()}
    path.write_text(json.dumps({"datasets": {ds: {"summary": summary} for ds in COLUMNS}}))


def _t2(build: Path, out: Path) -> tuple[dict, dict]:
    """Run gen_t2; return ({first cell of a row: the row}, ledger)."""
    out.mkdir()
    ledger: dict = {}
    conn = sqlite3.connect(str(build / "results.sqlite"))
    paper_tables.gen_t2([conn], out, ledger, build)
    conn.close()
    lines = (out / "T2_throughput.tex").read_text().splitlines()
    return {line.split(" & ")[0].strip(): line for line in lines}, ledger


def test_t2_reports_bpe_under_sorted_ids(tmp_path: Path) -> None:
    build = tmp_path / "build"
    _store(build)
    _dictorder(build)
    rows, ledger = _t2(build, tmp_path / "tables")
    # BPE: the stage's sorted-id number; OnPair: its store number, already sorted
    assert rows["\\compressor{BPE}"].count(" & 5.5 ") + rows["\\compressor{BPE}"].count(" & 5.5\\") == len(COLUMNS)
    assert rows["\\onpair"].count(" & 4.9 ") + rows["\\onpair"].count(" & 4.9\\") == len(COLUMNS)
    assert not [row for row in rows.values() if "\\,/\\," in row]
    assert ("\\caption{Main results: compression ratio (R, payload), compression"
            " throughput (C, MiB/s), and decompression throughput (D, GiB/s; BPE"
            " and \\onpair with sorted ids)}") in rows
    # both orders of both codecs stay in the ledger, next to the store's number
    assert ledger["td_gibs_sorted/tpch/p_name/bpe_v65536"] == 5.52
    assert ledger["td_gibs_freq/tpch/p_name/bpe_v65536"] == 6.14
    assert ledger["td_gibs_sorted/dbpedia/abstract/onpair"] == 5.44
    assert ledger["td_gibs_freq/dbpedia/abstract/onpair"] == 6.06
    assert ledger["td_gibs/tpch/p_name/onpair"] == pytest.approx(STORE_TD_GIBS)


def test_t2_falls_back_without_dictorder_json(tmp_path: Path) -> None:
    build = tmp_path / "build"
    _store(build)
    rows, ledger = _t2(build, tmp_path / "tables")
    for label in ("\\compressor{BPE}", "\\onpair"):
        assert "/" not in rows[label]
        assert rows[label].count(" & 4.9") == len(COLUMNS)
    assert ("\\caption{Main results: compression ratio (R, payload), compression"
            " throughput (C, MiB/s), and decompression throughput (D, GiB/s)}") in rows
    assert not [k for k in ledger if k.startswith(("td_gibs_sorted", "td_gibs_freq"))]


def _f1(build: Path, out: Path, monkeypatch) -> list[tuple]:
    """Run f1; return per panel (scatter marks, joining lines, upper x limit)."""
    out.mkdir()
    figs: list = []
    with monkeypatch.context() as m:
        m.setattr(paper_figures.plt, "close", figs.append)
        paper_figures.f1(build, out)
    assert (out / "F1_pareto.pdf").stat().st_size > 256
    panels = [(len(ax.collections), len(ax.lines), ax.get_xlim()[1])
              for ax in figs[0].axes]
    paper_figures.plt.close(figs[0])
    return panels


def test_f1_marks_by_id_order(tmp_path: Path, monkeypatch) -> None:
    build = tmp_path / "build"
    n = len(paper_figures.STYLE)
    # Re-Pair has no rows for the first column: skipped there, not a crash
    _store(build, skip=((COLUMNS[0], "repair"),))
    assert [p[:2] for p in _f1(build, tmp_path / "plain", monkeypatch)] == \
        [(n - 1, 0)] + [(n, 0)] * 5
    _dictorder(build)
    panels = _f1(build, tmp_path / "split", monkeypatch)
    # BPE and OnPair: one more (hollow) mark and one joining line each
    assert [p[:2] for p in panels] == [(n + 1, 2)] + [(n + 2, 2)] * 5
    assert all(xmax > DICTORDER["bpe_frequency"] for _, _, xmax in panels)


def _audit(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    """Run the audit against cwd/build (it reads the relative path `build`)."""
    return subprocess.run(
        [sys.executable, "-m", "tools.claims_audit", *args], cwd=str(cwd),
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        capture_output=True, text=True, timeout=60)


def test_claims_audit_skips_checks_without_repair(tmp_path: Path) -> None:
    build = tmp_path / "build"
    _store(build, skip=tuple((ds, "repair") for ds in COLUMNS))
    ra = sqlite3.connect(str(build / "ra_headline_results.sqlite"))
    ra.execute("CREATE TABLE ra_headline_results "
               "(baseline, dataset, column_name, status, l_ra_p50_ns)")
    ra.executemany("INSERT INTO ra_headline_results VALUES (?, ?, ?, 'ok', 100.0)",
                   [(b, *ds.split("/")) for ds in COLUMNS for b in BASELINES
                    if b != "repair"])
    ra.commit()
    ra.close()
    result = _audit(tmp_path)
    assert "Traceback" not in result.stderr, result.stderr
    lines = result.stdout.splitlines()
    for name in ("repair wins ratio on exactly 5/6 (all but p_name)",
                 "repair Tc 0.1-1.7 MiB/s", "repair p50 0.64-4.1us"):
        assert any(line.startswith(f"  SKIP {name} ") for line in lines), result.stdout
    # checks that have their data keep their verdict, and a FAIL the exit status
    assert "  PASS onpair Tc 93-162 MiB/s [100,100]" in lines
    assert "  FAIL zstd9 Tc 55-69 MiB/s [100,100]" in lines
    assert "  PASS per-string p50 64-349ns [100,100]" in lines
    assert result.returncode == 1


def test_claims_audit_exits_zero_on_skips_alone(tmp_path: Path) -> None:
    build = tmp_path / "build"
    build.mkdir()
    result = _audit(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    skipped = [line.split(": ", 1)[1] for line in result.stdout.splitlines()
               if line.startswith("   SKIPPED: ")]
    # with no store at all every check is skipped, each under its own name
    # claims are stated either as check("name", ...) or as @claim("name", ...)
    tree = ast.parse((REPO_ROOT / "tools" / "claims_audit.py").read_text())
    checks = sorted(node.args[0].value for node in ast.walk(tree)
                    if isinstance(node, ast.Call)
                    and getattr(node.func, "id", None) in ("check", "claim")
                    and isinstance(node.args[0], ast.Constant))
    assert sorted(skipped) == checks
    assert f"== RESULT: 0 pass, 0 fail, {len(checks)} skip" in result.stdout
    assert not list(build.iterdir()), "the audit must not create stores"


def test_figures_skip_what_has_not_been_measured(tmp_path: Path, monkeypatch) -> None:
    """A build root without any store: every figure is skipped, nothing fails,
    and no empty store is left behind for the next run to trip over."""
    build, out = tmp_path / "build", tmp_path / "figures"
    build.mkdir()
    monkeypatch.setattr(sys, "argv", ["paper_figures", "--build-root", str(build),
                                      "--out", str(out)])
    assert paper_figures.main() == 0
    assert not list(build.iterdir())
    assert not list(out.glob("*.pdf"))


def _scale(store: Path, statement: str) -> None:
    con = sqlite3.connect(store)
    con.execute(statement)
    con.commit()
    con.close()


def test_audit_of_a_new_measurement_tells_timings_from_sizes(tmp_path: Path) -> None:
    """With --timing-tolerance, as `./reproduce.sh paper` runs it: a timing
    near the printed value passes, one far from it is a DIFF that leaves the
    exit status alone, and a size that differs is still a FAIL."""
    import shutil
    from tools import published_results
    build = tmp_path / "build"
    shutil.copytree(published_results.unpack(results=tmp_path / "published"), build)
    block, skew = "block size on URLs", "query skew on URLs"
    seeds = "five shuffle seeds"

    def verdicts(*args):
        result = _audit(tmp_path, *args)
        assert "Traceback" not in result.stderr, result.stderr
        found = {}
        for line in result.stdout.splitlines():
            for name in (block, skew, seeds):
                if line[2:6] in ("PASS", "FAIL", "DIFF") and line[7:].startswith(name):
                    found[name] = line[2:6]
        return found, result

    # the published stores hold to the printed digit either way
    for args in ((), ("--timing-tolerance", "10")):
        found, result = verdicts(*args)
        assert set(found.values()) == {"PASS"} and result.returncode == 0
        assert "38 pass, 0 fail" in result.stdout

    # latencies 3% slower in one study, 30% slower in another
    _scale(build / "a5_results.sqlite", "UPDATE a5_results SET l_ra_p50_ns = l_ra_p50_ns * 1.03")
    _scale(build / "a6_results.sqlite", "UPDATE a6_results SET l_ra_p50_ns = l_ra_p50_ns * 1.3")
    found, result = verdicts()
    assert found == {block: "FAIL", skew: "FAIL", seeds: "PASS"} and result.returncode == 1
    found, result = verdicts("--timing-tolerance", "10")
    assert found == {block: "PASS", skew: "DIFF", seeds: "PASS"} and result.returncode == 0
    assert "37 pass, 0 fail, 1 differ, 0 skip" in result.stdout
    assert f"   DIFFERS: {skew}" in result.stdout

    # a size is not a timing: no tolerance applies
    _scale(build / "a7_results.sqlite",
           "UPDATE a7_results SET tokens_bytes = tokens_bytes * 1.05 WHERE rowid = 1")
    found, result = verdicts("--timing-tolerance", "10")
    assert found[seeds] == "FAIL" and result.returncode == 1
