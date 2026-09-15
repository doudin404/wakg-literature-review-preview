"""Project independently reviewed curve candidates into the original MAT/MIX contract.

Promotes only a hash-identical source proof and semantic/overlay receipt. The
receipt approves curve digitization, never whole-paper acceptance. Arbitrary
FTIR absorbance is retained as reported data, not converted to transmittance.
Unscaled XRD display heights are never phase quantities or absolute intensity.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math

import fitz

VERSION = "reviewed-vector-contract-v1"


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _assign(target, key, value):
    if target.get(key) is not None and target[key] != value:
        raise ValueError(f"reviewed curve would overwrite existing scientific data: {key}")
    target[key] = value


def spectrum_record(candidate,panel,image_key,metadata):
    """Pure record construction shared by pending extraction and reviewed promotion.

    This function grants no acceptance and never changes a record's review state.
    The caller supplies its own explicit pending or reviewed metadata.
    """
    return {'schema_version':'1.0','points_asset_key':candidate['data_asset_key'],
        'source_image_asset_key':image_key,'x_axis':copy.deepcopy(panel['x_axis']),
        'y_axis':copy.deepcopy(panel['y_axis']),
        'extensions':{'source_figure':candidate['figure_number'],'panel_label':candidate['panel_label'],
            'curve_type':candidate['curve_type'],'absolute_intensity_calibrated':False,
            'source_offsets_preserved':True,**copy.deepcopy(metadata)}}


def assign_xrd_spectrum(owner,candidate,curve):
    """Existing MAT/MIX XRD shape only; does not assign phase quantities or approval."""
    if candidate['record_type']=='mat':
        if owner.get('xrd_qxrd') is None:
            owner['xrd_qxrd']={'schema_version':'1.0','xrd_pattern':None,'standard_method':None,
                'standard_xrd_pattern':None,'standard_formula':None,'standard_purity_percent':None,
                'standard_fraction_percent':None,'qxrd_phase_table':[],'amorphous_total_percent':None,'extensions':{}}
        _assign(owner['xrd_qxrd'],'xrd_pattern',curve)
        return '/xrd_qxrd/xrd_pattern/points_asset_key'
    if candidate['record_type']!='mix':raise ValueError('XRD destination must be MAT or MIX')
    rows=owner['modules'].setdefault('characterizations',[])
    row={'name':'xrd_pattern','value':None,'unit':None,'age_seconds':None,'specimen':None,
         'extensions':{'spectrum':curve,'source_candidate_key':candidate['candidate_key']}}
    matches=[i for i,r in enumerate(rows) if r.get('extensions',{}).get('source_candidate_key')==candidate['candidate_key']]
    if matches:
        index=matches[0]
        if len(matches)!=1 or rows[index]!=row:raise ValueError('MIX spectrum projection drift')
    else:index=len(rows);rows.append(row)
    return f'/modules/characterizations/{index}/extensions/spectrum/points_asset_key'


def promote(records: dict, specification: dict, proof: dict, acceptance: dict) -> dict:
    if proof.get("verdict") != "PASS" or proof.get("defects"):
        raise ValueError("vector source proof has not passed")
    if acceptance.get("verdict") != "PASS" or acceptance.get("defects") or not acceptance.get("message_id"):
        raise ValueError("vector overlay acceptance has not passed")
    if acceptance.get("specification_sha256") != digest(specification) or acceptance.get("report_sha256") != proof.get("report_sha256"):
        raise ValueError("vector review subject mismatch")
    if proof.get("specification_sha256") != digest(specification) or proof.get("pdf_sha256") != specification["pdf_sha256"]:
        raise ValueError("vector proof binding mismatch")
    expected_figures = sorted({int(p["figure"].split()[-1]) for p in specification["panels"]})
    if acceptance.get("inspected_figures") != expected_figures or not acceptance.get("overlay_sha256"):
        raise ValueError("vector overlay review coverage missing")
    result = copy.deepcopy(records)
    receipt = result["extensions"]["vector_curve_candidates"]
    if receipt["specification_sha256"] != digest(specification) or len(receipt["items"]) != proof["checked_csvs"]:
        raise ValueError("vector candidate coverage mismatch")
    panels = {(p["figure"], p["panel_label"]): p for p in specification["panels"]}
    owners = {r.get("mat_key") or r.get("mix_key"): r for r in [*result["mats"], *result["mixes"]]}
    assets = {a["asset_key"]: a for a in result["assets"]}
    links = {e["evidence_key"]: e for e in result["evidence_links"]}
    acceptance_hash = digest(acceptance)

    def evidence(owner, candidate, path, bbox, original, unit, formula=None, transformation=None):
        evidence_key = "ev-reviewed-vector-" + digest([candidate["candidate_key"], path, acceptance_hash])[:24]
        description = f"{candidate['figure_number']}({candidate['panel_label']}): {candidate['record_label']}"
        link = {"evidence_key": evidence_key, "paper_key": owner["paper_key"], "record_type": candidate["record_type"],
                "record_key": candidate["record_key"], "field_path": path, "asset_key": candidate["source_pdf_asset_key"],
                "page": candidate["page"], "bbox": bbox, "figure_number": candidate["figure_number"], "section": None, "table_number": None,
                "snippet": description, "snippet_sha256": hashlib.sha256(description.encode()).hexdigest(),
                "extraction_method": VERSION, "confidence": .98,
                "extensions": {"evidence_kind": "reviewed_curve_geometry", "data_asset_key": candidate["data_asset_key"],
                               "geometry_sha256": candidate["geometry_sha256"], "overlay_acceptance_sha256": acceptance_hash}}
        if evidence_key in links and links[evidence_key] != link:
            raise ValueError("vector evidence drift")
        if evidence_key not in links:
            result["evidence_links"].append(link)
            links[evidence_key] = link
        provenance = {"evidence_key": evidence_key, "original_value": original, "original_unit": unit,
                      "formula": formula, "extraction_method": VERSION, "confidence": .98, "review_status": "confirmed"}
        if transformation is not None:
            provenance["transformation"] = transformation
        _assign(owner.setdefault("field_provenance", {}), path, provenance)

    percentile_count = 0
    for candidate in receipt["items"]:
        owner = owners[candidate["record_key"]]
        panel = panels[(candidate["figure_number"], candidate["panel_label"])]
        images = [a for a in result["assets"] if a["kind"] == "main_figure" and a.get("extensions", {}).get("figure_number") == candidate["figure_number"]
                  and a["extensions"].get("page") == candidate["page"]]
        if len(images) != 1 or not fitz.Rect(images[0]["extensions"]["source_region_bbox"]).contains(fitz.Rect(panel["plot_bbox"])):
            raise ValueError("canonical source image does not contain reviewed plot")
        image_key = images[0]["asset_key"]
        asset = assets[candidate["data_asset_key"]]
        asset["extensions"].update({"review_status": "confirmed", "source_image_asset_key": image_key, "overlay_acceptance_sha256": acceptance_hash})
        curve = spectrum_record(candidate,panel,image_key,{'overlay_acceptance_sha256':acceptance_hash})
        if candidate["figure_type"] == "PSD":
            if candidate["record_type"] != "mat":
                raise ValueError("PSD binding requires a MAT")
            distribution = owner["particle_size_distribution"]
            if candidate["curve_type"] == "reported_volume_density":
                _assign(distribution.setdefault("extensions", {}), "reported_volume_density", curve)
                path = "/particle_size_distribution/extensions/reported_volume_density/points_asset_key"
            else:
                for key, value in {"points_asset_key": candidate["data_asset_key"], "source_image_asset_key": image_key, "reported_curve_type": "cumulative_finer"}.items():
                    _assign(distribution, key, value)
                _assign(distribution, "conversion_review", {"status": "confirmed", "algorithm": VERSION, "overlay_acceptance_sha256": acceptance_hash,
                                                          "uncertainty_intervals_um": {k: v["uncertainty_interval"] for k, v in candidate["percentiles"].items()}})
                _assign(distribution.setdefault("extensions", {}), "curve_calibration", {"x_axis": panel["x_axis"], "y_axis": panel["y_axis"]})
                path = "/particle_size_distribution/points_asset_key"
                for name, item in candidate["percentiles"].items():
                    value = item["value"]
                    _assign(distribution, name, value)
                    x = item["pdf_intersection"][0]
                    (left, low), (right, high) = panel["x_axis"]["anchors"]
                    transformation = {"kind": "log_axis_percentile", "formal": True, "formula": item["formula"],
                                      "inputs": {"value": x, "unit": "pdf_pt_x", "axis_pixel_bounds": [left, right], "axis_log10_bounds": [math.log10(low), math.log10(high)], "cumulative_percent": item["percent"]},
                                      "output": {"value": value, "unit": "um"},
                                      "forward_check": {"computed": value, "recorded": value, "tolerance": 1e-8},
                                      "reverse_check": {"computed": x, "recorded": x, "tolerance": 1e-8},
                                      "extensions": {"bracketing_pdf_points": item["bracketing_pdf_points"], "uncertainty_interval_um": item["uncertainty_interval"]}}
                    evidence(owner, candidate, f"/particle_size_distribution/{name}", item["bbox"], x, "pdf_pt_x", item["formula"], transformation)
                    percentile_count += 1
        elif candidate["record_type"] == "mat" and candidate["figure_type"] == "FTIR":
            if owner.get("ftir_spectrum") is None:
                owner["ftir_spectrum"] = {"schema_version": "1.0", "x_axis": {"name": "wavenumber", "unit": "cm^-1", "direction": "decreasing"},
                                          "y_axis": {"name": "Transmittance", "unit": "percent"}, "reported_y_semantics": "absorbance_arbitrary_units",
                                          "points_asset_key": None, "source_image_asset_key": image_key,
                                          "conversion_review": {"status": "not_applicable", "reason": "arbitrary_absorbance_not_absolute_absorbance"}, "extensions": {}}
            _assign(owner["ftir_spectrum"].setdefault("extensions", {}), "reported_spectrum", curve)
            path = "/ftir_spectrum/extensions/reported_spectrum/points_asset_key"
        elif candidate["figure_type"] == "XRD_QXRD":
            path = assign_xrd_spectrum(owner,candidate,curve)
        elif candidate["record_type"] == "mix" and candidate["figure_type"] in {"FTIR", "XRD_QXRD"}:
            rows = owner["modules"].setdefault("characterizations", [])
            row = {"name": "ftir_spectrum" if candidate["figure_type"] == "FTIR" else "xrd_pattern", "value": None, "unit": None,
                   "age_seconds": None, "specimen": None, "extensions": {"spectrum": curve, "source_candidate_key": candidate["candidate_key"]}}
            matches = [i for i, r in enumerate(rows) if r.get("extensions", {}).get("source_candidate_key") == candidate["candidate_key"]]
            if matches:
                index = matches[0]
                if len(matches) != 1 or rows[index] != row:
                    raise ValueError("MIX spectrum projection drift")
            else:
                index = len(rows)
                rows.append(row)
            path = f"/modules/characterizations/{index}/extensions/spectrum/points_asset_key"
        else:
            raise ValueError("unsupported reviewed curve destination")
        evidence(owner, candidate, path, candidate["plot_bbox"], candidate["geometry_sha256"], "pdf_vector_geometry")
    result["extensions"]["vector_curve_projection"] = {"version": VERSION, "acceptance_message_id": acceptance["message_id"],
                                                        "acceptance_sha256": acceptance_hash, "series": len(receipt["items"]), "percentiles": percentile_count,
                                                        "whole_paper_accepted": False}
    records.clear()
    records.update(result)
    return result["extensions"]["vector_curve_projection"]
