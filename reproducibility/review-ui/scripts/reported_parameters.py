"""Backward-compatible display contract for paper-reported MIX parameters."""
from __future__ import annotations

from typing import Any

PARAMETER_STATUSES = {
    "reported",
    "not_reported",
    "not_applicable",
    "candidate_conversion",
    "not_extracted",
}

STATUS_LABELS = {
    "reported": "原文有值",
    "derived_from_source": "根据原表多列生成",
    "system_note": "系统处理说明",
    "not_reported": "原文未报告",
    "not_applicable": "不适用",
    "candidate_conversion": "候选换算（非正式值）",
    "not_extracted": "已定位，尚未转录",
    "evidence_blocked": "来源待修复",
}

# Reviewer-facing source wording is a closed vocabulary. Exact page, table,
# token, and formula details live in evidence/transformation fields instead of
# being concatenated into these labels.
SOURCE_EXPLANATION_TEMPLATES = {
    "DIRECT_SOURCE_VALUE": "原文直接报告",
    "SOURCE_DERIVED_VALUE": "根据原文换算",
    "SOURCE_COMPOSITE_VALUE": "根据原文多个字段组合",
    "SOURCE_DASH_NO_VALUE": "原表为“/”（无数值）",
    "SOURCE_NOT_REPORTED": "原文未报告",
    "SOURCE_NOT_APPLICABLE": "该字段不适用",
    "SOURCE_LOCATED_NOT_TRANSCRIBED": "已定位，尚未转录",
    "SOURCE_CANDIDATE_CONVERSION": "原文换算候选（非正式值）",
    "SYSTEM_PROCESSING_NOTE": "系统处理说明",
    "SOURCE_EVIDENCE_BLOCKED": "来源未通过自动核验",
}
SOURCE_EXPLANATION_TEMPLATE_VERSION = 1


def source_explanation_code(
    status: str,
    reason: str | None = None,
    provenance: dict[str, Any] | None = None,
) -> str:
    """Map internal state to one closed reviewer-facing source template."""
    if reason == "source_dash":
        return "SOURCE_DASH_NO_VALUE"
    if status == "reported":
        item = provenance or {}
        if item.get("extraction_method") == "identity-from-cited-formulation-row":
            return "SOURCE_COMPOSITE_VALUE"
        if item.get("formula"):
            return "SOURCE_DERIVED_VALUE"
        return "DIRECT_SOURCE_VALUE"
    return {
        "derived_from_source": "SOURCE_COMPOSITE_VALUE",
        "system_note": "SYSTEM_PROCESSING_NOTE",
        "not_reported": "SOURCE_NOT_REPORTED",
        "not_applicable": "SOURCE_NOT_APPLICABLE",
        "candidate_conversion": "SOURCE_CANDIDATE_CONVERSION",
        "not_extracted": "SOURCE_LOCATED_NOT_TRANSCRIBED",
        "evidence_blocked": "SOURCE_EVIDENCE_BLOCKED",
    }.get(status, "SYSTEM_PROCESSING_NOTE")


def source_explanation(
    status: str,
    reason: str | None = None,
    provenance: dict[str, Any] | None = None,
) -> tuple[str, str]:
    code = source_explanation_code(status, reason, provenance)
    return code, SOURCE_EXPLANATION_TEMPLATES[code]


def parameter(
    parameter_key: str,
    original_label: str,
    value: Any,
    unit: str | None,
    basis: str | None,
    semantic_role: str,
    status: str,
    reason: str | None,
    evidence_key: str | None,
    transformation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the stable, reviewer-facing parameter contract.

    ``reported_parameters`` is intentionally a materials extension: it retains
    paper wording and status without asserting a WAKG canonical conversion.
    """
    if status == 'derived':
        # Source writers distinguish observations from calculated values. The
        # UI uses its existing reported state plus SOURCE_DERIVED_VALUE; this
        # is not permission to promote an unreviewed conversion candidate.
        from transformation_contract import _validate_receipt
        if (value is None or not evidence_key or not isinstance(transformation,dict)
                or _validate_receipt(transformation,value)
                or transformation.get('output',{}).get('unit') != unit):
            raise ValueError('derived parameter requires a valid formal source transformation')
        status = 'reported'
    if status not in PARAMETER_STATUSES:
        raise ValueError(f"unsupported reported-parameter status: {status}")
    if status == "reported" and value is None:
        raise ValueError("a reported parameter requires a non-null value")
    if status == "candidate_conversion" and (value is None or not transformation):
        raise ValueError("a candidate conversion requires value and transformation")
    if status in {"not_reported", "not_applicable", "not_extracted"} and value is not None:
        raise ValueError(f"absent parameter must remain null: {parameter_key}")
    return {
        "parameter_key": parameter_key,
        "original_label": original_label,
        "value": value,
        "unit": unit,
        "basis": basis,
        "semantic_role": semantic_role,
        "status": status,
        "reason": reason,
        "evidence_key": evidence_key,
        "transformation": transformation,
    }


def _material_evidence(mix: dict[str, Any]) -> str | None:
    provenance = mix.get("field_provenance") or {}
    value = provenance.get("/modules/materials") or {}
    key = value.get("evidence_key")
    return key if isinstance(key, str) else None


def _explicit_parameters(values: Any) -> list[dict[str, Any]]:
    if not isinstance(values, list):
        raise ValueError("reported_parameters must be a list")
    result: list[dict[str, Any]] = []
    for item in values:
        if not isinstance(item, dict):
            raise ValueError("reported_parameters entries must be objects")
        row = parameter(
            str(item.get("parameter_key") or ""),
            str(item.get("original_label") or ""),
            item.get("value"),
            item.get("unit"),
            item.get("basis") or item.get("mass_basis"),
            str(item.get("semantic_role") or "paper_reported_parameter"),
            str(item.get("status") or ("reported" if item.get("value") is not None and item.get("evidence_key") else "not_reported")),
            item.get("reason"),
            item.get("evidence_key"),
            item.get("transformation"),
        )
        for key in ("original_value", "original_unit", "extraction_method", "confidence", "source_binding"):
            if key in item:
                row[key] = item[key]
        result.append(row)
    if any(not item["parameter_key"] or not item["original_label"] for item in result):
        raise ValueError("reported_parameters require parameter_key and original_label")
    seen={}
    for item in result:
        key=(item['parameter_key'],str(item.get('basis')),str(item.get('unit')))
        if key in seen and seen[key]!=item.get('value'):
            raise ValueError('reported_parameters contain conflicting values on the same basis and unit')
        seen[key]=item.get('value')
    return result


def material_reported_parameters(mix: dict[str, Any]) -> list[dict[str, Any]]:
    """Adapt future structured fields and legacy table extensions without inference."""
    materials = ((mix.get("modules") or {}).get("materials") or {})
    extensions = materials.get("extensions") or {}
    if "reported_parameters" in extensions:
        rows = _explicit_parameters(extensions["reported_parameters"])
        canonical = {}
        for destination in ("solid_materials", "activators", "fine_aggregate", "coarse_aggregate", "platform_ratios"):
            for index, item in enumerate(materials.get(destination) or []):
                meta = item.get("extensions") or {}
                key = meta.get("source_parameter_key")
                if not key:
                    continue
                if key in canonical:
                    raise ValueError(f"duplicate canonical material parameter: {key}")
                canonical[key] = (item, f"/modules/materials/{destination}/{index}/value")
        for index, row in enumerate(rows):
            row["_field_path"] = f"/modules/materials/extensions/reported_parameters/{index}/value"
            entry = canonical.pop(row["parameter_key"], None)
            if entry is None:
                continue
            item, path = entry
            if any(item.get(key) != row.get(key) for key in ("value", "unit", "transformation")):
                raise ValueError(f"canonical material projection drift: {row['parameter_key']}")
            row.update({key: item.get(key) for key in ("value", "unit", "transformation", "evidence_key")})
            receipt = (mix.get("field_provenance") or {}).get(path) or {}
            for key in ("original_value", "original_unit", "extraction_method", "confidence", "source_binding"):
                if receipt.get(key) is not None:
                    row[key] = receipt[key]
            row["_field_path"] = path
            row["_canonical_material"] = True
        if canonical:
            raise ValueError(f"canonical material parameters missing source receipts: {sorted(canonical)}")
        return rows
    evidence_key = _material_evidence(mix)
    table = extensions.get("reported_table_3")
    if isinstance(table, dict) and {"X2O_to_SiO2", "H2O_to_X2O", "H2O2_mass_percent"} <= set(table):
        ratio = table["X2O_to_SiO2"]
        if not isinstance(ratio, (int, float)) or ratio == 0:
            raise ValueError("legacy X2O/SiO2 ratio must be nonzero numeric")
        candidate = 1 / ratio
        return [
            parameter("x2o_to_sio2", "X2O/SiO2", ratio, "molar ratio", "reported activator ratio", "reported_activator_ratio", "reported", "Table 3 reported value; no canonical conversion applied.", evidence_key),
            parameter("h2o_to_x2o", "H2O/X2O", table["H2O_to_X2O"], "molar ratio", "reported activator ratio", "reported_activator_ratio", "reported", "Table 3 reported value; no canonical conversion applied.", evidence_key),
            parameter("h2o2_mass_percent", "H2O2", table["H2O2_mass_percent"], "mass-%", "reported additive mass percentage", "reported_additive_content", "reported", "Table 3 reported value.", evidence_key),
            parameter("fa_bfs", "FA/BFS", None, None, None, "precursor_blend_ratio", "not_applicable", "Selected AAM row does not report a fly-ash/slag blend parameter.", None),
            parameter("water_to_binder", "Water/binder", None, None, None, "water_binder_ratio", "not_reported", "Table 3 reports activator molar ratios, not a water/binder mass basis.", None),
            parameter("activator_dosage", "Activator dosage", None, None, None, "activator_dosage", "not_reported", "No activator dosage on a compatible mass basis was transcribed.", None),
            parameter("ms_candidate", "Ms", candidate, "dimensionless", "candidate derived from X2O/SiO2", "silicate_modulus", "candidate_conversion", "Non-formal candidate only; do not promote without semantic review.", evidence_key, {"formula": "Ms = 1 / (X2O/SiO2)", "forward_check": f"1 / {ratio} = {candidate}", "reverse_check": f"1 / {candidate} = {ratio}", "formal": False}),
        ]
    untranscribed = extensions.get("untranscribed_table_cells")
    source = extensions.get("source_locator") or materials.get("reported_mass_basis")
    reason = str(untranscribed or "No paper-reported composition parameter was transcribed in this bounded slice.")
    if source:
        reason = f"{reason} Source locator: {source}."
    return [
        parameter("reported_table_cells", "配比表格参数", None, None, str(source) if source else None, "paper_reported_parameters", "not_extracted", reason, None),
    ]
