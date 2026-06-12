import sys
from pathlib import Path

# Allow `pytest tests/` to import the `orchestrator` package without an install step.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
