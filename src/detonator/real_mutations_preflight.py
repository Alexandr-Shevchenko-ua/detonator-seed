"""DS-002 preflight: testmon masks, baselines, bounded oracle, headroom gates."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from detonator.mutation_corpus import (
    create_detached_worktree,
    load_mission,
    remove_worktree,
    sha256_file,
    sha256_text,
    _ensure_mutmut_config,
    _materialize_mutant_on_worktree,
    _mutmut_generate,
    _prepare_matrix_worktree,
)
from detonator.real_mutations import (
    composite_from_r_values,
    formula_fingerprint,
    largest_prefix_within_budget,
    load_search_corpus,
    mutant_metrics,
    total_clean_cost,
    budget_cap,
    validate_permutation,
    SearchCorpus,
)

ORACLE_WEIGHTS = (0.0, 0.5, 1.0, 2.0, 4.0)
BASELINE_NAMES: tuple[str, ...] = (
    "shortest",
    "dependency-first",
    "historical-kill",
    "risk-per-cost",
    "strong-human",
)

_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def enumerate_oracle_weight_combos() -> list[tuple[float, float, float, float]]:
    combos: list[tuple[float, float, float, float]] = []
    for w_dep in ORACLE_WEIGHTS:
        for w_hist in ORACLE_WEIGHTS:
            for w_cost in ORACLE_WEIGHTS:
                for w_name in ORACLE_WEIGHTS:
                    if (w_dep, w_hist, w_cost, w_name) == (0.0, 0.0, 0.0, 0.0):
                        continue
                    combos.append((w_dep, w_hist, w_cost, w_name))
    return combos


def smoothed_kill_rate(kills: int, observations: int) -> float:
    return (kills + 1) / (observations + 2)


def _tokens_from_patch(patch_text: str) -> set[str]:
    tokens: set[str] = set()
    for line in patch_text.splitlines():
        if not line or line.startswith(("---", "+++", "@@")):
            continue
        if line[0] in "+-":
            for match in _IDENTIFIER_RE.findall(line[1:]):
                if match not in {"if", "else", "return", "True", "False", "None"}:
                    tokens.add(match.lower())
    return tokens


def _name_overlap(change: dict[str, Any], test: dict[str, Any]) -> float:
    change_tokens = set(change.get("identifier_tokens") or [])
    if not change_tokens:
        return 0.0
    test_blob = f"{test.get('test_file', '')} {test.get('test_name', '')}".lower()
    test_tokens = set(_IDENTIFIER_RE.findall(test_blob))
    if not test_tokens:
        return 0.0
    return len(change_tokens & test_tokens) / len(change_tokens)


def build_change_record(mutant_row: dict[str, Any], patch_text: str) -> dict[str, Any]:
    added = sum(1 for line in patch_text.splitlines() if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in patch_text.splitlines() if line.startswith("-") and not line.startswith("---"))
    return {
        "path": mutant_row.get("path", ""),
        "qualified_symbol": mutant_row.get("qualified_symbol", ""),
        "changed_line_ranges": [],
        "changed_line_count": added + removed,
        "identifier_tokens": sorted(_tokens_from_patch(patch_text)),
    }


def load_search_mutant_rows(corpus_dir: Path) -> dict[str, dict[str, Any]]:
    split = json.loads((corpus_dir / "split.json").read_text(encoding="utf-8"))
    search_ids = set(split["search"])
    rows: dict[str, dict[str, Any]] = {}
    with (corpus_dir / "mutants.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            if row.get("mutmut_id") in search_ids:
                rows[row["mutmut_id"]] = row
    if set(rows) != search_ids:
        missing = search_ids - set(rows)
        raise ValueError(f"mutants.jsonl missing search ids: {sorted(missing)[:5]}")
    return rows


def load_patch_text(corpus_dir: Path, patch_sha256: str) -> str:
    path = corpus_dir / "patches" / f"{patch_sha256}.diff"
    return path.read_text(encoding="utf-8")


def loo_history_tables(
    corpus: SearchCorpus,
) -> tuple[dict[str, dict[str, int]], dict[str, dict[str, int]]]:
    """Per-mutant LOO kill/observation counts per test node (search mutants only)."""
    kills: dict[str, dict[str, int]] = {mid: {} for mid in corpus.search_mutant_ids}
    obs: dict[str, dict[str, int]] = {mid: {} for mid in corpus.search_mutant_ids}
    for mid in corpus.search_mutant_ids:
        killers = corpus.kill_tests_by_mutant.get(mid, set())
        for tid in corpus.test_ids:
            obs[mid][tid] = len(corpus.search_mutant_ids) - 1
            other_kills = sum(
                1
                for other in corpus.search_mutant_ids
                if other != mid and tid in corpus.kill_tests_by_mutant.get(other, set())
            )
            kills[mid][tid] = other_kills
    return kills, obs


def build_tests_for_mutant(
    corpus: SearchCorpus,
    mutant_id: str,
    dependency_hits: set[str],
    loo_kills: dict[str, int],
    loo_obs: dict[str, int],
) -> list[dict[str, Any]]:
    tests: list[dict[str, Any]] = []
    for tid in corpus.test_ids:
        duration_ms = corpus.cost_by_id[tid] * 1000.0
        hk = loo_kills.get(tid, 0)
        ho = loo_obs.get(tid, 0)
        tests.append(
            {
                "id": tid,
                "test_file": tid.split("::", 1)[0],
                "test_name": tid.split("::", 1)[-1],
                "baseline_duration_ms": duration_ms,
                "dependency_hit": tid in dependency_hits,
                "history_kills": hk,
                "history_observations": ho,
                "historical_kill_rate": smoothed_kill_rate(hk, ho),
            }
        )
    return tests


def order_shortest(tests: list[dict[str, Any]], change: dict[str, Any]) -> list[str]:
    _ = change
    return sorted((t["id"] for t in tests), key=lambda tid: (_duration_key(tests, tid), tid))


def order_dependency_first(tests: list[dict[str, Any]], change: dict[str, Any]) -> list[str]:
    _ = change
    return sorted(
        (t["id"] for t in tests),
        key=lambda tid: (_dep_key(tests, tid), _duration_key(tests, tid), tid),
    )


def order_historical_kill(tests: list[dict[str, Any]], change: dict[str, Any]) -> list[str]:
    _ = change
    return sorted(
        (t["id"] for t in tests),
        key=lambda tid: (
            -_hist_rate(tests, tid),
            -_hist_support(tests, tid),
            _duration_key(tests, tid),
            tid,
        ),
    )


def order_risk_per_cost(tests: list[dict[str, Any]], change: dict[str, Any]) -> list[str]:
    _ = change
    return sorted(
        (t["id"] for t in tests),
        key=lambda tid: (
            -_risk_per_cost(tests, tid),
            _dep_key(tests, tid),
            tid,
        ),
    )


def order_strong_human(tests: list[dict[str, Any]], change: dict[str, Any]) -> list[str]:
    _ = change
    return sorted(
        (t["id"] for t in tests),
        key=lambda tid: (
            _dep_key(tests, tid),
            -_risk_per_cost(tests, tid),
            _duration_key(tests, tid),
            tid,
        ),
    )


BASELINE_ORDER_FNS: dict[str, Callable[[list[dict[str, Any]], dict[str, Any]], list[str]]] = {
    "shortest": order_shortest,
    "dependency-first": order_dependency_first,
    "historical-kill": order_historical_kill,
    "risk-per-cost": order_risk_per_cost,
    "strong-human": order_strong_human,
}


def _duration_key(tests: list[dict[str, Any]], tid: str) -> float:
    return next(t["baseline_duration_ms"] for t in tests if t["id"] == tid)


def _dep_key(tests: list[dict[str, Any]], tid: str) -> int:
    return 0 if next(t["dependency_hit"] for t in tests if t["id"] == tid) else 1


def _hist_rate(tests: list[dict[str, Any]], tid: str) -> float:
    return next(t["historical_kill_rate"] for t in tests if t["id"] == tid)


def _hist_support(tests: list[dict[str, Any]], tid: str) -> int:
    return next(t["history_observations"] for t in tests if t["id"] == tid)


def _risk_per_cost(tests: list[dict[str, Any]], tid: str) -> float:
    test = next(t for t in tests if t["id"] == tid)
    duration = max(test["baseline_duration_ms"], 1e-9)
    return test["historical_kill_rate"] / duration


def order_oracle(
    tests: list[dict[str, Any]],
    change: dict[str, Any],
    weights: tuple[float, float, float, float],
) -> list[str]:
    w_dep, w_hist, w_cost, w_name = weights
    max_dur = max((t["baseline_duration_ms"] for t in tests), default=1.0)

    def sort_key(tid: str) -> tuple[float, str]:
        test = next(item for item in tests if item["id"] == tid)
        dep = 1.0 if test["dependency_hit"] else 0.0
        norm_cost = test["baseline_duration_ms"] / max_dur
        overlap = _name_overlap(change, test)
        score = (
            w_dep * dep
            + w_hist * test["historical_kill_rate"]
            - w_cost * norm_cost
            + w_name * overlap
        )
        return (-score, tid)

    return sorted((t["id"] for t in tests), key=sort_key)


@dataclass(frozen=True)
class PolicyMetrics:
    composite: float
    detection_rate: float
    median_kill_cost: float
    ordering_tuple: tuple[str, ...]


def evaluate_per_mutant_permutations(
    corpus: SearchCorpus,
    perms_by_mutant: dict[str, list[str]],
) -> PolicyMetrics:
    test_ids = corpus.test_ids
    cost_by_id = corpus.cost_by_id
    total_c = total_clean_cost(cost_by_id, test_ids)
    budget = budget_cap(cost_by_id, test_ids)
    detected: list[int] = []
    r_values: list[float] = []
    kill_costs: list[float] = []
    order_key: list[str] = []
    for mid in corpus.search_mutant_ids:
        perm = perms_by_mutant[mid]
        if not validate_permutation(perm, test_ids):
            return PolicyMetrics(0.0, 0.0, 0.0, tuple())
        prefix = largest_prefix_within_budget(perm, cost_by_id, budget)
        killers = corpus.kill_tests_by_mutant.get(mid, set())
        det, c_m = mutant_metrics(prefix, killers, cost_by_id, total_c)
        detected.append(det)
        r_values.append(c_m / total_c if total_c > 0 else 0.0)
        if det:
            kill_costs.append(c_m)
        order_key.append(",".join(perm))
    detection_rate = sum(detected) / len(detected) if detected else 0.0
    composite = composite_from_r_values(r_values, detection_rate)
    median_kill = sorted(kill_costs)[len(kill_costs) // 2] if kill_costs else 0.0
    # Representative ordering fingerprint: mode across mutants (for diversity counting)
    ordering_tuple = tuple(perms_by_mutant[corpus.search_mutant_ids[0]])
    return PolicyMetrics(composite, detection_rate, median_kill, ordering_tuple)


def evaluate_single_permutation(corpus: SearchCorpus, permutation: list[str]) -> PolicyMetrics:
    perms = {mid: permutation for mid in corpus.search_mutant_ids}
    return evaluate_per_mutant_permutations(corpus, perms)


def baseline_policy_hash(name: str, corpus: SearchCorpus) -> str:
    payload = json.dumps(
        {
            "name": name,
            "formula_fingerprint": formula_fingerprint(),
            "test_ids": corpus.test_ids,
            "search_mutants": corpus.search_mutant_ids,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_per_mutant_context(
    corpus: SearchCorpus,
    mutant_rows: dict[str, dict[str, Any]],
    corpus_dir: Path,
    dependency_masks: dict[str, set[str]],
) -> dict[str, tuple[dict[str, Any], list[dict[str, Any]]]]:
    loo_kills, loo_obs = loo_history_tables(corpus)
    out: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
    for mid in corpus.search_mutant_ids:
        row = mutant_rows[mid]
        patch = load_patch_text(corpus_dir, row["patch_sha256"])
        change = build_change_record(row, patch)
        deps = dependency_masks.get(mid, set())
        tests = build_tests_for_mutant(corpus, mid, deps, loo_kills[mid], loo_obs[mid])
        out[mid] = (change, tests)
    return out


def score_baseline(
    name: str,
    corpus: SearchCorpus,
    contexts: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]],
) -> PolicyMetrics:
    order_fn = BASELINE_ORDER_FNS[name]
    if name == "shortest":
        _, tests0 = contexts[corpus.search_mutant_ids[0]]
        perm = order_fn(tests0, {})
        return evaluate_single_permutation(corpus, perm)
    perms: dict[str, list[str]] = {}
    for mid in corpus.search_mutant_ids:
        change, tests = contexts[mid]
        perms[mid] = order_fn(tests, change)
    return evaluate_per_mutant_permutations(corpus, perms)


def score_oracle(
    corpus: SearchCorpus,
    contexts: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]],
) -> tuple[PolicyMetrics, tuple[float, float, float, float]]:
    best_metrics = PolicyMetrics(0.0, 0.0, 0.0, tuple())
    best_weights = (0.0, 0.0, 0.0, 0.0)
    for weights in enumerate_oracle_weight_combos():
        perms = {
            mid: order_oracle(tests, change, weights)
            for mid, (change, tests) in contexts.items()
        }
        metrics = evaluate_per_mutant_permutations(corpus, perms)
        if metrics.composite > best_metrics.composite:
            best_metrics = metrics
            best_weights = weights
    return best_metrics, best_weights


def baseline_ordering_map(
    name: str,
    corpus: SearchCorpus,
    contexts: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]],
) -> tuple[tuple[str, ...], ...]:
    order_fn = BASELINE_ORDER_FNS[name]
    if name == "shortest":
        _, tests0 = contexts[corpus.search_mutant_ids[0]]
        perm = order_fn(tests0, {})
        return (tuple(perm),)
    return tuple(
        tuple(order_fn(contexts[mid][1], contexts[mid][0])) for mid in corpus.search_mutant_ids
    )


def pareto_nondominated_baselines(
    baseline_metrics: dict[str, PolicyMetrics],
) -> list[str]:
    names = list(BASELINE_NAMES)
    nondom: list[str] = []
    for name in names:
        m = baseline_metrics[name]
        dominated = False
        for other in names:
            if other == name:
                continue
            o = baseline_metrics[other]
            if o.detection_rate >= m.detection_rate and o.median_kill_cost <= m.median_kill_cost:
                if o.detection_rate > m.detection_rate or o.median_kill_cost < m.median_kill_cost:
                    dominated = True
                    break
        if not dominated:
            nondom.append(name)
    return nondom


def compute_headroom(
    baseline_metrics: dict[str, PolicyMetrics],
    baseline_to_beat: str,
    oracle_metrics: PolicyMetrics,
    ordering_map_counts: dict[str, int],
) -> dict[str, Any]:
    beat = baseline_metrics[baseline_to_beat]
    composites = [baseline_metrics[n].composite for n in BASELINE_NAMES]
    best_b = max(composites)
    worst_b = min(composites)
    spread = best_b - worst_b
    distinct_maps = ordering_map_counts.get("distinct", 0)
    pareto = pareto_nondominated_baselines(baseline_metrics)
    rel_advantage = (
        (oracle_metrics.composite - beat.composite) / beat.composite if beat.composite > 0 else 0.0
    )
    median_improvement = (
        (beat.median_kill_cost - oracle_metrics.median_kill_cost) / beat.median_kill_cost
        if beat.median_kill_cost > 0
        else 0.0
    )
    oracle_pass = rel_advantage >= 0.10 or (
        oracle_metrics.detection_rate == beat.detection_rate and median_improvement >= 0.25
    )
    checks = {
        "oracle_advantage": oracle_pass,
        "distinct_baseline_ordering_maps": distinct_maps >= 3,
        "pareto_nondominated_baselines": len(pareto) >= 2,
        "baseline_composite_spread": spread >= 0.10,
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "baseline_to_beat": baseline_to_beat,
        "baseline_composite_spread": spread,
        "distinct_ordering_maps": distinct_maps,
        "pareto_nondominated": pareto,
        "oracle_relative_composite_advantage": rel_advantage,
        "oracle_median_kill_cost_improvement": median_improvement,
    }


def evaluate_testmon_gates(
    masks: dict[str, set[str]],
    all_test_ids: list[str],
) -> dict[str, Any]:
    full_suite = set(all_test_ids)
    fingerprints: set[str] = set()
    full_count = 0
    proper_subset = 0
    for node_set in masks.values():
        fp = hashlib.sha256(",".join(sorted(node_set)).encode()).hexdigest()
        fingerprints.add(fp)
        if node_set == full_suite:
            full_count += 1
        elif node_set and node_set < full_suite:
            proper_subset += 1
    total = len(masks) or 1
    gates = {
        "distinct_masks": len(fingerprints) >= 3,
        "full_suite_fraction": (full_count / total) < 0.90,
        "proper_subset_fraction": (proper_subset / total) >= 0.20,
    }
    return {
        "passed": all(gates.values()),
        "gates": gates,
        "distinct_mask_count": len(fingerprints),
        "full_suite_mutant_fraction": full_count / total,
        "proper_subset_mutant_fraction": proper_subset / total,
    }


def _pytest_cmd() -> list[str]:
    return [sys.executable, "-m", "pytest"]


def _parse_collect_stdout(stdout: str) -> list[str]:
    return [
        line.strip()
        for line in stdout.splitlines()
        if line.strip().startswith("tests/") and "::" in line
    ]


def build_testmon_masks(
    mission: dict[str, Any],
    corpus_dir: Path,
    output_dir: Path,
    mutant_rows: dict[str, dict[str, Any]],
    search_ids: list[str],
    all_test_ids: list[str],
) -> dict[str, set[str]]:
    cache_path = output_dir / "testmon_masks.json"
    masks: dict[str, set[str]] = {}
    collection_fallbacks: dict[str, bool] = {}
    if cache_path.is_file():
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        for mid, nodes in cached.get("masks", {}).items():
            masks[mid] = set(nodes)
        collection_fallbacks = cached.get("collection_fallbacks", {})

    pending = [mid for mid in search_ids if mid not in masks]
    if not pending:
        return masks

    full_suite = set(all_test_ids)
    pristine_wt = create_detached_worktree(mission["target_sha"])
    template_wt = create_detached_worktree(mission["target_sha"])
    matrix_wt: Path | None = None
    env_pristine = {**os.environ, "PYTHONPATH": str(pristine_wt / "src")}
    pytest = _pytest_cmd()
    try:
        proc = subprocess.run(
            pytest + ["--testmon", "-q"],
            cwd=pristine_wt,
            env=env_pristine,
            capture_output=True,
            text=True,
            timeout=600,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"pristine testmon failed:\n{proc.stdout}\n{proc.stderr}")
        pristine_bytes = (pristine_wt / ".testmondata").read_bytes()

        _ensure_mutmut_config(template_wt, mission["module_allowlist"])
        _mutmut_generate(template_wt, workers=mission.get("workers", 4))
        matrix_wt = _prepare_matrix_worktree(
            template_wt,
            mission["target_sha"],
            mission["module_allowlist"],
        )
        env_matrix = {**os.environ, "PYTHONPATH": str(matrix_wt / "src")}

        for mid in pending:
            row = mutant_rows[mid]
            _materialize_mutant_on_worktree(matrix_wt, mid, row["path"])
            (matrix_wt / ".testmondata").write_bytes(pristine_bytes)
            proc2 = subprocess.run(
                pytest + ["--testmon-forceselect", "--collect-only", "-q"],
                cwd=matrix_wt,
                env=env_matrix,
                capture_output=True,
                text=True,
                timeout=120,
            )
            if proc2.returncode != 0:
                masks[mid] = set(full_suite)
                collection_fallbacks[mid] = True
            else:
                masks[mid] = set(_parse_collect_stdout(proc2.stdout))
                collection_fallbacks[mid] = False
            cache_path.write_text(
                json.dumps(
                    {
                        "masks": {k: sorted(v) for k, v in masks.items()},
                        "collection_fallbacks": collection_fallbacks,
                    },
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
    finally:
        remove_worktree(pristine_wt)
        remove_worktree(template_wt)
        if matrix_wt is not None:
            remove_worktree(matrix_wt)
    return masks


def select_baseline_to_beat(baseline_metrics: dict[str, PolicyMetrics]) -> str:
    best_name = BASELINE_NAMES[0]
    best_score = baseline_metrics[best_name].composite
    for name in BASELINE_NAMES[1:]:
        score = baseline_metrics[name].composite
        if score > best_score:
            best_name = name
            best_score = score
    return best_name


def run_preflight(mission_path: Path, corpus_dir: Path, output_dir: Path) -> dict[str, Any]:
    mission = load_mission(mission_path)
    corpus_dir = corpus_dir.resolve()
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    corpus = load_search_corpus(corpus_dir)
    mutant_rows = load_search_mutant_rows(corpus_dir)
    search_ids = list(corpus.search_mutant_ids)

    masks = build_testmon_masks(
        mission, corpus_dir, output_dir, mutant_rows, search_ids, corpus.test_ids
    )
    testmon_report = evaluate_testmon_gates(masks, corpus.test_ids)

    contexts = build_per_mutant_context(corpus, mutant_rows, corpus_dir, masks)
    baseline_metrics: dict[str, PolicyMetrics] = {}
    baseline_payload: dict[str, Any] = {}
    all_ordering_maps: set[tuple[str, ...]] = set()
    for name in BASELINE_NAMES:
        metrics = score_baseline(name, corpus, contexts)
        baseline_metrics[name] = metrics
        maps = baseline_ordering_map(name, corpus, contexts)
        for item in maps:
            all_ordering_maps.add(item)
        baseline_payload[name] = {
            "content_hash": baseline_policy_hash(name, corpus),
            "composite": metrics.composite,
            "detection_rate": metrics.detection_rate,
            "median_kill_cost": metrics.median_kill_cost,
            "distinct_ordering_maps": len(set(maps)),
        }

    baseline_to_beat = select_baseline_to_beat(baseline_metrics)
    oracle_metrics, oracle_weights = score_oracle(corpus, contexts)
    headroom = compute_headroom(
        baseline_metrics,
        baseline_to_beat,
        oracle_metrics,
        {"distinct": len(all_ordering_maps)},
    )

    domain_rejected_reasons: list[str] = []
    if not testmon_report["passed"]:
        domain_rejected_reasons.append("testmon_usefulness_gates")
    if not headroom["passed"]:
        domain_rejected_reasons.append("headroom_gates")

    verdict = "PASS" if not domain_rejected_reasons else "DOMAIN REJECTED"

    payload: dict[str, Any] = {
        "schema_version": 1,
        "verdict": verdict,
        "provider_calls": 0,
        "formula_fingerprint": formula_fingerprint(),
        "baseline_to_beat": baseline_to_beat,
        "baselines": baseline_payload,
        "oracle": {
            "composite": oracle_metrics.composite,
            "detection_rate": oracle_metrics.detection_rate,
            "median_kill_cost": oracle_metrics.median_kill_cost,
            "weights": {
                "w_dep": oracle_weights[0],
                "w_hist": oracle_weights[1],
                "w_cost": oracle_weights[2],
                "w_name": oracle_weights[3],
            },
            "weight_combo_count": len(enumerate_oracle_weight_combos()),
        },
        "headroom": headroom,
        "testmon": testmon_report if testmon_report["passed"] else {
            "passed": False,
            "domain_rejected_reason": "testmon_usefulness_gates",
            **testmon_report,
        },
        "domain_rejected_reasons": domain_rejected_reasons,
        "corpus_dir": str(corpus_dir),
        "target_sha": mission["target_sha"],
        "artifact_hashes": {
            "real_mutations_py": sha256_file(Path(__file__).resolve().parent / "real_mutations.py"),
            "preflight_module_py": sha256_file(Path(__file__).resolve()),
        },
    }
    out_path = output_dir / "preflight.json"
    out_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload
