"""Bind declared material names to original PDF text, never inferred types."""
from __future__ import annotations

import hashlib
import re
import unicodedata
import json
from pathlib import Path

import fitz

VERSION = "source-material-identity-v2-material-sections"
ACCEPTANCE_SHA256 = "e85594435f462c71edfad6cc9bb18e57d2cece44245c575b0967bd3233353514"

# Source-name expansions, never platform enum values or mixture roles. SS is
# deliberately paper-scoped. A failed source lookup stays unresolved.
TYPE_NAMES = {
    "GGBS": ["ground granulated blast-furnace slag", "ground granulated blast furnace slag"],
    "GGBFS": ["ground granulated blast-furnace slag", "ground granulated blast furnace slag"],
    "FA": ["Class F fly ash", "fly ash"],
    "fly ash": ["Class F fly ash", "fly ash"],
    "Class F fly ash": ["Class F fly ash"],
    "Calcined kaolin": ["metakaolin", "calcined kaolin"],
    "Pure kaolin": ["kaolin"],
    "CDW": ["construction and demolition waste"],
    "RHA": ["rice husk ash"],
    "LP": ["limestone powder"],
    "CCR": ["calcium carbide residue"],
    "MgO": ["light-burned magnesium oxide", "magnesium oxide", "light-burned MgO"],
    "BFS": ["blast furnace slag"],
    "EFS": ["electric arc furnace slag"],
    "PC": ["Portland cement"],
    "sand": ["silica sand", "sand"],
    "flue gas residue": ["flue gas residue", "flue gas residues"],
}
IDENTITY_ALIASES = {
    ("4df39133d239", "fly ash"): {"fa"},
    ("993d666cc40b", "slag"): {"ggbfs"},
    ("993d666cc40b", "class f fly ash"): {"fly ash"},
    ("b7acfcbaf503", "flue gas residue"): {"fgr"},
}


def material_scopes(document):
    """Use numbered material subsections in PDF reading order, not first hits."""
    scopes = {}
    active = False
    heading = re.compile(r"^\d+(?:\.\d+)+\.?\s+([A-Za-z][^\n]*)$")
    for number, page in enumerate(document, 1):
        for block in page.get_text("blocks"):
            first = str(block[4]).strip().splitlines()[0] if str(block[4]).strip() else ""
            match = heading.match(first)
            if match:
                title = folded(match.group(1))
                active = bool(re.fullmatch(r"(?:raw )?materials(?: specifications| used)?", title))
            elif re.match(r"^(?:\d+\.?\s+[A-Za-z]|references\b|bibliography\b)", first, re.I):
                active = False
            if active:
                scopes.setdefault(number, []).append(fitz.Rect(block[:4]))
    return scopes


def locate(document, name, scopes=None):
    scopes = material_scopes(document) if scopes is None else scopes
    for number, page in enumerate(document, 1):
        allowed = scopes.get(number, [])
        if not allowed:
            continue
        for rect in page.search_for(str(name)):
            center = fitz.Point((rect.x0+rect.x1)/2, (rect.y0+rect.y1)/2)
            if not any(center in region for region in allowed):
                continue
            words = [w for w in page.get_text("words")
                     if abs((w[1]+w[3])/2-(rect.y0+rect.y1)/2) < rect.height * 0.4
                     and min(w[2], rect.x1)-max(w[0], rect.x0) > 1]
            words.sort(key=lambda w: w[0])
            full_words = " ".join(w[4] for w in words).strip(".,;:()[]")
            # search_for can match 'calcined' inside 'uncalcined'. Original
            # whole-word boundaries, not the clipped substring, decide identity.
            if folded(full_words) != folded(name):
                continue
            clipped = page.get_textbox(rect).strip()
            raw = next((line.strip() for line in clipped.splitlines() if folded(line) == folded(name)), "")
            if raw:
                return number, list(rect), raw
    return None


def locate_parts(document, name, scopes=None):
    """Whole-word, same-block matches across wrapped PDF lines.

    Keep each line box separately: a union rectangle may include unrelated
    adjacent prose and is not an exact source token.
    """
    scopes = material_scopes(document) if scopes is None else scopes
    target = re.sub(r"[^a-z0-9]", "", folded(name))
    for number, page in enumerate(document, 1):
        groups = {}
        for word in page.get_text("words"):
            center = fitz.Point((word[0]+word[2])/2, (word[1]+word[3])/2)
            if any(center in box for box in scopes.get(number, [])):
                groups.setdefault(word[5], []).append(word)
        for words in groups.values():
            words.sort(key=lambda w: (w[6], w[7]))
            for start in range(len(words)):
                accumulated = ""
                selected = []
                for word in words[start:start+len(name.split())+3]:
                    accumulated += re.sub(r"[^a-z0-9]", "", folded(word[4]))
                    selected.append(word)
                    if len(accumulated) >= len(target):
                        break
                if accumulated != target:
                    continue
                lines = {}
                for word in selected:
                    lines.setdefault(word[6], []).append(word)
                parts = []
                for line in lines.values():
                    box = [min(w[0] for w in line), min(w[1] for w in line),
                           max(w[2] for w in line), max(w[3] for w in line)]
                    raw = " ".join(w[4] for w in line)
                    clip = page.get_textbox(fitz.Rect(box))
                    # Word extraction and textbox extraction can differ on
                    # ligatures. Persist literal textbox bytes, not folded text.
                    variants = (raw, unicodedata.normalize("NFKC", raw))
                    verbatim = next((v for v in variants if v in clip), None)
                    if verbatim is None:
                        break
                    parts.append({"page": number, "bbox": box, "snippet": verbatim})
                else:
                    return parts
    return []


def abbreviation_definition(document, names, aliases):
    """Require an explicit parenthetical definition, never a distant name hit."""
    compact = lambda text: re.sub(r"[^a-z0-9()]", "", folded(text))
    matches = []
    for number, page in enumerate(document, 1):
        for block in page.get_text("blocks"):
            text = compact(block[4])
            for name in names:
                for alias in aliases:
                    if compact(alias) == compact(name):
                        continue
                    patterns = (compact(name)+"("+compact(alias)+")", compact(alias)+"("+compact(name)+")")
                    if not any(pattern in text for pattern in patterns):
                        continue
                    scope = {number: [fitz.Rect(block[:4])]}
                    parts = locate_parts(document, name, scope)
                    alias_parts = locate_parts(document, alias, scope)
                    if parts and alias_parts:
                        match = {"name": name, "alias": alias, "source_parts": parts, "alias_parts": alias_parts}
                        if match not in matches:
                            matches.append(match)
    return matches


def enrich_types(records, pdf, asset_key):
    """Separate source identity from semantic_role; no type from a role label."""
    unresolved = []
    candidates = []
    with fitz.open(pdf) as document:
        scopes = material_scopes(document)
        for mat in records["mats"]:
            name = mat.get("custom_material_id")
            if not name:
                unresolved.append(mat["mat_key"])
                continue
            names = TYPE_NAMES.get(name, [name])
            if name == "SS" and "7717e4cb8387" in mat["paper_key"]:
                names = ["steel slag"]
            located = next((hit for term in names if (hit := locate(document, term, scopes))), None)
            if located is None:
                mat["material_type"] = None
                unresolved.append(mat["mat_key"])
                parts = next((hit for term in names if (hit := locate_parts(document, term, scopes))), [])
                aliases = [name]
                for (short, identity_name), values in IDENTITY_ALIASES.items():
                    if short in mat.get("paper_key", "") and folded(name) == identity_name:
                        aliases.extend(sorted(values))
                alias_parts = next((hit for alias in aliases if (hit := locate_parts(document, alias, scopes))), [])
                definitions = abbreviation_definition(document, names, aliases) if alias_parts and not parts else []
                candidates.append({"record_key": mat["mat_key"], "field_path": "/material_type",
                                   "requested_source_names": names, "source_parts": parts,
                                   "local_identity_parts": alias_parts,
                                   "abbreviation_definitions": definitions,
                                   "status": "PENDING_COMPOSITE_SOURCE_REVIEW" if parts else "PENDING_ABBREVIATION_DEFINITION",
                                   "asset_key": asset_key})
                continue
            page, bbox, raw = located
            key = "ev-material-type-" + hashlib.sha256(f"{asset_key}|{mat['mat_key']}".encode()).hexdigest()[:24]
            link = {"evidence_key": key, "asset_key": asset_key, "record_key": mat["mat_key"],
                    "record_type": "mat", "field_path": "/material_type", "page": page, "bbox": bbox,
                    "section": None, "table_number": None, "figure_number": None, "snippet": raw,
                    "snippet_sha256": hashlib.sha256(raw.encode()).hexdigest(), "extraction_method": VERSION,
                    "confidence": 1.0}
            existing = next((e for e in records["evidence_links"] if e["evidence_key"] == key), None)
            if existing is not None and existing != link:
                raise ValueError("material type evidence drift")
            if existing is None:
                records["evidence_links"].append(link)
            mat["material_type"] = raw
            mat.setdefault("field_provenance", {})["/material_type"] = {
                "evidence_key": key, "original_value": raw, "original_unit": None, "formula": None,
                "extraction_method": VERSION, "confidence": 1.0, "review_status": "pending"}
    records.setdefault("extensions", {})["material_identity_resolution"] = {
        "version": VERSION, "unresolved_types": unresolved, "candidates": candidates}
    return unresolved


def promote_reviewed_types(records, pdf, asset_key, acceptance_path):
    """Replay reviewed locator recipes using PDF bytes, not old result files."""
    raw_acceptance = Path(acceptance_path).read_bytes()
    if hashlib.sha256(raw_acceptance).hexdigest() != ACCEPTANCE_SHA256:
        raise ValueError("material identity acceptance hash mismatch")
    acceptance = json.loads(raw_acceptance)
    pdf_sha = hashlib.sha256(Path(pdf).read_bytes()).hexdigest()
    mats = {m["mat_key"]: m for m in records["mats"]}
    promoted = []
    with fitz.open(pdf) as document:
        for recipe in acceptance["specifications"]:
            if recipe["pdf_sha256"] != pdf_sha:
                continue
            mat = mats.get(recipe["record_key"])
            if mat is None or mat["custom_material_id"] != recipe["custom_material_id"]:
                raise ValueError("reviewed material identity owner mismatch")
            if mat.get("material_type") not in (None, recipe["material_type"]):
                raise ValueError("reviewed material identity conflicts with direct extraction")
            all_parts = recipe["source_parts"] + recipe["definition_parts"] + recipe["local_identity_parts"]
            evidence = []
            for index, part in enumerate(all_parts):
                page = part["page"]
                if not 1 <= page <= len(document) or part["snippet"] not in document[page-1].get_textbox(fitz.Rect(part["bbox"])):
                    raise ValueError("reviewed material identity source changed")
                key = "ev-reviewed-material-" + hashlib.sha256(f"{mat['mat_key']}|{index}|{ACCEPTANCE_SHA256}".encode()).hexdigest()[:24]
                evidence.append({"evidence_key": key, "asset_key": asset_key, "record_key": mat["mat_key"],
                                 "record_type": "mat", "field_path": "/material_type", **part,
                                 "snippet_sha256": hashlib.sha256(part["snippet"].encode()).hexdigest(),
                                 "section": None, "table_number": None, "figure_number": None,
                                 "extraction_method": "reviewed-material-identity-v1", "confidence": 1.0})
            existing = {e["evidence_key"]: e for e in records["evidence_links"]}
            for link in evidence:
                if link["evidence_key"] in existing and existing[link["evidence_key"]] != link:
                    raise ValueError("reviewed material evidence conflict")
            for link in evidence:
                if link["evidence_key"] not in existing:
                    records["evidence_links"].append(link)
            mat["material_type"] = recipe["material_type"]
            mat.setdefault("field_provenance", {})["/material_type"] = {
                "evidence_key": evidence[0]["evidence_key"], "original_value": "\n".join(p["snippet"] for p in recipe["source_parts"]),
                "original_unit": None, "formula": "Join wrapped source name and resolve the explicitly defined same-paper abbreviation; no class or role inference.",
                "extraction_method": "reviewed-material-identity-v1", "confidence": 1.0, "review_status": "confirmed",
                "source_parts": [{**p, "evidence_key": e["evidence_key"]} for p, e in zip(all_parts, evidence)],
                "acceptance_sha256": ACCEPTANCE_SHA256}
            promoted.append(mat["mat_key"])
    state = records.setdefault("extensions", {}).setdefault("material_identity_resolution", {})
    state["unresolved_types"] = [key for key in state.get("unresolved_types", []) if key not in promoted]
    state["reviewed_types"] = promoted
    state["acceptance_sha256"] = ACCEPTANCE_SHA256
    return promoted


def folded(value):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value))).strip().casefold()


def enrich(records, pdf, asset_key):
    """Only exact full names; aliases and type classification need semantic proof."""
    links = {e["evidence_key"]: e for e in records["evidence_links"]}
    unresolved = []
    added = 0
    with fitz.open(pdf) as document:
        scopes = material_scopes(document)
        for mat in records["mats"]:
            path = "/custom_material_id"
            name = mat.get("custom_material_id")
            if name is None:
                continue
            provenance = mat.setdefault("field_provenance", {})
            prior = links.get(provenance.get(path, {}).get("evidence_key"))
            if prior is not None:
                if prior.get("record_key") != mat["mat_key"] or prior.get("field_path") != path:
                    raise ValueError("material identity provenance ownership mismatch")
                source_page = prior.get("page")
                snippet = str(prior.get("snippet") or "")
                original = provenance[path].get("original_value")
                permitted = {folded(name)}
                for (short, identity_name), aliases in IDENTITY_ALIASES.items():
                    if short in mat.get("paper_key", "") and folded(name) == identity_name:
                        permitted.update(aliases)
                if folded(original) not in permitted or folded(original) != folded(snippet):
                    raise ValueError("material identity current name or original value mismatch")
                # Supplement identity locators use a different asset protocol;
                # leave that protocol to its own validator, never relabel it PDF.
                if prior.get("asset_key") == asset_key:
                    if not isinstance(source_page, int) or not 1 <= source_page <= len(document):
                        raise ValueError("material identity source page mismatch")
                    clip = document[source_page-1].get_textbox(fitz.Rect(prior["bbox"]))
                    if not snippet or snippet not in clip:
                        raise ValueError("material identity source text mismatch")
                continue
            located = locate(document, name, scopes)
            if located is None:
                unresolved.append(mat["mat_key"])
                continue
            page, bbox, raw = located
            key = "ev-material-identity-" + hashlib.sha256(f"{asset_key}|{mat['mat_key']}|{path}".encode()).hexdigest()[:24]
            link = {"evidence_key": key, "asset_key": asset_key, "record_key": mat["mat_key"],
                    "record_type": "mat", "field_path": path, "page": page, "bbox": bbox,
                    "section": None, "table_number": None, "figure_number": None,
                    "snippet": raw, "snippet_sha256": hashlib.sha256(raw.encode()).hexdigest(),
                    "extraction_method": VERSION, "confidence": 1.0}
            if key in links:
                raise ValueError("orphaned material identity evidence collision")
            records["evidence_links"].append(link)
            links[key] = link
            provenance[path] = {"evidence_key": key, "original_value": raw, "original_unit": None,
                                "formula": None, "extraction_method": VERSION, "confidence": 1.0,
                                "review_status": "pending"}
            added += 1
    return {"added": added, "unresolved": unresolved}
