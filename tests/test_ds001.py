"""DS-001 evaluator and CLI smoke tests."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from detonator import test_priority as tp
from detonator.kernel import evolve, evaluate_candidate_source, inspect_run

ROOT = Path(__file__).resolve().parents[1]
MISSION = ROOT / "examples" / "test_priority" / "mission.json"


def test_benchmark_integrity():
    mission = tp.load_mission(MISSION)
    benchmark = tp.load_benchmark_module(mission["_benchmark_path"], validate="search")
    assert len(benchmark.SEARCH_FAULT_IDS) == 16
    assert benchmark.HOLDOUT_FAULT_IDS == []
    assert benchmark.holdout_specs_loaded() == []
    assert not benchmark.holdout_definitions_attached()
    # Search validation must not construct/execute holdout faults.
    assert benchmark.holdout_fault_access_log() == []
    # Physical holdout module must not be imported yet.
    import sys

    holdout_path = (MISSION.parent / "holdout_faults.py").resolve()
    for module in sys.modules.values():
        mod_file = getattr(module, "__file__", None)
        if mod_file and Path(mod_file).resolve() == holdout_path:
            raise AssertionError("holdout_faults.py imported during search load")

    benchmark.attach_holdout_definitions(holdout_path)
    assert benchmark.holdout_definitions_attached()
    assert benchmark.holdout_specs_loaded() == [f"h{i:02d}" for i in range(1, 9)]
    benchmark.validate_holdout_benchmark(benchmark.HOLDOUT_FAULT_IDS)
    assert benchmark.holdout_fault_access_log()


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

    # Causal lineage: exploration/injected probes are seed children; derived
    # records bind the parent they mutated from.
    seed_id = records[0]["candidate_id"]
    for record in records[1:]:
        mode = (record.get("mutation") or {}).get("lineage_mode")
        if mode == "seed":
            assert record["parent_id"] == seed_id
            assert record["generation"] == 1
            assert record["parent_sha256"] == records[0]["artifact"]["sha256"]
        elif mode == "derived":
            parent = id_to_record[record["parent_id"]]
            assert record["generation"] == parent["generation"] + 1
            assert record["parent_sha256"] == parent["artifact"]["sha256"]
        else:
            raise AssertionError(f"unexpected lineage_mode: {mode}")

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

    # Full evolve path: no holdout file read, fault construction, or definitions
    # load before freeze.
    assert result["pre_freeze_holdout_reads"] == 0
    assert result["pre_freeze_holdout_fault_access"] == []
    assert result["pre_freeze_holdout_specs"] == []
    assert result["pre_freeze_holdout_module_loaded"] is False
    assert result["holdout_gate"].read_count >= 1
    assert result["post_freeze_holdout_specs"] == [f"h{i:02d}" for i in range(1, 9)]


def test_consecutive_evolve_purges_holdout_before_benchmark_exec(tmp_path: Path):
    """A prior evolve must not leave holdout defs loaded into the next search phase."""
    first = tmp_path / "first"
    second = tmp_path / "second"
    holdout_path = (MISSION.parent / "holdout_faults.py").resolve()

    result1 = evolve(MISSION, budget=1, output=first)
    assert result1["pre_freeze_holdout_module_loaded"] is False
    assert result1["post_freeze_holdout_specs"]

    # After the first run the holdout module is present until the next load.
    present_after_first = any(
        getattr(module, "__file__", None)
        and Path(module.__file__).resolve() == holdout_path
        for module in sys.modules.values()
    )
    assert present_after_first is True

    observed_during_second_exec: list[bool] = []
    real_spec_from_file_location = importlib.util.spec_from_file_location

    def tracking_spec_from_file_location(name, location, *args, **kwargs):
        spec = real_spec_from_file_location(name, location, *args, **kwargs)
        if spec is None or spec.loader is None:
            return spec
        # Only observe search-phase benchmark execution.
        if name != "tp_benchmark":
            return spec
        real_exec = spec.loader.exec_module

        def exec_module(module):
            present = any(
                getattr(mod, "__file__", None)
                and Path(mod.__file__).resolve() == holdout_path
                for mod in sys.modules.values()
            )
            observed_during_second_exec.append(present)
            return real_exec(module)

        spec.loader.exec_module = exec_module  # type: ignore[method-assign]
        return spec

    importlib.util.spec_from_file_location = tracking_spec_from_file_location  # type: ignore[assignment]
    try:
        result2 = evolve(MISSION, budget=1, output=second)
    finally:
        importlib.util.spec_from_file_location = real_spec_from_file_location  # type: ignore[assignment]

    assert observed_during_second_exec, "benchmark exec was not observed"
    assert all(present is False for present in observed_during_second_exec)
    assert result2["pre_freeze_holdout_module_loaded"] is False
    assert result2["pre_freeze_holdout_specs"] == []
    assert result2["post_freeze_holdout_specs"] == [f"h{i:02d}" for i in range(1, 9)]


def test_output_refuses_existing_paths(tmp_path: Path):
    existing = tmp_path / "occupied"
    existing.mkdir()
    marker = existing / "keep.txt"
    marker.write_text("safe\n", encoding="utf-8")
    before = marker.read_bytes()
    try:
        evolve(MISSION, budget=0, output=existing)
        assert False, "expected existing output path to fail"
    except FileExistsError as exc:
        assert "refusing to overwrite" in str(exc)
    assert marker.read_bytes() == before

    # Checkout-like path must also fail.
    try:
        evolve(MISSION, budget=0, output=ROOT)
        assert False, "expected repo root output to fail"
    except FileExistsError:
        pass

    fresh = tmp_path / "fresh-out"
    result = evolve(MISSION, budget=0, output=fresh)
    assert fresh.is_dir()
    assert (fresh / "candidates" / "c0000.py").is_file()
    assert result["run_dir"] == fresh.resolve()


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
    # Raw command must not be persisted.
    summary_text = (out / "summary.json").read_text(encoding="utf-8")
    assert "sample_provider.py" not in summary_text
    assert result["summary"]["provider"]["kind"] == "external_command"
    assert result["summary"]["provider"]["timeout_seconds"] == 120
    metrics = result["summary"]["metrics"]
    assert metrics["executed_descendants"] == 4
    assert metrics["byte_hash_distinct_sources"] == 4
    assert metrics["ast_distinct_valid_sources"] >= 1


def test_provider_timeout_and_secret_nonleak(tmp_path: Path):
    slow = tmp_path / "slow_provider.py"
    slow.write_text(
        """import json, sys, time
req = json.load(sys.stdin)
time.sleep(6)
idx = int(req["proposal_index"])
src = '''def prioritize(change, tests):
    return [t["id"] for t in sorted(tests, key=lambda t: t["id"])]
'''
json.dump({"source": src, "mutation_description": f"slow-{idx}"}, sys.stdout)
print()
""",
        encoding="utf-8",
    )
    secret = "SUPERSECRET_TOKEN_XYZ"
    command = f"{sys.executable} {slow} --token {secret}"

    out_ok = tmp_path / "provider-ok"
    result = evolve(
        MISSION,
        budget=1,
        output=out_ok,
        variation_command=command,
        provider_timeout_seconds=10,
    )
    assert result["records"][1]["search"]["evaluation"]["status"] == "valid"
    assert result["summary"]["provider"]["timeout_seconds"] == 10
    for path in out_ok.rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="ignore")
            assert secret not in text
            assert "--token" not in text

    out_fail = tmp_path / "provider-fail"
    try:
        evolve(
            MISSION,
            budget=1,
            output=out_fail,
            variation_command=command,
            provider_timeout_seconds=1,
        )
        assert False, "expected provider timeout"
    except RuntimeError as exc:
        assert "timed out after 1.0s" in str(exc)
        assert secret not in str(exc)
