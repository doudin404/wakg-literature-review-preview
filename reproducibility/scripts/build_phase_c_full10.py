"""Source-only full-ten WAKG semantic extraction and contract verification."""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import fitz


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / 'scripts') not in sys.path:
    sys.path.insert(0, str(ROOT / 'scripts'))
GENERATOR_VERSION = "phase-c-source-only-82-source-relation-input"
DEFAULT_FIGURE_DECISIONS = Path("fixtures/user_corpus_figure_semantics_v3.json")


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


phase_b = load_module("phase_b_runtime", ROOT / "scripts" / "build_phase_b_semantic_canary.py")
pipeline = load_module("wakg_pipeline_runtime", ROOT / "skills" / "wakg-literature-pipeline" / "scripts" / "pipeline.py")
multimodal = load_module("multimodal_plan_runtime", ROOT / "scripts" / "multimodal_extraction_plan.py")
supplement_docx = load_module("supplement_docx_runtime", ROOT / "scripts" / "supplement_docx.py")
semantic_markdown = load_module("semantic_markdown_runtime", ROOT / "scripts" / "build_semantic_markdown.py")
semantic_framework = load_module("paper_semantic_framework_runtime", ROOT / "scripts" / "paper_semantic_framework.py")
source_relation_inputs = load_module("source_relation_inputs_runtime", ROOT / "scripts" / "build_source_relation_inputs.py")
transformation_contract = load_module("transformation_contract_runtime", ROOT / "scripts" / "transformation_contract.py")
source_bibliography = load_module("source_bibliography_runtime", ROOT / "scripts" / "source_bibliography.py")
source_material_identity = load_module("source_material_identity_runtime", ROOT / "scripts" / "source_material_identity.py")
source_narrative_psd = load_module("source_narrative_psd_runtime", ROOT / "scripts" / "source_narrative_psd.py")
source_fixed_mix = load_module("source_fixed_mix_runtime", ROOT / "scripts" / "source_fixed_mix_parameters.py")
source_performance = load_module("source_performance_runtime", ROOT / "scripts" / "source_performance_candidates.py")
PROSE_PERFORMANCE_PATH = ROOT / "fixtures/prose-performance-p01-bindings-v1.json"
PROSE_PERFORMANCE_P02_PATH = ROOT / "fixtures/prose-performance-p02-bindings-v1.json"
PROSE_SLUMP_P02_PATH = ROOT / "fixtures/prose-performance-p02-slump-v1.json"
PROSE_ENDPOINT_P06_PATH = ROOT / "fixtures/prose-performance-p06-endpoints-v1.json"
INGREDIENT_P01_PATH = ROOT / "fixtures/ingredient-inventory-p01-v1.json"
INGREDIENT_P02_PATH = ROOT / "fixtures/ingredient-inventory-p02-common-v1.json"
SOLUTION_P02_PATH = ROOT / "fixtures/ingredient-inventory-p02-solutions-v1.json"
solution_variants = load_module('solution_variants_runtime', ROOT / 'scripts/source_solution_variants.py')
source_sparse = load_module('source_sparse_runtime', ROOT / 'scripts/source_sparse_pipeline.py')
import extract_main_figure_markers as main_markers
import main_marker_source_context
import main_marker_owners
import main_marker_merge
import main_marker_assets
import main_marker_registry
import source_common_recipe
import source_specimen_curing
import source_design_relations
import source_recipe_performance
import performance_completion_consumer
import raster_routes
import vector_outline_routes
import raster_dot_routes
MAIN_MARKER_BINDING_PATH = ROOT / 'fixtures/main-marker-p06-fig1-v1.json'
MAIN_MERGE_ACCEPTANCE_PATH = ROOT / 'fixtures/main-marker-p06-merge-acceptance-v6.json'
ingredient_inventory = load_module('ingredient_inventory_runtime', ROOT / 'scripts/source_ingredient_inventory.py')
FIXED_MIX_MAPPING_PATH = ROOT / "fixtures/fixed-mix-p01-mapping-v1.json"
source_aggregate_materials = load_module('source_aggregate_materials_runtime',ROOT/'scripts/source_aggregate_materials.py')
canonical_materials = load_module("canonical_materials_runtime", ROOT / "scripts" / "canonical_materials.py")
vector_curves = load_module("vector_curves_runtime", ROOT / "scripts" / "vector_curves.py")
vector_curve_bindings = load_module("vector_curve_bindings_runtime", ROOT / "scripts" / "vector_curve_bindings.py")
verify_vector_curves = load_module("verify_vector_curves_runtime", ROOT / "scripts" / "verify_vector_curves.py")
promote_vector_curves = load_module("promote_vector_curves_runtime", ROOT / "scripts" / "promote_vector_curves.py")
raster_curves = load_module("raster_curves_runtime", ROOT / "scripts" / "raster_curves.py")
RASTER_SPEC_PATH = ROOT / 'fixtures/raster-psd-p01-candidate-v1.json'
RASTER_ACCEPTANCE_PATH = ROOT / 'fixtures/raster-psd-formal-acceptance-v1.json'
promote_raster_curves = load_module("promote_raster_curves_runtime", ROOT / "scripts" / "promote_raster_curves.py")
raster_panel_binding = load_module("raster_panel_binding_runtime", ROOT / "scripts" / "raster_panel_binding.py")
VECTOR_ACCEPTANCE_PATH = ROOT / "fixtures" / "vector_curve_acceptance_v1.json"

RULE_FORBIDDEN_KEYS = phase_b.FORBIDDEN_RULE_KEYS | {"expected_id", "expected_ids", "observation_value"}


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    raw = value if isinstance(value, bytes) else canonical(value).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def atomic_json(path: Path, value: Any) -> None:
    phase_b.atomic_json(path, value)


def reject_rule_answers(value: Any, location: str = "$") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).casefold() in RULE_FORBIDDEN_KEYS:
                raise ValueError(f"scientific answer key forbidden at {location}.{key}")
            reject_rule_answers(child, f"{location}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            reject_rule_answers(child, f"{location}[{index}]")


def load_inputs(root: Path, routing_path: Path, rules_path: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    routing = json.loads(routing_path.read_text(encoding="utf-8"))
    rules = json.loads(rules_path.read_text(encoding="utf-8"))
    reject_rule_answers(rules)
    schema_path = root / "fixtures" / "user_corpus_routing_schemas_v1.json"
    routing_schema = json.loads(schema_path.read_text(encoding="utf-8"))
    if rules.get("schema_version") != 2 or len(rules.get("papers", [])) != 10:
        raise ValueError("Phase-C rules must contain exactly ten papers")
    schemas = {paper["run_id"]: paper for paper in routing_schema["papers"]}
    rule_ids = [paper["run_id"] for paper in rules["papers"]]
    if set(rule_ids) != set(schemas):
        raise ValueError("routing and semantic paper sets differ")
    for run_id in rule_ids:
        pdf = root / schemas[run_id]["pdf_relative_path"]
        if phase_b.file_digest(pdf) != schemas[run_id]["pdf_sha256"]:
            raise ValueError(f"frozen PDF hash mismatch: {run_id}")
    supplement_path = root / "fixtures" / "supplement-manifests-v1.json"
    supplement_manifest = json.loads(supplement_path.read_text(encoding="utf-8")) if supplement_path.exists() else {"schema_version": 1, "papers": []}
    supplements = {paper["run_id"]: paper for paper in supplement_manifest.get("papers") or []}
    unknown_supplements = sorted(set(supplements) - set(schemas))
    if unknown_supplements:
        raise ValueError(f"supplement manifest has unknown papers: {unknown_supplements}")
    for run_id, specification in supplements.items():
        for member in specification.get("members") or []:
            path = root / member["relative_path"]
            if not path.is_file() or phase_b.file_digest(path) != member["sha256"] or path.stat().st_size != int(member["bytes"]):
                raise ValueError(f"frozen supplement mismatch: {run_id}:{member['relative_path']}")
    hashes = {"routing_bundle_sha256": phase_b.file_digest(routing_path), "routing_schema_sha256": phase_b.file_digest(schema_path), "semantic_rules_sha256": phase_b.file_digest(rules_path), "supplement_manifest_sha256": phase_b.file_digest(supplement_path) if supplement_path.exists() else digest(supplement_manifest)}
    return routing, rules, {"schemas": schemas, "hashes": hashes, "supplements": supplements}


def mat_record(run_id: str, paper_key: str, definition: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "1.0", "mat_key": f"mat-{run_id[9:]}-{definition['key']}", "paper_key": paper_key,
        "custom_material_id": definition["label"], "material_type": definition.get("material_type"), "source_type": "文献数据",
        "literature_title": None, "doi": None, "year": None,
        "physical_properties": {"specific_surface_m2_kg": None, "specific_surface_method": None, "true_density_kg_m3": None, "apparent_density_kg_m3": None, "bulk_density_kg_m3": None, "total_porosity_percent": None, "open_porosity_percent": None, "closed_porosity_percent": None},
        "particle_size_distribution": {"schema_version": "1.0", "x_axis": {"name":"particle_size","unit":"um","direction":"ascending"}, "y_axis":{"name":"distribution_fraction","unit":"percent"}, "reported_curve_type": None, "points_asset_key": None, "source_image_asset_key": None, "d10_um": None, "d50_um": None, "d90_um": None, "conversion_review": None, "extensions": {}},
        "xrf_composition": {"schema_version": "1.0", "unit": "wt.%", "rows": [], "original_total": None, "normalisation_candidate": None, "extensions": {}},
        "ftir_spectrum": None, "xrd_qxrd": None, "si29_nmr_spectrum": None, "al27_nmr_spectrum": None,
        "field_provenance": {}, "extensions": {"semantic_role": definition["role"]},
    }


def evidence_link(run_id: str, paper_key: str, asset_key: str, record_key: str, record_type: str, field_path: str, page: int, table: str, token: dict[str, Any], method: str, ordinal: int) -> dict[str, Any]:
    key = f"ev-{run_id[9:]}-{digest([record_key, field_path, page, table, token['bbox'], token['token'], ordinal])[:24]}"
    return {
        "evidence_key": key, "asset_key": asset_key, "record_type": record_type, "record_key": record_key, "field_path": field_path,
        "page": int(page), "section": None, "table_number": table, "figure_number": None,
        "bbox": [round(float(value), 3) for value in token["bbox"]],
        "snippet": str(token["token"]), "snippet_sha256": hashlib.sha256(str(token["token"]).encode("utf-8")).hexdigest(),
        "extraction_method": method, "confidence": 0.99, "paper_key": paper_key,
        **({"extensions":{"source_binding":token["source_binding"]}} if token.get("source_binding") else {}),
    }


def provenance(link: dict[str, Any], token: dict[str, Any], unit: str | None) -> dict[str, Any]:
    return {"evidence_key": link["evidence_key"], "original_value": str(token["token"]), "original_unit": unit, "formula": None, "extraction_method": link["extraction_method"], "confidence": link["confidence"], "review_status": "confirmed"}


def duration_transformation(formula: str, curing: dict[str, Any]) -> dict[str, Any]:
    matches = re.findall(r"(?:duration_seconds|stage_\d+_seconds)\s*=\s*([0-9]+(?:\.[0-9]+)?)\s*(h|d)\s*\*\s*(3600|86400)", formula)
    recorded = [
        float(stage["duration_seconds"])
        for stage in curing.get("curing_stages") or []
        if stage.get("duration_seconds") is not None
    ]
    inputs = []
    for index, (raw, unit, factor) in enumerate(matches):
        source_value = float(raw)
        scale = float(factor)
        target_value = source_value * scale
        if index >= len(recorded) or abs(recorded[index] - target_value) > 1e-9:
            raise ValueError(f"curing duration formula/record mismatch: {formula}")
        inputs.append({"value": source_value, "unit": unit, "scale_to_seconds": scale})
    if len(inputs) != len(recorded):
        raise ValueError(f"curing duration formula does not cover every finite stage: {formula}")
    return {
        "kind": "duration_sequence_to_seconds", "formal": True,
        "formula": formula, "inputs": inputs,
        "output": {"values": recorded, "unit": "s"},
        "forward_check": {"computed": [item["value"] * item["scale_to_seconds"] for item in inputs], "recorded": recorded, "tolerance": 1e-9},
        "reverse_check": {"computed": [recorded[index] / item["scale_to_seconds"] for index, item in enumerate(inputs)], "recorded": [item["value"] for item in inputs], "tolerance": 1e-9},
    }


def ratio_component_transformation(name: str, token: str, parts: list[str], component_index: int) -> dict[str, Any]:
    values = [float(value) for value in parts]
    value = values[component_index]
    return {
        "kind": "ratio_component", "formal": True,
        "formula": f"{name} = component {component_index + 1} of reported ratio token {token}",
        "inputs": {"token": token, "components": values, "separator": ":"},
        "output": {"value": value, "unit": None, "component_index": component_index},
        "forward_check": {"computed": values[component_index], "recorded": value, "tolerance": 1e-12},
        "reverse_check": {"computed": ":".join(parts), "recorded": token},
    }


def scale_transformation(original_value: float, original_unit: str, value: float, unit: str, scale: float, formula: str) -> dict[str, Any]:
    return {
        "kind": "unit_scale", "formal": True, "formula": formula,
        "inputs": {"value": float(original_value), "unit": original_unit, "scale": float(scale)},
        "output": {"value": float(value), "unit": unit},
        "forward_check": {"computed": float(original_value) * float(scale), "recorded": float(value), "tolerance": 1e-9},
        "reverse_check": {"computed": float(value) / float(scale), "recorded": float(original_value), "tolerance": 1e-9},
    }


def parameter(parameter_key: str, original_label: str, unit: str | None, token: dict[str, Any], link: dict[str, Any]) -> dict[str, Any]:
    is_null = token.get("typed_state") == "null"
    return {
        "parameter_key": parameter_key, "original_label": original_label, "value": None if is_null else token.get("typed"), "unit": unit,
        "original_value": str(token["token"]), "original_unit": unit,
        "status": "not_applicable" if is_null else "reported", "reason": "source_dash" if is_null else None,
        "semantic_role": "reported_formulation", "transformation": token.get("transformation"), "evidence_key": link["evidence_key"],
        "extraction_method": link["extraction_method"], "confidence": link["confidence"],
        **({"source_binding":token["source_binding"]} if token.get("source_binding") else {}),
    }


def composite_cell_token(tokens: list[dict[str, Any]]) -> dict[str, Any]:
    """Collapse one multi-word source cell into one typed text observation."""
    # PDF words on one visual baseline often differ by a few thousandths in
    # y.  Sorting on raw y therefore moved the right half of a row before its
    # left half (for example ``curing for 28 days 100 % slag``).  Quantize only
    # for ordering; the original coordinates remain untouched for evidence.
    ordered = sorted(tokens, key=lambda item: (round(float(item["bbox"][1]), 1), float(item["bbox"][0])))
    source = " ".join(str(item["token"]) for item in ordered)
    return {
        **ordered[0],
        "token": source,
        "typed": source,
        "typed_state": "text",
        "bbox": [
            min(float(item["bbox"][0]) for item in ordered),
            min(float(item["bbox"][1]) for item in ordered),
            max(float(item["bbox"][2]) for item in ordered),
            max(float(item["bbox"][3]) for item in ordered),
        ],
    }


def words_in_band(words: list[tuple[Any, ...]], x0: float, x1: float, y0: float, y1: float) -> list[tuple[Any, ...]]:
    return sorted([word for word in words if x0 <= (word[0] + word[2]) / 2 < x1 and y0 <= word[1] < y1], key=lambda word: (word[1], word[0]))


def table_context_token(words: dict[int, list[tuple[Any, ...]]], table: dict[str, Any]) -> dict[str, Any] | None:
    if not table.get("context_y"):
        return None
    x0, x1 = table.get("context_x", [0, 10000])
    y0, y1 = table["context_y"]
    selected = words_in_band(words[table["page"]], x0, x1, y0, y1)
    return composite_cell_token([phase_b.token_from_word(word) for word in selected]) if selected else None


def method_context_token(words: dict[int, list[tuple[Any, ...]]], table: dict[str, Any]) -> dict[str, Any] | None:
    """Return a separate method-section locator when the table title is insufficient.

    A value cell proves the scalar and a table title can prove the specimen or
    observation family, but neither necessarily proves the reported test
    method.  Keeping the method locator separate prevents the generator from
    attaching a plausible method name to unrelated table text.
    """
    if not table.get("method_context_y"):
        return None
    page = int(table.get("method_context_page", table["page"]))
    x0, x1 = table.get("method_context_x", [0, 10000])
    y0, y1 = table["method_context_y"]
    selected = words_in_band(words[page], x0, x1, y0, y1)
    return composite_cell_token([phase_b.token_from_word(word) for word in selected]) if selected else None


def p55_semantics(header_words: list[tuple[Any, ...]]) -> dict[str, Any]:
    raw = " ".join(str(word[4]) for word in header_words)
    folded = raw.casefold()
    source_unit = "MPa" if "mpa" in folded else "m2/g" if "m2/g" in folded else "Pa" if "(pa)" in folded else "%" if "%" in raw else None
    unit = "m2/kg" if source_unit == "m2/g" else source_unit
    if "strength" in folded:
        condition = next(iter(re.findall(r"\b[0-9]+\b", raw)), None)
        specimen = "plate" if "plate" in folded else "tube" if "tube" in folded else None
        name = "compressive_strength" + (f"_{condition}" if condition else "") + (f"_{specimen}" if specimen else "")
    elif "bet" in folded:
        specimen = "plate" if "plate" in folded else "tube" if "tube" in folded else None
        preparation = "reflux" if "reflux" in folded else "shaker" if "shaker" in folded else None
        name = "bet" + (f"_{specimen}" if specimen else "") + (f"_{preparation}" if preparation else "")
    elif "viscosity" in folded:
        # The source header literally reports only ``Pa``.  Dynamic viscosity
        # requires a time dimension, so this cannot be silently promoted to a
        # formal Pa·s observation.
        name = "quarantined_ambiguous_viscosity_unit" if source_unit == "Pa" else "fresh_paste_viscosity"
    elif "volume" in folded:
        name = "tube_volume_increase"
    else:
        name = "quarantined_ambiguous_property"
    specimen = "plate" if "plate" in folded else "tube" if "tube" in folded else None
    conditions = [token for token in re.findall(r"\b[0-9]+(?:\.[0-9]+)?\b", raw)]
    return {"name": name, "unit": unit, "source_unit": source_unit, "specimen": specimen, "source_header": raw, "reported_conditions": conditions}


def empty_mix(run_id: str, paper_key: str, mix_key: str, identity: str, table: str, row: int) -> dict[str, Any]:
    return {
        "schema_version": "1.0", "mix_key": mix_key, "paper_key": paper_key, "mix_family_key": None,
        "modules": {
            "identity_source_specimen": {"custom_test_id": identity, "literature_source": {"title": None, "doi": None, "year": None}, "specimen_type": None, "specimens": [], "extensions": {}},
            "materials": {"mat_refs": [], "solid_materials": [], "activator_total_mass_g": None, "activators": [], "fine_aggregate": None, "coarse_aggregate": None, "reported_mass_basis": table, "platform_ratios": None, "conversion": None, "review_status": "confirmed", "extensions": {"reported_parameters": []}},
            "mixing_curing": {"mixing": None, "forming": None, "demoulding": None, "curing_stages": [], "curing_route": None, "age_origin": None, "extensions": {}},
            "performance": [], "characterizations": [],
        },
        "field_provenance": {}, "extensions": {"source_table": table, "source_row": row},
    }


def resolve_source_identity(table_rule: dict[str, Any], packet: dict[str, Any]) -> tuple[str, list[dict[str, Any]], str | None]:
    """Resolve a source identity, including labels split across merged table rows."""
    groups = table_rule.get("identity_groups") or []
    if groups:
        row = int(packet["source_row_index"])
        matches = [group for group in groups if int(group["rows"][0]) <= row <= int(group["rows"][1])]
        if len(matches) != 1:
            raise ValueError(f"expected one identity group for {packet['table_id']} row {row}, found {len(matches)}")
        group = matches[0]
        value = str(group["source_label"])
        bbox = [float(item) for item in group["bbox"]]
        token = {
            "column_key": "source_identity_group",
            "bbox": bbox,
            "token": value,
            "typed": value,
            "typed_state": "text",
        }
        return value, [token], "pymupdf_words_merged_identity_group_v1"
    tokens = [token for column in table_rule["identity_columns"] for token in phase_b.cell_tokens(packet, column)]
    values = [str(token["token"]) for token in tokens]
    # A source ID split at a visual line break remains one verbatim identifier,
    # not two identity dimensions.  The routing schema explicitly supplies the
    # continuation line; joining only a trailing-hyphen fragment is therefore
    # deterministic and does not invent a readable alias.
    if len(table_rule["identity_columns"]) == 1 and len(values) == 2 and values[0].endswith("-"):
        identity = values[0] + values[1]
    else:
        identity = "|".join(values)
    identity = identity or f"{packet['table_id']}-row-{packet['source_row_index']}"
    return identity, tokens, None


def apply_curing_profiles(
    run_id: str,
    paper_key: str,
    asset_key: str,
    rule: dict[str, Any],
    words: dict[int, list[tuple[Any, ...]]],
    mixes: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
) -> None:
    """Attach source-verified curing routes from closed semantic profiles.

    Profiles contain normalization rules, never accepted-run output.  Each
    profile must be anchored to an exact PDF region and every generated MIX
    must match exactly one profile.  This makes missing stage boundaries a
    build error instead of a reviewer-facing repair request.
    """
    profiles = rule.get("curing_profiles") or []
    if not profiles:
        raise ValueError(f"missing curing profiles: {run_id}")
    matched_profiles: set[int] = set()
    for mix in mixes:
        table = str((mix.get("extensions") or {}).get("source_table") or "")
        source_row = int((mix.get("extensions") or {}).get("source_row"))
        identity = str((((mix.get("modules") or {}).get("identity_source_specimen") or {}).get("custom_test_id") or ""))
        matches: list[tuple[int, dict[str, Any]]] = []
        for index, profile in enumerate(profiles):
            target_tables = [str(value) for value in profile.get("target_tables") or []]
            if target_tables and table not in target_tables:
                continue
            source_rows = [int(value) for value in profile.get("source_rows") or []]
            if source_rows and source_row not in source_rows:
                continue
            identity_regex = profile.get("identity_regex")
            if identity_regex and not re.search(str(identity_regex), identity, re.IGNORECASE):
                continue
            matches.append((index, profile))
        if len(matches) != 1:
            raise ValueError(f"expected one curing profile for {run_id}:{mix['mix_key']}, found {len(matches)}")
        profile_index, profile = matches[0]
        matched_profiles.add(profile_index)
        source = profile["source"]
        page = int(source["page"])
        x0, x1 = [float(value) for value in source["x"]]
        y0, y1 = [float(value) for value in source["y"]]
        selected = words_in_band(words[page], x0, x1, y0, y1)
        if not selected:
            raise ValueError(f"empty curing source region: {run_id}:{profile_index}")
        token = composite_cell_token([phase_b.token_from_word(word) for word in selected])
        normalized_source = re.sub(r"\s+", " ", str(token["token"])).casefold()
        for required in source.get("required_terms") or []:
            if re.sub(r"\s+", " ", str(required)).casefold() not in normalized_source:
                raise ValueError(f"curing source term missing for {run_id}:{profile_index}: {required!r}")
        field_path = "/modules/mixing_curing/curing_stages"
        link = evidence_link(
            run_id, paper_key, asset_key, mix["mix_key"], "mix", field_path,
            page, str(source.get("section") or "curing-methods"), token,
            "pymupdf_words_curing_profile_v1", 1,
        )
        evidence.append(link)
        curing = copy.deepcopy(profile["curing"])
        mix["modules"]["mixing_curing"] = curing
        source_unit = source.get("original_unit")
        mix["field_provenance"][field_path] = provenance(link, token, source_unit)
        mix["field_provenance"][field_path]["formula"] = source.get("formula")
        if source.get("formula"):
            transformation = duration_transformation(str(source["formula"]), curing)
            mix["field_provenance"][field_path]["transformation"] = transformation
            units = {item["unit"] for item in transformation["inputs"]}
            if len(units) == 1:
                mix["field_provenance"][field_path]["original_unit"] = next(iter(units))
    unused = sorted(set(range(len(profiles))) - matched_profiles)
    if unused:
        raise ValueError(f"unused curing profiles for {run_id}: {unused}")


def paper_plan(root: Path, routing_paper: dict[str, Any], supplement_spec: dict[str, Any] | None, packets: list[dict[str, Any]], rule: dict[str, Any], figure_decisions: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any], list[tuple[Path, dict[str, Any]]], dict[str, Any]]:
    inventories: list[tuple[Path, dict[str, Any]]] = []
    for member in (supplement_spec or {}).get("members") or []:
        if member.get("kind") != "docx":
            raise ValueError(f"unsupported supplement kind: {member.get('kind')}")
        path = root / member["relative_path"]
        inventory = supplement_docx.inspect_docx(path)
        if inventory["docx"]["sha256"] != member["sha256"]:
            raise ValueError("supplement inventory/member hash mismatch")
        inventories.append((path, inventory))
    pdf = root / routing_paper["pdf_relative_path"]
    source_text, source_metadata = semantic_markdown.build_source_markdown(pdf, routing_paper["run_id"])
    semantic_source = semantic_markdown.parse_source_markdown(source_text, source_metadata)
    plan = multimodal.build_plan(pdf, routing_paper["run_id"], [inventory for _, inventory in inventories], semantic_source)
    framework = semantic_framework.build_framework(plan, routing_paper, packets, rule, supplement_spec, figure_decisions)
    errors = semantic_framework.validate_framework(framework, plan, packets)
    if errors:
        raise ValueError(f"semantic framework blocked for {routing_paper['run_id']}: {errors}")
    relation_input = source_relation_inputs.build(pdf, routing_paper['pdf_sha256'], routing_paper['run_id'])
    return plan, framework, inventories, {"text": source_text, "metadata": source_metadata, "relation_input": relation_input}


def paper_input_hashes(
    routing_paper: dict[str, Any],
    source_packets: list[dict[str, Any]],
    rule: dict[str, Any],
    supplement_spec: dict[str, Any] | None,
    plan: dict[str, Any],
    framework: dict[str, Any],
    semantic_artifact: dict[str, Any],
) -> dict[str, Any]:
    """Compute the one canonical input receipt used by build and resume.

    The record builder consumes framework-routed packets, not the raw routing
    bundle rows. Keeping this computation in one function prevents a resume
    check from hashing a different logical object than the writer.
    """
    ordered = sorted(source_packets, key=lambda packet: (packet["table_id"], packet["source_row_index"]))
    from qxrd_basis_context import input_receipt as basis_receipt
    routed = semantic_framework.route_packets(framework, ordered)
    return {
        "pdf_sha256": routing_paper["pdf_sha256"],
        "source_markdown_sha256": semantic_artifact["metadata"]["markdownSha256"],
        "source_relation_input_sha256": digest(semantic_artifact["relation_input"]),
        "source_relation_input_implementation_sha256": phase_b.file_digest(ROOT / 'scripts/build_source_relation_inputs.py'),
        "routing_packets_sha256": digest(routed),
        "semantic_rule_sha256": digest(rule),
        "extraction_plan_sha256": plan["plan_sha256"],
        "semantic_framework_sha256": framework["framework_sha256"],
        "generator_implementation_sha256": phase_b.file_digest(ROOT / 'scripts/build_phase_c_full10.py'),
        "semantic_framework_implementation_sha256": phase_b.file_digest(ROOT / 'scripts/paper_semantic_framework.py'),
        "paper_source_assets_implementation_sha256": phase_b.file_digest(ROOT / 'scripts/paper_source_assets.py'),
        "figure_semantic_decision_sha256": framework.get("figure_semantic_decision_sha256"),
        "supplement_semantic_bindings_sha256": digest((supplement_spec or {}).get("semantic_bindings") or {}),
        "supplement_dependencies": supplement_docx.dependency_receipt((supplement_spec or {}).get("semantic_bindings") or {}),
        "supplement_implementation_sha256": phase_b.file_digest(ROOT / 'scripts/supplement_docx.py'),
        "supplement_grid_implementation_sha256": phase_b.file_digest(ROOT / 'scripts/docx_table_grid.py'),
        "supplement_mixed_table_implementation_sha256": phase_b.file_digest(ROOT / 'scripts/supplement_mixed_table.py'),
        "supplement_xrf_cells_implementation_sha256": phase_b.file_digest(ROOT / 'scripts/supplement_xrf_cells.py'),
        "reference_formulation_scope_review_sha256": phase_b.file_digest(ROOT / 'fixtures/reference-formulation-p06-scope-review-v1.json'),
        "marker_geometry_implementation_sha256": phase_b.file_digest(ROOT / 'scripts/marker_geometry.py'),
        "marker_algorithm_identity": __import__('marker_implementation_identity').identity(),
        "marker_formulations_implementation_sha256": phase_b.file_digest(ROOT / 'scripts/marker_formulations.py'),
        "source_mass_precision_implementation_sha256": phase_b.file_digest(ROOT / 'scripts/source_mass_precision.py'),
        "specimen_variants_implementation_sha256": phase_b.file_digest(ROOT / 'scripts/source_specimen_variants.py'),
        "specimen_recipe_fields_implementation_sha256": phase_b.file_digest(ROOT / 'scripts/source_specimen_recipe_fields.py'),
        "quantity_producer_implementation_sha256": phase_b.file_digest(ROOT / 'scripts/source_quantity_producer.py'),
        "formulation_source_plan_implementation_sha256": phase_b.file_digest(ROOT / 'scripts/formulation_source_plan.py'),
        "design_relations_implementation_sha256": phase_b.file_digest(ROOT / 'scripts/source_design_relations.py'),
        "design_condition_cases_implementation_sha256": phase_b.file_digest(ROOT / 'scripts/design_condition_cases.py'),
        "design_relations_config_sha256": digest(rule.get('source_design_stage')),
        "recipe_performance_producer_sha256": phase_b.file_digest(ROOT / 'scripts/source_recipe_performance.py'),
        "recipe_performance_consumer_sha256": phase_b.file_digest(ROOT / 'scripts/recipe_performance_consumer.py'),
        "recipe_performance_bar_sha256": phase_b.file_digest(ROOT / 'scripts/raster_bars.py'),
        "recipe_performance_config_sha256": digest(rule.get('source_recipe_performance_stage')),
        "performance_completion_producer_sha256": phase_b.file_digest(ROOT / 'scripts/source_performance_completion.py'),
        "performance_completion_consumer_sha256": phase_b.file_digest(ROOT / 'scripts/performance_completion_consumer.py'),
        "performance_completion_config_sha256": digest(rule.get('source_performance_completion_stage')),
        "common_recipe_fields_implementation_sha256": phase_b.file_digest(ROOT / 'scripts/source_common_recipe.py'),
        "specimen_curing_implementation_sha256": phase_b.file_digest(ROOT / 'scripts/source_specimen_curing.py'),
        "concrete_context_implementation_sha256": phase_b.file_digest(ROOT / 'scripts/source_concrete_context.py'),
        "canonical_material_implementation_sha256": phase_b.file_digest(ROOT / 'scripts/canonical_materials.py'),
        "transformation_contract_implementation_sha256": phase_b.file_digest(ROOT / 'scripts/transformation_contract.py'),
        "main_marker_implementation_sha256": phase_b.file_digest(ROOT / 'scripts/extract_main_figure_markers.py'),
        "main_marker_context_sha256": phase_b.file_digest(ROOT / 'scripts/main_figure_context.py'),
        "main_marker_source_context_sha256": phase_b.file_digest(ROOT / 'scripts/main_marker_source_context.py'),
        "main_marker_owners_sha256": phase_b.file_digest(ROOT / 'scripts/main_marker_owners.py'),
        "main_marker_merge_sha256": phase_b.file_digest(ROOT / 'scripts/main_marker_merge.py'),
        "main_marker_assets_sha256": phase_b.file_digest(ROOT / 'scripts/main_marker_assets.py'),
        "main_marker_coverage_review_sha256": digest(main_marker_registry.review_receipt()),
        "main_marker_review_chain_sha256": phase_b.file_digest(ROOT / 'scripts/review_chain_preflight.py'),
        "qxrd_basis_review_sha256": digest(basis_receipt()),
        "qxrd_basis_implementation_sha256": phase_b.file_digest(ROOT / 'scripts/qxrd_basis_context.py'),
        "main_marker_merge_acceptance_sha256": digest(main_marker_registry.review_receipt()),
        "main_marker_binding_sha256": digest(main_marker_registry.receipt()),
        "generator_version": GENERATOR_VERSION,
        "material_identity_implementation_sha256": phase_b.file_digest(ROOT / "scripts/source_material_identity.py"),
        "narrative_psd_implementation_sha256": phase_b.file_digest(ROOT / "scripts/source_narrative_psd.py"),
        "fixed_mix_implementation_sha256": phase_b.file_digest(ROOT / "scripts/source_fixed_mix_parameters.py"),
        "prose_performance_implementation_sha256": phase_b.file_digest(ROOT / "scripts/source_performance_candidates.py"),
        "prose_performance_bindings_sha256": phase_b.file_digest(PROSE_PERFORMANCE_PATH),
        "prose_performance_p02_bindings_sha256": phase_b.file_digest(PROSE_PERFORMANCE_P02_PATH),
        "prose_slump_p02_bindings_sha256": phase_b.file_digest(PROSE_SLUMP_P02_PATH),
        "prose_endpoint_p06_bindings_sha256": phase_b.file_digest(PROSE_ENDPOINT_P06_PATH),
        "ingredient_inventory_implementation_sha256": phase_b.file_digest(ROOT / 'scripts/source_ingredient_inventory.py'),
        "ingredient_p01_bindings_sha256": phase_b.file_digest(INGREDIENT_P01_PATH),
        "ingredient_p02_bindings_sha256": phase_b.file_digest(INGREDIENT_P02_PATH),
        "solution_variant_implementation_sha256": phase_b.file_digest(ROOT / 'scripts/source_solution_variants.py'),
        "sparse_pipeline_implementation_sha256": phase_b.file_digest(ROOT / 'scripts/source_sparse_pipeline.py'),
        "sparse_promoter_implementation_sha256": phase_b.file_digest(ROOT / 'scripts/promote_sparse_performance.py'),
        "sparse_geometry_spec_sha256": phase_b.file_digest(ROOT / 'fixtures/sparse-vector-p02-fig3-v1.json'),
        "sparse_context_binding_sha256": phase_b.file_digest(ROOT / 'fixtures/sparse-performance-p02-fig3-binding-v1.json'),
        "sparse_recipe_dependencies": source_sparse.input_receipt(),
        "solution_p02_bindings_sha256": phase_b.file_digest(SOLUTION_P02_PATH),
        "fixed_mix_mapping_sha256": phase_b.file_digest(FIXED_MIX_MAPPING_PATH),
        "aggregate_material_implementation_sha256": phase_b.file_digest(ROOT / 'scripts/source_aggregate_materials.py'),
        "raster_implementation_sha256": phase_b.file_digest(ROOT / "scripts/raster_curves.py"),
        "raster_verifier_sha256": phase_b.file_digest(ROOT / "scripts/verify_raster_curves.py"),
        **raster_routes.input_hashes(ROOT, routing_paper['pdf_sha256']),
        **vector_outline_routes.input_hashes(ROOT, routing_paper['pdf_sha256']),
        "raster_projection_sha256": phase_b.file_digest(ROOT / "scripts/promote_raster_curves.py"),
        "raster_panel_binding_sha256": phase_b.file_digest(ROOT / "scripts/raster_panel_binding.py"),
        "material_identity_acceptance_sha256": phase_b.file_digest(ROOT / "fixtures/material_identity_acceptance_v1.json"),
        "vector_curve_bindings_sha256": digest(vector_curve_bindings.specification()) if routing_paper["pdf_sha256"] == vector_curve_bindings.PDF_SHA else None,
        "vector_overlay_acceptance_sha256": phase_b.file_digest(VECTOR_ACCEPTANCE_PATH) if routing_paper["pdf_sha256"] == vector_curve_bindings.PDF_SHA else None,
    }


def apply_source_design_stage(records, rule, run_dir, runner=None):
    return source_design_relations.production(records,rule.get('source_design_stage'),Path(run_dir)/'source-design-stage',runner)


def apply_source_recipe_performance_stage(records,rule,run_dir):
    return source_recipe_performance.production(records,rule.get('source_recipe_performance_stage'),
                                               Path(run_dir)/'source-recipe-performance-stage')


def apply_source_performance_completion_stage(records,rule,run_dir):
    return performance_completion_consumer.production(records,rule.get('source_performance_completion_stage'),
                                                      Path(run_dir)/'source-performance-completion-stage')


def build_paper(root: Path, output_root: Path, routing: dict[str, Any], rule: dict[str, Any], routing_paper: dict[str, Any], hashes: dict[str, str], cache_root: Path | None, supplement_spec: dict[str, Any] | None = None, plan: dict[str, Any] | None = None, framework: dict[str, Any] | None = None, inventories: list[tuple[Path, dict[str, Any]]] | None = None, semantic_artifact: dict[str, Any] | None = None, figure_decisions: dict[str, Any] | None = None) -> dict[str, Any]:
    run_id = rule["run_id"]
    paper_key, asset_key = f"paper-{run_id[9:]}", f"asset-{run_id[9:]}-pdf"
    pdf = root / routing_paper["pdf_relative_path"]
    source_packets = sorted([packet for packet in routing["packets"] if packet["run_id"] == run_id], key=lambda packet: (packet["table_id"], packet["source_row_index"]))
    if plan is None or framework is None or inventories is None or semantic_artifact is None:
        plan, framework, inventories, semantic_artifact = paper_plan(root, routing_paper, supplement_spec, source_packets, rule, figure_decisions)
    framework_errors = semantic_framework.validate_framework(framework, plan, source_packets)
    if framework_errors:
        raise ValueError(f"semantic framework invalid for {run_id}: {framework_errors}")
    cache_path = cache_root / f"{routing_paper['pdf_sha256']}-words.json" if cache_root else None
    words, cache_hit = phase_b.page_words(pdf, cache_path)
    packets = semantic_framework.route_packets(framework, source_packets)
    table_schema = {table["table_id"]: table for table in routing_paper["tables"]}
    # Reviewed identity rules are checked against the source on every cold build.
    # Keep stable keys so recipe references cannot drift when a source name resolves.
    identity_checks = [check for definition in rule['mats']
                       for check in definition.get('identity_source_checks', [])]
    if identity_checks:
        import fitz
        with fitz.open(pdf) as identity_doc:
            for check in identity_checks:
                text = ' '.join(identity_doc[check['page'] - 1].get_text().split())
                if check['quote'] not in text:
                    raise ValueError('material identity source check failed')
    mats = [mat_record(run_id, paper_key, definition) for definition in rule["mats"]]
    evidence: list[dict[str, Any]] = []
    quarantined: list[dict[str, Any]] = []
    mixes: list[dict[str, Any]] = []
    from table_group_semantics import foaming_agent_from_identity
    for packet in packets:
        table_rule = rule["tables"][packet["table_id"]]
        units = {column["key"]: column.get("unit") for column in table_schema[packet["table_id"]]["columns"]}
        labels = {column["key"]: column.get("label") for column in table_schema[packet["table_id"]]["columns"]}
        parameter_keys = {column["key"]: column.get("parameter_keys") or [] for column in table_schema[packet["table_id"]]["columns"]}
        identity, identity_tokens, identity_method = resolve_source_identity(table_rule, packet)
        mix_key = f"mix-{run_id[9:]}-{packet['table_id']}-r{packet['source_row_index']}"
        mix = empty_mix(run_id, paper_key, mix_key, identity, packet["table_id"], packet["source_row_index"])
        if identity_tokens and identity_method:
            identity_path = "/modules/identity_source_specimen/custom_test_id"
            identity_link = evidence_link(run_id, paper_key, asset_key, mix_key, "mix", identity_path,
                                          packet["page"], packet["table_id"], identity_tokens[0], identity_method, 0)
            evidence.append(identity_link)
            mix["field_provenance"][identity_path] = provenance(identity_link, identity_tokens[0], None)
        material_source_token: dict[str, Any] | None = None
        ranges = phase_b.routing_column_ranges(routing_paper, packet["table_id"])
        refs: list[str] = []
        for column in table_rule.get("material_columns", []):
            tokens = phase_b.cell_tokens(packet, column)
            if not tokens and any(c["key"]==column and c.get("carry_forward") for c in table_schema[packet["table_id"]]["columns"]):
                tokens=phase_b.shared_cell_tokens(packet,column,packets)
            if not tokens and column in table_rule.get("dash_recovery_columns", []):
                recovered = phase_b.recover_dash(packet, column, words, ranges)
                tokens = [recovered] if recovered else []
            semantic = table_rule.get("parameter_semantics", {}).get(column)
            # A numeric source column can contain a contextual text token plus
            # one number (for example "SS 15").  The formal numeric parameter
            # is the number.  A unitless multi-word column is instead one
            # composite source value, not many duplicate parameters.
            if units.get(column) is not None and any(token.get("typed_state") == "number" for token in tokens):
                tokens = [token for token in tokens if token.get("typed_state") in {"number", "null"}]
            elif len(tokens) > 1 and semantic is None:
                tokens = [composite_cell_token(tokens)]
            expanded: list[tuple[str, str, dict[str, Any]]] = []
            for token in tokens:
                if semantic == "split_alkali_ratio" and ":" in str(token["token"]):
                    left, right = str(token["token"]).split(":", 1)
                    parts = [left, right]
                    for component_index, (name, raw) in enumerate((("x2o_to_sio2", left), ("h2o_to_x2o", right))):
                        derived = dict(token); derived["typed"] = float(raw); derived["typed_state"] = "number"
                        derived["transformation"] = ratio_component_transformation(name, str(token["token"]), parts, component_index)
                        expanded.append((name, name, derived))
                elif semantic == "split_ratio" and ":" in str(token["token"]):
                    parts = str(token["token"]).split(":")
                    keys = parameter_keys.get(column) or []
                    if len(parts) != len(keys) or not keys:
                        raise ValueError(f"ratio component contract mismatch: {run_id}:{packet['table_id']}:{column}")
                    for component_index, (name, raw) in enumerate(zip(keys, parts)):
                        derived = dict(token); derived["typed"] = float(raw); derived["typed_state"] = "number"
                        derived["transformation"] = ratio_component_transformation(name, str(token["token"]), parts, component_index)
                        expanded.append((name, name, derived))
                elif semantic == "foaming_agent_mass_percent":
                    foamer = foaming_agent_from_identity(identity)
                    name = "h2o2_mass_percent" if foamer == "h2o2" else "spc_mass_percent" if foamer == "spc" else "foaming_agent_mass_percent"
                    expanded.append((name, name, token))
                elif semantic == "canonical_parameter_key":
                    keys = parameter_keys.get(column) or []
                    if len(keys) != 1:
                        raise ValueError(f"canonical parameter contract mismatch: {run_id}:{packet['table_id']}:{column}")
                    expanded.append((str(keys[0]), str(keys[0]), token))
                else:
                    source_label = labels.get(column) or column
                    expanded.append((source_label, source_label, token))
            for ordinal, (parameter_key, original_label, token) in enumerate(expanded, 1):
                existing_keys = {
                    str(item.get("parameter_key"))
                    for item in mix["modules"]["materials"]["extensions"]["reported_parameters"]
                }
                if parameter_key in existing_keys:
                    base_key = f"{parameter_key}_{column}"
                    parameter_key = base_key
                    suffix = 2
                    while parameter_key in existing_keys:
                        parameter_key = f"{base_key}_{suffix}"
                        suffix += 1
                index = len(mix["modules"]["materials"]["extensions"]["reported_parameters"])
                field_path = f"/modules/materials/extensions/reported_parameters/{index}/value"
                method = "pymupdf_words_dash_recovery_v1" if token.get("typed_state") == "null" else packet["extraction_method"]
                link = evidence_link(run_id, paper_key, asset_key, mix_key, "mix", field_path, packet["page"], packet["table_id"], token, method, ordinal)
                evidence.append(link)
                mix["modules"]["materials"]["extensions"]["reported_parameters"].append(parameter(parameter_key, original_label, units.get(column), token, link))
                if token.get("typed_state") == "null" and column in table_rule.get("ambiguous_null_columns", {}):
                    quarantined.append({
                        "type": "semantic_mismatch", "required": True, "record_key": mix_key, "field_path": field_path,
                        "parameter": parameter_key, "source_value": None, "source_token": str(token["token"]),
                        "reason": table_rule["ambiguous_null_columns"][column], "evidence_key": link["evidence_key"],
                        "resolution": "retain_source_null_never_coerce_to_zero",
                    })
                material_source_token = material_source_token or token
            ref = table_rule.get("mat_reference_columns", {}).get(column)
            if ref and any(token.get("typed_state") == "number" and float(token.get("typed") or 0) > 0 for token in tokens):
                refs.append(f"mat-{run_id[9:]}-{ref}")
        refs.extend(f"mat-{run_id[9:]}-{key}" for key in table_rule.get("default_mat_refs", []))
        source_text = " ".join(str(token["token"]) for token in packet["observed_cell_tokens"])
        for mat_key, terms in table_rule.get("mat_reference_text_terms", {}).items():
            if all(re.search(rf"\b{re.escape(term)}\b", source_text, re.IGNORECASE) for term in terms):
                refs.append(f"mat-{run_id[9:]}-{mat_key}")
        if not refs:
            if len(mats) == 1:
                refs = [mats[0]["mat_key"]]
            elif not table_rule.get("material_columns"):
                refs = [mat["mat_key"] for mat in mats]
            elif packet["table_id"] == "table-2" and run_id.endswith("7717e4cb8387"):
                factor = next((str(token["token"]).casefold() for token in phase_b.cell_tokens(packet, "factor")), "")
                refs = [mat["mat_key"] for mat in mats if mat["mat_key"].endswith("-ggbs") or factor and mat["mat_key"].endswith("-" + factor)]
        mix["modules"]["materials"]["mat_refs"] = sorted(set(refs))
        if refs:
            material_source_token = material_source_token or (identity_tokens[0] if identity_tokens else None)
            if material_source_token:
                summary_link = evidence_link(run_id, paper_key, asset_key, mix_key, "mix", "/modules/materials", packet["page"], packet["table_id"], material_source_token, packet["extraction_method"], 0)
                evidence.append(summary_link)
                mix["field_provenance"]["/modules/materials"] = provenance(summary_link, material_source_token, None)
        if run_id.endswith("55c422590f17"):
            observed_columns = {cell["column_key"] for cell in packet["cells"]}
            excluded_columns = set(table_rule["identity_columns"]) | set(table_rule["material_columns"])
            # Preserve the frozen source-table column order.  Iterating a set
            # made performance indices and evidence paths vary with Python's
            # per-process hash seed even when every input byte was identical.
            observation_columns = [
                column["key"]
                for column in table_schema[packet["table_id"]]["columns"]
                if column["key"] in observed_columns and column["key"] not in excluded_columns
            ]
            for column in observation_columns:
                for ordinal, token in enumerate(phase_b.cell_tokens(packet, column), 1):
                    if token.get("typed_state") != "number":
                        continue
                    center = (token["bbox"][0] + token["bbox"][2]) / 2
                    band = next((band for band in table_rule["observation_x_bands"] if band[0] <= center < band[1]), None)
                    if band is None:
                        continue
                    semantics = p55_semantics(words_in_band(words[packet["page"]], band[0], band[1], *table_rule["header_band"]))
                    if semantics["name"].startswith("quarantined"):
                        field_path = f"/quarantined/p55/{packet['table_id']}/{packet['source_row_index']}/{column}/{ordinal}"
                        link = evidence_link(run_id, paper_key, asset_key, mix_key, "mix", field_path, packet["page"], packet["table_id"], token, packet["extraction_method"], ordinal)
                        evidence.append(link)
                        quarantined.append({"record_key": mix_key, "field_path": field_path, "formal_value": None, "proposed_value": token["typed"], "source_unit": semantics["source_unit"], "reason": "source header reports viscosity in dimensionally ambiguous Pa; do not infer Pa·s", "evidence_key": link["evidence_key"]})
                        continue
                    index = len(mix["modules"]["performance"])
                    field_path = f"/modules/performance/{index}/value"
                    link = evidence_link(run_id, paper_key, asset_key, mix_key, "mix", field_path, packet["page"], packet["table_id"], token, packet["extraction_method"], ordinal)
                    evidence.append(link)
                    canonical_value = float(token["typed"]) * 1000.0 if semantics["source_unit"] == "m2/g" else token["typed"]
                    formula = "m2/kg = m2/g * 1000" if semantics["source_unit"] == "m2/g" else None
                    transformation = scale_transformation(float(token["typed"]), semantics["source_unit"], float(canonical_value), semantics["unit"], 1000.0, formula) if formula else None
                    observation = {"name": semantics["name"], "value": canonical_value, "unit": semantics["unit"], "age_seconds": None, "specimen": semantics["specimen"], "method": "source table", "extensions": {"original_value": str(token["token"]), "original_unit": semantics["source_unit"], "formula": formula, "transformation": transformation, "source_header": semantics["source_header"], "reported_conditions": semantics["reported_conditions"], "evidence_key": link["evidence_key"], "extraction_method": link["extraction_method"], "confidence": link["confidence"]}}
                    mix["modules"]["performance"].append(observation)
                    mix["field_provenance"][field_path] = provenance(link, token, semantics["source_unit"])
                    mix["field_provenance"][field_path]["formula"] = formula
                    if transformation:
                        mix["field_provenance"][field_path]["transformation"] = transformation
        mixes.append(mix)
    apply_curing_profiles(run_id, paper_key, asset_key, rule, words, mixes, evidence)
    if rule.get("extra_observation_tables"):
        add_extra_observations(run_id, paper_key, asset_key, rule, words, mixes, evidence)
    if rule.get("text_observations"):
        add_text_observations(run_id, paper_key, asset_key, rule, words, mixes, evidence)
    if rule.get("row_observation_tables"):
        add_row_observations(run_id, paper_key, asset_key, rule, words, mixes, evidence)
    records = {
        "schema_version": "1.0", "platform_contract": "WAKG-V1.1.4",
        "papers": [{"paper_key": paper_key, "title": None, "doi": None, "year": None, "citation": None, "extensions": {"pdf_sha256": routing_paper["pdf_sha256"]}}],
        "mats": mats, "mixes": mixes,
        "assets": [{"asset_key": asset_key, "paper_key": paper_key, "kind": "pdf", "relative_path": str(pdf.resolve()), "sha256": routing_paper["pdf_sha256"], "mime_type": "application/pdf", "extensions": {}}],
        "evidence_links": evidence, "candidates": [], "quarantined": quarantined,
        "extensions": {"generation": GENERATOR_VERSION, "input_hashes": paper_input_hashes(routing_paper, source_packets, rule, supplement_spec, plan, framework, semantic_artifact), "model_calls": 0, "token_proxy": 0},
    }
    run_dir = output_root / run_id
    records = multimodal.enrich_records(records, plan, run_dir / "main-figure-assets")
    # Seal the framework-owned base extraction before supplements enrich,
    # replace aliases, or add rows.  A downstream supplement transform must
    # preserve this receipt rather than trying to reconstruct the upstream
    # routing denominator from its rewritten source-table identities.
    framework_binding = semantic_framework.bind_records(framework, records)
    records.setdefault("extensions", {})["semantic_framework"] = framework_binding
    if framework_binding["verdict"] != "PASS":
        raise ValueError(f"semantic framework record binding blocked for {run_id}: {framework_binding['failures']}")
    if inventories:
        if len(inventories) != 1:
            raise ValueError(f"only one DOCX supplement is currently supported per paper: {run_id}")
        supplement_path, inventory = inventories[0]
        records = supplement_docx.enrich_records_with_supplement(records, plan, supplement_path, inventory, (supplement_spec or {}).get("semantic_bindings") or {}, run_dir / "supplement-assets")
        if (records.get("extensions") or {}).get("semantic_framework") != framework_binding:
            raise ValueError(f"supplement enrichment mutated framework receipt for {run_id}")
    source_bibliography.enrich(records, str(pdf), asset_key)
    source_material_identity.enrich(records, str(pdf), asset_key)
    source_material_identity.enrich_types(records, str(pdf), asset_key)
    source_material_identity.promote_reviewed_types(records, str(pdf), asset_key, ROOT / "fixtures/material_identity_acceptance_v1.json")
    source_narrative_psd.enrich(records, str(pdf), asset_key, source_material_identity.material_scopes)
    source_aggregate_materials.enrich(records,str(pdf),asset_key,source_material_identity.material_scopes,multimodal,
                                    lambda definition:mat_record(run_id,paper_key,definition))
    source_bibliography.enrich(records,str(pdf),asset_key)
    if main_marker_registry.select(records,run_dir) is not None:
        source_common_recipe.enrich(records)
        source_specimen_curing.enrich(records)
    canonical_materials.enrich(records)
    for ingredient_path in (INGREDIENT_P01_PATH, INGREDIENT_P02_PATH):
        ingredient_spec=json.loads(ingredient_path.read_text(encoding='utf-8'))
        if routing_paper['pdf_sha256']==ingredient_spec['pdf_sha256']:
            ingredient_inventory.enrich(records,str(pdf),ingredient_spec,
                                        lambda definition:mat_record(run_id,paper_key,definition))
            source_bibliography.enrich(records,str(pdf),asset_key)
    solution_spec=json.loads(SOLUTION_P02_PATH.read_text(encoding='utf-8'))
    if routing_paper['pdf_sha256']==solution_spec['pdf_sha256']:
        solution_variants.enrich(records,str(pdf),solution_spec,
                                lambda definition:mat_record(run_id,paper_key,definition))
        source_bibliography.enrich(records,str(pdf),asset_key)
    fixed_mapping=json.loads(FIXED_MIX_MAPPING_PATH.read_text(encoding='utf-8'))
    if routing_paper['pdf_sha256']==fixed_mapping['pdf_sha256']:
        source_fixed_mix.enrich(records,str(pdf),asset_key,fixed_mapping)
    for prose_path in (PROSE_PERFORMANCE_PATH, PROSE_PERFORMANCE_P02_PATH, PROSE_SLUMP_P02_PATH, PROSE_ENDPOINT_P06_PATH):
        prose_binding=json.loads(prose_path.read_text(encoding='utf-8'))
        if routing_paper['pdf_sha256']==prose_binding['pdf_sha256']:
            source_performance.enrich(records,str(pdf),prose_binding)
    source_sparse.enrich(records,pdf,run_dir)
    source_sparse.export_assets(records,run_dir)
    main_marker_config=main_marker_registry.select(records,run_dir)
    if main_marker_config is not None:
        main_marker_binding=main_marker_config['binding']
        # Source-only in-memory production path. Never reopen an earlier
        # generated-records.json to recover these observations.
        main_marker_report=main_markers.extract_records(run_dir,records,main_marker_binding)
        main_marker_report['source_context']=main_marker_source_context.bind(records,main_marker_report)
        main_marker_report['observation_owners']=main_marker_owners.bind(records,main_marker_report)
        merge_plan=main_marker_merge.prepare(records,run_dir,main_marker_report)
        review_pair=main_marker_registry.select_review(merge_plan)
        merge_acceptance=review_pair['acceptance'] if review_pair else {}
        # Preserve the exact review subject before the fail-closed gate.  A
        # rejected receipt must not erase the input needed by automatic repair.
        atomic_json(run_dir/'pending-main-figure-merge-plan.json', merge_plan)
        atomic_json(run_dir/'pending-main-figure-marker-report.json', main_marker_report)
        if merge_acceptance.get('plan_sha256') != digest(merge_plan):
            atomic_json(run_dir/'pending-main-figure-merge-review.json', {
                'status':'REQUIRES_INPUT_BOUND_AGENT_REVIEW', 'run_id':run_id,
                'plan_path':'pending-main-figure-merge-plan.json',
                'plan_sha256':digest(merge_plan),
                'previous_plan_sha256':merge_acceptance.get('plan_sha256'),
                'formal_publication_authorized':False})
        from review_chain_preflight import inspect as inspect_review_chain
        coverage = review_pair['coverage'] if review_pair else {}
        chain = inspect_review_chain(merge_plan, merge_acceptance, coverage,
            phase_b.file_digest(Path(main_marker_merge.__file__)))
        atomic_json(run_dir/'pending-main-figure-review-chain.json', chain)
        if chain['defects'] and figure_decisions is not None:
            raise ValueError('main marker review chain: ' + ', '.join(chain['defects']))
        if chain['defects']:
            records.setdefault('extensions',{})['pending_main_figure_merge']={
                'status':'REQUIRES_INPUT_BOUND_AGENT_REVIEW',
                'plan':merge_plan,'plan_sha256':digest(merge_plan),
                'review_chain':chain,'formal_publication_authorized':False}
        else:
            main_marker_merge.project(records,run_dir,main_marker_report,merge_plan,merge_acceptance)
            # Only an exact independently reviewed plan can create formal values.
            records['extensions']['main_figure_merge']['plan']=merge_plan
            records['extensions']['main_figure_merge']['acceptance']=merge_acceptance
            main_marker_assets.export_assets(records,run_dir)
        main_marker_path=run_dir/'main-marker-assets/candidates.json'
        atomic_json(main_marker_path,main_marker_report)
        records.setdefault('extensions',{})['main_marker_candidates']={
            'path':'main-marker-assets/candidates.json',
            'sha256':phase_b.file_digest(main_marker_path),
            'observation_count':main_marker_report['observation_count'],
            'scope':main_marker_report['scope'],'formal_values_allowed':False}
    raster_routes.execute(ROOT, routing_paper['pdf_sha256'], records, pdf, run_dir,
                          raster_curves, promote_raster_curves, raster_panel_binding)
    vector_outline_routes.execute(ROOT, routing_paper['pdf_sha256'], records, pdf, run_dir)
    if routing_paper["pdf_sha256"] == vector_curve_bindings.PDF_SHA:
        vector_curves.enrich_candidates(records, pdf, vector_curve_bindings.specification(), run_dir)
        vector_proof = verify_vector_curves.verify_payload(records, run_dir, vector_curve_bindings.specification())
        if vector_proof["verdict"] != "PASS":
            raise ValueError(f"vector source proof rejected: {vector_proof['defects']}")
        atomic_json(run_dir / "vector-assets/source-proof.json", vector_proof)
        records["extensions"]["vector_curve_source_proof"] = {"path": "vector-assets/source-proof.json", "sha256": phase_b.file_digest(run_dir / "vector-assets/source-proof.json"), "scope": vector_proof["scope"]}
        vector_acceptance = json.loads(VECTOR_ACCEPTANCE_PATH.read_text(encoding="utf-8"))
        promote_vector_curves.promote(records, vector_curve_bindings.specification(), vector_proof, vector_acceptance)
    design_stage_report = apply_source_design_stage(records, rule, run_dir)
    recipe_performance_report = apply_source_recipe_performance_stage(records,rule,run_dir)
    performance_completion_report = apply_source_performance_completion_stage(records,rule,run_dir)
    multimodal_binding = semantic_framework.assess_multimodal_records(framework, records)
    records.setdefault("extensions", {})["semantic_framework_multimodal"] = multimodal_binding
    figure_quarantines = semantic_framework.figure_digitization_quarantines(multimodal_binding)
    records.setdefault("quarantined", []).extend(figure_quarantines)
    records["extensions"]["figure_digitization_quarantine_sha256"] = digest(figure_quarantines)
    completeness = multimodal.validate_completeness(plan, records)
    if records.get('extensions',{}).get('pending_main_figure_merge'):
        completeness.setdefault('failures',[]).append('MAIN_FIGURE_MERGE_PENDING_REVIEW')
        completeness['verdict']='BLOCKED'
    completeness["semantic_framework_sha256"] = framework["framework_sha256"]
    completeness["semantic_framework_verdict"] = framework_binding["verdict"]
    completeness["figure_numeric_gate"] = multimodal_binding
    transformation_report = transformation_contract.validate_records(records)
    completeness["transformation_contract"] = transformation_report
    if transformation_report["verdict"] != "PASS":
        completeness.setdefault("failures", []).append(f"TRANSFORMATION_CONTRACT_REJECTED:{len(transformation_report['defects'])}")
        completeness["verdict"] = "BLOCKED"
    if multimodal_binding["verdict"] != "PASS":
        completeness.setdefault("failures", []).append(f"NUMERIC_FIGURE_GATE_BLOCKED:{multimodal_binding['blocking_figure_count']}")
        completeness["verdict"] = "BLOCKED"
    semantic_markdown.atomic_text(run_dir / "source-semantic.md", semantic_artifact["text"])
    atomic_json(run_dir / "source-semantic-manifest.json", semantic_artifact["metadata"])
    atomic_json(run_dir / "source-relation-input.json", semantic_artifact["relation_input"])
    atomic_json(run_dir / "extraction-plan.json", plan)
    atomic_json(run_dir / "paper-semantic-framework.json", framework)
    atomic_json(run_dir / "transformation-report.json", transformation_report)
    atomic_json(run_dir / "completeness-report.json", completeness)
    atomic_json(run_dir / "generated-records.json", records)
    manifest = {"run_id": run_id, "records_sha256": digest(records), "input_hashes": records["extensions"]["input_hashes"], "extraction_plan_sha256": plan["plan_sha256"], "transformation_report_sha256": digest(transformation_report), "completeness_sha256": digest(completeness), "completeness_verdict": completeness["verdict"], "counts": {"mats": len(records["mats"]), "mixes": len(records["mixes"]), "observations": sum(len(mix["modules"]["performance"]) + len(mix["modules"]["characterizations"]) for mix in records["mixes"]), "evidence_links": len(records["evidence_links"]), "quarantined": len(records["quarantined"])}, "cache_hit": cache_hit, "model_calls": 0, "token_proxy": 0}
    manifest['sparse_artifacts']=source_sparse.artifact_receipt(records,run_dir)
    manifest['source_design_stage']=design_stage_report
    manifest['source_recipe_performance_stage']=recipe_performance_report
    manifest['source_performance_completion_stage']=performance_completion_report
    manifest['model_calls']+=design_stage_report.get('model_calls',0)
    manifest['token_proxy']+=design_stage_report.get('token_proxy',0)
    manifest['source_design_artifacts']=source_design_relations.artifact_receipt(records,run_dir)
    manifest['raster_dot_artifacts']=raster_dot_routes.artifact_receipt(records,run_dir)
    manifest['vector_outline_artifacts']=vector_outline_routes.artifact_receipt(records,run_dir)
    manifest['main_marker_artifacts']=main_markers.artifact_receipt(records,run_dir)
    manifest['main_marker_data_artifacts']=main_marker_assets.artifact_receipt(records,run_dir)
    atomic_json(run_dir / "evidence-manifest.json", manifest)
    return manifest


def add_extra_observations(run_id: str, paper_key: str, asset_key: str, rule: dict[str, Any], words: dict[int, list[tuple[Any, ...]]], mixes: list[dict[str, Any]], evidence: list[dict[str, Any]]) -> None:
    by_source = {(mix["extensions"]["source_table"], mix["extensions"]["source_row"]): mix for mix in mixes}
    for table in rule["extra_observation_tables"]:
        context_token = table_context_token(words, table)
        method_token = method_context_token(words, table)
        header_lines = phase_b.line_groups(words[table["page"]], *table["header_y"])
        row_lines = phase_b.line_groups(words[table["page"]], *table["row_y"])
        if table.get("entity_headers_regex"):
            headers = [word for line in header_lines for word in line if re.match(table["entity_headers_regex"], str(word[4]))]
            targets = sorted((mix for (table_id, _), mix in by_source.items() if table_id == table["target_table"]), key=lambda mix: mix["extensions"]["source_row"])
            for measurement in table["measurement_rows"]:
                line = next(line for line in row_lines if re.search(measurement["label_regex"], " ".join(str(word[4]) for word in line if word[0] < 125)))
                for ordinal, (header, mix) in enumerate(zip(headers, targets), 1):
                    word = min((word for word in line if word[0] >= 125), key=lambda word: abs(word[0] - header[0]))
                    specimen = str(table.get("specimen_template") or "").format(id=mix["modules"]["identity_source_specimen"]["custom_test_id"]) or None
                    add_observation(run_id, paper_key, asset_key, mix, evidence, table["page"], table["table_id"], measurement, phase_b.token_from_word(word), ordinal, specimen=specimen, method=table.get("method"), age_status=table.get("age_status"), age_reason=table.get("age_reason"), context_token=context_token, method_token=method_token, method_page=table.get("method_context_page"), reported_conditions=table.get("reported_conditions"))
        else:
            header = next(word for line in header_lines for word in line if str(word[4]) == table["select_header"])
            mix = by_source[(table["target_table"], table["target_source_row"])]
            for ordinal, measurement in enumerate(table["measurement_rows"], 1):
                line = next(line for line in row_lines if re.search(measurement["label_regex"], " ".join(str(word[4]) for word in line if word[0] < 150)))
                word = min((word for word in line if word[0] >= 150), key=lambda word: abs(word[0] - header[0]))
                specimen = str(table.get("specimen_template") or "").format(id=mix["modules"]["identity_source_specimen"]["custom_test_id"]) or None
                add_observation(run_id, paper_key, asset_key, mix, evidence, table["page"], table["table_id"], measurement, phase_b.token_from_word(word), ordinal, specimen=specimen, method=table.get("method"), age_status=table.get("age_status"), age_reason=table.get("age_reason"), context_token=context_token, method_token=method_token, method_page=table.get("method_context_page"), reported_conditions=table.get("reported_conditions"))


def add_observation(run_id: str, paper_key: str, asset_key: str, mix: dict[str, Any], evidence: list[dict[str, Any]], page: int, table: str, measurement: dict[str, Any], token: dict[str, Any], ordinal: int, *, specimen: str | None = None, method: str | None = None, age_status: str | None = None, age_reason: str | None = None, context_token: dict[str, Any] | None = None, method_token: dict[str, Any] | None = None, method_page: int | None = None, reported_conditions: Any = None) -> None:
    index = len(mix["modules"]["performance"])
    field_path = f"/modules/performance/{index}/value"
    link = evidence_link(run_id, paper_key, asset_key, mix["mix_key"], "mix", field_path, page, table, token, "pymupdf_words_header_aligned_v1", ordinal)
    evidence.append(link)
    extensions = {"original_value": str(token["token"]), "original_unit": measurement["unit"], "evidence_key": link["evidence_key"], "extraction_method": link["extraction_method"], "confidence": link["confidence"]}
    if age_status: extensions["age_status"] = age_status
    if age_reason: extensions["age_reason"] = age_reason
    if reported_conditions is not None: extensions["reported_conditions"] = reported_conditions
    mix["modules"]["performance"].append({"name": measurement["kind"], "value": token["typed"], "unit": measurement["unit"], "age_seconds": None, "specimen": specimen, "method": method or "source table", "extensions": extensions})
    mix["field_provenance"][field_path] = provenance(link, token, measurement["unit"])
    if context_token is not None:
        context_fields = [("specimen", specimen, "specimen = source table context plus paper-reported row identity"), ("method", method, "method = source table or method-section context"), ("age_seconds", None if age_status else "", None)]
        for offset, (field_name, field_value, formula) in enumerate(context_fields, 1):
            if field_value is None and field_name != "age_seconds": continue
            if field_name == "age_seconds" and not age_status: continue
            context_path = f"/modules/performance/{index}/{field_name}"
            selected_token = method_token if field_name == "method" and method_token is not None else context_token
            selected_page = int(method_page) if field_name == "method" and method_token is not None and method_page is not None else page
            selected_table = "method-section" if field_name == "method" and method_token is not None else table
            context_link = evidence_link(run_id, paper_key, asset_key, mix["mix_key"], "mix", context_path, selected_page, selected_table, selected_token, "pymupdf_words_observation_context_v1", ordinal * 10 + offset)
            evidence.append(context_link)
            context_provenance = provenance(context_link, context_token, None)
            context_provenance["formula"] = formula
            mix["field_provenance"][context_path] = context_provenance


def add_text_observations(run_id: str, paper_key: str, asset_key: str, rule: dict[str, Any], words: dict[int, list[tuple[Any, ...]]], mixes: list[dict[str, Any]], evidence: list[dict[str, Any]]) -> None:
    by_source = {(mix["extensions"]["source_table"], mix["extensions"]["source_row"]): mix for mix in mixes}
    word_numbers = {"one": 1, "two": 2, "three": 3, "seven": 7, "fourteen": 14, "twenty-eight": 28}
    for locator in rule["text_observations"]:
        page_words = words[locator["page"]]
        phrase,value_text,value_word=source_performance.locate(page_words,locator['line_regex'],locator['value_regex'])
        token = phase_b.token_from_word(value_word)
        mix = by_source[(locator["target_table"], locator["target_source_row"])]
        index = len(mix["modules"]["performance"])
        field_path = f"/modules/performance/{index}/value"
        link = evidence_link(run_id, paper_key, asset_key, mix["mix_key"], "mix", field_path, locator["page"], "source-text", token, "pymupdf_words_regex_v1", 1)
        evidence.append(link)
        age_match = re.search(locator["age_regex"], phrase, re.IGNORECASE)
        age_word = age_match.group(1).casefold() if age_match else None
        age_seconds = float(word_numbers[age_word] * 86400) if age_word in word_numbers else None
        # A specimen identifier is not a specimen type, and extraction from
        # prose is not a scientific testing method. Missing context must stay
        # null until an independently sourced context binding supplies it.
        observation = {"name": locator["kind"], "value": float(value_text), "unit": locator["unit"], "age_seconds": age_seconds, "specimen": mix["modules"]["identity_source_specimen"].get("specimen_type"), "method": None, "extensions": {"original_value": value_text, "original_unit": locator["unit"], "original_age_expression": age_match.group(0) if age_match else None, "evidence_key": link["evidence_key"], "extraction_method": link["extraction_method"], "confidence": link["confidence"]}}
        mix["modules"]["performance"].append(observation)
        mix["field_provenance"][field_path] = provenance(link, token, locator["unit"])
        if age_seconds is not None:
            _,_,age_token_word=source_performance.locate(page_words,locator['line_regex'],locator['age_regex'])
            age_token = phase_b.token_from_word(age_token_word)
            age_path = f"/modules/performance/{index}/age_seconds"
            age_link = evidence_link(run_id, paper_key, asset_key, mix["mix_key"], "mix", age_path, locator["page"], "source-text", age_token, "pymupdf_words_regex_day_to_seconds_v1", 2)
            evidence.append(age_link)
            age_prov = provenance(age_link, age_token, "day")
            age_prov["formula"] = "seconds = days * 86400"
            age_prov["transformation"] = scale_transformation(float(word_numbers[age_word]), "day", float(age_seconds), "s", 86400.0, age_prov["formula"])
            mix["field_provenance"][age_path] = age_prov


def add_row_observations(run_id: str, paper_key: str, asset_key: str, rule: dict[str, Any], words: dict[int, list[tuple[Any, ...]]], mixes: list[dict[str, Any]], evidence: list[dict[str, Any]]) -> None:
    by_table: dict[str, list[dict[str, Any]]] = {}
    for mix in mixes:
        by_table.setdefault(mix["extensions"]["source_table"], []).append(mix)
    for locator in rule["row_observation_tables"]:
        context_token = table_context_token(words, locator)
        method_token = method_context_token(words, locator)
        lines = phase_b.line_groups(words[locator["page"]], *locator["row_band"], tolerance=2.2)
        source_rows = []
        for line in lines:
            identity_words = [word for word in line if locator["identity_x"][0] <= (word[0] + word[2]) / 2 < locator["identity_x"][1]]
            values = []
            for measurement in locator["measurements"]:
                candidates = [word for word in line if measurement["x"][0] <= (word[0] + word[2]) / 2 < measurement["x"][1]]
                values.append(candidates[0] if candidates else None)
            if identity_words and all(value is not None for value in values):
                source_rows.append((identity_words[0], values))
        targets = sorted(by_table[locator["target_table"]], key=lambda mix: mix["extensions"]["source_row"])
        if len(source_rows) != len(targets):
            raise ValueError(f"row observation alignment failed for {run_id}: {len(source_rows)} != {len(targets)}")
        for ordinal, (mix, (identity_word, values)) in enumerate(zip(targets, source_rows), 1):
            mix["modules"]["identity_source_specimen"]["custom_test_id"] = str(identity_word[4])
            condition = next((item.get("original_value") for item in mix["modules"]["materials"]["extensions"]["reported_parameters"] if item.get("original_label") == locator.get("condition_from_parameter")), None)
            specimen = str(locator.get("specimen_template") or "").format(id=str(identity_word[4])) or None
            for measurement, word in zip(locator["measurements"], values):
                add_observation(run_id, paper_key, asset_key, mix, evidence, locator["page"], locator["table_id"], measurement, phase_b.token_from_word(word), ordinal, specimen=specimen, method=locator.get("method"), age_status=locator.get("age_status"), age_reason=locator.get("age_reason"), context_token=context_token, method_token=method_token, method_page=locator.get("method_context_page"), reported_conditions=condition)


def build(root: Path, output_root: Path, routing_path: Path, rules_path: Path, cache_root: Path | None, fail_after: int | None = None, figure_decisions_path: Path | None = None, *, keep_going: bool = False, fault_campaign: str | None = None) -> dict[str, Any]:
    if keep_going and not fault_campaign:
        raise ValueError('keep-going regression requires an explicit fault campaign')
    if keep_going:
        output_root.resolve().relative_to(root.resolve())
    started = time.perf_counter()
    routing, rules, support = load_inputs(root, routing_path, rules_path)
    figure_decisions = None
    if figure_decisions_path is not None:
        decision_path = figure_decisions_path if figure_decisions_path.is_absolute() else root / figure_decisions_path
        figure_decisions = json.loads(decision_path.read_text(encoding="utf-8"))
    summaries = []
    for index, rule in enumerate(rules["papers"], 1):
        run_id = rule["run_id"]
        try:
            summaries.append(build_batch_item(root,output_root,routing,rule,support,cache_root,figure_decisions))
        except Exception as exc:
            if not keep_going:raise
            fingerprint='SOURCE_BATCH_'+type(exc).__name__.upper()+'_'+digest(str(exc))[:16]
            failure={'run_id':run_id,'status':'BUILD_FAILED','error_type':type(exc).__name__,
                'error':str(exc),'fingerprint':fingerprint,'records_sha256':None,
                'completeness_verdict':'BLOCKED','counts':{k:0 for k in ('mats','mixes','observations','evidence_links','quarantined')},
                'count_scope':'no_completed_output_counted_not_absence_of_paper_data'}
            failure_path=output_root/run_id/('build-failure-'+fingerprint+'.json')
            atomic_json(failure_path,failure)
            event=pipeline.record_fault_event(root,fault_campaign,fingerprint,'OBSERVED','SOFTWARE','high',
                'Per-paper source regression failed; remaining independent papers continue',
                symptom=str(exc),evidence_paths=[str(failure_path.relative_to(root))],related_run_ids=[run_id])
            failure['fault_id']=event['event']['fault_id']
            atomic_json(failure_path,failure)
            summaries.append(failure)
        if fail_after == index:
            raise RuntimeError("injected Phase-C interruption")
    counts = {key: sum(item["counts"][key] for item in summaries) for key in ("mats", "mixes", "observations", "evidence_links", "quarantined")}
    summary = {"schema_version": 1, "papers": summaries, "counts": counts, "semantic_hash": digest({item["run_id"]: item["records_sha256"] for item in summaries}), "completeness": {"pass": sum(item["completeness_verdict"] == "PASS" for item in summaries), "blocked": sum(item["completeness_verdict"] != "PASS" for item in summaries), "verdict": "PASS" if all(item["completeness_verdict"] == "PASS" for item in summaries) else "BLOCKED"}, "runtime_seconds": round(time.perf_counter() - started, 6), "model_calls": 0, "token_proxy": 0, "network_calls": 0, "wakg_calls": 0}
    summary['execution']={'failed':sum(i.get('status')=='BUILD_FAILED' for i in summaries),'completed':sum(i.get('status')!='BUILD_FAILED' for i in summaries)}
    atomic_json(output_root / "build-summary.json", summary)
    return summary


def build_batch_item(root,output_root,routing,rule,support,cache_root,figure_decisions):
        """One isolated paper operation, including resume and artifact checks."""
        run_id=rule['run_id']
        packets = [packet for packet in routing["packets"] if packet["run_id"] == run_id]
        supplement_spec = support["supplements"].get(run_id)
        plan, framework, inventories, semantic_artifact = paper_plan(root, support["schemas"][run_id], supplement_spec, packets, rule, figure_decisions)
        expected_inputs = paper_input_hashes(
            support["schemas"][run_id], packets, rule, supplement_spec,
            plan, framework, semantic_artifact,
        )
        run_dir = output_root / run_id
        manifest_path, records_path = run_dir / "evidence-manifest.json", run_dir / "generated-records.json"
        plan_path, framework_path, completeness_path = run_dir / "extraction-plan.json", run_dir / "paper-semantic-framework.json", run_dir / "completeness-report.json"
        source_markdown_path = run_dir / "source-semantic.md"
        source_manifest_path = run_dir / "source-semantic-manifest.json"
        relation_input_path = run_dir / "source-relation-input.json"
        if manifest_path.exists() and records_path.exists() and plan_path.exists() and framework_path.exists() and completeness_path.exists() and source_markdown_path.exists() and source_manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            records = json.loads(records_path.read_text(encoding="utf-8"))
            persisted_plan = json.loads(plan_path.read_text(encoding="utf-8"))
            persisted_framework = json.loads(framework_path.read_text(encoding="utf-8"))
            completeness = json.loads(completeness_path.read_text(encoding="utf-8"))
            persisted_source_manifest = json.loads(source_manifest_path.read_text(encoding="utf-8"))
            if manifest.get('source_design_artifacts',[])!=source_design_relations.artifact_receipt(records,run_dir):
                raise ValueError('source design artifact resume mismatch')
            if manifest.get('raster_dot_artifacts',[]) != raster_dot_routes.artifact_receipt(records,run_dir):
                raise ValueError('raster dot artifact resume mismatch')
            if manifest.get('vector_outline_artifacts',[]) != vector_outline_routes.artifact_receipt(records,run_dir):
                raise ValueError('vector outline artifact resume mismatch')
            if manifest.get('sparse_artifacts',[])!=source_sparse.artifact_receipt(records,run_dir):
                raise ValueError('sparse artifact manifest drift during resume')
            if manifest.get('main_marker_artifacts',[])!=main_markers.artifact_receipt(records,run_dir):
                raise ValueError('main marker artifact manifest drift during resume')
            if manifest.get('main_marker_data_artifacts',[])!=main_marker_assets.artifact_receipt(records,run_dir):
                raise ValueError('main marker data asset drift during resume')
            relation_input_matches = relation_input_path.exists() and json.loads(relation_input_path.read_text(encoding='utf-8')) == semantic_artifact['relation_input']
            if relation_input_matches and manifest.get("input_hashes") == expected_inputs and manifest.get("records_sha256") == digest(records) and persisted_plan == plan and persisted_framework == framework and manifest.get("completeness_sha256") == digest(completeness) and persisted_source_manifest == semantic_artifact["metadata"] and phase_b.file_digest(source_markdown_path) == semantic_artifact["metadata"]["markdownSha256"]:
                manifest = dict(manifest)
                manifest["resumed"] = True
                return manifest
        return build_paper(root, output_root, routing, rule, support["schemas"][run_id], support["hashes"], cache_root, supplement_spec, plan, framework, inventories, semantic_artifact, figure_decisions)


def fold(value: Any) -> str:
    text = re.sub(r"[^a-z0-9]+", "", str(value).casefold())
    aliases = {"ggbfs": "ggbs", "flyash": "fa", "sodiumhydroxide": "naoh", "sodiumsilicatesolution": "ss", "additionwater": "water", "addedwater": "water", "extrawater": "water", "watersolid": "ws", "waterbinder": "wb", "dynamicyieldstress": "dynamicyieldstress", "vfunneldischargingtime": "dischargingtime"}
    return aliases.get(text, text)


def source_identity(mix: dict[str, Any]) -> tuple[str, int] | None:
    ext = mix.get("extensions") or {}
    table, row = ext.get("source_table"), ext.get("source_row")
    if table is None or row is None:
        return None
    try:
        return fold(table), int(row)
    except (TypeError, ValueError):
        return None


def generated_field_records(mix: dict[str, Any]) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for item in mix["modules"]["materials"]["extensions"].get("reported_parameters", []):
        values.append({"label": fold(item.get("parameter_key") or item.get("original_label")), "value": item.get("value"), "unit": item.get("unit"), "evidence_key": item.get("evidence_key"), "field": item.get("parameter_key") or item.get("original_label")})
    for module in ("performance", "characterizations"):
        for index, item in enumerate(mix["modules"].get(module, [])):
            field_path = f"/modules/{module}/{index}/value"
            prov = mix.get("field_provenance", {}).get(field_path, {})
            values.append({"label": fold(item.get("name")), "value": item.get("value"), "unit": item.get("unit"), "evidence_key": prov.get("evidence_key"), "field": item.get("name")})
    return values


def generated_fields(mix: dict[str, Any]) -> list[tuple[str, Any, Any]]:
    return [(item["label"], item["value"], item["unit"]) for item in generated_field_records(mix)]


def accepted_field_records(mix: dict[str, Any]) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for item in mix.get("modules", {}).get("materials", {}).get("extensions", {}).get("reported_parameters", []):
        values.append({"label": fold(item.get("parameter_key") or item.get("original_label")), "value": item.get("value"), "unit": item.get("unit"), "evidence_key": item.get("evidence_key"), "field": item.get("parameter_key") or item.get("original_label")})
    for module in ("performance", "characterizations"):
        for index, item in enumerate(mix.get("modules", {}).get(module, []) or []):
            field_path = f"/modules/{module}/{index}/value"
            prov = mix.get("field_provenance", {}).get(field_path, {})
            values.append({"label": fold(item.get("name")), "value": item.get("value"), "unit": item.get("unit"), "evidence_key": prov.get("evidence_key"), "field": item.get("name")})
    return values


def accepted_fields(mix: dict[str, Any]) -> list[tuple[str, Any, Any]]:
    return [(item["label"], item["value"], item["unit"]) for item in accepted_field_records(mix)]


def compact_evidence(index: dict[str, dict[str, Any]], key: str | None) -> dict[str, Any] | None:
    link = index.get(key) if key else None
    if not link:
        return None
    return {name: link.get(name) for name in ("evidence_key", "page", "table_number", "bbox", "snippet_sha256")}


def deployment_decision(counts: dict[str, int], source_conflicts: list[dict[str, Any]], required_quarantine: list[dict[str, Any]]) -> dict[str, Any]:
    reasons = []
    if counts.get("mismatch", 0): reasons.append("semantic_mismatch")
    if source_conflicts: reasons.append("source_conflict")
    if required_quarantine: reasons.append("required_quarantine")
    if counts.get("unmapped", 0): reasons.append("unmapped_field")
    return {"AUTOMATIC_ACCEPTANCE": "REJECT" if reasons else "AUTO_ACCEPT", "reasons": reasons}


def post_compare(root: Path, output_root: Path, reference_root: Path) -> dict[str, Any]:
    from source_run_inventory import source_run_dirs
    results = []
    for run_dir in source_run_dirs(output_root):
        run_id = run_dir.name
        generated_path = run_dir / "generated-records.json"
        if not generated_path.exists():
            raise ValueError("post comparison requires atomically generated records")
        generated = json.loads(generated_path.read_text(encoding="utf-8"))
        reference = json.loads((reference_root / run_id / "records.json").read_text(encoding="utf-8"))
        by_source = {source_identity(mix): mix for mix in reference.get("mixes", []) if source_identity(mix)}
        by_name = {fold(mix.get("modules", {}).get("identity_source_specimen", {}).get("custom_test_id")): mix for mix in reference.get("mixes", [])}
        counts = {"identity_matches": 0, "exact": 0, "equivalent": 0, "mismatch": 0, "quarantined": 0, "unmapped": 0}
        mismatch_details: list[dict[str, Any]] = []
        generated_evidence = {link["evidence_key"]: link for link in generated.get("evidence_links", [])}
        accepted_evidence = {link["evidence_key"]: link for link in reference.get("evidence_links", [])}
        matched_evidence_keys: set[str] = set()
        for mix in generated["mixes"]:
            accepted = by_source.get(source_identity(mix)) or by_name.get(fold(mix["modules"]["identity_source_specimen"].get("custom_test_id")))
            fields = generated_field_records(mix)
            if accepted is None:
                counts["unmapped"] += len(fields)
                continue
            counts["identity_matches"] += 1
            pool = accepted_field_records(accepted)
            used: set[int] = set()
            for field in fields:
                label, value, unit = field["label"], field["value"], field["unit"]
                exact_index = next((index for index, other in enumerate(pool) if index not in used and (other["label"], other["value"], other["unit"]) == (label, value, unit)), None)
                if exact_index is not None:
                    used.add(exact_index); counts["exact"] += 1
                    if field.get("evidence_key"): matched_evidence_keys.add(field["evidence_key"])
                    continue
                equivalent_index = next((index for index, other in enumerate(pool) if index not in used and other["label"] == label and other["value"] == value), None)
                if equivalent_index is not None:
                    used.add(equivalent_index); counts["equivalent"] += 1
                    if field.get("evidence_key"): matched_evidence_keys.add(field["evidence_key"])
                    continue
                label_index = next((index for index, other in enumerate(pool) if index not in used and other["label"] == label), None)
                if label_index is not None:
                    used.add(label_index); counts["mismatch"] += 1
                    other = pool[label_index]
                    mismatch_details.append({
                        "mix_key": mix["mix_key"], "source_identity": source_identity(mix), "field": field["field"],
                        "generated": {"value": value, "unit": unit, "evidence": compact_evidence(generated_evidence, field.get("evidence_key"))},
                        "accepted": {"value": other["value"], "unit": other["unit"], "evidence": compact_evidence(accepted_evidence, other.get("evidence_key"))},
                        "decision": "QUARANTINED_SOURCE_NULL_RETAINED" if value is None else "UNRESOLVED_MISMATCH",
                    })
                else:
                    counts["unmapped"] += 1
        conflicts = accepted_source_conflicts(reference, Path(generated["assets"][0]["relative_path"]))
        required_quarantine = [item for item in generated.get("quarantined", []) if item.get("required") and item.get("evidence_key") not in matched_evidence_keys]
        counts["quarantined"] = len(conflicts) + len(required_quarantine)
        gate = deployment_decision(counts, conflicts, required_quarantine)
        status = "SOURCE_CONFLICT_GOLD_QUARANTINED" if conflicts else "SEMANTIC_MISMATCH_QUARANTINED" if mismatch_details else "PARTIAL" if counts["unmapped"] else "PASS"
        results.append({"run_id": run_id, "status": status, "counts": counts, "mismatch_details": mismatch_details, "source_conflicts": conflicts, "required_quarantine": required_quarantine, "deployment_gate": gate})
    totals = {key: sum(item["counts"][key] for item in results) for key in ("identity_matches", "exact", "equivalent", "mismatch", "quarantined", "unmapped")}
    report = {"schema_version": 1, "generated_before_reference_read": True, "source_authority": "frozen_pdf", "papers": results, "totals": totals, "deployment_gate": {"AUTOMATIC_ACCEPTANCE": "REJECT" if any(item["deployment_gate"]["AUTOMATIC_ACCEPTANCE"] == "REJECT" for item in results) else "AUTO_ACCEPT", "auto_accepted_papers": [item["run_id"] for item in results if item["deployment_gate"]["AUTOMATIC_ACCEPTANCE"] == "AUTO_ACCEPT"], "rejected_papers": [item["run_id"] for item in results if item["deployment_gate"]["AUTOMATIC_ACCEPTANCE"] == "REJECT"]}}
    atomic_json(output_root / "post-generation-comparison.json", report)
    return report


def accepted_source_conflicts(reference: dict[str, Any], pdf: Path) -> list[dict[str, Any]]:
    doc = fitz.open(pdf)
    conflicts = []
    for link in reference.get("evidence_links", []):
        if "/performance/" not in str(link.get("field_path", "")) or not link.get("bbox") or not link.get("page"):
            continue
        numbers = re.findall(r"(?<![A-Za-z])[0-9]+(?:\.[0-9]+)?", str(link.get("snippet", "")))
        if not numbers:
            continue
        expected = numbers[-1]
        x0, y0, x1, y1 = link["bbox"]
        tokens = [str(word[4]).strip(".,") for word in doc[int(link["page"]) - 1].get_text("words") if x0 <= (word[0] + word[2]) / 2 <= x1 and y0 <= (word[1] + word[3]) / 2 <= y1]
        if expected not in tokens:
            conflicts.append({"evidence_key": link.get("evidence_key"), "claimed_token": expected, "reason": "accepted_bbox_does_not_contain_claimed_source_token"})
    return conflicts


def benchmark(
    root: Path,
    output_root: Path,
    routing_path: Path,
    rules_path: Path,
    figure_decisions_path: Path | None = DEFAULT_FIGURE_DECISIONS,
) -> dict[str, Any]:
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(dir=output_root.parent) as temporary:
        work = Path(temporary)
        cache = work / "cache"
        cold = build(root, work / "cold", routing_path, rules_path, cache, figure_decisions_path=figure_decisions_path)
        warm = build(root, work / "warm", routing_path, rules_path, cache, figure_decisions_path=figure_decisions_path)
        interrupted = False
        try:
            build(
                root, work / "resume", routing_path, rules_path,
                work / "resume-cache", fail_after=3,
                figure_decisions_path=figure_decisions_path,
            )
        except RuntimeError:
            interrupted = True
        resumed = build(
            root, work / "resume", routing_path, rules_path,
            work / "resume-cache", figure_decisions_path=figure_decisions_path,
        )
        mutated = json.loads(routing_path.read_text(encoding="utf-8"))
        target_run = mutated["packets"][0]["run_id"]
        token = next(token for cell in mutated["packets"][0]["cells"] for token in cell["tokens"] if token["typed_state"] == "number")
        token["typed"] = float(token["typed"]) + 0.125
        token["token"] = str(token["typed"])
        mutated_path = work / "mutated-routing.json"
        atomic_json(mutated_path, mutated)
        corrupt = None
        corrupt_rejection = None
        try:
            corrupt = build(
                root, work / "corrupt", mutated_path, rules_path,
                work / "corrupt-cache", figure_decisions_path=figure_decisions_path,
            )
        except ValueError as error:
            if figure_decisions_path is None or "figure semantic base framework mismatch" not in str(error):
                raise
            corrupt_rejection = "STALE_FIGURE_SEMANTIC_DECISION_REJECTED"
        cold_hashes = {paper["run_id"]: paper["records_sha256"] for paper in cold["papers"]}
        corrupt_hashes = {paper["run_id"]: paper["records_sha256"] for paper in corrupt["papers"]} if corrupt else {}
        report = {"schema_version": 2, "runtime_only": True, "development_time_seconds": None, "development_time_note": "reported separately", "figure_decisions_sha256": digest(json.loads((figure_decisions_path if figure_decisions_path.is_absolute() else root / figure_decisions_path).read_text(encoding="utf-8"))) if figure_decisions_path is not None else None, "cold_seconds": cold["runtime_seconds"], "warm_seconds": warm["runtime_seconds"], "cold_warm_semantic_hash_equal": cold["semantic_hash"] == warm["semantic_hash"], "warm_pdf_parse_cache_hits": sum(bool(paper.get("cache_hit")) for paper in warm["papers"]), "crash_injected": interrupted, "resume_semantic_hash_equal": cold["semantic_hash"] == resumed["semantic_hash"], "corrupt_one_target_run": target_run, "corrupt_one_fail_closed": corrupt_rejection is not None, "corrupt_one_rejection": corrupt_rejection, "corrupt_one_changed_target": True if corrupt_rejection else cold_hashes[target_run] != corrupt_hashes[target_run], "corrupt_one_unaffected_papers": None if corrupt_rejection else sum(cold_hashes[run_id] == corrupt_hashes[run_id] for run_id in cold_hashes if run_id != target_run), "network_calls": 0, "wakg_calls": 0, "model_calls": 0, "token_proxy": 0, "benchmark_wall_seconds": round(time.perf_counter() - started, 6)}
    atomic_json(output_root / "runtime-benchmark.json", report)
    return report


def contract_validate(
    root: Path,
    output_root: Path,
    routing_path: Path | None = None,
    rules_path: Path | None = None,
    figure_decisions_path: Path | None = None,
) -> dict[str, Any]:
    routing_path = routing_path or root / "runs" / "user-corpus-10-efficiency-v2" / "phase-a-routing-bundle.json"
    rules_path = rules_path or root / "fixtures" / "user_corpus_semantic_rules_v2.json"
    routing, rules, support = load_inputs(root, routing_path, rules_path)
    rules_by_run = {str(item["run_id"]): item for item in rules["papers"]}
    figure_decisions = None
    if figure_decisions_path is not None:
        decision_path = figure_decisions_path if figure_decisions_path.is_absolute() else root / figure_decisions_path
        figure_decisions = json.loads(decision_path.read_text(encoding="utf-8"))
    results = []
    with tempfile.TemporaryDirectory(dir=output_root.parent) as temporary:
        project = Path(temporary) / "pipeline-project"
        from source_run_inventory import source_run_dirs
        for run_id in (path.name for path in source_run_dirs(output_root)):
            if run_id not in rules_by_run or run_id not in support["schemas"]:
                results.append({"source_run_id": run_id, "verdict": "REJECT", "defects": ["UNKNOWN_RUN_ID"]})
                continue
            required_artifacts = [
                "generated-records.json", "extraction-plan.json",
                "paper-semantic-framework.json", "completeness-report.json",
                "evidence-manifest.json", "source-semantic-manifest.json",
                "source-semantic.md", "transformation-report.json",
            ]
            missing_artifacts = [
                name for name in required_artifacts
                if not (output_root / run_id / name).is_file()
            ]
            if missing_artifacts:
                results.append({
                    "source_run_id": run_id,
                    "verdict": "REJECT",
                    "defects": [f"MISSING_REQUIRED_ARTIFACT:{name}" for name in missing_artifacts],
                })
                continue
            model_records = output_root / run_id / "generated-records.json"
            records = json.loads(model_records.read_text(encoding="utf-8"))
            plan = json.loads((output_root / run_id / "extraction-plan.json").read_text(encoding="utf-8"))
            framework = json.loads((output_root / run_id / "paper-semantic-framework.json").read_text(encoding="utf-8"))
            persisted_completeness = json.loads((output_root / run_id / "completeness-report.json").read_text(encoding="utf-8"))
            persisted_transformation = json.loads((output_root / run_id / "transformation-report.json").read_text(encoding="utf-8"))
            evidence_manifest = json.loads((output_root / run_id / "evidence-manifest.json").read_text(encoding="utf-8"))
            source_manifest = json.loads((output_root / run_id / "source-semantic-manifest.json").read_text(encoding="utf-8"))
            source_markdown_path = output_root / run_id / "source-semantic.md"
            source_packets = [packet for packet in routing["packets"] if packet["run_id"] == run_id]
            rule = rules_by_run[run_id]
            supplement_spec = support["supplements"].get(run_id)
            expected_plan, expected_framework, _, expected_semantic = paper_plan(
                root, support["schemas"][run_id], supplement_spec, source_packets,
                rule, figure_decisions,
            )
            expected_inputs = paper_input_hashes(
                support["schemas"][run_id], source_packets, rule, supplement_spec,
                expected_plan, expected_framework, expected_semantic,
            )
            recomputed_completeness = multimodal.validate_completeness(plan, records)
            recomputed_transformation = transformation_contract.validate_records(records)
            framework_receipt = (records.get("extensions") or {}).get("semantic_framework") or {}
            recomputed_figure_gate = semantic_framework.assess_multimodal_records(framework, records)
            recomputed_completeness["semantic_framework_sha256"] = framework["framework_sha256"]
            recomputed_completeness["semantic_framework_verdict"] = framework_receipt.get("verdict")
            recomputed_completeness["figure_numeric_gate"] = recomputed_figure_gate
            recomputed_completeness["transformation_contract"] = recomputed_transformation
            if recomputed_transformation["verdict"] != "PASS":
                recomputed_completeness.setdefault("failures", []).append(
                    f"TRANSFORMATION_CONTRACT_REJECTED:{len(recomputed_transformation['defects'])}"
                )
                recomputed_completeness["verdict"] = "BLOCKED"
            if recomputed_figure_gate["verdict"] != "PASS":
                recomputed_completeness.setdefault("failures", []).append(
                    f"NUMERIC_FIGURE_GATE_BLOCKED:{recomputed_figure_gate['blocking_figure_count']}"
                )
                recomputed_completeness["verdict"] = "BLOCKED"
            asset_defects = []
            try:
                if evidence_manifest.get('main_marker_data_artifacts',[])!=main_marker_assets.artifact_receipt(records,output_root/run_id):
                    asset_defects.append('MAIN_MARKER_DATA_MANIFEST_DRIFT')
            except (OSError,ValueError,KeyError) as exc:
                asset_defects.append('MAIN_MARKER_DATA_INVALID:'+str(exc))
            try:
                if evidence_manifest.get('main_marker_artifacts',[])!=main_markers.artifact_receipt(records,output_root/run_id):
                    asset_defects.append('MAIN_MARKER_ARTIFACT_MANIFEST_DRIFT')
            except (OSError,ValueError,KeyError) as exc:
                asset_defects.append('MAIN_MARKER_ARTIFACT_INVALID:'+str(exc))
            try:
                if evidence_manifest.get('raster_dot_artifacts',[]) != raster_dot_routes.artifact_receipt(records,output_root/run_id):
                    raise ValueError('raster dot artifact validation mismatch')
                if evidence_manifest.get('sparse_artifacts',[])!=source_sparse.artifact_receipt(records,output_root/run_id):
                    asset_defects.append('SPARSE_ARTIFACT_MANIFEST_DRIFT')
            except (OSError,ValueError,KeyError) as exc:
                asset_defects.append('SPARSE_ARTIFACT_INVALID:'+str(exc))
            try:
                if evidence_manifest.get('vector_outline_artifacts',[]) != vector_outline_routes.artifact_receipt(records,output_root/run_id):
                    asset_defects.append('VECTOR_OUTLINE_ARTIFACT_MANIFEST_DRIFT')
            except (OSError,ValueError,KeyError) as exc:
                asset_defects.append('VECTOR_OUTLINE_ARTIFACT_INVALID:'+str(exc))
            if plan != expected_plan:
                asset_defects.append("EXTRACTION_PLAN_INPUT_DRIFT")
            if framework != expected_framework:
                asset_defects.append("SEMANTIC_FRAMEWORK_INPUT_DRIFT")
            if source_manifest != expected_semantic["metadata"]:
                asset_defects.append("SOURCE_MARKDOWN_MANIFEST_DRIFT")
            if not source_markdown_path.is_file() or phase_b.file_digest(source_markdown_path) != expected_semantic["metadata"]["markdownSha256"]:
                asset_defects.append("SOURCE_MARKDOWN_BYTES_DRIFT")
            records_inputs = (records.get("extensions") or {}).get("input_hashes")
            if records_inputs != expected_inputs:
                asset_defects.append("RECORD_INPUT_RECEIPT_DRIFT")
            if evidence_manifest.get("input_hashes") != expected_inputs:
                asset_defects.append("EVIDENCE_MANIFEST_INPUT_RECEIPT_DRIFT")
            if evidence_manifest.get("records_sha256") != digest(records):
                asset_defects.append("EVIDENCE_MANIFEST_RECORDS_HASH_DRIFT")
            if evidence_manifest.get("transformation_report_sha256") != digest(persisted_transformation):
                asset_defects.append("EVIDENCE_MANIFEST_TRANSFORMATION_HASH_DRIFT")
            if persisted_transformation != recomputed_transformation:
                asset_defects.append("TRANSFORMATION_REPORT_DRIFT")
            if evidence_manifest.get("completeness_sha256") != digest(persisted_completeness):
                asset_defects.append("EVIDENCE_MANIFEST_COMPLETENESS_HASH_DRIFT")
            expected_pdf_sha = support["schemas"][run_id]["pdf_sha256"]
            pdf_assets = [asset for asset in records.get("assets") or [] if asset.get("kind") == "pdf"]
            if len(pdf_assets) != 1 or pdf_assets[0].get("sha256") != expected_pdf_sha:
                asset_defects.append("FROZEN_PDF_IDENTITY_DRIFT")
            for asset in records.get("assets") or []:
                relative = Path(str(asset.get("relative_path") or ""))
                path = relative if relative.is_absolute() else output_root / run_id / relative
                if not path.is_file():
                    asset_defects.append(f"MISSING_ASSET:{asset.get('asset_key')}")
                elif phase_b.file_digest(path) != asset.get("sha256"):
                    asset_defects.append(f"ASSET_HASH_MISMATCH:{asset.get('asset_key')}")
            pdf = root / support["schemas"][run_id]["pdf_relative_path"]
            # The canonical bundle intentionally keeps assets relative to its
            # paper run so that byte-identical cold rebuilds remain portable.
            # The isolated skill validator, however, resolves assets against
            # its own project root.  Stage an absolute-path validation copy so
            # it checks the real frozen bytes instead of rejecting a valid
            # portable reference from the wrong base directory.
            validation_records = json.loads(json.dumps(records))
            for asset in validation_records.get("assets") or []:
                relative = Path(str(asset.get("relative_path") or ""))
                if not relative.is_absolute():
                    asset["relative_path"] = str((output_root / run_id / relative).resolve())
            validation_records_path = Path(temporary) / f"{run_id}-validation-records.json"
            atomic_json(validation_records_path, validation_records)
            isolated_run = "phase-c-" + run_id[9:]
            extracted = pipeline.extract_document(project, pdf, isolated_run, validation_records_path)
            validation = pipeline.validate_run(project, isolated_run)
            defects = [*validation["defects"], *asset_defects]
            if recomputed_completeness != persisted_completeness:
                defects.append("COMPLETENESS_REPORT_DRIFT")
            if recomputed_completeness.get("verdict") != "PASS":
                defects.extend(recomputed_completeness.get("failures") or ["COMPLETENESS_GATE_BLOCKED"])
            verdict = "PASS" if validation["verdict"] == "PASS" and not defects else "REJECT"
            results.append({"source_run_id": run_id, "isolated_run_id": isolated_run, "extract_status": extracted["status"], "pipeline_verdict": validation["verdict"], "completeness_verdict": recomputed_completeness["verdict"], "verdict": verdict, "defects": defects})
    report = {"schema_version": 4, "validators": ["frozen-input-regeneration", "input-receipt-and-manifest-sha256", "skills/wakg-literature-pipeline/scripts/pipeline.py::validate_run", "scripts/multimodal_extraction_plan.py::validate_completeness", "scripts/paper_semantic_framework.py::assess_multimodal_records", "scripts/transformation_contract.py::validate_records", "asset-sha256"], "results": results, "verdict": "PASS" if len(results) == 10 and all(item["verdict"] == "PASS" for item in results) else "REJECT", "isolated_temp_project_removed": True}
    atomic_json(output_root / "actual-pipeline-contract-validation.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("build", "contract-validate", "compare", "benchmark"))
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, default=Path("runs/user-corpus-10-efficiency-v2/phase-c-full10"))
    parser.add_argument("--routing", type=Path, default=Path("runs/user-corpus-10-efficiency-v2/phase-a-routing-bundle.json"))
    parser.add_argument("--rules", type=Path, default=Path("fixtures/user_corpus_semantic_rules_v2.json"))
    figure_mode = parser.add_mutually_exclusive_group()
    figure_mode.add_argument("--figure-decisions", type=Path, default=DEFAULT_FIGURE_DECISIONS)
    figure_mode.add_argument("--planning", action="store_true", help="Build internal figure candidates without reusing prior semantic decisions; does not authorize review/publication.")
    parser.add_argument("--cache", type=Path, default=Path("cache/phase-c-pdf-words"))
    parser.add_argument("--reference-root", type=Path, default=Path("runs"))
    parser.add_argument('--keep-going',action='store_true',help='Record per-paper regression failures and continue; does not relax acceptance')
    parser.add_argument('--fault-campaign')
    args = parser.parse_args()
    if args.planning and args.command not in {"build", "contract-validate"}:
        parser.error("--planning is only available for internal build or validation")
    if args.keep_going and (args.command!='build' or not args.fault_campaign):
        parser.error('--keep-going requires build and --fault-campaign')
    root = args.root.resolve()
    output = root / args.output if not args.output.is_absolute() else args.output
    if args.command == "build":
        result=build(root, output, root / args.routing, root / args.rules, root / args.cache, figure_decisions_path=None if args.planning else args.figure_decisions,keep_going=args.keep_going,fault_campaign=args.fault_campaign)
        print(canonical(result))
        if result['execution']['failed']:raise SystemExit(2)
    elif args.command == "contract-validate":
        print(canonical(contract_validate(root, output, root / args.routing, root / args.rules, None if args.planning else args.figure_decisions)))
    elif args.command == "compare":
        reference = root / args.reference_root if not args.reference_root.is_absolute() else args.reference_root
        print(canonical(post_compare(root, output, reference)))
    else:
        print(canonical(benchmark(root, output, root / args.routing, root / args.rules, args.figure_decisions)))


if __name__ == "__main__":
    main()
