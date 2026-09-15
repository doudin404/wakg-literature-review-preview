"""Read-only, source-verified curve projection shared by review and its gates.

No token exception is granted by an evidence-mode string alone. Every curve
locator is compared with a hash-bound canonical record after re-opening the
PDF and independently checking every vector segment and CSV ordinate.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import fitz

from vector_curve_bindings import specification
from verify_vector_curves import sha, verify_payload

VERSION = "reviewed-curve-ui-v1"
PROOF_INPUTS = ("scripts/review_curve_projection.py", "scripts/verify_vector_curves.py",
                "scripts/vector_curve_bindings.py", "fixtures/vector_curve_acceptance_v1.json")
RASTER_PROOF_INPUTS = ("scripts/review_curve_projection.py", "scripts/raster_review_projection.py",
    "scripts/raster_curves.py", "scripts/verify_raster_curves.py", "scripts/promote_raster_curves.py",
    "scripts/raster_routes.py", "fixtures/raster-routes-v1.json", "scripts/raster_projection_coverage.py",
    "scripts/raster_dot_routes.py", "scripts/raster_dot_percentiles.py", "scripts/reviewed_raster_percentiles.py")
LABELS = {
    "cumulative_finer": "PSD 累计粒径分布曲线",
    "reported_volume_density": "PSD 体积分布曲线",
    "absorbance_arbitrary_units": "FTIR 光谱（吸光度，任意单位）",
    "xrd_offset_traces": "XRD 衍射曲线（保留原图偏移）",
}
FORMULA = "PDF vector coordinates -> independently reviewed axis calibration; original segments preserved"


def file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def pointer(record, path):
    value = record
    for part in path.strip("/").split("/"):
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


def curve_slots(record):
    """Canonical curve slots only; never invent a missing scalar observation."""
    if "mat_key" in record:
        paths = ["/particle_size_distribution/points_asset_key",
                 "/particle_size_distribution/extensions/reported_volume_density/points_asset_key",
                 "/ftir_spectrum/extensions/reported_spectrum/points_asset_key",
                 "/xrd_qxrd/xrd_pattern/points_asset_key"]
    else:
        paths = [f"/modules/characterizations/{i}/extensions/spectrum/points_asset_key"
                 for i, row in enumerate(record.get("modules", {}).get("characterizations", []))
                 if (row.get("extensions") or {}).get("spectrum")]
    for path in paths:
        try:
            value = pointer(record, path)
        except (KeyError, TypeError):
            continue
        if value:
            yield path, value


def safe_path(root, value):
    result = (root / value).resolve()
    result.relative_to(root.resolve())
    return result


def verified_index(project: Path, subject: dict):
    """Verify canonical assignments independently of the promotion writer."""
    check_subject_inputs(project,subject)
    if subject.get("curveProtocol") == "native-raster-v1":
        from raster_review_projection import verified_index as raster_index
        return raster_index(project,subject)
    path = safe_path(project, subject["recordsPath"])
    records = json.loads(path.read_text(encoding="utf-8"))
    run = path.parent
    spec = specification()
    proof = verify_payload(records, run, spec)
    acceptance = json.loads((project / "fixtures/vector_curve_acceptance_v1.json").read_text(encoding="utf-8"))
    if proof["verdict"] != "PASS" or acceptance.get("verdict") != "PASS" or acceptance.get("defects"):
        raise ValueError("curve source proof or semantic acceptance failed")
    if (acceptance.get("specification_sha256") != sha(spec) or acceptance.get("report_sha256") != proof["report_sha256"]
            or records["extensions"]["vector_curve_projection"]["acceptance_sha256"] != sha(acceptance)):
        raise ValueError("curve acceptance binding mismatch")
    panels = {(p["figure"], p["panel_label"]): p for p in spec["panels"]}
    candidates = {c["data_asset_key"]: c for c in records["extensions"]["vector_curve_candidates"]["items"]}
    assets = {a["asset_key"]: a for a in records["assets"]}
    links = {e["evidence_key"]: e for e in records["evidence_links"]}
    report = json.loads((run / records["extensions"]["vector_curve_candidates"]["report_path"]).read_text(encoding="utf-8"))
    series = {(p["panel"]["figure"], p["panel"]["panel_label"], s["record_type"], s["record_label"]): s
              for p in report["panels"] for s in p["series"]}
    index, seen = {}, set()
    for owner in [*records["mats"], *records["mixes"]]:
        owner_key = owner.get("mat_key") or owner["mix_key"]
        for field_path, asset_key in curve_slots(owner):
            candidate = candidates.get(asset_key)
            if candidate is None:
                # Other extractors must supply their own proof protocol.
                continue
            if candidate["record_key"] != owner_key or asset_key in seen:
                raise ValueError("curve canonical owner mismatch")
            seen.add(asset_key)
            panel = panels[(candidate["figure_number"], candidate["panel_label"])]
            curve = pointer(owner, field_path.rsplit("/", 1)[0])
            if field_path == "/particle_size_distribution/points_asset_key":
                calibration = curve.get("extensions", {}).get("curve_calibration") or {}
            else:
                calibration = curve
            if calibration.get("x_axis") != panel["x_axis"] or calibration.get("y_axis") != panel["y_axis"]:
                raise ValueError("curve canonical calibration mismatch")
            image = assets[curve["source_image_asset_key"]]
            image_path = (run / image["relative_path"]).resolve()
            image_path.relative_to(project.resolve())
            image_box = image["extensions"]["source_region_bbox"]
            if (file_sha(image_path) != image["sha256"] or not fitz.Rect(image_box).contains(fitz.Rect(panel["plot_bbox"]))
                    or image["extensions"].get("page") != candidate["page"] or image["extensions"].get("figure_number") != candidate["figure_number"]):
                raise ValueError("curve source image mismatch")
            raw_series = series[(candidate["figure_number"], candidate["panel_label"], candidate["record_type"], candidate["record_label"])]
            claims = [(field_path, asset_key, candidate["plot_bbox"], None)]
            if panel["curve_type"] == "cumulative_finer":
                for name, value in candidate["percentiles"].items():
                    claims.append((f"/particle_size_distribution/{name}", value["value"], value["bbox"], value))
            for claim_path, expected_value, bbox, percentile in claims:
                if pointer(owner, claim_path) != expected_value:
                    raise ValueError("curve canonical value mismatch")
                provenance = owner["field_provenance"][claim_path]
                link = links[provenance["evidence_key"]]
                expected_extensions = {"evidence_kind": "reviewed_curve_geometry", "data_asset_key": asset_key,
                                       "geometry_sha256": raw_series["geometry_sha256"], "overlay_acceptance_sha256": sha(acceptance)}
                if (link["record_key"] != owner_key or link["field_path"] != claim_path or link["bbox"] != bbox
                        or link["page"] != candidate["page"] or link["asset_key"] != candidate["source_pdf_asset_key"]
                        or link.get("extensions") != expected_extensions or provenance.get("review_status") != "confirmed"):
                    raise ValueError("curve canonical evidence mismatch")
                if percentile:
                    transform = provenance.get("transformation") or {}
                    if (transform.get("output") != {"value": expected_value, "unit": "um"}
                            or transform.get("inputs", {}).get("value") != percentile["pdf_intersection"][0]
                            or transform.get("extensions", {}).get("uncertainty_interval_um") != percentile["uncertainty_interval"]):
                        raise ValueError("curve percentile receipt mismatch")
                index[link["evidence_key"]] = {"recordKey": owner_key, "fieldPath": claim_path,
                    "value": f"{expected_value:g}" if percentile else "曲线数据", "unit": "µm" if percentile else None,
                    "page": candidate["page"], "bbox": bbox, "pdfSha256": spec["pdf_sha256"],
                    "curveKey": asset_key if not percentile else None, "candidate": candidate,
                    "curve": curve, "image": image, "series": raw_series, "label": LABELS[panel["curve_type"]],
                    "subject": subject, "asset": assets[asset_key], "run": run}
    if seen != set(candidates):
        raise ValueError("reviewed canonical curves omitted")
    return index


def proof_inputs(project,records,raster):
    paths=list(RASTER_PROOF_INPUTS if raster else PROOF_INPUTS)
    if raster:
        import raster_routes
        identity=records['extensions']['raster_curve_projection']['acceptance']['pdf_sha256']
        route,_=raster_routes.select(project,identity)
        if route is None or route['acceptance'] is None:raise ValueError('raster proof route unaccepted')
        entries=json.loads((project/raster_routes.REGISTRY).read_text(encoding='utf-8'))['routes']
        selected=[e for e in entries if (project/e['specification']).resolve()==Path(route['specification_path']).resolve()]
        if len(selected)!=1:raise ValueError('raster proof route ambiguous')
        paths.extend([selected[0]['specification'],selected[0]['acceptance']])
    return {p:file_sha(project/p) for p in paths}


def subject_for(project, records_path):
    records=json.loads(records_path.read_text(encoding="utf-8"))
    raster=bool(records.get("extensions",{}).get("raster_curve_projection"))
    if raster and records.get("extensions",{}).get("vector_curve_projection"):
        raise ValueError("mixed curve protocols require a combined proof")
    return {"version": VERSION, "recordsPath": records_path.resolve().relative_to(project.resolve()).as_posix(),
            **({"curveProtocol":"native-raster-v1"} if raster else {}),
            "recordsSha256": file_sha(records_path), "proofInputs":proof_inputs(project,records,raster)}


def check_subject_inputs(project,subject):
    """Cheap seal replay: hash every proof input; do not re-digitize curves."""
    if subject.get("curveProtocol") not in (None,"native-raster-v1"):
        raise ValueError("unknown curve proof protocol")
    raster=subject.get("curveProtocol")=="native-raster-v1"
    path=safe_path(project,subject["recordsPath"])
    if file_sha(path)!=subject["recordsSha256"]:raise ValueError("curve records hash mismatch")
    records=json.loads(path.read_text(encoding="utf-8"))
    if subject.get("version")!=VERSION or subject.get("proofInputs")!=proof_inputs(project,records,raster):
        raise ValueError("curve proof implementation or acceptance changed")
    if raster:
        from raster_dot_routes import artifact_receipt
        artifact_receipt(records,path.parent)
    receipt=records["extensions"]["raster_curve_candidates" if raster else "vector_curve_candidates"]
    report=(path.parent/receipt["report_path"]).resolve();report.relative_to(project.resolve())
    if file_sha(report)!=receipt["report_sha256"]:raise ValueError("curve report hash mismatch")
    candidate_keys={c["data_asset_key"] for c in receipt["items"]}
    for asset in records["assets"]:
        if asset["asset_key"] in candidate_keys or asset["kind"] in {"main_figure","pdf"}:
            source=(path.parent/asset["relative_path"]).resolve();source.relative_to(project.resolve())
            if file_sha(source)!=asset["sha256"]:raise ValueError("curve source asset hash mismatch")


def display_fields(record, field_factory):
    result = []
    for path, asset_key in curve_slots(record):
        provenance = record.get("field_provenance", {}).get(path) or {}
        if provenance.get("extraction_method") not in {"reviewed-vector-contract-v1","reviewed-raster-contract-v1"}:
            continue
        curve = pointer(record, path.rsplit("/", 1)[0])
        curve_type = "cumulative_finer" if path == "/particle_size_distribution/points_asset_key" else curve.get("extensions", {}).get("curve_type")
        if curve_type not in LABELS:
            raise ValueError("unknown reviewed curve display semantics")
        formula = "Native image pixels -> independently reviewed axis calibration; original gaps preserved" if provenance.get("extraction_method")=="reviewed-raster-contract-v1" else FORMULA
        row = field_factory(LABELS[curve_type], "曲线数据", None, {**provenance, "formula": formula}, semantic_role="curve_asset")
        row.update({"curveKey": asset_key, "_fieldPath": path})
        result.append(row)
    return result


def verify_locator(index, locator, field, record_key, pdf_sha):
    key = locator.get("sourceEvidenceKey") or locator.get("canonicalEvidenceKey")
    expected = index.get(key)
    if expected is None:
        raise ValueError("unverified curve evidence key")
    for name, actual in (("recordKey", record_key), ("fieldPath", locator.get("fieldPath")),
                         ("page", locator.get("sourcePage", locator.get("page"))), ("pdfSha256", pdf_sha)):
        if expected[name] != actual:
            raise ValueError(f"curve review {name} mismatch")
    if ([round(float(v), 3) for v in locator["bbox"]] != [round(float(v), 3) for v in expected["bbox"]]
            or field.get("value") != expected["value"] or field.get("unit") != expected["unit"]):
        raise ValueError("curve review value or geometry mismatch")
    if expected["curveKey"] and (field.get("curveKey") != expected["curveKey"] or field.get("label",field.get("fieldLabel")) != expected["label"]):
        raise ValueError("curve review asset or label mismatch")
    return expected


def asset_descriptors(index, run_id):
    result = {}
    for entry in index.values():
        key = entry["curveKey"]
        if not key:
            continue
        asset, image = entry["asset"], entry["image"]
        data_name, image_name = asset["sha256"] + ".csv", image["sha256"] + ".png"
        prefix = f"review-assets/{run_id}/curves/"
        box = image["extensions"]["source_region_bbox"]
        # Separate M/L for each real segment: do not bridge dashed gaps.
        path = " ".join(f"M {a[0]:.5f} {a[1]:.5f} L {b[0]:.5f} {b[1]:.5f}" for a, b in entry["series"]["pdf_segments"])
        result[key] = {"label": entry["label"], "pointsUrl": prefix + data_name, "pointsSha256": asset["sha256"],
                       "sourceImageUrl": prefix + image_name, "sourceImageSha256": image["sha256"],
                       "sourceBBox": box, "overlayPath": path, "pointCount": len(entry["series"]["points"]),
                       "xAxis": (entry["curve"].get("extensions",{}).get("curve_calibration") or entry["curve"]).get("x_axis"),
                       "yAxis": (entry["curve"].get("extensions",{}).get("curve_calibration") or entry["curve"]).get("y_axis")}
    return result


def materialize(index, run_assets):
    """Copy exact verified bytes; overlay uses source segment connections."""
    result = asset_descriptors(index,run_assets.name)
    destination = run_assets / "curves"
    destination.mkdir(parents=True, exist_ok=True)
    for entry in index.values():
        if not entry["curveKey"]:
            continue
        descriptor = result[entry["curveKey"]]
        for kind,url_key in (("asset","pointsUrl"),("image","sourceImageUrl")):
            shutil.copy2(entry["run"] / entry[kind]["relative_path"],destination / Path(descriptor[url_key]).name)
    return result


def verify_assets(index, paper, site_root):
    expected = asset_descriptors(index,paper["runId"])
    if paper.get("curves") != expected:
        raise ValueError("curve reviewer assets or overlay mismatch")
    shown=[field.get("evidenceKey") for group in ("mats","mixes") for record in paper.get("records",{}).get(group,[])
           for field in record.get("fields",[]) if field.get("evidenceKey") in index]
    if len(shown)!=len(index) or set(shown)!=set(index):raise ValueError("curve reviewer claim coverage mismatch")
    for descriptor in expected.values():
        for url_key,hash_key in (("pointsUrl","pointsSha256"),("sourceImageUrl","sourceImageSha256")):
            if file_sha(safe_path(site_root,descriptor[url_key])) != descriptor[hash_key]:
                raise ValueError("curve published asset hash mismatch")
