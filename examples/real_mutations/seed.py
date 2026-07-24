"""Executable seed policy selected as baseline_to_beat during DS-002 preflight."""

from __future__ import annotations

BASELINE_POLICY = "strong-human"


def _duration_ms(tests: list[dict], tid: str) -> float:
    return next(item["baseline_duration_ms"] for item in tests if item["id"] == tid)


def _dep_rank(tests: list[dict], tid: str) -> int:
    hit = next(item["dependency_hit"] for item in tests if item["id"] == tid)
    return 0 if hit else 1


def _hist_rate(tests: list[dict], tid: str) -> float:
    return next(item["historical_kill_rate"] for item in tests if item["id"] == tid)


def _risk_per_cost(tests: list[dict], tid: str) -> float:
    test = next(item for item in tests if item["id"] == tid)
    duration = max(test["baseline_duration_ms"], 1e-9)
    return test["historical_kill_rate"] / duration


def prioritize(change: dict, tests: list[dict]) -> list[str]:
    _ = change
    if BASELINE_POLICY == "shortest":
        return sorted((t["id"] for t in tests), key=lambda tid: (_duration_ms(tests, tid), tid))
    if BASELINE_POLICY == "dependency-first":
        return sorted(
            (t["id"] for t in tests),
            key=lambda tid: (_dep_rank(tests, tid), _duration_ms(tests, tid), tid),
        )
    if BASELINE_POLICY == "historical-kill":
        return sorted(
            (t["id"] for t in tests),
            key=lambda tid: (
                -_hist_rate(tests, tid),
                -next(item["history_observations"] for item in tests if item["id"] == tid),
                _duration_ms(tests, tid),
                tid,
            ),
        )
    if BASELINE_POLICY == "risk-per-cost":
        return sorted(
            (t["id"] for t in tests),
            key=lambda tid: (-_risk_per_cost(tests, tid), _dep_rank(tests, tid), tid),
        )
    if BASELINE_POLICY == "strong-human":
        return sorted(
            (t["id"] for t in tests),
            key=lambda tid: (
                _dep_rank(tests, tid),
                -_risk_per_cost(tests, tid),
                _duration_ms(tests, tid),
                tid,
            ),
        )
    raise ValueError(f"unknown baseline policy: {BASELINE_POLICY}")
