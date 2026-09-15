"""Build a source-wide extraction plan and close directly reported QXRD facts.

The planner is deliberately source-derived: it scans immutable PDF text and
geometry, inventories WAKG-relevant modalities and supplementary references,
and records a terminal disposition for every detected source item.  It never
uses an earlier records.json as the coverage denominator.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterable

import fitz


PLANNER_VERSION = "wakg-multimodal-plan-v9-explicit-amorphous-total"
TERMINAL_DISPOSITIONS = {
    "EXTRACTED",
    "RESOLVED_REFERENCE",
    "TYPED_ABSENT",
    "NOT_APPLICABLE",
    "QUARANTINED",
    "ACCESS_FAILED",
}
MODALITY_PATTERNS = {
    "XRF": re.compile(r"\bXRF\b|X[- ]ray fluorescence|chemical composition", re.I),
    "PSD": re.compile(r"particle[-\s]+size distribution|\bPSD\b|\bD10\b|\bD50\b|\bD90\b", re.I),
    "XRD_QXRD": re.compile(r"\bQXRD\b|Rietveld|quantitative X[- ]ray|X[- ]ray diffract(?:ion|ograms?)|\bXRD\b|amorphous phase", re.I),
    "FTIR": re.compile(r"FT[- ]?IR|Fourier transform infrared|\bDRIFTS?\s+spectra\b", re.I),
    "NMR": re.compile(r"(?:29\s*Si|27\s*Al).*NMR|nuclear magnetic resonance", re.I),
    "MIX_FORMULATION": re.compile(r"mix(?:ture)? proportion|mix design|mixture composition|water.to.binder|activator", re.I),
    "MIXING_CURING": re.compile(r"mixing|mixed|cur(?:ed|ing)|demould|demold", re.I),
    "PERFORMANCE": re.compile(r"\bstrengths?\b|workability|setting time|slump|flowability|\bflow diameters?\b|\bP\s*[-–]\s*CMOD\b|\bload\s*[-–]\s*(?:displacement|CMOD)\b|\bfracture\s+(?:energy|toughness)\b", re.I),
    "PHYSICAL_PROPERTY": re.compile(r"porosity|water absorption|density|specific surface|BET", re.I),
    "MICROSTRUCTURE": re.compile(r"\bSEM\b|microstructure|micrograph|EDS", re.I),
    "THERMAL": re.compile(r"thermograv|\bTGA\b|\bDTG\b|\bTG\b|\bDSC\b", re.I),
}
SUPPLEMENT_REF_RE = re.compile(r"\b(?:Table|Fig(?:ure)?\.?)[\s\u00a0]*S\d+[A-Za-z]?\b", re.I)
HEADING_RE = re.compile(r"^(?:\d+(?:\.\d+)*\.?\s+)?(?:materials?|experimental|methods?|mix(?:ture)? design|specimen|results?|discussion|conclusions?|appendix)\b", re.I)
MAIN_FIGURE_CAPTION_RE = re.compile(r"^(?:Fig(?:ure)?\.?)\s*(\d+[A-Za-z]?)[\.:]\s*(.+)$", re.I)
MAIN_TABLE_CAPTION_RE = re.compile(r"^(?:Table|Tab\.)\s*(\d+[A-Za-z]?)\s*[\.:]?\s*(.+)$", re.I)
# "List of binders" is a noun-phrase caption, unlike "Table 4 lists binders".
# Keep the prose-reference filter without discarding this ordinary table title.
TABLE_MENTION_VERBS = re.compile(r"^(?:shows?|provides?|presents?|reports?|lists?(?!\s+of\b)|summari[sz]es?|contains?|is|was|can)\b", re.I)
OUT_OF_SCOPE_RE = re.compile(r"\b(?:CO2 emissions?|carbon emissions?|costs?|life cycle|LCA|environmental impact)\b", re.I)
OXIDE_RE = re.compile(r"^(?:(?:[A-Z][a-z]?\d*)+(?:\+(?:[A-Z][a-z]?\d*)+)*|LOI[a-z]?|Others?)$")
NUMBER_RE = re.compile(r"^[-+]?(?:\d+(?:\.\d*)?|\.\d+)$")


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest(value: Any) -> str:
    return digest_bytes(canonical(value).encode("utf-8"))


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


def normalized_text(text: str) -> str:
    # PDF extractors commonly expose discretionary hyphenation as
    # ``word\u00ad\ncontinuation``.  Removing only U+00AD leaves a space in the
    # middle of the word (``con tained``), which breaks semantic patterns.
    text = re.sub(r"\u00ad\s*", "", text)
    return re.sub(r"\s+", " ", text.replace("−", "-").replace("–", "-")).strip()


def _bbox_for_token(page: fitz.Page, token: str) -> list[float] | None:
    hits = page.search_for(token)
    if not hits and token.endswith(".0"):
        hits = page.search_for(token[:-2])
    if not hits:
        return None
    rect = hits[0]
    return [round(float(value), 3) for value in (rect.x0, rect.y0, rect.x1, rect.y1)]


def _context(text: str, start: int, end: int, radius: int = 220) -> str:
    return text[max(0, start - radius) : min(len(text), end + radius)]


def _section_inventory(doc: fitz.Document) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    for page_index, page in enumerate(doc):
        for block in page.get_text("blocks"):
            text = normalized_text(str(block[4]))
            first = text.split(". ", 1)[0] if len(text) < 180 else text.split("\n", 1)[0]
            if 2 <= len(first) <= 140 and HEADING_RE.match(first):
                sections.append({
                    "page": page_index + 1,
                    "text": first,
                    "bbox": [round(float(value), 3) for value in block[:4]],
                })
    unique: dict[tuple[int, str], dict[str, Any]] = {}
    for section in sections:
        unique[(section["page"], section["text"].casefold())] = section
    return list(unique.values())


def _modality_for_context(context: str) -> list[str]:
    return [name for name, pattern in MODALITY_PATTERNS.items() if pattern.search(context)]


def _nearest_modality(text: str, start: int, end: int, radius: int = 180) -> str:
    """Bind a supplement label to the closest scientific modality mention.

    Taking every modality in a broad context window assigned Table S1 to PSD
    merely because Fig. S2 occurred later in the same paragraph.  Distance is
    deterministic and keeps one semantic owner per source reference.
    """
    lower, upper = max(0, start - radius), min(len(text), end + radius)
    candidates: list[tuple[int, str]] = []
    for name, pattern in MODALITY_PATTERNS.items():
        for match in pattern.finditer(text, lower, upper):
            distance = min(abs(match.end() - start), abs(match.start() - end), abs((match.start() + match.end()) // 2 - (start + end) // 2))
            candidates.append((distance, name))
    return min(candidates, key=lambda item: (item[0], item[1]))[1] if candidates else "SUPPLEMENT_OTHER"


def _supplement_items(page: fitz.Page, page_number: int, text: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for match in SUPPLEMENT_REF_RE.finditer(text):
        label = match.group(0).replace("Figure", "Fig.")
        context = _context(text, match.start(), match.end())
        item = {
            "kind": "SUPPLEMENT_REFERENCE",
            "modality": _nearest_modality(text, match.start(), match.end()),
            "source_scope": "supplement",
            "source_label": label,
            "page": page_number,
            "bbox": _bbox_for_token(page, match.group(0)),
            "context_sha256": digest_bytes(context.encode("utf-8")),
            "disposition": "REQUIRES_SUPPLEMENT",
        }
        item["item_id"] = "source-" + digest({key: item[key] for key in ("kind", "modality", "source_scope", "source_label", "page")})[:24]
        items.append(item)
    return items


def _supplement_label_key(value: str) -> str:
    match = re.search(r"\b(table|fig(?:ure)?)\.?\s*s(\d+)", value, re.I)
    if match:
        kind = "table" if match.group(1).casefold() == "table" else "fig"
        return f"{kind}s{match.group(2)}"
    return re.sub(r"\s+", "", value).casefold()


def _source_label_key(value: str) -> str:
    match = re.search(r"\b(table|tab\.|fig(?:ure)?\.?)\s*[-_]?\s*(\d+[A-Za-z]?)", value, re.I)
    if not match:
        return re.sub(r"[^a-z0-9]+", "", value.casefold())
    kind = "table" if match.group(1).casefold().startswith("tab") else "figure"
    return f"{kind}-{match.group(2).casefold()}"


def _caption_blocks(page: fitz.Page) -> Iterable[tuple[Any, ...]]:
    """Split neighboring captions while preserving their own line geometry."""
    for block in page.get_text("dict")["blocks"]:
        if block.get("type") != 0:
            continue
        block_text = "\n".join("".join(span["text"] for span in line["spans"]) for line in block.get("lines", []))
        normalized = normalized_text(block_text)
        if not (MAIN_FIGURE_CAPTION_RE.match(normalized) or MAIN_TABLE_CAPTION_RE.match(normalized)):
            # A line-wrapped prose reference inside an ordinary paragraph is
            # not a new caption even if it happens to start with Table/Fig.
            yield (*block["bbox"], block_text)
            continue
        groups: list[list[dict[str, Any]]] = [[]]
        for line in block.get("lines", []):
            text = normalized_text("".join(span["text"] for span in line["spans"]))
            starts_caption = MAIN_FIGURE_CAPTION_RE.match(text) or MAIN_TABLE_CAPTION_RE.match(text)
            if starts_caption and groups[-1]:
                groups.append([])
            groups[-1].append({"text": text, "bbox": line["bbox"]})
        for group in groups:
            if not group:
                continue
            rect = fitz.Rect(group[0]["bbox"])
            for line in group[1:]:
                rect |= fitz.Rect(line["bbox"])
            yield (*rect, "\n".join(line["text"] for line in group))


def _geometry_union(rectangles: list[fitz.Rect]) -> fitz.Rect:
    """Include zero-width/height PDF strokes, which Rect union discards."""
    if not rectangles:
        raise ValueError("cannot union missing geometry")
    return fitz.Rect(min(r.x0 for r in rectangles), min(r.y0 for r in rectangles),
                     max(r.x1 for r in rectangles), max(r.y1 for r in rectangles))


def _caption_geometry_group(rectangles: list[fitz.Rect], gap: float = 24) -> list[fitz.Rect]:
    """Follow vertically adjacent panels up from the caption, not a fixed height."""
    bottom = max(r.y1 for r in rectangles)
    top = bottom
    selected = []
    while True:
        group = [r for r in rectangles if r.y1 >= top - gap and r.y0 <= bottom]
        new_top = min(r.y0 for r in group)
        selected = group
        if new_top == top:
            return selected
        top = new_top


def _main_caption_items(doc: fitz.Document) -> list[dict[str, Any]]:
    """Inventory caption blocks, not running-text references.

    The source inventory is deliberately established before record extraction.
    A caption-like block must begin with Table/Fig/Figure and figures must carry
    caption punctuation.  Table running-text constructs such as ``Table 2
    shows`` are rejected.  Every retained item has stable PDF geometry.
    """
    items: list[dict[str, Any]] = []
    for page_index, page in enumerate(doc):
        page_rect = page.rect
        image_rects: list[fitz.Rect] = []
        for image in page.get_images(full=True):
            try:
                image_rects.extend(page.get_image_rects(image[0]))
            except Exception:
                continue
        caption_blocks = list(_caption_blocks(page))
        single_figure_page = sum(bool(MAIN_FIGURE_CAPTION_RE.match(normalized_text(str(b[4])))) for b in caption_blocks) == 1
        # A separate table on the page may own nearby raster/vector geometry.
        # Full-width expansion is not safe without an unambiguous figure page.
        single_figure_page = single_figure_page and not any(
            (table := MAIN_TABLE_CAPTION_RE.match(normalized_text(str(b[4]))))
            and not TABLE_MENTION_VERBS.match(table.group(2)) for b in caption_blocks)
        for block in caption_blocks:
            text = normalized_text(str(block[4]))
            figure_match = MAIN_FIGURE_CAPTION_RE.match(text)
            table_match = MAIN_TABLE_CAPTION_RE.match(text)
            kind: str | None = None
            number: str | None = None
            caption: str | None = None
            if figure_match:
                kind, number, caption = "MAIN_FIGURE", figure_match.group(1), figure_match.group(2)
            elif table_match and not TABLE_MENTION_VERBS.match(table_match.group(2)):
                kind, number, caption = "MAIN_TABLE", table_match.group(1), table_match.group(2)
            if kind is None or number is None or caption is None:
                continue
            caption_bbox = [round(float(value), 3) for value in block[:4]]
            modalities = _modality_for_context(caption)
            out_of_scope = bool(OUT_OF_SCOPE_RE.search(caption)) and not modalities
            region_bbox: list[float] | None = None
            if kind == "MAIN_FIGURE":
                caption_rect = fitz.Rect(*caption_bbox)
                previous_captions = [fitz.Rect(*b[:4]) for b in caption_blocks
                                     if float(b[3]) < caption_rect.y0
                                     and (MAIN_FIGURE_CAPTION_RE.match(normalized_text(str(b[4]))) or
                                          ((previous_table := MAIN_TABLE_CAPTION_RE.match(normalized_text(str(b[4])))) and not TABLE_MENTION_VERBS.match(previous_table.group(2))))
                                     and min(float(b[2]), caption_rect.x1) > max(float(b[0]), caption_rect.x0)]
                floor = max((b.y1 for b in previous_captions), default=page_rect.y0)
                candidates = [
                    rect for rect in image_rects
                    if rect.y1 <= caption_rect.y1 + 3
                    and rect.y0 >= floor
                    and caption_rect.y0 - rect.y1 <= page_rect.height * 0.72
                    and (single_figure_page or min(rect.x1, caption_rect.x1) - max(rect.x0, caption_rect.x0) > 0)
                ]
                if candidates:
                    nearest = _geometry_union(_caption_geometry_group(candidates))
                    region_bbox = [round(float(value), 3) for value in (nearest.x0, nearest.y0, nearest.x1, nearest.y1)]
                else:
                    drawing_rects = []
                    for drawing in page.get_drawings():
                        rect = fitz.Rect(drawing["rect"])
                        if (
                            rect.y1 <= caption_rect.y0 + 3
                            and rect.y0 >= floor
                            and caption_rect.y0 - rect.y1 <= page_rect.height * 0.85
                            and (single_figure_page or min(rect.x1, caption_rect.x1) - max(rect.x0, caption_rect.x0) > 0)
                        ):
                            drawing_rects.append(rect)
                    if drawing_rects:
                        connected = _caption_geometry_group(drawing_rects)
                        union = _geometry_union(connected)
                        if union.get_area() >= 400:
                            # Caption overlap selects the plot strokes, but
                            # tick numbers and rotated axis titles commonly
                            # sit outside that horizontal span. Preserve their
                            # immediate context without including the caption.
                            union = fitz.Rect(max(page_rect.x0, union.x0 - 30), max(page_rect.y0, union.y0 - 4),
                                              min(page_rect.x1, union.x1 + 12), min(caption_rect.y0, union.y1 + 4))
                            region_bbox = [round(float(value), 3) for value in (union.x0, union.y0, union.x1, union.y1)]
            source_label = ("Table " if kind == "MAIN_TABLE" else "Fig. ") + number
            item = {
                "kind": kind,
                "modality": modalities or (["OUT_OF_SCOPE"] if out_of_scope else ["SCIENTIFIC_OTHER"]),
                "source_scope": "main",
                "source_label": source_label,
                "source_key": _source_label_key(source_label),
                "page": page_index + 1,
                "bbox": caption_bbox,
                "region_bbox": region_bbox,
                "caption": caption,
                "caption_sha256": digest_bytes(caption.encode("utf-8")),
                "context_sha256": digest_bytes(text.encode("utf-8")),
                "disposition": "NOT_APPLICABLE" if out_of_scope else "EXTRACTABLE_DIRECT",
                "expected_extraction": "STRUCTURED_TABLE" if kind == "MAIN_TABLE" else "FIGURE_ASSET_OR_DIGITIZATION",
            }
            item["item_id"] = "source-" + digest({key: item.get(key) for key in ("kind", "source_key", "page", "caption_sha256")})[:24]
            items.append(item)
    # Some publisher PDFs repeat a caption in accessibility layers. Keep one
    # stable item per label and page; conflicting captions remain distinct by
    # page so that continued tables cannot silently disappear.
    unique: dict[tuple[str, str, int], dict[str, Any]] = {}
    for item in items:
        unique[(item["kind"], item["source_key"], item["page"])] = item
    return sorted(unique.values(), key=lambda item: (item["page"], item["kind"], item["source_key"]))


QXRD_RE = re.compile(
    r"(?P<material>uncalcined soil|calcined soil)"
    r".{0,110}?(?:consisted of|contained)\s+"
    r"(?P<value1>\d+(?:\.\d+)?)\s*%\s*(?P<phase1>quartz(?:\s*\([^)]*\))?)\s+and\s+"
    r"(?P<value2>\d+(?:\.\d+)?)\s*%\s*(?P<phase2>kaolinite(?:\s*\([^)]*\))?|amorphous phases?)",
    re.I,
)
STANDARD_RE = re.compile(
    r"(?:using|with)\s+(?P<name>zinc oxide\s*\(ZnO\)|ZnO)\s+as an internal standard"
    r".{0,100}?mass ratio of\s*(?P<left>\d+(?:\.\d+)?)\s*:\s*(?P<right>\d+(?:\.\d+)?)",
    re.I,
)


def _phase_name(value: str) -> str:
    folded = value.casefold()
    if "amorphous" in folded:
        return "Amorphous"
    if "kaolinite" in folded:
        return "Kaolinite"
    if "quartz" in folded:
        return "Quartz"
    return normalized_text(value)


AGGREGATE_QXRD_RE = re.compile(
    r"The (?P<material>[A-Z][A-Za-z0-9#_-]{0,30}) exhibits "
    r"(?P<description>(?:(?!\.\s+[A-Z]).){1,500}?)\.\s+"
    r"The crystalline and amorphous phase contents account for "
    r"(?P<crystalline>\d+(?:\.\d+)?)\s*%\s+and\s+"
    r"(?P<amorphous>\d+(?:\.\d+)?)\s*%,\s+respectively\b"
)


SINGLE_AMORPHOUS_RE = re.compile(
    r'(?P<material>GGBFS|GGBS|class F fly ash|fly ash|slag) is composed of '
    r'(?P<qualifier>approximately|around|about) (?P<value>\d+(?:\.\d+)?)\s*%\s*amorphous phase', re.I)


def _single_amorphous_qxrd(page, page_number, regions):
    """Keep reported approximation; do not infer complementary crystalline phases."""
    text, positions = _mapped_page_text(page)
    facts = []
    for match in SINGLE_AMORPHOUS_RE.finditer(text):
        boxes = [fitz.Rect(b) for b in positions[match.start():match.end()] if b is not None]
        if not boxes or not all(any(region.contains(box) for region in regions) for box in boxes):
            continue
        value = float(match['value'])
        if not 0 <= value <= 100:
            raise ValueError('reported amorphous fraction outside percentage range')
        parts = [{'role': name, 'snippet': match[name], 'page': page_number,
                  'bbox': _mapped_bbox(page, positions, match.span(name), match[name])}
                 for name in ('material', 'qualifier', 'value')]
        fact = {'material_label': match['material'], 'exact_material_only': True,
                'page': page_number, 'method': None, 'rows': [], 'internal_standard': None,
                'amorphous_total': {'value_percent': value, 'original_value': match['value'],
                    'value_bbox': parts[-1]['bbox'], 'source_parts': parts,
                    'reported_qualifier': match['qualifier']},
                'context_sha256': digest_bytes(match.group().encode('utf-8'))}
        fact['fact_id'] = 'qxrd-' + digest(fact)[:24]
        facts.append(fact)
    return facts


def _aggregate_qxrd(page, page_number, regions):
    """Explicit aggregate totals only; never split a crystalline total into phases."""
    if not regions:
        return []
    text, positions = _mapped_page_text(page)
    facts = []
    for match in AGGREGATE_QXRD_RE.finditer(text):
        boxes = [fitz.Rect(b) for b in positions[match.start():match.end()] if b is not None]
        if not boxes or not all(any(region.contains(box) for region in regions) for box in boxes):
            continue
        # No citation-based prior-study claim or multiple competing subjects.
        if re.search(r"\[|\b(?:and|whereas|while)\s+[A-Z]", match['description']):
            continue
        crystalline, amorphous = float(match['crystalline']), float(match['amorphous'])
        if not (0 <= crystalline <= 100 and 0 <= amorphous <= 100) or abs(crystalline+amorphous-100) > .11:
            raise ValueError("inconsistent directly reported crystalline/amorphous totals")
        raw = match['amorphous']
        parts = []
        for name in ('material', 'crystalline', 'amorphous'):
            parts.append({'role':name, 'snippet':match[name], 'page':page_number,
                          'bbox':_mapped_bbox(page, positions, match.span(name), match[name])})
        fact = {'material_label':match['material'], 'exact_material_only':True,
                'page':page_number, 'method':None, 'rows':[], 'internal_standard':None,
                'amorphous_total':{'value_percent':amorphous, 'original_value':raw,
                                   'value_bbox':parts[-1]['bbox'], 'source_parts':parts},
                'context_sha256':digest_bytes(match.group().encode('utf-8'))}
        fact['fact_id'] = 'qxrd-' + digest(fact)[:24]
        facts.append(fact)
    return facts


def _direct_qxrd(page: fitz.Page, page_number: int, text: str) -> list[dict[str, Any]]:
    if not QXRD_RE.search(text):
        return []
    text, positions = _mapped_page_text(page)
    facts: list[dict[str, Any]] = []
    for match in QXRD_RE.finditer(text):
        standard = None
        attached = [s for s in STANDARD_RE.finditer(text) if s.start() >= match.end()
                    and re.fullmatch(r"[\s,]*(?:determined\s+)?", text[match.end():s.start()])]
        if len(attached) > 1:
            raise ValueError("ambiguous QXRD standard clause")
        if attached:
            s = attached[0]
            left, right = float(s["left"]), float(s["right"])
            if left <= 0 or right <= 0:
                raise ValueError("QXRD standard ratio components must be positive")
            name_start = text.index("ZnO", s.start("name"), s.end("name"))
            method_start = text.index("internal standard", s.start(), s.end())
            ratio_span = (s.start("left"), s.end("right"))
            ratio_token = text[slice(*ratio_span)]
            # The arithmetic alone cannot establish operand identity. Preserve
            # unlabelled/reversed ratios as candidates instead of silently
            # interpreting their left operand as the standard mass.
            ratio_context = text[s.start():s.start('left')]
            standard_first = re.search(r"\b(?:ZnO|zinc oxide)[ -]+to[ -]+(?:soil|sample)\s+mass ratio of\s*$", ratio_context, re.I)
            standard = {"name": "ZnO", "reported_standard_to_sample_ratio": ratio_token,
                        "standard_to_sample_mass_ratio": left/right if standard_first else None,
                        "standard_fraction_of_spiked_mixture_percent": left/(left+right)*100 if standard_first else None,
                        "ratio_bbox": _mapped_bbox(page, positions, ratio_span, ratio_token),
                        "name_bbox": _mapped_bbox(page, positions, (name_start,name_start+3), "ZnO"),
                        "method_bbox": _mapped_bbox(page, positions, (method_start,method_start+17), "internal standard"),
                        "ratio_direction_verified": bool(standard_first)}
        rows = []
        for ordinal in (1, 2):
            raw_value = match.group(f"value{ordinal}")
            value_bbox = _mapped_bbox(page, positions, match.span(f"value{ordinal}"), raw_value)
            phase_token = match.group(f"phase{ordinal}").split()[0]
            phase_start = match.start(f"phase{ordinal}")
            phase_box = _mapped_bbox(page, positions, (phase_start,phase_start+len(phase_token)), phase_token)
            rows.append({
                "phase": _phase_name(match.group(f"phase{ordinal}")),
                "value_percent": float(raw_value),
                "original_value": raw_value,
                "value_bbox": value_bbox,
                "phase_original": phase_token,
                "phase_bbox": list(phase_box) if phase_box else None,
            })
        fact = {
            "material_label": normalized_text(match.group("material")),
            "page": page_number,
            "method": "Rietveld" if re.search(r"Rietveld", _context(text, match.start(), match.end(), 420), re.I) else None,
            "rows": rows,
            "internal_standard": copy.deepcopy(standard),
            "context_sha256": digest_bytes(_context(text, match.start(), match.end()).encode("utf-8")),
        }
        fact["fact_id"] = "qxrd-" + digest(fact)[:24]
        facts.append(fact)
    return facts


def _mapped_page_text(page):
    """Normalize reading-order source characters without losing their boxes."""
    characters = []
    for block in page.get_text("rawdict")["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                characters.extend((char["c"], char["bbox"]) for char in span["chars"])
            characters.append(("\n", None))
    raw = "".join(char for char, _ in characters)
    skip = {i for match in re.finditer(r"\u00ad\s*", raw) for i in range(match.start(), match.end())}
    text, positions = [], []
    for i, (char, box) in enumerate(characters):
        if i in skip:
            continue
        if char.isspace():
            if text and text[-1] != " ":
                text.append(" "); positions.append(None)
        else:
            text.append(char.replace("−", "-").replace("–", "-")); positions.append(box)
    if text and text[-1] == " ":
        text.pop(); positions.pop()
    return "".join(text), positions


def _mapped_bbox(page, positions, span, token):
    boxes = [fitz.Rect(box) for box in positions[slice(*span)] if box is not None]
    if not boxes:
        raise ValueError("QXRD matched token has no source characters")
    bounds = fitz.Rect(boxes[0])
    for box in boxes[1:]:
        bounds |= box
    for fraction in (0, .15, .25, .30):
        inset = bounds.height*fraction
        box = fitz.Rect(bounds.x0+.01, bounds.y0+inset, bounds.x1-.01, bounds.y1-inset)
        if page.get_textbox(box).strip() == token:
            return list(box)
    raise ValueError("QXRD matched token fails exact source-box readback")


def _semantic_pages(
    document: fitz.Document,
    run_id: str,
    pdf_sha256: str,
    semantic_source: dict[str, Any] | None,
) -> tuple[dict[int, str], dict[str, Any] | None]:
    if semantic_source is None:
        return {
            page_index + 1: normalized_text(page.get_text("text"))
            for page_index, page in enumerate(document)
        }, None
    if semantic_source.get("runId") != run_id:
        raise ValueError("semantic source run mismatch")
    if semantic_source.get("pdfSha256") != pdf_sha256:
        raise ValueError("semantic source PDF hash mismatch")
    if int(semantic_source.get("pageCount") or -1) != len(document):
        raise ValueError("semantic source page count mismatch")
    pages = semantic_source.get("pages") or []
    if [int(item.get("page") or -1) for item in pages] != list(range(1, len(document) + 1)):
        raise ValueError("semantic source pages are incomplete or unordered")
    markdown_sha256 = str(semantic_source.get("markdownSha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", markdown_sha256):
        raise ValueError("semantic source hash is invalid")
    metadata = {
        "generator_version": str(semantic_source.get("generatorVersion") or ""),
        "markdown_sha256": markdown_sha256,
        "pdf_sha256": pdf_sha256,
        "page_count": len(document),
    }
    return {int(item["page"]): normalized_text(str(item.get("text") or "")) for item in pages}, metadata


def build_plan(
    pdf_path: Path,
    run_id: str,
    supplement_inventories: list[dict[str, Any]] | None = None,
    semantic_source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    pdf_path = pdf_path.resolve()
    pdf_bytes = pdf_path.read_bytes()
    pdf_sha256 = digest_bytes(pdf_bytes)
    doc = fitz.open(pdf_path)
    try:
        semantic_pages, semantic_metadata = _semantic_pages(doc, run_id, pdf_sha256, semantic_source)
        source_items: list[dict[str, Any]] = _main_caption_items(doc)
        scope_file = Path(__file__).with_name('source_material_identity.py')
        scope_spec = importlib.util.spec_from_file_location('qxrd_material_scopes', scope_file)
        scope_module = importlib.util.module_from_spec(scope_spec)
        scope_spec.loader.exec_module(scope_module)
        material_regions = scope_module.material_scopes(doc)
        qxrd: list[dict[str, Any]] = []
        mentioned_modalities: set[str] = set()
        for page_index, page in enumerate(doc):
            text = semantic_pages[page_index + 1]
            mentioned_modalities.update(_modality_for_context(text))
            source_items.extend(_supplement_items(page, page_index + 1, text))
            qxrd.extend(_direct_qxrd(page, page_index + 1, text))
            qxrd.extend(_aggregate_qxrd(page, page_index + 1, material_regions.get(page_index+1, [])))
            qxrd.extend(_single_amorphous_qxrd(page, page_index + 1, material_regions.get(page_index+1, [])))
        supplement_inventories = supplement_inventories or []
        content_by_label: dict[str, list[str]] = {}
        supplement_bindings: list[dict[str, Any]] = []
        for inventory in supplement_inventories:
            inventory_hash = inventory.get("inventory_sha256")
            if inventory_hash != digest({key: value for key, value in inventory.items() if key != "inventory_sha256"}):
                raise ValueError("supplement inventory hash mismatch")
            supplement_bindings.append({"inventory_sha256": inventory_hash, "docx_sha256": (inventory.get("docx") or {}).get("sha256")})
            for group in ("tables", "figures"):
                for entry in inventory.get(group) or []:
                    modality = str(entry.get("modality") or "SUPPLEMENT_OTHER")
                    intended = "NOT_APPLICABLE" if modality == "OUT_OF_SCOPE_LCA" else "REQUIRES_DIGITIZATION" if group == "figures" and modality in {"PSD", "PERFORMANCE"} else "EXTRACTABLE_DIRECT"
                    item = {
                        "kind": "SUPPLEMENT_TABLE" if group == "tables" else "SUPPLEMENT_FIGURE",
                        "modality": modality,
                        "source_scope": "supplement_content",
                        "source_label": entry["label"],
                        "page": None,
                        "bbox": None,
                        "context_sha256": digest_bytes(str(entry.get("caption") or "").encode("utf-8")),
                        "content_sha256": entry.get("rows_sha256") or entry.get("sha256"),
                        "supplement_inventory_sha256": inventory_hash,
                        "disposition": intended,
                    }
                    item["item_id"] = "source-" + digest({key: item.get(key) for key in ("kind", "modality", "source_scope", "source_label", "content_sha256")})[:24]
                    source_items.append(item)
                    content_by_label.setdefault(_supplement_label_key(entry["label"]), []).append(item["item_id"])
        for item in source_items:
            if item.get("kind") != "SUPPLEMENT_REFERENCE":
                continue
            matches = content_by_label.get(_supplement_label_key(item["source_label"]), [])
            if matches:
                item["disposition"] = "REFERENCE_RESOLVED"
                item["resolved_content_item_ids"] = sorted(matches)
        for fact in qxrd:
            value_boxes = [row['value_bbox'] for row in fact.get('rows', []) if row.get('value_bbox')]
            if fact.get('amorphous_total', {}).get('value_bbox'):
                value_boxes.append(fact['amorphous_total']['value_bbox'])
            source_items.append({
                "item_id": "source-" + fact["fact_id"],
                "kind": "DIRECT_SCIENTIFIC_FACT",
                "modality": "XRD_QXRD",
                "source_scope": "main",
                "source_label": fact["fact_id"],
                "page": fact["page"],
                "bbox": value_boxes[0] if value_boxes else None,
                "value_bboxes": value_boxes,
                "context_sha256": fact["context_sha256"],
                "disposition": "EXTRACTABLE_DIRECT",
            })
        # The same reference can occur repeatedly in running text.  Keep one
        # stable source item per scope/modality/label/page.
        by_key: dict[tuple[Any, ...], dict[str, Any]] = {}
        for item in source_items:
            modality_key = tuple(item["modality"]) if isinstance(item.get("modality"), list) else item.get("modality")
            key = (item["kind"], modality_key, item["source_scope"], item["source_label"].casefold(), item["page"])
            by_key[key] = item
        source_items = sorted(by_key.values(), key=lambda item: (item["page"] is None, item["page"] if item["page"] is not None else 0, item["source_scope"], tuple(item["modality"]) if isinstance(item["modality"], list) else (item["modality"],), item["source_label"]))
        plan = {
            "schema_version": 1,
            "planner_version": PLANNER_VERSION,
            "material_scope_implementation_sha256": digest_bytes(scope_file.read_bytes()),
            "run_id": run_id,
            "pdf": {"sha256": pdf_sha256, "bytes": len(pdf_bytes), "pages": len(doc)},
            "semantic_source": semantic_metadata,
            "sections": _section_inventory(doc),
            "mentioned_modalities": sorted(mentioned_modalities),
            "source_items": source_items,
            "direct_facts": {"qxrd": qxrd},
            "supplement_bindings": supplement_bindings,
            "completeness_contract": {
                "required_source_item_ids": [item["item_id"] for item in source_items],
                "terminal_dispositions": sorted(TERMINAL_DISPOSITIONS),
                "overall_pass_requires_all_items_terminal": True,
            },
        }
        plan["plan_sha256"] = digest({key: value for key, value in plan.items() if key != "plan_sha256"})
        return plan
    finally:
        doc.close()


def _material_match(label: str, mats: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    folded = re.sub(r"[^a-z0-9]+", "", label.casefold())
    exact: list[dict[str, Any]] = []
    scored = []
    for mat in mats:
        aliases = {
            re.sub(r"[^a-z0-9]+", "", str(mat.get(key) or "").casefold())
            for key in ("custom_material_id", "material_type")
        }
        aliases.discard("")
        if folded in aliases:
            exact.append(mat)
            continue
        for candidate in aliases:
            if folded and (folded in candidate or candidate in folded):
                scored.append((abs(len(candidate) - len(folded)), candidate, mat))
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return None
    return min(scored, key=lambda item: (item[0], item[1]))[2] if scored else None


def _evidence(record: dict[str, Any], asset_key: str, field_path: str, page: int, bbox: list[float] | None, token: str, ordinal: int) -> dict[str, Any]:
    if not bbox:
        raise ValueError(f"missing source bbox for {field_path}: {token}")
    evidence_key = "ev-mm-" + digest([record["mat_key"], field_path, page, bbox, token, ordinal])[:24]
    return {
        "evidence_key": evidence_key,
        "asset_key": asset_key,
        "record_type": "mat",
        "record_key": record["mat_key"],
        "field_path": field_path,
        "page": page,
        "section": "Materials and methods",
        "table_number": None,
        "figure_number": None,
        "bbox": bbox,
        "snippet": token,
        "snippet_sha256": digest_bytes(token.encode("utf-8")),
        "extraction_method": "pymupdf-contextual-qxrd-v1",
        "confidence": 0.99,
        "paper_key": record["paper_key"],
    }


def _atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _quantity_mark_words(page, words):
    """Retain zero-advance combining tildes omitted by PDF word extraction."""
    marks=[ch for span in page.get_texttrace() for ch in span['chars']
           if ch[0] in (0x0334,0x0303)]
    result=list(words)
    for mark in marks:
        x,y=mark[2]
        candidates=[(i,w) for i,w in enumerate(result)
                    if re.match(r'^\d',w[4]) and (0<=w[0]-x<=5 or w[0]<=x<w[2])
                    and w[1]<=y<=w[3]]
        if candidates:
            i,w=min(candidates,key=lambda pair:pair[1][0]-x)
            result[i]=(x-1,w[1],w[2],w[3],'~'+w[4],*w[5:])
    return result


def _table_token_matrix(page: fitz.Page, item: dict[str, Any], siblings: list[dict[str, Any]]) -> dict[str, Any]:
    caption = fitz.Rect(*item["bbox"])
    # A ruled table's own edges determine its width and height. Captions may
    # occupy a small part of either a single-column or full-width table.
    strokes=[]
    for drawing in page.get_drawings():
        for segment in drawing.get('items', []):
            if segment[0]=='l':
                a,b=segment[1:3]
                if abs(a.y-b.y)<.5 and abs(a.x-b.x)>40:
                    strokes.append((min(a.x,b.x),max(a.x,b.x),(a.y+b.y)/2))
    tops=[s for s in strokes if caption.y1-2<=s[2]<=caption.y1+16
          and s[0]-4<=caption.x0<=s[1] and s[1]>=caption.x0+.8*caption.width]
    ruled=None
    if tops:
        top=min(tops,key=lambda s:abs(s[2]-caption.y1))
        next_caption=min([page.rect.y1]+[float(o['bbox'][1]) for o in siblings
            if o['item_id']!=item['item_id'] and int(o['page'])==int(item['page'])
            and o['bbox'][1]>top[2] and top[0]<=o['bbox'][0]<top[1]])
        bottoms=[s for s in strokes if top[2]+35<s[2]<next_caption
                 and abs(s[0]-top[0])<3 and abs(s[1]-top[1])<3]
        if not bottoms:
            short=[s for s in strokes if top[2]+8<s[2]<min(next_caption,top[2]+35)
                   and abs(s[0]-top[0])<3 and abs(s[1]-top[1])<3]
            if short:
                bottom=max(short,key=lambda s:s[2])
                if any(top[2]<w[1]<bottom[2] and top[0]<w[0]<top[1]
                       and re.fullmatch(r'\d+(?:\.\d+)?',w[4]) for w in page.get_text('words')):
                    bottoms=[bottom]
        if bottoms:
            ruled=(top[0],top[1],min(s[2] for s in bottoms)+.5)
    later = [float(other["bbox"][1]) for other in siblings if other["item_id"] != item["item_id"] and int(other["page"]) == int(item["page"]) and float(other["bbox"][1]) > caption.y1]
    y_limit = min([page.rect.y1, caption.y1 + 320.0, *later])
    if caption.x0 <= page.rect.x0 + page.rect.width * 0.2 or caption.width >= page.rect.width * 0.55:
        x0, x1 = page.rect.x0, page.rect.x1
    elif caption.x0 >= page.rect.x0 + page.rect.width * 0.45:
        x0, x1 = max(page.rect.x0, caption.x0 - 8.0), page.rect.x1
    else:
        x0, x1 = max(page.rect.x0, caption.x0 - 8.0), min(page.rect.x1, caption.x1 + 8.0)
    # Prefer a visible table bottom over a fixed-height window. This is a
    # geometric source boundary only, not a guess about row scientific meaning.
    rules=[]
    for drawing in page.get_drawings():
        for segment in drawing.get('items',[]):
            if segment[0]!='l':continue
            a,b=segment[1:3]
            if abs(a.y-b.y)<.5 and abs(a.x-b.x)>=.6*(x1-x0) and caption.y1<a.y<y_limit:
                rules.append(float(a.y))
    if len(set(round(y,1) for y in rules))>=2:
        numeric_bands={round((w[1]+w[3])/5.6) for w in page.get_text('words')
            if min(rules)<w[1]<max(rules) and x0<=(w[0]+w[2])/2<=x1
            and re.fullmatch(r'[-+]?\d+(?:\.\d+)?',w[4])}
        # Two header rules alone do not establish a bottom. Single-row/open
        # tables conservatively keep the existing window instead of losing data.
        if len(numeric_bands)>=2:y_limit=min(y_limit,max(rules)+.5)
    if ruled:
        x0,x1,y_limit=ruled
    words = sorted(
        [word for word in _quantity_mark_words(page,page.get_text("words")) if x0 <= (float(word[0]) + float(word[2])) / 2 <= x1 and caption.y1 - 2.0 <= float(word[1]) < y_limit],
        key=lambda word: ((float(word[1]) + float(word[3])) / 2, float(word[0])),
    )
    grouped: list[list[tuple[Any, ...]]] = []
    for word in words:
        if not grouped or abs((float(grouped[-1][0][1]) + float(grouped[-1][0][3])) / 2 - (float(word[1]) + float(word[3])) / 2) > 2.8:
            grouped.append([word])
        else:
            grouped[-1].append(word)
    rows: list[list[dict[str, Any]]] = []
    previous_y: float | None = None
    for line in grouped:
        line = sorted(line, key=lambda word: float(word[0]))
        raw = " ".join(str(word[4]) for word in line).strip()
        y = float(line[0][1])
        if not ruled and len(rows) >= 3 and previous_y is not None and y - previous_y > 18.0:
            break
        if not ruled and re.match(r"^(?:Note\s*:|[a-z]\s+LOI\b|\d+(?:\.\d+)+\.?\s+[A-Z])", raw, re.I):
            break
        numeric_or_dash = sum(bool(re.fullmatch(r"[-−–]|[-+]?\d+(?:\.\d+)?(?:%|\*)?", str(word[4]).strip("(),"))) for word in line)
        component = sum(bool(re.fullmatch(r"(?:[A-Z][a-z]?\d*){1,3}(?:\+[A-Z][a-z]?\d*)?|LOI|Others?", str(word[4]).strip("(),"), re.I)) for word in line)
        if not ruled and rows and numeric_or_dash == 0 and component == 0 and len(raw) > 100:
            break
        if not ruled and len(rows) >= 3 and numeric_or_dash <= 1 and component <= 1 and len(line) > 12:
            break
        if len(line) >= 1:
            rows.append([{"text": str(word[4]), "bbox": [round(float(value), 3) for value in word[:4]]} for word in line])
            previous_y = max(float(word[3]) for word in line)
    from native_signed_cells import merge_rows
    vertical_edges=[float(seg[1].x) for drawing in page.get_drawings() for seg in drawing.get('items',[])
                    if seg[0]=='l' and abs(seg[1].x-seg[2].x)<.5
                    and min(seg[1].y,seg[2].y)<=caption.y1+15
                    and max(seg[1].y,seg[2].y)>=y_limit-2]
    rows=merge_rows(rows,vertical_edges)
    if not rows:
        raise ValueError(f"no table tokens found for {item['source_label']} page {item['page']}")
    all_cells = [cell for row in rows for cell in row]
    bbox = [
        min(cell["bbox"][0] for cell in all_cells),
        min(cell["bbox"][1] for cell in all_cells),
        max(cell["bbox"][2] for cell in all_cells),
        max(cell["bbox"][3] for cell in all_cells),
    ]
    value = {
        "schema_version": 1,
        "source_label": item["source_label"],
        "page": item["page"],
        "caption": item.get("caption"),
        "bbox": [round(float(value), 3) for value in bbox],
        "rows": rows,
        "extraction_method": "pymupdf-word-geometry-table-v1",
    }
    value["table_sha256"] = digest({key: entry for key, entry in value.items() if key != "table_sha256"})
    return value


def _fold_material(value: Any) -> str:
    text = re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())
    aliases = {"fa": "flyash", "ggbs": "slag", "ggbfs": "slag", "bfs": "slag", "fgr": "fluegasresidue", "pc": "portlandcement", "rha": "ricehuskash"}
    return aliases.get(text, text)


def _new_material(result: dict[str, Any], label: str) -> dict[str, Any]:
    suffix = digest([result["papers"][0]["paper_key"], label])[:12]
    folded = _fold_material(label)
    role = "fly ash" if folded == "flyash" else "slag" if folded == "slag" else "rice husk ash" if folded == "ricehuskash" else "kaolin" if "kaolin" in folded else "precursor"
    return {
        "schema_version": "1.0", "mat_key": f"mat-{suffix}-xrf", "paper_key": result["papers"][0]["paper_key"],
        "custom_material_id": label, "material_type": role, "source_type": "literature", "literature_title": None, "doi": None, "year": None,
        "physical_properties": {"specific_surface_m2_kg": None, "specific_surface_method": None, "true_density_kg_m3": None, "apparent_density_kg_m3": None, "bulk_density_kg_m3": None, "total_porosity_percent": None, "open_porosity_percent": None, "closed_porosity_percent": None},
        "particle_size_distribution": {"schema_version": "1.0", "reported_curve_type": None, "points_asset_key": None, "source_image_asset_key": None, "d10_um": None, "d50_um": None, "d90_um": None, "conversion_review": None, "extensions": {}},
        "xrf_composition": {"schema_version": "1.0", "unit": "wt.%", "rows": [], "original_total": None, "normalisation_candidate": None, "extensions": {}},
        "ftir_spectrum": None, "xrd_qxrd": None, "si29_nmr_spectrum": None, "al27_nmr_spectrum": None,
        "field_provenance": {}, "extensions": {"semantic_role": role, "created_from_source_table": True},
    }


def _reviewed_xrf_aliases(doc, pdf_sha256):
    """Source-bound semantic decisions; never generalize away grade qualifiers."""
    if pdf_sha256 != '993d666cc40b6eb807176ef8e1c766792c40a0f7ad7560cc7a3e12baf3ce7e14':
        return {}
    quote = 'Ground granulated blast-furnace slag (GGBFS) and class F fly ash were selected as the aluminosilicate precursors.'
    text = ' '.join(doc[1].get_text().split())
    if quote not in text:
        raise ValueError('reviewed XRF material identity source changed')
    return {key: {'target': target, 'page': 2, 'quote': quote,
                  'review': 'xrf-empty-material-scope-review-v1', 'pdf_sha256': pdf_sha256}
            for key, target in [('flyash', 'Class F fly ash'), ('slag', 'slag')]}


def _material_for_xrf(result: dict[str, Any], label: str, reviewed_aliases=None) -> dict[str, Any]:
    source = _fold_material(label)
    decision = (reviewed_aliases or {}).get(source)
    if decision:
        owners = [m for m in result.get('mats', [])
                  if str(m.get('custom_material_id', '')).casefold() == decision['target'].casefold()]
        if len(owners) != 1:
            raise ValueError('reviewed XRF material owner must be unique')
        owners[0].setdefault('extensions', {})['xrf_source_identity'] = {
            **decision, 'table_label': label}
        return owners[0]
    for mat in result.get("mats") or []:
        target = _fold_material(mat.get("custom_material_id"))
        raw_target = re.sub(r"[^a-z0-9]+", "", str(mat.get("custom_material_id") or "").casefold())
        if "flyash" in raw_target and ("ggbs" in raw_target or "slag" in raw_target):
            continue
        if source == target:
            return mat
    # Partial names and generic precursor placeholders are not identity proof.
    # Preserve the source label separately for the grouped semantic relation gate.
    mat = _new_material(result, label)
    result.setdefault("mats", []).append(mat)
    return mat


def _numeric_token(cell: dict[str, Any]) -> float | None:
    text = str(cell.get("text") or "").strip().replace("−", "-").replace("–", "-").rstrip("*a")
    return float(text) if NUMBER_RE.match(text) else None


def _is_source_dash(cell: dict[str, Any]) -> bool:
    return str(cell.get("text") or "").strip() in {"-", "–", "−", "/"}


def _is_oxide_label(value: Any) -> bool:
    text = str(value or "").strip(".,()")
    return bool(OXIDE_RE.match(text)) and ("O" in text or text.casefold().startswith(("loi", "other")))


def _xrf_rows_from_table(table: dict[str, Any]) -> list[dict[str, Any]]:
    rows = table["rows"]
    oxide_cells = [cell for row in rows for cell in row if _is_oxide_label(cell["text"])]
    if len(oxide_cells) < 2:
        return []
    candidates = []
    for row in rows:
        numeric = [cell for cell in row if _numeric_token(cell) is not None or _is_source_dash(cell)]
        if len(numeric) < 2:
            continue
        first_x = min(float(cell["bbox"][0]) for cell in numeric)
        labels = [cell for cell in row if float(cell["bbox"][2]) < first_x and _numeric_token(cell) is None and not _is_source_dash(cell)]
        if labels:
            label = " ".join(str(cell["text"]) for cell in labels).strip()
            if len(label) <= 40 and not label.startswith("("):
                data_cells = [cell for cell in row if _numeric_token(cell) is not None or _is_source_dash(cell)]
                candidates.append((row, data_cells, label, labels))
    transposed_votes = sum(_is_oxide_label(label.replace(" ", "")) for _, _, label, _ in candidates)
    if candidates and transposed_votes < max(2, len(candidates) // 2):
        first_data_y = min(float(row[0]["bbox"][1]) for row, _, _, _ in candidates)
        oxide_cells = [cell for row in rows for cell in row if float(cell["bbox"][3]) < first_data_y and _is_oxide_label(cell["text"])]
        if len(oxide_cells) < 2:
            return []
        headers: list[dict[str, Any]] = []
        for cell in sorted(oxide_cells, key=lambda value: (float(value["bbox"][0]), float(value["bbox"][1]))):
            center = (float(cell["bbox"][0]) + float(cell["bbox"][2])) / 2
            if not any(abs(center - (float(other["bbox"][0]) + float(other["bbox"][2])) / 2) < 3 for other in headers):
                headers.append(cell)
        output = []
        for _, numeric, label, material_cells in candidates:
            values = []
            ordered_cells = sorted(numeric, key=lambda cell: float(cell["bbox"][0]))
            if len(ordered_cells) == len(headers):
                aligned = list(zip(ordered_cells, headers))
            else:
                choices = []
                for cell in ordered_cells:
                    for header_index, header in enumerate(headers):
                        distance = abs((float(cell["bbox"][0]) + float(cell["bbox"][2])) / 2 - (float(header["bbox"][0]) + float(header["bbox"][2])) / 2)
                        choices.append((distance, header_index, cell, header))
                aligned, used_cells, used_headers = [], set(), set()
                for _, header_index, cell, header in sorted(choices, key=lambda choice: choice[0]):
                    cell_key = tuple(cell["bbox"])
                    if cell_key in used_cells or header_index in used_headers:
                        continue
                    aligned.append((cell, header)); used_cells.add(cell_key); used_headers.add(header_index)
                aligned.sort(key=lambda pair: float(pair[0]["bbox"][0]))
            for cell, header in aligned:
                values.append({"component": str(header["text"]).strip(".,()"), "value": _numeric_token(cell), "cell": cell})
            output.append({"material": label, "material_cells": material_cells, "values": values,
                           "expected_components": [str(h['text']).strip('.,()') for h in headers]})
        return output
    component_rows = [(row, [cell for cell in row if _numeric_token(cell) is not None or _is_source_dash(cell)]) for row in rows if row and _is_oxide_label(row[0]["text"]) and len([cell for cell in row if _numeric_token(cell) is not None or _is_source_dash(cell)]) >= 2]
    if not component_rows:
        return []
    centers = [(float(cell["bbox"][0]) + float(cell["bbox"][2])) / 2 for cell in sorted(component_rows[0][1], key=lambda value: float(value["bbox"][0]))]
    first_y = min(float(row[0]["bbox"][1]) for row, _ in component_rows)
    header_cells = [cell for row in rows for cell in row if float(cell["bbox"][3]) < first_y and not _is_oxide_label(cell["text"])]
    usable_headers = sorted([cell for cell in header_cells if str(cell["text"]) not in {"Chemical", "compositions", "(%)"}], key=lambda cell: float(cell["bbox"][0]))
    header_groups: list[list[dict[str, Any]]] = []
    for cell in usable_headers:
        if not header_groups or float(cell["bbox"][0]) - float(header_groups[-1][-1]["bbox"][2]) > 7:
            header_groups.append([cell])
        else:
            header_groups[-1].append(cell)
    materials = []
    for center in centers:
        group = min(header_groups, key=lambda cells: abs(sum((float(cell["bbox"][0]) + float(cell["bbox"][2])) / 2 for cell in cells) / len(cells) - center)) if header_groups else []
        materials.append(" ".join(str(cell["text"]) for cell in group).strip() or f"column-{len(materials) + 1}")
    output = [{"material": label, "material_cells": group, "values": []} for label, group in zip(materials, [min(header_groups, key=lambda cells: abs(sum((float(cell["bbox"][0]) + float(cell["bbox"][2])) / 2 for cell in cells) / len(cells) - center)) if header_groups else [] for center in centers])]
    for row, numeric in component_rows:
        component = str(row[0]["text"]).strip(".,()")
        for cell in numeric:
            center = (float(cell["bbox"][0]) + float(cell["bbox"][2])) / 2
            index = min(range(len(centers)), key=lambda idx: abs(center - centers[idx]))
            output[index]["values"].append({"component": component, "value": _numeric_token(cell), "cell": cell})
    for material in output:
        material['expected_components'] = [str(row[0]['text']).strip('.,()') for row, _ in component_rows]
    return output


def _enrich_xrf_table(result: dict[str, Any], item: dict[str, Any], table: dict[str, Any], pdf_asset: dict[str, Any], reviewed_aliases=None, *, source_rows=None, bound_material=None) -> list[str]:
    emitted: list[str] = []
    paper_key = result["papers"][0]["paper_key"]
    if bound_material is not None and (source_rows is None or len(source_rows)!=1 or not any(m is bound_material for m in result['mats'])):
        raise ValueError('explicit XRF owner requires one selected row and an existing material')
    for source_row in (_xrf_rows_from_table(table) if source_rows is None else source_rows):
        expected = source_row.get('expected_components', [])
        actual = [value['component'] for value in source_row['values']]
        if sorted(actual) != sorted(expected):
            candidate = {
                'status': 'QUARANTINED', 'source_item_id': item['item_id'],
                'material_label': source_row['material'], 'expected_components': expected,
                'observed_components': actual, 'source_values': source_row['values'],
                'reason': 'INCOMPLETE_HEADER_COLUMN_COVERAGE'}
            candidates = result.setdefault('extensions', {}).setdefault('xrf_alignment_candidates', [])
            if candidate not in candidates:
                candidates.append(candidate)
            continue
        mat = bound_material if bound_material is not None else _material_for_xrf(result, source_row["material"], reviewed_aliases)
        xrf_rows, total = [], 0.0
        material_cells = source_row.get("material_cells") or []
        if material_cells:
            material_bbox = [min(float(cell["bbox"][0]) for cell in material_cells), min(float(cell["bbox"][1]) for cell in material_cells), max(float(cell["bbox"][2]) for cell in material_cells), max(float(cell["bbox"][3]) for cell in material_cells)]
            identity_key = "ev-main-xrf-identity-" + digest([mat["mat_key"], item["item_id"], material_bbox, source_row["material"]])[:20]
            result["evidence_links"].append({"evidence_key": identity_key, "asset_key": pdf_asset["asset_key"], "record_type": "mat", "record_key": mat["mat_key"], "field_path": "/custom_material_id", "page": item["page"], "section": None, "table_number": item["source_label"], "figure_number": None, "bbox": material_bbox, "snippet": source_row["material"], "snippet_sha256": digest_bytes(source_row["material"].encode("utf-8")), "extraction_method": "pymupdf-xrf-row-identity-v1", "confidence": 0.99, "paper_key": paper_key})
            mat.setdefault("field_provenance", {})["/custom_material_id"] = {"evidence_key": identity_key, "original_value": source_row["material"], "original_unit": None, "formula": None, "extraction_method": "pymupdf-xrf-row-identity-v1", "confidence": 0.99, "review_status": "confirmed"}
            emitted.append(identity_key)
        for value in source_row["values"]:
            component, numeric, cell = value["component"], value["value"], value["cell"]
            path = f"/xrf_composition/rows/{component}"
            evidence_key = "ev-main-xrf-" + digest([mat["mat_key"], item["item_id"], component, cell["bbox"], numeric])[:24]
            result["evidence_links"].append({"evidence_key": evidence_key, "asset_key": pdf_asset["asset_key"], "record_type": "mat", "record_key": mat["mat_key"], "field_path": path, "page": item["page"], "section": None, "table_number": item["source_label"], "figure_number": None, "bbox": cell["bbox"], "snippet": str(cell["text"]), "snippet_sha256": digest_bytes(str(cell["text"]).encode("utf-8")), "extraction_method": "pymupdf-xrf-column-alignment-v1", "confidence": 0.98, "paper_key": paper_key})
            xrf_rows.append({"component": component, "original_value": numeric, "normalized_value": numeric, "status": "confirmed" if numeric is not None else "not_reported", "reason": None if numeric is not None else "source_dash"})
            mat.setdefault("field_provenance", {})[path] = {"evidence_key": evidence_key, "original_value": str(cell["text"]), "original_unit": "wt.%", "formula": None, "extraction_method": "pymupdf-xrf-column-alignment-v1", "confidence": 0.98, "review_status": "confirmed"}
            if numeric is not None:
                total += numeric
            emitted.append(evidence_key)
        # Sum reported decimal values exactly before deciding the 100% gate.
        from decimal import Decimal
        total = float(sum((Decimal(str(row['original_value'])) for row in xrf_rows if row['original_value'] is not None), Decimal(0)))
        rounded_total = round(total, 4)
        candidate = None
        if total > 100.0:
            candidate = {"status": "candidate_not_promoted", "formula": "normalized = original / original_total * 100", "factor": 100.0 / total, "rows": [{"component": row["component"], "candidate_value": None if row["original_value"] is None else row["original_value"] / total * 100.0} for row in xrf_rows]}
        mat["xrf_composition"] = {"schema_version": "1.0", "unit": "wt.%", "rows": xrf_rows, "original_total": rounded_total, "normalisation_candidate": candidate, "extensions": {"source_table": item["source_label"], "normalization_status": "candidate_not_promoted" if candidate else "not_normalized"}}
    composite = [mat for mat in result.get("mats") or [] if not (mat.get("xrf_composition") or {}).get("rows") and "flyash" in re.sub(r"[^a-z0-9]+", "", str(mat.get("custom_material_id") or "").casefold()) and any(term in re.sub(r"[^a-z0-9]+", "", str(mat.get("custom_material_id") or "").casefold()) for term in ("ggbs", "slag"))]
    extracted_keys = [mat["mat_key"] for mat in result.get("mats") or [] if (mat.get("xrf_composition") or {}).get("rows")]
    for shell in composite:
        for mix in result.get("mixes") or []:
            refs = list((mix.get("modules") or {}).get("materials", {}).get("mat_refs") or [])
            if shell["mat_key"] in refs:
                mix["modules"]["materials"]["mat_refs"] = sorted(set([key for key in refs if key != shell["mat_key"]] + extracted_keys))
    return emitted


def _main_source_bindings(result: dict[str, Any], plan: dict[str, Any], asset_output_dir: Path | None) -> None:
    """Close every main caption item against records or a preserved figure asset."""
    binding = result.setdefault("extensions", {}).setdefault("extraction_plan", {})
    dispositions = binding.setdefault("source_item_dispositions", {})
    evidence = result.setdefault("evidence_links", [])
    paper_key = result["papers"][0]["paper_key"]
    pdf_asset = next(asset for asset in result.get("assets") or [] if asset.get("kind") == "pdf")
    pdf_path = Path(pdf_asset["relative_path"])
    links_by_table: dict[str, list[dict[str, Any]]] = {}
    for link in evidence:
        label = link.get("table_number")
        # A preserved paper-level caption is source inventory, not extracted
        # MAT/MIX data. It must not change this branch on the second call.
        if label and link.get('record_type') in {'mat', 'mix'}:
            links_by_table.setdefault(_source_label_key(str(label)), []).append(link)
    doc = fitz.open(pdf_path)
    try:
        reviewed_xrf_aliases = _reviewed_xrf_aliases(doc, digest_bytes(pdf_path.read_bytes()))
        papers = [p for p in result['papers'] if p['paper_key'] == paper_key]
        if len(papers) != 1:
            raise ValueError('source inventory requires one unambiguous paper owner')
        if any(result.get('extensions', {}).get(name) for name in ('scientific_assets', 'scientific_tables')):
            raise ValueError('legacy batch source inventory requires fresh reconstruction')
        paper_extensions = papers[0].setdefault('extensions', {})
        scientific_assets = paper_extensions.setdefault('scientific_assets', [])
        scientific_tables = paper_extensions.setdefault('scientific_tables', [])
        existing_asset_keys = {asset.get("asset_key") for asset in result.get("assets") or []}
        main_items = [entry for entry in plan.get("source_items") or [] if entry.get("source_scope") == "main" and entry.get("kind") in {"MAIN_TABLE", "MAIN_FIGURE"}]
        for item in plan.get("source_items") or []:
            if item.get("source_scope") != "main" or item.get("kind") not in {"MAIN_TABLE", "MAIN_FIGURE"}:
                continue
            if item.get("disposition") == "NOT_APPLICABLE":
                dispositions[item["item_id"]] = {"status": "NOT_APPLICABLE", "reason": "OUTSIDE_WAKG_MAT_MIX_SCOPE"}
                continue
            if item["kind"] == "MAIN_TABLE":
                matches = links_by_table.get(item["source_key"], [])
                if not matches or 'XRF' in (item.get('modality') or []):
                    table = _table_token_matrix(doc[int(item["page"]) - 1], item, main_items)
                    table_bytes = (canonical(table) + "\n").encode("utf-8")
                    table_hash = digest_bytes(table_bytes)
                    asset_key = "asset-main-table-" + table_hash[:20]
                    filename = f"{item['source_key']}-p{item['page']}-{table_hash[:12]}.json"
                    if asset_output_dir is not None:
                        _atomic_bytes(asset_output_dir.parent / "main-table-assets" / filename, table_bytes)
                    if asset_key not in existing_asset_keys:
                        result["assets"].append({
                            "asset_key": asset_key,
                            "paper_key": paper_key,
                            "kind": "main_table",
                            "relative_path": f"main-table-assets/{filename}",
                            "sha256": table_hash,
                            "mime_type": "application/json",
                            "extensions": {"source_pdf_asset_key": pdf_asset["asset_key"], "page": item["page"], "table_number": item["source_label"], "caption": item.get("caption"), "table_sha256": table["table_sha256"], "modality": item.get("modality")},
                        })
                        existing_asset_keys.add(asset_key)
                    table_index = next((i for i, entry in enumerate(scientific_tables) if entry.get('source_item_id') == item['item_id'] and entry.get('asset_key') == asset_key), len(scientific_tables))
                    field_path = f"/extensions/scientific_tables/{table_index}"
                    evidence_key = "ev-main-table-" + digest([paper_key, item["item_id"], table_hash])[:24]
                    evidence.append({
                        "evidence_key": evidence_key, "asset_key": pdf_asset["asset_key"], "record_type": "paper", "record_key": paper_key,
                        "field_path": field_path, "page": item["page"], "section": None, "table_number": item["source_label"], "figure_number": None,
                        "bbox": table["bbox"], "snippet": item.get("caption") or item["source_label"],
                        "snippet_sha256": digest_bytes(str(item.get("caption") or item["source_label"]).encode("utf-8")),
                        "extraction_method": table["extraction_method"], "confidence": 0.97, "paper_key": paper_key,
                    })
                    if table_index == len(scientific_tables):
                        scientific_tables.append({"source_item_id": item["item_id"], "table_number": item["source_label"], "caption": item.get("caption"), "modality": item.get("modality"), "asset_key": asset_key, "evidence_key": evidence_key, "status": "structured_source_table"})
                    xrf_keys = _enrich_xrf_table(result, item, table, pdf_asset, reviewed_xrf_aliases) if "XRF" in (item.get("modality") or []) else []
                    dispositions[item["item_id"]] = {"status": "EXTRACTED", "extraction_mode": "STRUCTURED_SOURCE_TABLE", "asset_key": asset_key, "evidence_keys": [evidence_key, *xrf_keys], "record_key": paper_key, "canonical_mapping": bool(xrf_keys)}
                    if any(c.get('source_item_id') == item['item_id'] and c.get('status') == 'QUARANTINED' for c in result.get('extensions', {}).get('xrf_alignment_candidates', [])):
                        dispositions[item['item_id']]['status'] = 'QUARANTINED'
                        dispositions[item['item_id']]['reason'] = 'INCOMPLETE_HEADER_COLUMN_COVERAGE'
                    continue
                dispositions[item["item_id"]] = {
                    "status": "EXTRACTED",
                    "extraction_mode": "STRUCTURED_TABLE",
                    "evidence_keys": sorted({str(link["evidence_key"]) for link in matches}),
                    "record_keys": sorted({str(link.get("record_key")) for link in matches if link.get("record_key")}),
                }
                continue
            page = doc[int(item["page"]) - 1]
            caption = item.get("caption") or item["source_label"]
            clip_values = item.get("region_bbox")
            if clip_values:
                clip = fitz.Rect(*clip_values)
            else:
                caption_rect = fitz.Rect(*item["bbox"])
                clip = fitz.Rect(page.rect.x0, max(page.rect.y0, caption_rect.y0 - min(320.0, page.rect.height * 0.48)), page.rect.x1, min(page.rect.y1, caption_rect.y1))
            clip &= page.rect
            pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), clip=clip, alpha=False)
            png = pixmap.tobytes("png")
            png_hash = digest_bytes(png)
            asset_key = "asset-main-figure-" + png_hash[:20]
            filename = f"{item['source_key']}-p{item['page']}-{png_hash[:12]}.png"
            if asset_output_dir is not None:
                _atomic_bytes(asset_output_dir / filename, png)
            if asset_key not in existing_asset_keys:
                result["assets"].append({
                    "asset_key": asset_key,
                    "paper_key": paper_key,
                    "kind": "main_figure",
                    "relative_path": f"main-figure-assets/{filename}",
                    "sha256": png_hash,
                    "mime_type": "image/png",
                    "extensions": {
                        "source_pdf_asset_key": pdf_asset["asset_key"],
                        "page": item["page"],
                        "figure_number": item["source_label"],
                        "caption": caption,
                        "source_region_bbox": [round(float(value), 3) for value in (clip.x0, clip.y0, clip.x1, clip.y1)],
                        "modality": item.get("modality"),
                    },
                })
                existing_asset_keys.add(asset_key)
            figure_index = next((i for i, entry in enumerate(scientific_assets)
                                 if entry.get('source_item_id') == item['item_id']
                                 and entry.get('asset_key') == asset_key), len(scientific_assets))
            field_path = f"/extensions/scientific_assets/{figure_index}"
            evidence_key = "ev-main-figure-" + digest([paper_key, item["item_id"], asset_key])[:24]
            evidence.append({
                "evidence_key": evidence_key,
                "asset_key": pdf_asset["asset_key"],
                "record_type": "paper",
                "record_key": paper_key,
                "field_path": field_path,
                "page": item["page"],
                "section": None,
                "table_number": None,
                "figure_number": item["source_label"],
                "bbox": item["bbox"],
                "snippet": caption,
                "snippet_sha256": digest_bytes(str(caption).encode("utf-8")),
                "extraction_method": "pymupdf-caption-and-render-v1",
                "confidence": 0.99,
                "paper_key": paper_key,
            })
            figure_entry = {
                "source_item_id": item["item_id"],
                "figure_number": item["source_label"],
                "caption": caption,
                "modality": item.get("modality"),
                "asset_key": asset_key,
                "evidence_key": evidence_key,
                "status": "source_figure_preserved",
            }
            if figure_index == len(scientific_assets):
                scientific_assets.append(figure_entry)
            elif scientific_assets[figure_index] != figure_entry:
                raise ValueError('conflicting preserved figure identity')
            dispositions[item["item_id"]] = {
                "status": "EXTRACTED",
                "extraction_mode": "FIGURE_ASSET",
                "asset_key": asset_key,
                "evidence_keys": [evidence_key],
                "record_key": paper_key,
            }
        unique_links = {}
        for link in evidence:
            key = link['evidence_key']
            if key in unique_links and unique_links[key] != link:
                raise ValueError('conflicting source evidence identity: ' + key)
            unique_links[key] = link
        evidence[:] = unique_links.values()
    finally:
        doc.close()


def enrich_records(records: dict[str, Any], plan: dict[str, Any], asset_output_dir: Path | None = None) -> dict[str, Any]:
    if plan.get("planner_version") != PLANNER_VERSION:
        raise ValueError("unsupported extraction plan")
    result = copy.deepcopy(records)
    existing_binding = (result.get("extensions") or {}).get("extraction_plan") or {}
    if existing_binding.get("plan_sha256") == plan.get("plan_sha256"):
        return result
    assets = result.get("assets") or []
    pdf_asset = next((asset for asset in assets if asset.get("kind") == "pdf" or asset.get("mime_type") == "application/pdf"), None)
    if not pdf_asset:
        raise ValueError("records have no PDF asset")
    if pdf_asset.get("sha256") != plan["pdf"]["sha256"]:
        raise ValueError("plan/records PDF hash mismatch")
    links = result.setdefault("evidence_links", [])
    item_dispositions: dict[str, dict[str, Any]] = {}
    for item in plan.get("source_items") or []:
        if item["disposition"] == "REQUIRES_SUPPLEMENT":
            item_dispositions[item["item_id"]] = {"status": "ACCESS_FAILED", "reason": "SUPPLEMENT_NOT_ACQUIRED"}
        elif item["disposition"] == "REFERENCE_RESOLVED":
            item_dispositions[item["item_id"]] = {"status": "RESOLVED_REFERENCE", "resolved_content_item_ids": item["resolved_content_item_ids"]}
        elif item["disposition"] == "NOT_APPLICABLE":
            item_dispositions[item["item_id"]] = {"status": "NOT_APPLICABLE", "reason": "OUTSIDE_WAKG_MAT_MIX_SCOPE"}
    with fitz.open(pdf_asset['relative_path']) as identity_doc:
        qxrd_identity_aliases = _reviewed_xrf_aliases(identity_doc,
            digest_bytes(Path(pdf_asset['relative_path']).read_bytes()))
    for fact in plan.get("direct_facts", {}).get("qxrd", []):
        mat = _material_match(fact["material_label"], result.get("mats") or [])
        if fact.get('exact_material_only') and mat is not None:
            if fact['material_label'].casefold() not in {str(mat.get(k) or '').casefold() for k in ('custom_material_id','material_type')}:
                mat = None
        identity_proof = qxrd_identity_aliases.get(_fold_material(fact['material_label']))
        if mat is None and identity_proof:
            owners = [m for m in result.get('mats', [])
                      if m.get('custom_material_id') == identity_proof['target']]
            if len(owners) == 1:
                mat = owners[0]
        source_id = "source-" + fact["fact_id"]
        if mat is None:
            item_dispositions[source_id] = {"status": "QUARANTINED", "reason": "MATERIAL_RELATION_UNRESOLVED"}
            continue
        previous_qxrd = copy.deepcopy(mat.get("xrd_qxrd"))
        if previous_qxrd and ("rows" in previous_qxrd or "internal_standard" in previous_qxrd):
            raise ValueError("legacy QXRD requires source regeneration, not overwrite")
        qxrd = {"schema_version": "1.0", "xrd_pattern": None, "standard_method": None,
                "standard_xrd_pattern": None, "standard_formula": None, "standard_purity_percent": None,
                "standard_fraction_percent": None, "qxrd_phase_table": [], "amorphous_total_percent": None,
                "extensions": {"schema_version": "1.0", "source_scope": "main", "quantification_method": fact.get("method"),
                    "phase_fraction_basis": {
                        "reported_for_material": fact['material_label'],
                        "value_handling": "AS_REPORTED_NO_REBASING",
                        "standard_included_in_denominator": None,
                        "review_status": "CONTEXT_REVIEW_REQUIRED",
                        "context_sha256": fact['context_sha256']}}}
        mat["xrd_qxrd"] = qxrd
        if identity_proof:
            qxrd['extensions']['source_material_identity'] = identity_proof
        from qxrd_basis_context import reviewed_basis
        basis = reviewed_basis(Path(pdf_asset['relative_path']), fact['material_label'])
        if basis is not None:
            qxrd['extensions']['phase_fraction_basis'] = basis
        for index, row in enumerate(fact["rows"]):
            path = f"/xrd_qxrd/qxrd_phase_table/{index}/mass_fraction_percent"
            link = _evidence(mat, pdf_asset["asset_key"], path, fact["page"], row["value_bbox"], row["original_value"], index)
            links.append(link)
            qxrd["qxrd_phase_table"].append({"phase": row["phase"], "mass_fraction_percent": row["value_percent"],
                                            "identifier_system": None, "phase_identifier": None,
                                            "uncertainty_percent": None, "notes": None})
            mat.setdefault("field_provenance", {})[path] = {
                "evidence_key": link["evidence_key"],
                "original_value": row["original_value"],
                "original_unit": "wt.%",
                "formula": None,
                "extraction_method": link["extraction_method"],
                "confidence": link["confidence"],
                "review_status": "confirmed",
            }
            phase_path = f"/xrd_qxrd/qxrd_phase_table/{index}/phase"
            if not row.get("phase_bbox"):
                raise ValueError("QXRD phase name has no value-local source locator")
            phase_link = _evidence(mat, pdf_asset["asset_key"], phase_path, fact["page"], row["phase_bbox"], row["phase_original"], 40+index)
            links.append(phase_link)
            mat["field_provenance"][phase_path] = {**mat["field_provenance"][path], "evidence_key": phase_link["evidence_key"], "original_value": row["phase_original"], "original_unit": None}
            if row["phase"] == "Amorphous":
                amorphous_path = "/xrd_qxrd/amorphous_total_percent"
                if qxrd["amorphous_total_percent"] is not None:
                    raise ValueError("multiple amorphous totals require explicit reconciliation")
                qxrd["amorphous_total_percent"] = row["value_percent"]
                total_link = _evidence(mat, pdf_asset["asset_key"], amorphous_path, fact["page"], row["value_bbox"], row["original_value"], 30)
                links.append(total_link)
                mat["field_provenance"][amorphous_path] = {**mat["field_provenance"][path], "evidence_key": total_link["evidence_key"]}
        aggregate = fact.get('amorphous_total')
        if aggregate:
            path = '/xrd_qxrd/amorphous_total_percent'
            if qxrd['amorphous_total_percent'] not in (None, aggregate['value_percent']):
                raise ValueError('conflicting direct amorphous totals')
            qxrd['amorphous_total_percent'] = aggregate['value_percent']
            if aggregate.get('reported_qualifier'):
                qxrd['extensions']['amorphous_total_qualifier'] = aggregate['reported_qualifier']
            link = _evidence(mat, pdf_asset['asset_key'], path, fact['page'], aggregate['value_bbox'], aggregate['original_value'], 31)
            link['extensions'] = {'ordered_aggregate_binding':aggregate['source_parts'], 'context_sha256':fact['context_sha256']}
            links.append(link)
            mat.setdefault('field_provenance', {})[path] = {'evidence_key':link['evidence_key'],
                'original_value':aggregate['original_value'], 'original_unit':'%', 'formula':None,
                'extraction_method':link['extraction_method'], 'confidence':link['confidence'], 'review_status':'confirmed'}
            if aggregate.get('reported_qualifier'):
                mat['field_provenance'][path]['reported_qualifier'] = aggregate['reported_qualifier']
                mat['field_provenance'][path]['review_status'] = 'pending'
        standard = fact.get("internal_standard")
        if standard:
            if not standard.get('ratio_direction_verified'):
                qxrd['extensions']['internal_standard_candidate'] = {
                    'reported_ratio': standard['reported_standard_to_sample_ratio'],
                    'reason': 'STANDARD_TO_SAMPLE_RATIO_DIRECTION_UNRESOLVED',
                    'page': fact['page'], 'bbox': standard.get('ratio_bbox'),
                    'standard_formula': standard['name'], 'status': 'QUARANTINED'}
                standard = None
        if standard:
            qxrd["standard_method"] = "internal standard"
            qxrd["standard_formula"] = standard["name"]
            qxrd["extensions"]["reported_standard_to_sample_ratio"] = standard["reported_standard_to_sample_ratio"]
            qxrd["extensions"]["standard_fraction_basis"] = "spiked_mixture"
            qxrd["standard_fraction_percent"] = standard["standard_fraction_of_spiked_mixture_percent"]
            ratio_path = "/xrd_qxrd/extensions/reported_standard_to_sample_ratio"
            ratio_link = _evidence(mat, pdf_asset["asset_key"], ratio_path, fact["page"], standard.get("ratio_bbox"), standard["reported_standard_to_sample_ratio"], 20)
            links.append(ratio_link)
            mat["field_provenance"][ratio_path] = {
                "evidence_key": ratio_link["evidence_key"],
                "original_value": standard["reported_standard_to_sample_ratio"],
                "original_unit": "mass ratio",
                "formula": None,
                "extraction_method": ratio_link["extraction_method"],
                "confidence": ratio_link["confidence"],
                "review_status": "confirmed",
            }
            formula_path = "/xrd_qxrd/standard_formula"
            formula_link = _evidence(mat, pdf_asset["asset_key"], formula_path, fact["page"], standard.get("name_bbox"), standard["name"], 21)
            links.append(formula_link)
            mat["field_provenance"][formula_path] = {**mat["field_provenance"][ratio_path], "evidence_key": formula_link["evidence_key"], "original_value": standard["name"], "original_unit": None}
            method_path = "/xrd_qxrd/standard_method"
            method_link = _evidence(mat, pdf_asset["asset_key"], method_path, fact["page"], standard.get("method_bbox"), "internal standard", 23)
            links.append(method_link)
            mat["field_provenance"][method_path] = {**mat["field_provenance"][ratio_path], "evidence_key": method_link["evidence_key"], "original_value": "internal standard", "original_unit": None}
            fraction_path = "/xrd_qxrd/standard_fraction_percent"
            ratio_parts = [float(value) for value in str(standard["reported_standard_to_sample_ratio"]).split(":")]
            if len(ratio_parts) != 2 or sum(ratio_parts) == 0:
                raise ValueError("QXRD standard:sample ratio must have two nonzero-total components")
            fraction_value = float(standard["standard_fraction_of_spiked_mixture_percent"])
            computed_fraction = ratio_parts[0] / sum(ratio_parts) * 100.0
            reconstructed_ratio = fraction_value / (100.0-fraction_value)
            recorded_ratio = float(standard["standard_to_sample_mass_ratio"])
            transformation = {
                "kind": "ratio_to_fraction_percent", "formal": True,
                "formula": "standard_fraction_percent = ratio_left / (ratio_left + ratio_right) * 100",
                "inputs": {"token": standard["reported_standard_to_sample_ratio"], "components": ratio_parts, "unit": "mass ratio"},
                "output": {"value": fraction_value, "unit": "%"},
                "forward_check": {"computed": computed_fraction, "recorded": fraction_value, "tolerance": 1e-12},
                "reverse_check": {"computed": reconstructed_ratio, "recorded": recorded_ratio, "tolerance": 1e-12},
            }
            fraction_link = _evidence(mat, pdf_asset["asset_key"], fraction_path, fact["page"], standard.get("ratio_bbox"), standard["reported_standard_to_sample_ratio"], 22)
            links.append(fraction_link)
            mat["field_provenance"][fraction_path] = {
                **mat["field_provenance"][ratio_path],
                "evidence_key": fraction_link["evidence_key"],
                "formula": "standard_fraction_percent = ratio_left / (ratio_left + ratio_right) * 100",
                "original_unit": "reported standard:sample mass ratio",
                "transformation": transformation,
            }
        mat["xrd_qxrd"] = _merge_qxrd(previous_qxrd, qxrd)
        item_dispositions[source_id] = {"status": "EXTRACTED", "record_key": mat["mat_key"], "field_path": "/xrd_qxrd"}
    result.setdefault("extensions", {})["extraction_plan"] = {
        "planner_version": plan["planner_version"],
        "plan_sha256": plan["plan_sha256"],
        "source_item_dispositions": item_dispositions,
    }
    _main_source_bindings(result, plan, asset_output_dir)
    return result


def _merge_qxrd(existing, incoming, path="/xrd_qxrd"):
    if existing is None:
        return copy.deepcopy(incoming)
    if incoming is None:
        return copy.deepcopy(existing)
    if isinstance(existing, dict) and isinstance(incoming, dict):
        result = copy.deepcopy(existing)
        for key, value in incoming.items():
            result[key] = _merge_qxrd(result.get(key), value, path+"/"+key)
        return result
    if isinstance(existing, list) and isinstance(incoming, list):
        if not existing:
            return copy.deepcopy(incoming)
        if not incoming:
            return copy.deepcopy(existing)
    if existing != incoming or type(existing) is not type(incoming):
        raise ValueError(f"QXRD reconciliation required: {path}")
    return copy.deepcopy(existing)


def validate_completeness(plan: dict[str, Any], records: dict[str, Any]) -> dict[str, Any]:
    binding = (records.get("extensions") or {}).get("extraction_plan") or {}
    failures: list[str] = []
    for candidate in (records.get('extensions') or {}).get('characterization_row_candidates', []):
        failures.append('CHARACTERIZATION_ROW_UNRESOLVED:' + str(candidate.get('source_table')) + ':' + str(candidate.get('source_row')))
    for candidate in (records.get('extensions') or {}).get('recipe_column_candidates', []):
        failures.append('RECIPE_COLUMN_SEMANTICS_UNRESOLVED:' + str(candidate.get('source_table')) + ':' + str(candidate.get('column')))
    for candidate in (records.get('extensions') or {}).get('recipe_identity_candidates', []):
        failures.append('RECIPE_SPECIMEN_CONTEXT_UNRESOLVED:' + str(candidate.get('identity')))
    for obj in (records.get('extensions') or {}).get('supplement_source_objects', []):
        if obj.get('issues') or obj.get('status') != 'BOUND_NOT_ACCEPTED':
            failures.append('SUPPLEMENT_SOURCE_BINDING_UNRESOLVED:' + str(obj.get('object_key')))
    for anomaly in (records.get('extensions') or {}).get('supplement_plan_binding_anomalies', []):
        failures.append('SUPPLEMENT_PLAN_OBJECT_NOT_IN_INVENTORY:' + str(
            (anomaly.get('source_item') or {}).get('item_id')))
    for candidate in (records.get('extensions') or {}).get('supplement_table_candidates', []):
        failures.append('SUPPLEMENT_TABLE_STRUCTURE_UNRESOLVED:' + str(candidate.get('source_item_id')))
    for candidate in (records.get('extensions') or {}).get('xrd_curve_candidates', []):
        failures.append('XRD_CURVE_EXTRACTION_UNRESOLVED:' + str(candidate.get('source_label')))
    for candidate in (records.get('extensions') or {}).get('reference_formulation_candidates', []):
        if candidate.get('source_scope_review'):
            from supplement_mixed_table import verify_preserved_reference
            try:
                verify_preserved_reference(candidate, records.get('assets', []))
            except (ValueError, OSError, KeyError, TypeError) as error:
                failures.append('REFERENCE_PRESERVATION_INVALID:' + str(candidate.get('table_label')) + ':' + str(error))
            else:
                # An assumed analytical comparator is preserved, not promoted
                # to a tested MIX. This does not authorize formal publication.
                continue
        # Candidate retention is not promotion. A status string must not turn
        # unresolved reference-material ownership into a completeness PASS.
        scope_bound = (candidate.get('semantic_scope') == 'ANALYTICAL_REFERENCE_NOT_EXPERIMENTAL_MIX'
                       and candidate.get('scope_review_sha256')
                       and candidate.get('existing_mat_links_permitted') is False)
        # Both states still block. Distinguish missing scope evidence from an
        # unfinished exchange mapping; do not repeatedly re-review settled scope.
        reason = 'REFERENCE_FORMULATION_MAPPING_UNRESOLVED' if scope_bound else 'REFERENCE_FORMULATION_SCOPE_UNRESOLVED'
        failures.append(reason + ':' + str(candidate.get('table_label')))
    for mat in records.get('mats', []):
        qxrd = mat.get('xrd_qxrd') or {}
        basis = (qxrd.get('extensions') or {}).get('phase_fraction_basis') or {}
        if (qxrd.get('qxrd_phase_table') or qxrd.get('amorphous_total_percent') is not None) and basis.get('review_status') != 'SOURCE_CONTEXT_VERIFIED':
            failures.append('QXRD_PHASE_BASIS_UNVERIFIED:' + str(mat.get('mat_key')))
    for candidate in (records.get('extensions') or {}).get('xrf_alignment_candidates', []):
        if candidate.get('status') == 'QUARANTINED':
            failures.append('XRF_ALIGNMENT_UNRESOLVED:' + str(candidate.get('source_item_id')))
    if binding.get("plan_sha256") != plan.get("plan_sha256"):
        failures.append("PLAN_BINDING_MISMATCH")
    dispositions = binding.get("source_item_dispositions") or {}
    required = plan.get("completeness_contract", {}).get("required_source_item_ids") or []
    terminal = set(plan.get("completeness_contract", {}).get("terminal_dispositions") or [])
    items = {item["item_id"]: item for item in plan.get("source_items") or []}
    main_items = [item for item in items.values() if item.get("source_scope") == "main" and item.get("kind") in {"MAIN_TABLE", "MAIN_FIGURE"}]
    if plan.get("pdf", {}).get("pages", 0) and not main_items:
        failures.append("MAIN_SOURCE_INVENTORY_EMPTY")
    evidence_keys = {str(link.get("evidence_key")) for link in records.get("evidence_links") or []}
    assets = {str(asset.get("asset_key")): asset for asset in records.get("assets") or []}
    counts: dict[str, int] = {}
    for item_id in required:
        status = str((dispositions.get(item_id) or {}).get("status") or "MISSING")
        counts[status] = counts.get(status, 0) + 1
        if status not in terminal:
            failures.append(f"NONTERMINAL_SOURCE_ITEM:{item_id}:{status}")
            continue
        disposition = dispositions.get(item_id) or {}
        item = items.get(item_id) or {}
        if status == "EXTRACTED" and item.get("kind") == "MAIN_TABLE":
            linked = disposition.get("evidence_keys") or []
            if not linked or any(str(key) not in evidence_keys for key in linked):
                failures.append(f"MAIN_TABLE_MAPPING_INVALID:{item_id}")
        if status == "EXTRACTED" and item.get("kind") == "MAIN_FIGURE":
            asset_key = str(disposition.get("asset_key") or "")
            linked = disposition.get("evidence_keys") or []
            if not asset_key or asset_key not in assets or not assets[asset_key].get("sha256"):
                failures.append(f"MAIN_FIGURE_ASSET_INVALID:{item_id}")
            if not linked or any(str(key) not in evidence_keys for key in linked):
                failures.append(f"MAIN_FIGURE_EVIDENCE_INVALID:{item_id}")
    unresolved = counts.get("ACCESS_FAILED", 0) + counts.get("QUARANTINED", 0)
    coverage = (len(required) - len([f for f in failures if f.startswith("NONTERMINAL_SOURCE_ITEM")])) / len(required) if required else 1.0
    extracted_or_absent = counts.get("EXTRACTED", 0) + counts.get("TYPED_ABSENT", 0) + counts.get("NOT_APPLICABLE", 0) + counts.get("RESOLVED_REFERENCE", 0)
    recall = extracted_or_absent / len(required) if required else 0.0
    verdict = "PASS" if not failures and unresolved == 0 else "BLOCKED"
    return {
        "schema_version": 1,
        "kind": "WAKG_EXTRACTION_COMPLETENESS",
        "run_id": plan.get("run_id"),
        "plan_sha256": plan.get("plan_sha256"),
        "planned_source_items": len(required),
        "terminal_source_items": sum(counts.get(status, 0) for status in terminal),
        "status_counts": counts,
        "source_item_coverage": coverage,
        "source_item_recall": recall,
        "main_source_items": len(main_items),
        "unresolved_source_items": unresolved,
        "failures": failures,
        "verdict": verdict,
    }


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description=__doc__)
    commands = cli.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("--pdf", type=Path, required=True)
    plan.add_argument("--run-id", required=True)
    plan.add_argument("--write", type=Path, required=True)
    plan.add_argument("--supplement-inventory", type=Path, action="append", default=[])
    enrich = commands.add_parser("enrich")
    enrich.add_argument("--records", type=Path, required=True)
    enrich.add_argument("--plan", type=Path, required=True)
    enrich.add_argument("--write", type=Path, required=True)
    validate = commands.add_parser("validate")
    validate.add_argument("--records", type=Path, required=True)
    validate.add_argument("--plan", type=Path, required=True)
    validate.add_argument("--write", type=Path)
    return cli


def main() -> int:
    args = parser().parse_args()
    if args.command == "plan":
        value = build_plan(args.pdf, args.run_id, [load_json(path) for path in args.supplement_inventory])
        atomic_json(args.write, value)
    elif args.command == "enrich":
        value = enrich_records(load_json(args.records), load_json(args.plan), args.write.parent / "main-figure-assets")
        atomic_json(args.write, value)
    else:
        value = validate_completeness(load_json(args.plan), load_json(args.records))
        if args.write:
            atomic_json(args.write, value)
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return 0 if value["verdict"] == "PASS" else 2
    try:
        print(json.dumps(value, ensure_ascii=False, indent=2))
    except UnicodeEncodeError:
        print(json.dumps(value, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
