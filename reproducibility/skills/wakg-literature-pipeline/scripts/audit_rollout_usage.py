#!/usr/bin/env python3
"""Read-only local rollout proxy accounting; never official credit accounting."""
from __future__ import annotations
import argparse, hashlib, json, os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LABEL = "LOCAL_ROLLOUT_PROXY_NOT_OFFICIAL_CREDITS"
def stamp(v: object) -> datetime:
    if not isinstance(v, str): raise ValueError("missing RFC3339 timestamp")
    x = datetime.fromisoformat(v.replace("Z", "+00:00"))
    if x.tzinfo is None: raise ValueError("UTC timestamp required")
    return x.astimezone(timezone.utc)
def norm(p: Path) -> str: return os.path.normcase(os.path.normpath(str(p.resolve())))
def physical(p: Path) -> tuple[object, object]:
    s=p.stat(); return (s.st_dev,s.st_ino) if s.st_ino else ("path",norm(p))
def payload(v: object) -> dict[str, Any]: return v.get("payload",{}) if isinstance(v,dict) and isinstance(v.get("payload"),dict) else {}
def usage(v: object) -> dict[str,int]:
    if not isinstance(v,dict): raise ValueError("missing last_token_usage")
    keys=("input_tokens","output_tokens","cached_input_tokens","reasoning_output_tokens")
    if any(not isinstance(v.get(k),int) or v[k]<0 for k in keys): raise ValueError("malformed token usage")
    if v["cached_input_tokens"]>v["input_tokens"]: raise ValueError("cached input exceeds input")
    # These totals are rollout-level/cumulative observations, not event deltas.
    # They are deliberately neither validated nor included in any token metric.
    return {**{k:v[k] for k in keys}, "cumulative_total_fields_present": int("total_tokens" in v) + int("total_token_usage" in v)}
def files(roots: list[Path]) -> list[Path]:
    out: dict[tuple[object,object],Path]={}
    for root in sorted({norm(x) for x in roots}):
        p=Path(root); paths=[p] if p.is_file() else sorted((x for x in p.rglob("*") if x.is_file()),key=norm) if p.is_dir() else None
        if paths is None: raise ValueError("rollout root missing")
        for x in paths: out.setdefault(physical(x),x)
    return [out[k] for k in sorted(out,key=repr)]
def session_meta(v: dict[str,Any], fallback: str) -> tuple[str,str|None]:
    p=payload(v)
    if v.get("type")!="session_meta" and p.get("type")!="session_meta": return fallback,None
    return str(p.get("id") or p.get("session_id") or v.get("session_id") or fallback), str(p.get("cwd") or v.get("cwd")) if (p.get("cwd") or v.get("cwd")) is not None else None
def label(labels:dict[str,Any],sid:str,task:str|None)->tuple[str,str]:
    sessions=labels.get("sessions",{}); tasks=labels.get("tasks", labels if all(isinstance(x,dict) for x in labels.values()) else {})
    v=(sessions.get(sid) if isinstance(sessions,dict) else None) or (tasks.get(task) if task and isinstance(tasks,dict) else None)
    if not isinstance(v,dict): raise ValueError("incomplete in-scope session/task mapping")
    mapped=str(v.get("task_id") or task or ""); role=v.get("role")
    if not mapped or not isinstance(role,str) or not role: raise ValueError("incomplete in-scope session/task mapping")
    if task and v.get("task_id") and str(v["task_id"])!=task: raise ValueError("session/task mapping conflict")
    return mapped,role
def audit(roots:list[Path],start:str,end:str,labels:dict[str,Any],project_cwd:Path)->dict[str,Any]:
    a,b=stamp(start),stamp(end)
    if a>=b: raise ValueError("invalid half-open window")
    project=norm(project_cwd); rows=[]; count=0; source_hashes=[]
    for path in files(roots):
        count+=1; records=[]; source_sha=hashlib.sha256(path.read_bytes()).hexdigest(); source_hashes.append(source_sha)
        for n,line in enumerate(path.read_text(encoding="utf-8").splitlines(),1):
            try: v=json.loads(line)
            except json.JSONDecodeError as exc:
                if records and norm(Path(str(session_meta(records[0],path.stem)[1] or "")))==project: raise ValueError(f"malformed in-scope rollout record at line {n}") from exc
                continue
            if isinstance(v,dict): records.append(v)
        if not records: continue
        sid,cwd=session_meta(records[0],path.stem)
        if cwd is None or norm(Path(cwd))!=project: continue
        current=None
        for n,v in enumerate(records,1):
            p=payload(v)
            if v.get("type")=="turn_context" or p.get("type")=="turn_context": current=str(p.get("task_id") or v.get("task_id") or current or "") or None
            if p.get("type")!="token_count": continue
            if not a<=stamp(v.get("timestamp") or p.get("timestamp"))<b: continue
            info=p.get("info")
            if not isinstance(info,dict): raise ValueError(f"malformed in-scope token_count at line {n}")
            task=p.get("task_id") or v.get("task_id") or current; task,role=label(labels,sid,str(task) if task else None)
            item=usage(info.get("last_token_usage"))
            rows.append({"task_id":task,"role":role,"usage":item,"session_id":sid,"timestamp":str(v.get("timestamp") or p.get("timestamp")),"source_sha256":source_sha})
    if not rows: raise ValueError("absent in-scope token_count events")
    def agg(items):
        i=sum(x["usage"]["input_tokens"] for x in items); o=sum(x["usage"]["output_tokens"] for x in items); c=sum(x["usage"]["cached_input_tokens"] for x in items)
        return {"events":len(items),"raw":i+o,"uncached_input":i-c,"output":o,"proxy":i-c+o,"cache_hit":round(c/i,6) if i else 0.0,"reasoning_output_reported":sum(x["usage"]["reasoning_output_tokens"] for x in items),"cumulative_total_fields_present":sum(x["usage"]["cumulative_total_fields_present"] for x in items)}
    groups={}
    for r in rows: groups.setdefault((r["task_id"],r["role"]),[]).append(r)
    receipts=[]
    for row in rows:
        item=row["usage"]
        receipts.append({"session_id":row["session_id"],"task_id":row["task_id"],"role":row["role"],"timestamp":row["timestamp"],"source_sha256":row["source_sha256"],"input_tokens":item["input_tokens"],"cached_input_tokens":item["cached_input_tokens"],"output_tokens":item["output_tokens"],"reasoning_output_tokens_reported":item["reasoning_output_tokens"],"proxy":item["input_tokens"]-item["cached_input_tokens"]+item["output_tokens"]})
    return {"schema_version":3,"label":LABEL,"accounting_policy":{"usage_source":"event_msg.payload.info.last_token_usage","event_rule":"each in-window token_count event exactly once","raw_formula":"input_tokens + output_tokens","proxy_formula":"input_tokens - cached_input_tokens + output_tokens","reasoning_policy":"reasoning_output_tokens is reported for audit only and is not added because it is included in output_tokens","cumulative_policy":"total_tokens and total_token_usage are presence-counted only and never summed","official_credits":False},"window":{"start_utc":start,"end_utc":end,"half_open":True},"event_count":len(rows),"source_file_count":count,"source_file_sha256":sorted(source_hashes),"event_receipts":receipts,"total":agg(rows),"per_task_role":[{"task_id":k[0],"role":k[1],**agg(v)} for k,v in sorted(groups.items())]}
def main()->int:
    q=argparse.ArgumentParser(); q.add_argument("--root",action="append",type=Path,required=True); q.add_argument("--start",required=True); q.add_argument("--end",required=True); q.add_argument("--labels",type=Path,required=True); q.add_argument("--project-cwd",type=Path,default=Path.cwd(),help="project directory; defaults to the current resolved directory so a rendered Unicode cwd never needs to be copied from logs"); q.add_argument("--out",type=Path)
    x=q.parse_args(); out=audit(x.root,x.start,x.end,json.loads(x.labels.read_text(encoding="utf-8")),x.project_cwd); text=json.dumps(out,sort_keys=True,separators=(",",":"))+"\n"
    if x.out: x.out.write_text(text,encoding="utf-8")
    else: print(text,end="")
    return 0
if __name__=="__main__": raise SystemExit(main())
