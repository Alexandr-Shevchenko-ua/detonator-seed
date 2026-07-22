"""Bounded evolution kernel for DS-001."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from detonator import test_priority as tp

WORKER_PATH = Path(__file__).resolve().parent / "_worker.py"


def _utc_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _write_text(path: Path, text: str) -> str:
    # Normalize to UTF-8 LF bytes for stable hashing.
    normalized = text.replace("\r\n", "\n")
    if not normalized.endswith("\n"):
        normalized += "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(normalized, encoding="utf-8", newline="\n")
    return normalized


def _candidate_id(index: int) -> str:
    return f"c{index:04d}"


def run_candidate_subprocess(
    source_text: str,
    requests: list[dict[str, Any]],
    timeout_seconds: float,
) -> dict[str, Any]:
    """Copy candidate into a disposable cwd and invoke prioritize via worker."""
    with tempfile.TemporaryDirectory(prefix="detonator-cand-") as tmp:
        tmp_path = Path(tmp)
        source_path = tmp_path / "candidate.py"
        _write_text(source_path, source_text)
        worker_copy = tmp_path / "_worker.py"
        shutil.copy2(WORKER_PATH, worker_copy)
        payload = {
            "source_path": str(source_path),
            "requests": requests,
        }
        started = time.perf_counter()
        try:
            completed = subprocess.run(
                [sys.executable, str(worker_copy)],
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                timeout=timeout_seconds,
                cwd=str(tmp_path),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""
            if isinstance(stdout, bytes):
                stdout = stdout.decode("utf-8", errors="replace")
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", errors="replace")
            return {
                "status": "timeout",
                "exit_code": None,
                "elapsed_ms": elapsed_ms,
                "stdout": stdout,
                "stderr": stderr or f"timeout after {timeout_seconds:.1f}s",
                "orderings": None,
            }

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        if completed.returncode != 0:
            return {
                "status": "crash",
                "exit_code": completed.returncode,
                "elapsed_ms": elapsed_ms,
                "stdout": stdout,
                "stderr": stderr,
                "orderings": None,
            }
        try:
            parsed = json.loads(stdout)
            orderings = parsed["orderings"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            return {
                "status": "crash",
                "exit_code": completed.returncode,
                "elapsed_ms": elapsed_ms,
                "stdout": stdout,
                "stderr": stderr + f"\nworker output parse error: {exc}",
                "orderings": None,
            }
        return {
            "status": "ok",
            "exit_code": completed.returncode,
            "elapsed_ms": elapsed_ms,
            "stdout": stdout,
            "stderr": stderr,
            "orderings": orderings,
        }


def evaluate_candidate_source(
    source_text: str,
    benchmark,
    fault_ids: list[str],
    timeout_seconds: float,
) -> dict[str, Any]:
    requests = tp.public_requests(benchmark, fault_ids)
    execution = run_candidate_subprocess(source_text, requests, timeout_seconds)
    if execution["status"] == "timeout":
        return {
            "execution": execution,
            "evaluation": {
                "status": "timeout",
                "reason": execution["stderr"],
                "score": None,
                "orderings": None,
                "fault_traces": [],
                "behavior": None,
            },
        }
    if execution["status"] == "crash":
        return {
            "execution": execution,
            "evaluation": {
                "status": "crash",
                "reason": (execution["stderr"] or "candidate crashed").strip().splitlines()[-1:],
                "score": None,
                "orderings": None,
                "fault_traces": [],
                "behavior": None,
            },
        }
    evaluation = tp.evaluate_orderings(benchmark, execution["orderings"], fault_ids)
    return {"execution": execution, "evaluation": evaluation}


def _normalize_eval_reason(evaluation: dict[str, Any]) -> dict[str, Any]:
    reason = evaluation.get("reason")
    if isinstance(reason, list):
        evaluation = dict(evaluation)
        evaluation["reason"] = reason[0] if reason else "candidate crashed"
    return evaluation


def materialize_candidate(
    *,
    run_dir: Path,
    index: int,
    source_text: str,
    generation: int,
    parent: dict[str, Any] | None,
    mutation: dict[str, Any],
    search_result: dict[str, Any],
    archive_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cand_id = _candidate_id(index)
    rel_path = f"candidates/{cand_id}.py"
    abs_path = run_dir / rel_path
    normalized = _write_text(abs_path, source_text)
    digest = tp.sha256_text(normalized)
    evaluation = _normalize_eval_reason(search_result["evaluation"])
    execution = search_result["execution"]
    record = {
        "schema_version": 1,
        "candidate_id": cand_id,
        "generation": generation,
        "parent_id": parent["candidate_id"] if parent else None,
        "parent_sha256": (parent.get("artifact") or {}).get("sha256") if parent else None,
        "artifact": {"path": rel_path, "sha256": digest},
        "mutation": {
            "provider": mutation.get("provider", "seed"),
            "operator": mutation.get("operator", "seed"),
            "seed": mutation.get("seed"),
            "description": mutation.get("description", "seed policy"),
            "meta": mutation.get("meta"),
        },
        "search": {
            "execution": {
                "status": execution["status"],
                "exit_code": execution["exit_code"],
                "elapsed_ms": execution["elapsed_ms"],
                "stdout": execution["stdout"],
                "stderr": execution["stderr"],
            },
            "evaluation": {
                "status": evaluation["status"],
                "score": evaluation.get("score"),
                "reason": evaluation.get("reason"),
                "orderings": evaluation.get("orderings"),
                "fault_traces": evaluation.get("fault_traces") or [],
            },
            "behavior": evaluation.get("behavior"),
        },
        "archive": archive_info,
    }
    return record


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=True, sort_keys=False) + "\n")


def format_candidate_line(record: dict[str, Any]) -> str:
    cid = record["candidate_id"]
    parent = record.get("parent_id") or "seed"
    status = record["search"]["evaluation"]["status"]
    score = record["search"]["evaluation"].get("score")
    behavior = record["search"].get("behavior") or {}
    cell = behavior.get("cell")
    archive = record.get("archive") or {}
    decision = archive.get("decision")

    score_part = f"search={score:.3f}" if isinstance(score, (int, float)) else "search=-"
    if cell:
        cell_part = f"cell={cell[0]}/{cell[1]}"
    else:
        cell_part = "cell=-"

    if status == "ok" or status == "valid":
        status = "ok"
    reason = record["search"]["evaluation"].get("reason")
    if status == "invalid" and reason:
        extra = f"  {reason}"
    elif status == "timeout":
        elapsed = record["search"]["execution"].get("elapsed_ms")
        if elapsed is not None:
            extra = f"  after {elapsed / 1000:.1f}s"
        else:
            extra = "  after timeout"
    elif status == "crash":
        extra = f"  {reason}" if reason else ""
    else:
        extra = ""

    archive_part = ""
    if decision == "inserted":
        archive_part = "  retained"
    elif decision == "replaced":
        archive_part = f"  retained; replaced {archive.get('replaced_candidate_id')}"
    elif decision == "rejected":
        archive_part = "  not retained"
    elif decision == "ineligible":
        archive_part = "  ineligible"

    arrow_parent = parent if parent != "seed" or record["generation"] > 0 else "-"
    if record["generation"] == 0:
        arrow_parent = "-"

    return f"{cid} <- {arrow_parent}  {status:<8} {score_part}  {cell_part}{archive_part}{extra}"


def evolve(
    mission_path: Path,
    *,
    budget: int | None = None,
    output: Path | None = None,
    variation_command: str | None = None,
) -> dict[str, Any]:
    mission = tp.load_mission(mission_path.resolve())
    benchmark = tp.load_benchmark_module(mission["_benchmark_path"])
    search_faults = tp.load_fault_ids(mission["_search_path"])
    timeout = float(mission["candidate_timeout_seconds"])
    variation_seed = int(mission["variation_seed"])
    descendant_budget = int(mission["descendant_budget"] if budget is None else budget)

    run_id = output.name if output is not None else _utc_run_id()
    run_dir = (output if output is not None else Path("runs") / run_id).resolve()
    if run_dir.exists():
        shutil.rmtree(run_dir)
    (run_dir / "candidates").mkdir(parents=True, exist_ok=True)
    jsonl_path = run_dir / "candidates.jsonl"

    executed_command = " ".join(
        ["detonator", "evolve", str(mission_path)]
        + ([f"--budget", str(descendant_budget)] if budget is not None else [])
        + ([f"--output", str(run_dir)] if output is not None else [])
        + ([f"--variation-command", variation_command] if variation_command else [])
    )

    # --- seed ---
    seed_source = mission["_seed_path"].read_text(encoding="utf-8")
    seed_result = evaluate_candidate_source(seed_source, benchmark, search_faults, timeout)
    seed_record = materialize_candidate(
        run_dir=run_dir,
        index=0,
        source_text=seed_source,
        generation=0,
        parent=None,
        mutation={
            "provider": "seed",
            "operator": "seed",
            "seed": variation_seed,
            "description": "mission seed",
            "meta": {"family": "seed"},
        },
        search_result=seed_result,
        archive_info=None,
    )
    append_jsonl(jsonl_path, seed_record)
    records = [seed_record]

    seed_score = seed_record["search"]["evaluation"].get("score")
    print(format_candidate_line(seed_record))

    if descendant_budget <= 0:
        summary = {
            "schema_version": 1,
            "run_id": run_dir.name,
            "executed_command": executed_command,
            "variation_seed": variation_seed,
            "python_version": sys.version.split()[0],
            "paths": {
                "run_dir": str(run_dir),
                "candidates_jsonl": str(jsonl_path.relative_to(run_dir)),
            },
            "hashes": {
                "mission": tp.sha256_file(mission["_mission_path"]),
                "benchmark": tp.sha256_file(mission["_benchmark_path"]),
                "search": tp.sha256_file(mission["_search_path"]),
            },
            "seed": {
                "candidate_id": seed_record["candidate_id"],
                "search_score": seed_score,
            },
            "descendants": {
                "attempts": 0,
                "unique_sources": 0,
                "valid": 0,
                "invalid": 0,
                "crash": 0,
                "timeout": 0,
            },
        }
        (run_dir / "summary.json").write_text(
            json.dumps(summary, indent=2) + "\n", encoding="utf-8"
        )
        print(f"seed search score: {seed_score:.6f}" if seed_score is not None else "seed search score: -")
        print(f"run directory: {run_dir}")
        return {"run_dir": run_dir, "records": records, "summary": summary}

    seen_hashes: set[str] = {seed_record["artifact"]["sha256"]}
    valid_records: list[dict[str, Any]] = []
    if seed_record["search"]["evaluation"]["status"] == "valid":
        valid_records.append(seed_record)

    counts = {"valid": 0, "invalid": 0, "crash": 0, "timeout": 0}

    for proposal_index in range(descendant_budget):
        parent = _select_parent(valid_records, proposal_index)
        proposal = _propose_descendant(
            proposal_index=proposal_index,
            variation_seed=variation_seed,
            parent=parent,
            occupied_cells=[],
            variation_command=variation_command,
            timeout_seconds=timeout,
            seen_hashes=seen_hashes,
        )
        source_text = proposal["source"]
        # Guarantee source uniqueness for offline schedule collisions.
        source_hash = tp.sha256_text(_normalize_source(source_text))
        attempt = 0
        while source_hash in seen_hashes:
            attempt += 1
            source_text = source_text.rstrip() + f"\n# unique_nudge_{proposal_index}_{attempt}\n"
            source_hash = tp.sha256_text(_normalize_source(source_text))
        seen_hashes.add(source_hash)

        search_result = evaluate_candidate_source(
            source_text, benchmark, search_faults, timeout
        )
        generation = (parent["generation"] + 1) if parent else 1
        mutation = {
            "provider": proposal.get("provider", "offline"),
            "operator": proposal.get("operator", "mutate"),
            "seed": variation_seed,
            "description": proposal.get("description", ""),
            "meta": proposal.get("meta"),
        }
        record = materialize_candidate(
            run_dir=run_dir,
            index=proposal_index + 1,
            source_text=source_text,
            generation=generation,
            parent=parent,
            mutation=mutation,
            search_result=search_result,
            archive_info=None,
        )
        status = record["search"]["evaluation"]["status"]
        if status in counts:
            counts[status] += 1
        append_jsonl(jsonl_path, record)
        records.append(record)
        if status == "valid":
            valid_records.append(record)
        print(format_candidate_line(record))

    descendant_records = records[1:]
    unique_sources = len({r["artifact"]["sha256"] for r in descendant_records})
    summary = {
        "schema_version": 1,
        "run_id": run_dir.name,
        "executed_command": executed_command,
        "variation_seed": variation_seed,
        "python_version": sys.version.split()[0],
        "paths": {
            "run_dir": str(run_dir),
            "candidates_jsonl": str(jsonl_path.relative_to(run_dir)),
        },
        "hashes": {
            "mission": tp.sha256_file(mission["_mission_path"]),
            "benchmark": tp.sha256_file(mission["_benchmark_path"]),
            "search": tp.sha256_file(mission["_search_path"]),
        },
        "seed": {
            "candidate_id": seed_record["candidate_id"],
            "search_score": seed_score,
        },
        "descendants": {
            "attempts": len(descendant_records),
            "unique_sources": unique_sources,
            "valid": counts["valid"],
            "invalid": counts["invalid"],
            "crash": counts["crash"],
            "timeout": counts["timeout"],
        },
    }
    (run_dir / "summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )

    print()
    print(f"seed search score: {seed_score:.6f}" if seed_score is not None else "seed search score: -")
    print(
        "descendants: "
        f"attempts={counts['valid'] + counts['invalid'] + counts['crash'] + counts['timeout']} "
        f"valid={counts['valid']} invalid={counts['invalid']} "
        f"crash={counts['crash']} timeout={counts['timeout']}"
    )
    print(f"run directory: {run_dir}")
    return {"run_dir": run_dir, "records": records, "summary": summary}


def _normalize_source(text: str) -> str:
    normalized = text.replace("\r\n", "\n")
    if not normalized.endswith("\n"):
        normalized += "\n"
    return normalized


def _select_parent(valid_records: list[dict[str, Any]], proposal_index: int) -> dict[str, Any]:
    """Pick a parent from valid candidates (seed included)."""
    if not valid_records:
        raise RuntimeError("no valid parents available")
    # Early proposals explore from the seed; later ones cycle retained valids.
    if proposal_index < 6:
        return valid_records[0]
    return valid_records[proposal_index % len(valid_records)]


def _propose_descendant(
    *,
    proposal_index: int,
    variation_seed: int,
    parent: dict[str, Any],
    occupied_cells: list[list[str]],
    variation_command: str | None,
    timeout_seconds: float,
    seen_hashes: set[str],
) -> dict[str, Any]:
    if variation_command:
        raise RuntimeError("external variation-command support lands in a later commit")
    proposal = tp.generate_offline_descendant(
        proposal_index=proposal_index,
        variation_seed=variation_seed,
        parent=parent,
        occupied_cells=occupied_cells,
    )
    # Embed proposal index so distinct schedule slots never hash-collide.
    source = proposal["source"].rstrip() + f"\n# variant_slot={proposal_index}\n"
    proposal = dict(proposal)
    proposal["source"] = source
    return proposal
