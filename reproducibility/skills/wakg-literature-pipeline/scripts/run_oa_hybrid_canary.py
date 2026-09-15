#!/usr/bin/env python3
"""Resumable controller for the preregistered OA hybrid-ranking canary.

``prepare`` is offline. A future authorised ``live`` run is bounded to one
OpenAlex DOI-or metadata request and one link-probe pass; ``replay`` and
``verify`` are cache-only and independently recompute the published result.
All public reports are body-free.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("wakg_pipeline", HERE / "pipeline.py")
pipeline = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(pipeline)


def body_free_failure(project: Path, run_id: str, command: str, error: Exception) -> dict:
    """Delegate counter derivation to the production state-aware helper."""
    return pipeline.hybrid_failure_envelope(project, run_id, command, error)


def add_prepare_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--h03-catalog", type=Path, required=True)
    parser.add_argument("--h03-probe", type=Path, required=True)
    parser.add_argument("--h05-pool", type=Path, required=True)
    parser.add_argument("--h05-arm-manifest", type=Path, required=True)
    parser.add_argument("--h05-envelope", type=Path, required=True)
    parser.add_argument("--h05-union", type=Path, required=True)
    parser.add_argument("--h05-probe", type=Path, required=True)
    parser.add_argument("--experiment-card", type=Path, help="Required to bind a viable profile for future live use")


def prepared(project: Path, args: argparse.Namespace) -> dict:
    output = pipeline.hybrid_prepare(project, args.run_id, args.h03_catalog, args.h03_probe, args.h05_pool,
                                     args.h05_arm_manifest, args.h05_envelope, args.h05_union, args.h05_probe)
    if output.get("status") == "PREPARED" and args.experiment_card:
        output = pipeline.hybrid_bind_experiment_card(project, args.run_id, args.experiment_card)
    return output


def main(argv: list[str] | None = None) -> int:
    root = argparse.ArgumentParser(description="OA hybrid canary controller")
    root.add_argument("--project", type=Path, default=Path.cwd())
    commands = root.add_subparsers(dest="command", required=True)
    prepare_parser = commands.add_parser("prepare", help="offline tune and freeze a viable prospective cohort"); add_prepare_args(prepare_parser)
    replay_parser = commands.add_parser("replay", help="zero-network replay of a completed cached live run"); replay_parser.add_argument("--run-id", required=True)
    verify_parser = commands.add_parser("verify", help="independently recompute a completed cached live run"); verify_parser.add_argument("--run-id", required=True)
    live_parser = commands.add_parser("live", help="future opt-in bounded metadata and link-probe controller")
    live_parser.add_argument("--run-id", required=True)
    live_parser.add_argument("--allow-network", action="store_true")
    live_parser.add_argument("--experiment-card", type=Path, required=True)
    live_parser.add_argument("--budget", type=Path, required=True)
    args = root.parse_args(argv); project = args.project.resolve()
    try:
        if args.command == "prepare":
            output = prepared(project, args)
        elif args.command == "live":
            if not args.allow_network:
                raise ValueError("live requires explicit --allow-network")
            output = pipeline.hybrid_live_controller(project, args.run_id, args.experiment_card, args.budget)
        elif args.command == "replay":
            output = pipeline.hybrid_replay_controller(project, args.run_id)
        else:
            output = pipeline.hybrid_verify_controller(project, args.run_id)
    except Exception as exc:
        output = body_free_failure(project, getattr(args, "run_id", "unknown"), args.command, exc)
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
        return 2
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
