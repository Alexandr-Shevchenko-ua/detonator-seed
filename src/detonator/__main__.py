"""CLI entrypoint for detonator."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from detonator.kernel import evolve


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="detonator", description="DS-001 mutation kernel")
    sub = parser.add_subparsers(dest="command", required=True)

    evolve_p = sub.add_parser("evolve", help="run a bounded evolution mission")
    evolve_p.add_argument("mission", type=Path, help="path to mission.json")
    evolve_p.add_argument("--budget", type=int, default=None, help="override descendant budget")
    evolve_p.add_argument("--output", type=Path, default=None, help="run output directory")
    evolve_p.add_argument(
        "--variation-command",
        default=None,
        help="external variation command (JSON stdin/stdout)",
    )

    inspect_p = sub.add_parser("inspect", help="inspect a previous run")
    inspect_p.add_argument("run_dir", type=Path, help="path to run directory")
    inspect_p.add_argument("--verify", action="store_true", help="verify artifact hashes")
    inspect_p.add_argument(
        "--replay-retained",
        action="store_true",
        help="replay retained candidates and compare scores",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "evolve":
        result = evolve(
            args.mission,
            budget=args.budget,
            output=args.output,
            variation_command=args.variation_command,
        )
        # Seed-only success path prints details inside evolve.
        _ = result
        return
    if args.command == "inspect":
        print("inspect is not implemented yet", file=sys.stderr)
        raise SystemExit(2)
    parser.error(f"unknown command: {args.command}")


if __name__ == "__main__":
    main()
