"""Inspect DOCX supplementary material and digitize simple PSD curves.

The module is deterministic and source-bound.  It reads OOXML directly so the
pipeline does not depend on Microsoft Word or LibreOffice.  Scientific curve
labels are supplied through an agent-reviewed semantic binding whose hashes
must match the DOCX member and embedded image; pixel geometry and D-values are
then derived without a model call.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import copy
import csv
import os
import re
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any
import xml.etree.ElementTree as ET

import numpy as np
from PIL import Image
from marker_geometry import marker_center
from marker_formulations import resolve_formulations
from source_specimen_variants import split as split_specimen_variants
from source_concrete_context import enrich as enrich_concrete_context


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PR = "http://schemas.openxmlformats.org/package/2006/relationships"
C = "http://schemas.openxmlformats.org/drawingml/2006/chart"
NS = {"w": W, "a": A, "r": R, "pr": PR, "c": C}
TABLE_CAPTION_RE = re.compile(r"^Table\s+(S\d+[A-Za-z]?)\b\s*(.*)$", re.I)
FIGURE_CAPTION_RE = re.compile(r"^Fig(?:ure)?\.?\s+(S\d+[A-Za-z]?)\b\s*(.*)$", re.I)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return sha256_bytes(compact(value).encode("utf-8"))


def _text(node: ET.Element) -> str:
    return " ".join("".join(item.text or "" for item in node.findall(".//w:t", NS)).split())


def _relationships(archive: zipfile.ZipFile) -> dict[str, str]:
    root = ET.fromstring(archive.read("word/_rels/document.xml.rels"))
    result: dict[str, str] = {}
    for rel in root.findall("pr:Relationship", NS):
        rel_id = rel.attrib.get("Id")
        target = rel.attrib.get("Target")
        if not rel_id or not target or rel.attrib.get("TargetMode") == "External":
            continue
        member = PurePosixPath("word") / PurePosixPath(target)
        normalized = PurePosixPath(*[part for part in member.parts if part not in {".", ""}])
        if ".." in normalized.parts or not str(normalized).startswith("word/"):
            raise ValueError(f"unsafe DOCX relationship target: {target}")
        result[rel_id] = str(normalized)
    return result


def _table_rows(table: ET.Element) -> list[list[str]]:
    rows: list[list[str]] = []
    for row in table.findall("./w:tr", NS):
        rows.append([_text(cell) for cell in row.findall("./w:tc", NS)])
    return rows


def classify_caption(caption: str) -> str:
    folded = caption.casefold()
    if "chemical composition" in folded or "xrf" in folded:
        return "XRF"
    if "particle size distribution" in folded or re.search(r"\bpsd\b", folded):
        return "PSD"
    if "xrd" in folded or "diffraction" in folded:
        return "XRD_QXRD"
    if any(term in folded for term in ("compressive", "flexural", "setting time", "flow diameter", "slump")):
        return "PERFORMANCE"
    if "mixture proportion" in folded:
        return "MIX_FORMULATION"
    if "porosity" in folded or "density" in folded or "water absorption" in folded:
        return "PHYSICAL_PROPERTY"
    if any(term in folded for term in ("carbon emission", "cost", "system boundar")):
        return "OUT_OF_SCOPE_LCA"
    return "SUPPLEMENT_OTHER"


def inspect_docx(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        names = set(archive.namelist())
        required = {"[Content_Types].xml", "word/document.xml", "word/_rels/document.xml.rels"}
        if not required <= names:
            raise ValueError("invalid DOCX package")
        relationships = _relationships(archive)
        root = ET.fromstring(archive.read("word/document.xml"))
        body = root.find("w:body", NS)
        if body is None:
            raise ValueError("DOCX has no body")
        tables: list[dict[str, Any]] = []
        figures: list[dict[str, Any]] = []
        pending_table: dict[str, str] | None = None
        pending_visuals: list[dict[str, Any]] = []
        pending_figure: dict[str, str] | None = None
        paragraphs: list[dict[str, Any]] = []
        for body_index, child in enumerate(body):
            tag = child.tag.rsplit("}", 1)[-1]
            if tag == "p":
                text = _text(child)
                ids = [(blip.attrib.get(f"{{{R}}}embed"), "image") for blip in child.findall(".//a:blip", NS)]
                ids.extend((chart.attrib.get(f"{{{R}}}id"), "chart") for chart in child.findall(".//c:chart", NS))
                for rel_id, content_kind in [(item, kind) for item, kind in ids if item]:
                    member = relationships.get(str(rel_id))
                    if not member or member not in names:
                        raise ValueError(f"missing embedded image for relationship {rel_id}")
                    data = archive.read(member)
                    visual = {"body_index": body_index, "relationship_id": rel_id, "member": member, "sha256": sha256_bytes(data), "bytes": len(data), "content_kind": content_kind}
                    if pending_figure is not None:
                        caption = pending_figure["caption"]
                        figures.append({"label": pending_figure["label"], "caption": caption, "modality": classify_caption(caption), **visual})
                        pending_figure = None
                    else:
                        pending_visuals.append(visual)
                if text:
                    paragraphs.append({"body_index": body_index, "text": text})
                    table_match = TABLE_CAPTION_RE.match(text)
                    figure_match = FIGURE_CAPTION_RE.match(text)
                    if table_match:
                        pending_table = {"label": f"Table {table_match.group(1).upper()}", "caption": text}
                    if figure_match:
                        caption = text
                        descriptor = {"label": f"Fig. {figure_match.group(1).upper()}", "caption": caption}
                        if pending_visuals:
                            visual = pending_visuals.pop(0)
                            figures.append({**descriptor, "modality": classify_caption(caption), **visual})
                        else:
                            if pending_figure is not None:
                                raise ValueError(f"consecutive figure captions without an image: {pending_figure['caption']} | {caption}")
                            pending_figure = descriptor
            elif tag == "tbl":
                if pending_table is None:
                    raise ValueError(f"supplement table at body index {body_index} has no caption")
                rows = _table_rows(child)
                from docx_table_grid import layout
                layout_grid = layout(child, _text)
                table = {
                    **pending_table,
                    "body_index": body_index,
                    "modality": classify_caption(pending_table["caption"]),
                    "rows": rows,
                    "rows_sha256": digest(rows),
                    "layout_grid": layout_grid,
                    "layout_grid_sha256": digest(layout_grid),
                }
                tables.append(table)
                pending_table = None
        if pending_visuals:
            raise ValueError("embedded supplement visuals remain unbound to captions")
        if pending_figure is not None:
            raise ValueError(f"supplement figure caption remains unbound: {pending_figure['caption']}")
        result = {
            "schema_version": 1,
            "kind": "DOCX_SUPPLEMENT_INVENTORY",
            "docx": {"sha256": sha256_bytes(raw), "bytes": len(raw)},
            "tables": tables,
            "figures": figures,
            "embedded_workbooks": [
                {"member": name, "sha256": sha256_bytes(archive.read(name)), "bytes": len(archive.read(name))}
                for name in sorted(names)
                if name.startswith("word/embeddings/") and name.casefold().endswith(".xlsx")
            ],
            "paragraphs": paragraphs,
            "paragraphs_sha256": digest(paragraphs),
        }
        result["inventory_sha256"] = digest(result)
        return result


def read_member(path: Path, member: str, expected_sha256: str | None = None) -> bytes:
    with zipfile.ZipFile(path) as archive:
        value = archive.read(member)
    actual = sha256_bytes(value)
    if expected_sha256 and actual != expected_sha256:
        raise ValueError("supplement image hash mismatch")
    return value


def _cached_points(parent: ET.Element | None) -> list[str]:
    if parent is None:
        return []
    indexed: dict[int, str] = {}
    for point in parent.findall(".//c:pt", NS):
        index = int(point.attrib.get("idx", len(indexed)))
        value = point.find("c:v", NS)
        indexed[index] = value.text if value is not None and value.text is not None else ""
    return [indexed[index] for index in sorted(indexed)]


def parse_native_chart(chart_bytes: bytes) -> dict[str, Any]:
    root = ET.fromstring(chart_bytes)
    series = []
    for node in root.findall(".//c:ser", NS):
        names = _cached_points(node.find("c:tx", NS))
        categories = _cached_points(node.find("c:cat", NS))
        values = _cached_points(node.find("c:val", NS))
        plus = _cached_points(node.find("c:errBars/c:plus", NS))
        minus = _cached_points(node.find("c:errBars/c:minus", NS))
        if not names or not categories or not values:
            continue
        series.append({
            "name": names[0],
            "categories": categories,
            "values": [float(value) for value in values],
            "error_plus": [float(value) for value in plus] if plus else None,
            "error_minus": [float(value) for value in minus] if minus else None,
        })
    if not series:
        raise ValueError("native chart has no cached data series")
    result = {"schema_version": 1, "kind": "OOXML_NATIVE_CHART_DATA", "chart_sha256": sha256_bytes(chart_bytes), "series": series}
    result["data_sha256"] = digest(result)
    return result


def _xlsx_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    return ["".join(node.text or "" for node in item.findall(".//s:t", ns)) for item in root.findall("s:si", ns)]


def read_xlsx_cells(xlsx_bytes: bytes, sheet_name: str, references: list[str]) -> dict[str, Any]:
    """Read cached scalar values from a frozen XLSX without recalculation."""
    spreadsheet_ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    office_rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    package_rel = "http://schemas.openxmlformats.org/package/2006/relationships"
    with zipfile.ZipFile(io.BytesIO(xlsx_bytes)) as archive:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        workbook_rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        rels = {node.attrib["Id"]: node.attrib["Target"] for node in workbook_rels.findall(f"{{{package_rel}}}Relationship")}
        target = None
        for sheet in workbook.findall(f".//{{{spreadsheet_ns}}}sheet"):
            if sheet.attrib.get("name") == sheet_name:
                target = rels.get(sheet.attrib.get(f"{{{office_rel}}}id"))
                break
        if target is None:
            raise ValueError(f"embedded workbook sheet not found: {sheet_name}")
        member = str(PurePosixPath("xl") / PurePosixPath(target))
        root = ET.fromstring(archive.read(member))
        shared = _xlsx_shared_strings(archive)
        wanted = {reference.upper() for reference in references}
        result: dict[str, Any] = {}
        for cell in root.findall(f".//{{{spreadsheet_ns}}}c"):
            ref = str(cell.attrib.get("r") or "").upper()
            if ref not in wanted:
                continue
            kind = cell.attrib.get("t")
            value = cell.find(f"{{{spreadsheet_ns}}}v")
            raw = value.text if value is not None else None
            if kind == "s" and raw is not None:
                parsed: Any = shared[int(raw)]
            elif kind == "inlineStr":
                parsed = "".join(node.text or "" for node in cell.findall(f".//{{{spreadsheet_ns}}}t"))
            elif raw is None:
                parsed = None
            else:
                number = float(raw)
                parsed = int(number) if number.is_integer() else number
            result[ref] = parsed
        missing = sorted(wanted - set(result))
        if missing:
            raise ValueError(f"embedded workbook cells missing: {missing}")
        return result


def _axis_bounds(rgb: np.ndarray) -> tuple[int, int, int, int]:
    dark = np.max(rgb, axis=2) < 70
    height, width = dark.shape
    row_counts = dark.sum(axis=1)
    col_counts = dark.sum(axis=0)
    horizontal = [index for index, count in enumerate(row_counts) if count > width * 0.55]
    vertical = [index for index, count in enumerate(col_counts) if count > height * 0.55]
    if len(horizontal) < 2 or len(vertical) < 2:
        raise ValueError("PSD plot axes could not be determined")
    top = min(horizontal)
    bottom = max(horizontal)
    left = min(vertical)
    right = max(vertical)
    if right - left < width * 0.45 or bottom - top < height * 0.45:
        raise ValueError("PSD plot axis bounds are implausible")
    return left, right, top, bottom


def _groups(xs: np.ndarray, counts: np.ndarray) -> list[tuple[int, int, int]]:
    groups: list[list[int]] = []
    for x in sorted(set(int(value) for value in xs if counts[int(value)] >= 2)):
        if not groups or x > groups[-1][-1] + 1:
            groups.append([x])
        else:
            groups[-1].append(x)
    return [(group[0], group[-1], int(counts[group].sum())) for group in groups]


def _curve_mask(rgb: np.ndarray, color: str) -> np.ndarray:
    red, green, blue = (rgb[:, :, index].astype(np.int16) for index in range(3))
    if color == "red":
        return (red > 125) & (red > green * 1.45) & (red > blue * 1.45)
    if color == "blue":
        return (blue > 105) & (blue > red * 1.25) & (blue > green * 1.15)
    if color == "black":
        return (np.maximum.reduce([red, green, blue]) < 105) & ((np.maximum.reduce([red, green, blue]) - np.minimum.reduce([red, green, blue])) < 28)
    raise ValueError(f"unsupported curve color: {color}")


def _rgb_image(image_bytes: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(image_bytes))
    if image.mode == "RGBA":
        background = Image.new("RGBA", image.size, "white")
        background.alpha_composite(image)
        return background.convert("RGB")
    return image.convert("RGB")


def _x_for_percent(mask: np.ndarray, percent: float, bounds: tuple[int, int, int, int], color: str, log_bounds: list[float]) -> tuple[float, float, float, int]:
    left, right, top, bottom = bounds
    y = int(round(bottom - percent / 100.0 * (bottom - top)))
    strip = mask[max(top + 2, y - 3) : min(bottom - 1, y + 4), left + 3 : right - 2]
    xs = np.where(strip)[1] + left + 3
    counts = np.bincount(xs, minlength=right + 1)
    candidates = _groups(xs, counts)
    if not candidates:
        raise ValueError(f"curve has no pixels at D{percent:g}")
    if len(candidates)!=1:
        raise ValueError(f'curve has ambiguous pixel groups at D{percent:g}')
    selected=candidates[0]
    center = (selected[0] + selected[1]) / 2.0
    half_width = max(2.0, (selected[1] - selected[0]) / 2.0 + 1.0)
    log_min, log_max = log_bounds
    value = 10 ** (log_min + (center - left) / (right - left) * (log_max - log_min))
    low = 10 ** (log_min + (center - half_width - left) / (right - left) * (log_max - log_min))
    high = 10 ** (log_min + (center + half_width - left) / (right - left) * (log_max - log_min))
    return value, max(value - low, high - value), center, y


def digitize_psd(image_bytes: bytes, binding: dict[str, Any]) -> dict[str, Any]:
    image_hash = sha256_bytes(image_bytes)
    if binding.get("image_sha256") != image_hash:
        raise ValueError("PSD semantic binding image hash mismatch")
    image = _rgb_image(image_bytes)
    rgb = np.asarray(image)
    bounds = _axis_bounds(rgb)
    calibration=binding.get('axis_calibration') or {}
    if (calibration.get('pixel_bounds')!=list(bounds)
            or calibration.get('y_percent_bounds')!=[0,100]):
        raise ValueError('PSD source axis calibration missing or mismatched')
    log_bounds=calibration.get('x_log10_bounds')
    if (not isinstance(log_bounds,list) or len(log_bounds)!=2
            or not all(math.isfinite(float(v)) for v in log_bounds) or log_bounds[0]>=log_bounds[1]):
        raise ValueError('PSD source log-axis calibration invalid')
    curves = []
    for curve_binding in [*(binding.get('curves') or []),*(binding.get('reference_curves') or [])]:
        color = str(curve_binding["color"]).casefold()
        mask = _curve_mask(rgb, color)
        for exclusion in binding.get('excluded_regions') or []:
            box=exclusion.get('bbox')
            if (exclusion.get('role')!='legend' or not isinstance(box,list) or len(box)!=4
                    or any(not isinstance(v,int) for v in box)
                    or not 0<=box[0]<box[2]<=image.width or not 0<=box[1]<box[3]<=image.height):
                raise ValueError('PSD exclusion region invalid')
            mask[box[1]:box[3],box[0]:box[2]]=False
        d_values: dict[str, float] = {}
        uncertainty: dict[str, float] = {}
        points = [];missing=[];segment=0;previous_percent=None
        for percent in range(1, 100):
            try:
                value, error, x_pixel, y_pixel = _x_for_percent(mask, float(percent), bounds, color,log_bounds)
            except ValueError as exc:
                missing.append({'cumulative_percent':percent,'reason':str(exc)})
                continue
            if previous_percent is None or percent!=previous_percent+1:segment+=1
            if points and value+error<points[-1]['particle_size_um']-points[-1]['uncertainty_um']:
                raise ValueError('PSD nonmonotonic source path; repair source selection')
            points.append({"cumulative_percent": float(percent), "particle_size_um": round(value, 3), "uncertainty_um": round(error, 3),
                           'segment_id':segment,'pixel_x':x_pixel,'pixel_y':y_pixel})
            previous_percent=percent
            if percent in (10,50,90):
                key=f'd{percent}_um';d_values[key]=round(value,3);uncertainty[key]=round(error,3)
        for percent in (10,50,90):
            key=f'd{percent}_um';d_values.setdefault(key,None);uncertainty.setdefault(key,None)
        curves.append({
            "material_label": curve_binding["material_label"],
            "color": color,
            'role':curve_binding.get('role','material'),
            **d_values,
            "uncertainty_um": uncertainty,
            "points": points,
            'missing_percentiles':missing,'segment_count':segment,'interpolated_points':0,
            'review_status':'pending_source_path_review',
        })
    result = {
        "schema_version": 1,
        "kind": "PSD_IMAGE_DIGITIZATION",
        "image_sha256": image_hash,
        "image_size_px": [int(image.width), int(image.height)],
        "axis": {"x_scale": "log10", "x_unit": "um", "x_min": 10**log_bounds[0], "x_max": 10**log_bounds[1], 'x_log10_bounds':log_bounds, "y_name": "cumulative distribution", "y_unit": "%", "y_min": 0.0, "y_max": 100.0, "pixel_bounds": list(bounds)},
        "algorithm": "masked-source-pixel-intersections-v2",
        'excluded_regions':binding.get('excluded_regions') or [],
        "curves": curves,
    }
    result["digitization_sha256"] = digest(result)
    return result


def digitize_xy_markers(image_bytes: bytes, binding: dict[str, Any]) -> dict[str, Any]:
    """Digitize agent-bound marker plots using deterministic pixel geometry."""
    image_hash = sha256_bytes(image_bytes)
    if binding.get("image_sha256") != image_hash:
        raise ValueError("XY semantic binding image hash mismatch")
    image = _rgb_image(image_bytes)
    rgb = np.asarray(image)
    panels = []
    for panel_binding in binding.get("panels") or []:
        left, right, top, bottom = [int(value) for value in panel_binding["pixel_bounds"]]
        x_axis = panel_binding["x_axis"]
        x_min, x_max = float(x_axis["min"]), float(x_axis["max"])
        x_values = [float(value) for value in panel_binding["x_values"]]
        series_rows = []
        for series_binding in panel_binding["series"]:
            color = str(series_binding["color"]).casefold()
            mask = _curve_mask(rgb, color)
            y_axis = series_binding["y_axis"]
            y_min, y_max = float(y_axis["min"]), float(y_axis["max"])
            points = []
            for x_value in x_values:
                expected_x = left + (x_value - x_min) / (x_max - x_min) * (right - left)
                geometry = marker_center(mask, expected_x, top, bottom, panel_binding.get('excluded_regions', []),series_binding.get('erosion_size',3))
                x_pixel, y_pixel = geometry['pixel']
                half_height = geometry['stroke_half_span_px']
                value = y_min + (bottom - y_pixel) / (bottom - top) * (y_max - y_min)
                uncertainty = half_height / (bottom - top) * (y_max - y_min)
                points.append({"x": x_value, "y": round(value, 3), "uncertainty_y": round(uncertainty, 3),
                    "pixel": [x_pixel, round(y_pixel, 3)],'marker_geometry':geometry,
                    'x_from_pixel':x_min+(x_pixel-left)/(right-left)*(x_max-x_min)})
            series_rows.append({
                "name": series_binding["name"],
                "color": color,
                "x_name": x_axis["name"], "x_axis": copy.deepcopy(x_axis),
                "x_unit": x_axis.get("unit"), "y_axis": copy.deepcopy(y_axis),
                "y_unit": y_axis.get("unit"),
                "age_seconds": series_binding.get("age_seconds"),
                "points": points,
            })
        panels.append({"label": panel_binding["label"], "pixel_bounds": [left, right, top, bottom],
            'x_axis':copy.deepcopy(panel_binding['x_axis']),
            'excluded_regions':copy.deepcopy(panel_binding.get('excluded_regions',[])),"series": series_rows})
    result = {
        "schema_version": 1,
        "kind": "XY_MARKER_DIGITIZATION",
        "image_sha256": image_hash,
        "image_size_px": [int(image.width), int(image.height)],
        "algorithm": "source-component-marker-v2",
        "panels": panels,
    }
    result["digitization_sha256"] = digest(result)
    return result


def validate_inventory(inventory: dict[str, Any]) -> dict[str, Any]:
    labels = [item["label"].casefold() for group in ("tables", "figures") for item in inventory.get(group) or []]
    duplicates = sorted({label for label in labels if labels.count(label) > 1})
    failures = []
    if duplicates:
        failures.append("DUPLICATE_SUPPLEMENT_LABELS")
    if inventory.get("inventory_sha256") != digest({key: value for key, value in inventory.items() if key != "inventory_sha256"}):
        failures.append("INVENTORY_HASH_MISMATCH")
    return {"schema_version": 1, "kind": "DOCX_SUPPLEMENT_VALIDATION", "failures": failures, "verdict": "PASS" if not failures else "REJECT"}


def _fold(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _material(label: str, records: dict[str, Any]) -> dict[str, Any] | None:
    wanted = _fold(label)
    matches = [mat for mat in records.get("mats") or [] if wanted in {_fold(mat.get("custom_material_id")), _fold(mat.get("material_type"))}]
    return matches[0] if len(matches) == 1 else None


def _asset_key(prefix: str, value: str) -> str:
    return f"asset-supp-{prefix}-{value[:16]}"


def _atomic_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(value)
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _add_asset(records: dict[str, Any], asset: dict[str, Any]) -> None:
    assets = records.setdefault("assets", [])
    existing = next((item for item in assets if item.get("asset_key") == asset["asset_key"]), None)
    if existing is not None and existing != asset:
        raise ValueError(f"asset key collision: {asset['asset_key']}")
    if existing is None:
        assets.append(asset)


def _evidence(
    records: dict[str, Any], *, asset_key: str, record_type: str, record_key: str,
    field_path: str, source_label: str, raw_value: str, row: int | None = None,
    column: int | None = None, bbox: list[float] | None = None, method: str,
    coordinate_space: str | None = None,
) -> dict[str, Any]:
    key = "ev-supp-" + digest([asset_key, record_key, field_path, source_label, row, column, bbox, raw_value])[:24]
    link = {
        "evidence_key": key,
        "asset_key": asset_key,
        "record_type": record_type,
        "record_key": record_key,
        "field_path": field_path,
        # Page numbers are local to the cited asset. DOCX tables and standalone
        # figure images are single logical source surfaces until the optional
        # Word-render projection binds them to physical supplement PDF pages.
        "page": 1,
        "section": "Supplementary Material",
        "table_number": source_label if source_label.startswith("Table") else None,
        "figure_number": source_label if source_label.startswith("Fig") else None,
        "bbox": bbox,
        "snippet": raw_value,
        "snippet_sha256": sha256_bytes(raw_value.encode("utf-8")),
        "extraction_method": method,
        "confidence": 0.99 if method == "docx-table-cell-v1" else 0.95,
        "paper_key": (records.get("papers") or [{}])[0].get("paper_key"),
        "source_locator": {"coordinate_space": coordinate_space or ("image_pixels" if bbox else "docx_table"), "row": row, "column": column},
    }
    links = records.setdefault("evidence_links", [])
    existing = next((item for item in links if item.get("evidence_key") == key), None)
    if existing is not None and existing != link:
        raise ValueError(f"evidence key collision: {key}")
    if existing is None:
        links.append(link)
    return link


def _prov(link: dict[str, Any], original_value: Any, original_unit: str | None, formula: str | None = None) -> dict[str, Any]:
    return {
        "evidence_key": link["evidence_key"],
        "original_value": str(original_value),
        "original_unit": original_unit,
        "formula": formula,
        "extraction_method": link["extraction_method"],
        "confidence": link["confidence"],
        "review_status": "confirmed",
    }


def _pixel_y_transformation(panel: dict[str, Any], series: dict[str, Any], point: dict[str, Any], formula: str) -> dict[str, Any]:
    _, y_pixel = [float(value) for value in point["pixel"]]
    _, _, top, bottom = [float(value) for value in panel["pixel_bounds"]]
    y_axis = series["y_axis"]
    y_min, y_max = float(y_axis["min"]), float(y_axis["max"])
    computed = round(y_min + (bottom - y_pixel) / (bottom - top) * (y_max - y_min), 3)
    recorded = float(point["y"])
    reconstructed_pixel = bottom - (recorded - y_min) / (y_max - y_min) * (bottom - top)
    reverse_tolerance = 0.0005 / (y_max - y_min) * (bottom - top) + 1e-6
    return {
        "kind": "linear_pixel_y", "formal": True, "formula": formula,
        "inputs": {"value": y_pixel, "unit": "image_px_y", "axis_pixel_bounds": [top, bottom], "axis_value_bounds": [y_min, y_max]},
        "output": {"value": recorded, "unit": series.get("y_unit")},
        "forward_check": {"computed": computed, "recorded": recorded, "tolerance": 0.0005},
        "reverse_check": {"computed": reconstructed_pixel, "recorded": y_pixel, "tolerance": reverse_tolerance},
    }


def _panel_age_transformation(age_seconds: float, panel_label: str) -> dict[str, Any]:
    days = float(age_seconds) / 86400.0
    return {
        "kind": "unit_scale", "formal": True,
        "formula": "seconds = paper panel age in days * 86400",
        "inputs": {"value": days, "unit": "day", "scale": 86400.0, "source_label": panel_label},
        "output": {"value": float(age_seconds), "unit": "s"},
        "forward_check": {"computed": days * 86400.0, "recorded": float(age_seconds), "tolerance": 1e-9},
        "reverse_check": {"computed": float(age_seconds) / 86400.0, "recorded": days, "tolerance": 1e-9},
    }


def _content_item(plan: dict[str, Any], label: str) -> dict[str, Any]:
    matches = [item for item in plan.get("source_items") or [] if item.get("source_scope") == "supplement_content" and item.get("source_label") == label]
    if len(matches) != 1:
        raise ValueError(f"expected one supplement content item for {label}, found {len(matches)}")
    return matches[0]


def _set_disposition(records: dict[str, Any], plan: dict[str, Any], label: str, status: str, **details: Any) -> None:
    item = _content_item(plan, label)
    binding = records.setdefault("extensions", {}).setdefault("extraction_plan", {})
    binding.setdefault("source_item_dispositions", {})[item["item_id"]] = {"status": status, **details}


def _csv_bytes(points: list[dict[str, Any]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=["cumulative_percent", "particle_size_um", "uncertainty_um",'segment_id','pixel_x','pixel_y'], lineterminator="\n")
    writer.writeheader()
    writer.writerows(points)
    return output.getvalue().encode("utf-8")


def _normalize_mix_id(value: Any) -> str:
    return re.sub(r"[^A-Za-z0-9.]+", "", str(value or ""))


def _merge_exact_observations(target: list[dict[str, Any]], source: list[dict[str, Any]]) -> None:
    # Numeric agreement does not establish a duplicate experiment. Preserve
    # specimen, age, method, uncertainty and evidence differences verbatim.
    def identity(item):
        return json.dumps(item, sort_keys=True, ensure_ascii=False, allow_nan=False)
    known = {identity(item) for item in target}
    for item in source:
        key = identity(item)
        if key not in known:
            target.append(copy.deepcopy(item))
            known.add(key)


def _ordered_recipe_candidates(candidates, source_label):
    """Choose a reproducible storage anchor, not a scientific identity verdict.

    A literal source label takes precedence over a routed alias. Remaining
    ties use stable record keys; every context/evidence check still applies.
    """
    keys = [m.get('mix_key') for m in candidates]
    if any(not isinstance(key, str) or not key for key in keys) or len(set(keys)) != len(keys):
        raise ValueError('recipe candidate keys must be unique and nonempty')
    return sorted(candidates, key=lambda m: (
        m.get('modules', {}).get('identity_source_specimen', {}).get('custom_test_id') != source_label,
        m['mix_key']))


def _recipe_identity_key(value):
    # Punctuation, case and non-Latin text can distinguish physical specimens.
    # Only presentation whitespace at the ends is removed for row lookup.
    return '' if value is None else str(value).strip()


def _reconcile_mix_table(records: dict[str, Any], plan: dict[str, Any], table: dict[str, Any], docx_asset_key: str, aliases: dict[str, str], row_review=None) -> None:
    staged = copy.deepcopy(records)
    replacements = _reconcile_mix_table_staged(staged, plan, table, docx_asset_key, aliases, row_review)
    from recipe_merge_evidence import migrate
    migrate(records, staged, replacements, {'source_table': table,
            'source_asset_key': docx_asset_key, 'declared_aliases': aliases})
    records.clear()
    records.update(staged)


def _reconcile_mix_table_staged(records: dict[str, Any], plan: dict[str, Any], table: dict[str, Any], docx_asset_key: str, aliases: dict[str, str], row_review=None) -> None:
    for candidate_key in ('recipe_identity_candidates', 'recipe_column_candidates'):
        extensions = records.get('extensions', {})
        if candidate_key in extensions:
            extensions[candidate_key] = [c for c in extensions[candidate_key]
                if (c.get('source_asset_key'), c.get('source_table')) != (docx_asset_key, table['label'])]
    raw_aliases = aliases
    aliases = {_recipe_identity_key(k): _recipe_identity_key(v) for k, v in raw_aliases.items()}
    if len(aliases) != len(raw_aliases) or any(not k or not v for k, v in aliases.items()):
        raise ValueError('recipe alias identities collide or are empty')
    rows = table["rows"]
    if len(rows) < 2:
        raise ValueError("Source MIX table layout is unsupported")
    from supplement_recipe_selection import recipe_columns
    known_ids = [m.get('modules', {}).get('identity_source_specimen', {}).get('custom_test_id') for m in records.get('mixes', [])]
    data_start, source_columns = recipe_columns(table, known_ids + list(aliases.values()), _recipe_identity_key)
    from recipe_row_review import packet, classifications, verify_supporting_sources
    verify_supporting_sources(row_review, records.get('assets', []))
    attachments = [a for a in records.get('assets', []) if a.get('asset_key') == docx_asset_key]
    source_packet = None
    roles = {}
    if len(attachments) == 1:
        source_packet = packet(table, attachments[0]['sha256'], data_start)
        roles = classifications(source_packet, row_review)
        records.setdefault('extensions', {})['recipe_row_review_packet'] = source_packet
    elif row_review is not None:
        raise ValueError('recipe row review requires unique source attachment')
    target_ids = [_recipe_identity_key(row[0]) for row in rows[data_start:] if row and _recipe_identity_key(row[0])]
    if len(target_ids) != len(set(target_ids)):
        raise ValueError('recipe contains duplicate source identities')
    existing_by_target: dict[str, list[dict[str, Any]]] = {}
    for mix in records.get("mixes") or []:
        identity = _recipe_identity_key((((mix.get("modules") or {}).get("identity_source_specimen") or {}).get("custom_test_id")))
        target = aliases.get(identity, identity)
        existing_by_target.setdefault(target, []).append(mix)
    mats = {_fold(mat.get("custom_material_id")): mat["mat_key"] for mat in records.get("mats") or []}
    parameter_specs = []
    for source in source_columns:
        if source['semantic_status'] != 'MASS_QUANTITY':
            records.setdefault('extensions', {}).setdefault('recipe_column_candidates', []).append({
                'source_table': table['label'], 'source_asset_key': docx_asset_key,
                'column': source['column'], 'header_hierarchy': source['header_hierarchy'],
                'source_values': [{'row': i, 'value': row[source['column']] if source['column'] < len(row) else None}
                                  for i, row in enumerate(rows) if i >= data_start],
                'status': 'COLUMN_SEMANTICS_UNRESOLVED'})
            continue
        label = source['label']
        existing = [p for m in records.get('mixes', []) for p in m.get('modules', {}).get('materials', {}).get('extensions', {}).get('reported_parameters', [])
                    if _fold(p.get('original_label')) == _fold(label)]
        keys = {p['parameter_key'] for p in existing}
        if len(keys) > 1:
            raise ValueError('recipe parameter identity requires reconciliation')
        key = next(iter(keys)) if keys else re.sub(r'[^a-z0-9]+', '_', label.lower()).strip('_')
        parameter_specs.append((source['column'], key, label, source['unit'], _fold(label), source))
    rebuilt: list[dict[str, Any]] = []
    replacements = {}
    for row_number, row in enumerate(rows[data_start:], data_start):
        if not row or not _recipe_identity_key(row[0]):
            continue
        identity = _recipe_identity_key(row[0])
        candidates = _ordered_recipe_candidates(existing_by_target.get(identity) or [], row[0])
        if candidates:
            if row_review is not None and roles.get(row_number, {}).get('role') != 'EXPERIMENTAL_FORMULATION':
                raise ValueError('recipe row classification contradicts existing MIX owner')
            mix = copy.deepcopy(candidates[0])
            replacements.update({candidate['mix_key']: mix['mix_key'] for candidate in candidates})
            for duplicate in candidates[1:]:
                _merge_exact_observations(mix["modules"]["performance"], duplicate["modules"]["performance"])
                _merge_exact_observations(mix["modules"]["characterizations"], duplicate["modules"]["characterizations"])
        else:
            if roles.get(row_number, {}).get('role') != 'EXPERIMENTAL_FORMULATION':
                records.setdefault('extensions', {}).setdefault('recipe_identity_candidates', []).append({
                    'source_table': table['label'], 'source_row': row_number,
                    'source_asset_key': docx_asset_key, 'source_values': copy.deepcopy(row),
                    'identity': identity, 'status': 'SPECIMEN_CONTEXT_UNRESOLVED',
                    'reviewed_role': roles.get(row_number, {}).get('role'),
                    'packet_sha256': source_packet['packet_sha256'] if source_packet else None})
                continue
            if not parameter_specs or len(records.get('papers', [])) != 1:
                raise ValueError('new recipe row requires unique paper and mass columns')
            from source_mix_record import empty_source_mix
            paper_key = records['papers'][0]['paper_key']
            identity_seed = json.dumps([paper_key, docx_asset_key, table['label'], row_number, identity], ensure_ascii=False)
            key = 'mix-source-' + hashlib.sha256(identity_seed.encode()).hexdigest()[:24]
            mix = empty_source_mix(paper_key, key, identity, table['label'], row_number)
            mix['extensions']['recipe_row_classification'] = copy.deepcopy(roles[row_number])
            mix['extensions']['recipe_row_review_packet_sha256'] = source_packet['packet_sha256']
        mix["modules"]["identity_source_specimen"]["custom_test_id"] = identity
        identity_path = "/modules/identity_source_specimen/custom_test_id"
        identity_link = _evidence(records, asset_key=docx_asset_key, record_type="mix", record_key=mix["mix_key"], field_path=identity_path, source_label=table['label'], raw_value=row[0], row=row_number, column=0, method="docx-table-cell-v1")
        mix.setdefault("field_provenance", {})[identity_path] = _prov(identity_link, row[0], None)
        params = []
        refs = []
        for column, key, original_label, unit, mat_alias, source in parameter_specs:
            if column >= len(row):
                raise ValueError('recipe source row/header alignment mismatch')
            raw = str(row[column]).strip()
            value = None if raw in {'', '/', '-', '–', '—'} else float(raw)
            if value is not None and (not math.isfinite(value) or value < 0):
                raise ValueError('recipe mass must be finite and nonnegative')
            path = f"/modules/materials/extensions/reported_parameters/{len(params)}/value"
            link = _evidence(records, asset_key=docx_asset_key, record_type="mix", record_key=mix["mix_key"], field_path=path, source_label=table['label'], raw_value=row[column], row=row_number, column=column, method="docx-table-cell-v1")
            params.append({"parameter_key": key, "original_label": original_label, "value": value, "unit": unit, "original_value": row[column], "original_unit": unit, "status": "reported", "reason": None, "semantic_role": "reported_formulation", "transformation": None, "evidence_key": link["evidence_key"], "extraction_method": link["extraction_method"], "confidence": link["confidence"]})
            mix["field_provenance"][path] = _prov(link, row[column], unit)
            params[-1]['original_unit'] = source['original_unit']
            if value is None:
                params[-1].update(status='missing', reason='source_missing_symbol')
            params[-1]['source_header_hierarchy'] = source['header_hierarchy']
            mix['field_provenance'][path]['original_unit'] = source['original_unit']
            if mat_alias and value is not None and value > 0 and mat_alias in mats:
                refs.append(mats[mat_alias])
        mix["modules"]["materials"]["extensions"]["reported_parameters"] = params
        mix["modules"]["materials"]["mat_refs"] = sorted(set(refs))
        if refs:
            materials_path = "/modules/materials"
            materials_link = _evidence(records, asset_key=docx_asset_key, record_type="mix", record_key=mix["mix_key"], field_path=materials_path, source_label=table['label'], raw_value=" | ".join(row), row=row_number, column=None, method="docx-table-row-v1")
            mix["field_provenance"][materials_path] = _prov(materials_link, " | ".join(row), "kg/m3")
        mix["extensions"].update({"source_table": table['label'], "source_row": row_number, "supplement_reconciled": True})
        rebuilt.append(mix)
    expected_known = [identity for identity in target_ids if existing_by_target.get(identity)]
    if [mix['modules']['identity_source_specimen']['custom_test_id'] for mix in rebuilt
        if mix['modules']['identity_source_specimen']['custom_test_id'] in expected_known] != expected_known:
        raise ValueError('Source MIX reconciliation lost or reordered known rows')
    replaced_keys = {m['mix_key'] for identity in expected_known for m in existing_by_target[identity]}
    records['mixes'] = [m for m in records['mixes'] if m['mix_key'] not in replaced_keys] + rebuilt

    _set_disposition(records, plan, table['label'], ("QUARANTINED" if len(rebuilt) != len(target_ids) else "EXTRACTED"), record_keys=[mix["mix_key"] for mix in rebuilt])
    return replacements


def _require_table_headers(table, header_row, expected):
    """Require semantic column identity after explicit OOXML merge resolution."""
    grid=table.get('layout_grid')
    if not grid or grid.get('status')!='STRUCTURE_RESOLVED':
        raise ValueError('supplement table structure must be resolved before column mapping')
    rows=grid['rows']
    if not 0<=header_row<len(rows):
        raise ValueError('supplement header row missing')
    for column, label in expected.items():
        if column>=len(rows[header_row]) or rows[header_row][column] is None:
            raise ValueError('supplement header column missing')
        cell=rows[header_row][column]
        if _fold(cell['text'])!=_fold(label):
            raise ValueError(f'supplement column identity mismatch: {table["label"]} column {column}')
    return True


def _add_table_s3_characterizations(records: dict[str, Any], plan: dict[str, Any], table: dict[str, Any], asset_key: str) -> None:
    staged = copy.deepcopy(records)
    _add_characterizations_staged(staged, plan, table, asset_key)
    records.clear()
    records.update(staged)


def _add_characterizations_staged(records, plan, table, asset_key):
    headers = [_normalize_mix_id(value) for value in table["rows"][0][1:]]
    if not headers or any(not value for value in headers) or len(headers) != len(set(headers)):
        raise ValueError('characterization specimen headers missing or duplicate')
    by_id = {_normalize_mix_id(mix["modules"]["identity_source_specimen"]["custom_test_id"]): mix
             for mix in records.get("mixes") or []
             if mix['modules']['identity_source_specimen'].get('specimen_type')=='paste'}
    owners = [mix for mix in records.get('mixes', [])
              if mix['modules']['identity_source_specimen'].get('specimen_type') == 'paste']
    if len(by_id) != len(owners):
        raise ValueError('characterization specimen owner ambiguous')
    from characterization_table import LABELS as label_map
    emitted = []
    unresolved = []
    for row_number, row in enumerate(table["rows"][1:], 1):
        if len(row) != len(headers) + 1:
            raise ValueError('characterization row/header alignment mismatch')
        if row[0] not in label_map:
            unresolved.append({'source_asset_key': asset_key, 'source_table': table['label'],
                               'source_row': row_number, 'raw_values': copy.deepcopy(row),
                               'status': 'MEASUREMENT_SEMANTICS_UNRESOLVED'})
            continue
        name, unit = label_map[row[0]]
        for column, (identity, raw) in enumerate(zip(headers, row[1:]), 1):
            value = None if str(raw).strip() in {'', '/', '-', '–', '—'} else float(raw)
            if value is not None and not math.isfinite(value):
                raise ValueError('characterization value must be finite')
            mix = by_id.get(identity)
            if mix is None:
                raise ValueError(f"{table['label']} references missing MIX: {identity}")
            index = len(mix["modules"]["characterizations"])
            links_by_key = {e['evidence_key']: e for e in records.get('evidence_links', [])}
            previous = []
            for position, observation in enumerate(mix['modules']['characterizations']):
                origin = links_by_key.get(observation.get('extensions', {}).get('evidence_key'), {})
                locator = origin.get('source_locator', {})
                if (origin.get('asset_key'), origin.get('table_number'), locator.get('row'), locator.get('column')) == (asset_key, table['label'], row_number, column):
                    previous.append((position, observation))
            if len(previous) > 1:
                raise ValueError('duplicate characterization source cell')
            if previous:
                index, observation = previous[0]
                if (observation.get('name'), observation.get('value'), observation.get('unit'), observation.get('extensions', {}).get('original_value')) != (name, value, unit, raw):
                    raise ValueError('changed characterization source requires reconciliation')
            path = f"/modules/characterizations/{index}/value"
            link = _evidence(records, asset_key=asset_key, record_type="mix", record_key=mix["mix_key"], field_path=path, source_label=table['label'], raw_value=raw, row=row_number, column=column, method="docx-table-cell-v1")
            if not previous:
                mix["modules"]["characterizations"].append({"name": name, "value": value, "unit": unit, "extensions": {"original_value": raw, "evidence_key": link["evidence_key"], "source_label": row[0]}})
            mix["field_provenance"][path] = _prov(link, raw, unit)
            emitted.append(link["evidence_key"])
    if unresolved:
        candidates = records.setdefault('extensions', {}).setdefault('characterization_row_candidates', [])
        for candidate in unresolved:
            if candidate not in candidates:
                candidates.append(candidate)
    _set_disposition(records, plan, table['label'], "QUARANTINED" if unresolved else "EXTRACTED", evidence_keys=emitted)


def _marker_outputs(records, members):
    mixes={m['mix_key']:m for m in records.get('mixes',[])}
    if len(mixes)!=len(records.get('mixes',[])):
        raise ValueError('duplicate marker MIX key')
    links={e['evidence_key']:e for e in records.get('evidence_links',[])}
    result=[]
    for member in members:
        mix=mixes[member['mix_key']];index=member['index']
        row=mix['modules']['performance'][index]
        identity=(row.get('name'),row.get('age_seconds'),row.get('specimen'))
        if sum((item.get('name'),item.get('age_seconds'),item.get('specimen'))==identity
               for item in mix['modules']['performance'])!=1:
            raise ValueError('duplicate or conflicting marker contribution')
        prefix=f'/modules/performance/{index}/'
        provenance={k:v for k,v in mix['field_provenance'].items() if k.startswith(prefix)}
        result.append({'mix_key':mix['mix_key'],'paper_key':mix.get('paper_key'),
            'identity':{key:mix['modules']['identity_source_specimen'].get(key)
                for key in ('custom_test_id','specimen_type','specimens')},'index':index,'row':row,
            'provenance':provenance,'evidence':[links[p['evidence_key']] for _,p in sorted(provenance.items())]})
    return result


def _add_xy_performance(records: dict[str, Any], plan: dict[str, Any], label: str, asset_key: str, digitization: dict[str, Any], identity_by_x: dict[float, str], *, source_caption: dict[str, Any], docx_asset_key: str, formulation_binding=None) -> None:
    """Atomic and idempotent figure contribution; never accept a stale receipt."""
    transactions=records.get('extensions',{}).get('supplement_marker_transactions',{})
    if transactions and transactions.get('schema_version')!=1:
        raise ValueError('unknown marker transaction schema')
    assets={a['asset_key']:a for a in records.get('assets',[])}
    if len(assets)!=len(records.get('assets',[])):
        raise ValueError('duplicate marker source asset key')
    image=assets.get(asset_key);docx=assets.get(docx_asset_key)
    papers=records.get('papers',[])
    if (not image or not docx or image.get('kind')!='supplement_figure' or docx.get('kind')!='supplement'
            or len(papers)!=1 or image.get('paper_key')!=papers[0].get('paper_key')
            or docx.get('paper_key')!=image.get('paper_key')
            or any(not re.fullmatch(r'[0-9a-f]{64}',str(a.get('sha256',''))) for a in (image,docx))
            or digitization.get('image_sha256')!=image['sha256']
            or source_caption.get('sha256')!=image['sha256']
            or image.get('extensions',{}).get('source_docx_asset_key')!=docx_asset_key
            or image.get('extensions',{}).get('figure_number')!=label
            or image.get('extensions',{}).get('caption')!=source_caption.get('caption')):
        raise ValueError('marker source assets or caption identity mismatch')
    if digitization.get('algorithm') == 'source-component-marker-v2':
        if not formulation_binding or formulation_binding.get('figure') != label:
            raise ValueError('marker source formulation binding required')
        assignments=formulation_binding['assignments']
        if {a['x']:a['canonical_test_id'] for a in assignments} != identity_by_x:
            raise ValueError('marker source formulation mapping conflict')
    input_hash=digest({'figure':label,'image':asset_key,'docx':docx_asset_key,
        'source_hashes':[assets.get(key,{}).get('sha256') for key in (asset_key,docx_asset_key)],
        'digitization':digitization,'caption':source_caption,
        'identity_by_x':sorted((float(x),identity) for x,identity in identity_by_x.items()),
        'formulation_binding':formulation_binding})
    previous=transactions.get('figures',{}).get(label)
    if previous:
        if previous.get('formulation_binding') != formulation_binding:
            raise ValueError('marker stored formulation binding changed')
        if previous['input_sha256']!=input_hash:
            raise ValueError('marker source input conflicts with existing contribution')
        try:current=digest(_marker_outputs(records,previous['members']))
        except (KeyError,IndexError,TypeError) as exc:
            raise ValueError('marker contribution missing or structurally changed') from exc
        if current!=previous['output_sha256']:
            raise ValueError('marker contribution changed after publication')
        return
    staged=copy.deepcopy(records);staged_plan=copy.deepcopy(plan)
    counts={m['mix_key']:len(m['modules']['performance']) for m in records.get('mixes',[])}
    _append_xy_performance(staged,staged_plan,label,asset_key,digitization,identity_by_x,
        source_caption=source_caption,docx_asset_key=docx_asset_key,formulation_binding=formulation_binding)
    members=[{'mix_key':m['mix_key'],'index':i} for m in staged['mixes']
        for i in range(counts[m['mix_key']],len(m['modules']['performance']))]
    registry=staged.setdefault('extensions',{}).setdefault('supplement_marker_transactions',{'schema_version':1,'figures':{}})
    registry['figures'][label]={'input_sha256':input_hash,'members':members,'formulation_binding':formulation_binding,
        'output_sha256':digest(_marker_outputs(staged,members))}
    records.clear();records.update(staged)
    plan.clear();plan.update(staged_plan)


def _append_xy_performance(records: dict[str, Any], plan: dict[str, Any], label: str, asset_key: str, digitization: dict[str, Any], identity_by_x: dict[float, str], *, source_caption: dict[str, Any], docx_asset_key: str, formulation_binding=None) -> None:
    if formulation_binding and formulation_binding.get('method') == 'semantic-series-axis-reference-v1':
        if any(not a['observation_transfer_authorized'] for a in formulation_binding['assignments']):
            from marker_formulations import FormulationPending
            raise FormulationPending('reference formulation cannot own specimen observations', formulation_binding)
    caption = source_caption['caption']
    if source_caption['label'] != label:
        raise ValueError('marker caption figure identity mismatch')
    specimen_matches = re.findall(r'\b(mortar|concrete)\b', caption, flags=re.I)
    if len(specimen_matches) != 1:
        raise ValueError('marker caption specimen context ambiguous')
    specimen_kind = specimen_matches[0]
    age_token = re.search(r'\bone-day\b', caption, flags=re.I)
    def caption_evidence(mix, path, token):
        link = _evidence(records, asset_key=docx_asset_key, record_type='mix',
            record_key=mix['mix_key'], field_path=path, source_label=label, raw_value=token,
            row=source_caption['body_index'], method='docx-caption-token-v1', coordinate_space='docx_caption')
        link['extensions'] = {'caption_text': caption, 'caption_body_index': source_caption['body_index']}
        return link
    allowed_keys={a['mix_key'] for a in formulation_binding['assignments']} if formulation_binding else {m['mix_key'] for m in records.get('mixes',[])}
    by_id = {_normalize_mix_id(mix["modules"]["identity_source_specimen"]["custom_test_id"]): mix for mix in records.get("mixes") or [] if mix['mix_key'] in allowed_keys}
    if len(by_id)!=len(allowed_keys):
        raise ValueError('duplicate normalized marker MIX identity')
    emitted = []
    for panel in digitization["panels"]:
        for series in panel["series"]:
            for point in series["points"]:
                identity = identity_by_x.get(float(point["x"]))
                mix = by_id.get(_normalize_mix_id(identity)) if identity else None
                if mix is None:
                    raise ValueError(f"{label} point has no MIX binding: x={point['x']}")
                if any(row.get('name')==series['name'] and row.get('age_seconds')==series.get('age_seconds')
                       and row.get('specimen')==specimen_kind for row in mix['modules']['performance']):
                    raise ValueError('duplicate or conflicting marker observation requires reconciliation')
                index = len(mix["modules"]["performance"])
                path = f"/modules/performance/{index}/value"
                x_pixel, y_pixel = point["pixel"]
                bbox = [x_pixel - 7, y_pixel - 7, x_pixel + 7, y_pixel + 7]
                raw = f"{series['name']} at {series['x_name']}={point['x']} {series.get('x_unit') or ''}"
                formula = "value = inverse linear pixel calibration on agent-reviewed plot axes"
                transformation = _pixel_y_transformation(panel, series, point, formula)
                link = _evidence(records, asset_key=asset_key, record_type="mix", record_key=mix["mix_key"], field_path=path, source_label=label, raw_value=raw, bbox=bbox, method=digitization.get('algorithm','deterministic-agent-bound-marker-v1'))
                # Extraction technique is not the scientific testing method.
                # The selected stroke extent does not establish SD/SE or a total
                # error bound: calibration and marker ambiguity remain separate.
                mix["modules"]["performance"].append({"name": series["name"], "value": point["y"], "unit": series.get("y_unit"), "age_seconds": series.get("age_seconds"), "specimen": specimen_kind, "method": None, "extensions": {"original_value": point["pixel"][1], "original_unit": "image_px_y", "formula": formula, "transformation": transformation,
                    "digitization": {"schema_version": 1, "selected_stroke_half_span": point["uncertainty_y"],
                        'marker_geometry':point.get('marker_geometry'),'x_from_pixel':point.get('x_from_pixel'),
                        "unit": series.get("y_unit"), "scope": "selected_pixel_strip_only",
                        "includes_axis_calibration_uncertainty": False, "experimental_error_semantics": None,
                        "review_status": "pending_source_path_review"},
                    "evidence_key": link["evidence_key"], "extraction_method": link["extraction_method"], "confidence": link["confidence"]}})
                mix["field_provenance"][path] = _prov(link, point["pixel"][1], "image_px_y", formula)
                mix["field_provenance"][path]["transformation"] = transformation
                mix["field_provenance"][path]["review_status"] = "pending_source_path_review"
                if formulation_binding:
                    assignments=formulation_binding['assignments']
                    owners=[(j,a) for j,a in enumerate(assignments) if a['x']==point['x']]
                    if len(owners)!=1 or owners[0][1]['mix_key']!=mix['mix_key'] or owners[0][1]['specimen']!=specimen_kind:
                        raise ValueError('marker formulation owner or specimen conflict')
                    mix['modules']['performance'][-1]['extensions']['formulation_binding']={
                        'figure':label,'assignment_index':owners[0][0],
                        'scope':formulation_binding['scope'],'review_status':'pending_source_path_review'}
                emitted.append(link["evidence_key"])
                specimen_path = f'/modules/performance/{index}/specimen'
                specimen_link = caption_evidence(mix, specimen_path, specimen_kind)
                mix['field_provenance'][specimen_path] = _prov(specimen_link, specimen_kind, None)
                emitted.append(specimen_link['evidence_key'])
                if series.get("age_seconds") is not None:
                    if age_token is None or series['age_seconds'] != 86400 or series['name'] not in {'compressive_strength','flexural_strength'}:
                        raise ValueError('marker age not supported by one-day strength caption')
                    age_path = f"/modules/performance/{index}/age_seconds"
                    age_formula = 'seconds = one-day (1 day) * 86400'
                    age_link = caption_evidence(mix, age_path, age_token.group())
                    mix["field_provenance"][age_path] = _prov(age_link, age_token.group(), "day", age_formula)
                    age_transformation = _panel_age_transformation(float(series['age_seconds']), age_token.group())
                    age_transformation['formula'] = age_formula
                    mix["field_provenance"][age_path]["transformation"] = age_transformation
                    mix["field_provenance"][age_path]["review_status"] = "pending_source_path_review"
                    emitted.append(age_link["evidence_key"])
    _set_disposition(records, plan, label, "EXTRACTED", evidence_keys=emitted, digitization_sha256=digitization["digitization_sha256"])


def _reported_parameter(mix: dict[str, Any], key: str) -> float | None:
    for item in ((((mix.get("modules") or {}).get("materials") or {}).get("extensions") or {}).get("reported_parameters") or []):
        if _fold(item.get("parameter_key")) == _fold(key) and isinstance(item.get("value"), (int, float)):
            return float(item["value"])
    return None


def _physical_mix_index(records: dict[str, Any], categories: list[str]) -> dict[tuple[str, str], dict[str, Any]]:
    cdw_values = [_reported_parameter(mix, "CDW") for mix in records.get("mixes") or []]
    maximum = max(value for value in cdw_values if value is not None)
    category_set = {_fold(value): value for value in categories}
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for mix in records.get("mixes") or []:
        cdw = _reported_parameter(mix, "CDW")
        modulus = _reported_parameter(mix, "Ms")
        if cdw is None or modulus is None:
            raise ValueError("supplement physical-property binding requires CDW and Ms")
        category_key = _fold(f"{int(round(cdw / maximum * 100))}CDW")
        category = category_set.get(category_key)
        if category is None:
            raise ValueError(f"CDW category not found in supplement: {category_key}")
        series = f"Ms={modulus:.1f}"
        key = (category, series)
        if key in result:
            raise ValueError(f"duplicate supplement physical-property MIX binding: {key}")
        result[key] = mix
    return result


def _add_characterization(
    records: dict[str, Any], mix: dict[str, Any], *, name: str, value: float, unit: str,
    age_seconds: int | None, uncertainty: float | None, asset_key: str, label: str,
    raw_value: str, row: int, column: int, method: str, bbox: list[float] | None = None,
    coordinate_space: str | None = None, source_binding: dict[str, Any] | None = None,
) -> str:
    index = len(mix["modules"]["characterizations"])
    path = f"/modules/characterizations/{index}/value"
    link = _evidence(records, asset_key=asset_key, record_type="mix", record_key=mix["mix_key"], field_path=path, source_label=label, raw_value=raw_value, row=row, column=column, bbox=bbox, method=method, coordinate_space=coordinate_space)
    binding = dict(source_binding or {})
    binding_formula = None
    if binding:
        binding_formula = (
            f"value = {label} category '{binding['category']}' x series '{binding['series']}' "
            f"at source row {int(row) + 1}, column {int(column) + 1}"
        )
    extensions = {
        "original_value": raw_value, "original_unit": unit, "uncertainty": uncertainty,
        "evidence_key": link["evidence_key"], "source_figure": label,
        "formula": binding_formula, "source_binding": binding or None,
        "extraction_method": method, "confidence": link["confidence"],
    }
    mix["modules"]["characterizations"].append({"name": name, "value": value, "unit": unit, "age_seconds": age_seconds, "extensions": extensions})
    mix.setdefault("field_provenance", {})[path] = _prov(link, raw_value, unit, binding_formula)
    mix["field_provenance"][path]["source_binding"] = binding or None
    return link["evidence_key"]


def _enrich_physical_properties(
    records: dict[str, Any], plan: dict[str, Any], docx_path: Path, inventory: dict[str, Any],
    semantic_bindings: dict[str, Any], asset_output_dir: Path,
) -> dict[str, Any]:
    result = copy.deepcopy(records)
    paper_key = result["papers"][0]["paper_key"]
    docx_key = _asset_key("docx", inventory["docx"]["sha256"])
    _add_asset(result, {"asset_key": docx_key, "paper_key": paper_key, "kind": "supplement", "relative_path": str(docx_path.resolve()), "sha256": inventory["docx"]["sha256"], "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "extensions": {"inventory_sha256": inventory["inventory_sha256"]}})
    from supplement_figure_routing import select_bound_figure
    s1, binding = select_bound_figure(inventory, semantic_bindings, "native_chart_cache")
    if s1.get("content_kind") != "chart":
        raise ValueError("bound source must be an OOXML native chart")
    chart_bytes = read_member(docx_path, s1["member"], s1["sha256"])
    chart = parse_native_chart(chart_bytes)
    chart_path = asset_output_dir / ("chart-" + s1["sha256"][:16] + ".xml")
    _atomic_bytes(chart_path, chart_bytes)
    chart_key = _asset_key("chart", s1["sha256"])
    _add_asset(result, {"asset_key": chart_key, "paper_key": paper_key, "kind": "supplement_chart", "relative_path": f"supplement-assets/{chart_path.name}", "sha256": s1["sha256"], "mime_type": "application/xml", "extensions": {"source_docx_asset_key": docx_key, "figure_number": s1['label'], "caption": s1["caption"], "data_sha256": chart["data_sha256"]}})
    categories = chart["series"][0]["categories"]
    mix_index = _physical_mix_index(result, categories)
    evidence = []

    for series_index, series in enumerate(chart["series"]):
        if not series["name"]:
            continue
        for category_index, (category, value) in enumerate(zip(series["categories"], series["values"])):
            mix = mix_index[(category, series["name"])]
            uncertainty = series["error_plus"][category_index] if series.get("error_plus") else None
            evidence.append(_add_characterization(result, mix, name=binding["metric"], value=float(value), unit=binding["unit"], age_seconds=binding.get("age_seconds"), uncertainty=uncertainty, asset_key=chart_key, label=s1['label'], raw_value=str(value), row=series_index, column=category_index, method="ooxml-native-chart-cache-v1", coordinate_space="ooxml_chart_cache", source_binding={"category": category, "series": series["name"], "source_kind": "OOXML native chart cache"}))
            # The repeated group identity is source-reported by the chart
            # category; it replaces internal table-row placeholders.
            mix["modules"]["identity_source_specimen"]["custom_test_id"] = category
    _set_disposition(result, plan, s1['label'], "EXTRACTED", evidence_keys=evidence, data_sha256=chart["data_sha256"])
    next(a for a in result['assets'] if a['asset_key']==chart_key)['extensions'].update({
        'review_status':'confirmed','validation_method':'ooxml-native-chart-cache-v1',
        'semantic_bindings_sha256':digest(semantic_bindings)})

    s2, s2_binding = select_bound_figure(inventory, semantic_bindings, "embedded_workbook")
    workbook_member = s2_binding["workbook_member"]
    workbook = next((item for item in inventory.get("embedded_workbooks") or [] if item["member"] == workbook_member), None)
    if workbook is None or workbook["sha256"] != s2_binding["workbook_sha256"]:
        raise ValueError("embedded workbook binding mismatch")
    workbook_bytes = read_member(docx_path, workbook_member, workbook["sha256"])
    refs = [cell for row in s2_binding["series_rows"] for key in ("name_cell", "value_cells", "error_cells") for cell in ([row[key]] if isinstance(row[key], str) else row[key])]
    cells = read_xlsx_cells(workbook_bytes, s2_binding["sheet"], refs)
    workbook_path = asset_output_dir / ("workbook-" + workbook['sha256'][:16] + ".xlsx")
    _atomic_bytes(workbook_path, workbook_bytes)
    workbook_key = _asset_key("xlsx", workbook["sha256"])
    _add_asset(result, {"asset_key": workbook_key, "paper_key": paper_key, "kind": "supplement_data", "relative_path": f"supplement-assets/{workbook_path.name}", "sha256": workbook["sha256"], "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "extensions": {"source_docx_asset_key": docx_key, "figure_number": s2['label'], "sheet": s2_binding["sheet"]}})
    image_bytes = read_member(docx_path, s2["member"], s2["sha256"])
    image_path = asset_output_dir / Path(s2["member"]).name
    _atomic_bytes(image_path, image_bytes)
    image_key = _asset_key("image", s2["sha256"])
    _add_asset(result, {"asset_key": image_key, "paper_key": paper_key, "kind": "supplement_figure", "relative_path": f"supplement-assets/{image_path.name}", "sha256": s2["sha256"], "mime_type": "image/png", "extensions": {"source_docx_asset_key": docx_key, "figure_number": s2['label'], "caption": s2["caption"], "source_data_asset_key": workbook_key}})
    evidence = []
    plot = s2_binding["plot"]
    for series_index, row in enumerate(s2_binding["series_rows"]):
        series_name = str(cells[row["name_cell"]])
        for category_index, (category, value_cell, error_cell) in enumerate(zip(s2_binding["categories"], row["value_cells"], row["error_cells"])):
            value = float(cells[value_cell])
            uncertainty = float(cells[error_cell])
            mix = mix_index[(category, series_name)]
            x = float(s2_binding["bar_centers_px"][category_index][series_index])
            y = float(plot["bottom"]) - (value - float(plot["y_min"])) / (float(plot["y_max"]) - float(plot["y_min"])) * (float(plot["bottom"]) - float(plot["top"]))
            bbox = [x - float(plot["bar_half_width"]), y, x + float(plot["bar_half_width"]), float(plot["bottom"])]
            evidence.append(_add_characterization(result, mix, name=s2_binding["metric"], value=value, unit=s2_binding["unit"], age_seconds=s2_binding.get("age_seconds"), uncertainty=uncertainty, asset_key=image_key, label=s2['label'], raw_value=str(cells[value_cell]), row=series_index, column=category_index, method="embedded-workbook-cache-v1", bbox=[round(v, 3) for v in bbox], coordinate_space="companion_figure_image_pixels", source_binding={"category": category, "series": series_name, "value_cell": value_cell, "source_kind": "embedded workbook plus companion figure"}))
    _set_disposition(result, plan, s2['label'], "EXTRACTED", evidence_keys=evidence, workbook_sha256=workbook["sha256"], image_asset_key=image_key)
    next(a for a in result['assets'] if a['asset_key']==workbook_key)['extensions'].update({
        'review_status':'confirmed','validation_method':'embedded-workbook-cache-v1',
        'semantic_bindings_sha256':digest(semantic_bindings)})
    apply_scope_dispositions(result, plan, inventory, semantic_bindings)
    result.setdefault("extensions", {})["supplement_processing"] = processing_receipt(inventory,semantic_bindings)
    return result


def dependency_receipt(semantic_bindings):
    directory=Path(__file__).resolve().parent
    from supplement_scope_review import configured_reviews
    identity_review_file = semantic_bindings.get('recipe_identity_review_file')
    identity_review_hash = None
    if identity_review_file:
        review_path = (directory.parent / identity_review_file).resolve()
        if not review_path.is_relative_to(directory.parent):
            raise ValueError('recipe identity review outside project')
        identity_review_hash = sha256_bytes(review_path.read_bytes())
    return {"source_context_dependencies": {name:sha256_bytes((directory/name).read_bytes())
                for name in ('source_concrete_context.py','relation_curing_stages.py','source_operation_scope.py','marker_stage_rebuild.py')},
            "recipe_identity_review_sha256": identity_review_hash,
            "configured_scope_reviews": [{k: entry[k] for k in ('path','sha256')}
                for entry in configured_reviews(semantic_bindings, directory.parent)],
            "object_scope_helper_sha256":sha256_bytes((directory/'supplement_scope_review.py').read_bytes()),
            "semantic_bindings_sha256":digest(semantic_bindings),
            "implementation_sha256":digest({name:sha256_bytes((directory/name).read_bytes())
                for name in ('supplement_docx.py','supplement_mixed_table.py','supplement_xrf_cells.py','docx_table_grid.py','supplement_source_objects.py','supplement_recipe_selection.py','source_mix_record.py','recipe_row_review.py','recipe_identity_review.py','recipe_merge_evidence.py','verify_recipe_merge_sources.py','characterization_table.py','supplement_figure_routing.py','raster_curves.py','marker_formulations.py','source_mass_precision.py','source_specimen_variants.py','source_specimen_recipe_fields.py','source_quantity_producer.py','formulation_source_plan.py')})}


def processing_receipt(inventory, semantic_bindings):
    return {**dependency_receipt(semantic_bindings),
            "docx_sha256":inventory['docx']['sha256'],
            "inventory_sha256":inventory['inventory_sha256'],
            "status":"PROCESSED_NOT_ACCEPTANCE",
            "scope_acceptance":"REQUIRES_SOURCE_ITEM_DISPOSITION_REVIEW"}


def enrich_records_with_supplement(
    records: dict[str, Any], plan: dict[str, Any], docx_path: Path, inventory: dict[str, Any],
    semantic_bindings: dict[str, Any], asset_output_dir: Path,
) -> dict[str, Any]:
    result = copy.deepcopy(records)
    raw_docx = docx_path.read_bytes()
    if sha256_bytes(raw_docx) != inventory["docx"]["sha256"]:
        raise ValueError("supplement DOCX/inventory hash mismatch")
    binding_hash = digest(semantic_bindings)
    previous = (result.get("extensions") or {}).get("supplement_processing") or {}
    expected_receipt=processing_receipt(inventory,semantic_bindings)
    if previous == expected_receipt:
        return result
    if previous:
        raise ValueError('stale supplement stage receipt; rebuild from clean pre-supplement input')
    profile = semantic_bindings.get("profile")
    if profile == "physical-properties-v1":
        return _enrich_physical_properties(result, plan, docx_path, inventory, semantic_bindings, asset_output_dir)
    if profile != "soil-multimodal-v1":
        raise ValueError(f"unsupported supplement semantic profile: {profile}")
    paper_key = result["papers"][0]["paper_key"]
    docx_key = _asset_key("docx", inventory["docx"]["sha256"])
    _add_asset(result, {"asset_key": docx_key, "paper_key": paper_key, "kind": "supplement", "relative_path": str(docx_path.resolve()), "sha256": inventory["docx"]["sha256"], "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "extensions": {"inventory_sha256": inventory["inventory_sha256"]}})

    # Resolve the explicit supplement MIX table before binding table/figure observations.
    from supplement_recipe_selection import select_recipe_table
    specimen_ids = [m.get('modules', {}).get('identity_source_specimen', {}).get('custom_test_id')
                    for m in result.get('mixes', [])]
    specimen_ids += list((semantic_bindings.get('mix_identity_aliases') or {}).values())
    table_s4 = select_recipe_table(inventory, specimen_ids, _normalize_mix_id)
    from recipe_row_review import load_review
    from recipe_merge_evidence import RecipeMergeRequired
    from recipe_identity_review import resolve_result as resolve_identity, load_review as load_identity_review, save_resolution, RecipeIdentityRequired
    try:
        identity_resolution = resolve_identity(result, table_s4, semantic_bindings.get('mix_identity_aliases') or {},
                                           load_identity_review(semantic_bindings, Path(__file__).resolve().parents[1]))
        approved_aliases = identity_resolution['approved_aliases']
        if identity_resolution.get('packet_sha256'):
            save_resolution(asset_output_dir.parent, identity_resolution)
        # Identity planning is independent of downstream XRF/MAT completeness.
        # Composition still must succeed before any recipe merge is published.
        extract_composition_tables(result, plan, inventory, docx_key)
        _reconcile_mix_table(result, plan, table_s4, docx_key, approved_aliases, load_review(semantic_bindings, Path(__file__).resolve().parents[1]))
    except RecipeIdentityRequired as exc:
        _atomic_bytes(asset_output_dir.parent / ('recipe-identity-context-' + exc.packet['packet_sha256'][:20] + '.json'),
                      (json.dumps(exc.packet, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode())
        if hasattr(exc, 'resolution'):
            save_resolution(asset_output_dir.parent, exc.resolution)
        raise
    except RecipeMergeRequired as exc:
        _atomic_bytes(asset_output_dir.parent / ('recipe-merge-context-' + exc.packet['packet_sha256'][:20] + '.json'),
                      (json.dumps(exc.packet, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode())
        from verify_recipe_merge_sources import verify as verify_recipe_sources
        proof = verify_recipe_sources(exc.packet)
        _atomic_bytes(asset_output_dir.parent / ('recipe-merge-source-proof-' + exc.packet['packet_sha256'][:20] + '.json'),
                      (json.dumps(proof, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode())
        raise
    from characterization_table import select_table as select_characterization_table
    table_s3 = select_characterization_table(inventory, specimen_ids, _normalize_mix_id)

    image_assets: dict[str, tuple[str, bytes]] = {}
    for figure in inventory["figures"]:
        data = read_member(docx_path, figure["member"], figure["sha256"])
        suffix = Path(figure["member"]).suffix.casefold()
        output = asset_output_dir / f"{figure['label'].replace(' ', '-').replace('.', '').casefold()}{suffix}"
        _atomic_bytes(output, data)
        asset_key = _asset_key("image", figure["sha256"])
        mime = "image/png" if suffix == ".png" else "image/jpeg"
        _add_asset(result, {"asset_key": asset_key, "paper_key": paper_key, "kind": "supplement_figure", "relative_path": f"supplement-assets/{output.name}", "sha256": figure["sha256"], "mime_type": mime, "extensions": {"source_docx_asset_key": docx_key, "source_member": figure["member"], "figure_number": figure["label"], "caption": figure["caption"]}})
        image_assets[figure["label"]] = (asset_key, data)

    from supplement_figure_routing import select_bound_figure
    xrd_figure, xrd_binding = select_bound_figure(inventory, semantic_bindings, 'xrd_curve_image')
    xrd_label = xrd_figure['label']
    # Curve extraction and quantitative phase fractions remain separate claims.
    _set_disposition(result, plan, xrd_label, "QUARANTINED",
                     reason="XRD_CURVE_POINTS_NOT_EXTRACTED_QXRD_IS_SEPARATE",
                     record_keys=[], asset_key=image_assets[xrd_label][0])
    result.setdefault('extensions',{}).setdefault('xrd_curve_candidates',[]).append({
        'source_label':xrd_label,'asset_key':image_assets[xrd_label][0],
        'status':'SOURCE_IMAGE_ONLY_REQUIRES_CURVE_EXTRACTION',
        'qxrd_record_keys':[], 'material_association_status':'REQUIRES_SOURCE_RELATION',
        'source_caption':xrd_figure.get('caption'), 'source_sha256':xrd_figure['sha256'],
        'formal_curve_points':None})
    if xrd_binding.get('intensity_specification'):
        from raster_curves import attach_intensity_candidates
        receipt=attach_intensity_candidates(result,image_assets[xrd_label][0],
                                            xrd_binding['intensity_specification'],asset_output_dir.parent)
        candidate=result['extensions']['xrd_curve_candidates'][-1]
        candidate.update(status='CURVE_GEOMETRY_CANDIDATE_REQUIRES_SOURCE_RELATION',
                         intensity_report_asset_key=receipt['report_asset_key'])

    from supplement_figure_routing import select_bound_figure
    psd_figure, psd_binding = select_bound_figure(inventory, semantic_bindings, "psd_curve_image")
    psd_label = psd_figure["label"]
    psd = digitize_psd(image_assets[psd_label][1], psd_binding)
    report_data=json.dumps(psd,sort_keys=True,ensure_ascii=False,indent=2).encode('utf-8')
    report_hash=sha256_bytes(report_data)
    report_key=_asset_key('psd-report',report_hash)
    _atomic_bytes(asset_output_dir/'fig-s2-digitization.json',report_data)
    _add_asset(result,{'asset_key':report_key,'paper_key':paper_key,'kind':'json','mime_type':'application/json',
        'relative_path':'supplement-assets/fig-s2-digitization.json','sha256':report_hash,
        'extensions':{'schema_version':1,'source_image_asset_key':image_assets[psd_label][0],
                      'role':'digitization_source_report','review_status':'pending_source_path_review',
                      'digitization_sha256':psd['digitization_sha256']}})
    psd_evidence = []
    for curve in psd["curves"]:
        csv_data = _csv_bytes(curve["points"])
        csv_hash = sha256_bytes(csv_data)
        csv_path = asset_output_dir / f"fig-s2-{_fold(curve['material_label'])}-points.csv"
        _atomic_bytes(csv_path, csv_data)
        csv_key = _asset_key("psd", csv_hash)
        _add_asset(result, {"asset_key": csv_key, "paper_key": paper_key, "kind": "csv", "relative_path": f"supplement-assets/{csv_path.name}", "sha256": csv_hash, "mime_type": "text/csv", "extensions": {"source_image_asset_key": image_assets[psd_label][0], "digitization_sha256": psd["digitization_sha256"],
            'review_status':'pending_source_path_review','source_label':curve['material_label'],
            'segment_count':curve['segment_count'],'missing_percentiles':curve['missing_percentiles'],
            'interpolated_points':0,'connect_across_segments':False,
            'source_report_asset_key':report_key,
            'digitization_report_path':'supplement-assets/fig-s2-digitization.json'}})
        if curve['role']=='reference_material_unresolved':
            result.setdefault('extensions',{}).setdefault('supplement_reference_curves',{'schema_version':1,'curves':[]})['curves'].append({
                'source_label':curve['material_label'],'role':curve['role'],'points_asset_key':csv_key,
                'source_image_asset_key':image_assets[psd_label][0],'figure_number':psd_label})
            continue
        mat = _material(curve["material_label"], result)
        if mat is None:
            raise ValueError(f"{psd_label} material binding unresolved: {curve['material_label']}")
        distribution = mat["particle_size_distribution"]
        distribution.update({"reported_curve_type": "cumulative_finer", "points_asset_key": csv_key, "source_image_asset_key": image_assets[psd_label][0], "d10_um": curve["d10_um"], "d50_um": curve["d50_um"], "d90_um": curve["d90_um"], "conversion_review": {"status": "pending_source_path_review", "algorithm": psd["algorithm"], "digitization_sha256": psd["digitization_sha256"], "uncertainty_um": curve["uncertainty_um"]}, "extensions": {"source_figure": psd_label, "axis": psd["axis"], 'missing_percentiles':curve['missing_percentiles'],'connect_across_segments':False}})
        for percent in (10, 50, 90):
            key = f"d{percent}_um"
            x_value = curve[key]
            if x_value is None:continue
            left, right, top, bottom = psd["axis"]["pixel_bounds"]
            log_min,log_max=psd['axis']['x_log10_bounds']
            source_point=next(point for point in curve['points'] if point['cumulative_percent']==percent)
            x_pixel=source_point['pixel_x'];y_pixel=source_point['pixel_y']
            path = f"/particle_size_distribution/{key}"
            formula = f"D{percent} = log-axis x at cumulative distribution {percent}%"
            reconstructed_x_pixel = left + (math.log10(x_value) - log_min) / (log_max-log_min) * (right - left)
            computed_x_value = 10 ** (((x_pixel - left) / (right - left)) * (log_max-log_min) + log_min)
            transformation = {
                "kind": "log_axis_percentile", "formal": True, "formula": formula,
                "inputs": {"value": x_pixel, "unit": "image_px_x", "axis_pixel_bounds": [left, right], "axis_log10_bounds": [log_min,log_max], "cumulative_percent": percent},
                "output": {"value": x_value, "unit": "um"},
                "forward_check": {"computed": computed_x_value, "recorded": x_value, "tolerance": max(0.001, float(curve["uncertainty_um"][key]))},
                "reverse_check": {"computed": reconstructed_x_pixel, "recorded": x_pixel, "tolerance": max(0.01,abs(math.log10(1+0.0005/max(x_value-0.0005,1e-12)))*(right-left)/(log_max-log_min)+1e-6)},
            }
            link = _evidence(result, asset_key=image_assets[psd_label][0], record_type="mat", record_key=mat["mat_key"], field_path=path, source_label=psd_label, raw_value=formula, bbox=[round(x_pixel - 7, 3), round(y_pixel - 7, 3), round(x_pixel + 7, 3), round(y_pixel + 7, 3)], method=psd['algorithm'])
            mat["field_provenance"][path] = _prov(link, x_pixel, "image_px_x", formula)
            mat["field_provenance"][path]["transformation"] = transformation
            mat["field_provenance"][path]['review_status']='pending_source_path_review'
            psd_evidence.append(link["evidence_key"])
    _set_disposition(result, plan, psd_label, "EXTRACTED", evidence_keys=psd_evidence, digitization_sha256=psd["digitization_sha256"])

    from supplement_figure_routing import select_main_scope
    main_caption = select_main_scope(result['assets'], 'panel_specific')
    mortar_caption, mortar_binding = select_bound_figure(inventory, semantic_bindings, 'mortar_marker_image')
    source_chart=digitize_xy_markers(image_assets[mortar_caption['label']][1],mortar_binding)
    concrete_caption, concrete_source = select_bound_figure(inventory, semantic_bindings, 'concrete_marker_image')
    from formulation_source_plan import produce_from_inventory as produce_source_plan
    source_plan_file = semantic_bindings.get('formulation_source_plan_file')
    source_plan = (json.loads(Path(source_plan_file).read_bytes()) if source_plan_file else
        produce_source_plan(result, table_s4, inventory, semantic_bindings,
                            asset_output_dir/'formulation-source', semantic_bindings.get('source_context_request')))
    recipe_binding=resolve_formulations(result,table_s4,main_caption,source_chart,source_plan=source_plan)
    from source_quantity_producer import complete_quantity_stage
    complete_quantity_stage(result,recipe_binding,semantic_bindings,asset_output_dir/'quantity-extraction')
    concrete_chart=digitize_xy_markers(image_assets[concrete_caption['label']][1],concrete_source)
    concrete_binding=resolve_formulations(result,table_s4,concrete_caption,concrete_chart,source_plan=source_plan)
    from source_concrete_context import produce as produce_context_plan
    context_file = semantic_bindings.get('source_context_plan_file')
    context_plan = (json.loads(Path(context_file).read_bytes()) if context_file else
                    produce_context_plan(result,concrete_binding,asset_output_dir/'source-context'))
    from source_operation_scope import produce as produce_operation_scope
    operation_file = semantic_bindings.get('source_operation_plan_file')
    operation_plan = (json.loads(Path(operation_file).read_bytes()) if operation_file else
                      produce_operation_scope(context_plan,asset_output_dir/'operation-scope'))
    enrich_concrete_context(result,concrete_binding,context_plan,operation_plan)
    _add_table_s3_characterizations(result, plan, table_s3, docx_key)
    for caption, chart in ((mortar_caption, source_chart), (concrete_caption, concrete_chart)):
        label = caption['label']
        formulation=resolve_formulations(result,table_s4,caption,chart,source_plan=source_plan)
        mapping={a['x']:a['canonical_test_id'] for a in formulation['assignments']}
        from marker_stage_rebuild import rebuild
        rebuild(result,plan,label,image_assets[label][0],chart,mapping,
            source_caption=caption,docx_asset_key=docx_key,formulation_binding=formulation)

    apply_scope_dispositions(result, plan, inventory, semantic_bindings)
    result.setdefault("extensions", {})["supplement_processing"] = expected_receipt
    return result


def extract_composition_tables(result, plan, inventory, docx_key):
    staged = copy.deepcopy(result)
    _extract_composition_tables(staged, plan, inventory, docx_key)
    result.clear()
    result.update(staged)


def _extract_composition_tables(result, plan, inventory, docx_key):
    # Every explicitly identified composition table enters the same producer.
    from supplement_xrf_cells import select_composition_tables, component_headers
    composition_tables = select_composition_tables(inventory)
    owners = set()
    for table in composition_tables:
        for row in table['rows'][1:]:
            owner = _material(row[0], result)
            if owner is None or owner['mat_key'] in owners:
                raise ValueError('XRF material ownership missing or repeated across source rows')
            owners.add(owner['mat_key'])
    for table_s1 in composition_tables:
        xrf_label = table_s1['label']
        raw_headers = table_s1['rows'][0][1:]
        headers = component_headers(raw_headers)
        xrf_evidence = []
        for row_number, row in enumerate(table_s1["rows"][1:], 1):
            mat = _material(row[0], result)
            if mat is None:
                raise ValueError(f"{xrf_label} material binding unresolved: {row[0]}")
            rows = []
            from supplement_xrf_cells import parse_row, existing_composition_matches
            parsed_values,total=parse_row(headers,row[1:])
            preserve_existing = existing_composition_matches(mat.get('xrf_composition'), headers, parsed_values)
            for column, (component, raw) in enumerate(zip(headers, row[1:]), 1):
                value = parsed_values[column-1]
                path = f"/xrf_composition/rows/{component}"
                link = _evidence(result, asset_key=docx_key, record_type="mat", record_key=mat["mat_key"], field_path=path, source_label=xrf_label, raw_value=raw, row=row_number, column=column, method="docx-table-cell-v1")
                rows.append({"component": component, "original_value": value, "normalized_value": value, "status": "missing" if value is None else "confirmed"})
                if not preserve_existing:
                    mat.setdefault("field_provenance", {})[path] = _prov(link, raw, "wt.%")
                xrf_evidence.append(link["evidence_key"])
            if not preserve_existing:
                mat["xrf_composition"] = {"schema_version": "1.0", "unit": "wt.%", "rows": rows, "original_total": round(total, 3), "normalisation_candidate": None, "extensions": {"source_table": xrf_label, "source_component_headers": raw_headers, "normalization_status": "not_normalized"}}
        _set_disposition(result, plan, xrf_label, "EXTRACTED", evidence_keys=xrf_evidence)


def apply_scope_dispositions(result, plan, inventory, semantic_bindings):
    """Apply attachment-local reviewed scope, retaining unreviewed source objects."""
    from supplement_scope_review import configured_reviews, reviewed_objects
    from supplement_mixed_table import candidates, bind_review
    from supplement_source_objects import assemble, unmatched_plan_items
    objects = assemble(inventory, plan)
    result.setdefault('extensions', {})['supplement_source_objects'] = objects
    result['extensions']['supplement_plan_binding_anomalies'] = unmatched_plan_items(objects, plan)
    configured = configured_reviews(semantic_bindings, Path(__file__).resolve().parents[1])
    object_path = semantic_bindings.get('object_scope_review')
    object_entry = next((r for r in configured if r['path'] == object_path), None)
    bound_ids = {o['plan_source_item_ids'][0] for o in objects if not o['issues']}
    source_items = [s for s in plan.get('source_items', []) if s.get('item_id') in bound_ids]
    resolved = reviewed_objects(object_entry['review'] if object_entry else None,
                               inventory['docx']['sha256'], source_items)
    reviewed_ids = {r['source']['item_id'] for r in resolved}
    result.setdefault('extensions', {})['supplement_scope_candidates'] = [
        {'source_item_id': s['item_id'], 'source_label': s['source_label'],
         'content_sha256': s.get('content_sha256'),
         'source_docx_sha256': inventory['docx']['sha256'],
         'status': 'SCOPE_UNREVIEWED_NOT_EXCLUDED'}
        for s in source_items if s['item_id'] not in reviewed_ids]
    excluded = set()
    for match in resolved:
        source, reviewed = match['source'], match['reviewed']
        if reviewed['decision'] == 'PRESERVE_QUARANTINED_REFERENCE_CANDIDATE':
            _set_disposition(result, plan, source['source_label'], 'QUARANTINED',
                reason=reviewed['reason'], source_content_sha256=source['content_sha256'],
                source_docx_sha256=inventory['docx']['sha256'],
                scope_review_status='SOURCE_SCOPE_VERIFIED', scope_review_sha256=object_entry['sha256'])
            result.setdefault('extensions', {}).setdefault('reviewed_scope_preservations', []).append({
                'source_item_id': source['item_id'], 'source_content_sha256': source['content_sha256'],
                'decision': reviewed['decision'], 'review_sha256': object_entry['sha256'],
                'status': 'QUARANTINED_NOT_FORMAL'})
            continue
        if reviewed['decision'] != 'EXCLUDE_FROM_FORMAL_MAT_MIX':
            raise ValueError('unsupported reviewed scope decision')
        label = source['source_label']
        excluded.add(source['item_id'])
        _set_disposition(result, plan, label, 'NOT_APPLICABLE',
            reason=reviewed['reason'], source_content_sha256=source['content_sha256'],
            source_docx_sha256=inventory['docx']['sha256'],
            scope_review_status='SOURCE_SCOPE_VERIFIED', scope_review_sha256=object_entry['sha256'],
            scope_review_requirements=reviewed['requirements'])
    # Discover mixed ingredient/economic tables by columns, never table number.
    for table_index, table in enumerate(inventory.get('tables', [])):
        if not table.get('rows'):
            continue
        obj = next(o for o in objects if o['kind'] == 'table' and o['inventory_index'] == table_index)
        if obj['issues']:
            # Already preserved in full, even when the planner omitted it.
            continue
        source = next(s for s in source_items if s['item_id'] == obj['plan_source_item_ids'][0])
        if source['item_id'] in excluded:
            continue
        existing = result.get('extensions', {}).get('extraction_plan', {}).get(
            'source_item_dispositions', {}).get(source['item_id'], {})
        if existing.get('status') == 'EXTRACTED':
            continue
        headers = ' '.join(table['rows'][0])
        if not re.search(r'cost|emission|kgCO2|USD', headers, re.I):
            continue
        try:
            candidate = candidates(table)
        except ValueError as error:
            result.setdefault('extensions', {}).setdefault('supplement_table_candidates', []).append({
                'source_item_id': source['item_id'], 'source_docx_sha256': inventory['docx']['sha256'],
                'table': copy.deepcopy(table), 'status': 'TABLE_STRUCTURE_REQUIRES_REVIEW',
                'reason': str(error)})
            _set_disposition(result, plan, table['label'], 'QUARANTINED',
                reason='TABLE_STRUCTURE_REQUIRES_REVIEW')
            continue
        if not any(c['role'] == 'INGREDIENT_MASS' for c in candidate['columns']):
            continue
        candidate['source_docx_sha256'] = inventory['docx']['sha256']
        reviews = [r['review'] for r in configured if r['path'] != object_path
                   and r['review'].get('docx_sha256') == inventory['docx']['sha256']
                   and r['review'].get('rows_sha256') == table['rows_sha256']
                   and r['review'].get('table_label') == table['label']]
        if len(reviews) > 1:
            raise ValueError('ambiguous reference scope review')
        if reviews:
            pdf = next(a for a in result['assets'] if a['kind'] == 'pdf')
            candidate = bind_review(candidate, reviews[0], Path(pdf['relative_path']),
                                    inventory['docx']['sha256'], table['caption'])
        bucket = result.setdefault('extensions', {}).setdefault('reference_formulation_candidates', [])
        if candidate not in bucket:
            bucket.append(candidate)
        _set_disposition(result, plan, table['label'], 'QUARANTINED',
            reason='REFERENCE_FORMULATION_REQUIRES_COLUMN_LEVEL_SCOPE_AND_MATERIAL_BINDING')
