#!/usr/bin/env python3
"""Build and verify compact, PDF-bound packets for pre-human WAKG review.

The packet builder removes reviewer-side repository discovery and groups all
claims from one record/page into a single source context.  The verifier is a
separate, model-free pass that reopens the immutable PDFs and checks every
published locator before an agent sees the packet.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import fitz


PLAN_KIND = "WAKG_COMPACT_PREHUMAN_REVIEW_PLAN"
PACKET_KIND = "WAKG_COMPACT_PREHUMAN_REVIEW_PACKET"
PROOF_KIND = "WAKG_COMPACT_PREHUMAN_REVIEW_PROOF"
BUDGET_KIND = "WAKG_PREHUMAN_REVIEW_BUDGET_VERDICT"
USAGE_LABEL = "LOCAL_ROLLOUT_PROXY_NOT_OFFICIAL_CREDITS"
SOURCE_CODES = {"DIRECT_SOURCE_VALUE", "SOURCE_DERIVED_VALUE", "SOURCE_COMPOSITE_VALUE"}
DEFAULT_PROXY_BUDGET = 1_000_000
DEFAULT_EVENT_BUDGET = 24
REVIEW_PROTOCOL_VERSION = "compact-prehuman-review-v5-curve-proof"
WAKG_CONTRACT_VERSION = "WAKG-V1.1.4"
PHASE_C_ROOT = Path("runs/user-corpus-10-efficiency-v2/phase-c-full10")


def stable_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest_value(value: Any) -> str:
    return hashlib.sha256(stable_bytes(value)).hexdigest()


def digest_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def atomic_text(path:Path,value:str)->None:
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,temporary=tempfile.mkstemp(prefix=path.name+".",suffix=".tmp",dir=path.parent)
    try:
        with os.fdopen(fd,"w",encoding="utf-8",newline="\n") as stream:stream.write(value)
        os.replace(temporary,path)
    except Exception:
        try:os.unlink(temporary)
        except FileNotFoundError:pass
        raise


def project_path(project: Path, value: str | Path) -> Path:
    path = Path(value)
    resolved = (path if path.is_absolute() else project / path).resolve()
    if resolved != project and project not in resolved.parents:
        raise ValueError(f"path escapes project: {value}")
    return resolved


def rel(project: Path, path: Path) -> str:
    return path.resolve().relative_to(project).as_posix()


def _source_pdf(project: Path, paper: dict[str, Any]) -> Path:
    run_id = str(paper.get("runId") or "")
    manifest_path = project / "runs" / run_id / "manifest.json"
    manifest = load_object(manifest_path)
    pdf_value = str((manifest.get("input") or {}).get("pdf_path") or "")
    if not pdf_value:
        raise ValueError(f"missing source PDF path: {run_id}")
    pdf = project_path(project, pdf_value)
    if not pdf.is_file():
        raise ValueError(f"missing source PDF: {run_id}")
    expected = str(paper.get("pdfSha256") or "")
    actual = digest_file(pdf)
    if actual != expected:
        raise ValueError(f"source PDF hash mismatch: {run_id}")
    return pdf


def _evidence_index(paper: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    index: dict[str, list[dict[str, Any]]] = {}
    for page in paper.get("pages") or []:
        page_number = int(page.get("number") or 0)
        for item in page.get("evidence") or []:
            key = str(item.get("evidenceKey") or "")
            if not key:
                continue
            bbox = item.get("bbox")
            token = str(item.get("sourceToken") or item.get("snippet") or "").strip()
            if page_number < 1 or not isinstance(bbox, list) or len(bbox) != 4 or not token:
                raise ValueError(f"incomplete evidence locator: {paper.get('runId')}:{key}")
            index.setdefault(key, []).append({
                "page": page_number,
                "bbox": [float(value) if item.get('evidenceMode') in {'reviewed-sparse-geometry','reviewed-main-figure-markers'} else round(float(value), 3) for value in bbox],
                "sourceToken": token,
                "mode": str(item.get("evidenceMode") or ""),
                "fieldPath": item.get("fieldPath"),
                "canonicalEvidenceKey": item.get("canonicalEvidenceKey"),
                "curveSubject": item.get("curveSubject"),
                "sparseSubject": item.get("sparseSubject"),
                "supplementPsdSubject": item.get("supplementPsdSubject"),
                "supplementMarkerSubject": item.get("supplementMarkerSubject"),
                "mainMarkerSubject": item.get("mainMarkerSubject"),
                "coordinateSpace": item.get("coordinateSpace"),
                "evidenceKey":key,"sourceEvidenceKey":item.get('sourceEvidenceKey'),"recordKey":item.get('recordKey'),
                "sourceKind": str(page.get("sourceKind") or "main"),
                "sourceArtifactPath": page.get("sourceArtifactPath"),
                "sourceArtifactSha256": page.get("sourceArtifactSha256"),
                "sourcePage": int(page.get("sourcePage") or page_number),
                "sourceWidth": float(page.get("width") or 0),
                "sourceHeight": float(page.get("height") or 0),
            })
    return index


def _formal_fields(card: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        field for field in card.get("fields") or []
        if field.get("status") == "reported" and field.get("value") not in (None, "")
    ]


def _typed_nulls(card: dict[str, Any]) -> list[dict[str, Any]]:
    values = []
    for field in card.get("fields") or []:
        if field.get("value") is not None:
            continue
        status = str(field.get("status") or "")
        if status not in {"not_reported", "not_applicable"}:
            raise ValueError(f"unreviewable null state: {card.get('key')}:{field.get('label')}:{status}")
        values.append({
            "fieldLabel": field.get("label"),
            "semanticRole": field.get("semanticRole"),
            "valueType": "null",
            "status": status,
            "reason": field.get("reason"),
            "sourceCode": field.get("sourceExplanationCode"),
        })
    return values


def _value_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def _claim(run_id: str, group: str, card: dict[str, Any], field: dict[str, Any], locators: list[dict[str, Any]]) -> dict[str, Any]:
    source_code = str(field.get("sourceExplanationCode") or "")
    if source_code not in SOURCE_CODES:
        raise ValueError(f"uncontrolled source explanation: {run_id}:{card.get('key')}:{field.get('label')}")
    formula = field.get("sourceFormula") or (field.get("transformation") or {}).get("formula")
    if source_code != "DIRECT_SOURCE_VALUE" and not formula:
        raise ValueError(f"derived claim lacks formula: {run_id}:{card.get('key')}:{field.get('label')}")
    if source_code == "DIRECT_SOURCE_VALUE" and (field.get("sourceFormula") or field.get("transformation")):
        raise ValueError(f"direct claim unexpectedly has a transformation: {run_id}:{card.get('key')}:{field.get('label')}")
    identity = {
        "runId": run_id,
        "recordKey": card.get("key"),
        "fieldLabel": field.get("label"),
        "value": field.get("value"),
        "valueType": _value_type(field.get("value")),
        "unit": field.get("unit"),
        "evidenceKey": field.get("evidenceKey"),
    }
    return {
        "claimId": "claim-" + digest_value(identity)[:24],
        "recordGroup": group,
        "recordKey": card.get("key"),
        "recordTitle": card.get("title") or card.get("label"),
        "fieldLabel": field.get("label"),
        "semanticRole": field.get("semanticRole"),
        "value": field.get("value"),
        "unit": field.get("unit"),
        "originalValue": field.get("originalValue"),
        "originalUnit": field.get("originalUnit"),
        "sourceCode": source_code,
        "formula": formula,
        "sourceBinding": field.get("sourceBinding"),
        "curveKey": field.get("curveKey"),
        "displayValue":field.get('displayValue'),"digitization":field.get('digitization'),
        "transformation":field.get('transformation'),
        "evidenceKey": field.get("evidenceKey"),
        "locators": [dict(locator) for locator in locators],
    }


def _cluster_contexts(doc: fitz.Document, claims: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    by_record_page: dict[tuple[str, int], list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for claim in claims:
        for locator in claim["locators"]:
            by_record_page.setdefault((str(claim["recordKey"]), int(locator["page"])), []).append((claim, locator))
    contexts: list[dict[str, Any]] = []
    for (record_key, page_number), members in sorted(by_record_page.items()):
        ordered = sorted(members, key=lambda member: (member[1]["bbox"][1], member[1]["bbox"][0]))
        clusters: list[list[tuple[dict[str, Any], dict[str, Any]]]] = []
        for member in ordered:
            y0, y1 = member[1]["bbox"][1], member[1]["bbox"][3]
            if not clusters or y0 > max(item[1]["bbox"][3] for item in clusters[-1]) + 18:
                clusters.append([member])
            else:
                clusters[-1].append(member)
        for cluster in clusters:
            boxes = [member[1]["bbox"] for member in cluster]
            source_locator=cluster[0][1]
            is_main=str(source_locator.get("sourceKind") or "main")=="main" and 1<=int(source_locator.get("sourcePage") or page_number)<=len(doc)
            page=doc[int(source_locator.get("sourcePage") or page_number)-1] if is_main else None
            width=float(page.rect.width) if page is not None else max(float(box[2]) for box in boxes)+8.0
            height=float(page.rect.height) if page is not None else max(float(box[3]) for box in boxes)+5.0
            rect = fitz.Rect(
                max(0.0, min(box[0] for box in boxes) - 8.0),
                max(0.0, min(box[1] for box in boxes) - 5.0),
                min(width, max(box[2] for box in boxes) + 8.0),
                min(height, max(box[3] for box in boxes) + 5.0),
            )
            text = page.get_textbox(rect).strip() if page is not None else "\n".join(str(member[1].get("sourceToken") or "") for member in cluster).strip()
            context_id = "ctx-" + digest_value({"record": record_key, "page": page_number, "bbox": list(rect), "text": text})[:24]
            context = {
                "contextId": context_id,
                "recordKey": record_key,
                "page": page_number,
                "bbox": [round(value, 3) for value in rect],
                "text": text,
                "textSha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "sourceKind": source_locator.get("sourceKind") or "main",
                "sourceArtifactPath": source_locator.get("sourceArtifactPath"),
                "sourceArtifactSha256": source_locator.get("sourceArtifactSha256"),
                "sourcePage": int(source_locator.get("sourcePage") or page_number),
                "syntheticContext": page is None,
            }
            contexts.append(context)
            for _, locator in cluster:
                locator["contextId"] = context_id
    # Add one preceding section/header context for each vertically separated
    # table or prose region.  This preserves the column/row semantics that a
    # scalar bbox alone cannot prove.
    sections: list[dict[str, Any]] = []
    by_page: dict[int, list[dict[str, Any]]] = {}
    for context in contexts:
        by_page.setdefault(int(context["page"]), []).append(context)
    for page_number, page_contexts in sorted(by_page.items()):
        ordered = sorted(page_contexts, key=lambda item: (item["bbox"][1], item["bbox"][0]))
        groups: list[list[dict[str, Any]]] = []
        for context in ordered:
            if not groups or context["bbox"][1] > max(item["bbox"][3] for item in groups[-1]) + 70:
                groups.append([context])
            else:
                groups[-1].append(context)
        for group in groups:
            source=group[0]
            is_main=str(source.get("sourceKind") or "main")=="main" and 1<=int(source.get("sourcePage") or page_number)<=len(doc)
            page=doc[int(source.get("sourcePage") or page_number)-1] if is_main else None
            top = min(item["bbox"][1] for item in group)
            height=float(page.rect.height) if page is not None else max(item["bbox"][3] for item in group)+8.0
            width=float(page.rect.width) if page is not None else max(item["bbox"][2] for item in group)+8.0
            bottom = min(height, min(item["bbox"][3] for item in group) + 8.0)
            rect = fitz.Rect(0.0, max(0.0, top - 180.0), width, bottom)
            text = page.get_textbox(rect).strip() if page is not None else "\n".join(item["text"] for item in group).strip()
            section_id = "section-" + digest_value({"page": page_number, "bbox": list(rect), "text": text})[:24]
            sections.append({
                "sectionId": section_id,
                "page": page_number,
                "bbox": [round(value, 3) for value in rect],
                "text": text,
                "textSha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "sourceKind": source.get("sourceKind") or "main",
                "sourceArtifactPath": source.get("sourceArtifactPath"),
                "sourceArtifactSha256": source.get("sourceArtifactSha256"),
                "sourcePage": int(source.get("sourcePage") or page_number),
                "syntheticContext": page is None,
            })
            for context in group:
                context["sectionId"] = section_id
    return contexts, sections


def _canonical_records(project: Path, run_id: str) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    paths = [project / "runs" / run_id / "records.json", project / PHASE_C_ROOT / run_id / "generated-records.json"]
    for path in paths:
        if not path.is_file():
            continue
        records = load_object(path)
        for record in records.get("mats") or []:
            key = str(record.get("mat_key") or "")
            if key:
                index[key] = record
        for record in records.get("mixes") or []:
            key = str(record.get("mix_key") or "")
            if key:
                index[key] = record
    return index


def _phase_c_records(project: Path, run_id: str) -> dict[str, list[dict[str, Any]]]:
    path=project / PHASE_C_ROOT / run_id / "generated-records.json"
    if not path.is_file():return {"mats":[],"mixes":[]}
    value=load_object(path)
    return {"mats":list(value.get("mats") or []),"mixes":list(value.get("mixes") or [])}


def _record_contract(group: str, record: dict[str, Any] | None, card: dict[str, Any]) -> dict[str, Any]:
    record = record or {}
    materials = ((record.get("modules") or {}).get("materials") or {}) if group == "mixes" else {}
    key_name = "mat_key" if group == "mats" else "mix_key"
    return {
        "contractVersion": WAKG_CONTRACT_VERSION,
        "recordType": "MAT" if group == "mats" else "MIX",
        "canonicalKeyField": key_name,
        "canonicalKey": record.get(key_name) or card.get("key"),
        "reviewRecordKey": card.get("key"),
        "schemaVersion": record.get("schema_version"),
        "paperKey": record.get("paper_key"),
        "matRefs": list(materials.get("mat_refs") or []),
        "topLevelFields": sorted(record.keys()),
        "moduleFields": sorted((record.get("modules") or {}).keys()),
    }


def build_paper_packet(project: Path, paper: dict[str, Any]) -> dict[str, Any]:
    run_id = str(paper.get("runId") or "")
    pdf = _source_pdf(project, paper)
    evidence = _evidence_index(paper)
    canonical = _canonical_records(project, run_id)
    phase_c = _phase_c_records(project,run_id)
    main_marker_subject=paper.get('mainMarkerSubject')
    phase_path=project/PHASE_C_ROOT/run_id/'generated-records.json'
    if phase_path.is_file() and json.loads(phase_path.read_text(encoding='utf-8')).get('extensions',{}).get('main_figure_merge'):
        import sys
        if str(project/'scripts') not in sys.path:sys.path.insert(0,str(project/'scripts'))
        import main_marker_review_projection as main_review
        if main_marker_subject!=main_review.subject_for(project,phase_path):
            raise ValueError('main marker paper subject missing or differs from source records')
    review_mat_keys={str(card.get("key") or "") for card in (paper.get("records") or {}).get("mats") or []}
    phase_mat_keys={str(record.get("mat_key") or "") for record in phase_c["mats"]}
    use_phase_c_relations=bool(review_mat_keys and review_mat_keys==phase_mat_keys)
    claims: list[dict[str, Any]] = []
    nulls: list[dict[str, Any]] = []
    contracts: list[dict[str, Any]] = []
    for group in ("mats", "mixes"):
        cards=(paper.get("records") or {}).get(group) or []
        for card_index,card in enumerate(cards):
            record=canonical.get(str(card.get("key") or ""))
            # The review projection intentionally keeps stable canonical MIX
            # display keys while enriching its rows from Phase C.  Use the
            # same index-aligned Phase C record for the relationship contract;
            # otherwise the packet would pair promoted MATs with the obsolete
            # anonymous canonical MAT reference.
            if group=="mixes" and use_phase_c_relations and len(phase_c["mixes"])==len(cards):record=phase_c["mixes"][card_index]
            if record is None:raise ValueError(f"review card lacks a WAKG record contract: {run_id}:{card.get('key')}")
            contracts.append(_record_contract(group,record,card))
            for item in _typed_nulls(card):
                nulls.append({"recordGroup": group, "recordKey": card.get("key"), **item})
            for field in _formal_fields(card):
                key = str(field.get("evidenceKey") or "")
                locators = evidence.get(key)
                if not key or not locators:
                    raise ValueError(f"formal claim lacks evidence: {run_id}:{card.get('key')}:{field.get('label')}")
                claims.append(_claim(run_id, group, card, field, locators))
    with fitz.open(pdf) as doc:
        contexts, sections = _cluster_contexts(doc, claims)
        page_count = len(doc)
    locator_table: dict[str, dict[str, Any]] = {}
    for claim in claims:
        refs = []
        for locator in claim.pop("locators"):
            locator_id = "loc-" + digest_value(locator)[:24]
            locator_table.setdefault(locator_id, locator)
            refs.append(locator_id)
        claim["locatorRefs"] = refs
    return {
        "runId": run_id,
        "paperKey": paper.get("paperKey"),
        "title": paper.get("title"),
        "pdfPath": rel(project, pdf),
        "pdfSha256": paper.get("pdfSha256"),
        "pageCount": int(paper.get("pageCount") or page_count),
        "mainPageCount": page_count,
        "mainMarkerSubject":main_marker_subject,
        "formalClaimCount": len(claims),
        "derivedClaimCount": sum(claim["sourceCode"] != "DIRECT_SOURCE_VALUE" for claim in claims),
        "typedNullCount": len(nulls),
        "typedNulls": nulls,
        "recordContracts": contracts,
        "sections": sections,
        "contexts": contexts,
        "locators": locator_table,
        "claims": claims,
    }


def _partitions(values: list[Any], shard_count: int) -> list[list[Any]]:
    count = max(1, min(shard_count, len(values) or 1))
    base = len(values) // count
    result = []
    start = 0
    for index in range(count):
        size = base + (len(values) % count if index == count - 1 else 0)
        result.append(values[start:start + size])
        start += size
    return [group for group in result if group]


def _balanced_papers(papers: list[dict[str, Any]], shard_count: int) -> list[list[dict[str, Any]]]:
    count = max(1, min(shard_count, len(papers) or 1))
    order = {str(paper.get("runId")): index for index, paper in enumerate(papers)}
    weighted = sorted(
        ((len(stable_bytes(paper)) + 160 * int(paper.get("derivedClaimCount") or 0), paper) for paper in papers),
        key=lambda item: (-item[0], order[str(item[1].get("runId"))]),
    )
    groups: list[list[dict[str, Any]]] = [[] for _ in range(count)]
    loads = [0] * count
    for weight, paper in weighted:
        target = min(range(count), key=lambda index: (loads[index], index))
        groups[target].append(paper)
        loads[target] += weight
    for group in groups:
        group.sort(key=lambda paper: order[str(paper.get("runId"))])
    return [group for group in groups if group]


def _line(value:Any,limit:int=900)->str:
    text=" ".join(str(value or "").split()).replace("|","/")
    return text if len(text)<=limit else text[:limit-1]+"…"


def semantic_markdown(packet:dict[str,Any])->str:
    lines=[
        f"# {packet['packetId']}",
        f"reviewedHead: {packet['reviewedHead']}",
        f"subjectSha256: {packet['subjectSha256']}",
        f"reviewProtocolSha256: {packet['reviewProtocolSha256']}",
        f"wakgContractVersion: {packet['wakgContractVersion']}",
        "",
        "Return the five receipt values above exactly as written. A response copied from an earlier round is invalid.",
        "",
    ]
    for paper in packet.get("papers") or []:
        lines.extend([f"## PAPER {paper['runId']} / {_line(paper.get('title'),180)}",f"PDF {paper['pdfSha256']} / formal={paper['formalClaimCount']} / typed-null={paper['typedNullCount']}"])
        contracts={str(item.get("reviewRecordKey")):item for item in paper.get("recordContracts") or []}
        locators=paper.get("locators") or {};contexts={str(item.get("contextId")):item for item in paper.get("contexts") or []};sections={str(item.get("sectionId")):item for item in paper.get("sections") or []}
        claims_by_record:dict[str,list[dict[str,Any]]]={}
        for claim in paper.get("claims") or []:claims_by_record.setdefault(str(claim.get("recordKey")),[]).append(claim)
        derived_sources:dict[tuple[int,str],str]={}
        for claim in paper.get("claims") or []:
            if not claim.get("formula"):continue
            for locator_ref in claim.get("locatorRefs") or []:
                locator=locators.get(str(locator_ref)) or {};source_token=str(locator.get("sourceToken") or "").strip()
                if source_token:
                    key=(int(locator.get("page") or 0),source_token)
                    derived_sources.setdefault(key,f"D{len(derived_sources)+1}")
        if derived_sources:
            lines.append("DERIVED EVIDENCE (deduplicated, complete source text for formula review):")
            for (page,source_token),source_id in derived_sources.items():lines.append(f"{source_id} p{page}: {_line(source_token,1200)}")
        nulls_by_record:dict[str,list[dict[str,Any]]]={}
        for item in paper.get("typedNulls") or []:nulls_by_record.setdefault(str(item.get("recordKey")),[]).append(item)
        for record_key in sorted(set(claims_by_record)|set(nulls_by_record)|set(contracts)):
            contract=contracts.get(record_key,{})
            lines.append(f"### {contract.get('recordType','?')} {record_key} / canonical={contract.get('canonicalKey')} / matRefs={','.join(contract.get('matRefs') or []) or '-'}")
            used_contexts=[]
            for claim in claims_by_record.get(record_key,[]):
                for locator_ref in claim.get("locatorRefs") or []:
                    locator=locators.get(str(locator_ref)) or {};context_id=str(locator.get("contextId") or "")
                    if context_id and context_id not in used_contexts:used_contexts.append(context_id)
            used_sections=[]
            for context_id in used_contexts:
                section_id=str((contexts.get(context_id) or {}).get("sectionId") or "")
                if section_id and section_id not in used_sections:used_sections.append(section_id)
            for section_id in used_sections:
                section=sections[section_id];lines.append(f"HEADER p{section['page']}: {_line(section.get('text'))}")
            for context_id in used_contexts:
                context=contexts[context_id];lines.append(f"ROW p{context['page']}: {_line(context.get('text'),500)}")
            lines.append("CLAIMS: id / field / normalized value / original / source / formula / source binding / page:token-or-derived-source / fieldPath")
            for claim in claims_by_record.get(record_key,[]):
                locator_values=[];paths=[]
                for locator_ref in claim.get("locatorRefs") or []:
                    locator=locators.get(str(locator_ref)) or {}
                    source_token=str(locator.get("sourceToken") or "").strip();source_id=derived_sources.get((int(locator.get("page") or 0),source_token)) if claim.get("formula") else None
                    locator_values.append(source_id or f"p{locator.get('page')}:{_line(source_token,120)}")
                    if locator.get("fieldPath") not in (None,"") and str(locator.get("fieldPath")) not in paths:paths.append(str(locator.get("fieldPath")))
                lines.append(" / ".join([
                    str(claim.get("claimId")),_line(claim.get("fieldLabel"),120),
                    _line(f"{claim.get('value')} {claim.get('unit') or ''}",160),_line(f"{claim.get('originalValue')} {claim.get('originalUnit') or ''}",160),
                    str(claim.get("sourceCode")),_line(claim.get("formula") or "-",400),_line(claim.get("sourceBinding") or "-",300),";".join(locator_values),",".join(paths) or "-",
                ]))
            if nulls_by_record.get(record_key):
                lines.append("TYPED NULLS: field / state / reason")
                for item in nulls_by_record[record_key]:lines.append(f"NULL / {_line(item.get('fieldLabel'),120)} / {item.get('status')} / {_line(item.get('reason'),300)}")
            lines.append("")
    assigned=",".join(str(paper.get("runId")) for paper in packet.get("papers") or [])
    lines.append(f"Return packetId={packet['packetId']} and exactly these papers: {assigned}. Return one PASS/REJECT/ESCALATE result per paper; do not perform per-field model calls.\n")
    return "\n".join(lines)


def write_review_packets(
    project: Path,
    queue: dict[str, Any],
    request: dict[str, Any],
    out_dir: Path,
    *,
    shard_count: int = 3,
    proxy_budget: int = DEFAULT_PROXY_BUDGET,
    event_budget: int = DEFAULT_EVENT_BUDGET,
) -> dict[str, Any]:
    project = project.resolve()
    out_dir = project_path(project, out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    papers = [build_paper_packet(project, paper) for paper in queue.get("papers") or []]
    shards: list[dict[str, Any]] = []
    for index, members in enumerate(_balanced_papers(papers, shard_count), 1):
        packet = {
            "schemaVersion": 1,
            "kind": PACKET_KIND,
            "reviewedHead": request["reviewedHead"],
            "subjectSha256": request["subjectSha256"],
            "reviewProtocolVersion": REVIEW_PROTOCOL_VERSION,
            "reviewProtocolSha256": digest_value({"version": REVIEW_PROTOCOL_VERSION, "contract": WAKG_CONTRACT_VERSION, "sourceCodes": sorted(SOURCE_CODES)}),
            "wakgContractVersion": WAKG_CONTRACT_VERSION,
            "packetId": f"review-packet-{index:02d}",
            "policy": "review grouped source contexts; do not scan the full candidate or repository history",
            "papers": members,
        }
        packet_path = out_dir / f"packet-{index:02d}.json"
        atomic_json(packet_path, packet)
        semantic_path=out_dir/f"packet-{index:02d}-semantic.md";atomic_text(semantic_path,semantic_markdown(packet))
        shards.append({
            "packetId": packet["packetId"],
            "path": rel(project, packet_path),
            "sha256": digest_file(packet_path),
            "semanticPath":rel(project,semantic_path),
            "semanticSha256":digest_file(semantic_path),
            "semanticBytes":semantic_path.stat().st_size,
            "runIds": [paper["runId"] for paper in members],
            "paperCount": len(members),
            "formalClaimCount": sum(paper["formalClaimCount"] for paper in members),
            "derivedClaimCount": sum(paper["derivedClaimCount"] for paper in members),
            "typedNullCount": sum(paper["typedNullCount"] for paper in members),
        })
    candidate_path = project / "internal_assets" / "pre-human-agent-review" / "candidate-queue.json"
    plan = {
        "schemaVersion": 1,
        "kind": PLAN_KIND,
        "reviewedHead": request["reviewedHead"],
        "subjectSha256": request["subjectSha256"],
        "candidatePath": rel(project, candidate_path),
        "candidateSha256": digest_file(candidate_path) if candidate_path.is_file() else digest_value(queue),
        "paperCount": len(papers),
        "formalClaimCount": sum(paper["formalClaimCount"] for paper in papers),
        "derivedClaimCount": sum(paper["derivedClaimCount"] for paper in papers),
        "typedNullCount": sum(paper["typedNullCount"] for paper in papers),
        "reviewProtocolVersion": REVIEW_PROTOCOL_VERSION,
        "reviewProtocolSha256": digest_value({"version": REVIEW_PROTOCOL_VERSION, "contract": WAKG_CONTRACT_VERSION, "sourceCodes": sorted(SOURCE_CODES)}),
        "wakgContractVersion": WAKG_CONTRACT_VERSION,
        "shards": shards,
        "budget": {
            "metric": "uncached_input_tokens + output_tokens",
            "proxyTokensMaxExclusive": int(proxy_budget),
            "modelEventsMax": int(event_budget),
            "allowedRoles": ["ACCEPTANCE_REVIEW", "PRE_HUMAN_AGENT"],
            "onExceed": "BLOCKED_BUDGET",
        },
        "reviewProtocol": [
            "Run the model-free verifier once before semantic review.",
            "Read only the assigned compact packet; do not rediscover candidate files or scan repository history.",
            "Treat verifier PASS as exhaustive PDF bbox/token checking for direct claims.",
            "Review every grouped context for row/header semantics and every derived/composite formula.",
            "Review every typed null and MAT/MIX matRefs relationship; null must never be interpreted as zero.",
            "Return packetId and exactly the runIds assigned to that packet; extra, missing, duplicate, or cross-packet results are invalid.",
            "Return one consolidated response per packet; do not call a model per field.",
        ],
        "requiredResponseFields": ["packetId", "reviewedHead", "subjectSha256", "reviewProtocolSha256", "wakgContractVersion", "paperResults"],
        "requiredPaperResult": ["runId", "verdict", "formalNonNullCount", "verifiedLocatorCount", "defects"],
    }
    plan_path = out_dir / "plan.json"
    atomic_json(plan_path, plan)
    return plan


def seal_verified_plan(out_dir: Path, plan: dict[str, Any], proof: dict[str, Any]) -> dict[str, Any]:
    """Persist a cheap-to-check integrity seal for an exhaustive PDF proof."""
    seal = {
        "schemaVersion": 1,
        "kind": "WAKG_COMPACT_PREHUMAN_PROOF_SEAL",
        "planSha256": digest_value(plan),
        "proofSha256": digest_value(proof),
        "candidateSha256": plan.get("candidateSha256"),
        "reviewedHead": plan.get("reviewedHead"),
        "subjectSha256": plan.get("subjectSha256"),
        "reviewProtocolSha256": plan.get("reviewProtocolSha256"),
        "wakgContractVersion": plan.get("wakgContractVersion"),
    }
    atomic_json(out_dir / "proof-seal.json", seal)
    return seal


def load_cached_verified_plan(
    project: Path,
    request: dict[str, Any],
    out_dir: Path,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Reuse a proof only when all bound bytes, including PDFs, are unchanged."""
    project = project.resolve()
    out_dir = project_path(project, out_dir)
    try:
        plan = load_object(out_dir / "plan.json")
        proof = load_object(out_dir / "proof.json")
        seal = load_object(out_dir / "proof-seal.json")
        protocol_sha = digest_value({"version": REVIEW_PROTOCOL_VERSION, "contract": WAKG_CONTRACT_VERSION, "sourceCodes": sorted(SOURCE_CODES)})
        identity = {
            "reviewedHead": request.get("reviewedHead"),
            "subjectSha256": request.get("subjectSha256"),
            "reviewProtocolSha256": protocol_sha,
            "wakgContractVersion": WAKG_CONTRACT_VERSION,
        }
        if plan.get("kind") != PLAN_KIND or proof.get("kind") != PROOF_KIND or proof.get("verdict") != "PASS" or proof.get("failures"):
            return None
        if any(plan.get(key) != value or proof.get(key) != value or seal.get(key) != value for key, value in identity.items()):
            return None
        if seal.get("kind") != "WAKG_COMPACT_PREHUMAN_PROOF_SEAL" or seal.get("planSha256") != digest_value(plan) or seal.get("proofSha256") != digest_value(proof):
            return None
        candidate = project_path(project, str(plan.get("candidatePath") or ""))
        if not candidate.is_file() or digest_file(candidate) != plan.get("candidateSha256") or seal.get("candidateSha256") != plan.get("candidateSha256"):
            return None
        seen_runs: list[str] = []
        for shard in plan.get("shards") or []:
            packet_path = project_path(project, str(shard.get("path") or ""))
            semantic_path = project_path(project, str(shard.get("semanticPath") or ""))
            if not packet_path.is_file() or digest_file(packet_path) != shard.get("sha256"):
                return None
            packet = load_object(packet_path)
            if packet.get("packetId") != shard.get("packetId") or any(packet.get(key) != identity[key] for key in identity):
                return None
            if not semantic_path.is_file() or digest_file(semantic_path) != shard.get("semanticSha256") or semantic_path.read_text(encoding="utf-8") != semantic_markdown(packet):
                return None
            packet_runs = [str(paper.get("runId") or "") for paper in packet.get("papers") or []]
            if packet_runs != [str(value) for value in shard.get("runIds") or []]:
                return None
            seen_runs.extend(packet_runs)
            for paper in packet.get("papers") or []:
                pdf = project_path(project, str(paper.get("pdfPath") or ""))
                if not pdf.is_file() or digest_file(pdf) != paper.get("pdfSha256"):
                    return None
                curve_subjects={digest_value(item["curveSubject"]):item["curveSubject"] for item in (paper.get("locators") or {}).values() if item.get("curveSubject")}
                sparse_subjects={digest_value(item['sparseSubject']):item['sparseSubject'] for item in (paper.get('locators') or {}).values() if item.get('sparseSubject')}
                psd_subjects={digest_value(item['supplementPsdSubject']):item['supplementPsdSubject'] for item in (paper.get('locators') or {}).values() if item.get('supplementPsdSubject')}
                marker_subjects={digest_value(item['supplementMarkerSubject']):item['supplementMarkerSubject'] for item in (paper.get('locators') or {}).values() if item.get('supplementMarkerSubject')}
                main_marker_subjects={digest_value(item['mainMarkerSubject']):item['mainMarkerSubject'] for item in (paper.get('locators') or {}).values() if item.get('mainMarkerSubject')}
                if paper.get('mainMarkerSubject'):main_marker_subjects[digest_value(paper['mainMarkerSubject'])]=paper['mainMarkerSubject']
                if main_marker_subjects:
                    import sys
                    if str(project/'scripts') not in sys.path:sys.path.insert(0,str(project/'scripts'))
                    import main_marker_review_projection as main_review
                    for subject in main_marker_subjects.values():main_review.check_subject_inputs(project,subject)
                if marker_subjects:
                    import sys
                    if str(project/'scripts') not in sys.path:sys.path.insert(0,str(project/'scripts'))
                    import supplement_marker_review_projection as marker_review
                    for subject in marker_subjects.values():marker_review.verified_index(project,subject)
                if psd_subjects:
                    import sys
                    if str(project/'scripts') not in sys.path:sys.path.insert(0,str(project/'scripts'))
                    import supplement_psd_review_projection as psd_review
                    for subject in psd_subjects.values():psd_review.verified_index(project,subject)
                if sparse_subjects:
                    import sys
                    if str(project/'scripts') not in sys.path:sys.path.insert(0,str(project/'scripts'))
                    import sparse_review_projection as sparse_review
                    for subject in sparse_subjects.values():sparse_review.check_subject_inputs(project,subject)
                if curve_subjects:
                    import sys
                    if str(project/"scripts") not in sys.path:sys.path.insert(0,str(project/"scripts"))
                    import review_curve_projection as curve_review
                    for subject in curve_subjects.values():curve_review.check_subject_inputs(project,subject)
        expected_runs = [str(item.get("runId") or "") for item in request.get("papers") or []]
        if len(seen_runs) != len(set(seen_runs)) or set(seen_runs) != set(expected_runs):
            return None
        proof_results = proof.get("paperResults") or []
        if {str(item.get("runId") or "") for item in proof_results} != set(expected_runs):
            return None
        if any(item.get("verdict") != "PASS" or int(item.get("verifiedLocatorCount") or -1) != int(item.get("formalClaimCount") or -2) for item in proof_results):
            return None
        if (int(proof.get("paperCount") or -1), int(proof.get("formalClaimCount") or -1), int(proof.get("typedNullCount") or -1)) != (int(plan.get("paperCount") or -2), int(plan.get("formalClaimCount") or -2), int(plan.get("typedNullCount") or -2)):
            return None
        return plan, proof
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def _same_text(actual: str, expected: str) -> bool:
    compact = lambda value: " ".join(value.replace("\u00ad", "").split())
    actual_value,expected_value=compact(actual),compact(expected)
    if actual_value==expected_value:return True
    start=0
    while True:
        index=actual_value.find(expected_value,start)
        if index<0:return False
        end=index+len(expected_value)
        left_ok=index==0 or not actual_value[index-1].isalnum()
        right_ok=end==len(actual_value) or not actual_value[end].isalnum()
        if left_ok and right_ok:return True
        start=index+1


def _locator_matches(page: fitz.Page, bbox: list[float], token: str, words: list[tuple[Any, ...]] | None = None) -> bool:
    rect = fitz.Rect(bbox)
    if rect.is_empty or rect.x0 < 0 or rect.y0 < 0 or rect.x1 > page.rect.width or rect.y1 > page.rect.height:
        return False
    # Almost every formal locator is a single PDF word.  Checking the cached
    # page word list first avoids rebuilding a MuPDF text page for each of
    # thousands of claims.  The textbox fallback remains for genuine phrases.
    for word in words if words is not None else page.get_text("words"):
        center = fitz.Point((float(word[0]) + float(word[2])) / 2, (float(word[1]) + float(word[3])) / 2)
        if rect.contains(center) and "".join(str(word[4]).split()).casefold() == "".join(token.split()).casefold():
            return True
    return _same_text(page.get_textbox(rect), token)


def verify_plan(project: Path, plan: dict[str, Any]) -> dict[str, Any]:
    project = project.resolve()
    failures: list[dict[str, Any]] = []
    paper_results: list[dict[str, Any]] = []
    if plan.get("kind") != PLAN_KIND:
        raise ValueError("invalid compact review plan")
    candidate = project_path(project, str(plan.get("candidatePath") or ""))
    if not candidate.is_file() or digest_file(candidate) != plan.get("candidateSha256"):
        failures.append({"scope": "plan", "reason": "candidate_hash_mismatch"})
    seen_runs: set[str] = set()
    total_claims = 0
    total_nulls = 0
    all_mat_keys: set[str] = set()
    all_mat_refs: list[tuple[str, str]] = []
    for shard in plan.get("shards") or []:
        packet_path = project_path(project, str(shard.get("path") or ""))
        if not packet_path.is_file() or digest_file(packet_path) != shard.get("sha256"):
            failures.append({"scope": shard.get("packetId"), "reason": "packet_hash_mismatch"})
            continue
        packet = load_object(packet_path)
        semantic_path=project_path(project,str(shard.get("semanticPath") or ""))
        if not semantic_path.is_file() or digest_file(semantic_path)!=shard.get("semanticSha256") or semantic_path.read_text(encoding="utf-8")!=semantic_markdown(packet):
            failures.append({"scope":shard.get("packetId"),"reason":"semantic_packet_mismatch"})
            continue
        if packet.get("kind") != PACKET_KIND or packet.get("subjectSha256") != plan.get("subjectSha256") or packet.get("reviewedHead") != plan.get("reviewedHead"):
            failures.append({"scope": shard.get("packetId"), "reason": "packet_identity_mismatch"})
            continue
        for paper in packet.get("papers") or []:
            run_id = str(paper.get("runId") or "")
            paper_failures: list[str] = []
            if not run_id or run_id in seen_runs:
                paper_failures.append("duplicate_or_missing_run_id")
            seen_runs.add(run_id)
            pdf = project_path(project, str(paper.get("pdfPath") or ""))
            if not pdf.is_file() or digest_file(pdf) != paper.get("pdfSha256"):
                paper_failures.append("pdf_hash_mismatch")
                paper_results.append({"runId": run_id, "verdict": "FAIL", "formalClaimCount": 0, "failures": paper_failures})
                continue
            claims = paper.get("claims") or []
            contexts = {str(item.get("contextId")): item for item in paper.get("contexts") or []}
            sections = {str(item.get("sectionId")): item for item in paper.get("sections") or []}
            locators = paper.get("locators") or {}
            typed_nulls = paper.get("typedNulls") or []
            contracts = paper.get("recordContracts") or []
            for contract in contracts:
                if contract.get("contractVersion") != WAKG_CONTRACT_VERSION or contract.get("recordType") not in {"MAT", "MIX"}:
                    paper_failures.append(f"invalid_record_contract:{contract.get('canonicalKey')}")
                if contract.get("recordType") == "MAT":
                    all_mat_keys.add(str(contract.get("canonicalKey") or ""))
                else:
                    all_mat_refs.extend((run_id, str(value)) for value in contract.get("matRefs") or [])
            for item in typed_nulls:
                if item.get("valueType") != "null" or item.get("status") not in {"not_reported", "not_applicable"} or not str(item.get("reason") or "").strip():
                    paper_failures.append(f"invalid_typed_null:{item.get('recordKey')}:{item.get('fieldLabel')}")
            with fitz.open(pdf) as doc:
                if len(doc) != int(paper.get("mainPageCount") or paper.get("pageCount") or -1):
                    paper_failures.append("page_count_mismatch")
                artifact_docs:dict[str,fitz.Document]={}
                artifact_hashes:dict[str,str]={}
                page_words:dict[tuple[Any,int],list[tuple[Any,...]]]={}
                def source_page(item:dict[str,Any])->fitz.Page|None:
                    kind=str(item.get("sourceKind") or "main")
                    if kind=="main":
                        source_document=doc
                    else:
                        artifact_value=str(item.get("sourceArtifactPath") or "")
                        if not artifact_value:return None
                        artifact=project_path(project,artifact_value)
                        expected_hash=str(item.get("sourceArtifactSha256") or "")
                        key=str(artifact)
                        if not artifact.is_file() or not expected_hash:return None
                        if key not in artifact_hashes:artifact_hashes[key]=digest_file(artifact)
                        if artifact_hashes[key]!=expected_hash:return None
                        if key not in artifact_docs:artifact_docs[key]=fitz.open(artifact)
                        source_document=artifact_docs[key]
                    number=int(item.get("sourcePage") or item.get("page") or 0)
                    return source_document[number-1] if 1<=number<=len(source_document) else None
                for context_id, context in contexts.items():
                    page=source_page(context)
                    if page is None:
                        paper_failures.append(f"invalid_context_page:{context_id}")
                        continue
                    text = str(context.get("text") or "") if context.get("syntheticContext") else page.get_textbox(fitz.Rect(context["bbox"])).strip()
                    if text != context.get("text") or hashlib.sha256(text.encode("utf-8")).hexdigest() != context.get("textSha256"):
                        paper_failures.append(f"context_mismatch:{context_id}")
                    if str(context.get("sectionId") or "") not in sections:
                        paper_failures.append(f"missing_section:{context_id}")
                for section_id, section in sections.items():
                    page=source_page(section)
                    if page is None:
                        paper_failures.append(f"invalid_section_page:{section_id}")
                        continue
                    text = str(section.get("text") or "") if section.get("syntheticContext") else page.get_textbox(fitz.Rect(section["bbox"])).strip()
                    if text != section.get("text") or hashlib.sha256(text.encode("utf-8")).hexdigest() != section.get("textSha256"):
                        paper_failures.append(f"section_mismatch:{section_id}")
                valid_locators: set[str] = set()
                curve_indexes = {}
                sparse_indexes = {}
                psd_indexes = {}
                marker_indexes = {}
                main_marker_indexes = {}
                if paper.get('mainMarkerSubject'):
                    try:
                        import sys
                        if str(project/'scripts') not in sys.path:sys.path.insert(0,str(project/'scripts'))
                        import main_marker_review_projection as main_review
                        subject=paper['mainMarkerSubject']
                        main_marker_indexes[digest_value(subject)]=main_review.verified_index(project,subject)
                    except (ValueError,KeyError,TypeError,OSError,IndexError,StopIteration) as exc:
                        paper_failures.append('main_marker_paper_proof_failed:'+str(exc))
                for locator_id, locator in locators.items():
                    context_id = str(locator.get("contextId") or "")
                    if context_id not in contexts:
                        paper_failures.append(f"missing_context:{locator_id}")
                        continue
                    page=source_page(locator);bbox=locator.get("bbox") or []
                    region_only=str(locator.get("mode") or "") in {"supplement-native-chart","supplement-figure-region"}
                    if str(locator.get("sourceKind") or "")=="supplement-figure":
                        boundary=fitz.Rect(0,0,float(locator.get("sourceWidth") or 0),float(locator.get("sourceHeight") or 0))
                    else:
                        boundary=page.rect if page is not None else fitz.Rect()
                    bounds_ok=page is not None and isinstance(bbox,list) and len(bbox)==4 and not fitz.Rect(bbox).is_empty and boundary.contains(fitz.Rect(bbox))
                    cache_key=(page.parent,int(page.number)) if page is not None else (None,-1)
                    if page is not None and cache_key not in page_words:page_words[cache_key]=page.get_text("words")
                    if locator.get('mode')=='reviewed-main-figure-markers':
                        try:
                            import sys
                            if str(project/'scripts') not in sys.path:sys.path.insert(0,str(project/'scripts'))
                            import main_marker_review_projection as main_review
                            subject=locator.get('mainMarkerSubject') or {};subject_key=digest_value(subject)
                            if subject_key not in main_marker_indexes:main_marker_indexes[subject_key]=main_review.verified_index(project,subject)
                            matching=[claim for claim in claims if locator_id in (claim.get('locatorRefs') or [])]
                            if not matching or not bounds_ok:raise ValueError('unowned or out-of-bounds main marker locator')
                            projected={**locator,'evidenceMode':locator['mode'],'snippet':locator['sourceToken']}
                            for claim in matching:
                                field={**claim,'sourceExplanationCode':claim.get('sourceCode'),'sourceFormula':claim.get('formula')}
                                main_review.verify_locator(main_marker_indexes[subject_key],projected,field,claim.get('recordKey'),paper.get('pdfSha256'))
                            valid_locators.add(str(locator_id))
                        except (ValueError,KeyError,TypeError,OSError,IndexError,StopIteration) as exc:
                            paper_failures.append(f'main_marker_proof_failed:{locator_id}:{exc}')
                        continue
                    if locator.get('mode')=='reviewed-supplement-markers':
                        try:
                            import sys
                            if str(project/'scripts') not in sys.path:sys.path.insert(0,str(project/'scripts'))
                            import supplement_marker_review_projection as marker_review
                            subject=locator.get('supplementMarkerSubject') or {};subject_key=digest_value(subject)
                            if subject_key not in marker_indexes:marker_indexes[subject_key]=marker_review.verified_index(project,subject)
                            matching=[claim for claim in claims if locator_id in (claim.get('locatorRefs') or [])]
                            if not matching or not bounds_ok:raise ValueError('unowned or out-of-bounds marker locator')
                            projected={**locator,'evidenceMode':locator['mode'],'snippet':locator['sourceToken']}
                            page_meta={'number':locator['page'],'sourceKind':locator['sourceKind'],
                                'sourceArtifactSha256':locator['sourceArtifactSha256'],'sourcePage':locator['sourcePage'],
                                'width':locator['sourceWidth'],'height':locator['sourceHeight']}
                            for claim in matching:
                                field={**claim,'sourceExplanationCode':claim.get('sourceCode'),'sourceFormula':claim.get('formula')}
                                marker_review.verify_locator(marker_indexes[subject_key],projected,field,claim.get('recordKey'),paper.get('pdfSha256'),page_meta)
                            valid_locators.add(str(locator_id))
                        except (ValueError,KeyError,TypeError,OSError,IndexError,StopIteration) as exc:
                            paper_failures.append(f'supplement_marker_proof_failed:{locator_id}:{exc}')
                        continue
                    if locator.get('mode')=='reviewed-supplement-psd':
                        try:
                            import sys
                            if str(project/'scripts') not in sys.path:sys.path.insert(0,str(project/'scripts'))
                            import supplement_psd_review_projection as psd_review
                            subject=locator.get('supplementPsdSubject') or {};subject_key=digest_value(subject)
                            if subject_key not in psd_indexes:psd_indexes[subject_key]=psd_review.verified_index(project,subject)
                            matching=[claim for claim in claims if locator_id in (claim.get('locatorRefs') or [])]
                            if not matching or not bounds_ok:raise ValueError('unowned or out-of-bounds PSD locator')
                            projected={**locator,'evidenceMode':locator['mode'],'snippet':locator['sourceToken']}
                            page_meta={'number':locator['page'],'sourceKind':locator['sourceKind'],
                                'sourceArtifactSha256':locator['sourceArtifactSha256'],'sourcePage':locator['sourcePage'],
                                'width':locator['sourceWidth'],'height':locator['sourceHeight']}
                            for claim in matching:
                                field={**claim,'sourceExplanationCode':claim.get('sourceCode'),'sourceFormula':claim.get('formula')}
                                psd_review.verify_locator(psd_indexes[subject_key],projected,field,claim.get('recordKey'),paper.get('pdfSha256'),page_meta)
                            valid_locators.add(str(locator_id))
                        except (ValueError,KeyError,TypeError,OSError,IndexError,StopIteration) as exc:
                            paper_failures.append(f'supplement_psd_proof_failed:{locator_id}:{exc}')
                        continue
                    if locator.get('mode')=='supplement-figure-region':
                        matching=[claim for claim in claims if locator_id in (claim.get('locatorRefs') or [])]
                        numeric=False
                        for claim in matching:
                            try:
                                float(claim.get('value'));numeric=True
                            except (TypeError,ValueError):pass
                        if numeric:
                            paper_failures.append(f'image_numeric_source_proof_required:{locator_id}')
                            continue
                    if locator.get('mode')=='reviewed-sparse-geometry':
                        try:
                            import sys
                            if str(project/'scripts') not in sys.path:sys.path.insert(0,str(project/'scripts'))
                            import sparse_review_projection as sparse_review
                            subject=locator.get('sparseSubject') or {};subject_key=digest_value(subject)
                            if subject_key not in sparse_indexes:sparse_indexes[subject_key]=sparse_review.verified_index(project,subject)
                            matching=[claim for claim in claims if locator_id in (claim.get('locatorRefs') or [])]
                            if not matching or not bounds_ok:raise ValueError('unowned or out-of-bounds sparse locator')
                            projected={**locator,'evidenceMode':locator['mode'],'snippet':locator['sourceToken']}
                            for claim in matching:
                                field={**claim,'sourceExplanationCode':claim.get('sourceCode'),'sourceFormula':claim.get('formula')}
                                sparse_review.verify_locator(sparse_indexes[subject_key],projected,field,claim.get('recordKey'),paper.get('pdfSha256'))
                            valid_locators.add(str(locator_id))
                        except (ValueError,KeyError,TypeError,OSError,IndexError,StopIteration) as exc:
                            paper_failures.append(f'sparse_proof_failed:{locator_id}:{exc}')
                        continue
                    if locator.get("mode")=="reviewed-curve-geometry":
                        try:
                            import sys
                            if str(project/"scripts") not in sys.path:sys.path.insert(0,str(project/"scripts"))
                            import review_curve_projection as curve_review
                            subject=locator.get("curveSubject") or {};subject_key=digest_value(subject)
                            if subject_key not in curve_indexes:curve_indexes[subject_key]=curve_review.verified_index(project,subject)
                            matching=[claim for claim in claims if locator_id in (claim.get("locatorRefs") or [])]
                            if not matching or not bounds_ok:raise ValueError("unowned or out-of-bounds curve locator")
                            for claim in matching:curve_review.verify_locator(curve_indexes[subject_key],locator,claim,claim.get("recordKey"),paper.get("pdfSha256"))
                            valid_locators.add(str(locator_id))
                        except (ValueError,KeyError,TypeError,OSError) as exc:
                            paper_failures.append(f"curve_proof_failed:{locator_id}:{exc}")
                        continue
                    if not bounds_ok or (not region_only and not _locator_matches(page,bbox,str(locator.get("sourceToken") or ""),page_words.get(cache_key))):
                        paper_failures.append(f"locator_mismatch:{locator_id}")
                        continue
                    valid_locators.add(str(locator_id))
                for index in main_marker_indexes.values():
                    shown=[locators[ref].get('sourceEvidenceKey') for claim in claims for ref in claim.get('locatorRefs',[]) if ref in locators and locators[ref].get('sourceEvidenceKey') in index]
                    if len(shown)!=len(index) or set(shown)!=set(index):paper_failures.append('main_marker_claim_coverage_mismatch')
                for index in curve_indexes.values():
                    shown=[locators[ref].get("canonicalEvidenceKey") for claim in claims for ref in claim.get("locatorRefs",[]) if ref in locators and locators[ref].get("canonicalEvidenceKey") in index]
                    if len(shown)!=len(index) or set(shown)!=set(index):paper_failures.append("curve_claim_coverage_mismatch")
                for index in sparse_indexes.values():
                    shown=[locators[ref].get('evidenceKey') for claim in claims for ref in claim.get('locatorRefs',[]) if ref in locators and locators[ref].get('evidenceKey') in index]
                    if len(shown)!=len(index) or set(shown)!=set(index):paper_failures.append('sparse_claim_coverage_mismatch')
                for index in psd_indexes.values():
                    shown=[locators[ref].get('sourceEvidenceKey') for claim in claims for ref in claim.get('locatorRefs',[]) if ref in locators and locators[ref].get('sourceEvidenceKey') in index]
                    if len(shown)!=len(index) or set(shown)!=set(index):paper_failures.append('supplement_psd_claim_coverage_mismatch')
                for index in marker_indexes.values():
                    shown=[locators[ref].get('sourceEvidenceKey') for claim in claims for ref in claim.get('locatorRefs',[]) if ref in locators and locators[ref].get('sourceEvidenceKey') in index]
                    if len(shown)!=len(index) or set(shown)!=set(index):paper_failures.append('supplement_marker_claim_coverage_mismatch')
                for claim in claims:
                    source_code = str(claim.get("sourceCode") or "")
                    if source_code not in SOURCE_CODES:
                        paper_failures.append(f"invalid_source_code:{claim.get('claimId')}")
                    if source_code != "DIRECT_SOURCE_VALUE" and not claim.get("formula"):
                        paper_failures.append(f"missing_formula:{claim.get('claimId')}")
                    locator_refs = claim.get("locatorRefs") or []
                    if not locator_refs:
                        paper_failures.append(f"missing_locator:{claim.get('claimId')}")
                    for locator_ref in locator_refs:
                        locator = locators.get(str(locator_ref))
                        if not isinstance(locator, dict):
                            paper_failures.append(f"missing_locator_ref:{claim.get('claimId')}:{locator_ref}")
                            continue
                        if str(locator_ref) not in valid_locators:
                            paper_failures.append(f"unverified_locator_ref:{claim.get('claimId')}:{locator_ref}")
                performance_paths: dict[tuple[str, str], set[int]] = {}
                for claim in claims:
                    label = str(claim.get("fieldLabel") or "")
                    role = str(claim.get("semanticRole") or "")
                    base_label = label.removeprefix("龄期 · ") if role == "performance_age" else label
                    for locator_ref in claim.get("locatorRefs") or []:
                        path = str((locators.get(str(locator_ref)) or {}).get("fieldPath") or "")
                        matched = re.fullmatch(r"/modules/performance/(\d+)/(value|age_seconds)", path)
                        if matched:
                            performance_paths.setdefault((str(claim.get("recordKey") or ""), base_label), set()).add(int(matched.group(1)))
                for (record_key, label), indices in performance_paths.items():
                    if len(indices) > 1:
                        paper_failures.append(f"performance_value_age_index_mismatch:{record_key}:{label}")
                for source_document in artifact_docs.values():source_document.close()
            expected = int(paper.get("formalClaimCount") or -1)
            if expected != len(claims):
                paper_failures.append("formal_claim_count_mismatch")
            total_claims += len(claims)
            if (int(paper["typedNullCount"]) if "typedNullCount" in paper else -1) != len(typed_nulls):
                paper_failures.append("typed_null_count_mismatch")
            total_nulls += len(typed_nulls)
            paper_results.append({
                "runId": run_id,
                "verdict": "PASS" if not paper_failures else "FAIL",
                "formalClaimCount": len(claims),
                "verifiedLocatorCount": len(claims) if not paper_failures else 0,
                "failures": paper_failures,
            })
            failures.extend({"scope": run_id, "reason": reason} for reason in paper_failures)
    if len(seen_runs) != int(plan.get("paperCount") or -1):
        failures.append({"scope": "plan", "reason": "paper_count_mismatch"})
    if total_claims != int(plan.get("formalClaimCount") or -1):
        failures.append({"scope": "plan", "reason": "claim_count_mismatch"})
    if total_nulls != (int(plan["typedNullCount"]) if "typedNullCount" in plan else -1):
        failures.append({"scope": "plan", "reason": "typed_null_count_mismatch"})
    for run_id, mat_ref in all_mat_refs:
        if mat_ref not in all_mat_keys:
            failures.append({"scope": run_id, "reason": f"unresolved_mat_ref:{mat_ref}"})
    return {
        "schemaVersion": 1,
        "kind": PROOF_KIND,
        "reviewedHead": plan.get("reviewedHead"),
        "subjectSha256": plan.get("subjectSha256"),
        "reviewProtocolSha256": plan.get("reviewProtocolSha256"),
        "wakgContractVersion": plan.get("wakgContractVersion"),
        "verdict": "PASS" if not failures else "FAIL",
        "paperCount": len(seen_runs),
        "formalClaimCount": total_claims,
        "typedNullCount": total_nulls,
        "matReferenceCount": len(all_mat_refs),
        "paperResults": paper_results,
        "failures": failures,
    }


def budget_verdict(plan: dict[str, Any], usage: dict[str, Any], acceptance: dict[str, Any], proof: dict[str, Any]) -> dict[str, Any]:
    failures: list[str] = []
    identity = (plan.get("reviewedHead"), plan.get("subjectSha256"))
    for name, value in (("acceptance", acceptance), ("proof", proof)):
        if (value.get("reviewedHead"), value.get("subjectSha256")) != identity:
            failures.append(f"{name}_identity_mismatch")
        if value.get("reviewProtocolSha256") != plan.get("reviewProtocolSha256") or value.get("wakgContractVersion") != plan.get("wakgContractVersion"):
            failures.append(f"{name}_protocol_mismatch")
    if proof.get("kind") != PROOF_KIND or proof.get("verdict") != "PASS":
        failures.append("proof_not_pass")
    if acceptance.get("overallVerdict") != "PASS":
        failures.append("acceptance_not_pass")
    results = acceptance.get("paperResults") or []
    formal = sum(int(item.get("formalNonNullCount") or 0) for item in results)
    verified = sum(int(item.get("verifiedLocatorCount") or 0) for item in results)
    if len(results) != int(plan.get("paperCount") or -1) or formal != int(plan.get("formalClaimCount") or -1) or verified != formal:
        failures.append("acceptance_coverage_mismatch")
    if usage.get("label") != USAGE_LABEL:
        failures.append("usage_scope_not_local_proxy")
    role_rows = usage.get("per_task_role")
    allowed_roles = set((plan.get("budget") or {}).get("allowedRoles") or [])
    deterministic_rebind = acceptance.get("bindingMode") == "deterministic-subject-rebind" and int(usage.get("event_count") or 0) == 0
    if not isinstance(role_rows, list) or (not role_rows and not deterministic_rebind) or any(not isinstance(row, dict) or str(row.get("role") or "") not in allowed_roles for row in role_rows):
        failures.append("usage_contains_nonreviewer_role")
    elif len({str(row.get("task_id") or "") for row in role_rows}) > len(plan.get("shards") or []):
        failures.append("usage_reviewer_task_count_exceeded")
    total_usage=usage.get("total") or {}
    proxy = int(total_usage["proxy"]) if "proxy" in total_usage else -1
    events = int(usage["event_count"]) if "event_count" in usage else -1
    budget = plan.get("budget") or {}
    if proxy < 0 or proxy >= int(budget.get("proxyTokensMaxExclusive") or 0):
        failures.append("proxy_token_budget_exceeded")
    if events < 0 or events > int(budget.get("modelEventsMax") or 0):
        failures.append("model_event_budget_exceeded")
    return {
        "schemaVersion": 1,
        "kind": BUDGET_KIND,
        "reviewedHead": plan.get("reviewedHead"),
        "subjectSha256": plan.get("subjectSha256"),
        "reviewProtocolSha256": plan.get("reviewProtocolSha256"),
        "wakgContractVersion": plan.get("wakgContractVersion"),
        "planSha256": digest_value(plan),
        "usageSha256": digest_value(usage),
        "acceptanceSha256": digest_value(acceptance),
        "proofSha256": digest_value(proof),
        "verdict": "PASS" if not failures else "BLOCKED_BUDGET",
        "proxyTokens": proxy,
        "proxyTokensMaxExclusive": budget.get("proxyTokensMaxExclusive"),
        "modelEvents": events,
        "modelEventsMax": budget.get("modelEventsMax"),
        "failures": failures,
    }


def rebind_acceptance(plan: dict[str, Any], proof: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any] | None:
    if proof.get("verdict") != "PASS":
        return None
    keys = ("subjectSha256", "reviewProtocolSha256", "wakgContractVersion")
    if any(previous.get(key) != plan.get(key) or proof.get(key) != plan.get(key) for key in keys):
        return None
    if previous.get("overallVerdict") != "PASS" or len(previous.get("paperResults") or []) != int(plan.get("paperCount") or -1):
        return None
    formal = sum(int(item.get("formalNonNullCount") or 0) for item in previous.get("paperResults") or [])
    verified = sum(int(item.get("verifiedLocatorCount") or 0) for item in previous.get("paperResults") or [])
    if formal != int(plan.get("formalClaimCount") or -1) or verified != formal:
        return None
    rebound = copy.deepcopy(previous)
    rebound.update({
        "reviewedHead": plan["reviewedHead"],
        "bindingMode": "deterministic-subject-rebind",
        "semanticReviewedHead": previous.get("semanticReviewedHead") or previous.get("reviewedHead"),
        "reboundFromAcceptanceSha256": digest_value(previous),
        "boundAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    })
    return rebound


def zero_usage_report() -> dict[str, Any]:
    return {
        "schema_version": 2,
        "label": USAGE_LABEL,
        "event_count": 0,
        "source_file_count": 0,
        "total": {"events": 0, "raw": 0, "uncached_input": 0, "output": 0, "proxy": 0, "cache_hit": 0.0, "reasoning_output_reported": 0, "cumulative_total_fields_present": 0},
        "per_task_role": [],
        "cacheDisposition": "semantic_acceptance_reused_by_subject_protocol_contract",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("--project", type=Path, required=True)
    build.add_argument("--candidate", type=Path, required=True)
    build.add_argument("--request", type=Path, required=True)
    build.add_argument("--out-dir", type=Path, required=True)
    build.add_argument("--shards", type=int, default=3)
    verify = commands.add_parser("verify")
    verify.add_argument("--project", type=Path, required=True)
    verify.add_argument("--plan", type=Path, required=True)
    verify.add_argument("--write", type=Path)
    budget = commands.add_parser("budget")
    budget.add_argument("--plan", type=Path, required=True)
    budget.add_argument("--usage", type=Path, required=True)
    budget.add_argument("--acceptance", type=Path, required=True)
    budget.add_argument("--proof", type=Path, required=True)
    budget.add_argument("--write", type=Path)
    args = parser.parse_args()
    if args.command == "build":
        project = args.project.resolve()
        request=load_object(args.request)
        plan = write_review_packets(project, load_object(args.candidate), request, args.out_dir, shard_count=args.shards)
        out_dir=project_path(project,args.out_dir)
        request.update({
            "reviewPacketPlan":rel(project,out_dir/"plan.json"),
            "reviewPacketCount":len(plan["shards"]),
            "formalClaimCount":plan["formalClaimCount"],
            "typedNullCount":plan["typedNullCount"],
            "reviewBudget":plan["budget"],
            "reviewProtocolVersion":plan["reviewProtocolVersion"],
            "reviewProtocolSha256":plan["reviewProtocolSha256"],
            "wakgContractVersion":plan["wakgContractVersion"],
            "reviewMethod":"model-free exhaustive locator verification, then one semantic pass per compact shard",
            "proofPath":rel(project,out_dir/"proof.json"),
            "semanticReviewNeeded":True,
        })
        atomic_json(project_path(project,args.request),request)
        print(json.dumps({"status": "PASS", "papers": plan["paperCount"], "claims": plan["formalClaimCount"], "shards": len(plan["shards"])}, sort_keys=True))
        return 0
    if args.command == "verify":
        result = verify_plan(args.project.resolve(), load_object(args.plan))
    else:
        result = budget_verdict(load_object(args.plan), load_object(args.usage), load_object(args.acceptance), load_object(args.proof))
    if args.write:
        atomic_json(args.write, result)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["verdict"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
