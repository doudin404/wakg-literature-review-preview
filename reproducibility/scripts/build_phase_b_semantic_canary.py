"""Build the three-paper Phase-B semantic canary from source evidence only.

The build path consumes frozen PDFs, the Phase-A routing bundle, and a
value-free semantic rule schema.  Prior accepted records are intentionally not
an input to generation.  Comparison is a separate, post-publication command.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

import fitz


SCHEMA_VERSION = 1
CANARY_RUNS = (
    "user10v2-55c422590f17",
    "user10v2-7f610942ec2f",
    "user10v2-f15f24af118a",
)
EXPECTED = {
    "user10v2-55c422590f17": {"mixes": 44, "mats": 1, "observations": 216},
    "user10v2-7f610942ec2f": {"mixes": 3, "mats": 2, "observations": 0},
    "user10v2-f15f24af118a": {"mixes": 9, "mats": 4, "observations": 11},
}
FORBIDDEN_RULE_KEYS = {
    "value", "values", "answer", "answers", "expected_mix_ids", "row_answers",
    "reported_value", "scientific_values", "records", "claims",
}


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    raw = value if isinstance(value, bytes) else canonical(value).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    os.replace(temporary, path)


def reject_answers(value: Any, location: str = "$") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).casefold() in FORBIDDEN_RULE_KEYS:
                raise ValueError(f"scientific answer key forbidden at {location}.{key}")
            reject_answers(child, f"{location}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            reject_answers(child, f"{location}[{index}]")


def load_rules(path: Path) -> dict[str, Any]:
    rules = json.loads(path.read_text(encoding="utf-8"))
    reject_answers(rules)
    if rules.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported semantic rules schema")
    run_ids = tuple(paper.get("run_id") for paper in rules.get("papers", []))
    if run_ids != CANARY_RUNS:
        raise ValueError("semantic rules must contain exactly the frozen canary papers")
    return rules


def load_source_inputs(root: Path, routing_path: Path, rules_path: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    routing = json.loads(routing_path.read_text(encoding="utf-8"))
    rules = load_rules(rules_path)
    schema_path = root / "fixtures" / "user_corpus_routing_schemas_v1.json"
    routing_schema = json.loads(schema_path.read_text(encoding="utf-8"))
    papers = {paper["run_id"]: paper for paper in routing_schema["papers"]}
    if set(CANARY_RUNS) - set(papers):
        raise ValueError("routing schema missing a canary paper")
    for run_id in CANARY_RUNS:
        paper = papers[run_id]
        pdf = root / paper["pdf_relative_path"]
        if file_digest(pdf) != paper["pdf_sha256"]:
            raise ValueError(f"frozen PDF hash mismatch: {run_id}")
    source = {
        "routing_bundle_sha256": file_digest(routing_path),
        "routing_schema_sha256": file_digest(schema_path),
        "semantic_rules_sha256": file_digest(rules_path),
    }
    return routing, rules, {"papers": papers, "hashes": source}


def cell_tokens(packet: dict[str, Any], column_key: str) -> list[dict[str, Any]]:
    return next((cell["tokens"] for cell in packet["cells"] if cell["column_key"] == column_key), [])


def shared_cell_tokens(packet: dict[str, Any], column_key: str, packets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Resolve explicit routing inheritance, never infer from a bare blank."""
    direct=cell_tokens(packet,column_key)
    if direct:return direct
    current=packet;chain=[]
    while True:
        carries=[c for c in current.get("carry_provenance",[]) if c["column_key"]==column_key]
        if not carries:return []
        if len(carries)!=1:raise ValueError("ambiguous shared-cell provenance")
        carry=carries[0];row=carry["inherited_from_source_row_index"]
        if row>=current["source_row_index"]:raise ValueError("non-decreasing shared-cell provenance")
        matches=[p for p in packets if p["run_id"]==packet["run_id"] and p["table_id"]==packet["table_id"] and p["page"]==packet["page"] and p["source_row_index"]==row and p["pdf_sha256"]==packet["pdf_sha256"]]
        if len(matches)!=1:raise ValueError("shared-cell source row missing or ambiguous")
        chain.append(carry);current=matches[0];direct=cell_tokens(current,column_key)
        if direct:
            observed=hashlib.sha256(json.dumps(direct,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()).hexdigest()
            if any(c["inherited_token_hash"]!=observed for c in chain):raise ValueError("shared-cell token hash mismatch")
            binding={"kind":"shared_table_cell","table":packet["table_id"],"column":column_key,
                     "source_row":row,"target_row":packet["source_row_index"],"source_tokens_sha256":observed}
            return [{**token,"source_binding":binding} for token in direct]


def union_bbox(tokens: list[dict[str, Any]]) -> list[float]:
    boxes = [token["bbox"] for token in tokens]
    return [
        round(min(box[0] for box in boxes), 3), round(min(box[1] for box in boxes), 3),
        round(max(box[2] for box in boxes), 3), round(max(box[3] for box in boxes), 3),
    ]


def make_evidence(run_id: str, packet: dict[str, Any], column: str, token: dict[str, Any], ordinal: int, method: str | None = None) -> dict[str, Any]:
    key = f"ev-{run_id[9:]}-{packet['table_id']}-r{packet['source_row_index']}-{column}-{ordinal}"
    return {
        "evidence_key": key,
        "source_page": packet["page"],
        "source_table": packet["table_id"],
        "source_row": packet["source_row_index"],
        "bbox": [round(float(x), 3) for x in token["bbox"]],
        "snippet_sha256": hashlib.sha256(str(token["token"]).encode("utf-8")).hexdigest(),
        "source_token": str(token["token"]),
        "extraction_method": method or packet["extraction_method"],
        "confidence": 0.99,
    }


def reported_value(column: str, unit: str | None, token: dict[str, Any], evidence: dict[str, Any], *, semantic_status: str = "source_reported") -> dict[str, Any]:
    typed_state = token.get("typed_state")
    if typed_state == "null":
        value = None
        status = "not_applicable"
    else:
        value = token.get("typed")
        status = semantic_status
    return {
        "parameter": column,
        "original_value": str(token.get("token")),
        "original_unit": unit,
        "value": value,
        "unit": unit,
        "status": status,
        "evidence_key": evidence["evidence_key"],
        "extraction_method": evidence["extraction_method"],
        "confidence": evidence["confidence"],
    }


def page_words(pdf: Path, cache_path: Path | None) -> tuple[dict[int, list[tuple[Any, ...]]], bool]:
    if cache_path and cache_path.exists():
        raw = json.loads(cache_path.read_text(encoding="utf-8"))
        return {int(page): [tuple(word) for word in words] for page, words in raw.items()}, True
    doc = fitz.open(pdf)
    words = {index + 1: page.get_text("words") for index, page in enumerate(doc)}
    if cache_path:
        atomic_json(cache_path, words)
    return words, False


def routing_column_ranges(routing_paper: dict[str, Any], table_id: str) -> dict[str, list[float]]:
    table = next(table for table in routing_paper["tables"] if table["table_id"] == table_id)
    return {column["key"]: column.get("x") for column in table["columns"] if column.get("x")}


def recover_dash(packet: dict[str, Any], column: str, words: dict[int, list[tuple[Any, ...]]], ranges: dict[str, list[float]]) -> dict[str, Any] | None:
    x0, x1 = ranges[column]
    y0, y1 = packet["row_bbox"][1], packet["row_bbox"][3]
    candidates = [word for word in words[packet["page"]] if x0 <= (word[0] + word[2]) / 2 < x1 and y0 - 1 <= word[1] <= y1 + 4]
    for word in candidates:
        text = str(word[4])
        try:
            float(text.rstrip("%"))
        except ValueError:
            return {"column_key": column, "bbox": [word[0], word[1], word[2], word[3]], "token": text, "typed": None, "typed_state": "null"}
    return None


def line_groups(words: list[tuple[Any, ...]], low: float, high: float, tolerance: float = 2.2) -> list[list[tuple[Any, ...]]]:
    selected = sorted((word for word in words if low <= word[1] < high), key=lambda word: (word[1], word[0]))
    groups: list[list[tuple[Any, ...]]] = []
    for word in selected:
        if not groups or abs(word[1] - groups[-1][0][1]) > tolerance:
            groups.append([word])
        else:
            groups[-1].append(word)
    return [sorted(group, key=lambda word: word[0]) for group in groups]


def token_from_word(word: tuple[Any, ...]) -> dict[str, Any]:
    text = str(word[4])
    try:
        typed: Any = float(text.rstrip("%"))
        state = "number"
    except ValueError:
        typed, state = text, "text"
    return {"bbox": [word[0], word[1], word[2], word[3]], "token": text, "typed": typed, "typed_state": state}


def extract_extra_observations(rule: dict[str, Any], words: dict[int, list[tuple[Any, ...]]], mixes: list[dict[str, Any]], evidence: list[dict[str, Any]], run_id: str) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    mix_by_source = {(mix["modules"]["identity_source_specimen"]["source_table"], mix["modules"]["identity_source_specimen"]["source_row"]): mix for mix in mixes}
    for table in rule.get("extra_observation_tables", []):
        page = table["page"]
        header_lines = line_groups(words[page], *table["header_y"])
        row_lines = line_groups(words[page], *table["row_y"])
        if table.get("entity_headers_regex"):
            headers = [word for line in header_lines for word in line if re.match(table["entity_headers_regex"], str(word[4]))]
            targets = sorted((mix for (table_id, _), mix in mix_by_source.items() if table_id == table["target_table"]), key=lambda mix: mix["modules"]["identity_source_specimen"]["source_row"])
            if len(headers) != len(targets):
                raise ValueError(f"header/mix mismatch in {table['table_id']}")
            for measurement in table["measurement_rows"]:
                line = next(line for line in row_lines if re.search(measurement["label_regex"], " ".join(str(word[4]) for word in line if word[0] < 125)))
                for index, (header, target) in enumerate(zip(headers, targets), 1):
                    numeric = min((word for word in line if word[0] >= 125), key=lambda word: abs(word[0] - header[0]))
                    token = token_from_word(numeric)
                    packet = {"page": page, "table_id": table["table_id"], "source_row_index": index, "extraction_method": "pymupdf_words_header_aligned_v1"}
                    ev = make_evidence(run_id, packet, measurement["kind"], token, index)
                    evidence.append(ev)
                    value = reported_value(measurement["kind"], measurement["unit"], token, ev)
                    target["modules"]["performance"].append(value)
                    observations.append(value)
        else:
            header = next(word for line in header_lines for word in line if str(word[4]) == table["select_header"])
            target = mix_by_source[(table["target_table"], table["target_source_row"])]
            for index, measurement in enumerate(table["measurement_rows"], 1):
                line = next(line for line in row_lines if re.search(measurement["label_regex"], " ".join(str(word[4]) for word in line if word[0] < 150)))
                numeric = min((word for word in line if word[0] >= 150), key=lambda word: abs(word[0] - header[0]))
                token = token_from_word(numeric)
                packet = {"page": page, "table_id": table["table_id"], "source_row_index": index, "extraction_method": "pymupdf_words_selected_header_v1"}
                ev = make_evidence(run_id, packet, measurement["kind"], token, index)
                evidence.append(ev)
                value = reported_value(measurement["kind"], measurement["unit"], token, ev)
                target["modules"]["performance"].append(value)
                observations.append(value)
    return observations


def build_paper(root: Path, output_root: Path, routing: dict[str, Any], rule: dict[str, Any], routing_paper: dict[str, Any], input_hashes: dict[str, str], cache_root: Path | None) -> dict[str, Any]:
    run_id = rule["run_id"]
    packets = [packet for packet in routing["packets"] if packet["run_id"] == run_id]
    packets.sort(key=lambda packet: (packet["table_id"], packet["source_row_index"]))
    pdf = root / routing_paper["pdf_relative_path"]
    cache_path = cache_root / f"{routing_paper['pdf_sha256']}-words.json" if cache_root else None
    words, cache_hit = page_words(pdf, cache_path)
    table_units = {table["table_id"]: {column["key"]: column.get("unit") for column in table["columns"]} for table in routing_paper["tables"]}
    evidence: list[dict[str, Any]] = []
    mixes: list[dict[str, Any]] = []
    quarantine: list[dict[str, Any]] = []
    for packet in packets:
        table_rule = rule["tables"][packet["table_id"]]
        ranges = routing_column_ranges(routing_paper, packet["table_id"])
        identity_tokens = [token for column in table_rule["mix_id_columns"] for token in cell_tokens(packet, column)]
        identity = "|".join(str(token["token"]) for token in identity_tokens) or f"row-{packet['source_row_index']}"
        mix_key = f"mix-{run_id[9:]}-{packet['table_id']}-r{packet['source_row_index']}"
        params: list[dict[str, Any]] = []
        refs: list[str] = []
        for column in table_rule["material_columns"]:
            tokens = cell_tokens(packet, column)
            if not tokens and column in table_rule.get("dash_recovery_columns", []):
                recovered = recover_dash(packet, column, words, ranges)
                tokens = [recovered] if recovered else []
            for ordinal, token in enumerate(tokens, 1):
                ev = make_evidence(run_id, packet, column, token, ordinal, "pymupdf_words_dash_recovery_v1" if token.get("typed_state") == "null" else None)
                evidence.append(ev)
                params.append(reported_value(column, table_units[packet["table_id"]].get(column), token, ev))
            ref_key = table_rule.get("mat_reference_columns", {}).get(column)
            if ref_key and any(token.get("typed_state") == "number" and float(token.get("typed") or 0) > 0 for token in tokens):
                refs.append(f"mat-{run_id[9:]}-{ref_key}")
        performance: list[dict[str, Any]] = []
        for column in table_rule.get("observation_columns", []):
            for ordinal, token in enumerate(cell_tokens(packet, column), 1):
                if token.get("typed_state") != "number":
                    continue
                ev = make_evidence(run_id, packet, column, token, ordinal)
                evidence.append(ev)
                semantic = "quarantined_pending_semantic_label" if table_rule.get("observation_semantics") else "source_reported"
                value = reported_value(f"{column}_{ordinal}", table_units[packet["table_id"]].get(column), token, ev, semantic_status=semantic)
                performance.append(value)
                if semantic.startswith("quarantined"):
                    quarantine.append({"mix_key": mix_key, "evidence_key": ev["evidence_key"], "reason": "ambiguous_merged_header_semantics"})
        if not refs and len(rule["mats"]) == 1:
            refs = [f"mat-{run_id[9:]}-{rule['mats'][0]['key']}"]
        mixes.append({
            "mix_key": mix_key,
            "paper_key": f"paper-{run_id[9:]}",
            "modules": {
                "identity_source_specimen": {"custom_test_id": identity, "source_table": packet["table_id"], "source_row": packet["source_row_index"]},
                "materials": {"mat_refs": sorted(set(refs)), "reported_parameters": params},
                "mixing_curing": {"stages": []},
                "performance": performance,
                "characterizations": [],
            },
        })
    extra = extract_extra_observations(rule, words, mixes, evidence, run_id)
    mats = [{
        "mat_key": f"mat-{run_id[9:]}-{mat['key']}", "paper_key": f"paper-{run_id[9:]}",
        "identity": {"label": mat["label"], "role": mat["role"]},
    } for mat in rule["mats"]]
    records = {
        "schema_version": 1,
        "wakg_contract_version": "1.1.4",
        "run_id": run_id,
        "state": "EXTRACTED",
        "paper": {"paper_key": f"paper-{run_id[9:]}", "pdf_sha256": routing_paper["pdf_sha256"], "pdf_relative_path": routing_paper["pdf_relative_path"]},
        "mats": mats,
        "mixes": mixes,
        "quarantine": quarantine,
    }
    observations = sum(len(mix["modules"]["performance"]) + len(mix["modules"]["characterizations"]) for mix in mixes)
    actual = {"mixes": len(mixes), "mats": len(mats), "observations": observations}
    if actual != EXPECTED[run_id]:
        raise ValueError(f"count gate failed for {run_id}: {actual} != {EXPECTED[run_id]}")
    manifest = {
        "schema_version": 1,
        "run_id": run_id,
        "input_hashes": {**input_hashes, "pdf_sha256": routing_paper["pdf_sha256"]},
        "counts": {**actual, "evidence_links": len(evidence), "typed_nulls": sum(1 for mix in mixes for p in mix["modules"]["materials"]["reported_parameters"] if p["value"] is None)},
        "evidence_links": evidence,
        "source_evidence_gate": "PASS",
        "pdf_parse_cache_hit": cache_hit,
        "model_calls": 0,
        "token_proxy": 0,
    }
    records_hash = digest(records)
    manifest["generated_records_sha256"] = records_hash
    run_dir = output_root / run_id
    atomic_json(run_dir / "generated-records.json", records)
    atomic_json(run_dir / "evidence-manifest.json", manifest)
    return {"run_id": run_id, "records_sha256": records_hash, "manifest_sha256": digest(manifest), "counts": actual, "cache_hit": cache_hit}


def build(root: Path, output_root: Path, routing_path: Path, rules_path: Path, cache_root: Path | None, fail_after: int | None = None) -> dict[str, Any]:
    started = time.perf_counter()
    routing, rules, support = load_source_inputs(root, routing_path, rules_path)
    summaries = []
    for index, rule in enumerate(rules["papers"], 1):
        marker = output_root / rule["run_id"] / "generated-records.json"
        if marker.exists():
            records = json.loads(marker.read_text(encoding="utf-8"))
            summaries.append({"run_id": rule["run_id"], "records_sha256": digest(records), "resumed": True})
            continue
        summaries.append(build_paper(root, output_root, routing, rule, support["papers"][rule["run_id"]], support["hashes"], cache_root))
        if fail_after == index:
            raise RuntimeError("injected build interruption")
    summary = {
        "schema_version": 1,
        "papers": summaries,
        "semantic_hash": digest({item["run_id"]: item["records_sha256"] for item in summaries}),
        "runtime_seconds": round(time.perf_counter() - started, 6),
        "model_calls": 0,
        "token_proxy": 0,
    }
    atomic_json(output_root / "build-summary.json", summary)
    return summary


def _fold_label(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).casefold())


def _generated_fields(mix: dict[str, Any]) -> dict[str, tuple[Any, Any]]:
    values = mix["modules"]["materials"]["reported_parameters"] + mix["modules"]["performance"] + mix["modules"]["characterizations"]
    return {_fold_label(value["parameter"]): (value.get("value"), value.get("unit")) for value in values}


def _reference_fields(mix: dict[str, Any]) -> dict[str, tuple[Any, Any]]:
    fields: dict[str, tuple[Any, Any]] = {}
    reported = mix.get("modules", {}).get("materials", {}).get("extensions", {}).get("reported_parameters", [])
    for value in reported:
        fields[_fold_label(value.get("parameter_key", value.get("original_label")))] = (value.get("value"), value.get("unit"))
    for module in ("performance", "characterizations"):
        for value in mix.get("modules", {}).get(module, []):
            fields[_fold_label(value.get("name", value.get("parameter")))] = (value.get("value"), value.get("unit"))
    return fields


def _accepted_evidence_conflicts(reference: dict[str, Any], pdf: Path) -> list[dict[str, Any]]:
    """Check whether an accepted evidence box actually contains its snippet value."""
    conflicts: list[dict[str, Any]] = []
    doc = fitz.open(pdf)
    for link in reference.get("evidence_links", []):
        if "dynamic_yield_stress" not in str(link.get("evidence_key", "")):
            continue
        numbers = re.findall(r"(?<![A-Za-z])[0-9]+(?:\.[0-9]+)?", str(link.get("snippet", "")))
        if not numbers or not link.get("bbox") or not link.get("page"):
            continue
        expected = numbers[-1]
        x0, y0, x1, y1 = link["bbox"]
        tokens = [str(word[4]) for word in doc[int(link["page"]) - 1].get_text("words") if x0 <= (word[0] + word[2]) / 2 <= x1 and y0 <= (word[1] + word[3]) / 2 <= y1]
        if expected not in tokens:
            conflicts.append({"evidence_key": link.get("evidence_key"), "reason": "accepted_bbox_does_not_contain_claimed_source_token", "claimed_token": expected})
    return conflicts


def compare_generated(output_root: Path, reference_root: Path) -> dict[str, Any]:
    """Post-generation field comparison; source evidence remains authoritative."""
    results = []
    for run_id in CANARY_RUNS:
        generated_path = output_root / run_id / "generated-records.json"
        if not generated_path.exists():
            raise ValueError("comparison requires an already atomically generated output")
        generated = json.loads(generated_path.read_text(encoding="utf-8"))
        reference_path = reference_root / run_id / "records.json"
        reference = json.loads(reference_path.read_text(encoding="utf-8")) if reference_path.exists() else None
        status = "NO_REFERENCE"
        comparison = {"identity_matches": 0, "field_exact": 0, "field_equivalent": 0, "field_mismatch": 0, "generated_unmapped": 0}
        conflicts: list[dict[str, Any]] = []
        if reference is not None:
            reference_mixes = {str(mix.get("modules", {}).get("identity_source_specimen", {}).get("custom_test_id")): mix for mix in reference.get("mixes", [])}
            for mix in generated["mixes"]:
                identity = str(mix["modules"]["identity_source_specimen"]["custom_test_id"])
                accepted = reference_mixes.get(identity)
                if accepted is None:
                    comparison["generated_unmapped"] += len(_generated_fields(mix))
                    continue
                comparison["identity_matches"] += 1
                accepted_fields = _reference_fields(accepted)
                for key, generated_value in _generated_fields(mix).items():
                    if key not in accepted_fields:
                        comparison["generated_unmapped"] += 1
                    elif generated_value == accepted_fields[key]:
                        comparison["field_exact"] += 1
                    elif generated_value[0] == accepted_fields[key][0]:
                        comparison["field_equivalent"] += 1
                    else:
                        comparison["field_mismatch"] += 1
            pdf = reference_root.parent / generated["paper"]["pdf_relative_path"]
            conflicts = _accepted_evidence_conflicts(reference, pdf)
            if conflicts:
                status = "SOURCE_CONFLICT_QUARANTINED"
            elif comparison["field_mismatch"]:
                status = "PARTIAL_MISMATCH"
            elif comparison["generated_unmapped"]:
                status = "PARTIAL_EQUIVALENT"
            else:
                status = "EXACT_OR_UNIT_EQUIVALENT"
        results.append({"run_id": run_id, "status": status, "source_authority": "frozen_pdf", "field_comparison": comparison, "source_conflicts": conflicts})
    report = {"schema_version": 1, "comparison": results, "generated_before_reference_read": True}
    atomic_json(output_root / "post-generation-comparison.json", report)
    return report


def benchmark(root: Path, output_root: Path, routing_path: Path, rules_path: Path) -> dict[str, Any]:
    """Exercise reusable runtime without including development time."""
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(dir=output_root.parent) as temporary:
        work = Path(temporary)
        cache = work / "cache"
        cold = build(root, work / "cold", routing_path, rules_path, cache)
        warm = build(root, work / "warm", routing_path, rules_path, cache)
        interrupted = False
        try:
            build(root, work / "resume", routing_path, rules_path, work / "resume-cache", fail_after=1)
        except RuntimeError:
            interrupted = True
        resumed = build(root, work / "resume", routing_path, rules_path, work / "resume-cache")
        mutated = json.loads(routing_path.read_text(encoding="utf-8"))
        packet = next(packet for packet in mutated["packets"] if packet["run_id"] == CANARY_RUNS[0])
        token = next(token for cell in packet["cells"] for token in cell["tokens"] if token["typed_state"] == "number")
        token["typed"] = float(token["typed"]) + 0.125
        token["token"] = str(token["typed"])
        mutated_path = work / "mutated-routing.json"
        atomic_json(mutated_path, mutated)
        corrupt = build(root, work / "corrupt", mutated_path, rules_path, work / "corrupt-cache")
        cold_hashes = {paper["run_id"]: paper["records_sha256"] for paper in cold["papers"]}
        corrupt_hashes = {paper["run_id"]: paper["records_sha256"] for paper in corrupt["papers"]}
        report = {
            "schema_version": 1,
            "runtime_only": True,
            "development_time_seconds": None,
            "development_time_note": "tracked separately from reusable runtime",
            "cold_seconds": cold["runtime_seconds"],
            "warm_seconds": warm["runtime_seconds"],
            "cold_warm_semantic_hash_equal": cold["semantic_hash"] == warm["semantic_hash"],
            "warm_pdf_parse_cache_hits": sum(bool(paper.get("cache_hit")) for paper in warm["papers"]),
            "crash_injected": interrupted,
            "resume_semantic_hash_equal": cold["semantic_hash"] == resumed["semantic_hash"],
            "corrupt_one_changed_target": cold_hashes[CANARY_RUNS[0]] != corrupt_hashes[CANARY_RUNS[0]],
            "corrupt_one_unaffected_papers": sum(cold_hashes[run_id] == corrupt_hashes[run_id] for run_id in CANARY_RUNS[1:]),
            "model_calls": 0,
            "token_proxy": 0,
            "benchmark_wall_seconds": round(time.perf_counter() - started, 6),
        }
    atomic_json(output_root / "runtime-benchmark.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("build", "validate", "benchmark"))
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, default=Path("runs/user-corpus-10-efficiency-v2/phase-b-canary"))
    parser.add_argument("--routing", type=Path, default=Path("runs/user-corpus-10-efficiency-v2/phase-a-routing-bundle.json"))
    parser.add_argument("--rules", type=Path, default=Path("fixtures/user_corpus_semantic_rules_v1.json"))
    parser.add_argument("--cache", type=Path, default=Path("cache/phase-b-pdf-words"))
    parser.add_argument("--reference-root", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    output = (root / args.output).resolve() if not args.output.is_absolute() else args.output
    if args.command == "build":
        summary = build(root, output, root / args.routing, root / args.rules, root / args.cache)
        print(canonical(summary))
    elif args.command == "benchmark":
        print(canonical(benchmark(root, output, root / args.routing, root / args.rules)))
    else:
        if args.reference_root is None:
            raise SystemExit("--reference-root is required only for post-generation validation")
        print(canonical(compare_generated(output, args.reference_root.resolve())))


if __name__ == "__main__":
    main()
