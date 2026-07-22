#!/usr/bin/env python3
"""Deterministic sample external variation provider for DS-001.

Reads one JSON request from stdin and writes one JSON response to stdout.
No network calls and no vendor SDKs.
"""

from __future__ import annotations

import json
import sys


def render_source(proposal_index: int, target_cell: list[str] | None) -> str:
    # Tiny deterministic family of policies driven only by proposal index.
    cost_weight = 1.0 if proposal_index % 2 == 0 else -1.0
    change_weight = 2.0 if target_cell and target_cell[1] == "change_focused" else 0.0
    risk_weight = 1.0 + 0.1 * proposal_index
    return f'''"""Sample-provider prioritization policy."""


def prioritize(change: dict, tests: list[dict]) -> list[str]:
    changed = set(change.get("changed_symbols") or [])

    def sort_key(test):
        covers = set(test.get("covers") or [])
        covers_change = 0 if covers & changed else 1
        risk = float(test.get("historical_failure_rate") or 0.0)
        cost = int(test["cost_units"])
        return (
            covers_change * {change_weight!r},
            cost * {cost_weight!r},
            -risk * {risk_weight!r},
            test["id"],
        )

    ordered = sorted(tests, key=sort_key)
    return [t["id"] for t in ordered]
'''


def main() -> None:
    request = json.load(sys.stdin)
    if "holdout" in request or "holdout_faults" in request:
        raise SystemExit("provider must not receive holdout data")
    proposal_index = int(request["proposal_index"])
    target_cell = request.get("target_cell")
    source = render_source(proposal_index, target_cell)
    response = {
        "source": source,
        "mutation_description": (
            f"sample_provider proposal={proposal_index} target={target_cell}"
        ),
    }
    json.dump(response, sys.stdout)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
