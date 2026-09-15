#!/usr/bin/env python3
"""Hash-addressed PDF page rendering for the local WAKG review UI."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import fitz


RENDERER_VERSION = 1
DEFAULT_SCALE = 1.45
LOCK_WAIT_SECONDS = 120.0
LOCK_POLL_SECONDS = 0.1


class RenderCacheMissing(RuntimeError):
    """Raised when publication is attempted before background rendering ends."""


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, value: object) -> None:
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


def scale_key(scale: float) -> str:
    if not 0.75 <= scale <= 2.5:
        raise ValueError("scale must be between 0.75 and 2.5")
    return f"scale-{round(scale * 1000):04d}"


def cache_entry(project: Path, pdf_sha256: str, scale: float) -> Path:
    if len(pdf_sha256) != 64 or any(char not in "0123456789abcdef" for char in pdf_sha256):
        raise ValueError("invalid PDF SHA256")
    return project / "internal_assets" / "review-page-cache" / f"v{RENDERER_VERSION}" / pdf_sha256 / scale_key(scale)


def load_valid_manifest(entry: Path, pdf_sha256: str, scale: float) -> dict[str, Any] | None:
    path = entry / "manifest.json"
    if not path.is_file():
        return None
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if (
        manifest.get("schemaVersion") != 1
        or manifest.get("rendererVersion") != RENDERER_VERSION
        or manifest.get("pdfSha256") != pdf_sha256
        or int(manifest.get("scaleMilli", -1)) != round(scale * 1000)
        or not isinstance(manifest.get("pages"), list)
        or int(manifest.get("pageCount", -1)) != len(manifest["pages"])
    ):
        return None
    for index, page in enumerate(manifest["pages"], start=1):
        expected_name = f"page-{index:03d}.png"
        if page.get("number") != index or page.get("file") != expected_name:
            return None
        image = entry / expected_name
        if not image.is_file() or sha256_path(image) != page.get("sha256"):
            return None
    return manifest


def render_pdf_to_cache(
    project: Path,
    pdf: Path,
    pdf_sha256: str,
    scale: float = DEFAULT_SCALE,
    lock_wait_seconds: float = LOCK_WAIT_SECONDS,
) -> dict[str, Any]:
    project = project.resolve()
    pdf = pdf.resolve()
    if project not in pdf.parents or not pdf.is_file():
        raise ValueError(f"missing or unsafe PDF: {pdf}")
    if sha256_path(pdf) != pdf_sha256:
        raise ValueError(f"PDF hash mismatch: {pdf}")
    entry = cache_entry(project, pdf_sha256, scale)
    cached = load_valid_manifest(entry, pdf_sha256, scale)
    if cached is not None:
        return {"status": "cache_hit", "cache": entry, "manifest": cached, "runtimeSeconds": 0.0}

    entry.parent.mkdir(parents=True, exist_ok=True)
    lock = entry.parent / f"{entry.name}.lock"
    descriptor: int | None = None
    deadline = time.monotonic() + max(0.0, lock_wait_seconds)
    while descriptor is None:
        try:
            descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            continue
        except FileExistsError as error:
            cached = load_valid_manifest(entry, pdf_sha256, scale)
            if cached is not None:
                return {"status": "cache_hit", "cache": entry, "manifest": cached, "runtimeSeconds": 0.0}
            try:
                stale = time.time() - lock.stat().st_mtime > 3600
            except FileNotFoundError:
                stale = True
            if stale:
                try:
                    lock.unlink()
                except FileNotFoundError:
                    pass
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"timed out waiting for PDF render {pdf_sha256[:12]} at {scale:g}x"
                ) from error
            time.sleep(min(LOCK_POLL_SECONDS, max(0.0, deadline - time.monotonic())))

    # Another process may have completed between our last cache check and
    # this successful lock acquisition. Recheck under the lock so duplicate
    # dispatch never performs a second render.
    cached = load_valid_manifest(entry, pdf_sha256, scale)
    if cached is not None:
        os.close(descriptor)
        try:
            lock.unlink()
        except FileNotFoundError:
            pass
        return {"status": "cache_hit", "cache": entry, "manifest": cached, "runtimeSeconds": 0.0}

    started = time.perf_counter()
    stage = Path(tempfile.mkdtemp(prefix=f"{entry.name}-", dir=entry.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            stream.write(str(os.getpid()))
        pages: list[dict[str, Any]] = []
        with fitz.open(pdf) as document:
            for index, page in enumerate(document, start=1):
                name = f"page-{index:03d}.png"
                image = stage / name
                page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False).save(image)
                pages.append({
                    "number": index,
                    "file": name,
                    "sha256": sha256_path(image),
                    "width": float(page.rect.width),
                    "height": float(page.rect.height),
                })
        manifest = {
            "schemaVersion": 1,
            "rendererVersion": RENDERER_VERSION,
            "pdfSha256": pdf_sha256,
            "scaleMilli": round(scale * 1000),
            "pageCount": len(pages),
            "pages": pages,
        }
        atomic_json(stage / "manifest.json", manifest)
        cached = load_valid_manifest(stage, pdf_sha256, scale)
        if cached is None:
            raise RuntimeError("rendered page cache failed self-validation")
        if entry.exists():
            shutil.rmtree(entry)
        os.replace(stage, entry)
        return {
            "status": "rendered",
            "cache": entry,
            "manifest": manifest,
            "runtimeSeconds": round(time.perf_counter() - started, 3),
        }
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        try:
            lock.unlink()
        except FileNotFoundError:
            pass


def materialize_cached_pages(project: Path, pdf_sha256: str, scale: float, target: Path) -> dict[str, Any]:
    entry = cache_entry(project.resolve(), pdf_sha256, scale)
    manifest = load_valid_manifest(entry, pdf_sha256, scale)
    if manifest is None:
        raise RenderCacheMissing(f"render cache missing for PDF {pdf_sha256[:12]} at {scale:g}x")
    target.mkdir(parents=True, exist_ok=True)
    for page in manifest["pages"]:
        source = entry / page["file"]
        destination = target / page["file"]
        try:
            os.link(source, destination)
        except OSError:
            shutil.copy2(source, destination)
    return manifest


def resolve_candidate_jobs(project: Path, candidate: Path | None, run_ids: list[str]) -> list[dict[str, Any]]:
    selected = set(run_ids)
    expected_hashes: dict[str, str] = {}
    expected_supplements: dict[str, list[dict[str, str]]] = {}
    ordered: list[str] = []
    if candidate is not None:
        candidate_path = candidate if candidate.is_absolute() else project / candidate
        candidate_path = candidate_path.resolve()
        if project not in candidate_path.parents or not candidate_path.is_file():
            raise ValueError("missing or unsafe review candidate")
        payload = json.loads(candidate_path.read_text(encoding="utf-8"))
        for paper in payload.get("papers") or []:
            run_id = str(paper.get("runId") or "")
            if run_id:
                ordered.append(run_id)
                expected_hashes[run_id] = str(paper.get("pdfSha256") or "")
                supplements: dict[str, dict[str, str]] = {}
                for page in paper.get("pages") or []:
                    if page.get("sourceKind") != "supplement":
                        continue
                    source_path = str(page.get("sourceArtifactPath") or "")
                    source_sha = str(page.get("sourceArtifactSha256") or "")
                    if not source_path or not source_sha:
                        raise ValueError(f"candidate supplement identity missing: {run_id}")
                    previous = supplements.get(source_sha)
                    current = {"path": source_path, "pdfSha256": source_sha}
                    if previous is not None and previous != current:
                        raise ValueError(f"candidate supplement identity conflict: {run_id}")
                    supplements[source_sha] = current
                expected_supplements[run_id] = [supplements[key] for key in sorted(supplements)]
        if selected:
            unknown = selected - set(ordered)
            if unknown:
                raise ValueError(f"selected runs absent from candidate: {sorted(unknown)}")
            ordered = [run_id for run_id in ordered if run_id in selected]
    else:
        if not run_ids:
            raise ValueError("provide --candidate or at least one --run")
        ordered = list(dict.fromkeys(run_ids))

    if not ordered:
        raise ValueError("no papers selected for rendering")

    jobs: list[dict[str, Any]] = []
    for run_id in ordered:
        if not run_id or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for char in run_id):
            raise ValueError(f"unsafe run id: {run_id!r}")
        manifest_path = project / "runs" / run_id / "manifest.json"
        if not manifest_path.is_file():
            raise ValueError(f"missing run manifest: {run_id}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        relative_pdf = str((manifest.get("input") or {}).get("pdf_path") or "")
        pdf = (project / relative_pdf).resolve()
        pdf_sha256 = str((manifest.get("input") or {}).get("pdf_sha256") or "")
        if expected_hashes.get(run_id) and expected_hashes[run_id] != pdf_sha256:
            raise ValueError(f"candidate PDF hash mismatch: {run_id}")
        supplement_pdfs = []
        for item in expected_supplements.get(run_id, []):
            source = (project / item["path"]).resolve()
            if project not in source.parents or not source.is_file() or source.suffix.casefold() != ".pdf":
                raise ValueError(f"missing or unsafe candidate supplement PDF: {run_id}")
            if sha256_path(source) != item["pdfSha256"]:
                raise ValueError(f"candidate supplement PDF hash mismatch: {run_id}")
            supplement_pdfs.append({"pdf": source, "pdfSha256": item["pdfSha256"]})
        jobs.append({"runId": run_id, "pdf": pdf, "pdfSha256": pdf_sha256, "supplementPdfs": supplement_pdfs})
    return jobs


def run_jobs(project: Path, jobs: list[dict[str, Any]], scale: float, workers: int) -> dict[str, Any]:
    started = time.perf_counter()
    results: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    def render_job(job: dict[str, Any]) -> dict[str, Any]:
        main = render_pdf_to_cache(project, job["pdf"], job["pdfSha256"], scale)
        supplements = [
            render_pdf_to_cache(project, item["pdf"], item["pdfSha256"], scale)
            for item in job.get("supplementPdfs") or []
        ]
        all_results = [main, *supplements]
        return {
            "status": "cache_hit" if all(item["status"] == "cache_hit" for item in all_results) else "rendered",
            "main": main,
            "supplements": supplements,
        }

    with ThreadPoolExecutor(max_workers=min(max(workers, 1), min(len(jobs), 8))) as executor:
        future_to_job = {
            executor.submit(render_job, job): job
            for job in jobs
        }
        for future in as_completed(future_to_job):
            job = future_to_job[future]
            try:
                result = future.result()
                main = result["main"]
                supplements = result["supplements"]
                results.append({
                    "runId": job["runId"],
                    "pdfSha256": job["pdfSha256"],
                    "status": result["status"],
                    "pageCount": main["manifest"]["pageCount"],
                    "sourceArtifactCount": 1 + len(supplements),
                    "sourcePageCount": int(main["manifest"]["pageCount"]) + sum(int(item["manifest"]["pageCount"]) for item in supplements),
                    "supplementPdfCount": len(supplements),
                    "runtimeSeconds": round(float(main["runtimeSeconds"]) + sum(float(item["runtimeSeconds"]) for item in supplements), 3),
                })
            except Exception as error:  # keep independent papers running
                failures.append({"runId": job["runId"], "error": f"{type(error).__name__}: {error}"})
    results.sort(key=lambda item: item["runId"])
    failures.sort(key=lambda item: item["runId"])
    return {
        "schemaVersion": 1,
        "kind": "WAKG_REVIEW_PAGE_RENDER",
        "status": "PASS" if not failures else "PARTIAL_FAILURE",
        "selectedPaperCount": len(jobs),
        "renderedPaperCount": sum(item["status"] == "rendered" for item in results),
        "cacheHitPaperCount": sum(item["status"] == "cache_hit" for item in results),
        "pageCount": sum(int(item["pageCount"]) for item in results),
        "sourceArtifactCount": sum(int(item["sourceArtifactCount"]) for item in results),
        "sourcePageCount": sum(int(item["sourcePageCount"]) for item in results),
        "scaleMilli": round(scale * 1000),
        "workers": min(max(workers, 1), min(len(jobs), 8)),
        "runtimeSeconds": round(time.perf_counter() - started, 3),
        "papers": results,
        "failures": failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Render selected review PDFs into the hash-addressed background cache")
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--run", action="append", default=[])
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--scale", type=float, default=DEFAULT_SCALE)
    parser.add_argument("--write-status", type=Path)
    args = parser.parse_args()
    project = args.project_root.resolve()
    status_path: Path | None = None
    if args.write_status:
        status_path = args.write_status if args.write_status.is_absolute() else project / args.write_status
        status_path = status_path.resolve()
        if project not in status_path.parents:
            raise ValueError("unsafe render status path")
    started = time.perf_counter()
    try:
        jobs = resolve_candidate_jobs(project, args.candidate, args.run)
        result = run_jobs(project, jobs, args.scale, args.workers)
        exit_code = 0 if result["status"] == "PASS" else 2
    except Exception as error:
        result = {
            "schemaVersion": 1,
            "kind": "WAKG_REVIEW_PAGE_RENDER",
            "status": "FAILED",
            "selectedPaperCount": 0,
            "renderedPaperCount": 0,
            "cacheHitPaperCount": 0,
            "pageCount": 0,
            "scaleMilli": round(args.scale * 1000),
            "workers": max(1, min(args.workers, 8)),
            "runtimeSeconds": round(time.perf_counter() - started, 3),
            "papers": [],
            "failures": [{"runId": None, "error": f"{type(error).__name__}: {error}"}],
        }
        exit_code = 1
    if status_path is not None:
        atomic_json(status_path, result)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
