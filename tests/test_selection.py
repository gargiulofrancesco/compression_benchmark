"""Running a part of the experiments: the names a reader can select, the
table printed from a result directory, and the stage runner's refusals."""
from __future__ import annotations
import os
import re
import subprocess
from pathlib import Path

import yaml

from experiments import ra_headline_driver
from tools import paper_tables, published_results, show_results, subset_cells

REPO_ROOT = Path(__file__).resolve().parent.parent


def _reproduce(*stages: str, **env: str) -> subprocess.CompletedProcess:
    return subprocess.run(["./reproduce.sh", *stages], cwd=REPO_ROOT, text=True,
                          capture_output=True, timeout=60,
                          env={**os.environ, **env})


def test_the_selectable_names_agree_everywhere() -> None:
    # every baseline of the roster has a label, hence a place in the listing
    assert set(show_results.roster()) <= set(show_results.LABELS)
    # one list of six columns: tables, point-access driver, stage runner
    columns = show_results.columns()
    assert columns == [c for c, _ in paper_tables.COLUMNS]
    assert sorted(columns) == sorted(f"{ds}/{fn.removesuffix('.col')}"
                                     for ds, fn in ra_headline_driver.HEADLINE_COLUMNS)
    script = (REPO_ROOT / "reproduce.sh").read_text()
    declared = re.search(r"PAPER_COLUMNS=\(([^)]*)\)", script).group(1).split()
    assert declared == columns
    declared = re.search(r"ROSTER=\(([^)]*)\)", script).group(1).split()
    assert sorted(declared) == sorted(show_results.roster())
    # the point-access driver measures a subset of the roster
    assert set(ra_headline_driver.RUNNER_REL) <= set(show_results.roster())


def test_list_prints_baselines_and_columns(capsys) -> None:
    show_results.print_names()
    out = capsys.readouterr().out
    assert re.search(r"^  zstd +Zstd-3$", out, re.M)
    assert re.search(r"^  onpair +OnPair$", out, re.M)
    assert "\n  tpch/p_name\n" in out


def test_table_of_the_published_measurements(tmp_path: Path, capsys) -> None:
    combined = published_results.unpack(results=tmp_path)
    assert show_results.show(combined) == 0
    out = capsys.readouterr().out
    assert "Main grid: 3 passes pooled" in out
    block = out.split("\ntpch/p_name")[1].split("\n\n")[0]
    row = next(l for l in block.splitlines() if l.split()[:1] == ["onpair"]).split()
    # ratio and decompression speed as printed in Table II, 33 repetitions,
    # then the two latencies of the point-access store
    assert row[1] == "5.19" and row[4] == "33" and len(row) == 7
    assert float(row[5]) < float(row[6]) < 1000
    # Re-Pair has a single pass
    assert next(l for l in block.splitlines() if l.split()[:1] == ["repair"]).split()[4] == "11"
    assert "sorted ids" in out


def test_table_covers_the_other_studies(tmp_path: Path, capsys) -> None:
    """The numbers the paper quotes from the ablations and the sweeps are
    readable in the table of the published measurements."""
    combined = published_results.unpack(results=tmp_path)
    assert show_results.show(combined) == 0
    out = capsys.readouterr().out

    def row(block: str, label: str, after: str = "") -> list[str]:
        text = out.split(block, 1)[1]
        text = text.split(after, 1)[1] if after else text
        return next(l for l in text.splitlines() if l.strip().startswith(label)).split()

    # thresholds: a tuned fixed threshold wins by up to 16% on p_name
    assert row("Thresholds (a1)", "tpch/p_name")[-1] == "-15.7"
    assert row("Training budget (a3)", "tpch/p_name")[1:] == ["2.99", "2.96", "2.89", "2.71", "2.53"]
    assert row("Shuffle seeds (a7)", "msmarco_urls/url")[1:] == ["5", "3.071", "3.082", "0.37%"]
    assert row("Dictionary capacity (a2)", "msmarco_urls/url")[1:] == [
        "1.80", "2.12", "2.49", "2.80", "3.08"]
    assert row("Model size (modelsize)", "ratio", after="tpch/c_comment")[1:] == [
        "3.93", "4.10", "4.26", "4.38", "4.39", "4.13", "3.39"]
    # block size on URLs: Zstd 5.7 us -> 3.0 ms; OnPair has no block
    zstd = row("Block size (a5)", "zstd", after="msmarco_urls/url")
    assert zstd[1:3] == ["5.7", "us"] and zstd[-2:] == ["2.99", "ms"]
    assert row("Block size (a5)", "onpair", after="msmarco_urls/url") == ["onpair", "289", "ns"]
    # query skew on URLs: OnPair 286 ns uniform, 30 ns under Zipf-1.5
    assert row("Query skew (a6)", "uniform identity")[-2:] == ["286", "ns"]
    assert row("Query skew (a6)", "zipf1.5 identity")[-2:] == ["30", "ns"]
    assert row("compression ratio by string length", "L=32 rho=0.95")[2:] == [
        "2.64", "4.22", "2.88", "3.98"]
    assert row("Scale (scale)", "10,000,000")[1:] == ["300", "MiB", "timeout", "64.04"]
    assert "a4 (whole-operation times" in out and "decode: `./reproduce.sh paper`" in out


def test_table_of_a_single_study(tmp_path: Path, capsys) -> None:
    import shutil
    combined = published_results.unpack(results=tmp_path / "published")
    alone = tmp_path / "alone"
    alone.mkdir()
    shutil.copy(combined / "a7_results.sqlite", alone)
    assert show_results.show(alone) == 0
    out = capsys.readouterr().out
    assert "Shuffle seeds (a7)" in out and "Thresholds" not in out and "tpch/p_name  (" not in out


def test_table_of_an_empty_directory(tmp_path: Path, capsys) -> None:
    assert show_results.show(tmp_path) == 1
    assert "no measurement" in capsys.readouterr().out


def test_ablation_grid_restricted_to_columns() -> None:
    doc = yaml.safe_load((REPO_ROOT / "experiments" / "a1_cells.yaml").read_text())
    cut = subset_cells.subset(doc, ["tpch/p_name", "amazon/titles"])
    assert cut["columns"] == [{"dataset": "amazon", "column": "titles"},
                              {"dataset": "tpch", "column": "p_name"}]
    assert {k: v for k, v in cut.items() if k != "columns"} == \
           {k: v for k, v in doc.items() if k != "columns"}
    # a5 studies two columns, neither of them p_name
    a5 = yaml.safe_load((REPO_ROOT / "experiments" / "a5_cells.yaml").read_text())
    assert subset_cells.subset(a5, ["tpch/p_name"])["columns"] == []


def test_stage_runner_refuses_what_it_cannot_do() -> None:
    r = _reproduce("headline", COLS="tpch/name")
    assert r.returncode == 1 and "unknown column 'tpch/name'" in r.stderr
    r = _reproduce("headline", BASELINES="onpair,zstd-3")
    assert r.returncode == 1 and "unknown baseline 'zstd-3'" in r.stderr
    r = _reproduce("all", BASELINES="onpair")
    assert r.returncode == 1 and "name the stages to run" in r.stderr
    r = _reproduce("headlines")
    assert r.returncode == 1 and "unknown stage 'headlines'" in r.stderr
    # a timing stage checks what it needs of the machine before it starts
    for stage in ("finalize", "scale", "access", "decode"):
        r = _reproduce(stage, CPU="999999")
        assert r.returncode == 1 and r.stdout == ""
        assert f"reproduce: {stage} cannot be measured on this machine as it is" in r.stderr
        assert "not a CPU this process may run on" in r.stderr
    # refused before the first stage runs, wherever `all` stands
    for stages in (("list", "all"), ("all", "list")):
        r = _reproduce(*stages)
        assert r.returncode == 1 and r.stdout == ""
        assert "'all' must be the only argument" in r.stderr
    # the usage text is the whole header of the script, and only that
    usage = _reproduce().stdout
    assert usage.startswith("# Reproduce the paper") and "set -euo" not in usage
    assert usage.rstrip().endswith("compares the machine with the paper's.")
