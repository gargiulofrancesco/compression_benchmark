"""minbpe independent round-trip cross-check for the `bpe` baseline.

ROLE: minbpe is NOT a baseline and NOT on any measurement
path. It is a test-only, independent round-trip cross-check. The canonical BPE
baseline is SentencePiece BPE driven through the GPT-2 byte<->unicode bijection
(runners/bpe/); its PRIMARY correctness gate is the exhaustive 0x00..0xFF
bytewise round-trip fuzzer in tests/bpe_roundtrip_test.cpp. This cross-check is
SECONDARY, cheap defence-in-depth.

Why keep it: minbpe is a completely independent BPE implementation that operates
on RAW bytes and never touches the byte<->unicode mapping. Driving it on the same
tiny slice and asserting BOTH it and the `bpe` runner round-trip to the exact
input bytes catches a class of bug a SentencePiece *self*-round-trip cannot — e.g.
the byte<->unicode mapping applied inconsistently between encode and decode (which
could still self-cancel) — because minbpe reaches the same input bytes by a
different code path. It is explicitly NOT a bit-identical-token oracle:
SentencePiece's vocab and tie-breaks legitimately differ from minbpe's; only
bytewise round-trip equivalence is asserted.

Skips gracefully if minbpe (third_party/minbpe) is absent or the runner is unbuilt.
"""
from __future__ import annotations
import json
import os
import struct
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
RUNNER = REPO / "build" / "runners" / "bpe" / "bpe_runner"
MINBPE = REPO / "third_party" / "minbpe"

# A tiny slice exercising arbitrary bytes incl. the historically-hard ones
# (lone high bytes, NUL, newline) and an empty row.
SLICE = [
    b"http://example.com/path?q=1",
    bytes([0x00, 0x80, 0xFF, 0x0a, 0x41, 0x42]),
    b"",
    bytes(range(256)),
    b"repeated_repeated_repeated",
]


def _write_column(path: Path, rows: list[bytes]) -> None:
    total = sum(len(r) for r in rows)
    offsets = [0]
    for r in rows:
        offsets.append(offsets[-1] + len(r))
    with open(path, "wb") as f:
        f.write(struct.pack("<II", len(rows), total))
        f.write(struct.pack(f"<{len(offsets)}I", *offsets))
        f.write(b"".join(rows))


def _read_column(path: Path) -> list[bytes]:
    with open(path, "rb") as f:
        n, total = struct.unpack("<II", f.read(8))
        offsets = list(struct.unpack(f"<{n + 1}I", f.read((n + 1) * 4)))
        data = f.read(total)
    return [data[offsets[i]:offsets[i + 1]] for i in range(n)]


def _send(proc, obj):
    proc.stdin.write(json.dumps(obj) + "\n")
    proc.stdin.flush()


def _recv(proc):
    line = proc.stdout.readline()
    if not line:
        raise RuntimeError("runner closed stdout")
    return json.loads(line)


@pytest.mark.skipif(not RUNNER.exists(), reason="bpe_runner not built (run `make build`)")
def test_bpe_runner_roundtrip_crosscheck(tmp_path):
    """The `bpe` runner round-trips the slice byte-exactly (independent of the
    C++ fuzzer: driven here from Python via NDJSON)."""
    in_col = tmp_path / "in.col"
    z = tmp_path / "z.bin"
    rt = tmp_path / "rt.col"
    _write_column(in_col, SLICE)
    cfg = '{"vocab_size":4096,"sample_fraction":1.0,"threads":1,"mem_cap_gb":4}'
    proc = subprocess.Popen(
        [str(RUNNER)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True,
        env={**os.environ, "BASELINE_BUILD_FLAGS_SHA": "0" * 64})
    try:
        hello = _recv(proc)
        assert hello["name"] == "bpe"
        for op, inp, outp in [("compress", in_col, z), ("bulk_decode", z, rt)]:
            _send(proc, {"type": "op", "op": op, "cell_id": "x",
                         "input_path": str(inp), "output_path": str(outp),
                         "config_json": cfg})
            resp = _recv(proc)
            assert resp.get("status") == "ok", resp
        _send(proc, {"type": "bye"})
        proc.stdin.close()
        proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert _read_column(rt) == SLICE, "bpe runner round-trip is not byte-exact"


@pytest.mark.skipif(not (MINBPE / "minbpe" / "basic.py").exists(),
                    reason="third_party/minbpe absent")
def test_minbpe_independent_roundtrip():
    """Independent reference: minbpe (raw-byte BPE, no byte<->unicode mapping)
    round-trips the same slice byte-exactly. A divergence between this and the
    runner cross-check above flags a harness/mapping bug. Asserts round-trip
    equivalence only — NOT token identity (legitimately differs from SentencePiece)."""
    # Load base.py + basic.py directly: minbpe/__init__.py eagerly imports
    # regex.py (needs the third-party `regex` package, not a dependency here).
    # Register them under the `minbpe.*` names that basic.py's relative import
    # (`from .base import ...`) expects, without executing the package __init__.
    import importlib.util
    import types

    pkg = types.ModuleType("minbpe")
    pkg.__path__ = [str(MINBPE / "minbpe")]
    sys.modules["minbpe"] = pkg

    def _load(name):
        spec = importlib.util.spec_from_file_location(
            f"minbpe.{name}", MINBPE / "minbpe" / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"minbpe.{name}"] = mod
        spec.loader.exec_module(mod)
        return mod

    try:
        _load("base")
        BasicTokenizer = _load("basic").BasicTokenizer
    except Exception as e:  # pragma: no cover
        pytest.skip(f"minbpe basic.py unimportable: {e}")

    corpus = b"".join(SLICE)
    if not corpus:
        pytest.skip("empty corpus")
    tok = BasicTokenizer()
    # minbpe trains on a text str; latin-1 makes the raw bytes a 1:1 str.
    tok.train(corpus.decode("latin-1"), vocab_size=300, verbose=False)
    for row in SLICE:
        ids = tok.encode(row.decode("latin-1"))
        back = tok.decode(ids).encode("latin-1")
        assert back == row, "minbpe round-trip is not byte-exact"
