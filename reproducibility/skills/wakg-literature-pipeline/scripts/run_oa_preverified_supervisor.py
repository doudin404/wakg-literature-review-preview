#!/usr/bin/env python3
"""Process receipts and offline route diagnostics for the preverified canary.

The supervisor never invokes transport unless its explicit dispatch command is
used with --allow-network.  inspect and reap are local-only.
"""
from __future__ import annotations
import argparse, ctypes, hashlib, importlib.util, json, os, subprocess, sys, time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("wakg_pipeline", HERE / "pipeline.py")
pipeline = importlib.util.module_from_spec(SPEC); assert SPEC and SPEC.loader; SPEC.loader.exec_module(pipeline)

TERMINAL = {"COMPLETED", "FAILED"}; NONTERMINAL = {"DISPATCHED", "RUNNING", "UNCERTAIN_RUNNING"}

def _root(project: Path, run: str) -> Path: return project / "runs" / run
def _receipt(project: Path, run: str) -> Path: return _root(project, run) / "preverified-process-receipt.json"
def _outcome(project: Path, run: str) -> Path: return project / "internal_assets" / "oa-preverified" / run / "supervisor-outcome.json"
def _sha(path: Path) -> str: return pipeline.sha256_path(path)
def _json(path: Path) -> dict[str, Any]: return pipeline.load_json(path)

def _process_start_identity(pid: int) -> str | None:
    if pid <= 0: return None
    if os.name == "nt":
        handle = ctypes.windll.kernel32.OpenProcess(0x0400, False, pid)
        if not handle: return None
        try:
            created = ctypes.c_ulonglong(); exit_time = ctypes.c_ulonglong(); kernel = ctypes.c_ulonglong(); user = ctypes.c_ulonglong()
            if not ctypes.windll.kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exit_time), ctypes.byref(kernel), ctypes.byref(user)): return None
            return f"win:{pid}:{created.value}"
        finally: ctypes.windll.kernel32.CloseHandle(handle)
    try:
        return f"posix:{pid}:{Path(f'/proc/{pid}/stat').read_text().split()[21]}"
    except (FileNotFoundError, IndexError): return None

def _binding(project: Path, run: str, pair: str, command: list[str]) -> dict[str, Any]:
    root = _root(project, run)
    paths = {"card": root / "experiment-card.json", "budget": root / "budget.json", "authorization": root / "live-authorization.json"}
    if not pair or any(not path.is_file() for path in paths.values()): raise ValueError("supervisor requires canonical card/budget/authorization")
    return {"pair_id": pair, "run_id": run, "card_sha256": _sha(paths["card"]), "budget_sha256": _sha(paths["budget"]), "authorization_sha256": _sha(paths["authorization"]), "command_sha256": hashlib.sha256(pipeline.stable_json(command).encode()).hexdigest()}

def _atomic(path: Path, value: dict[str, Any]) -> None: pipeline.atomic_write_json(path, value)

def _child(project: Path, outcome: Path, binding_json: str, command_json: str) -> int:
    binding, command = json.loads(binding_json), json.loads(command_json); outcome.parent.mkdir(parents=True, exist_ok=True)
    logroot = outcome.parent / "logs"; logroot.mkdir(parents=True, exist_ok=True); key = binding["command_sha256"]
    started = pipeline.utc_now(); proc = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    out, err = proc.stdout[:65536], proc.stderr[:65536]; outpath, errpath = logroot / f"{key}.stdout", logroot / f"{key}.stderr"
    pipeline.atomic_write_bytes(outpath, out); pipeline.atomic_write_bytes(errpath, err)
    _atomic(outcome, {"schema_version":1,"type":"OA_PREVERIFIED_PROCESS_OUTCOME","binding":binding,"started_at":started,"ended_at":pipeline.utc_now(),"exit_code":proc.returncode,"stdout":{"relative_path":pipeline.relative_to_project(outpath,project),"sha256":_sha(outpath),"bytes":len(out),"truncated":len(proc.stdout)>len(out)},"stderr":{"relative_path":pipeline.relative_to_project(errpath,project),"sha256":_sha(errpath),"bytes":len(err),"truncated":len(proc.stderr)>len(err)}})
    return proc.returncode

def _dispatch(project: Path, run: str, pair: str, command: list[str], *, allow_network: bool) -> dict[str, Any]:
    root, receipt, outcome = _root(project, run), _receipt(project, run), _outcome(project, run)
    if receipt.exists() or outcome.exists() or any((root / name).exists() for name in ("preverified-state.json", "probe-reservation.json", "probe-completed.json", "link-probe.json")): raise ValueError("supervisor refuses duplicate or already-live dispatch")
    binding = _binding(project, run, pair, command); root.mkdir(parents=True, exist_ok=True)
    base = {"schema_version":1,"type":"OA_PREVERIFIED_PROCESS_RECEIPT","status":"DISPATCHED","binding":binding,"command_sha256":binding["command_sha256"],"dispatched_at":pipeline.utc_now(),"network_authority":bool(allow_network),"outcome_relative_path":pipeline.relative_to_project(outcome,project),"network_requests":0}
    _atomic(receipt, base)
    child = [sys.executable, str(Path(__file__).resolve()), "--project", str(project), "_child", "--outcome", str(outcome), "--binding", pipeline.stable_json(binding), "--command", pipeline.stable_json(command)]
    proc = subprocess.Popen(child, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); identity = _process_start_identity(proc.pid)
    if not identity: raise RuntimeError("supervisor could not bind child start identity")
    running = {**base,"status":"RUNNING","pid":proc.pid,"process_start_identity":identity,"started_at":pipeline.utc_now()}; _atomic(receipt, running); proc._child_created=False; return running

def _production_command(project: Path, run: str) -> list[str]:
    root=_root(project,run); driver=(HERE/"run_oa_preverified_canary.py").resolve()
    if not driver.is_file() or driver.parent != HERE.resolve(): raise ValueError("supervisor driver allowlist mismatch")
    return [sys.executable,str(driver),"--project",str(project.resolve()),"live","--run-id",run,"--allow-network","--experiment-card",str((root/"experiment-card.json").resolve()),"--budget",str((root/"budget.json").resolve())]

def dispatch(project: Path, run: str, pair: str, *, allow_network: bool) -> dict[str, Any]:
    if not allow_network: raise ValueError("dispatch requires explicit --allow-network")
    return _dispatch(project,run,pair,_production_command(project,run),allow_network=True)

def _test_dispatch(project: Path, run: str, pair: str, command: list[str]) -> dict[str, Any]:
    """Private test hook: local fake children only; never exposed by the CLI."""
    return _dispatch(project,run,pair,command,allow_network=False)

def inspect(project: Path, run: str) -> dict[str, Any]:
    receipt = _json(_receipt(project, run)); status = receipt.get("status")
    if status in TERMINAL: return {"read_only":True, **receipt}
    pid, expected = int(receipt.get("pid") or 0), receipt.get("process_start_identity")
    actual = _process_start_identity(pid)
    observed = "RUNNING" if actual and actual == expected else "UNCERTAIN_RUNNING"
    return {"read_only":True, **receipt, "observed_status":observed, "observed_process_start_identity":actual}

def reap(project: Path, run: str) -> dict[str, Any]:
    path, receipt = _receipt(project, run), _json(_receipt(project, run))
    if receipt.get("status") in TERMINAL: return receipt
    outcome = pipeline._hybrid_safe_path(project, str(receipt.get("outcome_relative_path") or ""))
    if outcome.is_file():
        value = _json(outcome)
        if value.get("binding") != receipt.get("binding"): raise ValueError("supervisor outcome binding mismatch")
        for key in ("stdout", "stderr"):
            ref = value.get(key) or {}; p = pipeline._hybrid_safe_path(project, str(ref.get("relative_path") or ""))
            if not p.is_file() or _sha(p) != ref.get("sha256"): raise ValueError("supervisor bounded log hash mismatch")
        terminal = "COMPLETED" if value.get("exit_code") == 0 else "FAILED"; result = {**receipt,"status":terminal,"ended_at":value.get("ended_at"),"exit_code":value.get("exit_code"),"stdout":value.get("stdout"),"stderr":value.get("stderr"),"terminal_artifact_checks":{"outcome_sha256":_sha(outcome),"state_artifact_present":(_root(project, run)/"preverified-state.json").is_file()}}
        _atomic(path,result); return result
    check = inspect(project, run); result = {**receipt,"status":"UNCERTAIN_RUNNING","reap_checked_at":pipeline.utc_now(),"observed_process_start_identity":check.get("observed_process_start_identity")}; _atomic(path,result); return result

def route_diagnostic(project: Path, run: str, envelope: Path, output: Path) -> dict[str, Any]:
    root = _root(project, run); state = _json(root / "preverified-state.json"); probe = _json(root / "link-probe.json"); plan = _json(root / "preverified-plan.json"); preflight = _json(root / "preflight-manifest.json")
    if state.get("artifacts",{}).get("probe",{}).get("sha256") != _sha(root / "link-probe.json") or state.get("artifacts",{}).get("plan",{}).get("sha256") != _sha(root / "preverified-plan.json") or state.get("artifacts",{}).get("preflight",{}).get("sha256") != _sha(root / "preflight-manifest.json") or state.get("artifacts",{}).get("metadata_envelope",{}).get("sha256") != _sha(envelope): raise ValueError("route diagnostic frozen hash/binding mismatch")
    env = _json(envelope); bydoi = {pipeline.normalize_doi(row.get("doi")):row for row in env.get("results",[]) if pipeline.normalize_doi(row.get("doi"))}; classifications={row.get("url_sha256"):row.get("classification") for row in probe.get("results",[]) if isinstance(row,dict)}; pool={row.get("identity_sha256"):row for row in (_json(project/"internal_assets"/"oa-preverified"/run/"pool-catalog.json").get("members") or [])}
    members=[]
    for role, rows in (("A", ((plan.get("arms") or {}).get("A") or {}).get("members") or []), ("P", ((preflight.get("roles") or {}).get("P") or {}).get("members") or [])):
        for selected in rows:
            identity=str(selected.get("identity_sha256")); member=pool.get(identity) or next((x.get("member") for x in ((preflight.get("roles") or {}).get("P") or {}).get("rows",[]) if x.get("identity_sha256")==identity),None)
            doi=(member or {}).get("doi") or selected.get("doi"); routes=pipeline._openalex_location_routes(bydoi.get(pipeline.normalize_doi(doi),{})) if doi else []
            alternatives=[r for r in routes if r.get("url_sha256") != selected.get("url_sha256")]; qualified=[r for r in alternatives if (r.get("provenance") or {}).get("url_kind")=="pdf"]
            members.append({"role":role,"identity_sha256":identity,"selected_url_sha256":selected.get("url_sha256"),"selected_host":selected.get("hostname"),"selected_verified":str(classifications.get(selected.get("url_sha256"),"")).startswith("VERIFIED_PDF_"),"alternative_oa_route_count":len(alternatives),"untried_direct_pdf_or_repository_alternative_count":sum(1 for r in qualified if r.get("url_sha256") not in classifications),"alternative_hosts":sorted({r.get("hostname") for r in alternatives if r.get("hostname")}),"source_classes":sorted({str((r.get("provenance") or {}).get("source_type") or "unknown") for r in routes})})
    pfailed=[m for m in members if m["role"]=="P" and not m["selected_verified"]]; viable=sum(m["untried_direct_pdf_or_repository_alternative_count"]>0 for m in pfailed); report={"schema_version":1,"type":"H09_OFFLINE_ROUTE_DIAGNOSTIC","run_id":run,"network_requests":0,"model_calls":0,"wakg_accesses":0,"inputs":{"metadata_envelope_sha256":_sha(envelope),"link_probe_sha256":_sha(root/"link-probe.json"),"plan_sha256":_sha(root/"preverified-plan.json"),"preflight_sha256":_sha(root/"preflight-manifest.json")},"members":[{k:m[k] for k in ("role","identity_sha256","selected_url_sha256","selected_verified","alternative_oa_route_count","untried_direct_pdf_or_repository_alternative_count")} for m in members],"aggregates":{"p_failed_count":len(pfailed),"p_failures_with_untried_qualified_alternative":viable,"route_choice_change_count":sum(m["alternative_oa_route_count"]>0 for m in members),"domain_count":len({h for m in members for h in m["alternative_hosts"]}),"prospective_excluded_identity_count":330,"prospective_disjoint_pool_available":"UNKNOWN_NOT_TESTED"},"decision":"TUNE_BLOCKED","limitations":["Unprobed alternatives are not imputed as successful.","Prospective disjoint-pool availability is UNKNOWN_NOT_TESTED without a fresh candidate catalogue after the frozen 330-identity exclusion.","No live authorization is granted."]}
    pipeline.atomic_write_json(output,report); return report

def main(argv: list[str]|None=None) -> int:
    p=argparse.ArgumentParser(); p.add_argument("--project",type=Path,default=Path.cwd()); s=p.add_subparsers(dest="cmd",required=True)
    d=s.add_parser("dispatch"); d.add_argument("--run-id",required=True); d.add_argument("--pair-id",required=True); d.add_argument("--allow-network",action="store_true")
    for name in ("inspect","reap"): q=s.add_parser(name); q.add_argument("--run-id",required=True)
    r=s.add_parser("route-diagnostic"); r.add_argument("--run-id",required=True); r.add_argument("--metadata-envelope",type=Path,required=True); r.add_argument("--out",type=Path,required=True)
    c=s.add_parser("_child"); c.add_argument("--outcome",type=Path,required=True); c.add_argument("--binding",required=True); c.add_argument("--command",required=True)
    a=p.parse_args(argv)
    if a.cmd=="_child": return _child(a.project.resolve(),a.outcome,a.binding,a.command)
    if a.cmd=="dispatch":
        command=[sys.executable,str(HERE/"run_oa_preverified_canary.py"),"--project",str(a.project.resolve()),"live","--run-id",a.run_id,"--allow-network","--experiment-card",str(_root(a.project.resolve(),a.run_id)/"experiment-card.json"),"--budget",str(_root(a.project.resolve(),a.run_id)/"budget.json")]
        out=dispatch(a.project.resolve(),a.run_id,a.pair_id,allow_network=a.allow_network)
        print(json.dumps(out,sort_keys=True)); return 0
    out=inspect(a.project.resolve(),a.run_id) if a.cmd=="inspect" else reap(a.project.resolve(),a.run_id) if a.cmd=="reap" else route_diagnostic(a.project.resolve(),a.run_id,a.metadata_envelope,a.out)
    print(json.dumps(out,sort_keys=True)); return 0
if __name__=="__main__": raise SystemExit(main())
