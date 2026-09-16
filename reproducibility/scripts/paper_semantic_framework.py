#!/usr/bin/env python3
"""Build and validate the pre-extraction paper semantic control plane.

The framework contains identities, relationships, routes, and expected source
objects.  It deliberately contains no accepted scientific measurements.
Routing packets remain geometry/token evidence inputs, but their complete set
must be declared by the framework before record extraction starts.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any


FRAMEWORK_VERSION = "paper-semantic-framework-v2"
TERMINAL_STATUSES = {
    "EXTRACTED", "TYPED_ABSENT", "NOT_APPLICABLE", "RESOLVED_REFERENCE", "ASSET_ONLY",
    "QUARANTINED", "ACCESS_FAILED",
}
NUMERIC_FIGURE_MODALITIES = {
    "PSD", "XRD_QXRD", "FTIR", "NMR", "PERFORMANCE",
    "PHYSICAL_PROPERTY", "THERMAL",
}
NUMERIC_COMPONENT_TYPES = {
    "PSD", "XRD_QXRD", "FTIR", "NMR", "PERFORMANCE_CURVE", "BAR_CHART",
    "PHYSICAL_PROPERTY", "THERMAL",
}
MODALITY_COMPONENT_FALLBACK = {
    "PSD": "PSD", "XRD_QXRD": "XRD_QXRD", "FTIR": "FTIR", "NMR": "NMR",
    "PERFORMANCE": "PERFORMANCE_CURVE", "PHYSICAL_PROPERTY": "PHYSICAL_PROPERTY",
    "THERMAL": "THERMAL",
}
REQUIRED_COMPONENT_DISPOSITIONS = {"REQUIRED_UNIQUE", "AMBIGUOUS_REQUIRES_VISUAL"}


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    raw = value if isinstance(value, bytes) else canonical(value).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


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


def table_key(value: Any) -> str:
    match = re.search(r"(?:table|tab\.?)\s*[-_ ]*([0-9]+[a-z]?)", str(value or ""), re.I)
    return f"table-{match.group(1).casefold()}" if match else re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def packet_key(packet: dict[str, Any]) -> tuple[str, int]:
    return str(packet["table_id"]), int(packet["source_row_index"])


def stable_id(prefix: str, value: Any) -> str:
    return f"{prefix}-" + digest(value)[:24]


def figure_requires_numeric_series(item: dict[str, Any]) -> bool:
    modalities = item.get("modality") or []
    if isinstance(modalities, str):
        modalities = [modalities]
    return bool(set(modalities) & NUMERIC_FIGURE_MODALITIES)


def fallback_figure_components(obj: dict[str, Any], schema: dict[str, Any]) -> list[dict[str, Any]]:
    """Preserve explicit caption panels; unknown content needs semantic review.

    This creates extraction tasks only, never calibrated panel geometry or a
    scientific acceptance. Agent-reviewed component decisions still override it.
    """
    from multimodal_extraction_plan import _modality_for_context, OUT_OF_SCOPE_RE
    caption=str(obj.get('caption') or '')
    markers=list(re.finditer(r'\(([a-z])(?:\s*[-–]\s*([a-z]))?\)\s+',caption))
    panels=[]
    for i,marker in enumerate(markers):
        end=marker[2] or marker[1]
        labels=[chr(code) for code in range(ord(marker[1]),ord(end)+1)]
        text=caption[marker.end():markers[i+1].start() if i+1<len(markers) else len(caption)].strip()
        panels.extend((label,text) for label in labels)
    labels=[label for label,_ in panels]
    split=(len(markers)>1 and all(not m[2] or m[2]>=m[1] for m in markers)
           and labels==[chr(97+i) for i in range(len(labels))])
    if not split:
        # Some captions use a range as a group heading, then repeat its labels
        # individually. Preserve the established explicit-single-panel route;
        # do not duplicate observations or invent range geometry.
        singles=list(re.finditer(r'\(([a-z])\)\s+',caption))
        split=len(singles)>1 and [m[1] for m in singles]==[chr(97+i) for i in range(len(singles))]
        panels=[(m[1],caption[m.end():singles[i+1].start() if i+1<len(singles) else len(caption)].strip())
                for i,m in enumerate(singles)] if split else [('whole',caption)]
    rows=[]
    detect_modalities = _modality_for_context
    preamble=caption[:markers[0].start()].strip() if split and markers else ''
    shared_modalities=detect_modalities(preamble)
    shared_domains={MODALITY_COMPONENT_FALLBACK[m] for m in shared_modalities if m in MODALITY_COMPONENT_FALLBACK}
    if set(shared_modalities)&{'MICROSTRUCTURE','OUT_OF_SCOPE','OUT_OF_SCOPE_LCA'}:
        shared_domains.add('NON_NUMERIC_CONTEXT')
    local_context = ''
    for label,text in panels:
        context_for_panel = local_context
        # A clause ending in "of/for" before the next marker introduces the
        # following panel group, not the panel whose text precedes it.
        next_header = re.search(r'(?:^|[.;])\s*([^.;]+\b(?:of|for))\s*$', text, re.I) if split else None
        if next_header and detect_modalities(next_header[1]):
            local_context = next_header[1].strip()
            text = text[:next_header.start()].strip()
        modalities=detect_modalities(text)
        if modalities and not (next_header and detect_modalities(next_header[1])):
            # Explicit local content terminates the previous forward-scoped
            # group; it does not itself establish a new group for later panels.
            local_context = ''
        inherited=False
        inherited_context = preamble
        if not modalities and context_for_panel:
            modalities = detect_modalities(context_for_panel)
            inherited = True
            inherited_context = context_for_panel
        # A common, single-purpose caption such as "XRD patterns ... (a) A;
        # (b) B" governs its sample-labelled panels. Never spread a mixed
        # caption's modalities or override an explicit panel-specific type.
        if split and not modalities and shared_modalities and len(shared_domains)==1:
            modalities=list(shared_modalities);inherited=True
        if not split:
            modalities=list(dict.fromkeys([*modalities,*(obj.get('modalities') or [])]))
        types=list(dict.fromkeys(MODALITY_COMPONENT_FALLBACK[m] for m in modalities if m in MODALITY_COMPONENT_FALLBACK))
        if not types:
            known_context=bool(set(modalities)&{'MICROSTRUCTURE','OUT_OF_SCOPE','OUT_OF_SCOPE_LCA'}) or bool(OUT_OF_SCOPE_RE.search(text))
            types=['OTHER'];requirement='NON_NUMERIC_CONTEXT' if known_context else 'AMBIGUOUS_REQUIRES_VISUAL'
            # Preserve legacy explicitly nonnumeric objects without source text.
            if not text and not schema.get('numeric_series_required'):requirement='NON_NUMERIC_CONTEXT'
        else:requirement='REQUIRED_UNIQUE'
        for figure_type in types:
            row={'panelLabel':label,'requirement':requirement,'figureType':figure_type,
                'reasonCode':'CAPTION_PANEL_ROUTING' if split else 'DETERMINISTIC_MODALITY_FALLBACK',
                'sourceCaptionFragment':text,'sourceCaptionSha256':digest(text)}
            if inherited:
                row.update(sourceCaptionContext=inherited_context,sourceCaptionContextSha256=digest(inherited_context))
            rows.append(row)
    return rows


def _validated_figure_decision_paper(
    decisions: dict[str, Any], run_id: str, base_framework_sha256: str,
    expected_source_ids: set[str],
) -> tuple[dict[str, Any], str]:
    if decisions.get("schemaVersion") != 3 or decisions.get("kind") != "WAKG_FIGURE_SEMANTIC_DECISIONS":
        raise ValueError("unsupported figure semantic decisions")
    observed = decisions.get("decisionSha256")
    if observed != digest({key: value for key, value in decisions.items() if key != "decisionSha256"}):
        raise ValueError("figure semantic decision hash mismatch")
    if decisions.get("verdict") != "PASS" or decisions.get("formalScientificValuesAllowed") is not False:
        raise ValueError("figure semantic decisions are not value-free PASS")
    matches = [paper for paper in decisions.get("papers") or [] if paper.get("runId") == run_id]
    if len(matches) != 1:
        raise ValueError(f"figure semantic paper cardinality mismatch: {run_id}")
    paper = matches[0]
    if paper.get("frameworkSha256") != base_framework_sha256:
        raise ValueError(f"figure semantic base framework mismatch: {run_id}")
    figures = paper.get("figures") or []
    by_source = {str(item.get("sourceObjectId")): item for item in figures}
    if len(by_source) != len(figures) or set(by_source) != expected_source_ids:
        raise ValueError(f"figure semantic source set mismatch: {run_id}")
    return by_source, str(observed)


def build_framework(
    plan: dict[str, Any],
    routing_paper: dict[str, Any],
    packets: list[dict[str, Any]],
    rule: dict[str, Any],
    supplement_spec: dict[str, Any] | None = None,
    figure_decisions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    run_id = str(rule["run_id"])
    if plan.get("run_id") != run_id or routing_paper.get("run_id") != run_id:
        raise ValueError("framework input run mismatch")
    if plan.get("pdf", {}).get("sha256") != routing_paper.get("pdf_sha256"):
        raise ValueError("framework PDF hash mismatch")
    source_objects = []
    source_by_table: dict[str, list[dict[str, Any]]] = {}
    for item in plan.get("source_items") or []:
        obj = {
            "source_object_id": item["item_id"],
            "scope": str(item.get("source_scope") or "unknown").upper(),
            "kind": item.get("kind"),
            "label": item.get("source_label"),
            "page": item.get("page"),
            "bbox": item.get("bbox"),
            "caption": item.get("caption"),
            "caption_sha256": item.get("caption_sha256") or item.get("context_sha256"),
            "modalities": item.get("modality") if isinstance(item.get("modality"), list) else [item.get("modality")],
            "expected_extraction": item.get("expected_extraction"),
            "origin": "DETERMINISTIC_SOURCE_INVENTORY",
            "status": "PLANNED",
        }
        if str(item.get("kind") or "").endswith("FIGURE"):
            obj["numeric_series_required"] = figure_requires_numeric_series(item)
            obj["expected_extraction"] = "NUMERIC_SERIES_OR_QUARANTINE" if obj["numeric_series_required"] else "FIGURE_ASSET"
        source_objects.append(obj)
        if item.get("kind") == "MAIN_TABLE":
            source_by_table.setdefault(table_key(item.get("source_key") or item.get("source_label")), []).append(obj)

    routing_tables = {str(table["table_id"]): table for table in routing_paper.get("tables") or []}
    rule_tables = rule.get("tables") or {}
    table_schemas = []
    routing_rows = []
    for table_id in sorted(routing_tables):
        table = routing_tables[table_id]
        table_rule = rule_tables.get(table_id) or {}
        matches = source_by_table.get(table_key(table_id), [])
        if len(matches) == 1:
            source_object = matches[0]
        else:
            recovered = {
                "source_object_id": stable_id("source-routing-table", [run_id, table_id, table.get("page")]),
                "scope": "MAIN",
                "kind": "MAIN_TABLE",
                "label": table_id,
                "page": table.get("page"),
                "bbox": None,
                "caption": None,
                "caption_sha256": None,
                "modalities": ["MIX_FORMULATION"],
                "expected_extraction": "STRUCTURED_TABLE",
                "origin": "DETERMINISTIC_ROUTING_RECOVERY",
                "status": "PLANNED",
            }
            source_objects.append(recovered)
            source_object = recovered
        identity_columns = list(table_rule.get("identity_columns") or [])
        factor_columns = list(table_rule.get("material_columns") or [])
        columns = []
        for column in table.get("columns") or []:
            key = str(column["key"])
            semantic_kind = "IDENTITY" if key in identity_columns else "FACTOR" if key in factor_columns else "OBSERVATION"
            columns.append({
                "column_key": key,
                "source_label": column.get("label"),
                "unit": column.get("unit"),
                "x": column.get("x"),
                "semantic_kind": semantic_kind,
                "parameter_keys": column.get("parameter_keys") or [],
            })
        table_packets = sorted((packet for packet in packets if packet_key(packet)[0] == table_id), key=lambda value: packet_key(value)[1])
        row_ids = []
        for packet in table_packets:
            key = packet_key(packet)
            row_id = stable_id("framework-row", [run_id, key[0], key[1], digest(packet)])
            row_ids.append(row_id)
            routing_rows.append({
                "row_id": row_id,
                "source_object_id": source_object["source_object_id"],
                "table_id": key[0],
                "source_row_index": key[1],
                "packet_sha256": digest(packet),
                "status": "PLANNED",
            })
        schema_id = stable_id("table-schema", [run_id, table_id, columns, row_ids])
        table_schemas.append({
            "table_schema_id": schema_id,
            "source_object_id": source_object["source_object_id"],
            "table_id": table_id,
            "page": table.get("page"),
            "header_band": table.get("header_band"),
            "columns": columns,
            "identity_columns": identity_columns,
            "factor_columns": factor_columns,
            "row_ids": row_ids,
            "expected_row_count": len(row_ids),
            "status": "READY" if row_ids else "UNRESOLVED_STRUCTURE",
        })

    materials = []
    for definition in rule.get("mats") or []:
        material_id = f"material-{run_id[9:]}-{definition['key']}"
        materials.append({
            "material_id": material_id,
            "source_name": definition.get("label"),
            "canonical_role": definition.get("role") or "unknown",
            "aliases": [],
            "source_object_ids": [],
            "status": "RULE_CANDIDATE_REQUIRES_SOURCE_BINDING",
        })
    factors = []
    observations = []
    for schema in table_schemas:
        for column in schema["columns"]:
            if column["semantic_kind"] == "FACTOR":
                factors.append({
                    "factor_id": stable_id("factor", [schema["table_schema_id"], column["column_key"]]),
                    "name": column["source_label"] or column["column_key"],
                    "semantic_role": "formulation",
                    "unit": column.get("unit"),
                    "source_object_ids": [schema["source_object_id"]],
                    "status": "IDENTIFIED",
                })
            elif column["semantic_kind"] == "OBSERVATION":
                observations.append({
                    "observation_id": stable_id("observation", [schema["table_schema_id"], column["column_key"]]),
                    "name": column["source_label"] or column["column_key"],
                    "kind": "table_observation",
                    "expected_unit": column.get("unit"),
                    "source_object_ids": [schema["source_object_id"]],
                    "extraction_route": "TABLE_CELL",
                    "status": "PLANNED",
                })
    figure_schemas = []
    for obj in source_objects:
        if obj.get("kind") not in {"MAIN_FIGURE", "SUPPLEMENT_FIGURE"}:
            continue
        required = bool(obj.get("numeric_series_required"))
        components=fallback_figure_components(obj,{'numeric_series_required':required})
        required=any(c['requirement'] in REQUIRED_COMPONENT_DISPOSITIONS and c['figureType'] in NUMERIC_COMPONENT_TYPES for c in components)
        ambiguous=any(c['requirement']=='AMBIGUOUS_REQUIRES_VISUAL' for c in components)
        obj['numeric_series_required']=required
        obj['semantic_components']=copy.deepcopy(components)
        figure_schemas.append({
            "figure_schema_id": stable_id("figure-schema", [run_id, obj["source_object_id"]]),
            "source_object_id": obj["source_object_id"],
            "series": [],
            "calibration": {"x_axis": None, "y_axis": None, "coordinate_space": None},
            "numeric_series_required": required,
            "semantic_components":components,
            "status": "PLANNED_DIGITIZATION" if required else "REQUIRES_SEMANTIC_REVIEW" if ambiguous else "ASSET_ONLY",
        })
    supplement_links = []
    for item in plan.get("source_items") or []:
        if item.get("kind") != "SUPPLEMENT_REFERENCE":
            continue
        supplement_links.append({
            "main_reference_object_id": item["item_id"],
            "reference_label": item.get("source_label"),
            "resolved_content_object_ids": item.get("resolved_content_item_ids") or [],
            "member_sha256": [member.get("sha256") for member in (supplement_spec or {}).get("members") or []],
            "status": "REFERENCE_RESOLVED" if item.get("resolved_content_item_ids") else "ACCESS_FAILED",
        })
    mix_families = []
    for schema in table_schemas:
        mix_families.append({
            "mix_family_id": stable_id("mix-family", [run_id, schema["table_id"]]),
            "source_label": schema["table_id"],
            "row_ids": schema["row_ids"],
            "source_object_ids": [schema["source_object_id"]],
            "status": "TABLE_GROUP_NOT_VERIFIED_MIX_FAMILY" if schema["row_ids"] else "QUARANTINED",
        })
    errors = validate_components(source_objects, table_schemas, routing_rows, packets)
    core = {
        "schema_version": FRAMEWORK_VERSION,
        "run_id": run_id,
        "paper_key": f"paper-{run_id[9:]}",
        "pdf_sha256": routing_paper["pdf_sha256"],
        "source_markdown_sha256": (plan.get("semantic_source") or {}).get("markdown_sha256"),
        "extraction_plan_sha256": plan.get("plan_sha256"),
        "status": "READY" if not errors else "BLOCKED",
        "status_scope": "DETERMINISTIC_ROUTING_ONLY",
        "relation_understanding_status": "SOURCE_SEMANTIC_REVIEW_PENDING",
        "materials": materials,
        "mix_families": mix_families,
        "factors": factors,
        "observations": observations,
        "source_objects": sorted(source_objects, key=lambda value: (value.get("page") is None, value.get("page") or 0, value["source_object_id"])),
        "table_schemas": table_schemas,
        "figure_schemas": figure_schemas,
        "supplement_links": supplement_links,
        "routing_rows": sorted(routing_rows, key=lambda value: (value["table_id"], value["source_row_index"])),
        "validation_errors": errors,
        "completeness_contract": {
            "required_source_object_ids": sorted(obj["source_object_id"] for obj in source_objects),
            "required_row_ids": sorted(row["row_id"] for row in routing_rows),
            "terminal_statuses": sorted(TERMINAL_STATUSES),
        },
    }
    # Agent decisions are bound to this value-free base framework.  Keeping
    # the receipt separate avoids the circular requirement that a decision
    # must cite the hash of a framework which already contains that decision.
    base_framework_sha256 = digest(core)
    if figure_decisions is not None:
        expected_source_ids = {
            schema["source_object_id"] for schema in figure_schemas
        }
        decisions_by_source, decision_sha256 = _validated_figure_decision_paper(
            figure_decisions, run_id, base_framework_sha256, expected_source_ids,
        )
        objects_by_id = {obj["source_object_id"]: obj for obj in source_objects}
        for schema in figure_schemas:
            source_id = schema["source_object_id"]
            decision = decisions_by_source.get(source_id)
            if decision is None:
                schema["semantic_components"] = []
                schema["semantic_processing_required"] = False
                continue
            components = copy.deepcopy(decision.get("components") or [])
            numeric_required = any(
                component.get("requirement") in REQUIRED_COMPONENT_DISPOSITIONS
                and component.get("figureType") in NUMERIC_COMPONENT_TYPES
                for component in components
            )
            semantic_required = any(
                component.get("requirement") in REQUIRED_COMPONENT_DISPOSITIONS
                for component in components
            )
            schema["semantic_components"] = components
            schema["numeric_series_required"] = numeric_required
            schema["semantic_processing_required"] = semantic_required
            if numeric_required:
                schema["status"] = "PLANNED_DIGITIZATION"
            elif semantic_required:
                schema["status"] = "PLANNED_SEMANTIC_ASSET"
            elif components and all(component.get("requirement") == "REDUNDANT_WITH_STRUCTURED_SOURCE" for component in components):
                schema["status"] = "REDUNDANT_WITH_STRUCTURED_SOURCE"
            else:
                schema["status"] = "ASSET_ONLY"
            obj = objects_by_id[source_id]
            obj["semantic_components"] = copy.deepcopy(components)
            obj["numeric_series_required"] = numeric_required
            obj["semantic_processing_required"] = semantic_required
            obj["expected_extraction"] = schema["status"]
        core["base_framework_sha256"] = base_framework_sha256
        core["figure_semantic_decision_sha256"] = decision_sha256
    core["framework_sha256"] = digest(core)
    return core


def validate_components(source_objects: list[dict[str, Any]], table_schemas: list[dict[str, Any]], routing_rows: list[dict[str, Any]], packets: list[dict[str, Any]]) -> list[str]:
    errors = []
    source_ids = [obj["source_object_id"] for obj in source_objects]
    if len(source_ids) != len(set(source_ids)):
        errors.append("DUPLICATE_SOURCE_OBJECT_ID")
    row_ids = [row["row_id"] for row in routing_rows]
    if len(row_ids) != len(set(row_ids)):
        errors.append("DUPLICATE_ROW_ID")
    routes = [(row["table_id"], row["source_row_index"]) for row in routing_rows]
    packet_routes = [packet_key(packet) for packet in packets]
    if sorted(routes) != sorted(packet_routes):
        errors.append("ROUTING_ROW_SET_MISMATCH")
    if len(routes) != len(set(routes)):
        errors.append("DUPLICATE_ROUTING_ROW")
    for schema in table_schemas:
        if schema["source_object_id"] not in source_ids:
            errors.append(f"TABLE_SOURCE_OBJECT_MISSING:{schema['table_id']}")
        if schema["expected_row_count"] != len(schema["row_ids"]):
            errors.append(f"TABLE_ROW_COUNT_MISMATCH:{schema['table_id']}")
    return errors


def validate_framework(framework: dict[str, Any], plan: dict[str, Any], packets: list[dict[str, Any]]) -> list[str]:
    errors = []
    if framework.get("schema_version") != FRAMEWORK_VERSION:
        errors.append("UNSUPPORTED_FRAMEWORK_VERSION")
    observed_hash = framework.get("framework_sha256")
    if observed_hash != digest({key: value for key, value in framework.items() if key != "framework_sha256"}):
        errors.append("FRAMEWORK_HASH_MISMATCH")
    if framework.get("run_id") != plan.get("run_id"):
        errors.append("FRAMEWORK_PLAN_RUN_MISMATCH")
    if framework.get("pdf_sha256") != plan.get("pdf", {}).get("sha256"):
        errors.append("FRAMEWORK_PLAN_PDF_MISMATCH")
    if framework.get("source_markdown_sha256") != (plan.get("semantic_source") or {}).get("markdown_sha256"):
        errors.append("FRAMEWORK_MARKDOWN_MISMATCH")
    if framework.get("extraction_plan_sha256") != plan.get("plan_sha256"):
        errors.append("FRAMEWORK_PLAN_HASH_MISMATCH")
    source_objects = framework.get("source_objects") or []
    plan_ids = {item["item_id"] for item in plan.get("source_items") or []}
    framework_ids = {item["source_object_id"] for item in source_objects}
    if not plan_ids <= framework_ids:
        errors.append("PLAN_SOURCE_OBJECT_MISSING_FROM_FRAMEWORK")
    errors.extend(validate_components(source_objects, framework.get("table_schemas") or [], framework.get("routing_rows") or [], packets))
    if errors or framework.get("status") != "READY":
        return sorted(set(errors or framework.get("validation_errors") or ["FRAMEWORK_NOT_READY"]))
    return []


def route_packets(framework: dict[str, Any], packets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_route = {packet_key(packet): packet for packet in packets}
    if len(by_route) != len(packets):
        raise ValueError("duplicate packet route")
    routed = []
    for row in framework.get("routing_rows") or []:
        key = (str(row["table_id"]), int(row["source_row_index"]))
        packet = by_route.get(key)
        if packet is None or digest(packet) != row.get("packet_sha256"):
            raise ValueError(f"framework routing row mismatch: {key}")
        routed.append(packet)
    if len(routed) != len(packets):
        raise ValueError("framework does not own every routing packet")
    return routed


def bind_records(framework: dict[str, Any], records: dict[str, Any]) -> dict[str, Any]:
    mixes = records.get("mixes") or []
    by_route: dict[tuple[str, int], list[str]] = {}
    for mix in mixes:
        ext = mix.get("extensions") or {}
        if ext.get("source_table") is None or ext.get("source_row") is None:
            continue
        by_route.setdefault((str(ext["source_table"]), int(ext["source_row"])), []).append(str(mix["mix_key"]))
    row_dispositions = {}
    failures = []
    for row in framework.get("routing_rows") or []:
        key = (str(row["table_id"]), int(row["source_row_index"]))
        matches = by_route.get(key, [])
        if len(matches) != 1:
            failures.append(f"FRAMEWORK_ROW_RECORD_CARDINALITY:{key[0]}:{key[1]}:{len(matches)}")
            row_dispositions[row["row_id"]] = {"status": "QUARANTINED", "record_keys": matches}
        else:
            row_dispositions[row["row_id"]] = {"status": "EXTRACTED", "record_key": matches[0]}
    plan_dispositions = ((records.get("extensions") or {}).get("extraction_plan") or {}).get("source_item_dispositions") or {}
    source_dispositions = {}
    for obj in framework.get("source_objects") or []:
        item_id = obj["source_object_id"]
        disposition = copy.deepcopy(plan_dispositions.get(item_id) or {})
        status = disposition.get("status")
        if not status:
            owned_rows = [row_dispositions[row["row_id"]] for row in framework.get("routing_rows") or [] if row["source_object_id"] == item_id]
            if owned_rows and all(row.get("status") == "EXTRACTED" for row in owned_rows):
                disposition = {"status": "EXTRACTED", "extraction_mode": "FRAMEWORK_ROUTED_ROWS", "row_count": len(owned_rows)}
            else:
                disposition = {"status": "QUARANTINED", "reason": "SOURCE_OBJECT_NOT_CLOSED"}
        source_dispositions[item_id] = disposition
    terminal = set((framework.get("completeness_contract") or {}).get("terminal_statuses") or [])
    for item_id, disposition in source_dispositions.items():
        if disposition.get("status") not in terminal:
            failures.append(f"FRAMEWORK_SOURCE_NONTERMINAL:{item_id}:{disposition.get('status')}")
    return {
        "schema_version": 1,
        "framework_sha256": framework["framework_sha256"],
        "source_object_dispositions": source_dispositions,
        "row_dispositions": row_dispositions,
        "failures": sorted(failures),
        "verdict": "PASS" if not failures else "BLOCKED",
    }


def _record_asset_references(records: dict[str, Any]) -> set[str]:
    """Return assets participating in a formal value/provenance chain.

    Scientific data can be referenced either by a MAT/MIX field or by the
    evidence link that proves that field. Supplement workbooks are sometimes
    referenced indirectly through the companion figure used for the review
    bbox, so follow that explicit source-data edge as well.
    """
    known = {str(asset.get("asset_key")) for asset in records.get("assets") or [] if asset.get("asset_key")}
    assets = {str(asset.get("asset_key")): asset for asset in records.get("assets") or [] if asset.get("asset_key")}
    found: set[str] = set()
    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if (str(key).endswith("asset_key") or str(key).endswith("asset_keys")):
                    candidates = child if isinstance(child, list) else [child]
                    found.update(str(item) for item in candidates if str(item) in known)
                walk(child)
        elif isinstance(value, list):
            for child in value:walk(child)
    walk({
        "mats": records.get("mats") or [],
        "mixes": records.get("mixes") or [],
        "evidence_links": records.get("evidence_links") or [],
    })
    # Evidence may point at the rendered companion image while the values were
    # decoded from its embedded workbook. Only an explicit hash-bound edge is
    # followed; filename or caption similarity is never sufficient.
    for key in list(found):
        linked = (assets.get(key, {}).get("extensions") or {}).get("source_data_asset_key")
        if linked in known:
            found.add(str(linked))
    return found


def assess_multimodal_records(framework: dict[str, Any], records: dict[str, Any]) -> dict[str, Any]:
    """Separate captured pixels from validated data for every semantic panel."""
    assets = {str(asset.get("asset_key")): asset for asset in records.get("assets") or [] if asset.get("asset_key")}
    referenced = _record_asset_references(records)
    # This narrow scope closes only independently reviewed visible markers,
    # not raw repeat-level measurements, interpolation, or other panels.
    import main_marker_assets
    complete_marker_assets = main_marker_assets.validated_asset_keys(records)
    from paper_source_assets import source_entries
    scientific_assets = source_entries(records, 'scientific_assets')
    main_by_source = {str(item.get("source_item_id")): str(item.get("asset_key")) for item in scientific_assets if item.get("source_item_id") and item.get("asset_key")}
    objects = {str(item["source_object_id"]): item for item in framework.get("source_objects") or []}
    rows = []
    blocking_components: list[dict[str, Any]] = []
    for schema in framework.get("figure_schemas") or []:
        source_id = str(schema["source_object_id"]);obj = objects[source_id]
        image_keys = []
        if source_id in main_by_source:
            image_keys.append(main_by_source[source_id])
        for key, asset in assets.items():
            ext = asset.get("extensions") or {}
            if str(ext.get("figure_number") or "").casefold() == str(obj.get("label") or "").casefold() and asset.get("kind") in {"supplement_figure", "main_figure"}:
                image_keys.append(key)
        image_keys = sorted(set(key for key in image_keys if key in assets))
        data_keys = []
        for key, asset in assets.items():
            ext = asset.get("extensions") or {}
            if str(ext.get("source_image_asset_key") or "") in image_keys:
                data_keys.append(key)
            if (
                str(ext.get("figure_number") or "").casefold() == str(obj.get("label") or "").casefold()
                and asset.get("kind") in {"supplement_chart", "supplement_data", "csv"}
            ):
                data_keys.append(key)
        for image_key in image_keys:
            linked = (assets[image_key].get("extensions") or {}).get("source_data_asset_key")
            if linked in assets:data_keys.append(str(linked))
        data_keys = sorted(set(data_keys))
        validated = sorted(key for key in data_keys if key in referenced and assets[key].get("sha256")
                           and (assets[key].get("extensions") or {}).get("review_status") == "confirmed"
                           # A valid partial observation is not complete panel coverage.
                           and not (assets[key].get("extensions") or {}).get("missing_percentiles")
                           and ((assets[key].get("extensions") or {}).get("coverage_scope", "complete") == "complete"
                                or key in complete_marker_assets))
        components = copy.deepcopy(schema.get("semantic_components") or [])
        if not components:
            components = fallback_figure_components(obj,schema)
        required_numeric = [
            component for component in components
            if component.get("requirement") in REQUIRED_COMPONENT_DISPOSITIONS
            and component.get("figureType") in NUMERIC_COMPONENT_TYPES
        ]
        component_rows = []
        for component in components:
            requirement = component.get("requirement")
            figure_type = component.get("figureType")
            panel_label = str(component.get("panelLabel") or "")
            numeric_required = requirement in REQUIRED_COMPONENT_DISPOSITIONS and figure_type in NUMERIC_COMPONENT_TYPES
            typed_validated = []
            for key in validated:
                ext = assets[key].get("extensions") or {}
                asset_type = ext.get("figure_type") or ext.get("figureType") or ext.get("modality")
                asset_panel = ext.get("panel_label") or ext.get("panelLabel")
                if len(components)>1 and (not asset_type or not asset_panel):
                    continue
                if asset_type and str(asset_type) != str(figure_type):
                    continue
                if asset_panel and str(asset_panel).casefold() != panel_label.casefold():
                    continue
                if asset_type or asset_panel:
                    typed_validated.append(key)
            # A generic source-data asset can close only one numeric semantic
            # component.  It must never make every panel in a compound figure
            # look validated merely because one CSV was attached.
            if not typed_validated and len(components) == 1 and len(required_numeric) == 1:
                typed_validated = [key for key in validated if not any(
                    (assets[key].get("extensions") or {}).get(name)
                    for name in ("figure_type", "figureType", "modality", "panel_label", "panelLabel"))]
            blocking = False
            if requirement == "AMBIGUOUS_REQUIRES_VISUAL":
                # An attachment cannot resolve an unknown scientific meaning.
                # A new reviewed semantic decision must identify the component.
                typed_validated=[]
                status = "AMBIGUOUS_REQUIRES_VISUAL"
                blocking = True
            elif requirement == "REDUNDANT_WITH_STRUCTURED_SOURCE":
                status = "REDUNDANT_WITH_STRUCTURED_SOURCE"
            elif requirement == "NON_NUMERIC_CONTEXT":
                status = "NON_NUMERIC_CONTEXT"
            elif requirement == "REQUIRED_UNIQUE" and not numeric_required:
                # An unsupported type is an implementation gap, not permission
                # to replace explicitly required extraction with captured pixels.
                typed_validated=[]
                status = "EXTRACTION_ROUTE_REQUIRED"
                blocking = True
            elif not numeric_required:
                status = "IMAGE_CAPTURED" if image_keys else "ASSET_MISSING"
                blocking = not bool(image_keys)
            elif typed_validated:
                status = "DETERMINISTIC_VALIDATED"
            elif image_keys:
                # Captured pixels are not scientific values.  They are a
                # internal candidate, not completion of required extraction.
                # A digitizer must create a hash-bound data asset before the
                # paper can enter final human review.
                status = "DIGITIZATION_CANDIDATE"
                blocking = True
            else:
                status = "ASSET_MISSING"
                blocking = True
            component_row = {
                **component,
                "numeric_series_required": numeric_required,
                "validated_data_asset_keys": sorted(typed_validated),
                "status": status,
                "blocking": blocking,
            }
            component_rows.append(component_row)
            if blocking:
                blocking_components.append({
                    "source_object_id": source_id,
                    "panel_label": panel_label,
                    "figure_type": figure_type,
                    "status": status,
                })
        if any(component["blocking"] for component in component_rows):
            status = "BLOCKED"
        elif any(component["status"] == "DETERMINISTIC_VALIDATED" for component in component_rows):
            status = "DETERMINISTIC_VALIDATED"
        elif any(component["status"] == "DIGITIZATION_CANDIDATE" for component in component_rows):
            status = "DIGITIZATION_CANDIDATE"
        elif any(component["status"] == "IMAGE_CAPTURED" for component in component_rows):
            status = "SEMANTIC_ASSET_CAPTURED"
        elif all(component["status"] == "REDUNDANT_WITH_STRUCTURED_SOURCE" for component in component_rows):
            status = "REDUNDANT_WITH_STRUCTURED_SOURCE"
        else:
            status = "ASSET_ONLY_NOT_NUMERIC"
        rows.append({
            "figure_schema_id": schema["figure_schema_id"], "source_object_id": source_id,
            "label": obj.get("label"), "modalities": obj.get("modalities") or [],
            "numeric_series_required": bool(required_numeric), "image_asset_keys": image_keys,
            "data_asset_keys": data_keys, "validated_data_asset_keys": validated,
            "components": component_rows, "status": status,
        })
    blocking_source_ids = sorted({item["source_object_id"] for item in blocking_components})
    return {
        "schema_version": 1,
        "framework_sha256": framework["framework_sha256"],
        "figure_count": len(rows),
        "numeric_series_required_count": sum(row["numeric_series_required"] for row in rows),
        "component_count": sum(len(row["components"]) for row in rows),
        "numeric_component_required_count": sum(component["numeric_series_required"] for row in rows for component in row["components"]),
        "deterministic_validated_count": sum(
            component["status"] == "DETERMINISTIC_VALIDATED"
            for row in rows for component in row["components"]
        ),
        "asset_only_count": sum(component["status"] == "ASSET_ONLY" for row in rows for component in row["components"]),
        "digitization_candidate_count": sum(
            component["status"] == "DIGITIZATION_CANDIDATE"
            for row in rows for component in row["components"]
        ),
        "blocking_component_count": len(blocking_components),
        "blocking_figure_count": len(blocking_source_ids),
        "blocking_source_object_ids": blocking_source_ids,
        "blocking_components": blocking_components,
        "figures": rows,
        "verdict": "PASS" if not blocking_components else "BLOCKED",
    }


def figure_digitization_quarantines(assessment: dict[str, Any]) -> list[dict[str, Any]]:
    """Create stable, value-free quarantine rows for image-only components."""
    rows: list[dict[str, Any]] = []
    for figure in assessment.get("figures") or []:
        for component in figure.get("components") or []:
            if component.get("status") not in ("DIGITIZATION_CANDIDATE", "EXTRACTION_ROUTE_REQUIRED"):
                continue
            core = {
                "type": "figure_digitization_candidate",
                "source_object_id": figure["source_object_id"],
                "figure_label": figure.get("label"),
                "panel_label": component.get("panelLabel"),
                "figure_type": component.get("figureType"),
                "source_value": None,
                "formal_value_present": False,
                "image_asset_keys": figure.get("image_asset_keys") or [],
                "resolution": "retain_hash_bound_image_without_promoting_scientific_values",
                "reason": "no validated native or digitized data asset",
            }
            if component.get('status')=='EXTRACTION_ROUTE_REQUIRED':
                core['type']='figure_extraction_route_required'
                core['reason']='explicit required component has no supported extraction route'
            rows.append({"quarantine_key": "figure-q-" + digest(core)[:24], **core})
    return sorted(rows, key=lambda item: item["quarantine_key"])


def main() -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate")
    validate.add_argument("--framework", type=Path, required=True)
    validate.add_argument("--plan", type=Path, required=True)
    validate.add_argument("--packets", type=Path, required=True)
    validate.add_argument("--write", type=Path)
    args = parser.parse_args()
    framework = json.loads(args.framework.read_text(encoding="utf-8"))
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    packet_payload = json.loads(args.packets.read_text(encoding="utf-8"))
    packets = packet_payload.get("packets") if isinstance(packet_payload, dict) else packet_payload
    errors = validate_framework(framework, plan, packets or [])
    result = {"schema_version": 1, "verdict": "PASS" if not errors else "BLOCKED", "failures": errors}
    if args.write:
        atomic_json(args.write, result)
    print(canonical(result))
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
