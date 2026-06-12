"""Fingerprint-strict rerun manifest (hardens; stub).

Records the inputs/seeds/flags so a second `make reproduce-all` run on the
same hardware can be diffed against the first to detect nondeterminism.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path


def manifest(run_id: str, env_dict: dict, cells: list[dict], extra: dict) -> dict:
    payload = {
        "run_id": run_id,
        "env": env_dict,
        "cells": cells,
        "extra": extra,
    }
    j = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    payload["manifest_sha"] = hashlib.sha256(j.encode()).hexdigest()
    return payload


def write_manifest(path: Path, m: dict) -> None:
    path.write_text(json.dumps(m, sort_keys=True, indent=2))
