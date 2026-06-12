import pytest

from orchestrator.modes import (
    load, check_g7, check_runner_name,
    load_full, lossless_baselines, assert_lossless_only,
    random_access_models,
)


def test_load_baseline_modes(tmp_path):
    p = tmp_path / "modes.yaml"
    p.write_text("""
baselines:
  - name: uncompressed
    prefix_search_mode: Uncompressed
""")
    t = load(p)
    assert t == {"uncompressed": "Uncompressed"}


def test_load_rejects_invalid_mode(tmp_path):
    p = tmp_path / "modes.yaml"
    p.write_text("""
baselines:
  - name: bogus
    prefix_search_mode: WeirdMode
""")
    with pytest.raises(ValueError):
        load(p)


def test_g7_pass():
    check_g7("uncompressed", "Uncompressed", {"uncompressed": "Uncompressed"})


def test_g7_pass_zstd():
    check_g7("zstd", "DecompressThenScan",
             {"uncompressed": "Uncompressed", "zstd": "DecompressThenScan"})


def test_g7_pass_lz4():
    check_g7("lz4", "DecompressThenScan",
             {"uncompressed": "Uncompressed",
              "zstd": "DecompressThenScan",
              "lz4": "DecompressThenScan"})


def test_g7_pass_fsst():
    check_g7("fsst", "DecompressThenScan",
             {"uncompressed": "Uncompressed",
              "zstd": "DecompressThenScan",
              "lz4": "DecompressThenScan",
              "fsst": "DecompressThenScan"})


def test_g7_pass_repair():
    # Re-Pair family is now represented by plain full-column `repair` (the dict
    # baselines libcsd_rpdac/pfc/rpfc were removed).
    check_g7("repair", "DecompressThenScan",
             {"uncompressed": "Uncompressed",
              "zstd": "DecompressThenScan",
              "lz4": "DecompressThenScan",
              "fsst": "DecompressThenScan",
              "repair": "DecompressThenScan"})


def test_g7_pass_bpe():
    check_g7("bpe", "DecompressThenScan",
             {"uncompressed": "Uncompressed",
              "zstd": "DecompressThenScan",
              "lz4": "DecompressThenScan",
              "fsst": "DecompressThenScan",
              "repair": "DecompressThenScan",
              "bpe": "DecompressThenScan"})


def test_g7_pass_rlz():
    check_g7("rlz", "DecompressThenScan",
             {"uncompressed": "Uncompressed",
              "zstd": "DecompressThenScan",
              "lz4": "DecompressThenScan",
              "fsst": "DecompressThenScan",
              "repair": "DecompressThenScan",
              "bpe": "DecompressThenScan",
              "rlz": "DecompressThenScan"})


def test_g7_pass_onpair():
    # OnPair is the ONLY NativeOnCompressed claimant (FLIPPED).
    check_g7("onpair", "NativeOnCompressed",
             {"uncompressed": "Uncompressed",
              "zstd": "DecompressThenScan",
              "lz4": "DecompressThenScan",
              "fsst": "DecompressThenScan",
              "repair": "DecompressThenScan",
              "bpe": "DecompressThenScan",
              "rlz": "DecompressThenScan",
              "onpair": "NativeOnCompressed"})


def test_load_baseline_modes_phase4_through_onpair():
    from pathlib import Path
    p = Path(__file__).resolve().parent.parent / "experiments" / "baseline_modes.yaml"
    t = load(p)
    assert t == {
        "uncompressed": "Uncompressed",
        "zstd": "DecompressThenScan",
        "zstd_l1": "DecompressThenScan",   # zstd level twin (ICDE T1/F1 sweep)
        "zstd_l9": "DecompressThenScan",   # zstd level twin (ICDE T1/F1 sweep)
        "lz4": "DecompressThenScan",
        "fsst": "DecompressThenScan",
        "fsst_scalar": "DecompressThenScan", # forced-scalar twin (no-AVX-512 FSST point)
        "repair": "DecompressThenScan",
        "bpe": "DecompressThenScan",       # canonical BPE row (SentencePiece+bytemap), V=2^14
        "bpe_v65536": "DecompressThenScan",# size-matched twin (V=2^16) vs OnPair bits=16
        "rlz": "DecompressThenScan",
        "onpair": "NativeOnCompressed",
    }


def test_random_access_model_truth_table():
    """The per-baseline random-access model (per_string | block | full_column)
    must match the runners' actual random_decode implementations — it is the
    determinant of L_ra fairness (§8.4) and the basis of the A5/A6 block-vs-flat
    split. block: zstd/lz4 decode the enclosing block; full_column: repair has no
    RA index (decode_all then slice); everything else is per-string."""
    from pathlib import Path
    p = Path(__file__).resolve().parent.parent / "experiments" / "baseline_modes.yaml"
    ra = random_access_models(p)
    assert ra == {
        "uncompressed": "per_string",
        "zstd": "block",
        "zstd_l1": "block",
        "zstd_l9": "block",
        "lz4": "block",
        "fsst": "per_string",
        "fsst_scalar": "per_string",
        "repair": "per_string",
        "bpe": "per_string",
        "bpe_v65536": "per_string",
        "rlz": "per_string",
        "onpair": "per_string",
    }
    # OnPair is the only NativeOnCompressed prefix baseline; per-string RA is
    # shared with FSST/BPE/RLZ (so it is not an OnPair-unique capability).
    full = load_full(p)
    assert full["onpair"]["prefix_search_mode"] == "NativeOnCompressed"
    native = [n for n, i in full.items()
              if i["prefix_search_mode"] == "NativeOnCompressed"]
    assert native == ["onpair"], f"OnPair must be the sole native-prefix baseline, got {native}"


def test_lossless_baselines_includes_bpe():
    from pathlib import Path
    p = Path(__file__).resolve().parent.parent / "experiments" / "baseline_modes.yaml"
    lossless = lossless_baselines(p)
    # Every current baseline is lossless. No baseline is quarantined.
    for b in ("uncompressed", "zstd", "lz4", "fsst", "fsst_scalar", "repair", "bpe", "rlz", "onpair"):
        assert b in lossless, f"{b} should be lossless"


def test_assert_lossless_only_raises_on_quarantined():
    # The guard machinery is retained even though no baseline is currently
    # quarantined: a fabricated non-lossless entry must still trip it.
    lossless = {"uncompressed", "zstd", "lz4", "fsst"}
    assert_lossless_only(["uncompressed", "fsst"], lossless, "test")  # clean passes
    with pytest.raises(RuntimeError) as excinfo:
        assert_lossless_only(
            ["uncompressed", "some-lossy-baseline"], lossless, "headline-T1")
    assert "some-lossy-baseline" in str(excinfo.value)
    assert "quarantined" in str(excinfo.value)


def test_load_full_carries_is_lossless():
    from pathlib import Path
    p = Path(__file__).resolve().parent.parent / "experiments" / "baseline_modes.yaml"
    full = load_full(p)
    assert full["bpe"]["is_lossless"] is True
    assert full["fsst"]["is_lossless"] is True
    assert full["uncompressed"]["is_lossless"] is True


def test_runner_alias_passthrough():
    # Default: no alias entry → expect cell_baseline == hello_name.
    check_runner_name("zstd", "zstd")
    check_runner_name("repair", "repair")


def test_runner_alias_bpe_v65536():
    # bpe_v65536 is served by the shared bpe runner → hello.name == "bpe".
    check_runner_name("bpe_v65536", "bpe")


def test_runner_alias_fsst_scalar():
    # fsst_scalar (forced-scalar, no-AVX-512 point) is served by the shared fsst
    # runner → hello.name == "fsst".
    check_runner_name("fsst_scalar", "fsst")


def test_runner_alias_mismatch():
    with pytest.raises(RuntimeError):
        check_runner_name("bpe_v65536", "rlz")   # expected "bpe", got "rlz"
    with pytest.raises(RuntimeError):
        check_runner_name("zstd", "lz4")


def test_g7_unknown_runner():
    with pytest.raises(RuntimeError):
        check_g7("ghost", "Uncompressed", {"uncompressed": "Uncompressed"})


def test_g7_mode_mismatch():
    with pytest.raises(RuntimeError):
        check_g7("uncompressed", "DecompressThenScan", {"uncompressed": "Uncompressed"})
