#!/usr/bin/env python3
"""H07 bounded preverified-ranking controller; public output is body-free."""
from __future__ import annotations
import argparse, importlib.util, json
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("wakg_pipeline", HERE / "pipeline.py")
pipeline = importlib.util.module_from_spec(SPEC); assert SPEC and SPEC.loader; SPEC.loader.exec_module(pipeline)

def main(argv: list[str] | None = None) -> int:
    root = argparse.ArgumentParser(description="OA preverified ranking canary controller"); root.add_argument("--project", type=Path, default=Path.cwd()); commands = root.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="offline pool/A/budget preparation"); prepare.add_argument("--run-id", required=True); prepare.add_argument("--experiment-card", type=Path, required=True); prepare.add_argument("--h03-catalog", type=Path, required=True); prepare.add_argument("--h03-probe", type=Path, required=True); prepare.add_argument("--h05-pool", type=Path, required=True); prepare.add_argument("--additional-exclusion", action="append", type=Path, default=[], help="frozen member-envelope to exclude; repeatable")
    authorize = commands.add_parser("authorize-live", help="bind independently accepted frozen inputs before future live transport"); authorize.add_argument("--run-id", required=True); authorize.add_argument("--acceptance", type=Path, required=True)
    stage = commands.add_parser("stage-live", help="copy one fresh accepted live input set to an isolated destination project"); stage.add_argument("--run-id", required=True); stage.add_argument("--destination", type=Path, required=True)
    publish = commands.add_parser("publish-live", help="zero-network publish of a verified isolated live execution"); publish.add_argument("--run-id", required=True); publish.add_argument("--staged-project", type=Path, required=True)
    live = commands.add_parser("live", help="future opt-in one-metadata/one-probe controller"); live.add_argument("--run-id", required=True); live.add_argument("--allow-network", action="store_true"); live.add_argument("--experiment-card", type=Path, required=True); live.add_argument("--budget", type=Path, required=True)
    replay = commands.add_parser("replay", help="zero-network cached replay"); replay.add_argument("--run-id", required=True)
    verify = commands.add_parser("verify", help="independent cached verification"); verify.add_argument("--run-id", required=True)
    reconcile = commands.add_parser("reconcile-summary", help="zero-network replacement of one known-invalid summary"); reconcile.add_argument("--run-id", required=True); reconcile.add_argument("--old-summary-sha256", required=True); reconcile.add_argument("--probe-sha256", required=True)
    args = root.parse_args(argv); project = args.project.resolve()
    try:
        if args.command == "prepare": out = pipeline.preverified_prepare(project, args.run_id, args.experiment_card, args.h03_catalog, args.h03_probe, args.h05_pool, args.additional_exclusion)
        elif args.command == "authorize-live": out = pipeline.preverified_authorize_live(project, args.run_id, args.acceptance)
        elif args.command == "stage-live": out = pipeline.preverified_stage_live(project, args.run_id, args.destination)
        elif args.command == "publish-live": out = pipeline.preverified_publish_live(project, args.run_id, args.staged_project)
        elif args.command == "live":
            if not args.allow_network: raise ValueError("live requires explicit --allow-network")
            out = pipeline.preverified_live_controller(project, args.run_id, args.experiment_card, args.budget)
        elif args.command == "replay": out = pipeline.preverified_replay_controller(project, args.run_id)
        elif args.command == "reconcile-summary": out = pipeline.preverified_reconcile_summary(project,args.run_id,args.old_summary_sha256,args.probe_sha256)
        else: out = pipeline.preverified_verify_controller(project, args.run_id)
    except Exception as exc:
        if args.command in {"stage-live", "publish-live"}: out = {"schema_version": 1, "type": "OA_PREVERIFIED_LIVE_STAGE" if args.command == "stage-live" else "OA_PREVERIFIED_LIVE_PUBLICATION", "run_id": getattr(args, "run_id", "unknown"), "verdict": "INVALID", "error_type": type(exc).__name__, "network_requests": 0}
        else: out = pipeline.preverified_failure_envelope(project, getattr(args, "run_id", "unknown"), args.command, exc)
        print(json.dumps(out, ensure_ascii=False, sort_keys=True)); return 2
    print(json.dumps(out, ensure_ascii=False, sort_keys=True)); return 0

if __name__ == "__main__": raise SystemExit(main())
