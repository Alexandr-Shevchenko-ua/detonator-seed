"""DS-001 evaluator and CLI smoke tests (commit 1)."""

from __future__ import annotations

from pathlib import Path

from detonator import test_priority as tp
from detonator.kernel import evolve, evaluate_candidate_source, inspect_run

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


def test_archive_and_holdout_discipline(tmp_path: Path):
    out = tmp_path / "slice-3"
    result = evolve(MISSION, budget=24, output=out)
    archive = result["archive"]
    holdout = result["holdout"]
    occupied = [c for c in archive["cells"] if c["candidate_id"]]
    assert len(occupied) == 4
    cells = {tuple(c["cell"]) for c in occupied}
    assert cells == {
        ("fast", "change_focused"),
        ("fast", "general"),
        ("cost_tolerant", "change_focused"),
        ("cost_tolerant", "general"),
    }

    # Winners are search-best inside their cell among valid records.
    records = result["records"]
    by_cell: dict[tuple[str, str], list] = {}
    for record in records:
        behavior = record["search"].get("behavior")
        if record["search"]["evaluation"]["status"] != "valid" or not behavior:
            continue
        cell = tuple(behavior["cell"])
        by_cell.setdefault(cell, []).append(record)
    for cell_info in occupied:
        cell = tuple(cell_info["cell"])
        best = max(by_cell[cell], key=lambda r: r["search"]["evaluation"]["score"])
        assert cell_info["candidate_id"] == best["candidate_id"]

    # Lineage depth >= 2 for at least one winner.
    id_to_record = {r["candidate_id"]: r for r in records}

    def depth(cid: str) -> int:
        d = 0
        seen = set()
        cur = cid
        while cur is not None and cur not in seen:
            seen.add(cur)
            parent = id_to_record[cur].get("parent_id")
            if parent is None:
                break
            d += 1
            cur = parent
        return d

    assert max(depth(c["candidate_id"]) for c in occupied) >= 2

    seed_id = records[0]["candidate_id"]
    winner_ids = {c["candidate_id"] for c in occupied}
    assert set(holdout["evaluated_ids"]) == {seed_id} | winner_ids
    assert holdout["improved_over_seed"] is False or holdout["delta"] > 0
    if not holdout["improved_over_seed"]:
        assert result["summary"]["holdout"]["conclusion"] == "no holdout improvement found"

    # Holdout loader must fail before freeze.
    gate = tp.HoldoutGate(MISSION.parent / "holdout.json")
    try:
        gate.load_fault_ids()
        assert False, "expected pre-freeze holdout access to fail"
    except RuntimeError as exc:
        assert "before archive freeze" in str(exc)


def test_inspect_verify_and_replay(tmp_path: Path):
    out = tmp_path / "slice-3"
    evolve(MISSION, budget=24, output=out)
    code = inspect_run(out, verify=True, replay_retained=True, mission_path=MISSION)
    assert code == 0


def test_external_variation_command(tmp_path: Path):
    out = tmp_path / "slice-5"
    command = f"uv run python {ROOT / 'examples' / 'providers' / 'sample_provider.py'}"
    result = evolve(MISSION, budget=4, output=out, variation_command=command)
    descendants = result["records"][1:]
    assert len(descendants) == 4
    for record in descendants:
        assert record["mutation"]["provider"] == "external"
        assert (out / record["artifact"]["path"]).is_file()
        assert record["search"]["evaluation"]["status"] in {
            "valid",
            "invalid",
            "crash",
            "timeout",
        }
