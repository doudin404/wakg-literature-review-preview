"""Independent source-geometry and persisted CSV checks for curve candidates.

Does not call the digitizer. A PASS proves fidelity to the supplied, separately
reviewed axis/legend binding, not correctness of arbitrary semantic bindings.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from collections import Counter
from pathlib import Path

import fitz


def sha(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def convert(position, axis):
    first, last = axis["anchors"]
    fraction = (position - first[0]) / (last[0] - first[0])
    if axis["scale"] == "linear":
        return first[1] * (1 - fraction) + last[1] * fraction
    if axis["scale"] == "log10" and min(first[1], last[1]) > 0:
        return math.exp(math.log(first[1]) * (1 - fraction) + math.log(last[1]) * fraction)
    raise ValueError("invalid calibration")


def contains(point, box, tolerance):
    return box[0] - tolerance <= point[0] <= box[2] + tolerance and box[1] - tolerance <= point[1] <= box[3] + tolerance


def source_segments(page, binding, series):
    result, paths = [], set()
    for index, drawing in enumerate(page.get_drawings()):
        color = drawing.get("color")
        if color is None or len(color) != 3 or any(abs(a - b) > .005 for a, b in zip(color, series["color"])):
            continue
        if len(drawing["items"]) < series.get("min_path_items", 1):
            continue
        for item in drawing["items"]:
            if item[0] != "l":
                continue
            start, end = tuple(item[1]), tuple(item[2])
            if start == end or not all(contains(p, binding["plot_bbox"], .3) for p in (start, end)):
                continue
            if any(contains(p, box, 0) for p in (start, end) for box in binding.get("exclude_boxes", [])):
                continue
            result.append((start, end))
            paths.add(index)
    return result, sorted(paths)


def verify(pdf: Path, specification: dict, report: dict) -> dict:
    defects = []
    checked = 0
    pdf_hash = hashlib.sha256(pdf.read_bytes()).hexdigest()
    if pdf_hash != specification.get("pdf_sha256") or report.get("pdf_sha256") != pdf_hash:
        defects.append("PDF_HASH_MISMATCH")
    if report.get("specification_sha256") != sha(specification):
        defects.append("BINDING_HASH_MISMATCH")
    if len(report.get("panels", [])) != len(specification["panels"]):
        defects.append("PANEL_COUNT_MISMATCH")
    with fitz.open(pdf) as document:
        for binding, panel in zip(specification["panels"], report.get("panels", [])):
            label = f"{binding['figure']}:{binding['panel_label']}"
            if panel.get("panel") != binding or panel.get("binding_sha256") != sha(binding):
                defects.append(f"PANEL_BINDING_MISMATCH:{label}")
                continue
            if len(panel.get("series", [])) != len(binding["series"]):
                defects.append(f"SERIES_COUNT_MISMATCH:{label}")
            for expected, series in zip(binding["series"], panel.get("series", [])):
                sid = f"{label}:{expected['label']}"
                if any(series.get(k) != expected.get(k) for k in ("label", "record_type", "record_label", "color")):
                    defects.append(f"SERIES_IDENTITY_MISMATCH:{sid}")
                segments, paths = source_segments(document[binding["page"] - 1], binding, expected)
                claimed = [(tuple(a), tuple(b)) for a, b in series.get("pdf_segments", [])]
                if not segments or Counter(segments) != Counter(claimed) or series.get("source_path_indices") != paths:
                    defects.append(f"SOURCE_SEGMENTS_MISMATCH:{sid}")
                if series.get("geometry_sha256") != sha(segments):
                    defects.append(f"GEOMETRY_HASH_MISMATCH:{sid}")
                raw = sorted({p for segment in segments for p in segment})
                if series.get("pdf_points") != [list(p) for p in raw]:
                    defects.append(f"SOURCE_VERTICES_MISMATCH:{sid}")
                values = series.get("points", [])
                if len(values) != len(raw) or any(len(data) != 2 for data in values) or any(not math.isfinite(float(v)) or not math.isclose(v, convert(p, axis), abs_tol=1e-9, rel_tol=1e-10)
                                                 for point, data in zip(raw, values) for p, v, axis in zip(point, data, (binding["x_axis"], binding["y_axis"]))):
                    defects.append(f"CALIBRATED_VALUES_MISMATCH:{sid}")
                percentiles = series.get("percentiles", {})
                expected_keys = {"d10_um", "d50_um", "d90_um"} if binding.get("curve_type") == "cumulative_finer" else set()
                if set(percentiles) != expected_keys:
                    defects.append(f"PERCENTILE_SET_MISMATCH:{sid}")
                for key in sorted(expected_keys & set(percentiles)):
                    entry = percentiles[key]
                    percent = int(key[1:-3])
                    axis = binding["y_axis"]
                    (p0, v0), (p1, v1) = axis["anchors"]
                    y_target = p0 + (percent - v0) / (v1 - v0) * (p1 - p0)
                    brackets = [(a, b) for a, b in zip(raw, raw[1:]) if convert(a[1], axis) <= percent < convert(b[1], axis)]
                    if len(brackets) != 1:
                        defects.append(f"PERCENTILE_AMBIGUOUS:{sid}:{key}")
                        continue
                    a, b = brackets[0]
                    x = a[0] + (b[0] - a[0]) * (y_target - a[1]) / (b[1] - a[1])
                    if entry.get("bracketing_pdf_points") != [list(a), list(b)] or not math.isclose(entry.get("value", math.nan), convert(x, binding["x_axis"]), abs_tol=1e-8):
                        defects.append(f"PERCENTILE_VALUE_MISMATCH:{sid}:{key}")
                checked += len(raw)
    return {"schema_version": 1, "scope": "source_geometry_and_calibration_not_semantic_acceptance", "pdf_sha256": pdf_hash,
            "specification_sha256": sha(specification), "report_sha256": sha(report), "checked_vertices": checked,
            "defects": defects, "verdict": "REJECT" if defects else "PASS"}


def verify_run(run: Path, specification: dict) -> dict:
    records = json.loads((run / "generated-records.json").read_text(encoding="utf-8"))
    return verify_payload(records, run, specification)


def verify_payload(records: dict, run: Path, specification: dict) -> dict:
    receipt = records["extensions"]["vector_curve_candidates"]
    report_path = run / receipt["report_path"]
    report = json.loads(report_path.read_text(encoding="utf-8"))
    pdf_asset = next(a for a in records["assets"] if a["kind"] == "pdf" and a["sha256"] == specification["pdf_sha256"])
    pdf = Path(pdf_asset["relative_path"])
    result = verify(pdf if pdf.is_absolute() else run / pdf, specification, report)
    failures = result["defects"]
    if hashlib.sha256(report_path.read_bytes()).hexdigest() != receipt["report_sha256"]:
        failures.append("PERSISTED_REPORT_HASH_MISMATCH")
    assets = {a["asset_key"]: a for a in records["assets"]}
    expected_series = {(p["panel"]["figure"], p["panel"]["panel_label"], s["record_type"], s["record_label"]): s for p in report["panels"] for s in p["series"]}
    bindings = {(p["figure"], p["panel_label"]): p for p in specification["panels"]}
    seen = set()
    for candidate in receipt["items"]:
        key = (candidate["figure_number"], candidate["panel_label"], candidate["record_type"], candidate["record_label"])
        if key not in expected_series or key in seen:
            failures.append(f"CANDIDATE_IDENTITY_MISMATCH:{key}")
            continue
        seen.add(key)
        series = expected_series[key]
        binding = bindings[key[:2]]
        owners = records["mats"] if key[2] == "mat" else records["mixes"]
        matches = [owner for owner in owners if (owner.get("custom_material_id") if key[2] == "mat" else owner["modules"]["identity_source_specimen"].get("custom_test_id")) == key[3]]
        if len(matches) != 1 or candidate["record_key"] != matches[0]["mat_key" if key[2] == "mat" else "mix_key"]:
            failures.append(f"CANDIDATE_OWNER_MISMATCH:{key}")
        if candidate["page"] != binding["page"] or candidate["plot_bbox"] != binding["plot_bbox"] or candidate["binding_sha256"] != sha(binding) or candidate["source_pdf_asset_key"] != pdf_asset["asset_key"]:
            failures.append(f"CANDIDATE_SOURCE_BINDING_MISMATCH:{key}")
        asset = assets[candidate["data_asset_key"]]
        path = run / asset["relative_path"]
        if hashlib.sha256(path.read_bytes()).hexdigest() != asset["sha256"]:
            failures.append(f"CSV_HASH_MISMATCH:{key}")
        with path.open(encoding="utf-8", newline="") as handle:
            reader = csv.reader(handle)
            header = next(reader, None)
            rows = list(reader)
        expected_rows = [[format(v, ".12g") for v in [*point, *raw]] for point, raw in zip(series["points"], series["pdf_points"])]
        if header != ["x", "y", "pdf_x_pt", "pdf_y_pt"] or rows != expected_rows:
            failures.append(f"CSV_VALUES_MISMATCH:{key}")
        if candidate["percentiles"] != series.get("percentiles", {}):
            failures.append(f"CANDIDATE_PERCENTILES_MISMATCH:{key}")
    if seen != set(expected_series):
        failures.append("MISSING_CANDIDATE_SERIES")
    result["checked_csvs"] = len(seen)
    result["verdict"] = "REJECT" if failures else "PASS"
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--bindings", type=Path)
    parser.add_argument("--write", type=Path)
    args = parser.parse_args()
    if args.bindings:
        specification = json.loads(args.bindings.read_text(encoding="utf-8"))
    else:
        from vector_curve_bindings import specification as default_specification
        specification = default_specification()
    result = verify_run(args.run, specification)
    if args.write:
        from vector_curves import atomic_bytes
        atomic_bytes(args.write, (json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"))
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result["verdict"] == "PASS" else 2)
