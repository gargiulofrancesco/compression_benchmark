"""Render diagnostic figures/tables from whichever stores are present.

`make figures` entry point. Resolves the headline store + each a*_results.sqlite
under --build-root and dispatches to the headline + ablation renderers. Missing
stores (or all-refused-env L_ra stores on a non-x86 machine) are skipped with a
printed note — safe to run after any partial set of `make bench` / `make a1..a7`
runs. Prints the list of artifacts written into --out. The figures and tables
of the paper itself come from tools/paper_figures.py and tools/paper_tables.py.
"""
from __future__ import annotations
import argparse
from pathlib import Path

from . import headline
from . import ablations


def render_all(build_root: Path, out_dir: Path) -> list[Path]:
    b = Path(build_root)
    results = b / "results.sqlite"
    a = {n: b / f"{n}_results.sqlite" for n in
         ("a1", "a2", "a3", "a4", "a5", "a6", "a7")}

    written: list[Path] = []
    written += headline.render_all(results, out_dir)            # F1_pareto, T1_ratio, T2_*
    written += ablations.render_f4(a["a1"], a["a3"], a["a7"], out_dir)
    written += ablations.render_a2(a["a2"], out_dir)
    written += ablations.render_a4(a["a4"], out_dir)
    written += ablations.render_a5(a["a5"], out_dir)
    written += ablations.render_a6(a["a6"], out_dir)
    written += ablations.render_f5(a["a6"], out_dir)
    written += ablations.render_t3(a["a2"], a["a4"], a["a5"], a["a7"], out_dir)
    return written


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--build-root", type=Path, default=Path("build"))
    p.add_argument("--out", type=Path, default=Path("build/figures"))
    a = p.parse_args()
    written = render_all(a.build_root, a.out)
    if not written:
        print(f"render_all: no stores found under {a.build_root} — nothing rendered "
              f"(run `make bench` / `make a1..a7` first)")
    else:
        for f in written:
            print(f"render_all: wrote {f}")
    print(f"render_all: {len(written)} artifact(s) → {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
