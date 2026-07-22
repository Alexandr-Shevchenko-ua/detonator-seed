"""Concrete test-prioritization benchmark for DS-001.

Four pure change areas, twelve short tests, and twenty-four deterministic
faulty revisions (sixteen search, eight holdout). Candidates receive only
public metadata — never fault IDs, implementations, or outcomes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Clean implementations
# ---------------------------------------------------------------------------


def normalize_path(path: str) -> str:
    text = path.strip()
    while "//" in text:
        text = text.replace("//", "/")
    if len(text) > 1 and text.endswith("/"):
        text = text[:-1]
    return text


def clamp(value: float, low: float, high: float) -> float:
    if low > high:
        low, high = high, low
    if value < low:
        return low
    if value > high:
        return high
    return value


def merge_dicts(left: dict, right: dict) -> dict:
    out = dict(left)
    out.update(right)
    return out


def format_amount(value: float, currency: str = "UAH") -> str:
    sign = "-" if value < 0 else ""
    return f"{sign}{abs(value):.2f} {currency}"


CLEAN_FUNCTIONS: dict[str, Callable[..., Any]] = {
    "normalize_path": normalize_path,
    "clamp": clamp,
    "merge_dicts": merge_dicts,
    "format_amount": format_amount,
}


# ---------------------------------------------------------------------------
# Faulty variants (never exposed to candidates)
# ---------------------------------------------------------------------------


def _np_no_strip(path: str) -> str:
    text = path
    while "//" in text:
        text = text.replace("//", "/")
    if len(text) > 1 and text.endswith("/"):
        text = text[:-1]
    return text


def _np_no_collapse(path: str) -> str:
    text = path.strip()
    if len(text) > 1 and text.endswith("/"):
        text = text[:-1]
    return text


def _np_keep_trailing(path: str) -> str:
    text = path.strip()
    while "//" in text:
        text = text.replace("//", "/")
    return text


def _np_upper(path: str) -> str:
    return normalize_path(path).upper()


def _clamp_no_low(value: float, low: float, high: float) -> float:
    if low > high:
        low, high = high, low
    if value > high:
        return high
    return value


def _clamp_no_high(value: float, low: float, high: float) -> float:
    if low > high:
        low, high = high, low
    if value < low:
        return low
    return value


def _clamp_swap_ignore(value: float, low: float, high: float) -> float:
    if value < low:
        return low
    if value > high:
        return high
    return value


def _clamp_always_mid(value: float, low: float, high: float) -> float:
    return (low + high) / 2


def _merge_overwrite_left(left: dict, right: dict) -> dict:
    out = dict(right)
    out.update(left)
    return out


def _merge_no_right(left: dict, right: dict) -> dict:
    return dict(left)


def _merge_no_left(left: dict, right: dict) -> dict:
    return dict(right)


def _merge_stringify_right(left: dict, right: dict) -> dict:
    out = dict(left)
    for key, value in right.items():
        out[key] = str(value)
    return out


def _fmt_no_sign(value: float, currency: str = "UAH") -> str:
    return f"{abs(value):.2f} {currency}"


def _fmt_one_decimal(value: float, currency: str = "UAH") -> str:
    sign = "-" if value < 0 else ""
    return f"{sign}{abs(value):.1f} {currency}"


def _fmt_no_currency(value: float, currency: str = "UAH") -> str:
    sign = "-" if value < 0 else ""
    return f"{sign}{abs(value):.2f}"


def _fmt_comma(value: float, currency: str = "UAH") -> str:
    sign = "-" if value < 0 else ""
    return f"{sign}{abs(value):.2f}".replace(".", ",") + f" {currency}"


# Search-only fault specs. Holdout specs live in holdout_faults.py and are
# attached only after archive freeze.
FAULT_SPECS: dict[str, dict[str, Any]] = {
    "s01": {"changed_symbols": ["normalize_path"], "overrides": {"normalize_path": _np_no_strip}},
    "s02": {"changed_symbols": ["normalize_path"], "overrides": {"normalize_path": _np_no_collapse}},
    "s03": {"changed_symbols": ["normalize_path"], "overrides": {"normalize_path": _np_keep_trailing}},
    "s04": {"changed_symbols": ["normalize_path"], "overrides": {"normalize_path": _np_upper}},
    "s05": {"changed_symbols": ["clamp"], "overrides": {"clamp": _clamp_no_low}},
    "s06": {"changed_symbols": ["clamp"], "overrides": {"clamp": _clamp_no_high}},
    "s07": {"changed_symbols": ["clamp"], "overrides": {"clamp": _clamp_swap_ignore}},
    "s08": {"changed_symbols": ["clamp"], "overrides": {"clamp": _clamp_always_mid}},
    "s09": {"changed_symbols": ["merge_dicts"], "overrides": {"merge_dicts": _merge_overwrite_left}},
    "s10": {"changed_symbols": ["merge_dicts"], "overrides": {"merge_dicts": _merge_no_right}},
    "s11": {"changed_symbols": ["merge_dicts"], "overrides": {"merge_dicts": _merge_no_left}},
    "s12": {"changed_symbols": ["merge_dicts"], "overrides": {"merge_dicts": _merge_stringify_right}},
    "s13": {"changed_symbols": ["format_amount"], "overrides": {"format_amount": _fmt_no_sign}},
    "s14": {"changed_symbols": ["format_amount"], "overrides": {"format_amount": _fmt_one_decimal}},
    "s15": {"changed_symbols": ["format_amount"], "overrides": {"format_amount": _fmt_no_currency}},
    "s16": {"changed_symbols": ["format_amount"], "overrides": {"format_amount": _fmt_comma}},
}

SEARCH_FAULT_IDS = [f"s{i:02d}" for i in range(1, 17)]
HOLDOUT_FAULT_IDS: list[str] = []
_HOLDOUT_DEFINITIONS_ATTACHED = False
_HOLDOUT_MODULE_NAME: str | None = None


# ---------------------------------------------------------------------------
# Tests (public metadata + private checkers)
# ---------------------------------------------------------------------------


def _check_np_strip(fns: dict[str, Callable[..., Any]]) -> None:
    assert fns["normalize_path"]("  /a/b  ") == "/a/b"


def _check_np_collapse(fns: dict[str, Callable[..., Any]]) -> None:
    assert fns["normalize_path"]("/a//b///c") == "/a/b/c"


def _check_np_trailing(fns: dict[str, Callable[..., Any]]) -> None:
    assert fns["normalize_path"]("/a/b/") == "/a/b"


def _check_clamp_low(fns: dict[str, Callable[..., Any]]) -> None:
    assert fns["clamp"](-1.0, 0.0, 10.0) == 0.0


def _check_clamp_high(fns: dict[str, Callable[..., Any]]) -> None:
    assert fns["clamp"](99.0, 0.0, 10.0) == 10.0


def _check_clamp_swap(fns: dict[str, Callable[..., Any]]) -> None:
    assert fns["clamp"](5.0, 10.0, 0.0) == 5.0


def _check_merge_override(fns: dict[str, Callable[..., Any]]) -> None:
    assert fns["merge_dicts"]({"a": 1, "b": 2}, {"b": 9, "c": 3}) == {"a": 1, "b": 9, "c": 3}


def _check_merge_empty_right(fns: dict[str, Callable[..., Any]]) -> None:
    assert fns["merge_dicts"]({"a": 1}, {}) == {"a": 1}


def _check_merge_empty_left(fns: dict[str, Callable[..., Any]]) -> None:
    assert fns["merge_dicts"]({}, {"a": 1}) == {"a": 1}


def _check_fmt_negative(fns: dict[str, Callable[..., Any]]) -> None:
    assert fns["format_amount"](-3.5, "USD") == "-3.50 USD"


def _check_fmt_precision(fns: dict[str, Callable[..., Any]]) -> None:
    assert fns["format_amount"](1.2, "UAH") == "1.20 UAH"


def _check_fmt_currency(fns: dict[str, Callable[..., Any]]) -> None:
    assert fns["format_amount"](0.0, "EUR") == "0.00 EUR"


# Private checkers keyed by test id. Public rows omit callables.
_TEST_CHECKS: dict[str, Callable[[dict[str, Callable[..., Any]]], None]] = {
    "t01": _check_np_strip,
    "t02": _check_np_collapse,
    "t03": _check_np_trailing,
    "t04": _check_clamp_low,
    "t05": _check_clamp_high,
    "t06": _check_clamp_swap,
    "t07": _check_merge_override,
    "t08": _check_merge_empty_right,
    "t09": _check_merge_empty_left,
    "t10": _check_fmt_negative,
    "t11": _check_fmt_precision,
    "t12": _check_fmt_currency,
}

PUBLIC_TESTS: list[dict[str, Any]] = [
    {"id": "t01", "cost_units": 1, "covers": ["normalize_path"], "historical_failure_rate": 0.40},
    {"id": "t02", "cost_units": 3, "covers": ["normalize_path"], "historical_failure_rate": 0.20},
    {"id": "t03", "cost_units": 5, "covers": ["normalize_path"], "historical_failure_rate": 0.10},
    {"id": "t04", "cost_units": 1, "covers": ["clamp"], "historical_failure_rate": 0.50},
    {"id": "t05", "cost_units": 2, "covers": ["clamp"], "historical_failure_rate": 0.30},
    {"id": "t06", "cost_units": 8, "covers": ["clamp"], "historical_failure_rate": 0.05},
    {"id": "t07", "cost_units": 2, "covers": ["merge_dicts"], "historical_failure_rate": 0.35},
    {"id": "t08", "cost_units": 4, "covers": ["merge_dicts"], "historical_failure_rate": 0.15},
    {"id": "t09", "cost_units": 6, "covers": ["merge_dicts"], "historical_failure_rate": 0.10},
    {"id": "t10", "cost_units": 1, "covers": ["format_amount"], "historical_failure_rate": 0.45},
    {"id": "t11", "cost_units": 3, "covers": ["format_amount"], "historical_failure_rate": 0.25},
    {"id": "t12", "cost_units": 7, "covers": ["format_amount"], "historical_failure_rate": 0.05},
]

TOTAL_SUITE_COST = sum(t["cost_units"] for t in PUBLIC_TESTS)

# Access log for integration tests that observe holdout construction/execution.
_HOLDOUT_FAULT_ACCESS: list[tuple[str, str]] = []


def reset_holdout_fault_access() -> None:
    _HOLDOUT_FAULT_ACCESS.clear()


def holdout_fault_access_log() -> list[tuple[str, str]]:
    return list(_HOLDOUT_FAULT_ACCESS)


def note_holdout_fault_access(fault_id: str, action: str) -> None:
    _HOLDOUT_FAULT_ACCESS.append((fault_id, action))


def public_tests() -> list[dict[str, Any]]:
    """Return candidate-visible test metadata (no callables)."""
    return [dict(t) for t in PUBLIC_TESTS]


def test_ids() -> list[str]:
    return [t["id"] for t in PUBLIC_TESTS]


def cost_by_id() -> dict[str, int]:
    return {t["id"]: int(t["cost_units"]) for t in PUBLIC_TESTS}


def build_functions(fault_id: str | None = None) -> dict[str, Callable[..., Any]]:
    funcs = dict(CLEAN_FUNCTIONS)
    if fault_id is not None:
        if fault_id not in FAULT_SPECS:
            raise KeyError(f"unknown fault_id: {fault_id}")
        if fault_id.startswith("h"):
            note_holdout_fault_access(fault_id, "build_functions")
        funcs.update(FAULT_SPECS[fault_id]["overrides"])
    return funcs


def change_for_fault(fault_id: str) -> dict[str, Any]:
    if fault_id.startswith("h"):
        note_holdout_fault_access(fault_id, "change_for_fault")
    return {"changed_symbols": list(FAULT_SPECS[fault_id]["changed_symbols"])}


def run_test(test_id: str, functions: dict[str, Callable[..., Any]]) -> bool:
    """Return True if the test passes."""
    try:
        _TEST_CHECKS[test_id](functions)
        return True
    except AssertionError:
        return False
    except Exception:
        return False


def run_suite_until_failure(
    order: list[str],
    fault_id: str,
) -> dict[str, Any]:
    """Execute tests in order against a faulty revision until first failure."""
    functions = build_functions(fault_id)
    costs = cost_by_id()
    executed: list[str] = []
    cost_to_failure = 0
    first_failing: str | None = None
    for test_id in order:
        executed.append(test_id)
        cost_to_failure += costs[test_id]
        if not run_test(test_id, functions):
            first_failing = test_id
            break
    if first_failing is None:
        score = 0.0
    else:
        score = 1.0 - (cost_to_failure / TOTAL_SUITE_COST)
    return {
        "fault_id": fault_id,
        "executed_tests": executed,
        "first_failing_test": first_failing,
        "cost_to_failure": cost_to_failure if first_failing is not None else None,
        "score": score,
    }


def validate_search_benchmark() -> None:
    """Search-phase integrity checks only (no holdout construction/execution)."""
    clean = build_functions(None)
    for test_id in test_ids():
        if not run_test(test_id, clean):
            raise RuntimeError(f"clean implementation fails {test_id}")

    for fault_id in SEARCH_FAULT_IDS:
        functions = build_functions(fault_id)
        if all(run_test(tid, functions) for tid in test_ids()):
            raise RuntimeError(f"fault {fault_id} is not caught by any test")

    for fault_id in SEARCH_FAULT_IDS:
        if fault_id not in FAULT_SPECS:
            raise RuntimeError(f"missing fault spec for {fault_id}")


def validate_holdout_benchmark(holdout_ids: list[str]) -> None:
    """Holdout integrity checks — only after archive freeze and attach."""
    if not _HOLDOUT_DEFINITIONS_ATTACHED:
        raise RuntimeError("holdout definitions must be attached before validation")
    for fault_id in holdout_ids:
        if fault_id not in FAULT_SPECS:
            raise RuntimeError(f"missing holdout fault spec for {fault_id}")
        functions = build_functions(fault_id)
        if all(run_test(tid, functions) for tid in test_ids()):
            raise RuntimeError(f"holdout fault {fault_id} is not caught by any test")

    overlap = set(SEARCH_FAULT_IDS) & set(holdout_ids)
    if overlap:
        raise RuntimeError(f"search/holdout fault id overlap: {sorted(overlap)}")


def attach_holdout_definitions(holdout_module_path: Path) -> None:
    """Import holdout fault definitions after archive freeze."""
    global HOLDOUT_FAULT_IDS, _HOLDOUT_DEFINITIONS_ATTACHED, _HOLDOUT_MODULE_NAME
    if _HOLDOUT_DEFINITIONS_ATTACHED:
        return
    import importlib.util
    import sys

    path = Path(holdout_module_path).resolve()
    module_name = f"tp_holdout_faults_{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load holdout definitions from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    holdout_specs = getattr(module, "HOLDOUT_FAULT_SPECS", None)
    if not isinstance(holdout_specs, dict) or not holdout_specs:
        raise RuntimeError(f"holdout module missing HOLDOUT_FAULT_SPECS: {path}")
    for fault_id, spec_row in holdout_specs.items():
        if not str(fault_id).startswith("h"):
            raise RuntimeError(f"holdout fault id must start with 'h': {fault_id}")
        FAULT_SPECS[fault_id] = spec_row
    HOLDOUT_FAULT_IDS = list(getattr(module, "HOLDOUT_FAULT_IDS", list(holdout_specs)))
    _HOLDOUT_DEFINITIONS_ATTACHED = True
    _HOLDOUT_MODULE_NAME = module_name
    note_holdout_fault_access("module", f"attached:{path.name}")


def holdout_definitions_attached() -> bool:
    return _HOLDOUT_DEFINITIONS_ATTACHED


def holdout_specs_loaded() -> list[str]:
    return sorted(fid for fid in FAULT_SPECS if str(fid).startswith("h"))


def validate_benchmark() -> None:
    """Full integrity checks (search + holdout). Prefer split validators in evolve."""
    validate_search_benchmark()
    holdout_path = Path(__file__).resolve().parent / "holdout_faults.py"
    attach_holdout_definitions(holdout_path)
    validate_holdout_benchmark(HOLDOUT_FAULT_IDS)
