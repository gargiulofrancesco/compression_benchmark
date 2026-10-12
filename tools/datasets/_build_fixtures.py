"""One-shot script to (re)generate committed .fixture.col files from the
hand-crafted raw samples under testdata/datasets/. NOT invoked by smoke;
run by hand when fixtures are first authored or when an extractor changes
its output format.

  PYTHONPATH=. python -m tools.datasets._build_fixtures
"""
from __future__ import annotations
import shutil
import tempfile
from pathlib import Path

from . import dblp


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURE_DIR = REPO_ROOT / "testdata" / "datasets"


def _build_one(name: str, extractor, raw: Path, manifest_entry: dict,
               column_map: dict[str, str]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        cols = extractor.extract_columns(raw, tmp_dir, manifest_entry)
        for col_name, src in cols.items():
            target_name = column_map.get(col_name)
            if target_name is None:
                continue
            dst = FIXTURE_DIR / target_name
            shutil.copy(src, dst)
            print(f"  {col_name}: {dst} ({dst.stat().st_size} bytes)")


def main() -> int:
    print("=== dblp ===")
    _build_one(
        "dblp", dblp,
        FIXTURE_DIR / "dblp__dump.raw.fixture.xml",
        manifest_entry={"columns": [{"name": "titles"}, {"name": "authors"}]},
        column_map={"titles":  "dblp__titles.fixture.col",
                    "authors": "dblp__authors.fixture.col"},
    )
    # NOTE: the amazon / dbpedia / msmarco_urls / msmarco_queries extractors have
    # no committed raw fixtures; adding one means a small sample under
    # testdata/datasets/ plus an oracle test.
    print("\ndone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
