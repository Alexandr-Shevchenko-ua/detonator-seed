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
    payload = {k: v for k, v in record.items() if not k.startswith("_")}
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, ensure_ascii=True, sort_keys=False) + "\n")


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
    seed_record["_source_text"] = seed_source
    records = [seed_record]

    seed_score = seed_record["search"]["evaluation"].get("score")
    archive: dict[tuple[str, str], dict[str, Any]] = {}
    seed_record["archive"] = _archive_consider(archive, seed_record)
    append_jsonl(jsonl_path, seed_record)
    print(format_candidate_line(seed_record))

    holdout_gate = tp.HoldoutGate(mission["_holdout_path"])

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
        parent = _select_parent(archive, valid_records, proposal_index)
        occupied = [[k[0], k[1]] for k in sorted(archive.keys())]
        proposal = _propose_descendant(
            proposal_index=proposal_index,
            variation_seed=variation_seed,
            parent=parent,
            occupied_cells=occupied,
            variation_command=variation_command,
            timeout_seconds=timeout,
            seen_hashes=seen_hashes,
        )
        source_text = proposal["source"]
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
        provisional = materialize_candidate(
            run_dir=run_dir,
            index=proposal_index + 1,
            source_text=source_text,
            generation=generation,
            parent=parent,
            mutation=mutation,
            search_result=search_result,
            archive_info=None,
        )
        provisional["_source_text"] = source_text
        archive_info = _archive_consider(archive, provisional)
        provisional["archive"] = archive_info
        status = provisional["search"]["evaluation"]["status"]
        if status in counts:
            counts[status] += 1
        append_jsonl(jsonl_path, provisional)
        records.append(provisional)
        if status == "valid":
            valid_records.append(provisional)
        print(format_candidate_line(provisional))

    # Freeze archive before any holdout access.
    archive_body = _freeze_archive(archive, records[-1]["candidate_id"])
    archive_body_bytes = json.dumps(archive_body, indent=2, sort_keys=True) + "\n"
    archive_hash = tp.sha256_text(archive_body_bytes)
    archive_doc = dict(archive_body)
    archive_doc["archive_sha256"] = archive_hash
    archive_path = run_dir / "archive.json"
    archive_path.write_text(
        json.dumps(archive_doc, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    holdout_gate.mark_frozen()
    holdout_faults = holdout_gate.load_fault_ids()
    winner_ids = [cell["candidate_id"] for cell in archive_doc["cells"] if cell.get("candidate_id")]
    evaluate_ids = []
    seen_eval = set()
    for cid in [seed_record["candidate_id"], *winner_ids]:
        if cid not in seen_eval:
            evaluate_ids.append(cid)
            seen_eval.add(cid)

    id_to_record = {r["candidate_id"]: r for r in records}
    holdout_results = []
    for cid in evaluate_ids:
        record = id_to_record[cid]
        source_text = (run_dir / record["artifact"]["path"]).read_text(encoding="utf-8")
        result = evaluate_candidate_source(source_text, benchmark, holdout_faults, timeout)
        evaluation = _normalize_eval_reason(result["evaluation"])
        holdout_results.append(
            {
                "candidate_id": cid,
                "execution": {
                    "status": result["execution"]["status"],
                    "exit_code": result["execution"]["exit_code"],
                    "elapsed_ms": result["execution"]["elapsed_ms"],
                    "stdout": result["execution"]["stdout"],
                    "stderr": result["execution"]["stderr"],
                },
                "evaluation": {
                    "status": evaluation["status"],
                    "score": evaluation.get("score"),
                    "reason": evaluation.get("reason"),
                    "fault_traces": evaluation.get("fault_traces") or [],
                },
            }
        )

    seed_holdout = next(h for h in holdout_results if h["candidate_id"] == seed_record["candidate_id"])
    seed_holdout_score = seed_holdout["evaluation"].get("score")
    scored = [h for h in holdout_results if isinstance(h["evaluation"].get("score"), (int, float))]
    if scored:
        best = max(scored, key=lambda h: h["evaluation"]["score"])
        best_id = best["candidate_id"]
        best_score = float(best["evaluation"]["score"])
    else:
        best_id = seed_record["candidate_id"]
        best_score = seed_holdout_score
    delta = None
    improved = False
    if isinstance(best_score, (int, float)) and isinstance(seed_holdout_score, (int, float)):
        delta = best_score - seed_holdout_score
        improved = delta > 0

    holdout_doc = {
        "schema_version": 1,
        "frozen_archive": {
            "path": "archive.json",
            "sha256": archive_doc["archive_sha256"],
            "frozen_after_candidate_id": archive_doc["frozen_after_candidate_id"],
        },
        "evaluated_ids": evaluate_ids,
        "candidates": holdout_results,
        "seed_score": seed_holdout_score,
        "best_observed_candidate_id": best_id,
        "best_observed_score": best_score,
        "delta": delta,
        "improved_over_seed": improved,
    }
    holdout_path = run_dir / "holdout.json"
    holdout_path.write_text(json.dumps(holdout_doc, indent=2) + "\n", encoding="utf-8")

    descendant_records = records[1:]
    unique_sources = len({r["artifact"]["sha256"] for r in descendant_records})
    lineage = _lineage_ids(id_to_record, best_id)
    summary = {
        "schema_version": 1,
        "run_id": run_dir.name,
        "executed_command": executed_command,
        "variation_seed": variation_seed,
        "python_version": sys.version.split()[0],
        "paths": {
            "run_dir": str(run_dir),
            "candidates_jsonl": "candidates.jsonl",
            "archive": "archive.json",
            "holdout": "holdout.json",
        },
        "hashes": {
            "mission": tp.sha256_file(mission["_mission_path"]),
            "benchmark": tp.sha256_file(mission["_benchmark_path"]),
            "search": tp.sha256_file(mission["_search_path"]),
            "holdout": tp.sha256_file(mission["_holdout_path"]),
            "archive": archive_doc["archive_sha256"],
        },
        "seed": {
            "candidate_id": seed_record["candidate_id"],
            "search_score": seed_score,
            "holdout_score": seed_holdout_score,
        },
        "descendants": {
            "attempts": len(descendant_records),
            "unique_sources": unique_sources,
            "valid": counts["valid"],
            "invalid": counts["invalid"],
            "crash": counts["crash"],
            "timeout": counts["timeout"],
        },
        "archive": {
            "occupied_cells": [
                {
                    "cell": cell["cell"],
                    "candidate_id": cell.get("candidate_id"),
                    "search_score": cell.get("search_score"),
                }
                for cell in archive_doc["cells"]
            ]
        },
        "holdout": {
            "evaluated_ids": evaluate_ids,
            "seed_score": seed_holdout_score,
            "best_observed_candidate_id": best_id,
            "best_observed_score": best_score,
            "delta": delta,
            "improved_over_seed": improved,
            "conclusion": (
                "improved over seed" if improved else "no holdout improvement found"
            ),
        },
        "best_observed_lineage": lineage,
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
    print("archive cells:")
    for cell in archive_doc["cells"]:
        cid = cell.get("candidate_id") or "-"
        score = cell.get("search_score")
        score_s = f"{score:.3f}" if isinstance(score, (int, float)) else "-"
        print(f"  {cell['cell'][0]}/{cell['cell'][1]}: {cid} search={score_s}")
    print("holdout:")
    for item in holdout_results:
        score = item["evaluation"].get("score")
        score_s = f"{score:.6f}" if isinstance(score, (int, float)) else "-"
        print(f"  {item['candidate_id']}: {score_s}")
    if isinstance(best_score, (int, float)) and isinstance(delta, (int, float)):
        print(
            f"best observed holdout: {best_id} score={best_score:.6f} "
            f"delta={delta:+.6f}"
        )
    print(summary["holdout"]["conclusion"])
    print(f"lineage: {' -> '.join(lineage)}")
    print(f"run directory: {run_dir}")
    return {
        "run_dir": run_dir,
        "records": records,
        "summary": summary,
        "archive": archive_doc,
        "holdout": holdout_doc,
        "holdout_gate": holdout_gate,
    }


def _normalize_source(text: str) -> str:
    normalized = text.replace("\r\n", "\n")
    if not normalized.endswith("\n"):
        normalized += "\n"
    return normalized


def _archive_consider(
    archive: dict[tuple[str, str], dict[str, Any]],
    record: dict[str, Any],
) -> dict[str, Any]:
    evaluation = record["search"]["evaluation"]
    behavior = record["search"].get("behavior")
    if evaluation.get("status") != "valid" or not behavior or not behavior.get("cell"):
        return {
            "decision": "ineligible",
            "cell": None,
            "replaced_candidate_id": None,
            "reason": evaluation.get("status") or "ineligible",
        }
    cell = (behavior["cell"][0], behavior["cell"][1])
    score = float(evaluation["score"])
    if cell not in archive:
        archive[cell] = {"record": record, "score": score}
        return {
            "decision": "inserted",
            "cell": [cell[0], cell[1]],
            "replaced_candidate_id": None,
            "reason": "empty_cell",
        }
    incumbent = archive[cell]
    if score > incumbent["score"]:
        replaced = incumbent["record"]["candidate_id"]
        archive[cell] = {"record": record, "score": score}
        return {
            "decision": "replaced",
            "cell": [cell[0], cell[1]],
            "replaced_candidate_id": replaced,
            "reason": "higher_search_score",
        }
    return {
        "decision": "rejected",
        "cell": [cell[0], cell[1]],
        "replaced_candidate_id": None,
        "reason": "not_strictly_better",
    }


def _freeze_archive(
    archive: dict[tuple[str, str], dict[str, Any]],
    frozen_after_candidate_id: str,
) -> dict[str, Any]:
    cells = []
    for cell in tp.ARCHIVE_CELLS:
        entry = archive.get(cell)
        if entry is None:
            cells.append(
                {
                    "cell": [cell[0], cell[1]],
                    "candidate_id": None,
                    "artifact": None,
                    "search_score": None,
                    "behavior": None,
                }
            )
            continue
        record = entry["record"]
        cells.append(
            {
                "cell": [cell[0], cell[1]],
                "candidate_id": record["candidate_id"],
                "artifact": record["artifact"],
                "search_score": entry["score"],
                "behavior": record["search"]["behavior"],
            }
        )
    return {
        "schema_version": 1,
        "frozen_after_candidate_id": frozen_after_candidate_id,
        "cells": cells,
    }


def _lineage_ids(
    id_to_record: dict[str, dict[str, Any]],
    candidate_id: str,
) -> list[str]:
    chain: list[str] = []
    current: str | None = candidate_id
    seen: set[str] = set()
    while current is not None and current not in seen:
        seen.add(current)
        chain.append(current)
        record = id_to_record.get(current)
        if record is None:
            break
        current = record.get("parent_id")
    chain.reverse()
    return chain


def _select_parent(
    archive: dict[tuple[str, str], dict[str, Any]],
    valid_records: list[dict[str, Any]],
    proposal_index: int,
) -> dict[str, Any]:
    """Round-robin parents from sorted occupied archive cells when available."""
    occupied = sorted(archive.keys())
    if occupied:
        cell = occupied[proposal_index % len(occupied)]
        return archive[cell]["record"]
    if not valid_records:
        raise RuntimeError("no valid parents available")
    return valid_records[0]


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
        return _propose_via_command(
            variation_command=variation_command,
            proposal_index=proposal_index,
            variation_seed=variation_seed,
            parent=parent,
            occupied_cells=occupied_cells,
            timeout_seconds=timeout_seconds,
        )
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


def _propose_via_command(
    *,
    variation_command: str,
    proposal_index: int,
    variation_seed: int,
    parent: dict[str, Any],
    occupied_cells: list[list[str]],
    timeout_seconds: float,
) -> dict[str, Any]:
    import shlex

    parent_source = parent.get("_source_text")
    if not parent_source:
        raise RuntimeError(
            f"parent {parent.get('candidate_id')} missing in-memory source for variation-command"
        )

    behavior = parent.get("search", {}).get("behavior") or {}
    target_cell = behavior.get("cell")
    if not target_cell and occupied_cells:
        target_cell = occupied_cells[proposal_index % len(occupied_cells)]

    evaluation = parent.get("search", {}).get("evaluation") or {}
    request = {
        "protocol_version": 1,
        "proposal_index": proposal_index,
        "run_seed": variation_seed,
        "parent": {
            "candidate_id": parent["candidate_id"],
            "sha256": (parent.get("artifact") or {}).get("sha256"),
            "source": parent_source,
        },
        "target_cell": target_cell,
        "search_feedback": {
            "score": evaluation.get("score"),
            "fault_traces": evaluation.get("fault_traces") or [],
            "behavior": behavior,
        },
        "archive": {"occupied_cells": occupied_cells},
        "artifact_contract": {
            "entrypoint": "prioritize",
            "required_output": "exact permutation of test ids",
        },
    }
    # Explicitly omit holdout fields.
    assert "holdout" not in request

    argv = shlex.split(variation_command)
    try:
        completed = subprocess.run(
            argv,
            input=json.dumps(request),
            text=True,
            capture_output=True,
            timeout=max(timeout_seconds, 5.0),
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"variation-command timed out after {max(timeout_seconds, 5.0):.1f}s: "
            f"{variation_command}"
        ) from exc
    except OSError as exc:
        raise RuntimeError(f"variation-command failed to start: {exc}") from exc

    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        raise RuntimeError(
            f"variation-command exited {completed.returncode}: {stderr or completed.stdout}"
        )
    try:
        response = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"variation-command returned invalid JSON: {completed.stdout!r}"
        ) from exc
    source = response.get("source")
    if not isinstance(source, str) or not source.strip():
        raise RuntimeError("variation-command response missing source string")
    description = response.get("mutation_description") or "external proposal"
    return {
        "source": source.rstrip() + f"\n# external_slot={proposal_index}\n",
        "provider": "external",
        "operator": "external_command",
        "description": description,
        "meta": {"family": "external", "command": variation_command},
    }


def load_run_records(run_dir: Path) -> list[dict[str, Any]]:
    records = []
    with (run_dir / "candidates.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def inspect_run(
    run_dir: Path,
    *,
    verify: bool = False,
    replay_retained: bool = False,
    mission_path: Path | None = None,
) -> int:
    """Inspect a prior run. Returns process exit code."""
    run_dir = run_dir.resolve()
    if not run_dir.is_dir():
        print(f"run directory not found: {run_dir}", file=sys.stderr)
        return 2

    records = load_run_records(run_dir)
    id_to_record = {r["candidate_id"]: r for r in records}
    archive = json.loads((run_dir / "archive.json").read_text(encoding="utf-8"))
    holdout = json.loads((run_dir / "holdout.json").read_text(encoding="utf-8"))
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))

    retained = [c for c in archive["cells"] if c.get("candidate_id")]
    print(f"run: {run_dir}")
    print(f"candidates: {len(records)} (seed + {max(0, len(records) - 1)} descendants)")
    print("retained lineages:")
    for cell in retained:
        cid = cell["candidate_id"]
        lineage = _lineage_ids(id_to_record, cid)
        if lineage[0] != records[0]["candidate_id"]:
            print(f"  ERROR: lineage for {cid} does not end at seed", file=sys.stderr)
            return 1
        print(f"  {cell['cell'][0]}/{cell['cell'][1]}: {' -> '.join(lineage)}")

    mismatches: list[str] = []

    if verify:
        print("verify:")
        for record in records:
            path = run_dir / record["artifact"]["path"]
            if not path.is_file():
                mismatches.append(f"missing artifact {record['artifact']['path']}")
                continue
            digest = tp.sha256_file(path)
            if digest != record["artifact"]["sha256"]:
                mismatches.append(
                    f"{record['candidate_id']} hash mismatch: "
                    f"stored={record['artifact']['sha256']} actual={digest}"
                )
            else:
                print(f"  {record['candidate_id']} ok {digest[:12]}…")

        body = {k: v for k, v in archive.items() if k != "archive_sha256"}
        body_bytes = json.dumps(body, indent=2, sort_keys=True) + "\n"
        archive_digest = tp.sha256_text(body_bytes)
        if archive_digest != archive.get("archive_sha256"):
            mismatches.append(
                f"archive hash mismatch: stored={archive.get('archive_sha256')} "
                f"actual={archive_digest}"
            )
        else:
            print(f"  archive.json ok {archive_digest[:12]}…")

        if holdout["frozen_archive"]["sha256"] != archive.get("archive_sha256"):
            mismatches.append("holdout frozen archive hash does not match archive.json")

    if replay_retained:
        print("replay-retained:")
        mission_path = mission_path or Path("examples/test_priority/mission.json")
        if not mission_path.is_file():
            # Fall back to summary-relative default when invoked from repo root.
            mismatches.append(f"mission not found for replay: {mission_path}")
        else:
            mission = tp.load_mission(mission_path.resolve())
            benchmark = tp.load_benchmark_module(mission["_benchmark_path"])
            search_faults = tp.load_fault_ids(mission["_search_path"])
            holdout_faults = tp.load_fault_ids(mission["_holdout_path"])
            timeout = float(mission["candidate_timeout_seconds"])

            replay_ids = []
            seen = set()
            for cid in [records[0]["candidate_id"], *[c["candidate_id"] for c in retained]]:
                if cid not in seen:
                    replay_ids.append(cid)
                    seen.add(cid)

            holdout_by_id = {h["candidate_id"]: h for h in holdout["candidates"]}

            for cid in replay_ids:
                record = id_to_record[cid]
                source = (run_dir / record["artifact"]["path"]).read_text(encoding="utf-8")
                search_replay = evaluate_candidate_source(
                    source, benchmark, search_faults, timeout
                )
                search_eval = _normalize_eval_reason(search_replay["evaluation"])
                stored_search = record["search"]["evaluation"]
                stored_behavior = record["search"].get("behavior")

                if search_eval.get("status") != stored_search.get("status"):
                    mismatches.append(
                        f"{cid} search status: stored={stored_search.get('status')} "
                        f"replay={search_eval.get('status')}"
                    )
                if search_eval.get("score") != stored_search.get("score"):
                    # Allow tiny float noise; scores are rational over 43.
                    s1 = search_eval.get("score")
                    s2 = stored_search.get("score")
                    if s1 is None or s2 is None or abs(float(s1) - float(s2)) > 1e-12:
                        mismatches.append(
                            f"{cid} search score: stored={s2} replay={s1}"
                        )
                replay_behavior = search_eval.get("behavior")
                if replay_behavior != stored_behavior:
                    mismatches.append(
                        f"{cid} behavior: stored={stored_behavior} replay={replay_behavior}"
                    )
                if search_eval.get("orderings") != stored_search.get("orderings"):
                    mismatches.append(f"{cid} search orderings differ")

                holdout_replay = evaluate_candidate_source(
                    source, benchmark, holdout_faults, timeout
                )
                holdout_eval = _normalize_eval_reason(holdout_replay["evaluation"])
                stored_holdout = holdout_by_id[cid]["evaluation"]
                if holdout_eval.get("score") != stored_holdout.get("score"):
                    s1 = holdout_eval.get("score")
                    s2 = stored_holdout.get("score")
                    if s1 is None or s2 is None or abs(float(s1) - float(s2)) > 1e-12:
                        mismatches.append(
                            f"{cid} holdout score: stored={s2} replay={s1}"
                        )
                print(
                    f"  {cid} search={search_eval.get('score')} "
                    f"holdout={holdout_eval.get('score')} "
                    f"cell={replay_behavior.get('cell') if replay_behavior else None}"
                )

    print("summary:")
    print(f"  conclusion: {summary['holdout']['conclusion']}")
    print(f"  best observed: {summary['holdout']['best_observed_candidate_id']}")
    print(f"  lineage: {' -> '.join(summary.get('best_observed_lineage') or [])}")

    if mismatches:
        print("mismatches:", file=sys.stderr)
        for item in mismatches:
            print(f"  - {item}", file=sys.stderr)
        return 1
    print("inspect ok")
    return 0
