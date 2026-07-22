"""Holdout-only fault definitions for DS-001.

This module must be imported only after the archive is frozen and written.
Search-phase code loads ``benchmark.py`` alone and must not import this file.
"""

from __future__ import annotations

from typing import Any, Callable

# Marker observed by pre-freeze probes.
HOLDOUT_DEFINITIONS_LOADED = True


def _np_drop_leading(path: str) -> str:
    text = path.strip()
    while "//" in text:
        text = text.replace("//", "/")
    if len(text) > 1 and text.endswith("/"):
        text = text[:-1]
    return text[1:] if text.startswith("/") else text


def _np_double_strip_only(path: str) -> str:
    return path.strip()


def _clamp_identity(value: float, low: float, high: float) -> float:
    return value


def _clamp_floor_only(value: float, low: float, high: float) -> float:
    return low if value < low else value


def _merge_increment_overlap(left: dict, right: dict) -> dict:
    out = dict(left)
    for key, value in right.items():
        if isinstance(value, int) and isinstance(out.get(key), int):
            out[key] = out[key] + value
        else:
            out[key] = value
    return out


def _merge_prefer_left(left: dict, right: dict) -> dict:
    return {k: left.get(k, right.get(k)) for k in set(left) | set(right)}


def _fmt_space_currency(value: float, currency: str = "UAH") -> str:
    sign = "-" if value < 0 else ""
    return f"{sign}{abs(value):.2f}{currency}"


def _fmt_int_only(value: float, currency: str = "UAH") -> str:
    sign = "-" if value < 0 else ""
    return f"{sign}{int(abs(value))} {currency}"


HOLDOUT_FAULT_SPECS: dict[str, dict[str, Any]] = {
    "h01": {"changed_symbols": ["normalize_path"], "overrides": {"normalize_path": _np_drop_leading}},
    "h02": {"changed_symbols": ["normalize_path"], "overrides": {"normalize_path": _np_double_strip_only}},
    "h03": {"changed_symbols": ["clamp"], "overrides": {"clamp": _clamp_identity}},
    "h04": {"changed_symbols": ["clamp"], "overrides": {"clamp": _clamp_floor_only}},
    "h05": {"changed_symbols": ["merge_dicts"], "overrides": {"merge_dicts": _merge_increment_overlap}},
    "h06": {"changed_symbols": ["merge_dicts"], "overrides": {"merge_dicts": _merge_prefer_left}},
    "h07": {"changed_symbols": ["format_amount"], "overrides": {"format_amount": _fmt_space_currency}},
    "h08": {"changed_symbols": ["format_amount"], "overrides": {"format_amount": _fmt_int_only}},
}

HOLDOUT_FAULT_IDS = [f"h{i:02d}" for i in range(1, 9)]
