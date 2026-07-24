"""DS-002 mutation corpus unit tests (VT-2, VT-3)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from detonator.real_mutations_publish import publish_evidence
from detonator.real_mutations import (
    COMPOSITE_WEIGHTS,
    MISS_COST_MULTIPLIER,
    SearchCorpus,
    budget_cap,
    evaluate_permutation,
    formula_fingerprint,
    largest_prefix_within_budget,
    load_search_corpus,
    mutant_metrics,
    prioritize,
    total_clean_cost,
    validate_permutation,
)
from detonator.mutation_corpus import (
    BuildContext,
    OutcomeOrderingViolation,
    _ensure_mutmut_config,
    _mutmut_bin,
    _mutmut_extract_patches_batch,
    _mutmut_generate,
    _mutmut_id_might_match_allowlist,
    _mutmut_list_ids,
    _mutmut_show_patch,
    _normalize_mutmut_patch,
    apply_selection_caps,
    assign_split,
    classify_operator,
    create_detached_worktree,
    load_mission,
    read_matrix_outcomes_for_selection,
    remove_worktree,
    selection_functions_avoid_outcome_reads,
    sha256_text,
    write_rejected_row,
)

ROOT = Path(__file__).resolve().parents[1]
MISSION = ROOT / "examples" / "real_mutations" / "mission.json"
DS002_CORPUS = ROOT / "runs" / "ds002-corpus"
FORMULA_FINGERPRINT_EXPECTED = "01c8e4a02e12bf86784dbf8a94e44b01e19fa9ecb29667eb9f4647130190ed0e"


def _sample_mutant(
    mid: str,
    symbol: str,
    family: str,
    patch: str,
) -> dict:
    return {
        "mutmut_id": mid,
        "qualified_symbol": symbol,
        "operator_family": family,
        "patch_sha256": sha256_text(patch),
        "path": "src/detonator/kernel.py",
        "mutmut_version": "3.6.0",
        "target_sha": "bd17a50d22ccbabd40f1e230868e9dbb7b19c8ff",
    }


def _selection_manifest(mutants: list[dict]) -> dict:
    return {
        "schema_version": 1,
        "target_sha": "bd17a50d22ccbabd40f1e230868e9dbb7b19c8ff",
        "mutmut_version": "3.6.0",
        "lockfile_sha256": "abc",
        "created_at": "2026-01-01T00:00:00+00:00",
        "mutants": mutants,
    }


def test_provenance_fields_present():
    """VT-2: selection manifest includes reproducibility fields."""
    mutants = [
        _sample_mutant("m1", "compute_run_metrics", "comparison_boundary", "- if x == 1\n+ if x != 1\n"),
    ]
    manifest = _selection_manifest(mutants)
    required = {"patch_sha256", "operator_family", "qualified_symbol", "mutmut_version"}
    for mutant in manifest["mutants"]:
        assert required.issubset(mutant)
    mission = load_mission(MISSION)
    assert mission["target_sha"] == "bd17a50d22ccbabd40f1e230868e9dbb7b19c8ff"


def test_selection_determinism():
    """VT-2: identical raw inputs produce identical selection.json hash."""
    raw = [
        _sample_mutant("m3", "evolve", "guard_return", "- return 1\n+ return 0\n"),
        _sample_mutant("m1", "compute_run_metrics", "comparison_boundary", "- if x == 1\n+ if x != 1\n"),
        _sample_mutant("m2", "load_mission", "boolean_negation", "- if not ok\n+ if ok\n"),
    ]
    selected_a, _ = apply_selection_caps(raw)
    selected_b, _ = apply_selection_caps(list(reversed(raw)))
    hash_a = sha256_text(json.dumps(_selection_manifest(selected_a), sort_keys=True))
    hash_b = sha256_text(json.dumps(_selection_manifest(selected_b), sort_keys=True))
    assert hash_a == hash_b


def test_split_freeze_before_matrix(tmp_path: Path):
    """VT-3: split manifest is written before any matrix artifact."""
    ctx = BuildContext()
    with pytest.raises(OutcomeOrderingViolation):
        ctx.record_write("search-matrix.jsonl")

    ctx.enter_selection()
    ctx.freeze_split()
    split_path = tmp_path / "split.json"
    split_path.write_text('{"frozen_at": "t0"}\n', encoding="utf-8")
    ctx.record_write("split.json")

    ctx.enter_matrix()
    matrix_path = tmp_path / "search-matrix.jsonl"
    ctx.record_write("search-matrix.jsonl")
    matrix_path.write_text("{}\n", encoding="utf-8")
    assert ctx.write_log.index("split.json") < ctx.write_log.index("search-matrix.jsonl")


def test_selection_caps_enforced():
    """VT-2: caps ≤4 per (symbol, operator) and ≤12 per symbol."""
    mutants = []
    for i in range(6):
        mutants.append(
            _sample_mutant(
                f"m{i}",
                "compute_run_metrics",
                "comparison_boundary",
                f"- if x == {i}\n+ if x != {i}\n",
            )
        )
    for i in range(15):
        mutants.append(
            _sample_mutant(
                f"n{i}",
                "evolve",
                "guard_return",
                f"- return {i}\n+ return 0\n",
            )
        )
    selected, rejected = apply_selection_caps(mutants, per_symbol_operator=4, per_symbol_total=12)
    pair_counts: dict[tuple[str, str], int] = {}
    symbol_counts: dict[str, int] = {}
    for row in selected:
        key = (row["qualified_symbol"], row["operator_family"])
        pair_counts[key] = pair_counts.get(key, 0) + 1
        symbol_counts[row["qualified_symbol"]] = symbol_counts.get(row["qualified_symbol"], 0) + 1
    assert all(count <= 4 for count in pair_counts.values())
    assert all(count <= 12 for count in symbol_counts.values())
    assert rejected


def test_operator_classification():
    """VT-2: known patch snippets map to expected operator families."""
    assert classify_operator("- if x == 1\n+ if x != 1\n") == "comparison_boundary"
    assert classify_operator("- if not ok\n+ if ok\n") == "boolean_negation"
    assert classify_operator("- i + 1\n+ i - 1\n") == "arithmetic_index_counter"
    assert classify_operator("- return x\n+ return None\n") == "guard_return"


def test_reject_visibility(tmp_path: Path):
    """VT-2: excluded mutants produce rejected.jsonl rows with reasons."""
    rejected_path = tmp_path / "rejected.jsonl"
    row = _sample_mutant("m9", "compute_run_metrics", "comparison_boundary", "- pass\n+ fail\n")
    write_rejected_row(rejected_path, {**row, "reason": "cap exceeded"})
    lines = rejected_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["reason"] == "cap exceeded"
    assert payload["mutmut_id"] == "m9"


def test_no_outcome_based_selection():
    """VT-3 NEGATIVE: selection path cannot read matrix outcomes before split."""
    with pytest.raises(OutcomeOrderingViolation):
        read_matrix_outcomes_for_selection()
    assert selection_functions_avoid_outcome_reads() is True

    mission = load_mission(MISSION)
    selected = [
        _sample_mutant("s1", "compute_run_metrics", "comparison_boundary", "- a\n+ b\n"),
        _sample_mutant("h1", "resolve_run_dir", "guard_return", "- return p\n+ return q\n"),
    ]
    split = assign_split(selected, mission)
    assert split["search"] == ["s1"]
    assert split["holdout"] == ["h1"]


def test_mutmut_id_prefilter_skips_unrelated_symbols():
    mission = load_mission(MISSION)
    assert _mutmut_id_might_match_allowlist(
        "detonator.kernel.x__compute_run_metrics__mutmut_1",
        mission,
    )
    assert not _mutmut_id_might_match_allowlist(
        "detonator.kernel.x__totally_unrelated_symbol__mutmut_1",
        mission,
    )


def _cli_mutmut_show_patch(worktree: Path, mutant_id: str) -> str:
    # NOTE: invoke the orchestrator's own installed `mutmut` binary, not
    # `uv run mutmut`. The corpus target SHA does not (and should not)
    # declare `mutmut` as a product dependency, so `uv run` inside the
    # worktree resolves against that commit's own pyproject.toml/uv.lock
    # and never finds it (see _mutmut_bin() docstring in mutation_corpus.py).
    #
    # Memory-capped: `mutmut show` (like get_diff_for_mutant) parses the
    # WHOLE generated mutants file with libcst on every call. Callers of
    # this helper MUST restrict mutant_id to the small test_priority.py
    # module (~2MB generated) — never kernel.py (~25MB generated measured
    # 4GB+ resident for a single libcst parse; see mutation_corpus.py
    # _mutmut_extract_patches_batch docstring). This cap is defense in
    # depth only, not a substitute for that restriction.
    proc = subprocess.run(
        ["systemd-run", "--user", "--scope", "--quiet", "-p", "MemoryMax=1536M", "-p", "MemorySwapMax=256M", "--"]
        + [_mutmut_bin(), "show", mutant_id],
        cwd=worktree,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"mutmut show {mutant_id} failed:\n{proc.stderr}")
    return _normalize_mutmut_patch(proc.stdout)


@pytest.mark.slow
def test_mutmut_show_patch_cli_api_parity():
    """Slice A1: in-process get_diff_for_mutant matches CLI mutmut show normalization.

    Deliberately restricted to test_priority.py-derived mutant IDs. kernel.py's
    generated mutants file is ~25MB; libcst's cst.parse_module() on that file
    measured 4GB+ resident memory for a SINGLE parse (confirmed root cause of
    repeated WSL-VM-wide OOM crashes during this build). _mutmut_show_patch /
    `mutmut show` both parse the whole file in-process with no subprocess
    boundary, so callers must never point them at kernel.py in test code.
    """
    mission = load_mission(MISSION)
    target_sha = mission["target_sha"]
    worktree = create_detached_worktree(target_sha)
    try:
        _ensure_mutmut_config(worktree, mission["module_allowlist"])
        _mutmut_generate(worktree, workers=2)
        all_ids = _mutmut_list_ids(worktree)
        mutant_ids = [mid for mid in all_ids if "test_priority" in mid][:3]
        assert len(mutant_ids) >= 3, f"expected >=3 test_priority mutant IDs, got {mutant_ids}"

        parity_rows: list[tuple[str, str, bool]] = []
        for mutant_id in mutant_ids:
            cli_patch = _cli_mutmut_show_patch(worktree, mutant_id)
            api_patch = _mutmut_show_patch(worktree, mutant_id)
            patch_hash = sha256_text(api_patch)
            match = sha256_text(cli_patch) == patch_hash
            parity_rows.append((mutant_id, patch_hash, match))
            assert cli_patch == api_patch, (
                f"parity failed for {mutant_id}: cli={sha256_text(cli_patch)} api={patch_hash}"
            )

        for mutant_id, patch_hash, match in parity_rows:
            assert match, mutant_id
            assert patch_hash
    finally:
        remove_worktree(worktree)


@pytest.mark.slow
def test_extract_patches_batch_matches_show_patch():
    """Text-based batch extractor must be byte-identical to the libcst-based
    single-mutant path (see _mutmut_extract_patches_batch docstring for why
    the batch path avoids libcst: a real ~25MB generated mutants file made
    cst.parse_module() use 4GB+ resident memory for a single parse).

    The *batch* call itself (kernel.py + test_priority.py, thousands of IDs)
    is exercised separately as part of the full corpus build and in manual
    validation; this test only points the REFERENCE (in-process,
    subprocess-free) _mutmut_show_patch at test_priority.py-derived IDs to
    keep the automated suite's peak memory bounded.
    """
    mission = load_mission(MISSION)
    target_sha = mission["target_sha"]
    worktree = create_detached_worktree(target_sha)
    try:
        _ensure_mutmut_config(worktree, mission["module_allowlist"])
        _mutmut_generate(worktree, workers=2)
        all_ids = _mutmut_list_ids(worktree)
        tp_ids = [mid for mid in all_ids if "test_priority" in mid]
        assert len(tp_ids) >= 20, f"expected a real test_priority corpus, got {len(tp_ids)} ids"
        sample = tp_ids[:: max(1, len(tp_ids) // 20)][:20]

        batch_patches = _mutmut_extract_patches_batch(worktree, sample, mission["module_allowlist"])
        assert set(batch_patches) == set(sample)

        for mutant_id in sample:
            reference = _mutmut_show_patch(worktree, mutant_id)
            assert batch_patches[mutant_id] == reference, mutant_id
    finally:
        remove_worktree(worktree)


def test_real_mutations_formula_fingerprint_frozen():
    assert formula_fingerprint() == FORMULA_FINGERPRINT_EXPECTED
    assert COMPOSITE_WEIGHTS == {
        "detection": 0.60,
        "mean_r": 0.15,
        "median_r": 0.15,
        "p90_r": 0.10,
    }


def test_real_mutations_miss_cost_is_two_c():
    test_ids = ["t1", "t2"]
    cost_by_id = {"t1": 1.0, "t2": 3.0}
    total_c = total_clean_cost(cost_by_id, test_ids)
    budget = budget_cap(cost_by_id, test_ids)
    permutation = ["t1", "t2"]
    prefix = largest_prefix_within_budget(permutation, cost_by_id, budget)
    detected, c_m = mutant_metrics(prefix, killers=set(), cost_by_id=cost_by_id, total_c=total_c)
    assert detected == 0
    assert c_m == MISS_COST_MULTIPLIER * total_c


def test_real_mutations_invalid_permutation_scores_zero():
    test_ids = ["a", "b"]
    cost_by_id = {"a": 1.0, "b": 1.0}
    score_dup = evaluate_permutation(
        ["a", "a"],
        test_ids,
        cost_by_id,
        mutant_ids=["m1"],
        kill_tests_by_mutant={"m1": {"a"}},
    )
    score_missing = evaluate_permutation(
        ["a"],
        test_ids,
        cost_by_id,
        mutant_ids=["m1"],
        kill_tests_by_mutant={"m1": {"a"}},
    )
    assert score_dup == 0.0
    assert score_missing == 0.0
    assert validate_permutation(["a", "a"], test_ids) is False


def test_real_mutations_holdout_matrix_never_opened(monkeypatch, tmp_path: Path):
    corpus_dir = tmp_path / "mini-corpus"
    corpus_dir.mkdir()
    (corpus_dir / "tests.json").write_text(
        json.dumps({"node_ids": ["tests/x.py::test_one"]}),
        encoding="utf-8",
    )
    (corpus_dir / "split.json").write_text(
        json.dumps({"search": ["mutant_a"], "holdout": ["mutant_h"]}),
        encoding="utf-8",
    )
    (corpus_dir / "search-matrix.jsonl").write_text(
        json.dumps(
            {
                "duration_seconds": 0.5,
                "mutmut_id": "mutant_a",
                "node_id": "tests/x.py::test_one",
                "outcome": "pass",
                "side": "search",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (corpus_dir / "holdout-matrix.jsonl").write_text("{}\n", encoding="utf-8")

    real_open = Path.open

    def guarded_open(self, *args, **kwargs):
        if "holdout-matrix" in str(self):
            raise AssertionError("holdout-matrix.jsonl must not be opened")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    corpus = load_search_corpus(corpus_dir)
    assert corpus.search_mutant_ids == ["mutant_a"]


def test_real_mutations_search_only_loader_excludes_holdout_mutants():
    if not DS002_CORPUS.is_dir():
        pytest.skip("ds002 corpus not present")
    corpus = load_search_corpus(DS002_CORPUS)
    split = json.loads((DS002_CORPUS / "split.json").read_text(encoding="utf-8"))
    assert set(corpus.search_mutant_ids) == set(split["search"])
    assert set(corpus.search_mutant_ids).isdisjoint(set(split["holdout"]))


def test_real_mutations_prioritize_shortest_baseline_first():
    tests = [
        {"id": "slow", "baseline_duration_ms": 300.0},
        {"id": "fast", "baseline_duration_ms": 100.0},
    ]
    order = prioritize({"path": "p.py", "qualified_symbol": "sym"}, tests)
    assert order == ["fast", "slow"]


def test_oracle_weight_combo_count_is_624():
    from detonator.real_mutations_preflight import enumerate_oracle_weight_combos

    combos = enumerate_oracle_weight_combos()
    assert len(combos) == 624
    assert (0.0, 0.0, 0.0, 0.0) not in combos


def test_baseline_tie_break_uses_fixed_list_order():
    from detonator.real_mutations_preflight import PolicyMetrics, select_baseline_to_beat

    tied = PolicyMetrics(composite=0.5, detection_rate=0.5, median_kill_cost=1.0, ordering_tuple=())
    metrics = {
        "shortest": tied,
        "dependency-first": tied,
        "historical-kill": tied,
        "risk-per-cost": tied,
        "strong-human": tied,
    }
    assert select_baseline_to_beat(metrics) == "shortest"


def test_headroom_struct_excludes_holdout_fields():
    from detonator.real_mutations_preflight import PolicyMetrics, compute_headroom

    metrics = {
        name: PolicyMetrics(composite=0.4 + i * 0.05, detection_rate=0.5, median_kill_cost=1.0, ordering_tuple=())
        for i, name in enumerate(
            [
                "shortest",
                "dependency-first",
                "historical-kill",
                "risk-per-cost",
                "strong-human",
            ]
        )
    }
    oracle = PolicyMetrics(composite=0.7, detection_rate=0.6, median_kill_cost=0.5, ordering_tuple=())
    payload = compute_headroom(metrics, "shortest", oracle, {"distinct": 4})
    blob = json.dumps(payload)
    assert "holdout" not in blob


def test_testmon_mask_gates_fixture():
    from detonator.real_mutations_preflight import evaluate_testmon_gates

    all_tests = [f"tests/t.py::test_{i}" for i in range(5)]
    full = set(all_tests)
    masks = {
        "m1": full,
        "m2": full,
        "m3": {"tests/t.py::test_0"},
        "m4": {"tests/t.py::test_0", "tests/t.py::test_1"},
        "m5": {"tests/t.py::test_2"},
    }
    report = evaluate_testmon_gates(masks, all_tests)
    assert report["gates"]["distinct_masks"] is True
    assert report["gates"]["proper_subset_fraction"] is True


def test_seed_policy_matches_strong_human_baseline():
    import importlib.util

    seed_path = ROOT / "examples" / "real_mutations" / "seed.py"
    spec = importlib.util.spec_from_file_location("ds002_seed", seed_path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.BASELINE_POLICY == "strong-human"
    tests = [
        {
            "id": "tests/a.py::test_dep",
            "baseline_duration_ms": 100.0,
            "dependency_hit": True,
            "historical_kill_rate": 0.1,
            "history_observations": 10,
        },
        {
            "id": "tests/a.py::test_other",
            "baseline_duration_ms": 50.0,
            "dependency_hit": False,
            "historical_kill_rate": 0.9,
            "history_observations": 10,
        },
    ]
    order = mod.prioritize({"path": "src/x.py", "qualified_symbol": "sym"}, tests)
    assert order[0] == "tests/a.py::test_dep"


PREFLIGHT_PATH = ROOT / "runs" / "ds002-preflight" / "preflight.json"


def test_ds002_publish_writes_evidence_artifacts(tmp_path: Path):
    if not PREFLIGHT_PATH.is_file():
        pytest.skip("ds002 preflight not present")
    out = tmp_path / "evidence"
    publish_evidence(MISSION, PREFLIGHT_PATH, out)
    assert (out / "result.json").is_file()
    assert (out / "report.md").is_file()
    result = json.loads((out / "result.json").read_text(encoding="utf-8"))
    preflight = json.loads(PREFLIGHT_PATH.read_text(encoding="utf-8"))
    assert result["verdict"] == "DOMAIN REJECTED"
    assert result["provider_calls"] == 0
    assert result["formula_fingerprint"] == preflight["formula_fingerprint"]
    assert result["target_sha"] == preflight["target_sha"]
    assert "best_policy" not in (out / "result.json").read_text(encoding="utf-8")
    report = (out / "report.md").read_text(encoding="utf-8")
    assert "# DS-002 published verdict" in report
    assert "DOMAIN REJECTED" in report


def test_ds002_publish_cli(tmp_path: Path):
    if not PREFLIGHT_PATH.is_file():
        pytest.skip("ds002 preflight not present")
    out = tmp_path / "out"
    proc = subprocess.run(
        [
            "uv",
            "run",
            "detonator",
            "publish",
            str(MISSION),
            "--preflight",
            str(PREFLIGHT_PATH),
            "--output",
            str(out),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads((out / "result.json").read_text(encoding="utf-8"))["verdict"] == "DOMAIN REJECTED"


def test_ds002_order_cli_strong_human():
    if not DS002_CORPUS.is_dir():
        pytest.skip("ds002 corpus not present")
    proc = subprocess.run(
        [
            "uv",
            "run",
            "detonator",
            "order",
            str(MISSION),
            "--corpus",
            str(DS002_CORPUS),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["policy"] == "strong-human"
    assert payload["mutant_id"]
    assert len(payload["test_order"]) > 0

