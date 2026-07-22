"""Test-priority domain: loading, real evaluation, behavior, offline mutations."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import random
import textwrap
from pathlib import Path
from typing import Any


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_text(path.read_text(encoding="utf-8"))


def load_mission(mission_path: Path) -> dict[str, Any]:
    mission = json.loads(mission_path.read_text(encoding="utf-8"))
    base = mission_path.parent
    mission["_mission_path"] = mission_path
    mission["_base"] = base
    mission["_seed_path"] = (base / mission["seed"]).resolve()
    mission["_benchmark_path"] = (base / mission["benchmark"]).resolve()
    mission["_search_path"] = (base / mission["search_faults"]).resolve()
    mission["_holdout_path"] = (base / mission["holdout_faults"]).resolve()
    return mission


def load_benchmark_module(benchmark_path: Path, *, validate: str = "search"):
    """Load benchmark module.

    validate:
      - "search": run only search-visible integrity checks
      - "none": skip validation
      - "all": run search + holdout checks (tests/tools only)
    """
    # Purge before exec so a prior evolve's holdout module cannot be observed
    # while the next search-phase benchmark module is created.
    holdout_path = Path(benchmark_path).resolve().parent / "holdout_faults.py"
    purge_holdout_modules(holdout_path)

    spec = importlib.util.spec_from_file_location("tp_benchmark", benchmark_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load benchmark from {benchmark_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if validate == "search":
        module.validate_search_benchmark()
    elif validate == "all":
        module.validate_benchmark()
    elif validate != "none":
        raise ValueError(f"unknown validate mode: {validate}")
    return module


def purge_holdout_modules(holdout_module_path: Path) -> None:
    """Drop previously imported holdout definition modules from sys.modules."""
    import sys

    target = Path(holdout_module_path).resolve()
    to_drop = []
    for name, module in sys.modules.items():
        mod_file = getattr(module, "__file__", None)
        if mod_file and Path(mod_file).resolve() == target:
            to_drop.append(name)
    for name in to_drop:
        del sys.modules[name]


def load_fault_ids(path: Path) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return list(data["fault_ids"])


def public_requests(benchmark, fault_ids: list[str]) -> list[dict[str, Any]]:
    tests = benchmark.public_tests()
    return [
        {
            "key": fault_id,
            "change": benchmark.change_for_fault(fault_id),
            "tests": tests,
        }
        for fault_id in fault_ids
    ]


def validate_ordering(order: list[str], expected_ids: list[str]) -> str | None:
    if sorted(order) != sorted(expected_ids):
        missing = sorted(set(expected_ids) - set(order))
        extra = sorted(set(order) - set(expected_ids))
        if len(order) != len(set(order)):
            return "duplicate test ids"
        if missing or extra:
            return "ordering must be an exact permutation of test ids"
        return "ordering must be an exact permutation of test ids"
    if len(order) != len(set(order)):
        return "duplicate test ids"
    return None


def evaluate_orderings(
    benchmark,
    orderings: dict[str, list[str]],
    fault_ids: list[str],
) -> dict[str, Any]:
    expected = benchmark.test_ids()
    traces: list[dict[str, Any]] = []
    scores: list[float] = []
    for fault_id in fault_ids:
        order = orderings.get(fault_id)
        if order is None:
            return {
                "status": "invalid",
                "reason": f"missing ordering for {fault_id}",
                "score": None,
                "orderings": orderings,
                "fault_traces": traces,
                "behavior": None,
            }
        reason = validate_ordering(order, expected)
        if reason is not None:
            return {
                "status": "invalid",
                "reason": reason,
                "score": None,
                "orderings": orderings,
                "fault_traces": traces,
                "behavior": None,
            }
        trace = benchmark.run_suite_until_failure(order, fault_id)
        traces.append(trace)
        scores.append(float(trace["score"]))

    mean_score = sum(scores) / len(scores) if scores else 0.0
    behavior = compute_behavior(benchmark, orderings, fault_ids)
    return {
        "status": "valid",
        "reason": None,
        "score": mean_score,
        "orderings": orderings,
        "fault_traces": traces,
        "behavior": behavior,
    }


def compute_behavior(
    benchmark,
    orderings: dict[str, list[str]],
    fault_ids: list[str],
) -> dict[str, Any]:
    costs = benchmark.cost_by_id()
    tests_by_id = {t["id"]: t for t in benchmark.public_tests()}
    mean_all = sum(costs.values()) / len(costs)

    speed_ratios: list[float] = []
    change_focuses: list[float] = []
    for fault_id in fault_ids:
        order = orderings[fault_id]
        first3 = order[:3]
        speed_ratios.append((sum(costs[t] for t in first3) / 3) / mean_all)
        changed = set(benchmark.change_for_fault(fault_id)["changed_symbols"])
        focused = 0
        for tid in first3:
            covers = set(tests_by_id[tid].get("covers") or [])
            if covers & changed:
                focused += 1
        change_focuses.append(focused / 3)

    speed_ratio = sum(speed_ratios) / len(speed_ratios)
    change_focus = sum(change_focuses) / len(change_focuses)
    speed_bin = "fast" if speed_ratio < 0.75 else "cost_tolerant"
    change_bin = "change_focused" if change_focus >= (2 / 3) else "general"
    return {
        "speed_ratio": speed_ratio,
        "change_focus": change_focus,
        "cell": [speed_bin, change_bin],
    }


# ---------------------------------------------------------------------------
# Offline mutations (commit 2+)
# ---------------------------------------------------------------------------

POLICY_FAMILIES = (
    "shortest_first",
    "historical_risk_first",
    "change_coverage_first",
    "risk_per_cost",
    "weighted_hybrid",
    "coverage_diverse",
)


def render_policy_source(
    family: str,
    *,
    change_weight: float = 1.0,
    risk_weight: float = 1.0,
    cost_weight: float = 1.0,
    tie_breaker: str = "id",
) -> str:
    """Render a concrete prioritize() implementation."""
    cw = float(change_weight)
    rw = float(risk_weight)
    cost_w = float(cost_weight)
    tb = tie_breaker

    if family == "shortest_first":
        sort_key = f"""
    def sort_key(test):
        covers = set(test.get("covers") or [])
        covers_change = 0 if covers & changed else 1
        tie = test["id"] if {tb!r} == "id" else -float(test.get("historical_failure_rate") or 0.0)
        return (int(test["cost_units"]) * {cost_w!r}, covers_change * {cw!r}, tie)
"""
        return _render_sorted_policy(family, sort_key)

    if family == "historical_risk_first":
        sort_key = f"""
    def sort_key(test):
        covers = set(test.get("covers") or [])
        covers_change = 0 if covers & changed else 1
        risk = float(test.get("historical_failure_rate") or 0.0)
        tie = test["id"] if {tb!r} == "id" else int(test["cost_units"])
        return (-risk * {rw!r}, covers_change * {cw!r}, int(test["cost_units"]) * {cost_w!r}, tie)
"""
        return _render_sorted_policy(family, sort_key)

    if family == "change_coverage_first":
        sort_key = f"""
    def sort_key(test):
        covers = set(test.get("covers") or [])
        covers_change = 0 if covers & changed else 1
        risk = float(test.get("historical_failure_rate") or 0.0)
        tie = test["id"] if {tb!r} == "id" else -risk
        return (covers_change, int(test["cost_units"]) * {cost_w!r}, -risk * {rw!r}, tie)
"""
        return _render_sorted_policy(family, sort_key)

    if family == "risk_per_cost":
        sort_key = f"""
    def sort_key(test):
        covers = set(test.get("covers") or [])
        covers_change = 0 if covers & changed else 1
        risk = float(test.get("historical_failure_rate") or 0.0)
        cost = max(int(test["cost_units"]), 1)
        rpc = risk / cost
        tie = test["id"] if {tb!r} == "id" else covers_change
        return (-rpc * {rw!r}, covers_change * {cw!r}, cost * {cost_w!r}, tie)
"""
        return _render_sorted_policy(family, sort_key)

    if family == "weighted_hybrid":
        sort_key = f"""
    def sort_key(test):
        covers = set(test.get("covers") or [])
        covers_change = 1.0 if covers & changed else 0.0
        risk = float(test.get("historical_failure_rate") or 0.0)
        cost = max(int(test["cost_units"]), 1)
        score = (
            covers_change * {cw!r}
            + risk * {rw!r}
            - math.log(cost) * {cost_w!r}
        )
        tie = test["id"] if {tb!r} == "id" else -risk
        return (-score, tie)
"""
        return _render_sorted_policy(family, sort_key)

    if family == "coverage_diverse":
        return textwrap.dedent(
            f'''\
            """Offline-generated prioritization policy ({family})."""


            def prioritize(change: dict, tests: list[dict]) -> list[str]:
                changed = set(change.get("changed_symbols") or [])
                remaining = list(tests)
                ordered = []
                seen_symbols = set()
                while remaining:
                    def greedy_key(test):
                        covers = set(test.get("covers") or [])
                        novel = 0 if (covers - seen_symbols) else 1
                        covers_change = 0 if covers & changed else 1
                        risk = float(test.get("historical_failure_rate") or 0.0)
                        cost = int(test["cost_units"])
                        tie = test["id"] if {tb!r} == "id" else -risk
                        return (
                            novel,
                            covers_change,
                            -risk * {rw!r},
                            cost * {cost_w!r},
                            tie,
                        )
                    remaining.sort(key=greedy_key)
                    chosen = remaining.pop(0)
                    ordered.append(chosen["id"])
                    seen_symbols.update(chosen.get("covers") or [])
                return ordered
            '''
        )

    raise ValueError(f"unknown policy family: {family}")


def _render_sorted_policy(family: str, sort_key_block: str) -> str:
    body = textwrap.indent(textwrap.dedent(sort_key_block).strip("\n"), "    ")
    return (
        f'"""Offline-generated prioritization policy ({family})."""\n'
        "\n"
        "import math\n"
        "\n"
        "\n"
        "def prioritize(change: dict, tests: list[dict]) -> list[str]:\n"
        '    changed = set(change.get("changed_symbols") or [])\n'
        f"{body}\n"
        "    ordered = sorted(tests, key=sort_key)\n"
        '    return [t["id"] for t in ordered]\n'
    )


def render_invalid_source() -> str:
    return textwrap.dedent(
        '''\
        """Intentionally contract-invalid prioritization policy."""

        def prioritize(change: dict, tests: list[dict]) -> list[str]:
            # Duplicate the first id so the ordering is not a permutation.
            ids = [t["id"] for t in tests]
            if not ids:
                return ids
            return [ids[0], *ids]
        '''
    )


def render_crash_source() -> str:
    return textwrap.dedent(
        '''\
        """Intentionally crashing prioritization policy."""

        def prioritize(change: dict, tests: list[dict]) -> list[str]:
            raise RuntimeError("intentional candidate crash")
        '''
    )


def render_timeout_source() -> str:
    return textwrap.dedent(
        '''\
        """Intentionally timing-out prioritization policy."""

        import time

        def prioritize(change: dict, tests: list[dict]) -> list[str]:
            time.sleep(30)
            return [t["id"] for t in tests]
        '''
    )


def mutate_params(rng: random.Random, family: str, parent_meta: dict[str, Any] | None) -> dict[str, Any]:
    base = {
        "family": family,
        "change_weight": 1.0,
        "risk_weight": 1.0,
        "cost_weight": 1.0,
        "tie_breaker": "id",
    }
    if parent_meta:
        base.update({k: parent_meta[k] for k in base if k in parent_meta})
        base["family"] = parent_meta.get("family", family)

    operator = rng.choice(["adjust_weight", "switch_family", "flip_tie_breaker", "nudge_operator"])
    description = ""
    if operator == "adjust_weight":
        key = rng.choice(["change_weight", "risk_weight", "cost_weight"])
        factor = rng.choice([0.5, 0.75, 1.25, 1.5, 2.0])
        base[key] = float(base[key]) * factor
        description = f"scale {key} by {factor}"
    elif operator == "switch_family":
        choices = [f for f in POLICY_FAMILIES if f != base["family"]]
        base["family"] = rng.choice(choices)
        description = f"switch family to {base['family']}"
    elif operator == "flip_tie_breaker":
        base["tie_breaker"] = "risk" if base.get("tie_breaker") == "id" else "id"
        description = f"tie-breaker={base['tie_breaker']}"
    else:
        base["change_weight"] = float(base["change_weight"]) + rng.choice([-0.25, 0.25, 0.5])
        base["risk_weight"] = max(0.1, float(base["risk_weight"]) + rng.choice([-0.25, 0.25]))
        description = "nudge change/risk weights"

    base["family"] = base.get("family", family)
    source = render_policy_source(
        base["family"],
        change_weight=base["change_weight"],
        risk_weight=base["risk_weight"],
        cost_weight=base["cost_weight"],
        tie_breaker=base["tie_breaker"],
    )
    return {
        "source": source,
        "operator": operator,
        "description": description,
        "meta": base,
    }


def generate_offline_descendant(
    proposal_index: int,
    variation_seed: int,
    parent: dict[str, Any] | None,
    occupied_cells: list[list[str]],
) -> dict[str, Any]:
    """Return a descendant source with mutation metadata.

    Schedule guarantees (within budget 24):
    - proposal 5 → contract-invalid
    - proposal 11 → crash
    - proposal 17 → timeout
    - remaining → unique policy variants across families

    Lineage modes:
    - ``seed``: source does not depend on a selected archive parent
    - ``derived``: source/parameters are mutated from the provided parent
    """
    rng = random.Random(variation_seed * 1_000_003 + proposal_index * 97)

    if proposal_index == 5:
        return {
            "source": render_invalid_source(),
            "provider": "offline",
            "operator": "inject_invalid",
            "description": "duplicate test ids",
            "meta": {"family": "invalid"},
            "lineage_mode": "seed",
            "injected_probe": "invalid",
        }
    if proposal_index == 11:
        return {
            "source": render_crash_source(),
            "provider": "offline",
            "operator": "inject_crash",
            "description": "raise during prioritize",
            "meta": {"family": "crash"},
            "lineage_mode": "seed",
            "injected_probe": "crash",
        }
    if proposal_index == 17:
        return {
            "source": render_timeout_source(),
            "provider": "offline",
            "operator": "inject_timeout",
            "description": "sleep beyond timeout",
            "meta": {"family": "timeout"},
            "lineage_mode": "seed",
            "injected_probe": "timeout",
        }

    # Exploration: cover policy families and all four behavior cells.
    exploration = [
        ("shortest_first", {"change_weight": 0.0, "risk_weight": 0.0, "cost_weight": 1.0}),
        # Expensive-first, change-agnostic → cost_tolerant/general.
        ("shortest_first", {"change_weight": 0.0, "risk_weight": 0.0, "cost_weight": -1.0}),
        ("change_coverage_first", {"change_weight": 2.0, "risk_weight": 0.5, "cost_weight": 1.0}),
        ("risk_per_cost", {"change_weight": 1.0, "risk_weight": 2.0, "cost_weight": 1.0}),
        ("weighted_hybrid", {"change_weight": 1.5, "risk_weight": 1.0, "cost_weight": 0.8}),
        ("coverage_diverse", {"change_weight": 1.0, "risk_weight": 1.0, "cost_weight": 1.0}),
        ("historical_risk_first", {"change_weight": 0.0, "risk_weight": 2.0, "cost_weight": 0.5}),
    ]

    # Map proposal index to valid exploration/mutation slots, skipping reserved.
    reserved = {5, 11, 17}
    valid_slots = [i for i in range(24) if i not in reserved]
    slot_pos = valid_slots.index(proposal_index) if proposal_index in valid_slots else proposal_index

    parent_meta = None
    if parent is not None:
        parent_meta = (parent.get("mutation") or {}).get("meta")

    if slot_pos < len(exploration):
        family, params = exploration[slot_pos]
        source = render_policy_source(family, **params, tie_breaker="id")
        return {
            "source": source,
            "provider": "offline",
            "operator": "seed_family",
            "description": f"explore {family}",
            "meta": {"family": family, "tie_breaker": "id", **params},
            "lineage_mode": "seed",
            "injected_probe": None,
        }

    # Later slots: mutate from parent when available, else seed-rooted families.
    if parent is not None and parent_meta and parent_meta.get("family") in POLICY_FAMILIES:
        mutation = mutate_params(rng, parent_meta["family"], parent_meta)
        mutation["provider"] = "offline"
        mutation["lineage_mode"] = "derived"
        mutation["injected_probe"] = None
        return mutation

    family = POLICY_FAMILIES[slot_pos % len(POLICY_FAMILIES)]
    mutation = mutate_params(rng, family, None)
    mutation["provider"] = "offline"
    mutation["lineage_mode"] = "seed"
    mutation["injected_probe"] = None
    return mutation


def cell_key(cell: list[str] | tuple[str, str] | None) -> tuple[str, str] | None:
    if cell is None:
        return None
    return (cell[0], cell[1])


ARCHIVE_CELLS: list[tuple[str, str]] = [
    ("fast", "change_focused"),
    ("fast", "general"),
    ("cost_tolerant", "change_focused"),
    ("cost_tolerant", "general"),
]


class HoldoutGate:
    """Procedural gate: holdout bytes are readable only after archive freeze."""

    def __init__(self, path: Path):
        self.path = path
        self._frozen = False
        self.read_count = 0

    def mark_frozen(self) -> None:
        self._frozen = True

    @property
    def is_frozen(self) -> bool:
        return self._frozen

    def load_fault_ids(self) -> list[str]:
        if not self._frozen:
            raise RuntimeError("holdout must not be loaded before archive freeze")
        self.read_count += 1
        return load_fault_ids(self.path)
