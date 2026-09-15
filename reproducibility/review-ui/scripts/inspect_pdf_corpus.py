#!/usr/bin/env python3
"""Inspect a PDF corpus without persisting paper text or treating it as instructions."""
from __future__ import annotations
import argparse, hashlib, json, re
from pathlib import Path
from typing import Any
import fitz

DOI_RE=re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+",re.I)

def sha(path:Path)->str:
    digest=hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b""):digest.update(chunk)
    return digest.hexdigest()

def inspect(path:Path)->dict[str,Any]:
    digest=sha(path)
    with fitz.open(path) as doc:
        metadata=doc.metadata or {};chars=[];first_text=[]
        for index,page in enumerate(doc):
            text=page.get_text("text")
            chars.append(len(text.strip()))
            if index<2:first_text.append(text[:25000])
        doi_match=DOI_RE.search("\n".join(first_text))
        return {"filename":path.name,"sha256":digest,"bytes":path.stat().st_size,"pages":len(doc),"encrypted":bool(doc.needs_pass),"title":str(metadata.get("title") or "").strip() or None,"author":str(metadata.get("author") or "").strip() or None,"doi":doi_match.group(0).rstrip(".,;)") if doi_match else None,"text_chars_total":sum(chars),"low_text_pages":sum(value<80 for value in chars),"text_layer_status":"INSUFFICIENT_OR_SCANNED" if chars and sum(value<80 for value in chars)/len(chars)>.5 else "AVAILABLE"}

def main()->int:
    parser=argparse.ArgumentParser();parser.add_argument("pdf_dir",type=Path);parser.add_argument("--out",type=Path);args=parser.parse_args();root=args.pdf_dir.resolve();pdfs=sorted(root.glob("*.pdf"),key=lambda p:p.name.lower())
    if not pdfs:raise ValueError("no PDF files found")
    rows=[];errors=[]
    for path in pdfs:
        try:rows.append(inspect(path))
        except Exception as exc:errors.append({"filename":path.name,"error_type":type(exc).__name__,"message":str(exc)[:300]})
    by_hash:dict[str,list[str]]={}
    for row in rows:by_hash.setdefault(row["sha256"],[]).append(row["filename"])
    report={"schema_version":1,"source_type":"USER_SUPPLIED_TEST_PDF_CORPUS","paper_count":len(rows),"error_count":len(errors),"total_bytes":sum(row["bytes"] for row in rows),"total_pages":sum(row["pages"] for row in rows),"duplicate_groups":[names for names in by_hash.values() if len(names)>1],"text_layer_available":sum(row["text_layer_status"]=="AVAILABLE" for row in rows),"papers":rows,"errors":errors,"paper_text_persisted":False}
    text=json.dumps(report,ensure_ascii=False,sort_keys=True,separators=(",",":"))+"\n"
    if args.out:args.out.write_text(text,encoding="utf-8")
    else:print(text,end="")
    return 0 if not errors else 2

if __name__=="__main__":raise SystemExit(main())
