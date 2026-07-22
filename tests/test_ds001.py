"""DS-001 evaluator and CLI smoke tests (commit 1)."""

from __future__ import annotations

from pathlib import Path

from detonator import test_priority as tp
from detonator.kernel import evolve, evaluate_candidate_source

ROOT = Path(__file__).resolve().parents[1]
MISSION = ROOT / "examples" / "test_priority" / "mission.json"


def test_benchmark_integrity():
    mission = tp.load_mission(MISSION)
    benchmark = tp.load_benchmark_module(mission["_benchmark_path"])
    assert len(benchmark.SEARCH_FAULT_IDS) == 16
    assert len(benchmark.HOLDOUT_FAULT_IDS) == 8
    assert set(benchmark.SEARCH_FAULT_IDS).isdisjoint(benchmark.HOLDOUT_FAULT_IDS)


def test_seed_scores_via_subprocess(tmp_path: Path):
    mission = tp.load_mission(MISSION)
    benchmark = tp.load_benchmark_module(mission["_benchmark_path"])
    search_faults = tp.load_fault_ids(mission["_search_path"])
    source = mission["_seed_path"].read_text(encoding="utf-8")
    result = evaluate_candidate_source(source, benchmark, search_faults, timeout_seconds=2.0)
    assert result["execution"]["status"] == "ok"
    assert result["evaluation"]["status"] == "valid"
    assert isinstance(result["evaluation"]["score"], float)
    assert len(result["evaluation"]["fault_traces"]) == 16
    for trace in result["evaluation"]["fault_traces"]:
        assert trace["first_failing_test"] is not None
        assert trace["executed_tests"]


def test_evolve_budget_zero(tmp_path: Path):
    out = tmp_path / "slice-1"
    result = evolve(MISSION, budget=0, output=out)
    records = result["records"]
    assert len(records) == 1
    seed = records[0]
    assert seed["candidate_id"] == "c0000"
    assert seed["search"]["evaluation"]["status"] == "valid"
    assert seed["search"]["evaluation"]["score"] is not None
    assert len(seed["search"]["evaluation"]["fault_traces"]) == 16
    jsonl = (out / "candidates.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(jsonl) == 1
    assert (out / "candidates" / "c0000.py").is_file()


def test_evolve_population_exposes_breakage(tmp_path: Path):
    out = tmp_path / "slice-2"
    result = evolve(MISSION, budget=24, output=out)
    records = result["records"]
    descendants = records[1:]
    assert len(descendants) == 24
    statuses = [r["search"]["evaluation"]["status"] for r in descendants]
    assert statuses.count("valid") >= 20
    assert "invalid" in statuses
    assert "crash" in statuses
    assert "timeout" in statuses
    hashes = {r["artifact"]["sha256"] for r in descendants}
    assert len(hashes) == 24
    for record in descendants:
        assert (out / record["artifact"]["path"]).is_file()
        assert record["parent_id"] is not None
        assert record["search"]["execution"]["status"] in {"ok", "crash", "timeout"}
