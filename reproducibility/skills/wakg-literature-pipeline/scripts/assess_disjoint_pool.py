#!/usr/bin/env python3
"""Offline proof that cached source pools are identity-disjoint."""
from __future__ import annotations
import argparse, hashlib, json
from pathlib import Path
from typing import Any
def sha(p:Path)->str: return hashlib.sha256(p.read_bytes()).hexdigest()
def rows(p:Path,results=False)->list[dict[str,Any]]:
    x=json.loads(p.read_text(encoding="utf-8")).get("results" if results else "members")
    if not isinstance(x,list) or any(not isinstance(r,dict) or not isinstance(r.get("identity_sha256"),str) for r in x): raise ValueError(f"invalid identity catalog: {p.name}")
    return x
def assess(h03:Path,h05:Path,h07:Path,h08:Path,catalogs:list[Path]|None=None,h03_report:Path|None=None)->dict[str,Any]:
    paths={"h03":(h03,True),"h05":(h05,False),"h07":(h07,False),"h08":(h08,False)}; sets={}; bound={}
    for name,(p,result) in paths.items():
        rs=rows(p,result); sets[name]={r["identity_sha256"] for r in rs}
        if len(rs)!=len(sets[name]): raise ValueError(f"duplicate identity in {name}")
        bound[name]={"sha256":sha(p),"count":len(rs)}
    if {n:len(s) for n,s in sets.items()}!={"h03":30,"h05":100,"h07":100,"h08":100}: raise ValueError("frozen source pool count mismatch")
    names=list(paths); inter={f"{a}_{b}":len(sets[a]&sets[b]) for i,a in enumerate(names) for b in names[i+1:]}; union=set().union(*sets.values())
    if any(inter.values()) or len(union)!=330: raise ValueError("frozen source pools are not disjoint")
    h03_binding={"observed_stdout_sha256":sha(h03),"observed_selected_order_sha256":hashlib.sha256(json.dumps([r.get("url_sha256") for r in rows(h03,True)],sort_keys=True,separators=(",",":")).encode("utf-8")).hexdigest()}
    if h03_report:
        report=json.loads(h03_report.read_text(encoding="utf-8")); evidence=report.get("evidence",{}); selection=report.get("selection",{})
        if evidence.get("stdout_sha256")!=h03_binding["observed_stdout_sha256"] or selection.get("selected_order_sha256")!=h03_binding["observed_selected_order_sha256"]: raise ValueError("H03 report binding mismatch")
        h03_binding["report_sha256"]=sha(h03_report)
    fresh={}
    for p in catalogs or []:
        for r in rows(p):
            if r["identity_sha256"] not in union: fresh[r["identity_sha256"]]=r
    fs=list(fresh.values()); domains={r.get("legacy_host") for r in fs if r.get("legacy_host")}; strata={s for r in fs for s in r.get("query_strata",[]) if isinstance(s,str)}
    route={"repository_or_preprint_direct_pdf":sum(1 for r in fs if r.get("route_signal")=="REPOSITORY_OR_PREPRINT_DIRECT_PDF"),"publisher_direct_pdf":sum(1 for r in fs if r.get("route_signal")=="PUBLISHER_DIRECT_PDF"),"other_or_unknown":sum(1 for r in fs if r.get("route_signal") not in {"REPOSITORY_OR_PREPRINT_DIRECT_PDF","PUBLISHER_DIRECT_PDF"})}
    outcome="POOL_READY" if len(fs)>=100 and len(domains)>=20 and len(strata)>=8 else "METADATA_EXTENSION_READY"
    return {"schema_version":2,"offline_only":True,"network_requests":0,"model_calls":0,"wakg_accesses":0,"frozen_inputs":bound,"h03_binding":h03_binding,"frozen_union_count":len(union),"pairwise_intersections":inter,"compatible_catalog_count":len(catalogs or []),"fresh":{"count":len(fs),"domains":len(domains),"strata":len(strata),"route_signals":route},"outcome":outcome,"reason":None if outcome=="POOL_READY" else "Cached catalogs contain only identities already excluded by the proven 330-member union; one separately authorized metadata-only extension can answer fresh-pool coverage."}
def main()->int:
    q=argparse.ArgumentParser(); [q.add_argument(x,type=Path,required=True) for x in ("--h03","--h05","--h07","--h08")]; q.add_argument("--h03-report",type=Path); q.add_argument("--catalog",action="append",type=Path); q.add_argument("--out",type=Path,required=True); x=q.parse_args(); x.out.write_text(json.dumps(assess(x.h03,x.h05,x.h07,x.h08,x.catalog,x.h03_report),sort_keys=True,separators=(",",":"))+"\n",encoding="utf-8"); return 0
if __name__=="__main__": raise SystemExit(main())
