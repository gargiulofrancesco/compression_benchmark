from orchestrator.env_probe import build_flags_sha, probe, DEV_BUILD_FLAGS


def test_build_flags_sha_deterministic():
    a = build_flags_sha()
    b = build_flags_sha()
    assert a == b
    assert len(a) == 64


def test_build_flags_sha_order_invariant():
    a = build_flags_sha(DEV_BUILD_FLAGS)
    b = build_flags_sha(list(reversed(DEV_BUILD_FLAGS)))
    assert a == b


def test_build_flags_sha_sensitive_to_change():
    a = build_flags_sha(DEV_BUILD_FLAGS)
    b = build_flags_sha(DEV_BUILD_FLAGS + ["-funroll-loops"])
    assert a != b


def test_probe_runs():
    p = probe()
    assert p.machine_id and len(p.machine_id) == 16
    assert p.build_flags_sha == build_flags_sha()
    assert isinstance(p.m1_grade, bool)


def test_probe_fsst_fingerprint_fields():
    p = probe()
    assert isinstance(p.avx512f, bool)
    assert p.fsst_simd_path in ("avx512", "scalar")
    assert isinstance(p.fsst_simd_reason, str) and p.fsst_simd_reason
    # FSST can only be on the AVX-512 path when the CPU advertises AVX-512F.
    if not p.avx512f:
        assert p.fsst_simd_path == "scalar"
