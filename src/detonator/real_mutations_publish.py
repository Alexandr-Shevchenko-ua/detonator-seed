"""DS-002 publish: re-export preflight verdict to evidence artifacts."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any, Callable

from detonator.mutation_corpus import load_mission
from detonator.real_mutations import load_search_corpus
from detonator.real_mutations_preflight import (
    build_change_record,
    build_tests_for_mutant,
    load_patch_text,
    load_search_mutant_rows,
    loo_history_tables,
)

FORMULA_FINGERPRINT_EXPECTED = (
    "01c8e4a02e12bf86784dbf8a94e44b01e19fa9ecb29667eb9f4647130190ed0e"
)
PUBLISHED_REFERENCE_POLICY = "strong-human"
SEED_SOURCE = "examples/real_mutations/seed.py"


def require_real_mutations_mission(mission_path: Path) -> dict[str, Any]:
    mission = load_mission(mission_path)
    if mission.get("domain") != "real_mutations":
        raise ValueError(
            f"mission domain must be 'real_mutations', got {mission.get('domain')!r}"
        )
    return mission


def load_preflight(preflight_path: Path) -> dict[str, Any]:
    return json.loads(preflight_path.read_text(encoding="utf-8"))


def validate_preflight_contract(preflight: dict[str, Any]) -> None:
    if preflight.get("verdict") != "DOMAIN REJECTED":
        raise ValueError(f"expected DOMAIN REJECTED verdict, got {preflight.get('verdict')!r}")
    if preflight.get("provider_calls", -1) != 0:
        raise ValueError("preflight provider_calls must be 0")
    reasons = preflight.get("domain_rejected_reasons") or []
    if "headroom_gates" not in reasons:
        raise ValueError("domain_rejected_reasons must include headroom_gates")
    headroom = preflight.get("headroom") or {}
    if headroom.get("passed") is not False:
        raise ValueError("headroom.passed must be false")
    if preflight.get("formula_fingerprint") != FORMULA_FINGERPRINT_EXPECTED:
        raise ValueError("formula_fingerprint mismatch")
    if preflight.get("baseline_to_beat") != PUBLISHED_REFERENCE_POLICY:
        raise ValueError("baseline_to_beat must be strong-human")
    for key in ("baselines", "target_sha"):
        if key not in preflight:
            raise ValueError(f"preflight missing required field: {key}")


def build_result_json(preflight: dict[str, Any], preflight_source: str) -> dict[str, Any]:
    validate_preflight_contract(preflight)
    return {
        "schema_version": 1,
        "verdict": "DOMAIN REJECTED",
        "target_sha": preflight["target_sha"],
        "formula_fingerprint": preflight["formula_fingerprint"],
        "provider_calls": 0,
        "baseline_to_beat": PUBLISHED_REFERENCE_POLICY,
        "baselines": preflight["baselines"],
        "headroom": preflight["headroom"],
        "domain_rejected_reasons": list(preflight["domain_rejected_reasons"]),
        "preflight_source": preflight_source,
        "published_reference_policy": PUBLISHED_REFERENCE_POLICY,
    }


def _failed_headroom_checks(headroom: dict[str, Any]) -> list[str]:
    checks = headroom.get("checks") or {}
    return sorted(name for name, passed in checks.items() if passed is False)


def render_report_md(result: dict[str, Any], preflight_path: str) -> str:
    headroom = result["headroom"]
    failed = _failed_headroom_checks(headroom)
    oracle_line = (
        f"Oracle relative composite advantage: {headroom.get('oracle_relative_composite_advantage')}; "
        f"oracle median kill cost improvement: {headroom.get('oracle_median_kill_cost_improvement')}; "
        f"baseline composite spread: {headroom.get('baseline_composite_spread')}."
    )
    baseline_rows = []
    for name, metrics in sorted(result["baselines"].items()):
        baseline_rows.append(
            f"| {name} | {metrics['composite']} | {metrics['detection_rate']} | {metrics['median_kill_cost']} |"
        )
    baselines_table = "\n".join(
        [
            "| policy | composite | detection_rate | median_kill_cost |",
            "| --- | --- | --- | --- |",
            *baseline_rows,
        ]
    )
    return "\n".join(
        [
            "# DS-002 published verdict",
            "",
            "## Verdict",
            "",
            "`DOMAIN REJECTED` — live evolution was **not** run (no `runs/ds002-live`).",
            "",
            "## Target",
            "",
            f"- `target_sha`: `{result['target_sha']}`",
            f"- `formula_fingerprint`: `{result['formula_fingerprint']}`",
            "",
            "## Baselines",
            "",
            baselines_table,
            "",
            "## Headroom",
            "",
            f"- `passed`: `false`",
            f"- failed checks: {', '.join(f'`{c}`' for c in failed) or '(none)'}",
            f"- {oracle_line}",
            "",
            "## How to verify",
            "",
            "```bash",
            "uv run detonator publish examples/real_mutations/mission.json \\",
            "  --preflight runs/ds002-preflight/preflight.json \\",
            "  --output evidence/ds002",
            "test -f evidence/ds002/result.json && test -f evidence/ds002/report.md",
            "uv run python -c \"",
            "import json",
            "r=json.load(open('evidence/ds002/result.json'))",
            "p=json.load(open('runs/ds002-preflight/preflight.json'))",
            "assert r['verdict']=='DOMAIN REJECTED' and r['provider_calls']==0",
            "assert r['formula_fingerprint']==p['formula_fingerprint']",
            "assert r['target_sha']==p['target_sha']",
            "print('S3_OK')",
            "\"",
            "uv run detonator order --help",
            "uv run detonator order examples/real_mutations/mission.json --corpus runs/ds002-corpus",
            "uv run pytest -q tests/test_ds002.py",
            "```",
            "",
            "## Provenance",
            "",
            f"- Preflight source: `{preflight_path}`",
            "- `provider_calls`: `0`",
            "",
        ]
    )


def publish_evidence(
    mission_path: Path,
    preflight_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    require_real_mutations_mission(mission_path)
    preflight_path = preflight_path.resolve()
    preflight = load_preflight(preflight_path)
    result = build_result_json(preflight, str(preflight_path))
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    result_path = output_dir / "result.json"
    report_path = output_dir / "report.md"
    result_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    report_path.write_text(render_report_md(result, str(preflight_path)), encoding="utf-8")
    return result


def load_seed_prioritize(seed_path: Path) -> Callable[..., list[str]]:
    seed_path = seed_path.resolve()
    spec = importlib.util.spec_from_file_location("ds002_seed_publish", seed_path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load seed module from {seed_path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    prioritize = getattr(mod, "prioritize", None)
    if prioritize is None:
        raise ValueError(f"{seed_path} missing prioritize()")
    if getattr(mod, "BASELINE_POLICY", None) != PUBLISHED_REFERENCE_POLICY:
        raise ValueError(f"{seed_path} BASELINE_POLICY must be {PUBLISHED_REFERENCE_POLICY!r}")
    return prioritize


def emit_strong_human_order(mission_path: Path, corpus_dir: Path, seed_path: Path | None = None) -> dict[str, Any]:
    require_real_mutations_mission(mission_path)
    corpus_dir = corpus_dir.resolve()
    corpus = load_search_corpus(corpus_dir)
    if not corpus.search_mutant_ids:
        raise ValueError("corpus has no search mutants")
    mutant_id = corpus.search_mutant_ids[0]
    mutant_rows = load_search_mutant_rows(corpus_dir)
    row = mutant_rows[mutant_id]
    patch = load_patch_text(corpus_dir, row["patch_sha256"])
    change = build_change_record(row, patch)
    loo_kills, loo_obs = loo_history_tables(corpus)
    tests = build_tests_for_mutant(
        corpus,
        mutant_id,
        set(),
        loo_kills[mutant_id],
        loo_obs[mutant_id],
    )
    if seed_path is None:
        seed_path = mission_path.resolve().parent / "seed.py"
    prioritize = load_seed_prioritize(seed_path)
    test_order = prioritize(change, tests)
    resolved_seed = seed_path.resolve()
    try:
        source = str(resolved_seed.relative_to(Path.cwd()))
    except ValueError:
        source = str(resolved_seed)
    return {
        "policy": PUBLISHED_REFERENCE_POLICY,
        "source": source,
        "mutant_id": mutant_id,
        "test_order": test_order,
    }
