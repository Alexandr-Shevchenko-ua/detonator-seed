"""DS-002 real mutations: frozen §6 evaluator and search-only corpus loaders.

Corpus loading reads ``tests.json``, ``split.json`` (search mutant IDs only), and
``search-matrix.jsonl``. Kill lookup and per-test costs are derived from
``search-matrix.jsonl`` only. Holdout matrix and holdout-side scoring are never
loaded. Per-test clean cost ``d_t`` is the median ``duration_seconds`` for each
``node_id`` across all search-matrix rows (one row per search mutant × test).
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FORMULA_VERSION = "codex-0004-section-6-v1"
MISS_COST_MULTIPLIER = 2
BUDGET_FRACTION = 0.25
COMPOSITE_WEIGHTS: dict[str, float] = {
    "detection": 0.60,
    "mean_r": 0.15,
    "median_r": 0.15,
    "p90_r": 0.10,
}


def formula_fingerprint() -> str:
    """Stable hash of frozen §6 constants for snapshot tests."""
    payload = json.dumps(
        {
            "version": FORMULA_VERSION,
            "weights": COMPOSITE_WEIGHTS,
            "miss_cost_multiplier": MISS_COST_MULTIPLIER,
            "budget_fraction": BUDGET_FRACTION,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def prioritize(change: dict, tests: list[dict]) -> list[str]:
    """Reference policy: shortest ``baseline_duration_ms`` first (deterministic tie-break)."""
    _ = change
    return sorted(
        (t["id"] for t in tests),
        key=lambda tid: (
            next(item["baseline_duration_ms"] for item in tests if item["id"] == tid),
            tid,
        ),
    )


def validate_permutation(order: list[str], expected_ids: list[str]) -> bool:
    if len(order) != len(expected_ids):
        return False
    if len(order) != len(set(order)):
        return False
    return sorted(order) == sorted(expected_ids)


def total_clean_cost(cost_by_id: dict[str, float], test_ids: list[str]) -> float:
    return sum(cost_by_id[tid] for tid in test_ids)


def budget_cap(cost_by_id: dict[str, float], test_ids: list[str]) -> float:
    costs = [cost_by_id[tid] for tid in test_ids]
    c = sum(costs)
    return max(BUDGET_FRACTION * c, max(costs) if costs else 0.0)


def largest_prefix_within_budget(
    permutation: list[str],
    cost_by_id: dict[str, float],
    budget: float,
) -> list[str]:
    prefix: list[str] = []
    spent = 0.0
    for tid in permutation:
        cost = cost_by_id[tid]
        if spent + cost > budget:
            break
        prefix.append(tid)
        spent += cost
    return prefix


def nearest_rank_p90(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    n = len(ordered)
    rank = math.ceil(0.9 * n)
    index = max(0, min(n - 1, rank - 1))
    return ordered[index]


def mutant_metrics(
    prefix: list[str],
    killers: set[str],
    cost_by_id: dict[str, float],
    total_c: float,
) -> tuple[int, float]:
    """Return (detected_m, c_m) for one mutant."""
    if total_c <= 0:
        return 0, 0.0
    prefix_set = set(prefix)
    killing_in_prefix = killers & prefix_set
    if not killing_in_prefix:
        return 0, MISS_COST_MULTIPLIER * total_c
    detected = 1
    cumulative = 0.0
    for tid in prefix:
        cumulative += cost_by_id[tid]
        if tid in killers:
            return detected, cumulative
    return detected, MISS_COST_MULTIPLIER * total_c


def composite_from_r_values(r_values: list[float], detection_rate: float) -> float:
    if not r_values:
        return 0.0
    mean_r = sum(r_values) / len(r_values)
    median_r = statistics.median(r_values)
    p90_r = nearest_rank_p90(r_values)
    return (
        COMPOSITE_WEIGHTS["detection"] * detection_rate
        + COMPOSITE_WEIGHTS["mean_r"] * max(0.0, 1.0 - mean_r)
        + COMPOSITE_WEIGHTS["median_r"] * max(0.0, 1.0 - median_r)
        + COMPOSITE_WEIGHTS["p90_r"] * max(0.0, 1.0 - p90_r)
    )


def evaluate_permutation(
    permutation: list[str],
    test_ids: list[str],
    cost_by_id: dict[str, float],
    mutant_ids: list[str],
    kill_tests_by_mutant: dict[str, set[str]],
) -> float:
    """Frozen §6 composite score for one full candidate permutation."""
    if not validate_permutation(permutation, test_ids):
        return 0.0
    total_c = total_clean_cost(cost_by_id, test_ids)
    if total_c <= 0:
        return 0.0
    budget = budget_cap(cost_by_id, test_ids)
    prefix = largest_prefix_within_budget(permutation, cost_by_id, budget)
    detected: list[int] = []
    r_values: list[float] = []
    for mid in mutant_ids:
        killers = kill_tests_by_mutant.get(mid, set())
        det, c_m = mutant_metrics(prefix, killers, cost_by_id, total_c)
        detected.append(det)
        r_values.append(c_m / total_c)
    detection_rate = sum(detected) / len(detected) if detected else 0.0
    return composite_from_r_values(r_values, detection_rate)


@dataclass(frozen=True)
class SearchCorpus:
    test_ids: list[str]
    cost_by_id: dict[str, float]
    search_mutant_ids: list[str]
    kill_tests_by_mutant: dict[str, set[str]]

    def score_permutation(self, permutation: list[str]) -> float:
        return evaluate_permutation(
            permutation,
            self.test_ids,
            self.cost_by_id,
            self.search_mutant_ids,
            self.kill_tests_by_mutant,
        )


def _median_per_node_durations(matrix_path: Path) -> dict[str, float]:
    buckets: dict[str, list[float]] = {}
    with matrix_path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row.get("side") == "holdout":
                continue
            node_id = row["node_id"]
            buckets.setdefault(node_id, []).append(float(row["duration_seconds"]))
    return {node_id: statistics.median(durs) for node_id, durs in buckets.items()}


def _kill_lookup_from_search_matrix(matrix_path: Path) -> dict[str, set[str]]:
    killers: dict[str, set[str]] = {}
    with matrix_path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row.get("side") == "holdout":
                continue
            if row.get("outcome") != "killed":
                continue
            mid = row["mutmut_id"]
            killers.setdefault(mid, set()).add(row["node_id"])
    return killers


def load_search_corpus(corpus_dir: Path) -> SearchCorpus:
    """Load search-only evaluation artifacts from a verified DS-002 corpus directory."""
    corpus_dir = corpus_dir.resolve()
    tests_payload = json.loads((corpus_dir / "tests.json").read_text(encoding="utf-8"))
    split_payload = json.loads((corpus_dir / "split.json").read_text(encoding="utf-8"))
    test_ids = list(tests_payload["node_ids"])
    search_mutant_ids = list(split_payload["search"])

    matrix_path = corpus_dir / "search-matrix.jsonl"
    cost_by_id = _median_per_node_durations(matrix_path)
    missing = [tid for tid in test_ids if tid not in cost_by_id]
    if missing:
        raise ValueError(f"search-matrix missing durations for tests: {missing[:3]}")

    kill_lookup = _kill_lookup_from_search_matrix(matrix_path)
    kill_tests_by_mutant = {mid: kill_lookup.get(mid, set()) for mid in search_mutant_ids}

    return SearchCorpus(
        test_ids=test_ids,
        cost_by_id={tid: cost_by_id[tid] for tid in test_ids},
        search_mutant_ids=search_mutant_ids,
        kill_tests_by_mutant=kill_tests_by_mutant,
    )


def public_tests_from_corpus(corpus: SearchCorpus) -> list[dict[str, Any]]:
    """Build candidate-visible test payloads (search-only historical fields stubbed)."""
    tests: list[dict[str, Any]] = []
    for tid in corpus.test_ids:
        duration_ms = corpus.cost_by_id[tid] * 1000.0
        tests.append(
            {
                "id": tid,
                "test_file": tid.split("::", 1)[0],
                "test_name": tid.split("::", 1)[-1],
                "baseline_duration_ms": duration_ms,
                "dependency_hit": False,
                "history_kills": 0,
                "history_observations": 0,
                "historical_kill_rate": 0.0,
            }
        )
    return tests
