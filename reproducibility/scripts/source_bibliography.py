"""Read bibliography from frozen PDF text, with record-specific provenance."""
from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Any

import fitz

VERSION = "source-bibliography-v1"
DOI = re.compile(r"10\.\d{4,9}/[-._;()/:a-zA-Z0-9]+")


def folded(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).replace("\u00ad", "")
    return re.sub(r"\s+", " ", text).strip().casefold()


def read_bibliography(pdf: str) -> dict[str, dict[str, Any]]:
    with fitz.open(pdf) as doc:
        page = doc[0]
        blocks = [b for b in page.get_text("blocks") if len(b) < 7 or b[6] == 0]
        title_hint = folded((doc.metadata or {}).get("title") or "")
        title_blocks = [b for b in blocks if title_hint and folded(str(b[4])) == title_hint]
        if not title_blocks:
            # Publisher PDFs without metadata normally print the article title
            # in their largest multiline text block. Reject ambiguous choices.
            candidates = []
            for block in page.get_text("dict")["blocks"]:
                if block.get("type") != 0:
                    continue
                spans = [s for line in block["lines"] for s in line["spans"]]
                text = " ".join(s["text"] for s in spans)
                if len(text.split()) >= 8 and not re.search(r"https?://|www\.", text):
                    candidates.append((max(s["size"] for s in spans), (*block["bbox"], text)))
            candidates.sort(key=lambda item: -item[0])
            if candidates and (len(candidates) == 1 or candidates[0][0] > candidates[1][0]):
                title_blocks = [candidates[0][1]]
        result = {}
        if len(title_blocks) == 1:
            b = title_blocks[0]
            raw = page.get_textbox(fitz.Rect(b[:4])).strip()
            result["title"] = {"value": re.sub(r"\s+", " ", unicodedata.normalize("NFKC", raw)), "raw": raw, "bbox": list(b[:4]), "page": 1}
        matches = {}
        for b in blocks:
            text = str(b[4])
            for match in DOI.finditer(text):
                token = match.group().rstrip(".,;)")
                # First-page DOI links identify the article; references in the
                # abstract are not accepted unless formatted as a DOI link.
                if "doi.org/" not in text and "doi:" not in text.casefold():
                    continue
                rects = page.search_for(token, clip=fitz.Rect(b[:4]))
                if len(rects) == 1:
                    matches[token.casefold()] = {"value": token.casefold(), "raw": token, "bbox": list(rects[0]), "page": 1}
        if len(matches) == 1:
            result["doi"] = next(iter(matches.values()))
        years = {}
        for b in blocks:
            # Journal volume (publication year), not received/accepted dates or
            # a year embedded in the DOI.
            match = re.search(r"\b\d+\s*\((19\d{2}|20\d{2})\)\s*\d", str(b[4]))
            if match:
                rects = page.search_for(match.group(1), clip=fitz.Rect(b[:4]))
                if len(rects) == 1:
                    years[int(match.group(1))] = {"value": int(match.group(1)), "raw": match.group(1), "bbox": list(rects[0]), "page": 1}
        if len(years) == 1:
            result["year"] = next(iter(years.values()))
        return result


def enrich(records: dict[str, Any], pdf: str, asset_key: str) -> None:
    fields = read_bibliography(pdf)
    existing_evidence = {item["evidence_key"]: item for item in records["evidence_links"]}
    targets = [(p, p["paper_key"], "paper", {k: "/" + k for k in fields}) for p in records["papers"]]
    targets += [(m, m["mat_key"], "mat", {k: "/literature_title" if k == "title" else "/" + k for k in fields}) for m in records["mats"]]
    targets += [(m, m["mix_key"], "mix", {k: "/modules/identity_source_specimen/literature_source/" + k for k in fields}) for m in records["mixes"]]
    for record, key, kind, paths in targets:
        for name, path in paths.items():
            source = fields[name]
            target = record
            parts = path.strip("/").split("/")
            for part in parts[:-1]:
                target = target.setdefault(part, {})
            if target.get(parts[-1]) not in (None, source["value"]):
                raise ValueError(f"bibliographic conflict: {key} {path}")
            target[parts[-1]] = source["value"]
            evidence_key = "ev-bibliography-" + hashlib.sha256(f"{asset_key}|{key}|{path}".encode()).hexdigest()[:24]
            link = {"evidence_key": evidence_key, "asset_key": asset_key, "record_key": key, "record_type": kind, "field_path": path, "page": 1, "bbox": source["bbox"], "section": "article bibliography", "table_number": None, "figure_number": None, "snippet": source["raw"], "snippet_sha256": hashlib.sha256(source["raw"].encode()).hexdigest(), "extraction_method": VERSION, "confidence": 1.0}
            if evidence_key in existing_evidence and existing_evidence[evidence_key] != link:
                raise ValueError(f"bibliography evidence drift: {evidence_key}")
            if evidence_key not in existing_evidence:
                records["evidence_links"].append(link)
                existing_evidence[evidence_key] = link
            record.setdefault("field_provenance", {})[path] = {"evidence_key": evidence_key, "original_value": source["raw"], "original_unit": None, "formula": None, "extraction_method": VERSION, "confidence": 1.0, "review_status": "pending"}
    records.setdefault("extensions", {})["bibliography_extraction"] = {"version": VERSION, "fields": sorted(fields), "missing": sorted({"title", "doi", "year"} - fields.keys())}
