from orchestrator.status import classify, REFUSED_ENV_EXIT


def test_ok():
    assert classify(0, None, 100, 1.0, 4 << 30, 60.0, None) == "ok"


def test_crash_exit():
    assert classify(1, None, 0, 1.0, 4 << 30, 60.0, None) == "crash"


def test_crash_signal():
    assert classify(0, 11, 0, 1.0, 4 << 30, 60.0, None) == "crash"


def test_oom_signal_with_high_rss():
    cap = 4 << 30
    assert classify(0, 9, int(0.95 * cap), 1.0, cap, 60.0, None) == "oom"


def test_sigkill_low_rss_is_crash_not_oom():
    cap = 4 << 30
    assert classify(0, 9, 1024, 1.0, cap, 60.0, None) == "crash"


def test_timeout():
    assert classify(0, None, 0, 120.0, 4 << 30, 60.0, None) == "timeout"


def test_refused_env():
    assert classify(REFUSED_ENV_EXIT, None, 0, 1.0, 4 << 30, 60.0, None) == "refused-env"


def test_declared_unsupported_propagates():
    assert classify(0, None, 0, 1.0, 4 << 30, 60.0, "unsupported") == "unsupported"
