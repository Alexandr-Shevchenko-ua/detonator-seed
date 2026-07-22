"""Disposable candidate worker.

Loads a candidate module in a temporary cwd and invokes ``prioritize`` for each
request. Speaks a tiny JSON protocol on stdin/stdout.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import traceback
from pathlib import Path
from typing import Any


def _load_prioritize(source_path: Path):
    spec = importlib.util.spec_from_file_location("candidate_mod", source_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load candidate from {source_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    prioritize = getattr(module, "prioritize", None)
    if prioritize is None or not callable(prioritize):
        raise RuntimeError("candidate must define callable prioritize(change, tests)")
    return prioritize


def _run(payload: dict[str, Any]) -> dict[str, Any]:
    source_path = Path(payload["source_path"])
    prioritize = _load_prioritize(source_path)
    orderings: dict[str, list[str]] = {}
    for request in payload["requests"]:
        key = request["key"]
        change = request["change"]
        tests = request["tests"]
        result = prioritize(change, tests)
        if not isinstance(result, list) or not all(isinstance(x, str) for x in result):
            raise RuntimeError(f"prioritize must return list[str], got {type(result)!r}")
        orderings[key] = result
    return {"orderings": orderings}


def main() -> None:
    try:
        payload = json.load(sys.stdin)
        result = _run(payload)
        json.dump(result, sys.stdout)
        sys.stdout.write("\n")
    except Exception as exc:  # noqa: BLE001 — surface any candidate failure
        sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
