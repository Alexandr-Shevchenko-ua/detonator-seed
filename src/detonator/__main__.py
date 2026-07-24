"""CLI entrypoint for detonator."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from detonator.kernel import evolve, inspect_run
from detonator.mutation_corpus import build_corpus, verify_corpus
from detonator.real_mutations_preflight import run_preflight
from detonator.real_mutations_publish import emit_strong_human_order, publish_evidence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="detonator", description="DS-001 mutation kernel")
    sub = parser.add_subparsers(dest="command", required=True)

    corpus = sub.add_parser("corpus", help="build or verify real-mutation corpus")
    corpus_sub = corpus.add_subparsers(dest="corpus_command", required=True)

    corpus_build = corpus_sub.add_parser("build", help="build mutation corpus")
    corpus_build.add_argument("mission", type=Path, help="path to mission.json")
    corpus_build.add_argument("--target", required=True, help="target git SHA")
    corpus_build.add_argument("--workers", type=int, default=4, help="worker count")
    corpus_build.add_argument("--output", type=Path, required=True, help="output directory")

    corpus_verify = corpus_sub.add_parser("verify", help="verify mutation corpus")
    corpus_verify.add_argument("corpus_dir", type=Path, help="corpus directory")

    preflight_p = sub.add_parser("preflight", help="DS-002 domain preflight (baselines, oracle, headroom)")
    preflight_p.add_argument("mission", type=Path, help="path to mission.json")
    preflight_p.add_argument("--corpus", type=Path, required=True, help="verified corpus directory")
    preflight_p.add_argument("--output", type=Path, required=True, help="preflight output directory")

    evolve_p = sub.add_parser("evolve", help="run a bounded evolution mission")
    evolve_p.add_argument("mission", type=Path, help="path to mission.json")
    evolve_p.add_argument("--budget", type=int, default=None, help="override descendant budget")
    evolve_p.add_argument("--output", type=Path, default=None, help="run output directory")
    evolve_p.add_argument(
        "--variation-command",
        default=None,
        help="external variation command (JSON stdin/stdout)",
    )
    evolve_p.add_argument(
        "--provider-timeout-seconds",
        type=float,
        default=None,
        help="timeout for external variation-command (default: mission or 120)",
    )

    publish_p = sub.add_parser(
        "publish",
        help="publish DS-002 preflight verdict to evidence (re-export only)",
    )
    publish_p.add_argument("mission", type=Path, help="path to mission.json")
    publish_p.add_argument(
        "--preflight",
        type=Path,
        default=Path("runs/ds002-preflight/preflight.json"),
        help="preflight JSON to re-export",
    )
    publish_p.add_argument(
        "--output",
        type=Path,
        default=Path("evidence/ds002"),
        help="evidence output directory",
    )

    order_p = sub.add_parser(
        "order",
        help="emit test order from the published strong-human reference seed (not a search winner)",
    )
    order_p.add_argument("mission", type=Path, help="path to mission.json")
    order_p.add_argument(
        "--corpus",
        type=Path,
        default=Path("runs/ds002-corpus"),
        help="verified DS-002 corpus directory",
    )

    inspect_p = sub.add_parser("inspect", help="inspect a previous run")
    inspect_p.add_argument("run_dir", type=Path, help="path to run directory")
    inspect_p.add_argument("--verify", action="store_true", help="verify artifact hashes")
    inspect_p.add_argument(
        "--replay-retained",
        action="store_true",
        help="re-execute retained artifacts and compare evaluation results",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "corpus":
        if args.corpus_command == "build":
            try:
                result = build_corpus(
                    args.mission,
                    args.target,
                    args.output,
                    workers=args.workers,
                )
            except FileExistsError as exc:
                print(str(exc), file=sys.stderr)
                raise SystemExit(2)
            except Exception as exc:
                print(str(exc), file=sys.stderr)
                raise SystemExit(1)
            print(json.dumps(result, indent=2))
            return
        if args.corpus_command == "verify":
            result = verify_corpus(args.corpus_dir)
            print(json.dumps(result, indent=2))
            raise SystemExit(0 if result.get("status") == "ok" else 1)
        parser.error(f"unknown corpus command: {args.corpus_command}")
    if args.command == "preflight":
        try:
            result = run_preflight(args.mission, args.corpus, args.output)
        except Exception as exc:
            print(str(exc), file=sys.stderr)
            raise SystemExit(1)
        print(json.dumps({"verdict": result.get("verdict"), "output": str(args.output)}, indent=2))
        return
    if args.command == "evolve":
        try:
            evolve(
                args.mission,
                budget=args.budget,
                output=args.output,
                variation_command=args.variation_command,
                provider_timeout_seconds=args.provider_timeout_seconds,
            )
        except FileExistsError:
            raise SystemExit(2)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            raise SystemExit(1)
        return
    if args.command == "publish":
        try:
            result = publish_evidence(args.mission, args.preflight, args.output)
        except Exception as exc:
            print(str(exc), file=sys.stderr)
            raise SystemExit(1)
        print(json.dumps({"verdict": result["verdict"], "output": str(args.output)}, indent=2))
        return
    if args.command == "order":
        try:
            payload = emit_strong_human_order(args.mission, args.corpus)
        except Exception as exc:
            print(str(exc), file=sys.stderr)
            raise SystemExit(1)
        print(json.dumps(payload, indent=2))
        return
    if args.command == "inspect":
        code = inspect_run(
            args.run_dir,
            verify=args.verify,
            replay_retained=args.replay_retained,
        )
        raise SystemExit(code)
    parser.error(f"unknown command: {args.command}")


if __name__ == "__main__":
    main()
