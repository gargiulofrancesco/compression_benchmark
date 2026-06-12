"""Spawn the bpe runner binary, handshake, drive one compress op end to end.
Asserts BPE bit-tight identity (M + ceil(T*B/8) + 16+4*(N+1) == output_bytes)
at V=4096 plus the wire-shape sanity checks the BPE card mandates
(structurally guaranteed natural dict/tokens split — sixth bit-tight baseline).
"""
from __future__ import annotations

import json
import struct
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
BUILD = REPO / "build"
RUNNER = BUILD / "runners" / "bpe" / "bpe_runner"


def _write_column(path: Path, rows: list[bytes]) -> None:
    n = len(rows)
    total = sum(len(r) for r in rows)
    offsets = [0]
    for r in rows:
        offsets.append(offsets[-1] + len(r))
    with open(path, "wb") as f:
        f.write(struct.pack("<II", n, total))
        f.write(struct.pack(f"<{n + 1}I", *offsets))
        for r in rows:
            f.write(r)


def _make_word_vocab_column(seed: int) -> list[bytes]:
    import random as _rnd
    _rnd.seed(seed)
    vocab = [
        b"GET", b"POST", b"PUT", b"DELETE", b"HTTP/1.1", b"200", b"404", b"500",
        b"/api/v1/", b"/users/", b"/orders/", b"/items/", b"/login", b"/logout",
        b"session=", b"token=", b"user_id=", b"order_id=", b"item_id=",
        b"created_at", b"updated_at", b"status:ok", b"status:err",
        b"INFO", b"WARN", b"ERROR", b"DEBUG",
        b"alice", b"bob", b"carol", b"dave", b"eve", b"frank",
        b" ", b" ", b" ", b"|", b":", b"-",
    ]
    rows = []
    for _ in range(500):
        n_tokens = _rnd.randint(8, 24)
        row = b" ".join(_rnd.choice(vocab) for _ in range(n_tokens))
        rows.append(row)
    return rows


@pytest.mark.skipif(not RUNNER.exists(), reason="bpe_runner not built (run `make build`)")
def test_bpe_handshake_and_compress(tmp_path: Path) -> None:
    sha = "test_" + "0" * 40
    env = {"BASELINE_BUILD_FLAGS_SHA": sha, "PATH": "/usr/bin:/bin"}
    proc = subprocess.Popen(
        [str(RUNNER)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1, env=env,
    )
    try:
        hello = json.loads(proc.stdout.readline())
        assert hello["type"] == "hello"
        assert hello["name"] == "bpe"
        assert hello["prefix_search_mode"] == "DecompressThenScan"
        assert hello["baseline_build_flags_sha"] == sha
        assert hello["version"].startswith("sentencepiece-v0.2.0")

        col = tmp_path / "in.col"
        rows = _make_word_vocab_column(seed=123)
        _write_column(col, rows)
        cz = tmp_path / "z.bin"
        cfg = json.dumps({
            "vocab_size": 4096,
            "sample_fraction": 1.0,
            "normalization": "identity",
            "threads": 1,
            "mem_cap_gb": 4,
        })
        op = {"type": "op", "op": "compress", "cell_id": "smoke",
              "input_path": str(col), "output_path": str(cz),
              "config_json": cfg}
        proc.stdin.write(json.dumps(op) + "\n")
        proc.stdin.flush()
        resp = json.loads(proc.stdout.readline())
        assert resp["type"] == "result"
        assert resp["status"] == "ok", f"compress failed: {resp}"

        metrics = {m["name"]: m["value"] for m in resp["metrics"]}
        N = 500

        # natural split — structurally guaranteed.
        # dict_bytes is the DECODE-MINIMAL model (raw id->bytes content + u32
        # offsets), symmetric with OnPair — NOT the full SentencePiece proto. The
        # proto is reported separately as model_proto_bytes (transparency); it is
        # >= the raw decode content it serialises (mapped pieces + scores + framing).
        assert metrics["dict_bytes"] > 0
        assert (metrics["dict_content_bytes"] + metrics["dict_offset_bytes"]
                == metrics["dict_bytes"])
        assert metrics["model_proto_bytes"] >= metrics["dict_content_bytes"]
        assert metrics["tokens_bytes"] > 0         # column non-empty
        # boundaries: 24 + 4*(N+1). Wire scalar overhead = M,N,V,T (16) +
        # the decoded_bytes u64 (8) carried so bulk_decode sizes its output buffer
        # exactly outside the timer; plus the per-row string offsets (N+1)*4.
        assert metrics["boundaries_bytes"] == 24 + 4 * (N + 1)
        assert metrics["num_rows"] == N
        # BIT-TIGHT identity on the ACCOUNTED (fair) compressed size: decode model +
        # tokens + boundaries == output_bytes. The literal on-disk file (wire_bytes)
        # is larger by the encoder-only proto overhead and is reported separately.
        assert (metrics["dict_bytes"]
                + metrics["tokens_bytes"]
                + metrics["boundaries_bytes"]) == metrics["output_bytes"]
        assert metrics["wire_bytes"] >= metrics["output_bytes"]

        proc.stdin.write(json.dumps({"type": "bye"}) + "\n")
        proc.stdin.flush()
        proc.stdin.close()
        proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
