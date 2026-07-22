#!/usr/bin/env python3
"""Live Cursor Agent variation provider for detonator (JSON stdin/stdout).

Credentials come from the agent CLI login / CURSOR_API_KEY environment.
This wrapper never embeds secrets and never receives holdout data.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import textwrap


def _extract_source(text: str) -> str:
    fenced = re.search(r"```(?:python)?\s*([\s\S]*?)```", text)
    if fenced:
        return fenced.group(1).strip() + "\n"
    # Fall back to full response if it already looks like a module.
    if "def prioritize" in text:
        start = text.index("def prioritize")
        # Include any imports immediately above prioritize.
        prefix = text[:start]
        import_block = []
        for line in prefix.splitlines()[::-1]:
            stripped = line.strip()
            if stripped.startswith("import ") or stripped.startswith("from ") or stripped == "":
                import_block.append(line)
            else:
                break
        import_block.reverse()
        body = text[start:].strip()
        return ("\n".join(import_block) + "\n" + body).strip() + "\n"
    raise RuntimeError("provider response missing prioritize()")


def main() -> None:
    request = json.load(sys.stdin)
    if "holdout" in request or "holdout_faults" in request:
        raise SystemExit("provider must not receive holdout data")

    parent_source = request["parent"]["source"]
    proposal_index = int(request["proposal_index"])
    target_cell = request.get("target_cell")
    score = (request.get("search_feedback") or {}).get("score")

    prompt = textwrap.dedent(
        f"""
        Write a complete Python module for test prioritization.

        Requirements:
        - Define exactly: def prioritize(change: dict, tests: list[dict]) -> list[str]
        - Return every test id exactly once (exact permutation of input test ids)
        - Each test dict has: id, cost_units, covers, historical_failure_rate
        - change has changed_symbols
        - Prefer a strategy suited to target cell {target_cell!r}
        - Slightly mutate the parent policy; proposal_index={proposal_index}
        - Parent search score was {score!r}

        Parent source:
        ```python
        {parent_source}
        ```

        Reply with ONLY Python source code. No markdown. No explanation.
        """
    ).strip()

    completed = subprocess.run(
        [
            "agent",
            "-p",
            prompt,
            "--mode",
            "ask",
            "--output-format",
            "text",
            "-f",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        # Do not print provider stderr to keep credentials/tokens out of caller logs.
        raise SystemExit(f"agent exited {completed.returncode}")

    source = _extract_source(completed.stdout)
    # Validate it at least parses and defines prioritize.
    namespace: dict = {}
    exec(compile(source, "<live-provider>", "exec"), namespace, namespace)
    if "prioritize" not in namespace or not callable(namespace["prioritize"]):
        raise SystemExit("generated source missing prioritize()")

    json.dump(
        {
            "source": source,
            "mutation_description": (
                f"live-agent proposal={proposal_index} target={target_cell}"
            ),
        },
        sys.stdout,
    )
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
