from pathlib import Path

import pytest

# Skip if matplotlib unavailable (smoke runs ensure it's installed via venv)
matplotlib = pytest.importorskip("matplotlib")
from tools.plot import figure_template  # noqa: E402


FIXTURE = Path(__file__).resolve().parent.parent / "schema" / "results.fixture.csv"


def test_renders_from_csv_fixture(tmp_path):
    out = tmp_path / "fig.png"
    rows = figure_template._load_from_csv(FIXTURE)
    assert rows, "fixture CSV produced no rows for compress throughput"
    figure_template.render(rows, out, out_pdf=None)
    assert out.exists()
    head = out.read_bytes()[:8]
    assert head == b"\x89PNG\r\n\x1a\n", "output is not a valid PNG"
    assert out.stat().st_size > 256
