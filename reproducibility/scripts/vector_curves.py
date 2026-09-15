"""PDF-vector curve digitization with explicit, source-bound axis calibration.

Bindings contain layout/legend semantics, never output scientific samples.
No raster tracing, baseline removal, smoothing, or intensity normalization is
performed. Dashed-line gaps remain explicit and percentile interpolation is
reported separately from actual printed vertices.
"""
from __future__ import annotations

import hashlib
import json
import math
import csv
import io
import os
import tempfile
from pathlib import Path
from typing import Any

import fitz

VERSION = "pdf-vector-curves-v1"


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def coordinate(position: float, axis: dict, reverse: bool = False) -> float:
    (p0, v0), (p1, v1) = axis["anchors"]
    if not all(math.isfinite(float(v)) for v in (position, p0, p1, v0, v1)) or p0 == p1 or v0 == v1:
        raise ValueError("invalid axis anchors")
    scale = axis["scale"]
    if scale not in {"linear", "log10"}:
        raise ValueError("unsupported axis scale")
    if scale == "log10":
        if min(v0, v1) <= 0 or (reverse and position <= 0):
            raise ValueError("log axis requires positive values")
        v0, v1 = math.log10(v0), math.log10(v1)
        if reverse:
            position = math.log10(position)
    if reverse:
        return p0 + (position - v0) / (v1 - v0) * (p1 - p0)
    value = v0 + (position - p0) / (p1 - p0) * (v1 - v0)
    return 10 ** value if scale == "log10" else value


def _inside(point, bounds, pad=0.3):
    return bounds[0] - pad <= point[0] <= bounds[2] + pad and bounds[1] - pad <= point[1] <= bounds[3] + pad


def extract_panel(page: fitz.Page, binding: dict) -> dict:
    bounds = binding["plot_bbox"]
    if len(bounds) != 4 or bounds[0] >= bounds[2] or bounds[1] >= bounds[3]:
        raise ValueError("invalid plot bounds")
    x_axis, y_axis = binding["x_axis"], binding["y_axis"]
    drawings = page.get_drawings()
    results = []
    for series in binding["series"]:
        segments, path_ids, stroke_widths = [], [], []
        for index, drawing in enumerate(drawings):
            if series.get('source_path_indices') is not None and index not in series['source_path_indices']:
                continue
            color = drawing.get("color")
            if color is None or len(color) != 3 or max(abs(a - b) for a, b in zip(color, series["color"])) > 0.005:
                continue
            if len(drawing["items"]) < series.get("min_path_items", 1):
                continue
            for item in drawing["items"]:
                if item[0] != "l":
                    continue
                start, end = tuple(item[1]), tuple(item[2])
                if not _inside(start, bounds) or not _inside(end, bounds):
                    continue
                if any(_inside(start, box, 0) or _inside(end, box, 0) for box in binding.get("exclude_boxes", [])):
                    continue
                if start == end:
                    continue
                segments.append([list(start), list(end)])
                path_ids.append(index)
                stroke_widths.append(drawing.get("width", 0))
        if len(segments) < 15:
            raise ValueError(f"insufficient curve geometry: {series['label']}")
        points = sorted({tuple(p) for segment in segments for p in segment}, key=lambda p: (p[0], p[1]))
        if points[-1][0] - points[0][0] < (bounds[2] - bounds[0]) * 0.85:
            raise ValueError(f"incomplete curve x coverage: {series['label']}")
        calibrated = [[coordinate(x, x_axis), coordinate(y, y_axis)] for x, y in points]
        error = max(abs(coordinate(v, axis, reverse=True) - p) for raw, data in zip(points, calibrated) for p, v, axis in zip(raw, data, (x_axis, y_axis)))
        if error > 1e-7:
            raise ValueError("axis roundtrip failed")
        results.append({
            "label": series["label"], "record_type": series["record_type"],
            "record_label": series["record_label"], "color": series["color"],
            "pdf_points": [list(p) for p in points], "points": calibrated,
            "pdf_segments": segments, "source_path_indices": sorted(set(path_ids)),
            "stroke_width_pt": max(stroke_widths), "axis_roundtrip_max_error_pt": error,
            "geometry_sha256": digest(segments), "smoothing": False,
        })
    return {"algorithm": VERSION, "binding_sha256": digest(binding), "panel": binding, "series": results}


def percentile(series: dict, panel: dict, percent: float) -> dict:
    if panel.get("curve_type") != "cumulative_finer" or panel["y_axis"].get("unit") != "%":
        raise ValueError("percentile requires cumulative finer percent, not differential PSD")
    points = series["pdf_points"]
    y_values = [coordinate(p[1], panel["y_axis"]) for p in points]
    if any(b < a - 0.5 for a, b in zip(y_values, y_values[1:])):
        raise ValueError("nonmonotone cumulative curve")
    crossings = [(a, b) for a, b, va, vb in zip(points, points[1:], y_values, y_values[1:]) if va <= percent < vb]
    if len(crossings) != 1:
        raise ValueError("percentile intersection not unique or not covered")
    a, b = crossings[0]
    if b[0] - a[0] > 0.08 * (panel["plot_bbox"][2] - panel["plot_bbox"][0]):
        raise ValueError("percentile gap too large")
    y = coordinate(percent, panel["y_axis"], reverse=True)
    x = a[0] + (y - a[1]) / (b[1] - a[1]) * (b[0] - a[0])
    value = coordinate(x, panel["x_axis"])
    pad = max(0.3, series["stroke_width_pt"] / 2)
    uncertainty = [coordinate(a[0] - pad, panel["x_axis"]), coordinate(b[0] + pad, panel["x_axis"])]
    return {"percent": percent, "value": value, "unit": panel["x_axis"]["unit"],
            "pdf_intersection": [x, y], "bracketing_pdf_points": [a, b],
            "uncertainty_interval": sorted(uncertainty), "method": "linear-interpolation-in-printed-axis-space",
            "formula": "x_pdf=a_x+(y_target-a_y)/(b_y-a_y)*(b_x-a_x); D=axis_x(x_pdf)",
            "bbox": [min(a[0], x) - pad, min(a[1], b[1], y) - pad, max(b[0], x) + pad, max(a[1], b[1], y) + pad]}


def extract(pdf_path: Path, specification: dict) -> dict:
    pdf_sha = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    if pdf_sha != specification["pdf_sha256"]:
        raise ValueError("vector source PDF hash mismatch")
    with fitz.open(pdf_path) as document:
        panels = []
        for binding in specification["panels"]:
            panel = extract_panel(document[binding["page"] - 1], binding)
            if binding.get("curve_type") == "cumulative_finer":
                for series in panel["series"]:
                    series["percentiles"] = {f"d{p}_um": percentile(series, binding, p) for p in (10, 50, 90)}
            panels.append(panel)
    return {"algorithm": VERSION, "pdf_sha256": pdf_sha, "specification_sha256": digest(specification), "panels": panels}


def atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def enrich_candidates(records: dict, pdf: Path, specification: dict, run_dir: Path) -> dict:
    """Generate source-only candidate assets; never silently promote them.

    A downstream review must bind the coordinate/legend receipt and verify
    the overlay before these assets enter formal MAT/MIX fields.
    """
    report = extract(pdf, specification)
    known = {a["asset_key"]: a for a in records["assets"]}
    pdf_asset = next(a for a in records["assets"] if a["kind"] == "pdf" and a["sha256"] == report["pdf_sha256"])
    candidates = []
    for panel in report["panels"]:
        binding = panel["panel"]
        for series in panel["series"]:
            group = records["mats"] if series["record_type"] == "mat" else records["mixes"]
            matches = [r for r in group if (r.get("custom_material_id") if series["record_type"] == "mat" else r["modules"]["identity_source_specimen"].get("custom_test_id")) == series["record_label"]]
            if len(matches) != 1:
                raise ValueError(f"vector curve record binding not unique: {series['record_label']}")
            owner = matches[0]
            key = owner["mat_key"] if series["record_type"] == "mat" else owner["mix_key"]
            buffer = io.StringIO(newline="")
            writer = csv.writer(buffer, lineterminator="\n")
            writer.writerow(["x", "y", "pdf_x_pt", "pdf_y_pt"])
            for point, raw in zip(series["points"], series["pdf_points"]):
                writer.writerow([format(v, ".12g") for v in [*point, *raw]])
            data = buffer.getvalue().encode("utf-8")
            sha = hashlib.sha256(data).hexdigest()
            asset_key = "asset-vector-" + sha[:24]
            path = f"vector-assets/{sha}.csv"
            atomic_bytes(run_dir / path, data)
            asset = {"asset_key": asset_key, "paper_key": owner["paper_key"], "kind": "csv", "mime_type": "text/csv", "relative_path": path, "sha256": sha,
                     "extensions": {"source_pdf_asset_key": pdf_asset["asset_key"], "figure_number": binding["figure"], "page": binding["page"],
                                    "panel_label": binding["panel_label"], "figure_type": binding["figure_type"], "review_status": "candidate",
                                    "x_axis": binding["x_axis"], "y_axis": binding["y_axis"], "geometry_sha256": series["geometry_sha256"],
                                    "specification_sha256": report["specification_sha256"]}}
            if asset_key in known and known[asset_key] != asset:
                raise ValueError("vector asset identity collision")
            if asset_key not in known:
                records["assets"].append(asset)
                known[asset_key] = asset
            candidates.append({"candidate_key": "vector-candidate-" + digest([key, binding["figure"], binding["panel_label"], sha])[:24],
                               "record_key": key, "record_type": series["record_type"], "record_label": series["record_label"],
                               "data_asset_key": asset_key, "source_pdf_asset_key": pdf_asset["asset_key"], "page": binding["page"],
                               "plot_bbox": binding["plot_bbox"], "figure_number": binding["figure"], "panel_label": binding["panel_label"],
                               "figure_type": binding["figure_type"], "curve_type": binding["curve_type"], "point_count": len(series["points"]),
                               "percentiles": series.get("percentiles", {}), "status": "pending_coordinate_and_overlay_review",
                               "binding_sha256": panel["binding_sha256"], "geometry_sha256": series["geometry_sha256"]})
    report_bytes = (json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    atomic_bytes(run_dir / "vector-assets/extraction-report.json", report_bytes)
    payload = {"algorithm": VERSION, "specification_sha256": report["specification_sha256"], "report_path": "vector-assets/extraction-report.json",
               "report_sha256": hashlib.sha256(report_bytes).hexdigest(), "items": candidates}
    old = records.setdefault("extensions", {}).get("vector_curve_candidates")
    if old is not None and old != payload:
        raise ValueError("vector candidate receipt drift")
    records["extensions"]["vector_curve_candidates"] = payload
    return {"series": len(candidates), "points": sum(c["point_count"] for c in candidates), "percentiles": sum(len(c["percentiles"]) for c in candidates)}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", type=Path, required=True)
    parser.add_argument("--bindings", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = extract(args.pdf, json.loads(args.bindings.read_text(encoding="utf-8")))
    atomic_bytes(args.output, (json.dumps(report, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
    print(json.dumps({"panels": len(report["panels"]), "series": sum(len(p["series"]) for p in report["panels"]), "output": str(args.output)}))
