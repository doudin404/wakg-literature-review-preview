"""Extract explicitly paired material diameters, preserving respectively order."""
import hashlib
import json
import re

import fitz

VERSION = "source-narrative-psd-v1"
NUMBER = r"(?:\d+(?:\.\d+)?)"
PAIR = re.compile(
    rf"\((?P<first>[A-Za-z][A-Za-z0-9_-]*)\)\s+and\s+(?P<second_name>[^().;!?]{{1,160}}?)"
    rf"\((?P<second>[A-Za-z][A-Za-z0-9_-]*)\),\s+with\s+"
    rf"(?:(?P<median>me(?:\u00ad|-)?\s*dian)\s+)?particle\s+(?:sizes|diameters)\s+"
    rf"\(D(?P<percentile>10|50|90)\)\s+of\s+(?P<a>{NUMBER})\s+(?P<ua>[μµu]m)\s+and\s+"
    rf"(?P<b>{NUMBER})\s+(?P<ub>[μµu]m),?\s+respectively\b"
)


def matches(text):
    for match in PAIR.finditer(text):
        prefix = re.split(r"[.;!?]\s+", text[:match.start()])[-1]
        if re.search(r"\([A-Za-z][A-Za-z0-9_-]*\)", prefix):
            continue  # Never recover only the tail of a longer material list.
        if re.search(r"\band\b", match["second_name"]):
            continue
        if match["median"] and match["percentile"] != "50":
            continue
        if match["first"] != match["second"]:
            yield match


def candidates(document, scopes):
    found = []
    for page_number, regions in scopes.items():
        page = document[page_number - 1]
        blocks = {}
        for word in page.get_text("words"):
            if any(region.contains(fitz.Rect(word[:4])) for region in regions):
                blocks.setdefault(word[5], []).append(word)
        for words in blocks.values():
            words.sort(key=lambda word: (word[6], word[7]))
            text = ""
            spans = []
            for word in words:
                start = len(text)
                text += word[4] + " "
                spans.append((start, len(text) - 1, word))
            for match in matches(text):
                parts = []
                for name in ("first", "second", "a", "b", "ua", "ub", "percentile"):
                    start, end = match.span(name)
                    covering = [word for left, right, word in spans if left <= start and right >= end]
                    if len(covering) != 1:
                        break
                    word = covering[0]
                    native = fitz.Rect(word[:4])
                    box = native
                    snippet = page.get_textbox(box).strip()
                    # Oversized font ascenders can cross neighboring baselines.
                    # Accept a tighter box only after exact PDF token read-back.
                    for fraction in (0.15, 0.25, 0.30):
                        if snippet == word[4]:
                            break
                        inset = native.height * fraction
                        box = fitz.Rect(native.x0, native.y0 + inset, native.x1, native.y1 - inset)
                        snippet = page.get_textbox(box).strip()
                    if snippet != word[4] or match[name] not in snippet:
                        break
                    parts.append({"part": name, "snippet": snippet, "bbox": list(box)})
                if len(parts) != 7:
                    continue
                for label, value_part in (("first", "a"), ("second", "b")):
                    found.append({"material": match[label], "field_path": f"/particle_size_distribution/d{match['percentile']}_um",
                                  "value": float(match[value_part]), "raw": match[value_part], "unit": match["u" + value_part],
                                  "page": page_number, "value_part": value_part, "parts": parts,
                                  "relationship": "respectively", "source_sentence": match.group()})
    return found


def enrich(records, pdf_path, asset_key, scopes):
    with fitz.open(pdf_path) as document:
        claims = candidates(document, scopes(document))
    planned = {}
    for claim in claims:
        owners = [mat for mat in records["mats"] if mat.get("custom_material_id") == claim["material"]]
        if len(owners) != 1:
            continue
        mat = owners[0]
        field = claim["field_path"].rsplit("/", 1)[1]
        key = (mat["mat_key"], field)
        old = mat["particle_size_distribution"].get(field)
        if old is not None and old != claim["value"]:
            raise ValueError("narrative PSD contradicts existing field")
        if key in planned and planned[key][1]["value"] != claim["value"]:
            raise ValueError("conflicting narrative PSD claims")
        planned.setdefault(key, (mat, claim))
    for (record_key, field), (mat, claim) in planned.items():
        if mat["particle_size_distribution"].get(field) is not None:
            continue
        part = next(p for p in claim["parts"] if p["part"] == claim["value_part"])
        evidence_key = "ev-psd-" + hashlib.sha256(json.dumps([record_key, claim], sort_keys=True).encode()).hexdigest()[:24]
        records["evidence_links"].append({
            "evidence_key": evidence_key, "asset_key": asset_key, "record_type": "mat", "record_key": record_key,
            "paper_key": mat["paper_key"], "field_path": claim["field_path"], "page": claim["page"],
            "section": "Materials", "table_number": None, "figure_number": None, "bbox": part["bbox"],
            "snippet": part["snippet"], "snippet_sha256": hashlib.sha256(part["snippet"].encode()).hexdigest(),
            "extraction_method": VERSION, "confidence": 0.99,
            "extensions": {"ordered_material_value_binding": claim}})
        mat["particle_size_distribution"][field] = claim["value"]
        mat.setdefault("field_provenance", {})[claim["field_path"]] = {
            "evidence_key": evidence_key, "original_value": claim["raw"], "original_unit": claim["unit"],
            "formula": None, "extraction_method": VERSION, "confidence": 0.99, "review_status": "confirmed"}
