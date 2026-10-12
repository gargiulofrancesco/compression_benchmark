"""baseline_modes.yaml loader + mode check (check_g7).

check_g7 asserts the runner's declared prefix_search_mode (from its hello
envelope) matches the expected value from baseline_modes.yaml. Mismatch is fatal.

is_lossless field: per-baseline boolean flag distinguishing baselines that
preserve arbitrary input bytes through compress→bulk_decode round-trip (true)
from ones that don't (false; quarantined from the main table and figure).
No baseline is currently quarantined — the `bpe` baseline (SentencePiece BPE via
the GPT-2 byte<->unicode bijection) is bytewise lossless — but the guard
machinery below is retained as a standing safety check.
"""
from __future__ import annotations
import yaml
from pathlib import Path

VALID_MODES = {"NativeOnCompressed", "DecompressThenScan", "Uncompressed"}

# random_access_model: how random_decode(i) reaches row i (L_ra fairness, Section VIII-B4).
#   per_string  — decode only row i (per-row offset/phrase index + resident dict)
#   block       — decode the enclosing fixed-size block, then slice row i
#   full_column — no RA index: decode the whole column, slice row i (worst)
VALID_RA_MODELS = {"per_string", "block", "full_column"}

# Variant-multiplexed runners: cell.baseline → expected hello.name.
# Same runner binary serves multiple cell-level baseline rows via config_json
# variant dimension. Default (no entry): cell.baseline == hello.name.
RUNNER_ALIASES: dict[str, str] = {
    # Dictionary-size-matched bpe twin (V=65536); same bpe runner (hello.name "bpe").
    "bpe_v65536":  "bpe",
    # Forced-scalar FSST twin (simd=false → AVX-512 compression off); same fsst
    # runner (hello.name "fsst").
    "fsst_scalar": "fsst",
    # zstd level twins (the paper's zstd-{1,3,9} rows); same zstd
    # runner binary (hello.name "zstd"), only the level config differs.
    "zstd_l1": "zstd",
    "zstd_l9": "zstd",
}


def check_runner_name(cell_baseline: str, hello_name: str) -> None:
    expected = RUNNER_ALIASES.get(cell_baseline, cell_baseline)
    if hello_name != expected:
        raise RuntimeError(
            f"runner name mismatch: cell.baseline={cell_baseline!r} expects "
            f"hello.name={expected!r} but runner reported {hello_name!r}"
        )


def load(path: Path) -> dict[str, str]:
    """Backward-compatible loader: returns {name: prefix_search_mode}. Drops
    the is_lossless flag; use load_full() to retain it."""
    data = yaml.safe_load(path.read_text())
    out: dict[str, str] = {}
    for row in data.get("baselines", []):
        name = row["name"]
        mode = row["prefix_search_mode"]
        if mode not in VALID_MODES:
            raise ValueError(f"baseline_modes: invalid mode '{mode}' for '{name}'")
        out[name] = mode
    return out


def load_full(path: Path) -> dict[str, dict]:
    """Full loader returning {name: {"prefix_search_mode", "is_lossless",
    "random_access_model"}}. is_lossless defaults to True and
    random_access_model defaults to "per_string" if absent (older yaml format)."""
    data = yaml.safe_load(path.read_text())
    out: dict[str, dict] = {}
    for row in data.get("baselines", []):
        name = row["name"]
        mode = row["prefix_search_mode"]
        if mode not in VALID_MODES:
            raise ValueError(f"baseline_modes: invalid mode '{mode}' for '{name}'")
        ra = row.get("random_access_model", "per_string")
        if ra not in VALID_RA_MODELS:
            raise ValueError(
                f"baseline_modes: invalid random_access_model '{ra}' for '{name}'")
        out[name] = {
            "prefix_search_mode": mode,
            "is_lossless": bool(row.get("is_lossless", True)),
            "random_access_model": ra,
        }
    return out


def random_access_models(path: Path) -> dict[str, str]:
    """Return {name: random_access_model}. The per-string vs block vs full_column
    split that determines L_ra fairness (Section VIII-B4); the A5/A6 block-vs-flat rosters
    derive from it."""
    return {n: info["random_access_model"] for n, info in load_full(path).items()}


def lossless_baselines(path: Path) -> set[str]:
    """Return the set of baselines flagged is_lossless: true. Headline-table
    aggregations (Table II, Fig. 4) MUST filter by membership in
    this set."""
    return {n for n, info in load_full(path).items() if info["is_lossless"]}


def assert_lossless_only(baselines: list[str], lossless: set[str], context: str) -> None:
    """Raise if `baselines` contains any quarantined (non-lossless) entry.
    For use at headline-table generation time."""
    quarantined = [b for b in baselines if b not in lossless]
    if quarantined:
        raise RuntimeError(
            f"{context}: cross-baseline aggregation includes quarantined "
            f"non-lossless baselines {quarantined!r}; remove them or move "
            f"the aggregation to appendix scope. See "
            f"docs/benchmark/fairness/bpe.md."
        )


def check_g7(name: str, declared_mode: str, table: dict[str, str]) -> None:
    if name not in table:
        raise RuntimeError(f"mode check: runner '{name}' not present in baseline_modes.yaml")
    expected = table[name]
    if declared_mode != expected:
        raise RuntimeError(
            f"mode check: runner '{name}' declared prefix_search_mode={declared_mode!r} "
            f"but baseline_modes.yaml says {expected!r}"
        )
