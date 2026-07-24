"""DS-002 real-mutation corpus build and verify adapter."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from contextlib import redirect_stdout
from io import StringIO
from typing import Any, Literal

from mutmut.__main__ import get_diff_for_mutant
from mutmut.configuration import Config
from mutmut.utils.file_utils import change_cwd

MUTMUT_VERSION = "3.6.0"
OPERATOR_FAMILIES = (
    "comparison_boundary",
    "boolean_negation",
    "arithmetic_index_counter",
    "guard_return",
)
CellOutcome = Literal[
    "pass",
    "killed",
    "timeout",
    "collection_error",
    "infrastructure_error",
]
PipelinePhase = Literal["init", "selection", "split_frozen", "matrix", "done"]


class OutcomeOrderingViolation(RuntimeError):
    """Raised when selection/split logic attempts to read matrix outcomes."""


class DomainRejected(RuntimeError):
    """Raised when corpus hard gates fail (fail-closed)."""


@dataclass
class BuildContext:
    """Tracks pipeline phase and artifact write order."""

    phase: PipelinePhase = "init"
    write_log: list[str] = field(default_factory=list)

    def enter_selection(self) -> None:
        self.phase = "selection"

    def freeze_split(self) -> None:
        self.phase = "split_frozen"

    def enter_matrix(self) -> None:
        if self.phase != "split_frozen":
            raise OutcomeOrderingViolation(
                "matrix artifacts may be written only after split is frozen"
            )
        self.phase = "matrix"

    def record_write(self, artifact: str) -> None:
        if artifact.endswith("matrix.jsonl") and self.phase != "matrix":
            raise OutcomeOrderingViolation(
                f"refusing to write {artifact} before split freeze"
            )
        self.write_log.append(artifact)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_text(path.read_text(encoding="utf-8"))


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _assert_pre_outcome_phase(ctx: BuildContext, *, operation: str) -> None:
    if ctx.phase not in ("init", "selection"):
        raise OutcomeOrderingViolation(
            f"{operation} must run before split freeze (phase={ctx.phase})"
        )


def read_matrix_outcomes_for_selection() -> None:
    """Guard entry: selection/split must never call this."""
    raise OutcomeOrderingViolation(
        "selection and split must not read matrix outcomes or kill signatures"
    )


def classify_operator(patch: str) -> str:
    """Classify a canonical mutmut patch into an operator family."""
    lines = [ln for ln in patch.splitlines() if ln.startswith(("+", "-")) and not ln.startswith(("+++", "---"))]
    delta = "\n".join(lines)
    lowered = delta.lower()

    if re.search(r"[<>!=]=|[<>](?!=)|\bis\b|\bin\b", delta):
        return "comparison_boundary"
    if re.search(r"\bnot\b|\band\b|\bor\b|\btrue\b|\bfalse\b", lowered):
        return "boolean_negation"
    if re.search(r"^\+\s*return\b|^\-\s*return\b", delta, re.MULTILINE):
        return "guard_return"
    if re.search(r"^\+\s*if\b|^\-\s*if\b|^\+\s*raise\b|^\-\s*raise\b", delta, re.MULTILINE):
        return "guard_return"
    if re.search(r"[\+\-\*/%]|//|\[|\]|\+\=|\-\=", delta):
        return "arithmetic_index_counter"
    return "guard_return"


def extract_qualified_symbol(mutmut_id: str, patch: str) -> str:
    """Derive qualified symbol from mutmut id and patch context."""
    if "HoldoutGate" in mutmut_id or "HoldoutGate" in patch:
        for name in ("load_fault_ids", "mark_frozen", "is_frozen"):
            if name in mutmut_id or name in patch:
                return f"HoldoutGate.{name}"
        return "HoldoutGate"

    token = mutmut_id.split(".")[-1]
    if token.startswith("x_"):
        return token[2:].split("_x")[0].split("__")[0]
    if "ǁ" in token:
        parts = token.split("ǁ")
        if len(parts) >= 3:
            return f"{parts[1]}.{parts[2].split('_')[0]}"
    for symbol in (
        "run_candidate_subprocess",
        "evaluate_candidate_source",
        "materialize_candidate",
        "compute_run_metrics",
        "resolve_run_dir",
        "evolve",
        "inspect_run",
        "load_mission",
        "load_benchmark_module",
        "validate_ordering",
        "evaluate_orderings",
        "compute_behavior",
        "purge_holdout_modules",
        "_archive_consider",
        "_freeze_archive",
        "_lineage_ids",
        "_select_parent",
        "_propose_via_command",
    ):
        if symbol in mutmut_id or symbol in patch:
            return symbol
    return token


def _symbol_allowed(symbol: str, mission: dict[str, Any]) -> bool:
    search = _flatten_symbol_groups(mission.get("search_symbol_groups", []))
    holdout = _flatten_symbol_groups(mission.get("holdout_symbol_groups", []))
    return symbol in search or symbol in holdout


def _allowlisted_symbols(mission: dict[str, Any]) -> set[str]:
    return _flatten_symbol_groups(mission.get("search_symbol_groups", [])) | _flatten_symbol_groups(
        mission.get("holdout_symbol_groups", [])
    )


def _mutmut_id_might_match_allowlist(mutant_id: str, mission: dict[str, Any]) -> bool:
    """Cheap pre-filter before expensive patch extraction."""
    return any(symbol in mutant_id for symbol in _allowlisted_symbols(mission))


def _flatten_symbol_groups(groups: list[list[str]]) -> set[str]:
    out: set[str] = set()
    for group in groups:
        out.update(group)
    return out


def apply_selection_caps(
    mutants: list[dict[str, Any]],
    *,
    per_symbol_operator: int = 4,
    per_symbol_total: int = 12,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Deterministic pre-outcome selection with caps."""
    ordered = sorted(mutants, key=lambda m: m["patch_sha256"])
    selected: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    per_pair: dict[tuple[str, str], int] = defaultdict(int)
    per_symbol: dict[str, int] = defaultdict(int)

    for mutant in ordered:
        symbol = mutant["qualified_symbol"]
        family = mutant["operator_family"]
        pair_key = (symbol, family)
        reason: str | None = None
        if per_pair[pair_key] >= per_symbol_operator:
            reason = f"cap exceeded for ({symbol}, {family})"
        elif per_symbol[symbol] >= per_symbol_total:
            reason = f"cap exceeded for symbol {symbol}"
        if reason:
            rejected.append({**mutant, "reason": reason})
            continue
        per_pair[pair_key] += 1
        per_symbol[symbol] += 1
        selected.append(mutant)
    return selected, rejected


def assign_split(
    selected: list[dict[str, Any]],
    mission: dict[str, Any],
) -> dict[str, Any]:
    """Assign selected mutants to search/holdout by symbol groups (zero overlap)."""
    search_symbols = _flatten_symbol_groups(mission["search_symbol_groups"])
    holdout_symbols = _flatten_symbol_groups(mission["holdout_symbol_groups"])
    overlap = search_symbols & holdout_symbols
    if overlap:
        raise ValueError(f"search/holdout symbol overlap: {sorted(overlap)}")

    search_ids: list[str] = []
    holdout_ids: list[str] = []
    for mutant in selected:
        symbol = mutant["qualified_symbol"]
        mid = mutant["mutmut_id"]
        if symbol in search_symbols:
            search_ids.append(mid)
        elif symbol in holdout_symbols:
            holdout_ids.append(mid)
        else:
            raise ValueError(f"symbol {symbol!r} not in mission split groups")
    search_ids.sort()
    holdout_ids.sort()
    return {
        "schema_version": 1,
        "search": search_ids,
        "holdout": holdout_ids,
    }


def write_rejected_row(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")


def _write_json(path: Path, payload: dict[str, Any], ctx: BuildContext | None = None) -> str:
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    content_hash = sha256_text(text)
    payload_with_hash = {**payload, "content_sha256": content_hash}
    text = json.dumps(payload_with_hash, indent=2, sort_keys=True) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if ctx is not None:
        ctx.record_write(path.name)
    return content_hash


def evaluate_gates(
    primary: list[dict[str, Any]],
    split: dict[str, Any],
) -> dict[str, Any]:
    """Evaluate hard corpus gates (fail-closed, no side-swap)."""
    search_ids = set(split["search"])
    holdout_ids = set(split["holdout"])
    search_primary = [m for m in primary if m["mutmut_id"] in search_ids]
    holdout_primary = [m for m in primary if m["mutmut_id"] in holdout_ids]

    def side_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
        symbols = {r["qualified_symbol"] for r in rows}
        families = {r["operator_family"] for r in rows}
        signatures = [r.get("kill_signature", "") for r in rows if r.get("kill_signature")]
        unique_sigs = set(signatures)
        sig_counts: dict[str, int] = defaultdict(int)
        for sig in signatures:
            sig_counts[sig] += 1
        dominance = max(sig_counts.values()) / len(rows) if rows else 1.0
        return {
            "count": len(rows),
            "symbols": sorted(symbols),
            "families": sorted(families),
            "kill_signatures": sorted(unique_sigs),
            "max_signature_fraction": dominance,
        }

    search_stats = side_stats(search_primary)
    holdout_stats = side_stats(holdout_primary)
    failures: list[str] = []
    if len(primary) < 30:
        failures.append(f"primary count {len(primary)} < 30")
    if search_stats["count"] < 20:
        failures.append(f"search primary {search_stats['count']} < 20")
    if holdout_stats["count"] < 10:
        failures.append(f"holdout primary {holdout_stats['count']} < 10")
    for side_name, stats in (("search", search_stats), ("holdout", holdout_stats)):
        if len(stats["symbols"]) < 3:
            failures.append(f"{side_name} symbols {len(stats['symbols'])} < 3")
        if len(stats["families"]) < 3:
            failures.append(f"{side_name} operator families {len(stats['families'])} < 3")
        if len(stats["kill_signatures"]) < 3:
            failures.append(f"{side_name} kill signatures {len(stats['kill_signatures'])} < 3")
        if stats["max_signature_fraction"] > 0.5:
            failures.append(f"{side_name} signature dominance {stats['max_signature_fraction']:.2f} > 0.5")
    if len(search_stats["kill_signatures"]) < 6:
        failures.append(f"search kill signatures {len(search_stats['kill_signatures'])} < 6")

    return {
        "passed": not failures,
        "failures": failures,
        "primary_count": len(primary),
        "search": search_stats,
        "holdout": holdout_stats,
    }


def load_mission(mission_path: Path) -> dict[str, Any]:
    mission = json.loads(mission_path.read_text(encoding="utf-8"))
    required = (
        "search_symbol_groups",
        "holdout_symbol_groups",
        "module_allowlist",
    )
    for key in required:
        if key not in mission:
            raise ValueError(f"mission missing required field: {key}")
    return mission


def _memory_bounded_cmd(cmd: list[str], mem_limit_mb: int = 3072) -> list[str]:
    """Wrap a command in a per-process cgroup memory cap.

    Rationale: mutmut/pytest subprocesses have caused WSL-VM-wide OOM crashes
    (the whole VM became unresponsive, not just one process getting killed).
    A cgroup-scoped MemoryMax makes a runaway subprocess get cleanly SIGKILLed
    by the kernel once it exceeds the cap, instead of the host exhausting all
    RAM+swap. Falls back to the unwrapped command if systemd-run is
    unavailable (e.g. non-systemd containers) so behavior degrades gracefully.
    """
    if os.environ.get("DETONATOR_NO_MEM_CAP") or shutil.which("systemd-run") is None:
        return cmd
    return [
        "systemd-run",
        "--user",
        "--scope",
        "--quiet",
        "-p",
        f"MemoryMax={mem_limit_mb}M",
        "-p",
        "MemorySwapMax=512M",
        "-p",
        "TasksMax=256",
        "--",
        *cmd,
    ]


def _run_git(args: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def create_detached_worktree(target_sha: str) -> Path:
    """Create a detached temporary worktree at target SHA."""
    tmp = Path(tempfile.mkdtemp(prefix="ds002-worktree-"))
    _run_git(["worktree", "add", "--detach", str(tmp), target_sha])
    return tmp


def remove_worktree(path: Path) -> None:
    if not path.exists():
        return
    try:
        _run_git(["worktree", "remove", "--force", str(path)])
    except subprocess.CalledProcessError:
        subprocess.run(["rm", "-rf", str(path)], check=False)


def verify_clean_pytest(worktree: Path) -> float:
    """Run clean pytest in worktree; return total duration seconds."""
    start = datetime.now(timezone.utc)
    proc = subprocess.run(
        _memory_bounded_cmd(["uv", "run", "pytest", "-q"]),
        cwd=worktree,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"clean pytest failed in worktree (exit {proc.returncode}):\n{proc.stdout}\n{proc.stderr}"
        )
    return (datetime.now(timezone.utc) - start).total_seconds()


def collect_test_node_ids(worktree: Path) -> list[str]:
    proc = subprocess.run(
        _memory_bounded_cmd(["uv", "run", "pytest", "--collect-only", "-q"]),
        cwd=worktree,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"pytest collect failed:\n{proc.stdout}\n{proc.stderr}")
    full_ids = [
        line.strip()
        for line in proc.stdout.splitlines()
        if line.strip().startswith("tests/") and "::" in line
    ]
    return sorted(full_ids)


def _ensure_mutmut_config(worktree: Path, module_allowlist: list[str]) -> None:
    pyproject = worktree / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    only_mutate = ", ".join(f'"{p}"' for p in module_allowlist)
    block = f"""
[tool.mutmut]
source_paths = ["src"]
only_mutate = [{only_mutate}]
"""
    if "[tool.mutmut]" not in text:
        pyproject.write_text(text.rstrip() + "\n" + block, encoding="utf-8")


def _mutmut_generate(worktree: Path, workers: int) -> None:
    script = """
import os
from pathlib import Path
from mutmut.configuration import Config
from mutmut.__main__ import (
    copy_also_copy_files,
    copy_src_dir,
    create_mutants,
    setup_source_paths,
    store_lines_covered_by_tests,
)

os.environ["MUTANT_UNDER_TEST"] = "mutant_generation"
Config.reset()
Config.ensure_loaded()
Path("mutants").mkdir(exist_ok=True)
copy_src_dir()
copy_also_copy_files()
setup_source_paths()
store_lines_covered_by_tests()
create_mutants(int(os.environ.get("MUTMUT_WORKERS", "4")))
"""
    env = {**os.environ, "MUTMUT_WORKERS": str(workers)}
    proc = subprocess.run(
        _memory_bounded_cmd([sys.executable, "-c", script], mem_limit_mb=6144),
        cwd=worktree,
        env=env,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"mutmut generate failed:\n{proc.stdout}\n{proc.stderr}")


def _mutmut_bin() -> str:
    """Path to the `mutmut` console script in the orchestrator's own venv.

    The mutation-corpus target SHA intentionally does not declare `mutmut` as
    a product dependency (it is our tooling, not the product's). `uv run`
    inside a per-commit worktree resolves against that worktree's own
    pyproject.toml/uv.lock and therefore never has `mutmut` installed. We
    invoke the orchestrator's own installed `mutmut` binary directly (with
    `cwd` set to the worktree) instead, mirroring how `_mutmut_generate`
    already uses `sys.executable` rather than `uv run`.
    """
    candidate = Path(sys.executable).parent / "mutmut"
    if candidate.is_file():
        return str(candidate)
    return "mutmut"  # fall back to PATH resolution


def _mutmut_list_ids(worktree: Path) -> list[str]:
    proc = subprocess.run(
        _memory_bounded_cmd([_mutmut_bin(), "results", "--all", "true"]),
        cwd=worktree,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"mutmut results failed:\n{proc.stdout}\n{proc.stderr}")
    ids: list[str] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        mutant_id = line.split(":", 1)[0].strip()
        ids.append(mutant_id)
    return sorted(set(ids))


def _normalize_mutmut_patch(raw: str) -> str:
    lines = [ln for ln in raw.splitlines() if not ln.startswith("# ")]
    return "\n".join(lines).strip() + "\n"


def _mutmut_show_patch(worktree: Path, mutant_id: str) -> str:
    with change_cwd(worktree):
        Config.reset()
        Config.ensure_loaded()
        buffer = StringIO()
        with redirect_stdout(buffer):
            raw = get_diff_for_mutant(mutant_id)
    return _normalize_mutmut_patch(raw)


_EXTRACT_PATCHES_SCRIPT = """
import json
import re
import sys
from difflib import unified_diff

from mutmut.__main__ import (
    mangled_name_from_mutant_name,
    orig_function_and_class_names_from_key,
)

ids = json.loads(sys.argv[1])
allowlist_paths = json.loads(sys.argv[2])

# Text-based (NOT libcst) function-boundary indexing. mutmut always emits
# mutant variants as flat, top-level `def <name>(...):` blocks (methods are
# flattened with a class-separator token baked into the name), so a
# column-0 regex scan is sufficient to slice out exact function bodies.
#
# We deliberately avoid `cst.parse_module()` on the generated mutants file
# here: for a real ~25MB generated module (kernel.py x hundreds of mutants)
# libcst's full-fidelity concrete syntax tree measured 4GB+ resident memory
# for a SINGLE parse and was the actual mechanism behind repeated WSL-VM
# OOM crashes during this build. Plain line scanning is O(file size) with a
# tiny constant factor instead.
DEF_RE = re.compile(r"^def\\s+([A-Za-z_][A-Za-z0-9_]*)\\s*\\(")


def index_top_level_defs(lines):
    starts = []
    for i, line in enumerate(lines):
        m = DEF_RE.match(line)
        if m:
            starts.append((m.group(1), i))
    index = {}
    for idx, (name, start) in enumerate(starts):
        end = starts[idx + 1][1] if idx + 1 < len(starts) else len(lines)
        index[name] = (start, end)
    return index


def extract_renamed(lines, span, rename_to):
    start, end = span
    block = lines[start:end]
    while block and block[-1].strip() == "":
        block.pop()
    text = "\\n".join(block)
    return re.sub(
        r"^(def\\s+)[A-Za-z_][A-Za-z0-9_]*(\\s*\\()",
        lambda mo: mo.group(1) + rename_to + mo.group(2),
        text,
        count=1,
    )


file_lines = {}
file_index = {}
for rel_path in allowlist_paths:
    mutants_path = "mutants/" + rel_path
    try:
        with open(mutants_path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except FileNotFoundError:
        continue
    file_lines[rel_path] = lines
    file_index[rel_path] = index_top_level_defs(lines)

out = {}
errors = {}
for mutant_id in ids:
    try:
        orig_name, _class_name = orig_function_and_class_names_from_key(mutant_id)
        mangled = mangled_name_from_mutant_name(mutant_id)
        orig_local_name = (mangled + "__mutmut_orig").split(".")[-1]
        mutant_local_name = mutant_id.split(".")[-1]

        path_str = None
        for rel_path, idx in file_index.items():
            if orig_local_name in idx and mutant_local_name in idx:
                path_str = rel_path
                break
        if path_str is None:
            raise FileNotFoundError(f"could not locate functions for {mutant_id!r}")

        lines = file_lines[path_str]
        idx = file_index[path_str]
        orig_code = extract_renamed(lines, idx[orig_local_name], orig_name)
        mutant_code = extract_renamed(lines, idx[mutant_local_name], orig_name)
        diff_lines = list(
            unified_diff(
                orig_code.split("\\n"),
                mutant_code.split("\\n"),
                fromfile=path_str,
                tofile=path_str,
                lineterm="",
            )
        )
        out[mutant_id] = "\\n".join(diff_lines).strip() + "\\n"
    except Exception as exc:  # noqa: BLE001
        errors[mutant_id] = repr(exc)

with open(sys.argv[3], "w", encoding="utf-8") as fh:
    json.dump({"patches": out, "errors": errors}, fh)
"""


def _mutmut_extract_patches_batch(
    worktree: Path,
    mutant_ids: list[str],
    module_allowlist: list[str],
    mem_limit_mb: int = 768,
) -> dict[str, str]:
    """Extract canonical diffs for many mutant IDs in ONE bounded subprocess.

    Isolating this in a subprocess (instead of looping in-process across
    hundreds of mutant IDs) prevents mutmut's per-call Config/module state
    from accumulating in the long-lived corpus-build orchestrator process.
    Function bodies are located with a plain top-level `def` line scan
    (see _EXTRACT_PATCHES_SCRIPT) rather than `cst.parse_module()`: parsing
    a real ~25MB generated mutants file (kernel.py x hundreds of mutants)
    with libcst measured 4GB+ resident memory for a SINGLE parse and was
    the confirmed mechanism behind repeated WSL-VM-wide OOM crashes during
    this build. The text-based scan processes the full real corpus
    (2600+ candidates across kernel.py + test_priority.py) in under 2
    seconds with well under 200MB resident, verified byte-identical to the
    libcst/CLI-canonical diff on random samples (see
    test_extract_patches_batch_matches_show_patch). The subprocess is also
    cgroup-memory-capped so a future regression fails closed instead of
    threatening the host.
    """
    if not mutant_ids:
        return {}
    with tempfile.TemporaryDirectory(prefix="ds002-extract-") as tmp:
        out_path = Path(tmp) / "patches.json"
        proc = subprocess.run(
            _memory_bounded_cmd(
                [
                    sys.executable,
                    "-c",
                    _EXTRACT_PATCHES_SCRIPT,
                    json.dumps(mutant_ids),
                    json.dumps(module_allowlist),
                    str(out_path),
                ],
                mem_limit_mb=mem_limit_mb,
            ),
            cwd=worktree,
            capture_output=True,
            text=True,
        )
        if not out_path.is_file():
            raise RuntimeError(
                f"patch extraction subprocess produced no output "
                f"(exit {proc.returncode}):\n{proc.stdout}\n{proc.stderr}"
            )
        payload = json.loads(out_path.read_text(encoding="utf-8"))
    if payload.get("errors"):
        raise RuntimeError(f"patch extraction failed for some mutant ids: {payload['errors']}")
    return payload.get("patches", {})


_TOP_LEVEL_DEF_RE = re.compile(r"^def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(")


def _index_top_level_defs(lines: list[str]) -> dict[str, tuple[int, int]]:
    """Map top-level function name -> (start_line, end_line_exclusive)."""
    starts: list[tuple[str, int]] = []
    for i, line in enumerate(lines):
        m = _TOP_LEVEL_DEF_RE.match(line)
        if m:
            starts.append((m.group(1), i))
    index: dict[str, tuple[int, int]] = {}
    for idx, (name, start) in enumerate(starts):
        end = starts[idx + 1][1] if idx + 1 < len(starts) else len(lines)
        index[name] = (start, end)
    return index


def _extract_renamed_block(lines: list[str], span: tuple[int, int], rename_to: str) -> str:
    start, end = span
    block = lines[start:end]
    while block and block[-1].strip() == "":
        block.pop()
    text = "\n".join(block)
    return re.sub(
        r"^(def\s+)[A-Za-z_][A-Za-z0-9_]*(\s*\()",
        lambda mo: mo.group(1) + rename_to + mo.group(2),
        text,
        count=1,
    )


def apply_mutant_text_based(worktree: Path, mutant_id: str, source_path: str) -> None:
    """Apply exactly one mutant to `worktree/source_path` via direct text splice.

    This deliberately avoids BOTH of the two mechanisms that were tried and
    failed during this build:

    - `patch -p0` against a unified diff: fragile, because the diff's
      context/removed lines come from mutmut's *regenerated* rendering of
      the original function, which is not always byte-identical to the
      literal on-disk source (observed real hunk-apply failures).
    - `mutmut apply <id>` / any `cst.parse_module()` on the generated
      mutants file: parsing a real ~25MB generated mutants file (kernel.py
      x hundreds of mutants) with libcst measured 4GB+ resident memory for
      a SINGLE call and reproducibly crashed/OOM-killed under a 2GB cgroup
      cap -- confirmed via a direct benchmark during this build.

    Both `kernel.py` and `test_priority.py` (the only module_allowlist
    entries) declare zero classes (verified), so every mutable symbol is a
    flat top-level function. A `^def name(` line scan is therefore exact
    for locating the target span in BOTH the real source file and the
    always-flattened generated mutants file -- no CST needed, no
    indentation/class-nesting handling needed. If this assumption is ever
    violated (a class method appears in the allowlist), this raises
    instead of silently producing a corrupted mutation.
    """
    from mutmut.__main__ import orig_function_and_class_names_from_key

    orig_name, class_name = orig_function_and_class_names_from_key(mutant_id)
    if class_name:
        raise NotImplementedError(
            f"mutant {mutant_id!r} belongs to class {class_name!r}; text-based apply "
            "only supports flat top-level functions (module_allowlist files were "
            "verified to declare no classes at the time this was written)"
        )
    mutant_local_name = mutant_id.split(".")[-1]

    real_path = worktree / source_path
    real_lines = real_path.read_text(encoding="utf-8").splitlines()
    real_index = _index_top_level_defs(real_lines)
    if orig_name not in real_index:
        raise RuntimeError(f"function {orig_name!r} not found in {source_path}")

    mutants_path = worktree / "mutants" / source_path
    mutant_lines = mutants_path.read_text(encoding="utf-8").splitlines()
    mutant_index = _index_top_level_defs(mutant_lines)
    if mutant_local_name not in mutant_index:
        raise RuntimeError(f"mutant function {mutant_local_name!r} not found in generated {source_path}")

    mutant_block = _extract_renamed_block(mutant_lines, mutant_index[mutant_local_name], orig_name)
    start, end = real_index[orig_name]
    replacement = mutant_block.splitlines()
    tail = real_lines[end:]
    # Preserve blank-line separation before whatever follows (mutmut always
    # emits >=1 trailing blank line before the next top-level def; our
    # extraction strips it for clean diffs, so restore it here for the
    # real file to keep normal PEP8 spacing / avoid two defs on one edge).
    if tail and tail[0].strip() != "":
        replacement = [*replacement, ""]
    new_lines = real_lines[:start] + replacement + tail
    real_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def _cell_timeout_seconds(clean_duration: float, mission: dict[str, Any]) -> float:
    floor = mission.get("cell_timeout_floor_seconds", 15)
    cap = mission.get("cell_timeout_cap_seconds", 45)
    return min(cap, max(floor, 4 * clean_duration + 5))


def _run_pytest_node(
    worktree: Path,
    node_id: str,
    timeout: float,
) -> dict[str, Any]:
    start = datetime.now(timezone.utc)
    try:
        proc = subprocess.run(
            _memory_bounded_cmd(["uv", "run", "pytest", "-q", node_id], mem_limit_mb=1536),
            cwd=worktree,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        duration = (datetime.now(timezone.utc) - start).total_seconds()
        stdout_hash = sha256_text(proc.stdout)
        stderr_hash = sha256_text(proc.stderr)
        if proc.returncode == 0:
            outcome: CellOutcome = "pass"
        elif "collection error" in proc.stderr.lower() or "error during collection" in proc.stderr.lower():
            outcome = "collection_error"
        else:
            outcome = "killed"
        return {
            "outcome": outcome,
            "duration_seconds": duration,
            "stdout_sha256": stdout_hash,
            "stderr_sha256": stderr_hash,
        }
    except subprocess.TimeoutExpired as exc:
        duration = (datetime.now(timezone.utc) - start).total_seconds()
        return {
            "outcome": "timeout",
            "duration_seconds": duration,
            "stdout_sha256": sha256_text(exc.stdout.decode() if exc.stdout else ""),
            "stderr_sha256": sha256_text(exc.stderr.decode() if exc.stderr else ""),
        }
    except Exception as exc:  # noqa: BLE001
        duration = (datetime.now(timezone.utc) - start).total_seconds()
        return {
            "outcome": "infrastructure_error",
            "duration_seconds": duration,
            "stdout_sha256": "",
            "stderr_sha256": sha256_text(str(exc)),
        }


def _reset_worktree_sources(worktree: Path) -> None:
    """Restore tracked sources to HEAD (keeps mutants/ tree)."""
    subprocess.run(
        ["git", "checkout", "--", "."],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    )


def _materialize_mutant_on_worktree(
    worktree: Path,
    mutant_id: str,
    source_path: str,
) -> None:
    """Apply one mutant on a prepared matrix worktree (call after reset).

    Uses the text-based direct splice (apply_mutant_text_based), not
    `patch -p0` or `mutmut apply`. Both alternatives were tried against the
    real corpus and rejected: `patch -p0` hit a real hunk-apply failure
    (formatting drift between mutmut's regenerated diff context and the
    literal source), and `mutmut apply` was benchmarked to hit the same
    4GB+ libcst-parse-of-the-whole-generated-file memory blowup as
    `mutmut show` for kernel.py mutants (cgroup-killed at a 2GB cap).
    """
    _reset_worktree_sources(worktree)
    apply_mutant_text_based(worktree, mutant_id, source_path)


def _prepare_matrix_worktree(
    template_worktree: Path,
    base_sha: str,
    module_allowlist: list[str],
) -> Path:
    """Single reusable matrix worktree with mutants/ copied once."""
    wt = create_detached_worktree(base_sha)
    try:
        _ensure_mutmut_config(wt, module_allowlist)
        shutil.copytree(template_worktree / "mutants", wt / "mutants", dirs_exist_ok=True)
        return wt
    except Exception:
        remove_worktree(wt)
        raise


def _compute_kill_signature(outcomes: list[dict[str, Any]]) -> str:
    bits = ["1" if row["outcome"] == "killed" else "0" for row in outcomes]
    return sha256_text("".join(bits))[:16]


def _evaluate_primary_inclusion(
    mutant: dict[str, Any],
    matrix_rows: list[dict[str, Any]],
) -> tuple[bool, str | None]:
    outcomes = [r["outcome"] for r in matrix_rows]
    if "timeout" in outcomes or "infrastructure_error" in outcomes:
        return False, "timeout_or_infrastructure_error"
    if "collection_error" in outcomes:
        return False, "collection_error"
    killed = outcomes.count("killed")
    passed = outcomes.count("pass")
    if killed == 0:
        return False, "no_killing_test"
    if passed == 0:
        return False, "no_passing_test"
    if killed == len(outcomes):
        return False, "killed_by_all_tests"
    return True, None


def build_corpus(
    mission_path: Path,
    target_sha: str,
    output_dir: Path,
    workers: int = 4,
) -> dict[str, Any]:
    """Build a real-mutation corpus at target SHA."""
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(f"refusing to overwrite non-empty output: {output_dir}")

    # Resolve to absolute up front: the matrix phase applies patches with
    # `cwd` set to a detached worktree under /tmp, so a relative output_dir
    # (as typically passed on the CLI) would silently fail to resolve there.
    output_dir = output_dir.resolve()
    mission = load_mission(mission_path)
    ctx = BuildContext()
    output_dir.mkdir(parents=True, exist_ok=True)
    rejected_path = output_dir / "rejected.jsonl"
    patches_dir = output_dir / "patches"
    patches_dir.mkdir(parents=True, exist_ok=True)

    repo_root = Path(__file__).resolve().parents[2]
    lock_hash = sha256_file(repo_root / "uv.lock") if (repo_root / "uv.lock").exists() else ""

    worktree = create_detached_worktree(target_sha)
    try:
        clean_duration = verify_clean_pytest(worktree)
        node_ids = collect_test_node_ids(worktree)
        if len(node_ids) != 10:
            raise RuntimeError(f"expected 10 pytest node IDs, got {len(node_ids)}: {node_ids}")

        _ensure_mutmut_config(worktree, mission["module_allowlist"])
        _mutmut_generate(worktree, workers)
        raw_ids = _mutmut_list_ids(worktree)

        ctx.enter_selection()
        _assert_pre_outcome_phase(ctx, operation="filter/classify mutants")
        candidate_ids = [mid for mid in raw_ids if _mutmut_id_might_match_allowlist(mid, mission)]
        patches_by_id = _mutmut_extract_patches_batch(worktree, candidate_ids, mission["module_allowlist"])
        raw_mutants: list[dict[str, Any]] = []
        for mutant_id in candidate_ids:
            patch = patches_by_id[mutant_id]
            patch_hash = sha256_text(patch)
            symbol = extract_qualified_symbol(mutant_id, patch)
            path_match = re.search(r"(?:src/)?detonator/[\w_]+\.py", patch)
            source_path = path_match.group(0) if path_match else ""
            operator = classify_operator(patch)
            record = {
                "mutmut_id": mutant_id,
                "qualified_symbol": symbol,
                "operator_family": operator,
                "patch_sha256": patch_hash,
                "path": source_path,
                "mutmut_version": MUTMUT_VERSION,
                "target_sha": target_sha,
            }
            if not _symbol_allowed(symbol, mission):
                write_rejected_row(
                    rejected_path,
                    {**record, "reason": f"symbol {symbol!r} not allowlisted"},
                )
                continue
            if source_path and source_path not in mission["module_allowlist"]:
                write_rejected_row(
                    rejected_path,
                    {**record, "reason": f"path {source_path!r} not in module allowlist"},
                )
                continue
            raw_mutants.append(record)
            (patches_dir / f"{patch_hash}.diff").write_text(patch, encoding="utf-8")

        caps = mission.get("selection_caps", {})
        selected, cap_rejected = apply_selection_caps(
            raw_mutants,
            per_symbol_operator=caps.get("per_symbol_operator", 4),
            per_symbol_total=caps.get("per_symbol_total", 12),
        )
        for row in cap_rejected:
            write_rejected_row(rejected_path, row)

        selection_manifest = {
            "schema_version": 1,
            "target_sha": target_sha,
            "mutmut_version": MUTMUT_VERSION,
            "lockfile_sha256": lock_hash,
            "python_version": platform.python_version(),
            "platform": platform.platform(),
            "created_at": utc_now_iso(),
            "mutants": selected,
        }
        selection_hash = _write_json(output_dir / "selection.json", selection_manifest, ctx)

        split_payload = assign_split(selected, mission)
        split_payload["frozen_at"] = utc_now_iso()
        split_payload["selection_sha256"] = selection_hash
        ctx.freeze_split()
        split_hash = _write_json(output_dir / "split.json", split_payload, ctx)

        provenance = {
            "schema_version": 1,
            "target_sha": target_sha,
            "mutmut_version": MUTMUT_VERSION,
            "lockfile_sha256": lock_hash,
            "clean_pytest_duration_seconds": clean_duration,
            "test_node_ids": node_ids,
            "selection_sha256": selection_hash,
            "split_sha256": split_hash,
            "created_at": utc_now_iso(),
        }
        _write_json(output_dir / "provenance.json", provenance, ctx)
        _write_json(output_dir / "tests.json", {"node_ids": node_ids}, ctx)

        ctx.enter_matrix()
        cell_timeout = _cell_timeout_seconds(clean_duration, mission)
        search_matrix_path = output_dir / "search-matrix.jsonl"
        holdout_matrix_path = output_dir / "holdout-matrix.jsonl"
        mutants_jsonl = output_dir / "mutants.jsonl"
        search_set = set(split_payload["search"])
        holdout_set = set(split_payload["holdout"])
        primary: list[dict[str, Any]] = []

        matrix_worktree = _prepare_matrix_worktree(worktree, target_sha, mission["module_allowlist"])
        try:
            for mutant in selected:
                mutant_id = mutant["mutmut_id"]
                side = "search" if mutant_id in search_set else "holdout"
                _materialize_mutant_on_worktree(matrix_worktree, mutant_id, mutant["path"])
                matrix_rows: list[dict[str, Any]] = []
                for node_id in node_ids:
                    row = {
                        "mutmut_id": mutant_id,
                        "node_id": node_id,
                        "side": side,
                        **_run_pytest_node(matrix_worktree, node_id, cell_timeout),
                    }
                    matrix_rows.append(row)
                    target = search_matrix_path if side == "search" else holdout_matrix_path
                    with target.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(row, sort_keys=True) + "\n")

                kill_signature = _compute_kill_signature(matrix_rows)
                included, reject_reason = _evaluate_primary_inclusion(mutant, matrix_rows)
                mutant_record = {
                    **mutant,
                    "side": side,
                    "kill_signature": kill_signature,
                    "primary": included,
                    "matrix_outcomes": matrix_rows,
                }
                if not included and reject_reason:
                    write_rejected_row(
                        rejected_path,
                        {**mutant, "reason": reject_reason, "kill_signature": kill_signature},
                    )
                if included:
                    primary.append(mutant_record)
                with mutants_jsonl.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(mutant_record, sort_keys=True) + "\n")
        finally:
            remove_worktree(matrix_worktree)

        gate_result = evaluate_gates(primary, split_payload)
        ctx.phase = "done"
        if not gate_result["passed"]:
            raise DomainRejected(
                "DOMAIN REJECTED: " + "; ".join(gate_result["failures"])
            )
        return {
            "status": "ok",
            "output_dir": str(output_dir.resolve()),
            "primary_count": len(primary),
            "gates": gate_result,
            "write_log": ctx.write_log,
        }
    finally:
        remove_worktree(worktree)


def verify_corpus(corpus_dir: Path) -> dict[str, Any]:
    """Verify corpus artifact presence and hash seals."""
    required = [
        "provenance.json",
        "split.json",
        "tests.json",
        "mutants.jsonl",
        "rejected.jsonl",
        "selection.json",
        "search-matrix.jsonl",
        "holdout-matrix.jsonl",
    ]
    missing = [name for name in required if not (corpus_dir / name).is_file()]
    if missing:
        return {"status": "invalid", "missing": missing}

    split = json.loads((corpus_dir / "split.json").read_text(encoding="utf-8"))
    selection = json.loads((corpus_dir / "selection.json").read_text(encoding="utf-8"))
    primary = [json.loads(line) for line in (corpus_dir / "mutants.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    primary = [m for m in primary if m.get("primary")]
    gates = evaluate_gates(primary, split)

    # Ordering invariant: split timestamp/hash precedes matrix files
    split_mtime = (corpus_dir / "split.json").stat().st_mtime
    for matrix_name in ("search-matrix.jsonl", "holdout-matrix.jsonl"):
        matrix_path = corpus_dir / matrix_name
        if matrix_path.stat().st_mtime < split_mtime:
            gates["passed"] = False
            gates.setdefault("failures", []).append(f"{matrix_name} predates split.json")

    provenance_fields = {
        "patch_sha256",
        "operator_family",
        "qualified_symbol",
        "mutmut_version",
    }
    for mutant in selection.get("mutants", []):
        if not provenance_fields.issubset(mutant):
            gates["passed"] = False
            gates.setdefault("failures", []).append("selection manifest missing provenance fields")

    return {
        "status": "ok" if gates["passed"] else "domain_rejected",
        "gates": gates,
        "selection_count": len(selection.get("mutants", [])),
        "primary_count": len(primary),
    }


def selection_functions_avoid_outcome_reads() -> bool:
    """Return True if selection helpers do not reference outcome readers."""
    forbidden = "read_matrix_outcomes_for_selection"
    for fn in (apply_selection_caps, assign_split, classify_operator):
        source = inspect.getsource(fn)
        if forbidden in source:
            return False
    return True
