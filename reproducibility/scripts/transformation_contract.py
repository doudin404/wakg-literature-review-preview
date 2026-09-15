"""Deterministic forward/reverse checks for formal transformed WAKG values."""

from __future__ import annotations

import math
from typing import Any


NUMERIC_PATH_MARKERS = (
    "/performance/", "/curing_stages", "/particle_size_distribution/d",
    "/xrd_qxrd/internal_standard/standard_fraction_of_spiked_mixture_percent",
    "/xrd_qxrd/standard_fraction_percent",
)


def _close(left: Any, right: Any, tolerance: float = 1e-9) -> bool:
    try:
        return math.isclose(float(left), float(right), rel_tol=0.0, abs_tol=float(tolerance))
    except (TypeError, ValueError):
        return left == right


def _list_close(left: list[Any], right: list[Any], tolerance: float) -> bool:
    return len(left) == len(right) and all(_close(a, b, tolerance) for a, b in zip(left, right))


def _pointer(record: dict[str, Any], path: str) -> Any:
    current: Any = record
    for part in path.strip("/").split("/"):
        if not part:
            continue
        if isinstance(current, list):
            current = current[int(part)]
        elif isinstance(current, dict):
            current = current[part]
        else:
            raise KeyError(path)
    return current


def _validate_receipt(receipt: dict[str, Any], actual: Any) -> list[str]:
    defects: list[str] = []
    kind = receipt.get("kind")
    inputs = receipt.get("inputs")
    output = receipt.get("output")
    forward = receipt.get("forward_check")
    reverse = receipt.get("reverse_check")
    if receipt.get("formal") is not True or not receipt.get("formula"):
        return ["TRANSFORM_NOT_FORMAL_OR_FORMULA_MISSING"]
    if not isinstance(inputs, (dict, list)) or not isinstance(output, dict) or not isinstance(forward, dict) or not isinstance(reverse, dict):
        return ["TRANSFORM_RECEIPT_INCOMPLETE"]

    tolerance = float(forward.get("tolerance", 0.0))
    reverse_tolerance = float(reverse.get("tolerance", 0.0))
    if kind == "unit_scale":
        computed = float(inputs["value"]) * float(inputs["scale"])
        reconstructed = float(output["value"]) / float(inputs["scale"])
        if not _close(computed, output["value"], tolerance): defects.append("FORWARD_RECOMPUTE_FAILED")
        if not _close(reconstructed, inputs["value"], reverse_tolerance): defects.append("REVERSE_RECOMPUTE_FAILED")
        if not _close(actual, output["value"], tolerance): defects.append("TRANSFORM_OUTPUT_RECORD_DRIFT")
    elif kind == "binary_mass_complement_percent":
        value=float(inputs['value']);total=float(inputs['total_percent'])
        if inputs.get('component_count')!=2 or total!=100 or not math.isfinite(value) or not 0<=value<=100:
            return ['INVALID_BINARY_MASS_BASIS']
        computed=total-value;reconstructed=total-float(output['value'])
        if not _close(computed,output['value'],tolerance):defects.append('FORWARD_RECOMPUTE_FAILED')
        if not _close(actual,output['value'],tolerance):defects.append('TRANSFORM_OUTPUT_RECORD_DRIFT')
        if not _close(reconstructed,value,reverse_tolerance):defects.append('REVERSE_RECOMPUTE_FAILED')
        if not _close(forward.get('computed'),computed,tolerance):defects.append('FORWARD_RECEIPT_DRIFT')
        if not _close(reverse.get('computed'),reconstructed,reverse_tolerance) or not _close(reverse.get('recorded'),value,reverse_tolerance):
            defects.append('REVERSE_RECEIPT_DRIFT')
    elif kind == "ratio_component":
        components = [float(value) for value in inputs["components"]]
        index = int(output["component_index"])
        token = str(inputs["token"])
        if not _close(components[index], output["value"], tolerance): defects.append("FORWARD_RECOMPUTE_FAILED")
        if forward.get("recorded") is None or not _close(forward.get("computed"), forward.get("recorded"), tolerance): defects.append("FORWARD_RECEIPT_DRIFT")
        if reverse.get("computed") != token or reverse.get("recorded") != token: defects.append("REVERSE_RECOMPUTE_FAILED")
        if not _close(actual, output["value"], tolerance): defects.append("TRANSFORM_OUTPUT_RECORD_DRIFT")
    elif kind == "duration_sequence_to_seconds":
        computed = [float(item["value"]) * float(item["scale_to_seconds"]) for item in inputs]
        reconstructed = [float(value) / float(item["scale_to_seconds"]) for value, item in zip(output["values"], inputs)]
        if not _list_close(computed, list(output["values"]), tolerance): defects.append("FORWARD_RECOMPUTE_FAILED")
        if not _list_close(reconstructed, [item["value"] for item in inputs], reverse_tolerance): defects.append("REVERSE_RECOMPUTE_FAILED")
        actual_values = [float(stage["duration_seconds"]) for stage in actual if stage.get("duration_seconds") is not None]
        if not _list_close(actual_values, list(output["values"]), tolerance): defects.append("TRANSFORM_OUTPUT_RECORD_DRIFT")
    elif kind == "ratio_to_fraction_percent":
        components = [float(value) for value in inputs["components"]]
        if len(components) != 2 or any(not math.isfinite(value) or value <= 0 for value in components):
            return ["INVALID_STANDARD_RATIO"]
        fraction = float(output["value"])
        if not math.isfinite(fraction) or not 0 < fraction < 100:
            return ["INVALID_STANDARD_FRACTION"]
        computed = components[0] / sum(components) * 100.0
        reconstructed = fraction / (100.0-fraction)
        if not _close(computed, output["value"], tolerance): defects.append("FORWARD_RECOMPUTE_FAILED")
        if not _close(reconstructed, reverse["recorded"], reverse_tolerance): defects.append("REVERSE_RECOMPUTE_FAILED")
        if not _close(reconstructed, reverse.get("computed"), reverse_tolerance): defects.append("REVERSE_RECEIPT_DRIFT")
        if not _close(actual, output["value"], tolerance): defects.append("TRANSFORM_OUTPUT_RECORD_DRIFT")
    elif kind == "linear_axis_coordinate":
        (p0,v0),(p1,v1)=inputs['anchors']
        source=float(inputs['value']);p0,v0,p1,v1=map(float,(p0,v0,p1,v1))
        if not all(math.isfinite(v) for v in (source,p0,v0,p1,v1)) or p0==p1 or v0==v1:
            return ['INVALID_LINEAR_AXIS']
        computed=v0+(source-p0)/(p1-p0)*(v1-v0)
        reconstructed=p0+(float(output['value'])-v0)/(v1-v0)*(p1-p0)
        if not _close(computed,output['value'],tolerance):defects.append('FORWARD_RECOMPUTE_FAILED')
        if not _close(reconstructed,source,reverse_tolerance):defects.append('REVERSE_RECOMPUTE_FAILED')
        if not _close(actual,output['value'],tolerance):defects.append('TRANSFORM_OUTPUT_RECORD_DRIFT')
        if not _close(forward.get('computed'),computed,tolerance):defects.append('FORWARD_RECEIPT_DRIFT')
        if not _close(reverse.get('computed'),reconstructed,reverse_tolerance) or not _close(reverse.get('recorded'),source,reverse_tolerance):
            defects.append('REVERSE_RECEIPT_DRIFT')
    elif kind == "linear_pixel_y":
        top, bottom = [float(value) for value in inputs["axis_pixel_bounds"]]
        minimum, maximum = [float(value) for value in inputs["axis_value_bounds"]]
        pixel = float(inputs["value"])
        computed = round(minimum + (bottom - pixel) / (bottom - top) * (maximum - minimum), 3)
        reconstructed = bottom - (float(output["value"]) - minimum) / (maximum - minimum) * (bottom - top)
        if not _close(computed, output["value"], tolerance): defects.append("FORWARD_RECOMPUTE_FAILED")
        if not _close(reconstructed, pixel, reverse_tolerance): defects.append("REVERSE_RECOMPUTE_FAILED")
        if not _close(actual, output["value"], tolerance): defects.append("TRANSFORM_OUTPUT_RECORD_DRIFT")
    elif kind == "log_axis_percentile":
        left, right = [float(value) for value in inputs["axis_pixel_bounds"]]
        log_min, log_max = [float(value) for value in inputs["axis_log10_bounds"]]
        pixel = float(inputs["value"])
        computed = 10 ** (log_min + (pixel - left) / (right - left) * (log_max - log_min))
        reconstructed = left + (math.log10(float(output["value"])) - log_min) / (log_max - log_min) * (right - left)
        if not _close(computed, output["value"], tolerance): defects.append("FORWARD_RECOMPUTE_FAILED")
        if not _close(reconstructed, pixel, reverse_tolerance): defects.append("REVERSE_RECOMPUTE_FAILED")
        if not _close(actual, output["value"], tolerance): defects.append("TRANSFORM_OUTPUT_RECORD_DRIFT")
    else:
        defects.append(f"UNKNOWN_TRANSFORM_KIND:{kind}")

    if kind != "ratio_component":
        recorded_forward = forward.get("recorded")
        recorded_reverse = reverse.get("recorded")
        if isinstance(recorded_forward, list):
            if not _list_close(recorded_forward, list(output.get("values") or []), tolerance): defects.append("FORWARD_RECEIPT_DRIFT")
        elif recorded_forward is not None and not _close(recorded_forward, output.get("value"), tolerance):
            defects.append("FORWARD_RECEIPT_DRIFT")
        if recorded_reverse is None:
            defects.append("REVERSE_RECEIPT_MISSING")
    return sorted(set(defects))


def validate_records(records: dict[str, Any]) -> dict[str, Any]:
    defects: list[dict[str, Any]] = []
    checked = 0
    for record_type, records_key in (("mat", "mats"), ("mix", "mixes")):
        for record in records.get(records_key) or []:
            record_key = str(record.get(f"{record_type}_key"))
            for path, provenance in (record.get("field_provenance") or {}).items():
                formula = provenance.get("formula") if isinstance(provenance, dict) else None
                try:
                    actual = _pointer(record, path)
                except (KeyError, IndexError, TypeError, ValueError):
                    actual = None
                is_numeric_output = isinstance(actual, (int, float)) or (
                    "/curing_stages" in str(path) and isinstance(actual, list)
                    and any(isinstance(stage, dict) and stage.get("duration_seconds") is not None for stage in actual)
                )
                is_numeric_transform = bool(formula) and is_numeric_output and any(marker in str(path) for marker in NUMERIC_PATH_MARKERS)
                if not is_numeric_transform:
                    continue
                receipt = provenance.get("transformation")
                if not isinstance(receipt, dict):
                    defects.append({"record_key": record_key, "field_path": path, "kind": "FORMAL_TRANSFORM_RECEIPT_MISSING"})
                    continue
                try:
                    receipt_defects = _validate_receipt(receipt, actual)
                except (KeyError, IndexError, TypeError, ValueError, ZeroDivisionError) as error:
                    receipt_defects = [f"TRANSFORM_VALIDATION_ERROR:{type(error).__name__}"]
                checked += 1
                defects.extend({"record_key": record_key, "field_path": path, "kind": kind} for kind in receipt_defects)
            for parameter in (((record.get("modules") or {}).get("materials") or {}).get("extensions") or {}).get("reported_parameters") or []:
                receipt = parameter.get("transformation")
                if not isinstance(receipt, dict) or receipt.get("formal") is not True:
                    continue
                checked += 1
                for kind in _validate_receipt(receipt, parameter.get("value")):
                    defects.append({"record_key": record_key, "field_path": f"reported_parameters/{parameter.get('parameter_key')}", "kind": kind})
    return {"schema_version": 1, "checked_formal_transformations": checked, "defects": defects, "verdict": "PASS" if not defects else "REJECT"}
