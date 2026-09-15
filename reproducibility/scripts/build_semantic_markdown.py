#!/usr/bin/env python3
"""Build deterministic, page-bound Markdown reading layers for WAKG agents.

The Markdown is deliberately not evidence and never becomes the source of a
formal value.  It gives one semantic Agent a compact view of the paper's
materials/methods framework, source-object inventory, table rows and page text.
Every item remains bound to immutable PDF / extraction-plan hashes so the Agent
can route a claim back to the coordinate-bearing source artifacts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import tempfile
from pathlib import Path
from typing import Any

import fitz


VERSION = "wakg-semantic-markdown-v2-upstream-source"
DEFAULT_PHASE_ROOT = Path("runs/user-corpus-10-efficiency-v2/phase-c-full10")


def stable_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def digest_file(path: Path) -> str:
    return digest_bytes(path.read_bytes())


def load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(value)
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def atomic_json(path: Path, value: Any) -> None:
    atomic_text(path, stable_bytes(value).decode("utf-8") + "\n")


def normalized_lines(value: str) -> str:
    value = re.sub(r"\u00ad\s*", "", value).replace("\r", "")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.split("\n")]
    output: list[str] = []
    for line in lines:
        if not line and (not output or not output[-1]):
            continue
        output.append(line)
    return "\n".join(output).strip()


def page_markdown(page: fitz.Page) -> str:
    """Return stable block text while retaining explicit page boundaries.

    Native PDF block order is preferred over a global y-sort because the latter
    interleaves the left and right columns of journal articles.  Tables are
    emitted separately from coordinate-derived assets below.
    """
    blocks = sorted(
        [block for block in page.get_text("blocks", sort=False) if len(block) > 6 and int(block[6]) == 0],
        key=lambda block: int(block[5]),
    )
    values = [normalized_lines(str(block[4])) for block in blocks]
    return "\n\n".join(value for value in values if value)


def build_source_markdown(pdf_path: Path, run_id: str) -> tuple[str, dict[str, Any]]:
    """Create the source-only Markdown consumed before extraction planning.

    This artifact deliberately has no extraction-plan or record dependency.
    It is a deterministic reading representation of immutable PDF bytes; the
    PDF remains the sole evidence authority.
    """
    pdf_path = pdf_path.resolve()
    pdf_sha256 = digest_file(pdf_path)
    lines = [
        f"# WAKG source reading layer — {run_id}",
        "",
        "> Agent/planner reading aid only. Formal evidence must resolve to the immutable PDF or supplement asset.",
        "",
        "## Immutable source",
        "",
        f"- generator: `{VERSION}`",
        f"- run-id: `{run_id}`",
        f"- pdf-sha256: `{pdf_sha256}`",
    ]
    with fitz.open(pdf_path) as document:
        page_count = len(document)
        lines.extend([f"- pages: {page_count}", "", "## Page-bounded source text", ""])
        for page_number, page in enumerate(document, 1):
            lines.extend([
                f"### <page {page_number}>",
                "",
                page_markdown(page),
                "",
                f"### </page {page_number}>",
                "",
            ])
    text = "\n".join(lines).rstrip() + "\n"
    metadata = {
        "runId": run_id,
        "generatorVersion": VERSION,
        "pdfSha256": pdf_sha256,
        "pageCount": page_count,
        "markdownSha256": digest_bytes(text.encode("utf-8")),
        "bytes": len(text.encode("utf-8")),
        "characters": len(text),
        "approximateTokenProxy": math.ceil(len(text) / 4),
    }
    return text, metadata


def parse_source_markdown(text: str, expected: dict[str, Any] | None = None) -> dict[str, Any]:
    """Validate a source Markdown artifact and return its page text."""
    run_match = re.search(r"^- run-id: `([^`]+)`$", text, re.MULTILINE)
    pdf_match = re.search(r"^- pdf-sha256: `([0-9a-f]{64})`$", text, re.MULTILINE)
    count_match = re.search(r"^- pages: (\d+)$", text, re.MULTILINE)
    if not run_match or not pdf_match or not count_match:
        raise ValueError("invalid source Markdown metadata")
    pages = [
        {"page": int(match.group(1)), "text": normalized_lines(match.group(2))}
        for match in re.finditer(r"^### <page (\d+)>\s*$(.*?)^### </page \1>\s*$", text, re.MULTILINE | re.DOTALL)
    ]
    page_count = int(count_match.group(1))
    if [item["page"] for item in pages] != list(range(1, page_count + 1)):
        raise ValueError("source Markdown page boundaries are incomplete or unordered")
    result = {
        "runId": run_match.group(1),
        "generatorVersion": VERSION,
        "pdfSha256": pdf_match.group(1),
        "pageCount": page_count,
        "markdownSha256": digest_bytes(text.encode("utf-8")),
        "pages": pages,
    }
    if expected:
        for key in ("runId", "pdfSha256", "pageCount", "markdownSha256"):
            if expected.get(key) != result.get(key):
                raise ValueError(f"source Markdown {key} mismatch")
    return result


def _line(value: Any, limit: int = 260) -> str:
    text = " ".join(str(value or "").split()).replace("|", "/")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def table_markdown(value: dict[str, Any]) -> str:
    lines = [
        f"#### {value.get('source_label')} — page {value.get('page')}",
        f"caption: {_line(value.get('caption'), 500)}",
        f"table-sha256: `{value.get('table_sha256')}`",
        "",
    ]
    for index, row in enumerate(value.get("rows") or [], 1):
        row_text = " ".join(str(cell.get("text") or "") for cell in row).strip()
        lines.append(f"- row-{index}: {_line(row_text, 1200)}")
    return "\n".join(lines)


def _best_table_assets(run_dir: Path) -> list[dict[str, Any]]:
    best: dict[tuple[str, int], tuple[int, dict[str, Any]]] = {}
    for path in sorted((run_dir / "main-table-assets").glob("*.json")):
        value = load_object(path)
        score = sum(len(row) for row in value.get("rows") or [])
        key = (str(value.get("source_label") or ""), int(value.get("page") or 0))
        if key not in best or score > best[key][0]:
            best[key] = (score, value)
    return [best[key][1] for key in sorted(best, key=lambda item: (item[1], item[0]))]


def build_paper_markdown(project: Path, phase_root: Path, run_id: str) -> tuple[str, dict[str, Any]]:
    run_dir = phase_root / run_id
    plan_path = run_dir / "extraction-plan.json"
    plan = load_object(plan_path)
    expected_plan_hash = plan.get("plan_sha256")
    observed_plan_hash = digest_bytes(stable_bytes({key: value for key, value in plan.items() if key != "plan_sha256"}))
    if expected_plan_hash != observed_plan_hash:
        raise ValueError(f"extraction plan hash mismatch: {run_id}")
    manifest = load_object(project / "runs" / run_id / "manifest.json")
    pdf = Path(str((manifest.get("input") or {}).get("pdf_path") or ""))
    pdf = pdf if pdf.is_absolute() else project / pdf
    pdf = pdf.resolve()
    if not pdf.is_file() or digest_file(pdf) != str((plan.get("pdf") or {}).get("sha256") or ""):
        raise ValueError(f"frozen PDF mismatch: {run_id}")

    source_items = list(plan.get("source_items") or [])
    lines = [
        f"# WAKG semantic reading layer — {run_id}",
        "",
        "> Agent reading aid only. Formal values must resolve to PDF/supplement coordinates; this Markdown is never evidence.",
        "",
        "## Immutable inputs",
        "",
        f"- generator: `{VERSION}`",
        f"- pdf-sha256: `{digest_file(pdf)}`",
        f"- extraction-plan-sha256: `{expected_plan_hash}`",
        f"- pages: {int((plan.get('pdf') or {}).get('pages') or 0)}",
        "",
        "## Extraction framework",
        "",
        "Read materials and experimental methods first; resolve MAT reuse and MIX identity before binding properties. "
        "Then inspect every inventoried table, figure and supplement reference. Null, inferred and quarantined values remain distinct.",
        "",
        f"- detected modalities: {', '.join(plan.get('mentioned_modalities') or []) or 'none'}",
        f"- source objects: {len(source_items)}",
        f"- direct QXRD facts: {len(((plan.get('direct_facts') or {}).get('qxrd') or []))}",
        "",
        "### Source-object inventory",
        "",
        "| object | scope | kind | page | modality | label | disposition | caption |",
        "|---|---|---|---:|---|---|---|---|",
    ]
    for item in source_items:
        modality = item.get("modality")
        if isinstance(modality, list):
            modality = ",".join(str(value) for value in modality)
        lines.append("| " + " | ".join([
            _line(item.get("item_id"), 80), _line(item.get("source_scope"), 40), _line(item.get("kind"), 50),
            str(item.get("page") or "-"), _line(modality, 100), _line(item.get("source_label"), 100),
            _line(item.get("disposition"), 50), _line(item.get("caption"), 240),
        ]) + " |")

    tables = _best_table_assets(run_dir)
    lines.extend(["", "## Coordinate-derived table rows", ""])
    if tables:
        for table in tables:
            lines.extend([table_markdown(table), ""])
    else:
        lines.append("No table asset was available at this stage; use the source-object inventory and page text.\n")

    lines.extend(["## Page-bounded source text", ""])
    with fitz.open(pdf) as document:
        for page_number, page in enumerate(document, 1):
            anchors = [item for item in source_items if item.get("page") == page_number]
            lines.append(f"### <page {page_number}>")
            if anchors:
                lines.append("anchors: " + ", ".join(f"{item.get('item_id')}[{item.get('source_label')}]" for item in anchors))
            lines.extend(["", page_markdown(page), "", f"### </page {page_number}>", ""])

    text = "\n".join(lines).rstrip() + "\n"
    metadata = {
        "runId": run_id,
        "pdfSha256": digest_file(pdf),
        "planSha256": expected_plan_hash,
        "markdownSha256": digest_bytes(text.encode("utf-8")),
        "bytes": len(text.encode("utf-8")),
        "characters": len(text),
        "approximateTokenProxy": math.ceil(len(text) / 4),
        "pageCount": int((plan.get("pdf") or {}).get("pages") or 0),
        "sourceObjectCount": len(source_items),
        "tableObjectCount": len(tables),
        "allPagesPresent": all(f"### <page {number}>" in text and f"### </page {number}>" in text for number in range(1, int((plan.get("pdf") or {}).get("pages") or 0) + 1)),
        "allSourceObjectsPresent": all(str(item.get("item_id")) in text for item in source_items),
    }
    return text, metadata


def build_all(project: Path, phase_root: Path, out_dir: Path) -> dict[str, Any]:
    project = project.resolve()
    phase_root = (phase_root if phase_root.is_absolute() else project / phase_root).resolve()
    out_dir = (out_dir if out_dir.is_absolute() else project / out_dir).resolve()
    runs = sorted(path.name for path in phase_root.iterdir() if path.is_dir() and (path / "extraction-plan.json").is_file())
    papers = []
    for run_id in runs:
        text, metadata = build_paper_markdown(project, phase_root, run_id)
        path = out_dir / f"{run_id}.md"
        atomic_text(path, text)
        metadata["path"] = path.relative_to(project).as_posix()
        papers.append(metadata)
    report = {
        "schemaVersion": 1,
        "kind": "WAKG_SEMANTIC_MARKDOWN_MANIFEST",
        "generatorVersion": VERSION,
        "paperCount": len(papers),
        "papers": papers,
        "totals": {
            "bytes": sum(item["bytes"] for item in papers),
            "characters": sum(item["characters"] for item in papers),
            "approximateTokenProxy": sum(item["approximateTokenProxy"] for item in papers),
            "sourceObjects": sum(item["sourceObjectCount"] for item in papers),
            "tableObjects": sum(item["tableObjectCount"] for item in papers),
        },
        "gates": {
            "allPapersBuilt": len(papers) == 10,
            "allPagesPresent": all(item["allPagesPresent"] for item in papers),
            "allSourceObjectsPresent": all(item["allSourceObjectsPresent"] for item in papers),
            "underOneMillionTokenProxy": sum(item["approximateTokenProxy"] for item in papers) < 1_000_000,
            "modelCalls": 0,
        },
    }
    report["verdict"] = "PASS" if all(value is True or value == 0 for value in report["gates"].values()) else "FAIL"
    report["manifestSha256"] = digest_bytes(stable_bytes({key: value for key, value in report.items() if key != "manifestSha256"}))
    atomic_json(out_dir / "manifest.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--phase-root", type=Path, default=DEFAULT_PHASE_ROOT)
    parser.add_argument("--out", type=Path, default=DEFAULT_PHASE_ROOT / "semantic-markdown")
    args = parser.parse_args()
    report = build_all(args.project_root, args.phase_root, args.out)
    print(json.dumps({"verdict": report["verdict"], "paperCount": report["paperCount"], **report["totals"]}, ensure_ascii=False, sort_keys=True))
    return 0 if report["verdict"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
