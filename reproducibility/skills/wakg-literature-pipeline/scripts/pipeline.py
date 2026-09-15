#!/usr/bin/env python3
"""Deterministic core for the WAKG literature pipeline.

The script deliberately contains no model or WAKG-platform integration.  It
prepares bounded evidence packets that Codex project tasks can discover, map,
and independently validate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Iterable


SCHEMA_VERSION = "1.0"
PLATFORM_CONTRACT = "WAKG-V1.1.4"
TOOL_VERSION = "0.3.0"
CONTROL_SCHEMA_VERSION = 1
CLAIM_SCHEMA_VERSION = 1
PARSER_VERSION = "fitz-text-blocks-v1"
EXTRACTION_POLICY_VERSION = "wakg-extraction-v1"
OPENALEX_PROVIDER_CONTRACT_VERSION = "openalex-works-list-2026-08"
OA_ROUTE_RESOLVER_CONTRACT_VERSION = "oa-route-resolver-v1"
OA_RANK_AB_CONTRACT_VERSION = "oa-rank-ab-v1"
OA_HYBRID_RANKING_CONTRACT_VERSION = "oa-hybrid-ranking-v1"
HARDEN_03_D3_SELECTED_ORDER_SHA256 = "3bd2699393af9349d6af82067ad49163279ca608370debc952e00637538046f5"
TERMINAL_STAGES = {"ACCESS_FAILED", "QUARANTINED", "BLOCKED_BUDGET", "BLOCKED_REPEATED_DEFECT"}
ORDERED_STAGES = ["DISCOVERED", "SELECTED", "ACQUIRED", "PARSED", "EXTRACTED", "VALIDATED", "ACCEPTED"]
DEFAULT_BUDGET = {
    "max_source_candidates": 20,
    "max_papers": 1,
    "max_visual_pages_per_paper": 3,
    "http_attempts": 3,
    "http_timeout_seconds": 20,
    "max_repair_rounds": 2,
}

FAULT_EVENTS = {"OBSERVED", "MITIGATED", "RESOLVED", "VERIFIED", "REOPENED", "REPRODUCED", "EXERCISED"}
FAULT_KINDS = {"SOFTWARE", "PROVIDER_CONTRACT", "ORCHESTRATION", "ENVIRONMENT", "DATA_COVERAGE"}
FAULT_SEVERITIES = {"critical", "high", "medium", "low"}
FAULT_OCCURRENCES = {"observed", "injected", "expected_limitation"}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def pretty_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def atomic_write_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_text(path, pretty_json(value))


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def relative_to_project(path: Path, project: Path) -> str:
    try:
        return path.resolve().relative_to(project.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def project_paths(project: Path) -> dict[str, Path]:
    root = project.resolve()
    return {
        "root": root,
        "control": root / "control",
        "project": root / "control" / "project.json",
        "state": root / "control" / "state.json",
        "state_db": root / "control" / "state.sqlite3",
        "events": root / "control" / "events.jsonl",
        "faults": root / "research" / "fault-ledger.jsonl",
        "runs": root / "runs",
        "cache": root / "cache" / "discovery",
        "internal": root / "internal_assets",
        "fixtures": root / "fixtures" / "synthetic",
    }


def connect_control_db(project: Path) -> sqlite3.Connection:
    """Open the canonical runtime-state database with safe local defaults."""
    path = project_paths(project)["state_db"]
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA busy_timeout = 30000")
    con.execute("PRAGMA journal_mode = WAL")
    return con


def initialize_control_db(project: Path, project_record: dict[str, Any], legacy_state: dict[str, Any] | None = None) -> None:
    """Create or migrate the M0 control plane without touching exchange data."""
    con = connect_control_db(project)
    try:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY,
            stage TEXT NOT NULL,
            manifest_path TEXT NOT NULL,
            records_sha256 TEXT,
            terminal_exception_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            stage TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            input_hash TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('queued','leased','running','succeeded','retryable_failed','blocked','skipped','dead_letter')),
            lease_owner TEXT,
            lease_expires_epoch REAL,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 2,
            output_artifact_id TEXT,
            error_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS attempts (
            attempt_id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL REFERENCES jobs(job_id),
            attempt_number INTEGER NOT NULL,
            lease_owner TEXT NOT NULL,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            outcome TEXT,
            error_json TEXT,
            UNIQUE(job_id, attempt_number)
        );
        CREATE TABLE IF NOT EXISTS artifacts (
            artifact_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            relative_path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            bytes INTEGER NOT NULL,
            immutable INTEGER NOT NULL CHECK(immutable IN (0,1)),
            created_at TEXT NOT NULL,
            UNIQUE(run_id, kind, sha256)
        );
        CREATE TABLE IF NOT EXISTS claims (
            claim_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
            paper_key TEXT,
            subject_type TEXT NOT NULL,
            subject_key TEXT NOT NULL,
            field_path TEXT NOT NULL,
            value_status TEXT NOT NULL,
            raw_value_json TEXT,
            raw_unit TEXT,
            normalized_value_json TEXT,
            normalized_unit TEXT,
            evidence_json TEXT NOT NULL,
            extraction_method TEXT NOT NULL,
            issues_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS issues (
            issue_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            code TEXT NOT NULL,
            severity TEXT NOT NULL,
            status TEXT NOT NULL,
            record_key TEXT,
            field_path TEXT,
            detail_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS record_versions (
            version_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            records_sha256 TEXT NOT NULL,
            artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
            created_at TEXT NOT NULL,
            UNIQUE(run_id, records_sha256)
        );
        CREATE TABLE IF NOT EXISTS snapshots (
            snapshot_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            status TEXT NOT NULL,
            snapshot_manifest_path TEXT NOT NULL,
            snapshot_manifest_sha256 TEXT NOT NULL,
            records_sha256 TEXT NOT NULL,
            exchange_sha256 TEXT NOT NULL,
            validation_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS snapshot_members (
            snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id),
            artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
            role TEXT NOT NULL,
            PRIMARY KEY(snapshot_id, role)
        );
        CREATE TABLE IF NOT EXISTS usage_events (
            event_id TEXT PRIMARY KEY,
            run_id TEXT,
            stage TEXT NOT NULL,
            executor TEXT NOT NULL,
            model TEXT,
            reasoning_effort TEXT,
            input_tokens INTEGER,
            cached_input_tokens INTEGER,
            output_tokens INTEGER,
            latency_ms INTEGER,
            outcome TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_jobs_runnable ON jobs(status, lease_expires_epoch, created_at);
        CREATE INDEX IF NOT EXISTS idx_claims_run_subject ON claims(run_id, subject_type, subject_key);
        CREATE INDEX IF NOT EXISTS idx_artifacts_run_kind ON artifacts(run_id, kind);
        """)
        metadata = {
            "control_schema_version": str(CONTROL_SCHEMA_VERSION),
            "project_id": str(project_record["project_id"]),
            "pair_id": str(project_record["pair_id"]),
            "completion_gate": str((legacy_state or {}).get("completion_gate", "OPEN")),
            "project_stage": str((legacy_state or {}).get("stage", "BOOTSTRAPPED")),
        }
        con.executemany(
            "INSERT INTO metadata(key,value) VALUES (?,?) ON CONFLICT(key) DO NOTHING",
            metadata.items(),
        )
        for run_id, row in (legacy_state or {}).get("runs", {}).items():
            now = utc_now()
            con.execute(
                """INSERT INTO runs(run_id,stage,manifest_path,records_sha256,terminal_exception_json,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?) ON CONFLICT(run_id) DO NOTHING""",
                (
                    run_id,
                    row.get("stage", "DISCOVERED"),
                    row.get("manifest", f"runs/{run_id}/manifest.json"),
                    row.get("records_sha256"),
                    stable_json(row.get("terminal_exception")) if row.get("terminal_exception") is not None else None,
                    now,
                    now,
                ),
            )
        con.commit()
    finally:
        con.close()


def state_projection(project: Path) -> dict[str, Any]:
    """Return a human-readable projection; SQLite remains canonical."""
    con = connect_control_db(project)
    try:
        meta = {row["key"]: row["value"] for row in con.execute("SELECT key,value FROM metadata")}
        runs: dict[str, Any] = {}
        for row in con.execute("SELECT * FROM runs ORDER BY run_id"):
            runs[row["run_id"]] = {
                "stage": row["stage"],
                "manifest": row["manifest_path"],
                "records_sha256": row["records_sha256"],
                "terminal_exception": json.loads(row["terminal_exception_json"]) if row["terminal_exception_json"] else None,
            }
        return {
            "schema_version": CONTROL_SCHEMA_VERSION,
            "project_id": meta["project_id"],
            "pair_id": meta["pair_id"],
            "stage": meta.get("project_stage", "BOOTSTRAPPED"),
            "runs": runs,
            "task_bindings": {},
            "latest_event_id": None,
            "completion_gate": meta.get("completion_gate", "OPEN"),
            "canonical_state": "control/state.sqlite3",
        }
    finally:
        con.close()


def sync_state_projection(project: Path) -> dict[str, Any]:
    state = state_projection(project)
    atomic_write_json(project_paths(project)["state"], state)
    return state


def bootstrap_project(project: Path) -> dict[str, Any]:
    paths = project_paths(project)
    for key in ("control", "runs", "cache", "internal", "fixtures"):
        paths[key].mkdir(parents=True, exist_ok=True)

    stable_id = str(uuid.uuid5(uuid.NAMESPACE_URL, paths["root"].as_uri()))
    pair_id = str(uuid.uuid5(uuid.NAMESPACE_URL, paths["root"].as_uri() + "#wakg-literature-pipeline"))
    if paths["project"].exists():
        project_record = load_json(paths["project"])
        project_record["tool_version"] = TOOL_VERSION
        project_record["platform_contract"] = PLATFORM_CONTRACT
        project_record["control_schema_version"] = CONTROL_SCHEMA_VERSION
        project_record["claim_schema_version"] = CLAIM_SCHEMA_VERSION
        project_record.setdefault("budgets", DEFAULT_BUDGET)
        atomic_write_json(paths["project"], project_record)
    else:
        project_record = {
            "schema_version": 1,
            "project_id": stable_id,
            "pair_id": pair_id,
            "canonical_repo": str(paths["root"]),
            "platform_contract": PLATFORM_CONTRACT,
            "tool_version": TOOL_VERSION,
            "control_schema_version": CONTROL_SCHEMA_VERSION,
            "claim_schema_version": CLAIM_SCHEMA_VERSION,
            "writer_role": "EXECUTION_CONTROL",
            "source_role": "SOURCE_SCOUT",
            "acceptance_role": "ACCEPTANCE_REVIEW",
            "budgets": DEFAULT_BUDGET,
            "created_at": utc_now(),
        }
        atomic_write_json(paths["project"], project_record)

    legacy_state = load_json(paths["state"]) if paths["state"].exists() else None
    initialize_control_db(project, project_record, legacy_state)
    state = sync_state_projection(project)
    return {"project": project_record, "state": state, "status": "ready"}


def stage_idempotency_key(stage: str, input_hashes: dict[str, Any], stage_config: dict[str, Any] | None = None) -> str:
    payload = {
        "stage": stage,
        "inputs": input_hashes,
        "parser_version": PARSER_VERSION,
        "schema_version": SCHEMA_VERSION,
        "platform_contract": PLATFORM_CONTRACT,
        "extraction_policy_version": EXTRACTION_POLICY_VERSION,
        "tool_version": TOOL_VERSION,
        "stage_config": stage_config or {},
    }
    return sha256_bytes(stable_json(payload).encode("utf-8"))


def artifact_is_current(project: Path, artifact_id: str | None, con: sqlite3.Connection | None = None) -> bool:
    """Check that a registered artifact still exists at its recorded hash."""
    if not artifact_id:
        return False
    owns_connection = con is None
    if owns_connection:
        con = connect_control_db(project)
    assert con is not None
    try:
        row = con.execute("SELECT relative_path,sha256 FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
        if row is None:
            return False
        path = Path(row["relative_path"])
        candidate = path if path.is_absolute() else project_paths(project)["root"] / path
        return candidate.is_file() and sha256_path(candidate) == row["sha256"]
    finally:
        if owns_connection:
            con.close()


def invalidate_stale_job_cache(project: Path, idempotency_key: str, reason: str) -> bool:
    """Make a stale succeeded job leaseable again without claiming a cache hit."""
    con = connect_control_db(project)
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT job_id,status FROM jobs WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        if row is None or row["status"] != "succeeded":
            con.rollback()
            return False
        con.execute(
            """UPDATE jobs SET status='retryable_failed',error_json=?,lease_owner=NULL,
               lease_expires_epoch=NULL,updated_at=? WHERE job_id=?""",
            (stable_json({"type": "stale_cache", "detail": reason[:500]}), utc_now(), row["job_id"]),
        )
        con.commit()
        return True
    except Exception:
        if con.in_transaction:
            con.rollback()
        raise
    finally:
        con.close()


def queue_job(
    project: Path,
    run_id: str,
    stage: str,
    idempotency_key: str,
    input_hash: str,
    max_attempts: int = 2,
) -> dict[str, Any]:
    bootstrap_project(project)
    job_id = "job-" + sha256_bytes(f"{run_id}|{idempotency_key}".encode("utf-8"))[:24]
    now = utc_now()
    con = connect_control_db(project)
    try:
        con.execute(
            """INSERT INTO jobs(job_id,run_id,stage,idempotency_key,input_hash,status,max_attempts,created_at,updated_at)
               VALUES (?,?,?,?,?,'queued',?,?,?) ON CONFLICT(idempotency_key) DO NOTHING""",
            (job_id, run_id, stage, idempotency_key, input_hash, max_attempts, now, now),
        )
        con.commit()
        row = con.execute("SELECT * FROM jobs WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        assert row is not None
        return dict(row)
    finally:
        con.close()


def lease_job(project: Path, idempotency_key: str, owner: str, lease_seconds: int = 300) -> dict[str, Any] | None:
    """Atomically lease one known job; expired leases are recoverable."""
    con = connect_control_db(project)
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM jobs WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        if row is None:
            con.rollback()
            raise KeyError(f"unknown idempotency key: {idempotency_key}")
        now_epoch = time.time()
        stale_cache_recovery = False
        active = row["status"] in {"leased", "running"} and (row["lease_expires_epoch"] or 0) > now_epoch
        if row["status"] == "succeeded":
            if artifact_is_current(project, row["output_artifact_id"], con):
                con.rollback()
                return {**dict(row), "cache_hit": True}
            stale_cache_recovery = True
            con.execute(
                """UPDATE jobs SET status='retryable_failed',error_json=?,lease_owner=NULL,
                   lease_expires_epoch=NULL,updated_at=? WHERE job_id=?""",
                (stable_json({"type": "stale_cache", "detail": "succeeded output artifact is missing or hash-mismatched"}), utc_now(), row["job_id"]),
            )
            row = con.execute("SELECT * FROM jobs WHERE job_id=?", (row["job_id"],)).fetchone()
            assert row is not None
        elif row["status"] == "retryable_failed":
            try:
                stale_cache_recovery = json.loads(row["error_json"] or "{}").get("type") == "stale_cache"
            except json.JSONDecodeError:
                stale_cache_recovery = False
        if active and row["lease_owner"] != owner:
            con.rollback()
            return None
        if int(row["attempt_count"]) >= int(row["max_attempts"]) and not stale_cache_recovery:
            con.execute(
                "UPDATE jobs SET status='dead_letter',lease_owner=NULL,lease_expires_epoch=NULL,updated_at=? WHERE job_id=?",
                (utc_now(), row["job_id"]),
            )
            con.commit()
            return None
        if row["status"] in {"leased", "running"} and int(row["attempt_count"]) > 0:
            con.execute(
                """UPDATE attempts SET ended_at=?,outcome='lease_expired'
                   WHERE job_id=? AND attempt_number=? AND ended_at IS NULL""",
                (utc_now(), row["job_id"], row["attempt_count"]),
            )
        attempt_number = int(row["attempt_count"]) + 1
        attempt_id = f"attempt-{row['job_id'][4:]}-{attempt_number}"
        now = utc_now()
        con.execute(
            """UPDATE jobs SET status='running',lease_owner=?,lease_expires_epoch=?,attempt_count=?,updated_at=?
               WHERE job_id=?""",
            (owner, now_epoch + max(1, lease_seconds), attempt_number, now, row["job_id"]),
        )
        con.execute(
            """INSERT INTO attempts(attempt_id,job_id,attempt_number,lease_owner,started_at)
               VALUES (?,?,?,?,?)""",
            (attempt_id, row["job_id"], attempt_number, owner, now),
        )
        con.commit()
        leased = con.execute("SELECT * FROM jobs WHERE job_id=?", (row["job_id"],)).fetchone()
        return {**dict(leased), "attempt_id": attempt_id, "cache_hit": False}
    except Exception:
        if con.in_transaction:
            con.rollback()
        raise
    finally:
        con.close()


def complete_job(project: Path, job_id: str, owner: str, output_artifact_id: str | None = None) -> dict[str, Any]:
    con = connect_control_db(project)
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        if row["lease_owner"] != owner or row["status"] not in {"leased", "running"}:
            raise RuntimeError("job is not leased by this owner")
        now = utc_now()
        con.execute(
            """UPDATE jobs SET status='succeeded',output_artifact_id=?,lease_owner=NULL,lease_expires_epoch=NULL,
               error_json=NULL,updated_at=? WHERE job_id=?""",
            (output_artifact_id, now, job_id),
        )
        con.execute(
            """UPDATE attempts SET ended_at=?,outcome='succeeded'
               WHERE job_id=? AND attempt_number=?""",
            (now, job_id, row["attempt_count"]),
        )
        con.commit()
        return dict(con.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone())
    except Exception:
        if con.in_transaction:
            con.rollback()
        raise
    finally:
        con.close()


def fail_job(project: Path, job_id: str, owner: str, error: dict[str, Any], retryable: bool) -> dict[str, Any]:
    con = connect_control_db(project)
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        if row["lease_owner"] != owner:
            raise RuntimeError("job is not leased by this owner")
        exhausted = int(row["attempt_count"]) >= int(row["max_attempts"])
        status = "retryable_failed" if retryable and not exhausted else "dead_letter"
        now = utc_now()
        payload = stable_json(error)
        con.execute(
            """UPDATE jobs SET status=?,error_json=?,lease_owner=NULL,lease_expires_epoch=NULL,updated_at=?
               WHERE job_id=?""",
            (status, payload, now, job_id),
        )
        con.execute(
            """UPDATE attempts SET ended_at=?,outcome=?,error_json=?
               WHERE job_id=? AND attempt_number=?""",
            (now, status, payload, job_id, row["attempt_count"]),
        )
        con.commit()
        return dict(con.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone())
    except Exception:
        if con.in_transaction:
            con.rollback()
        raise
    finally:
        con.close()


def register_artifact(project: Path, run_id: str, kind: str, path: Path, immutable: bool = True) -> dict[str, Any]:
    path = path.resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    digest = sha256_path(path)
    artifact_id = "artifact-" + sha256_bytes(f"{run_id}|{kind}|{digest}".encode("utf-8"))[:24]
    row = {
        "artifact_id": artifact_id,
        "run_id": run_id,
        "kind": kind,
        "relative_path": relative_to_project(path, project),
        "sha256": digest,
        "bytes": path.stat().st_size,
        "immutable": 1 if immutable else 0,
        "created_at": utc_now(),
    }
    con = connect_control_db(project)
    try:
        con.execute(
            """INSERT INTO artifacts(artifact_id,run_id,kind,relative_path,sha256,bytes,immutable,created_at)
               VALUES (:artifact_id,:run_id,:kind,:relative_path,:sha256,:bytes,:immutable,:created_at)
               ON CONFLICT(artifact_id) DO NOTHING""",
            row,
        )
        con.commit()
        return dict(con.execute("SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone())
    finally:
        con.close()


def record_run_issue(
    project: Path,
    run_id: str,
    code: str,
    severity: str,
    status: str,
    detail: dict[str, Any],
    record_key: str | None = None,
    field_path: str | None = None,
) -> str:
    """Persist one deterministic, source-level issue without duplicating retries."""
    identity = stable_json([run_id, code, record_key, field_path, detail])
    issue_id = "issue-" + sha256_bytes(identity.encode("utf-8"))[:24]
    con = connect_control_db(project)
    try:
        con.execute(
            """INSERT INTO issues(issue_id,run_id,code,severity,status,record_key,field_path,detail_json,created_at)
               VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(issue_id) DO NOTHING""",
            (issue_id, run_id, code, severity, status, record_key, field_path, stable_json(detail), utc_now()),
        )
        con.commit()
    finally:
        con.close()
    return issue_id


def record_usage_event(
    project: Path,
    run_id: str | None,
    stage: str,
    executor: str,
    outcome: str,
    latency_ms: int,
    model: str | None = None,
    reasoning_effort: str | None = None,
    input_tokens: int | None = None,
    cached_input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> str:
    payload = [run_id, stage, executor, outcome, latency_ms, utc_now(), uuid.uuid4().hex]
    event_id = "usage-" + sha256_bytes(stable_json(payload).encode("utf-8"))[:24]
    con = connect_control_db(project)
    try:
        con.execute(
            """INSERT INTO usage_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (event_id, run_id, stage, executor, model, reasoning_effort, input_tokens, cached_input_tokens, output_tokens, latency_ms, outcome, utc_now()),
        )
        con.commit()
    finally:
        con.close()
    return event_id


def normalize_doi(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.strip().lower()
    cleaned = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", cleaned)
    cleaned = re.sub(r"^doi:\s*", "", cleaned)
    match = re.search(r"10\.\d{4,9}/[-._;()/:a-z0-9]+", cleaned, flags=re.I)
    return match.group(0).rstrip(".,;)") if match else None


def normalize_title(value: str | None) -> str:
    value = value or ""
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def candidate_identity(candidate: dict[str, Any]) -> str:
    doi = normalize_doi(candidate.get("doi"))
    if doi:
        return "doi:" + doi
    pii = re.sub(r"[^a-z0-9]", "", str(candidate.get("pii") or "").casefold())
    if pii:
        return "pii:" + pii
    pdf_hash = str(candidate.get("pdf_sha256") or "").casefold()
    if re.fullmatch(r"[0-9a-f]{64}", pdf_hash):
        return "pdf:" + pdf_hash
    title = normalize_title(candidate.get("title"))
    author_key = " ".join(sorted(a.casefold() for a in candidate.get("authors", [])[:3] if a))
    if title and author_key:
        return "title-author:" + title + "|" + author_key
    openalex_id = str(candidate.get("openalex_id") or "").strip()
    if openalex_id:
        return "openalex:" + openalex_id.rsplit("/", 1)[-1].casefold()
    return ""


def score_candidate(candidate: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
    text = " ".join(
        str(candidate.get(key) or "") for key in ("title", "abstract", "keywords")
    ).casefold()
    components: list[tuple[str, int, bool]] = [
        ("QXRD/Rietveld/amorphous", 30, bool(re.search(r"\bqxrd\b|rietveld|quantitative x[- ]ray|amorphous", text))),
        ("MIX and macroscopic performance", 20, bool(re.search(r"mix proportion|mixture proportion|compressive strength|flexural strength|mortar|paste", text))),
        ("PSD", 15, bool(re.search(r"particle size|\bpsd\b|d10|d50|d90", text))),
        ("XRF", 15, bool(re.search(r"\bxrf\b|x[- ]ray fluorescence|chemical composition", text))),
        ("Other characterization", 8, bool(re.search(r"ft[- ]?ir|nmr|sem|eds|tga|calorim|xrd", text))),
        ("Trusted identity", 7, bool(normalize_doi(candidate.get("doi"))) and bool(candidate.get("metadata_sources"))),
        ("Accessible/recent", 5, bool(candidate.get("fulltext_url")) or int(candidate.get("year") or 0) >= 2018),
    ]
    explanation = [{"criterion": name, "points": points if matched else 0, "matched": matched} for name, points, matched in components]
    return sum(row["points"] for row in explanation), explanation


def merge_candidate(target: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(target)
    for key in ("doi", "pii", "pdf_sha256", "title", "year", "abstract", "fulltext_url", "landing_url", "openalex_id"):
        if not merged.get(key) and incoming.get(key):
            merged[key] = incoming[key]
    merged["authors"] = sorted(set((merged.get("authors") or []) + (incoming.get("authors") or [])))
    merged["metadata_sources"] = sorted(set((merged.get("metadata_sources") or []) + (incoming.get("metadata_sources") or [])))
    if incoming.get("access_status") == "OPEN_FULLTEXT_CANDIDATE":
        merged["access_status"] = incoming["access_status"]
    for key in ("has_raw_points", "has_supplements", "has_complete_mat_mix_results"):
        if key in target or key in incoming:
            merged[key] = target.get(key) is True or incoming.get(key) is True
    return merged


def dedupe_candidates(candidates: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    by_identity: dict[str, dict[str, Any]] = {}
    aliases: dict[str, str] = {}
    for candidate in candidates:
        identity = candidate_identity(candidate)
        if not identity:
            continue
        # Index every available identity, including title/author when a DOI is
        # present. A later provider row can bridge two earlier partial records.
        keys = {identity}
        partial = dict(candidate)
        for key in ("doi", "pii", "pdf_sha256"):
            partial.pop(key, None)
            alias = candidate_identity(partial)
            if alias:
                keys.add(alias)
        for key in ("pii", "pdf_sha256", "openalex_id"):
            alias = candidate_identity({key: candidate.get(key)})
            if alias:
                keys.add(alias)
        matched = sorted({aliases[key] for key in keys if key in aliases})
        # Provider IDs and title matches cannot override contradictory DOIs.
        doi = normalize_doi(candidate.get("doi"))
        matched = [prior for prior in matched if not (
            doi and normalize_doi(by_identity[prior].get("doi"))
            and doi != normalize_doi(by_identity[prior].get("doi")))]
        root = matched[0] if matched else identity
        merged = dict(candidate)
        for prior in matched:
            merged = merge_candidate(by_identity.pop(prior), merged)
        by_identity[root] = merged
        for alias, prior in list(aliases.items()):
            if prior in matched:
                aliases[alias] = root
        for alias in keys:
            aliases[alias] = root
    output = []
    for candidate in by_identity.values():
        candidate["doi"] = normalize_doi(candidate.get("doi"))
        candidate["normalized_title"] = normalize_title(candidate.get("title"))
        score, explanation = score_candidate(candidate)
        candidate["score"] = score
        candidate["score_explanation"] = explanation
        output.append(candidate)
    output.sort(key=lambda row: (
        -row["score"],
        -(row.get("has_raw_points") is True),
        -(row.get("has_supplements") is True),
        -(row.get("has_complete_mat_mix_results") is True),
        -(row.get("year") or 0), row.get("title") or "",
    ))
    return output


def cache_path_for_url(cache_dir: Path, url: str, cache_context: dict[str, Any] | None = None) -> Path:
    """Address cached provider bytes by URL and discovery contract, never result count."""
    identity: Any = url if cache_context is None else {"url": url, "cache_context": cache_context}
    return cache_dir / (sha256_bytes(stable_json(identity).encode("utf-8")) + ".json")


def fetch_json(
    url: str,
    cache_dir: Path,
    ttl_seconds: int = 30 * 86400,
    attempts: int = 3,
    timeout: int = 20,
    cache_context: dict[str, Any] | None = None,
) -> Any:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.netloc not in {"api.crossref.org", "api.openalex.org"}:
        raise ValueError(f"unsupported API endpoint: {url}")
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_path_for_url(cache_dir, url, cache_context)
    if cache_path.exists():
        cached = load_json(cache_path)
        if time.time() - float(cached.get("fetched_epoch", 0)) <= ttl_seconds:
            return cached["payload"]

    delays = [0, 2, 5]
    last_error: Exception | None = None
    for attempt in range(min(attempts, len(delays))):
        if delays[attempt]:
            time.sleep(delays[attempt])
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": f"wakg-literature-pipeline/{TOOL_VERSION} anonymous-public-pilot"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            atomic_write_json(cache_path, {"fetched_epoch": time.time(), "url": url, "payload": payload})
            return payload
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code == 429:
                retry_after = exc.headers.get("Retry-After")
                if retry_after and attempt + 1 < attempts:
                    try:
                        time.sleep(min(float(retry_after), 30.0))
                    except ValueError:
                        pass
                    continue
            if 500 <= exc.code < 600:
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            continue
    raise RuntimeError(f"API request failed after {attempts} attempts: {last_error}")


def parse_crossref(payload: dict[str, Any]) -> list[dict[str, Any]]:
    output = []
    for item in payload.get("message", {}).get("items", []):
        title = " ".join(item.get("title") or [])
        authors = [" ".join(part for part in (a.get("given"), a.get("family")) if part) for a in item.get("author", [])]
        date_parts = (item.get("published-print") or item.get("published-online") or item.get("issued") or {}).get("date-parts", [[]])
        year = date_parts[0][0] if date_parts and date_parts[0] else None
        links = item.get("link") or []
        pdf_links = [link.get("URL") for link in links if "pdf" in str(link.get("content-type", "")).casefold() and link.get("URL")]
        license_evidence = crossref_open_license_evidence(item)
        output.append({
            "doi": item.get("DOI"),
            "title": title,
            "authors": [author for author in authors if author],
            "year": year,
            "abstract": re.sub(r"<[^>]+>", " ", item.get("abstract") or ""),
            "fulltext_url": pdf_links[0] if pdf_links and license_evidence else None,
            "landing_url": item.get("URL"),
            "access_status": "OPEN_FULLTEXT_CANDIDATE" if pdf_links and license_evidence else ("METADATA_FULLTEXT_LINK" if pdf_links else "METADATA_ONLY"),
            "license_evidence": license_evidence,
            "metadata_sources": ["crossref"],
        })
    return output


class ProviderProtocolError(ValueError):
    """A provider response that cannot truthfully be treated as a search result."""


def _license_date_is_effective(license_row: dict[str, Any]) -> bool:
    """Reject future-dated Crossref licence assertions without inferring missing dates."""
    parts = ((license_row.get("start") or {}).get("date-parts") or [[]])[0]
    if not parts:
        return True
    try:
        year, month, day = (list(parts) + [1, 1])[:3]
        return datetime(int(year), int(month), int(day), tzinfo=timezone.utc) <= datetime.now(timezone.utc)
    except (TypeError, ValueError):
        return False


def _recognized_open_license(url: Any) -> str | None:
    value = str(url or "").strip().casefold()
    if not value.startswith("https://"):
        return None
    if "creativecommons.org/licenses/" not in value:
        return None
    match = re.search(r"licenses/([^/?#]+)/?(?:([0-9.]+))?", value)
    if not match:
        return "CC"
    return "CC-" + match.group(1).upper() + ("-" + match.group(2) if match.group(2) else "")


def crossref_open_license_evidence(item: dict[str, Any]) -> dict[str, str] | None:
    """Return explicit current OA licence evidence, never treating a PDF link as OA proof."""
    compatible_versions = {"vor", "published-version", "am", "accepted-manuscript"}
    for row in item.get("license") or []:
        if not isinstance(row, dict):
            continue
        license_kind = _recognized_open_license(row.get("URL"))
        version = str(row.get("content-version") or "").casefold()
        if license_kind and version in compatible_versions and _license_date_is_effective(row):
            return {"license": license_kind, "content_version": version, "evidence_tier": "CROSSREF_EXPLICIT_OPEN_LICENSE"}
    return None


def validate_openalex_envelope(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or not isinstance(payload.get("meta"), dict) or not isinstance(payload.get("results"), list):
        raise ProviderProtocolError("OpenAlex response must contain object meta and array results")
    return payload


def parse_openalex(payload: dict[str, Any]) -> list[dict[str, Any]]:
    payload = validate_openalex_envelope(payload)
    output = []
    for item in payload.get("results", []):
        authors = [
            ((row.get("author") or {}).get("display_name") or "")
            for row in item.get("authorships", [])
        ]
        location = item.get("best_oa_location") or item.get("primary_location") or {}
        fulltext = location.get("pdf_url")
        output.append({
            "doi": item.get("doi"),
            "title": item.get("title") or "",
            "authors": [author for author in authors if author],
            "year": item.get("publication_year"),
            "abstract": "",
            "fulltext_url": fulltext,
            "landing_url": location.get("landing_page_url") or item.get("id"),
            "openalex_id": item.get("id"),
            "access_status": "OPEN_FULLTEXT_CANDIDATE" if fulltext else "METADATA_ONLY",
            "metadata_sources": ["openalex"],
        })
    return output


def _safe_https_url(value: Any) -> tuple[str, str] | None:
    if not isinstance(value, str):
        return None
    parsed = urllib.parse.urlparse(value.strip())
    if parsed.scheme.casefold() != "https" or not parsed.hostname or parsed.username or parsed.password:
        return None
    return value.strip(), parsed.hostname.casefold()


def _openalex_id_key(value: Any) -> str:
    text = str(value or "").strip().rstrip("/").casefold()
    return text.rsplit("/", 1)[-1] if text else ""


def openalex_batch_route_url(dois: Iterable[Any]) -> str:
    """Build, but never fetch, a bounded OpenAlex DOI-OR route query."""
    unique = sorted({doi for doi in (normalize_doi(value) for value in dois) if doi})
    if not unique or len(unique) > 100:
        raise ValueError("OpenAlex route resolver requires one to 100 normalized DOIs")
    params = {
        "filter": "doi:" + "|".join(unique),
        "per_page": 100,
        "select": "id,doi,title,locations,best_oa_location,primary_location,open_access",
    }
    return "https://api.openalex.org/works?" + urllib.parse.urlencode(params)


def _openalex_location_routes(work: dict[str, Any]) -> list[dict[str, Any]]:
    locations: list[dict[str, Any]] = []
    for candidate in list(work.get("locations") or []) + [work.get("best_oa_location"), work.get("primary_location")]:
        if not isinstance(candidate, dict) or not candidate.get("is_oa"):
            continue
        source_type = str((candidate.get("source") or {}).get("type") or "unknown").casefold()
        source_rank = {"repository": 0, "preprint": 1, "publisher": 2}.get(source_type, 3)
        license_value = candidate.get("license") or (work.get("open_access") or {}).get("oa_status") or "unknown"
        for url_kind, raw_url in (("pdf", candidate.get("pdf_url")), ("landing", candidate.get("landing_page_url"))):
            safe = _safe_https_url(raw_url)
            if not safe:
                continue
            url, hostname = safe
            route = {
                "provider": "openalex",
                "source_type": source_type,
                "is_oa": True,
                "license": str(license_value),
                "version": str(candidate.get("version") or "unknown"),
                "evidence_tier": "OPENALEX_LOCATION_IS_OA",
                "provenance": {"openalex_id": _openalex_id_key(work.get("id")), "location_source_type": source_type, "url_kind": url_kind},
                "url": url,
                "url_sha256": sha256_bytes(url.encode("utf-8")),
                "hostname": hostname,
                "rank": [source_rank, 0 if url_kind == "pdf" else 1, hostname, sha256_bytes(url.encode("utf-8"))],
            }
            locations.append(route)
    unique = {row["url_sha256"]: row for row in locations}
    return sorted(unique.values(), key=lambda row: tuple(row["rank"]))


def _catalog_rows(catalog_path: Path) -> list[dict[str, Any]]:
    payload = load_json(catalog_path)
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        return [row for row in payload.get("candidates", []) if isinstance(row, dict)]
    raise ValueError("route catalog must be a candidate list or envelope")


def resolve_oa_routes(
    project: Path,
    run_id: str,
    catalog_path: Path,
    link_probe_path: Path,
    openalex_envelope_path: Path,
    required_selected_order_sha256: str = HARDEN_03_D3_SELECTED_ORDER_SHA256,
) -> dict[str, Any]:
    """Resolve frozen D3 identities using an offline OpenAlex envelope; no network is performed."""
    bootstrap_project(project)
    catalog_rows = _catalog_rows(catalog_path)
    probe = load_json(link_probe_path)
    envelope = validate_openalex_envelope(load_json(openalex_envelope_path))
    selected_order = [str(row.get("identity_sha256") or "") for row in probe.get("results", [])]
    selected_url_order = [str(row.get("url_sha256") or "") for row in probe.get("results", [])]
    expected_hash = str((probe.get("selection") or {}).get("selected_order_sha256") or "")
    observed_url_hash = sha256_bytes(stable_json(selected_url_order).encode("utf-8"))
    if len(selected_order) != 30 or not expected_hash or expected_hash != observed_url_hash or expected_hash != required_selected_order_sha256:
        raise ValueError("route resolver requires exactly the frozen 30-result D3 probe")
    candidates_by_identity: dict[str, dict[str, Any]] = {}
    for row in catalog_rows:
        identity = candidate_identity(row)
        if identity:
            candidates_by_identity[sha256_bytes(identity.encode("utf-8"))] = row
    if len(set(selected_order)) != 30 or any(identity not in candidates_by_identity for identity in selected_order):
        raise ValueError("frozen D3 identities do not map exactly to catalog candidates")
    selected_rows = [candidates_by_identity[identity] for identity in selected_order]
    route_url = openalex_batch_route_url(row.get("doi") for row in selected_rows if normalize_doi(row.get("doi")))
    by_doi = {normalize_doi(row.get("doi")): row for row in envelope["results"] if normalize_doi(row.get("doi"))}
    by_openalex = {_openalex_id_key(row.get("id")): row for row in envelope["results"] if _openalex_id_key(row.get("id"))}
    internal_rows: list[dict[str, Any]] = []
    for identity_sha, candidate in zip(selected_order, selected_rows):
        work = by_doi.get(normalize_doi(candidate.get("doi"))) or by_openalex.get(_openalex_id_key(candidate.get("openalex_id")))
        routes = _openalex_location_routes(work) if work else []
        internal_rows.append({"identity_sha256": identity_sha, "doi": normalize_doi(candidate.get("doi")), "openalex_id": _openalex_id_key(candidate.get("openalex_id")), "routes": routes})
    internal_path = project_paths(project)["internal"] / "oa-route-resolve" / run_id / "route-catalog.json"
    atomic_write_json(internal_path, {"schema_version": 1, "contract": OA_ROUTE_RESOLVER_CONTRACT_VERSION, "routes": internal_rows})
    body_free_members = []
    for row in internal_rows:
        selected = row["routes"][0] if row["routes"] else None
        body_free_members.append({
            "identity_sha256": row["identity_sha256"], "provider_match": bool(row["routes"]), "route_count": len(row["routes"]),
            "selected_route": ({key: selected[key] for key in ("provider", "source_type", "is_oa", "license", "version", "evidence_tier", "url_sha256", "hostname", "provenance")} if selected else None),
        })
    report = {
        "schema_version": 1, "run_id": run_id, "contract": OA_ROUTE_RESOLVER_CONTRACT_VERSION,
        "offline_only": True, "network_requests": 0, "pdf_or_body_persisted": False,
        "frozen_selection": {"identity_count": len(selected_order), "selected_order_sha256": expected_hash, "observed_identity_order_sha256": sha256_bytes(stable_json(selected_order).encode("utf-8"))},
        "inputs": {"catalog_sha256": sha256_path(catalog_path), "link_probe_sha256": sha256_path(link_probe_path), "openalex_envelope_sha256": sha256_path(openalex_envelope_path), "openalex_request_url_sha256": sha256_bytes(route_url.encode("utf-8")), "doi_count": sum(bool(normalize_doi(row.get("doi"))) for row in selected_rows)},
        "route_catalog": {"relative_path": relative_to_project(internal_path, project), "sha256": sha256_path(internal_path)},
        "members": body_free_members,
        "route_count": sum(len(row["routes"]) for row in internal_rows),
        "no_false_oa_promotion": all(route["is_oa"] and _safe_https_url(route["url"]) for row in internal_rows for route in row["routes"]),
    }
    report_path = project_paths(project)["runs"] / run_id / "route-manifest.json"
    atomic_write_json(report_path, report)
    return report


def _require_sha256(value: Any, label: str) -> str:
    text = str(value or "")
    if not re.fullmatch(r"[0-9a-f]{64}", text):
        raise ValueError(f"{label} must be a 64-hex SHA256")
    return text


def _identity_sha256(candidate: dict[str, Any]) -> str:
    identity = candidate_identity(candidate)
    if not identity:
        raise ValueError("candidate has no stable identity")
    return sha256_bytes(identity.encode("utf-8"))


def _legacy_member(candidate: dict[str, Any]) -> dict[str, Any]:
    """Normalize one H03 candidate without treating its URL as verified access."""
    doi = normalize_doi(candidate.get("doi"))
    safe = _safe_https_url(candidate.get("fulltext_url"))
    if not doi or not safe:
        raise ValueError("pool member requires normalized DOI and safe HTTPS legacy fulltext URL")
    identity_sha256 = _identity_sha256(candidate)
    computed_score, computed_explanation = score_candidate(candidate)
    score = candidate.get("score", computed_score)
    if not isinstance(score, int):
        raise ValueError("legacy candidate score must be an integer")
    explanation = candidate.get("score_explanation", computed_explanation)
    if not isinstance(explanation, list):
        raise ValueError("legacy candidate score explanation must be a list")
    try:
        year = int(candidate.get("year") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("legacy candidate year must be numeric") from exc
    strata = sorted({str(value).strip() for value in candidate.get("query_strata", []) if str(value).strip()})
    if not strata:
        raise ValueError("pool member requires at least one query stratum")
    return {
        "identity_sha256": identity_sha256,
        "doi": doi,
        "doi_sha256": sha256_bytes(doi.encode("utf-8")),
        "legacy_url": safe[0],
        "legacy_url_sha256": sha256_bytes(safe[0].encode("utf-8")),
        "legacy_host": safe[1],
        "score": score,
        "score_explanation": explanation,
        "year": year,
        "query_strata": strata,
    }


def _legacy_order_key(member: dict[str, Any]) -> tuple[Any, ...]:
    return (-int(member["score"]), -int(member["year"]), str(member["identity_sha256"]))


def _member_body_free(member: dict[str, Any], route: dict[str, Any] | None = None) -> dict[str, Any]:
    output = {
        "identity_sha256": member["identity_sha256"], "doi_sha256": member["doi_sha256"],
        "url_sha256": member["legacy_url_sha256"], "hostname": member["legacy_host"],
        "score": member["score"], "score_explanation": member["score_explanation"],
        "year": member["year"], "query_strata": member["query_strata"],
        "mix_macroscopic_matched": any(
            row.get("criterion") == "MIX and macroscopic performance" and bool(row.get("matched"))
            for row in member["score_explanation"] if isinstance(row, dict)
        ),
    }
    if route is not None:
        output.update({
            "url_sha256": route["url_sha256"], "hostname": route["hostname"],
            "route_source_type": route["source_type"], "route_tier": _route_tier(route),
            "route_evidence_tier": route["evidence_tier"],
        })
    return output


def _arm_member_manifest(member: dict[str, Any], route: dict[str, Any] | None = None) -> dict[str, Any]:
    """The committed arm plan contains identifiers and route hashes, never per-paper semantics."""
    output = {"identity_sha256": member["identity_sha256"], "url_sha256": member["legacy_url_sha256"]}
    if route is not None:
        output.update({"url_sha256": route["url_sha256"], "route_tier": _route_tier(route)})
    return output


def _route_tier(route: dict[str, Any]) -> str:
    source_type = str(route.get("source_type") or "").casefold()
    return "REPOSITORY_OR_PREPRINT_DIRECT_PDF" if source_type in {"repository", "preprint"} else "OTHER_DIRECT_PDF_OA"


def content_score(member: dict[str, Any]) -> int:
    """Remove only the registered legacy Accessible/recent component."""
    explanation = member.get("score_explanation")
    if not isinstance(explanation, list):
        raise ValueError("content score requires registered score explanation")
    matches = [row for row in explanation if isinstance(row, dict) and row.get("criterion") == "Accessible/recent"]
    if len(matches) != 1 or not isinstance(matches[0].get("points"), int):
        raise ValueError("content score requires exactly one Accessible/recent component")
    score = member.get("score")
    if not isinstance(score, int):
        raise ValueError("content score requires integer legacy score")
    return score - int(matches[0]["points"])


def _hybrid_candidates(members: list[dict[str, Any]], envelope: dict[str, Any]) -> dict[str, dict[str, Any]]:
    by_doi = {normalize_doi(row.get("doi")): row for row in envelope.get("results", []) if isinstance(row, dict) and normalize_doi(row.get("doi"))}
    if len(by_doi) != len(members) or {member["doi"] for member in members} != set(by_doi):
        raise ValueError("hybrid envelope DOI identity mismatch")
    output = {}
    for member in members:
        routes = [row for row in _openalex_location_routes(by_doi[member["doi"]]) if row["provenance"].get("url_kind") == "pdf"]
        output[member["identity_sha256"]] = {"member": member, "route": routes[0] if routes else None}
    return output


def _hybrid_rank(candidates: dict[str, dict[str, Any]], weight: int) -> list[dict[str, Any]]:
    ranked = []
    for identity, value in candidates.items():
        member = value["member"]; route = value["route"]; base = content_score(member)
        ranked.append({"identity_sha256": identity, "content_score": base, "hybrid_score": base + (weight if route else 0),
                       "year": member["year"], "route": route, "member": member,
                       "url_sha256": route["url_sha256"] if route else member["legacy_url_sha256"],
                       "hostname": route["hostname"] if route else member["legacy_host"]})
    return sorted(ranked, key=lambda row: (-row["hybrid_score"], -row["content_score"], -int(row["year"]), row["identity_sha256"]))


def _hybrid_metrics(rows: list[dict[str, Any]], outcome_by_url: dict[str, str] | None = None) -> dict[str, Any]:
    verified = {"VERIFIED_PDF_CONTENT_TYPE", "VERIFIED_PDF_MAGIC"}
    known = 0; verified_count = 0
    for row in rows:
        result = (outcome_by_url or {}).get(row["url_sha256"])
        if result is not None:
            known += 1; verified_count += result in verified
    return {"identity_count": len(rows), "mean_content_score": sum(row["content_score"] for row in rows) / len(rows),
            "mix_macroscopic_matched_count": sum(any(item.get("criterion") == "MIX and macroscopic performance" and bool(item.get("matched")) for item in row["member"]["score_explanation"] if isinstance(item, dict)) for row in rows),
            "domain_count": len({row["hostname"] for row in rows}), "query_strata_count": len({item for row in rows for item in row["member"]["query_strata"]}),
            "known_outcome_count": known, "unknown_outcome_count": len(rows) - known, "verified_pdf_count": verified_count,
            "member_order_sha256": sha256_bytes(stable_json([{"identity_sha256": row["identity_sha256"], "url_sha256": row["url_sha256"]} for row in rows]).encode("utf-8"))}


def hybrid_prepare(
    project: Path, run_id: str, h03_catalog_path: Path, h03_probe_path: Path, h05_pool_path: Path,
    h05_arm_manifest_path: Path, h05_envelope_path: Path, h05_union_path: Path, h05_probe_path: Path,
) -> dict[str, Any]:
    """Offline-only H05 tuning and disjoint H06 pool freeze; no metadata or link request is made."""
    h05_members = load_json(h05_pool_path).get("members", [])
    if not isinstance(h05_members, list) or len(h05_members) != 100:
        raise ValueError("H05 pool must contain exactly 100 internal members")
    envelope = validate_openalex_envelope(load_json(h05_envelope_path))
    candidates = _hybrid_candidates(h05_members, envelope)
    arm = load_json(h05_arm_manifest_path); union = load_json(h05_union_path); probe = load_json(h05_probe_path)
    if arm.get("status") != "FROZEN_PRE_PROBE" or len((arm.get("arms") or {}).get("A", {}).get("members", [])) != 20:
        raise ValueError("H05 arm A must be a frozen 20-member plan")
    mappings = union.get("mappings") if isinstance(union, dict) else None
    if not isinstance(mappings, list) or len(mappings) != 40:
        raise ValueError("H05 union mapping must retain 40 arm memberships")
    outcome_by_url = {str(row.get("url_sha256")): str(row.get("classification")) for row in probe.get("results", []) if isinstance(row, dict)}
    arm_a = []
    for row in arm["arms"]["A"]["members"]:
        identity = str(row.get("identity_sha256")); value = candidates.get(identity)
        if not value or str(row.get("url_sha256")) != value["member"]["legacy_url_sha256"]:
            raise ValueError("H05 legacy arm provenance mismatch")
        member = value["member"]
        arm_a.append({"identity_sha256": identity, "content_score": content_score(member), "hybrid_score": content_score(member), "year": member["year"], "route": None, "member": member, "url_sha256": member["legacy_url_sha256"], "hostname": member["legacy_host"]})
    baseline = _hybrid_metrics(arm_a, outcome_by_url)
    table = []
    for weight in (0, 2, 4, 6, 8, 10, 12):
        selected = _hybrid_rank(candidates, weight)[:20]; metrics = _hybrid_metrics(selected, outcome_by_url)
        score_drop = baseline["mean_content_score"] - metrics["mean_content_score"]
        viable = score_drop <= 3.0 and baseline["mix_macroscopic_matched_count"] - metrics["mix_macroscopic_matched_count"] <= 2 and metrics["domain_count"] > 5 and metrics["query_strata_count"] > 4 and metrics["verified_pdf_count"] >= baseline["verified_pdf_count"] + 2
        table.append({"weight": weight, "metrics": metrics, "content_score_drop": score_drop, "verified_improvement": metrics["verified_pdf_count"] - baseline["verified_pdf_count"], "viable": viable})
    viable = [row for row in table if row["viable"]]
    result = {"schema_version": 1, "type": "OA_HYBRID_PREPARE", "run_id": run_id, "contract": OA_HYBRID_RANKING_CONTRACT_VERSION,
              "offline_only": True, "network_requests": 0, "wakg_accesses": 0, "model_calls": 0,
              "inputs": {"h03_catalog_sha256": sha256_path(h03_catalog_path), "h03_probe_sha256": sha256_path(h03_probe_path), "h05_pool_sha256": sha256_path(h05_pool_path), "h05_arm_manifest_sha256": sha256_path(h05_arm_manifest_path), "h05_envelope_sha256": sha256_path(h05_envelope_path), "h05_union_sha256": sha256_path(h05_union_path), "h05_probe_sha256": sha256_path(h05_probe_path)},
              "baseline": baseline, "tune_table": table}
    report_path = project_paths(project)["runs"] / run_id / "prepare-report.json"
    if not viable:
        result.update({"status": "TUNE_BLOCKED", "chosen_weight": None, "live_authorized": False})
        atomic_write_json(report_path, result); return result
    chosen = sorted(viable, key=lambda row: (-row["metrics"]["verified_pdf_count"], row["content_score_drop"], row["weight"]))[0]
    h03_probe = load_json(h03_probe_path)
    excluded = {str(row.get("identity_sha256")) for row in h03_probe.get("results", []) if isinstance(row, dict)} | {row["identity_sha256"] for row in h05_members}
    unique: dict[str, dict[str, Any]] = {}
    for raw in _catalog_rows(h03_catalog_path):
        try: member = _legacy_member(raw)
        except ValueError: continue
        if member["identity_sha256"] not in excluded: unique.setdefault(member["identity_sha256"], member)
    pool = _select_diversified_pool(list(unique.values()), 100)
    pool_path = project_paths(project)["internal"] / "oa-hybrid" / run_id / "pool-catalog.json"
    atomic_write_json(pool_path, {"schema_version": 1, "contract": OA_HYBRID_RANKING_CONTRACT_VERSION, "members": pool})
    result.update({"status": "PREPARED", "chosen_weight": chosen["weight"], "chosen_profile": chosen, "live_authorized": True,
                   "h06_pool": {"size": 100, "identity_order_sha256": sha256_bytes(stable_json([row["identity_sha256"] for row in pool]).encode("utf-8")), "host_count": len({row["legacy_host"] for row in pool}), "query_strata": sorted({item for row in pool for item in row["query_strata"]}), "excluded_identity_count": len(excluded), "internal_catalog": {"relative_path": relative_to_project(pool_path, project), "sha256": sha256_path(pool_path)}}})
    atomic_write_json(report_path, result); return result


HYBRID_LIVE_STAGES = ("PREPARED", "METADATA_CACHED", "ARMS_FROZEN", "PROBED", "SUMMARIZED", "REPLAYED", "VERIFIED")


def hybrid_default_budgets() -> dict[str, Any]:
    """The frozen, low-cost transport caps for a future H06 confirmation only."""
    return {"schema_version": 1, "metadata": {"requests_max": 1, "bytes_max": 10 * 1024 * 1024, "timeout_seconds": 120},
            "probe": {"unique_urls_max": 40, "requests_max": 80, "range_read_max": 8193, "timeout_seconds": 8, "wall_seconds_max": 300},
            "model_calls_max": 0, "wakg_accesses_max": 0, "body_free_outputs": True}


def _hybrid_run_root(project: Path, run_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", run_id):
        raise ValueError("hybrid run_id is unsafe")
    return project_paths(project)["runs"] / run_id


def _hybrid_internal_root(project: Path, run_id: str) -> Path:
    return project_paths(project)["internal"] / "oa-hybrid" / run_id


def _hybrid_state_path(project: Path, run_id: str) -> Path:
    return _hybrid_run_root(project, run_id) / "hybrid-state.json"


def _hybrid_safe_path(project: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise ValueError("hybrid artifact path is unsafe")
    candidate = (project / relative).resolve()
    def comparable(path: Path) -> str:
        value = str(path)
        # pathlib can preserve a Windows extended-length prefix for a child but
        # not its parent; compare canonical DOS forms before containment.
        if value.startswith("\\\\?\\"):
            value = value[4:]
        return os.path.normcase(os.path.abspath(os.path.normpath(value)))
    try:
        if os.path.commonpath([comparable(candidate), comparable(project)]) != comparable(project):
            raise ValueError("outside project")
    except ValueError as exc:
        raise ValueError("hybrid artifact path escapes project") from exc
    return candidate


def _hybrid_state(project: Path, run_id: str) -> dict[str, Any]:
    path = _hybrid_state_path(project, run_id)
    if not path.is_file():
        raise FileNotFoundError("hybrid live state is absent")
    state = load_json(path)
    if state.get("contract") != OA_HYBRID_RANKING_CONTRACT_VERSION or state.get("run_id") != run_id:
        raise ValueError("hybrid state contract mismatch")
    if state.get("stage") not in HYBRID_LIVE_STAGES:
        raise ValueError("hybrid state stage is invalid")
    return state


def _hybrid_write_state(project: Path, run_id: str, state: dict[str, Any]) -> None:
    if state.get("stage") not in HYBRID_LIVE_STAGES:
        raise ValueError("cannot publish invalid hybrid stage")
    atomic_write_json(_hybrid_state_path(project, run_id), state)


def _hybrid_ref(project: Path, path: Path) -> dict[str, str]:
    return {"relative_path": relative_to_project(path, project), "sha256": sha256_path(path)}


def _hybrid_load_ref(project: Path, ref: Any) -> tuple[Path, Any]:
    if not isinstance(ref, dict):
        raise ValueError("hybrid artifact reference is malformed")
    path = _hybrid_safe_path(project, str(ref.get("relative_path") or ""))
    if not path.is_file() or ref.get("sha256") != sha256_path(path):
        raise ValueError("hybrid artifact hash mismatch")
    return path, load_json(path)


def hybrid_bind_experiment_card(project: Path, run_id: str, experiment_card_path: Path) -> dict[str, Any]:
    """Bind a viable prepared profile to one immutable experiment card before live use."""
    report_path = _hybrid_run_root(project, run_id) / "prepare-report.json"
    report = load_json(report_path); card = load_json(experiment_card_path)
    if report.get("status") != "PREPARED" or report.get("live_authorized") is not True:
        raise ValueError("only a viable PREPARED report can bind a live experiment card")
    if card.get("run_id") != run_id or card.get("phase") != "offline hybrid-ranking formulation and prospective cohort preparation":
        raise ValueError("experiment card does not match prepared hybrid run")
    bound = {**report, "experiment_card": _hybrid_ref(project, experiment_card_path)}
    atomic_write_json(report_path, bound)
    return bound


def hybrid_doi_or_url(members: list[dict[str, Any]]) -> str:
    """Build the one deterministic OpenAlex DOI-or request without retaining it in reports."""
    dois = sorted({normalize_doi(row.get("doi")) for row in members if normalize_doi(row.get("doi"))})
    if len(dois) != 100:
        raise ValueError("hybrid metadata request requires exactly 100 unique DOI members")
    query = urllib.parse.urlencode({"filter": "doi:" + "|".join(dois), "per-page": "100"})
    return "https://api.openalex.org/works?" + query


def _hybrid_budget(budgets: Any) -> dict[str, Any]:
    if not isinstance(budgets, dict) or budgets.get("body_free_outputs") is not True:
        raise ValueError("hybrid budget manifest is malformed")
    metadata = budgets.get("metadata"); probe = budgets.get("probe")
    if not isinstance(metadata, dict) or not isinstance(probe, dict):
        raise ValueError("hybrid budget sections are missing")
    required = ((metadata, "requests_max", 1, 1), (metadata, "bytes_max", 1, 10 * 1024 * 1024), (metadata, "timeout_seconds", 1, 120),
                (probe, "unique_urls_max", 1, 40), (probe, "requests_max", 1, 80), (probe, "range_read_max", 1, 8193),
                (probe, "timeout_seconds", 1, 8), (probe, "wall_seconds_max", 1, 300))
    for section, key, minimum, maximum in required:
        value = section.get(key)
        if not isinstance(value, int) or not minimum <= value <= maximum:
            raise ValueError(f"hybrid budget {key} is outside contract")
    if budgets.get("model_calls_max") != 0 or budgets.get("wakg_accesses_max") != 0:
        raise ValueError("hybrid budget permits forbidden calls")
    return budgets


def _hybrid_metrics_from_members(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"identity_count": len(rows), "domain_count": len({row["hostname"] for row in rows}),
            "query_strata_count": len({item for row in rows for item in row["member"]["query_strata"]}),
            "mean_content_score": sum(row["content_score"] for row in rows) / len(rows),
            "mix_macroscopic_matched_count": sum(any(item.get("criterion") == "MIX and macroscopic performance" and bool(item.get("matched")) for item in row["member"]["score_explanation"] if isinstance(item, dict)) for row in rows),
            "member_order_sha256": sha256_bytes(stable_json([{"identity_sha256": row["identity_sha256"], "url_sha256": row["url_sha256"]} for row in rows]).encode("utf-8"))}


def _hybrid_arm_manifest(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [{"identity_sha256": row["identity_sha256"], "url_sha256": row["url_sha256"]} for row in rows]


def _hybrid_freeze_arms(project: Path, run_id: str, state: dict[str, Any], envelope: dict[str, Any], pool: list[dict[str, Any]]) -> dict[str, Any]:
    chosen_weight = state.get("chosen_weight")
    if not isinstance(chosen_weight, int):
        raise ValueError("hybrid chosen weight is absent")
    candidates = _hybrid_candidates(pool, envelope)
    arm_a = []
    for member in sorted(pool, key=_legacy_order_key)[:20]:
        arm_a.append({"identity_sha256": member["identity_sha256"], "content_score": content_score(member), "member": member,
                      "url_sha256": member["legacy_url_sha256"], "hostname": member["legacy_host"], "url": member["legacy_url"]})
    arm_c = _hybrid_rank(candidates, chosen_weight)[:20]
    if len(arm_a) != 20 or len(arm_c) != 20 or len({row["identity_sha256"] for row in arm_a}) != 20 or len({row["identity_sha256"] for row in arm_c}) != 20:
        raise ValueError("hybrid arms must each contain 20 unique identities")
    mappings = []
    for arm, rows in (("A", arm_a), ("C", arm_c)):
        for row in rows:
            mappings.append({"arm": arm, "identity_sha256": row["identity_sha256"], "url": row["url"] if "url" in row else row["route"]["url"],
                             "url_sha256": row["url_sha256"], "hostname": row["hostname"], "score": row["content_score"],
                             "query_strata": row["member"]["query_strata"]})
    unique = {row["url_sha256"]: row for row in mappings}
    if len(unique) > 40:
        raise ValueError("hybrid union exceeds unique URL budget")
    internal = _hybrid_internal_root(project, run_id); internal.mkdir(parents=True, exist_ok=True)
    union_path = internal / "union-probe-catalog.json"
    atomic_write_json(union_path, {"schema_version": 1, "contract": OA_HYBRID_RANKING_CONTRACT_VERSION, "mappings": mappings,
                                   "candidates": [{"fulltext_url": row["url"], "score": row["score"], "query_strata": row["query_strata"], "doi": row["identity_sha256"]} for _, row in sorted(unique.items())]})
    arm_path = _hybrid_run_root(project, run_id) / "hybrid-arms.json"
    arm_report = {"schema_version": 1, "contract": OA_HYBRID_RANKING_CONTRACT_VERSION, "run_id": run_id, "status": "FROZEN_PRE_PROBE",
                  "inputs": {"metadata_envelope_sha256": state["artifacts"]["metadata_envelope"]["sha256"], "pool_sha256": state["artifacts"]["pool"]["sha256"]},
                  "arms": {"A": {"members": _hybrid_arm_manifest(arm_a), "metrics": _hybrid_metrics_from_members(arm_a)},
                           "C": {"members": _hybrid_arm_manifest(arm_c), "metrics": _hybrid_metrics_from_members(arm_c)}},
                  "union": {"mapping_count": len(mappings), "unique_url_count": len(unique), "internal_catalog": _hybrid_ref(project, union_path)},
                  "raw_urls_or_bodies_persisted": False}
    atomic_write_json(arm_path, arm_report)
    return {"arms": _hybrid_ref(project, arm_path), "union": _hybrid_ref(project, union_path)}


def _hybrid_summary(project: Path, state: dict[str, Any]) -> dict[str, Any]:
    _, arm = _hybrid_load_ref(project, state["artifacts"].get("arms")); _, union = _hybrid_load_ref(project, state["artifacts"].get("union")); _, probe = _hybrid_load_ref(project, state["artifacts"].get("probe"))
    results = probe.get("results") if isinstance(probe, dict) else None; mappings = union.get("mappings") if isinstance(union, dict) else None
    if not isinstance(results, list) or not isinstance(mappings, list) or arm.get("status") != "FROZEN_PRE_PROBE":
        raise ValueError("hybrid summary inputs are malformed")
    unique_urls = {str(row.get("url_sha256")) for row in mappings if isinstance(row, dict)}
    result_by_url = {str(row.get("url_sha256")): row for row in results if isinstance(row, dict)}
    if len(result_by_url) != len(results) or set(result_by_url) != unique_urls:
        raise ValueError("hybrid probe coverage is incomplete or duplicated")
    verified = {"VERIFIED_PDF_CONTENT_TYPE", "VERIFIED_PDF_MAGIC"}; outcomes: dict[str, Any] = {}
    for name in ("A", "C"):
        members = (arm.get("arms") or {}).get(name, {}).get("members"); metrics = (arm.get("arms") or {}).get(name, {}).get("metrics")
        if not isinstance(members, list) or len(members) != 20 or not isinstance(metrics, dict):
            raise ValueError("hybrid arm is malformed")
        observed = [result_by_url.get(str(row.get("url_sha256"))) for row in members]
        if any(row is None for row in observed):
            raise ValueError("hybrid arm lacks probe observation")
        count = sum(row.get("classification") in verified for row in observed)
        outcomes[name] = {"verified_pdf_count": count, "verified_pdf_ratio": count / 20, **metrics}
    budget = state["budgets"]; requests = int(probe.get("actual_requests") or 0); bytes_used = int(probe.get("bytes_read") or 0); wall = int(probe.get("latency_ms") or 0) / 1000
    caps = budget["probe"]
    if len(unique_urls) > caps["unique_urls_max"] or requests > caps["requests_max"] or bytes_used > len(unique_urls) * caps["range_read_max"] or wall > caps["wall_seconds_max"]:
        raise ValueError("hybrid link-probe budget exceeded")
    relevance = outcomes["C"]["mean_content_score"] >= outcomes["A"]["mean_content_score"] - 3 and outcomes["C"]["mix_macroscopic_matched_count"] >= outcomes["A"]["mix_macroscopic_matched_count"] - 2 and all(outcomes[name]["domain_count"] > 5 and outcomes[name]["query_strata_count"] > 4 for name in ("A", "C"))
    ratio = outcomes["C"]["verified_pdf_ratio"]; delta = ratio - outcomes["A"]["verified_pdf_ratio"]
    verdict = "INVALID" if not relevance else ("PASS" if ratio >= .75 and delta >= .10 else ("PARTIAL" if ratio >= .75 or delta >= .10 else "FAIL"))
    return {"schema_version": 1, "type": "SOURCE_RESULT", "contract": OA_HYBRID_RANKING_CONTRACT_VERSION, "run_id": state["run_id"], "verdict": verdict,
            "arms": outcomes, "primary": {"c_ratio": ratio, "c_minus_a": delta, "pass_conditions": {"c_ratio_ge_075": ratio >= .75, "delta_ge_010": delta >= .10}},
            "relevance_guardrails_pass": relevance, "accounting": {"metadata_requests": state["counters"]["metadata_requests"], "metadata_bytes": state["counters"]["metadata_bytes"], "replay_requests": 0, "replay_bytes": 0, "unique_urls": len(unique_urls), "link_requests": requests, "link_bytes": bytes_used, "link_wall_seconds": wall},
            "raw_urls_or_bodies_persisted": False, "model_calls": 0, "wakg_accesses": 0}


def _hybrid_recompute_frozen_arms(project: Path, state: dict[str, Any]) -> None:
    """Rebuild both arms and their union from immutable pool and metadata evidence."""
    pool_path, pool_blob = _hybrid_load_ref(project, state["artifacts"].get("pool")); _, envelope = _hybrid_load_ref(project, state["artifacts"].get("metadata_envelope")); _, arm = _hybrid_load_ref(project, state["artifacts"].get("arms")); _, union = _hybrid_load_ref(project, state["artifacts"].get("union"))
    pool = pool_blob.get("members") if isinstance(pool_blob, dict) else None
    if not isinstance(pool, list) or len(pool) != 100 or arm.get("inputs", {}).get("pool_sha256") != sha256_path(pool_path) or arm.get("inputs", {}).get("metadata_envelope_sha256") != state["artifacts"]["metadata_envelope"]["sha256"]:
        raise ValueError("hybrid pool or arm provenance mismatch")
    candidates = _hybrid_candidates(pool, envelope); expected_a = []
    for member in sorted(pool, key=_legacy_order_key)[:20]:
        expected_a.append({"identity_sha256": member["identity_sha256"], "content_score": content_score(member), "member": member, "url_sha256": member["legacy_url_sha256"], "hostname": member["legacy_host"]})
    expected_c = _hybrid_rank(candidates, int(state.get("chosen_weight")))[:20]
    for name, expected in (("A", expected_a), ("C", expected_c)):
        actual = (arm.get("arms") or {}).get(name, {})
        if stable_json(actual.get("members")) != stable_json(_hybrid_arm_manifest(expected)) or stable_json(actual.get("metrics")) != stable_json(_hybrid_metrics_from_members(expected)):
            raise ValueError("hybrid arm membership or metric recomputation mismatch")
    expected_pairs = {(name, row["identity_sha256"], row["url_sha256"]) for name, rows in (("A", expected_a), ("C", expected_c)) for row in rows}
    observed_pairs = {(str(row.get("arm")), str(row.get("identity_sha256")), str(row.get("url_sha256"))) for row in (union.get("mappings") or []) if isinstance(row, dict)}
    if observed_pairs != expected_pairs or len(observed_pairs) != 40:
        raise ValueError("hybrid union mapping recomputation mismatch")


def hybrid_failure_envelope(project: Path, run_id: str, command: str, error: Exception) -> dict[str, Any]:
    """Body-free failure report with counters derived from the latest atomic state."""
    try:
        state = _hybrid_state(project, run_id); counters = state.get("counters") or {}; stage = state.get("stage")
    except Exception:
        counters = {}; stage = "UNSTARTED"
    result = {"schema_version": 1, "type": "SOURCE_RESULT", "run_id": run_id, "command": command, "verdict": "INVALID",
              "failure_fingerprint": "OA_HYBRID_" + sha256_bytes((type(error).__name__ + ":" + str(error)).encode("utf-8"))[:16],
              "error_type": type(error).__name__, "last_completed_stage": stage,
              "network_requests": int(counters.get("metadata_requests", 0)) + int(counters.get("link_requests", 0)),
              "metadata_bytes": int(counters.get("metadata_bytes", 0)), "link_bytes": int(counters.get("link_bytes", 0)),
              "link_wall_seconds": counters.get("link_wall_seconds", 0), "raw_url_or_body_persisted": False}
    atomic_write_json(_hybrid_run_root(project, run_id) / f"{command}-failure.json", result)
    return result


def hybrid_live_controller(project: Path, run_id: str, experiment_card_path: Path, budget_path: Path,
                           metadata_fetcher: Any = None, probe_runner: Any = None, fail_after: str | None = None) -> dict[str, Any]:
    """Run or resume the bounded H06 transport only after a viable frozen preparation."""
    run_root = _hybrid_run_root(project, run_id); prepare_path = run_root / "prepare-report.json"; prepare = load_json(prepare_path)
    card = load_json(experiment_card_path); budgets = _hybrid_budget(load_json(budget_path))
    if prepare.get("status") != "PREPARED" or prepare.get("live_authorized") is not True:
        raise ValueError("live requires a viable authorized PREPARED report")
    if card.get("run_id") != run_id or prepare.get("experiment_card", {}).get("sha256") != sha256_path(experiment_card_path):
        raise ValueError("live experiment-card hash is not frozen in prepare report")
    pool_ref = prepare.get("h06_pool", {}).get("internal_catalog") or {}; pool_path = _hybrid_safe_path(project, str(pool_ref.get("relative_path") or ""))
    if not pool_path.is_file() or pool_ref.get("sha256") != sha256_path(pool_path):
        raise ValueError("prepared H06 pool is absent or tampered")
    pool = load_json(pool_path).get("members");
    if not isinstance(pool, list) or len(pool) != 100 or len({row.get("identity_sha256") for row in pool}) != 100:
        raise ValueError("prepared H06 pool must contain 100 unique members")
    state_path = _hybrid_state_path(project, run_id)
    if state_path.is_file():
        state = _hybrid_state(project, run_id)
        if state.get("prepare_sha256") != sha256_path(prepare_path) or state.get("budget_sha256") != sha256_path(budget_path):
            raise ValueError("live resume inputs changed")
    else:
        state = {"schema_version": 1, "contract": OA_HYBRID_RANKING_CONTRACT_VERSION, "run_id": run_id, "stage": "PREPARED",
                 "prepare_sha256": sha256_path(prepare_path), "experiment_card_sha256": sha256_path(experiment_card_path), "budget_sha256": sha256_path(budget_path), "budgets": budgets,
                 "chosen_weight": prepare.get("chosen_weight"), "idempotency_keys": {"metadata": sha256_bytes(stable_json([run_id, "metadata", sha256_path(prepare_path), sha256_path(pool_path), sha256_path(budget_path)]).encode("utf-8")), "arms": sha256_bytes(stable_json([run_id, "arms", sha256_path(pool_path), prepare.get("chosen_weight")]).encode("utf-8")), "probe": sha256_bytes(stable_json([run_id, "probe", sha256_path(pool_path), sha256_path(budget_path)]).encode("utf-8"))},
                 "artifacts": {"pool": _hybrid_ref(project, pool_path), "experiment_card": _hybrid_ref(project, experiment_card_path), "budget": _hybrid_ref(project, budget_path)},
                 "counters": {"metadata_requests": 0, "metadata_bytes": 0, "link_requests": 0, "link_bytes": 0, "link_wall_seconds": 0, "model_calls": 0, "wakg_accesses": 0}}
        _hybrid_write_state(project, run_id, state)
    internal = _hybrid_internal_root(project, run_id); internal.mkdir(parents=True, exist_ok=True)
    if state["stage"] == "PREPARED":
        url = hybrid_doi_or_url(pool); fetch = metadata_fetcher or benchmark_fetch_json
        payload, receipt = fetch(project, run_id, "openalex", "h06-doi-or", 1, url, timeout=budgets["metadata"]["timeout_seconds"], attempt_budget=1, raw_byte_cap=budgets["metadata"]["bytes_max"])
        envelope = validate_openalex_envelope(payload)
        planned = {row["doi"] for row in pool}; observed = {normalize_doi(row.get("doi")) for row in envelope.get("results", []) if normalize_doi(row.get("doi"))}
        attempts = int(receipt.get("actual_http_attempts") or 0); raw_bytes = int(receipt.get("raw_network_bytes", receipt.get("bytes", 0)) or 0)
        if len(envelope.get("results", [])) != 100 or observed != planned or attempts > budgets["metadata"]["requests_max"] or raw_bytes > budgets["metadata"]["bytes_max"]:
            raise ValueError("hybrid metadata envelope or budget mismatch")
        envelope_path = internal / "metadata-envelope.json"; receipt_path = internal / "metadata-receipt.json"; atomic_write_json(envelope_path, envelope); atomic_write_json(receipt_path, receipt)
        state["artifacts"].update({"metadata_envelope": _hybrid_ref(project, envelope_path), "metadata_receipt": _hybrid_ref(project, receipt_path)})
        state["counters"].update({"metadata_requests": attempts, "metadata_bytes": raw_bytes}); state["stage"] = "METADATA_CACHED"; _hybrid_write_state(project, run_id, state)
        if fail_after == "METADATA_CACHED": raise RuntimeError("injected crash after metadata cache")
    if state["stage"] == "METADATA_CACHED":
        _, envelope = _hybrid_load_ref(project, state["artifacts"].get("metadata_envelope")); state["artifacts"].update(_hybrid_freeze_arms(project, run_id, state, envelope, pool)); state["stage"] = "ARMS_FROZEN"; _hybrid_write_state(project, run_id, state)
        if fail_after == "ARMS_FROZEN": raise RuntimeError("injected crash after arms freeze")
    if state["stage"] == "ARMS_FROZEN":
        union_path, union = _hybrid_load_ref(project, state["artifacts"].get("union")); unique_count = len(union.get("candidates") or [])
        probe = (probe_runner or probe_links)(project, run_id, catalog=union_path, probe_limit=unique_count, byte_cap=budgets["probe"]["range_read_max"], timeout=budgets["probe"]["timeout_seconds"], max_wall_seconds=budgets["probe"]["wall_seconds_max"], max_requests=budgets["probe"]["requests_max"])
        if not isinstance(probe, dict) or int(probe.get("actual_requests") or 0) > budgets["probe"]["requests_max"]:
            raise ValueError("hybrid probe result or request budget mismatch")
        probe_path = run_root / "link-probe.json"; atomic_write_json(probe_path, probe)
        state["artifacts"]["probe"] = _hybrid_ref(project, probe_path); state["counters"].update({"link_requests": int(probe.get("actual_requests") or 0), "link_bytes": int(probe.get("bytes_read") or 0), "link_wall_seconds": int(probe.get("latency_ms") or 0) / 1000}); state["stage"] = "PROBED"; _hybrid_write_state(project, run_id, state)
        if fail_after == "PROBED": raise RuntimeError("injected crash after link probe")
    if state["stage"] == "PROBED":
        summary = _hybrid_summary(project, state); result_path = run_root / "source-result.json"; atomic_write_json(result_path, summary); state["artifacts"]["summary"] = _hybrid_ref(project, result_path); state["stage"] = "SUMMARIZED"; _hybrid_write_state(project, run_id, state)
        if fail_after == "SUMMARIZED": raise RuntimeError("injected crash after summary")
    return state


def hybrid_replay_controller(project: Path, run_id: str) -> dict[str, Any]:
    """Strictly zero-network replay from verified cached evidence and frozen arms."""
    state = _hybrid_state(project, run_id)
    if HYBRID_LIVE_STAGES.index(state["stage"]) < HYBRID_LIVE_STAGES.index("SUMMARIZED"):
        raise ValueError("replay requires a completed live summary")
    _, receipt = _hybrid_load_ref(project, state["artifacts"].get("metadata_receipt")); _, envelope = _hybrid_load_ref(project, state["artifacts"].get("metadata_envelope")); _, prior = _hybrid_load_ref(project, state["artifacts"].get("summary"))
    if int(receipt.get("actual_http_attempts") or 0) > 1 or not validate_openalex_envelope(envelope):
        raise ValueError("cached metadata evidence is invalid")
    _hybrid_recompute_frozen_arms(project, state)
    replayed = _hybrid_summary(project, state)
    if stable_json(replayed) != stable_json(prior):
        raise ValueError("cache-only replay summary differs from frozen live summary")
    replay_path = _hybrid_run_root(project, run_id) / "replay-proof.json"; proof = {"schema_version": 1, "run_id": run_id, "network_requests": 0, "metadata_replay": "cache_only", "summary_sha256": sha256_path(_hybrid_safe_path(project, state["artifacts"]["summary"]["relative_path"])), "replayed_sha256": sha256_bytes(stable_json(replayed).encode("utf-8")), "raw_urls_or_bodies_persisted": False}; atomic_write_json(replay_path, proof)
    state["artifacts"]["replay"] = _hybrid_ref(project, replay_path); state["stage"] = "REPLAYED"; _hybrid_write_state(project, run_id, state)
    return state


def hybrid_verify_controller(project: Path, run_id: str) -> dict[str, Any]:
    """Independently recompute hashes, arms, budgets, verdict, and replay equality."""
    state = _hybrid_state(project, run_id)
    if HYBRID_LIVE_STAGES.index(state["stage"]) < HYBRID_LIVE_STAGES.index("REPLAYED"):
        raise ValueError("verification requires a completed zero-network replay")
    prepare_path = _hybrid_run_root(project, run_id) / "prepare-report.json"
    _, budget = _hybrid_load_ref(project, state["artifacts"].get("budget")); _, card = _hybrid_load_ref(project, state["artifacts"].get("experiment_card"))
    _hybrid_budget(budget)
    if (state.get("prepare_sha256") != sha256_path(prepare_path) or state.get("budget_sha256") != state["artifacts"]["budget"]["sha256"]
            or state.get("experiment_card_sha256") != state["artifacts"]["experiment_card"]["sha256"]
            or card.get("run_id") != run_id or state["counters"].get("model_calls") != 0 or state["counters"].get("wakg_accesses") != 0):
        raise ValueError("hybrid immutable inputs or forbidden-call counters mismatch")
    _hybrid_recompute_frozen_arms(project, state)
    _, summary = _hybrid_load_ref(project, state["artifacts"].get("summary")); _, replay = _hybrid_load_ref(project, state["artifacts"].get("replay")); recomputed = _hybrid_summary(project, state)
    if stable_json(summary) != stable_json(recomputed) or replay.get("network_requests") != 0 or replay.get("replayed_sha256") != sha256_bytes(stable_json(recomputed).encode("utf-8")):
        raise ValueError("hybrid replay or summary verification failed")
    report = {"schema_version": 1, "type": "OA_HYBRID_VERIFY", "run_id": run_id, "status": "VERIFIED", "source_result_sha256": sha256_bytes(stable_json(summary).encode("utf-8")), "verdict": summary.get("verdict"), "network_requests": 0, "raw_urls_or_bodies_persisted": False}
    report_path = _hybrid_run_root(project, run_id) / "verify-report.json"; atomic_write_json(report_path, report); state["artifacts"]["verify"] = _hybrid_ref(project, report_path); state["stage"] = "VERIFIED"; _hybrid_write_state(project, run_id, state)
    return state


PREVERIFIED_RANKING_CONTRACT_VERSION = "oa-preverified-ranking-v1"
PREVERIFIED_LIVE_STAGES = HYBRID_LIVE_STAGES


def preverified_default_budgets() -> dict[str, Any]:
    """H07's registered future-live bounds; no current caller may spend them."""
    return {"schema_version": 1, "metadata": {"requests_max": 1, "bytes_max": 10 * 1024 * 1024, "timeout_seconds": 120, "cost_usd_max": 0.01},
            "probe": {"unique_urls_max": 60, "requests_max": 120, "range_read_max": 8193, "timeout_seconds": 8, "wall_seconds_max": 450},
            "model_calls_max": 0, "wakg_accesses_max": 0, "body_free_outputs": True}


def _pre_root(project: Path, run_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", run_id):
        raise ValueError("preverified run_id is unsafe")
    return project_paths(project)["runs"] / run_id


def _pre_state_path(project: Path, run_id: str) -> Path:
    return _pre_root(project, run_id) / "preverified-state.json"


def _pre_watermark_path(project: Path, run_id: str) -> Path:
    return _pre_root(project, run_id) / "preverified-stage-watermark.json"


def _pre_state(project: Path, run_id: str) -> dict[str, Any]:
    path = _pre_state_path(project, run_id)
    if not path.is_file(): raise FileNotFoundError("preverified live state is absent")
    state = load_json(path)
    if state.get("contract") != PREVERIFIED_RANKING_CONTRACT_VERSION or state.get("run_id") != run_id or state.get("stage") not in PREVERIFIED_LIVE_STAGES:
        raise ValueError("preverified state contract mismatch")
    watermark_path = _pre_watermark_path(project, run_id)
    if watermark_path.is_file():
        watermark = load_json(watermark_path).get("stage")
        if watermark not in PREVERIFIED_LIVE_STAGES or PREVERIFIED_LIVE_STAGES.index(state["stage"]) < PREVERIFIED_LIVE_STAGES.index(watermark):
            raise ValueError("PREVERIFIED_STAGE_ROLLBACK")
    return state


def _pre_write_state(project: Path, run_id: str, state: dict[str, Any]) -> None:
    if state.get("stage") not in PREVERIFIED_LIVE_STAGES: raise ValueError("invalid preverified state stage")
    watermark_path = _pre_watermark_path(project, run_id)
    prior = load_json(watermark_path).get("stage") if watermark_path.is_file() else None
    if prior in PREVERIFIED_LIVE_STAGES and PREVERIFIED_LIVE_STAGES.index(state["stage"]) < PREVERIFIED_LIVE_STAGES.index(prior): raise ValueError("PREVERIFIED_STAGE_ROLLBACK")
    atomic_write_json(watermark_path, {"schema_version":1,"run_id":run_id,"stage":state["stage"]})
    atomic_write_json(_pre_state_path(project, run_id), state)


def _pre_budget(value: Any) -> dict[str, Any]:
    # H07 differs only in larger probe ceilings; use H06's schema checks first.
    if not isinstance(value, dict) or value.get("body_free_outputs") is not True or value.get("model_calls_max") != 0 or value.get("wakg_accesses_max") != 0:
        raise ValueError("preverified budget manifest is malformed")
    metadata = value.get("metadata"); probe = value.get("probe")
    required = ((metadata, "requests_max", 1, 1), (metadata, "bytes_max", 1, 10 * 1024 * 1024), (metadata, "timeout_seconds", 1, 120),
                (probe, "unique_urls_max", 1, 60), (probe, "requests_max", 1, 120), (probe, "range_read_max", 1, 8193), (probe, "timeout_seconds", 1, 8), (probe, "wall_seconds_max", 1, 450))
    for section, key, minimum, maximum in required:
        if not isinstance(section, dict) or not isinstance(section.get(key), int) or not minimum <= section[key] <= maximum:
            raise ValueError(f"preverified budget {key} is outside contract")
    return value


def preverified_probe_adapter(probe_budget: dict[str, Any], unique_url_count: int) -> tuple[int, int]:
    """Translate H07 sentinel-total and request caps to the probe primitive."""
    if not isinstance(unique_url_count, int) or not 1 <= unique_url_count <= 60: raise ValueError("preverified unique URL count is outside contract")
    sentinel_cap = probe_budget.get("range_read_max")
    if sentinel_cap != 8193: raise ValueError("preverified range_read_max must include exactly one 8192-byte sentinel")
    effective = min(int(probe_budget.get("requests_max") or 0), 2 * unique_url_count, 100)
    if effective < unique_url_count or effective > int(probe_budget["requests_max"]): raise ValueError("preverified effective probe request cap is invalid")
    return sentinel_cap - 1, effective


def preverified_probe_journal_decision(reservation_path: Path, completed_path: Path, probe_path: Path, binding: dict[str, Any]) -> str:
    """Fail closed before transport; returns ADOPT only for exact completed evidence."""
    if completed_path.is_file():
        completed = load_json(completed_path)
        if completed.get("status") == "COMPLETED" and completed.get("binding") == binding and probe_path.is_file() and completed.get("probe_sha256") == sha256_path(probe_path): return "ADOPT"
        raise ValueError("BLOCKED_UNCERTAIN_PROBE")
    if reservation_path.is_file() or probe_path.is_file(): raise ValueError("BLOCKED_UNCERTAIN_PROBE")
    atomic_write_json(reservation_path, {"schema_version":1,"status":"STARTED","binding":binding})
    return "START"


def _pre_ref(project: Path, path: Path) -> dict[str, str]: return _hybrid_ref(project, path)


def _pre_load_ref(project: Path, ref: Any) -> tuple[Path, Any]: return _hybrid_load_ref(project, ref)


def _pre_content_rows(members: list[dict[str, Any]], envelope: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = _hybrid_candidates(members, envelope); rows = []
    for identity, value in candidates.items():
        member = value["member"]; routes = [route for route in _openalex_location_routes(next(row for row in envelope["results"] if normalize_doi(row.get("doi")) == member["doi"])) if route["provenance"].get("url_kind") == "pdf"]
        route = routes[0] if routes else None
        rows.append({"identity_sha256": identity, "member": member, "content_score": content_score(member), "year": member["year"],
                     "url": route["url"] if route else member["legacy_url"], "url_sha256": route["url_sha256"] if route else member["legacy_url_sha256"],
                     "hostname": route["hostname"] if route else member["legacy_host"], "route_kind": "DIRECT_PDF_OA" if route else "LEGACY"})
    return sorted(rows, key=lambda row: (-row["content_score"], -int(row["year"]), row["identity_sha256"]))


def _pre_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"identity_count": len(rows), "mean_content_score": (sum(row["content_score"] for row in rows) / len(rows)) if rows else None,
            "mix_macroscopic_matched_count": sum(any(item.get("criterion") == "MIX and macroscopic performance" and bool(item.get("matched")) for item in row["member"]["score_explanation"] if isinstance(item, dict)) for row in rows),
            "domain_count": len({row["hostname"] for row in rows}), "query_strata_count": len({item for row in rows for item in row["member"]["query_strata"]}),
            "member_order_sha256": sha256_bytes(stable_json([{"identity_sha256": row["identity_sha256"], "url_sha256": row["url_sha256"]} for row in rows]).encode("utf-8"))}


def _pre_member_envelope_identities(path: Path) -> tuple[list[str], set[str]]:
    """Load a frozen pool/member envelope without accepting ambiguous identities."""
    payload = load_json(path)
    rows = payload if isinstance(payload, list) else payload.get("members") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError("preverified additional exclusion must be a member envelope")
    identities = [str(row.get("identity_sha256") or "") for row in rows if isinstance(row, dict)]
    if len(identities) != len(rows) or not identities or len(identities) != len(set(identities)) or any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in identities):
        raise ValueError("preverified additional exclusion identity contract mismatch")
    return identities, set(identities)


def _pre_requires_separate_acceptance(card: dict[str, Any]) -> bool:
    policy = card.get("future_live_authorization") or {}
    return isinstance(policy, dict) and (policy.get("requires_separate_acceptance") is True or policy.get("required") == "separate explicit network authorization")


def _pre_live_binding(project: Path, run_id: str, prepare: dict[str, Any], experiment_card_path: Path, budget_path: Path, pool_path: Path) -> dict[str, Any]:
    return {"run_id": run_id, "prepare_sha256": sha256_path(_pre_root(project, run_id) / "prepare-report.json"), "experiment_card_sha256": sha256_path(experiment_card_path), "budget_sha256": sha256_path(budget_path), "pool_sha256": sha256_path(pool_path), "metadata_attempts_max": 1, "probe_runs_max": 1}


def preverified_authorize_live(project: Path, run_id: str, acceptance_path: Path) -> dict[str, Any]:
    """Bind an independently accepted prepare to one future metadata/probe execution."""
    root = _pre_root(project, run_id); prepare_path = root / "prepare-report.json"; prepare = load_json(prepare_path)
    if prepare.get("status") != "PREPARED": raise ValueError("preverified prepare is not eligible for live authorization")
    card_path, card = _pre_load_ref(project, prepare.get("experiment_card")); budget_path, _ = _pre_load_ref(project, prepare.get("budget")); plan_path, plan = _pre_load_ref(project, prepare.get("plan"))
    if card.get("run_id") != run_id or not _pre_requires_separate_acceptance(card): raise ValueError("preverified run does not require separate acceptance")
    pool_path = _hybrid_safe_path(project, str((plan.get("pool") or {}).get("internal_catalog", {}).get("relative_path") or ""))
    if not pool_path.is_file() or (plan.get("pool") or {}).get("internal_catalog", {}).get("sha256") != sha256_path(pool_path): raise ValueError("preverified pool provenance mismatch")
    acceptance = load_json(acceptance_path); verdicts = [acceptance[key] for key in ("verdict", "implementation_verdict") if key in acceptance]
    if acceptance.get("run_id") != run_id or not verdicts or any(value != "PASS" for value in verdicts) or not re.fullmatch(r"[0-9a-f]{40}", str(acceptance.get("reviewed_head") or "")):
        raise ValueError("preverified acceptance verdict, run_id, or reviewed_head mismatch")
    binding = _pre_live_binding(project, run_id, prepare, card_path, budget_path, pool_path)
    for key in ("prepare_sha256", "experiment_card_sha256", "budget_sha256", "pool_sha256"):
        if acceptance.get(key) != binding[key]: raise ValueError("preverified acceptance hash mismatch")
    if any((root / name).exists() for name in ("preverified-state.json", "probe-reservation.json", "probe-completed.json", "link-probe.json")):
        raise ValueError("preverified authorization requires a fresh absent probe journal")
    artifact = {"schema_version": 1, "type": "OA_PREVERIFIED_LIVE_AUTHORIZATION", "status": "AUTHORIZED", "reviewed_head": acceptance["reviewed_head"], "acceptance_sha256": sha256_path(acceptance_path), "binding": binding, "fresh_probe_journal": True}
    path = root / "live-authorization.json"
    if path.is_file():
        if stable_json(load_json(path)) != stable_json(artifact): raise ValueError("preverified live authorization is already bound differently")
    else:
        atomic_write_json(path, artifact)
    return artifact


def _pre_require_live_authorization(project: Path, run_id: str, prepare: dict[str, Any], card: dict[str, Any], experiment_card_path: Path, budget_path: Path, pool_path: Path) -> None:
    if not _pre_requires_separate_acceptance(card):
        if prepare.get("live_authorized") is not True: raise ValueError("preverified live authorization or card mismatch")
        return
    path = _pre_root(project, run_id) / "live-authorization.json"
    if not path.is_file(): raise ValueError("preverified live authorization is absent")
    artifact = load_json(path); expected = _pre_live_binding(project, run_id, prepare, experiment_card_path, budget_path, pool_path)
    if artifact.get("status") != "AUTHORIZED" or not re.fullmatch(r"[0-9a-f]{40}", str(artifact.get("reviewed_head") or "")) or artifact.get("binding") != expected or not re.fullmatch(r"[0-9a-f]{64}", str(artifact.get("acceptance_sha256") or "")) or artifact.get("fresh_probe_journal") is not True:
        raise ValueError("preverified live authorization mismatch")


def preverified_stage_live(project: Path, run_id: str, destination: Path) -> dict[str, Any]:
    """Copy only an accepted fresh preverified live input set into a new project."""
    source = project.resolve(); dest = destination.resolve()
    if dest.exists(): raise ValueError("preverified stage destination already exists")
    if source == dest or source in dest.parents: raise ValueError("preverified stage destination must stay outside source project")
    if not dest.parent.is_dir(): raise ValueError("preverified stage destination parent is absent")
    root = _pre_root(source, run_id); prepare_path = root / "prepare-report.json"; acceptance_path = root / "acceptance.json"; authorization_path = root / "live-authorization.json"
    if any((root / name).exists() for name in ("preverified-state.json", "probe-reservation.json", "probe-completed.json", "link-probe.json")):
        raise ValueError("preverified stage source has non-fresh live history")
    prepare = load_json(prepare_path); card_path, card = _pre_load_ref(source, prepare.get("experiment_card")); budget_path, _ = _pre_load_ref(source, prepare.get("budget")); plan_path, plan = _pre_load_ref(source, prepare.get("plan"))
    if prepare.get("status") != "PREPARED" or not _pre_requires_separate_acceptance(card) or card_path.resolve() != (root / "experiment-card.json").resolve() or budget_path.resolve() != (root / "budget.json").resolve() or plan_path.resolve() != (root / "preverified-plan.json").resolve():
        raise ValueError("preverified stage canonical input contract mismatch")
    pool_path = _hybrid_safe_path(source, str((plan.get("pool") or {}).get("internal_catalog", {}).get("relative_path") or ""))
    canonical_pool = project_paths(source)["internal"] / "oa-preverified" / run_id / "pool-catalog.json"
    if pool_path.resolve() != canonical_pool.resolve() or not pool_path.is_file() or (plan.get("pool") or {}).get("internal_catalog", {}).get("sha256") != sha256_path(pool_path): raise ValueError("preverified stage pool provenance mismatch")
    acceptance = load_json(acceptance_path); verdicts = [acceptance[key] for key in ("verdict", "implementation_verdict") if key in acceptance]
    binding = _pre_live_binding(source, run_id, prepare, card_path, budget_path, pool_path)
    if acceptance.get("run_id") != run_id or not verdicts or any(value != "PASS" for value in verdicts) or not re.fullmatch(r"[0-9a-f]{40}", str(acceptance.get("reviewed_head") or "")):
        raise ValueError("preverified stage acceptance contract mismatch")
    for key in ("prepare_sha256", "experiment_card_sha256", "budget_sha256", "pool_sha256"):
        if acceptance.get(key) != binding[key]: raise ValueError("preverified stage acceptance hash mismatch")
    authorization = load_json(authorization_path)
    if authorization.get("status") != "AUTHORIZED" or authorization.get("fresh_probe_journal") is not True or authorization.get("binding") != binding or authorization.get("acceptance_sha256") != sha256_path(acceptance_path) or authorization.get("reviewed_head") != acceptance.get("reviewed_head"):
        raise ValueError("preverified stage authorization mismatch")
    result = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"], capture_output=True, text=True, check=False)
    source_head = result.stdout.strip().lower()
    if result.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", source_head): raise ValueError("preverified stage source git head is unavailable")
    source_files = [prepare_path, card_path, budget_path, plan_path, acceptance_path, authorization_path, pool_path]
    before = {relative_to_project(path, source): sha256_path(path) for path in source_files}
    staging = dest.parent / ("." + dest.name + ".stage-" + uuid.uuid4().hex)
    if staging.exists(): raise ValueError("preverified stage temporary destination already exists")
    try:
        for path in source_files:
            relative = Path(relative_to_project(path, source)); target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(path, target)
            if sha256_path(target) != before[relative.as_posix()]: raise ValueError("preverified stage copy hash mismatch")
        manifest = {"schema_version": 1, "type": "OA_PREVERIFIED_LIVE_STAGE", "run_id": run_id, "source_head": source_head, "source_files": [{"relative_path": key, "sha256": value} for key, value in sorted(before.items())], "network_requests": 0, "raw_urls_or_bodies_persisted_outside_internal_assets": False}
        atomic_write_json(staging / "stage-manifest.json", manifest)
        if {relative_to_project(path, source): sha256_path(path) for path in source_files} != before: raise ValueError("preverified stage modified source bytes")
        os.replace(staging, dest)
    except Exception:
        if staging.exists(): shutil.rmtree(staging)
        raise
    return manifest


def preverified_prepare(project: Path, run_id: str, experiment_card_path: Path, h03_catalog_path: Path, h03_probe_path: Path, h05_pool_path: Path, additional_exclusion_paths: Iterable[Path] | None = None) -> dict[str, Any]:
    """Offline H07 pool/A freeze. It never obtains metadata or probes a URL."""
    card = load_json(experiment_card_path)
    if card.get("run_id") != run_id or card.get("phase") != "offline preverified-ranking formulation and prospective cohort preparation":
        raise ValueError("preverified experiment card mismatch")
    expected = card.get("frozen_inputs") or {}
    for label, path in (("h03_catalog_sha256", h03_catalog_path), ("h03_probe_sha256", h03_probe_path), ("h05_pool_sha256", h05_pool_path)):
        if expected.get(label) != sha256_path(path): raise ValueError(f"preverified frozen {label} mismatch")
    additional_paths = list(additional_exclusion_paths or [])
    expected_additional = expected.get("additional_exclusions", [])
    if not isinstance(expected_additional, list) or len(expected_additional) != len(additional_paths):
        raise ValueError("preverified additional exclusion manifest mismatch")
    excluded_probe = {str(row.get("identity_sha256")) for row in (load_json(h03_probe_path).get("results") or []) if isinstance(row, dict)}
    h05_members = load_json(h05_pool_path).get("members") or []; excluded_h05 = {str(row.get("identity_sha256")) for row in h05_members if isinstance(row, dict)}
    if len(excluded_probe) != 30 or len(excluded_h05) != 100: raise ValueError("preverified exclusion population mismatch")
    additional_excluded: set[str] = set(); additional_inputs = []
    for item, path in zip(expected_additional, additional_paths):
        if not isinstance(item, dict) or item.get("sha256") != sha256_path(path):
            raise ValueError("preverified additional exclusion hash mismatch")
        identities, identity_set = _pre_member_envelope_identities(path)
        if identity_set & (excluded_probe | excluded_h05 | additional_excluded):
            raise ValueError("preverified additional exclusion overlaps frozen population")
        additional_excluded.update(identity_set)
        additional_inputs.append({"sha256": sha256_path(path), "identity_count": len(identities), "identity_order_sha256": sha256_bytes(stable_json(identities).encode("utf-8"))})
    excluded = excluded_probe | excluded_h05 | additional_excluded
    unique: dict[str, dict[str, Any]] = {}
    for raw in _catalog_rows(h03_catalog_path):
        try: member = _legacy_member(raw)
        except ValueError: continue
        if member["identity_sha256"] not in excluded: unique.setdefault(member["identity_sha256"], member)
    pool = _select_diversified_pool(list(unique.values()), 100)
    if {row["identity_sha256"] for row in pool} & excluded: raise ValueError("preverified pool exclusion leakage")
    internal = project_paths(project)["internal"] / "oa-preverified" / run_id; internal.mkdir(parents=True, exist_ok=True)
    pool_path = internal / "pool-catalog.json"; atomic_write_json(pool_path, {"schema_version": 1, "contract": PREVERIFIED_RANKING_CONTRACT_VERSION, "members": pool})
    arm_a = []
    for member in sorted(pool, key=_legacy_order_key)[:20]: arm_a.append({"identity_sha256": member["identity_sha256"], "member": member, "content_score": content_score(member), "year": member["year"], "url": member["legacy_url"], "url_sha256": member["legacy_url_sha256"], "hostname": member["legacy_host"], "route_kind": "LEGACY"})
    budget_path = _pre_root(project, run_id) / "budget.json"; atomic_write_json(budget_path, preverified_default_budgets())
    plan_path = _pre_root(project, run_id) / "preverified-plan.json"; plan = {"schema_version": 1, "contract": PREVERIFIED_RANKING_CONTRACT_VERSION, "run_id": run_id, "status": "PREPARED_PRE_METADATA",
        "pool": {"size": 100, "identity_order_sha256": sha256_bytes(stable_json([row["identity_sha256"] for row in pool]).encode("utf-8")), "internal_catalog": _pre_ref(project, pool_path), "excluded_probe_count": len(excluded_probe), "excluded_h05_count": len(excluded_h05), "additional_exclusion_count": len(additional_excluded), "excluded_identity_count": len(excluded)},
        "arms": {"A": {"members": [{"identity_sha256": row["identity_sha256"], "url_sha256": row["url_sha256"]} for row in arm_a], "metrics": _pre_metrics(arm_a)}}, "raw_urls_or_bodies_persisted": False}
    atomic_write_json(plan_path, plan)
    report = {"schema_version": 1, "type": "OA_PREVERIFIED_PREPARE", "run_id": run_id, "contract": PREVERIFIED_RANKING_CONTRACT_VERSION, "status": "PREPARED", "live_authorized": not _pre_requires_separate_acceptance(card), "network_requests": 0, "model_calls": 0, "wakg_accesses": 0,
              "experiment_card": _pre_ref(project, experiment_card_path), "inputs": {label: sha256_path(path) for label, path in (("h03_catalog_sha256", h03_catalog_path), ("h03_probe_sha256", h03_probe_path), ("h05_pool_sha256", h05_pool_path))}, "additional_exclusions": additional_inputs, "plan": _pre_ref(project, plan_path), "budget": _pre_ref(project, budget_path), "raw_urls_or_bodies_persisted": False}
    report_path = _pre_root(project, run_id) / "prepare-report.json"; atomic_write_json(report_path, report); return report


def preverified_failure_envelope(project: Path, run_id: str, command: str, error: Exception) -> dict[str, Any]:
    try: state = _pre_state(project, run_id); counters = state.get("counters") or {}; stage = state.get("stage")
    except Exception: counters = {}; stage = "UNSTARTED"
    report = {"schema_version": 1, "type": "SOURCE_RESULT", "run_id": run_id, "command": command, "verdict": "INVALID", "error_type": type(error).__name__, "failure_fingerprint": "OA_PREVERIFIED_" + sha256_bytes((type(error).__name__ + str(error)).encode("utf-8"))[:16], "last_completed_stage": stage,
              "network_requests": int(counters.get("metadata_requests", 0)) + int(counters.get("link_requests", 0)), "metadata_bytes": int(counters.get("metadata_bytes", 0)), "link_bytes": int(counters.get("link_bytes", 0)), "raw_urls_or_bodies_persisted": False}
    atomic_write_json(_pre_root(project, run_id) / f"{command}-failure.json", report); return report


def _pre_freeze_after_metadata(project: Path, run_id: str, state: dict[str, Any], pool: list[dict[str, Any]], envelope: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    content_rows = _pre_content_rows(pool, envelope); preflight = content_rows[:40]
    if len(preflight) != 40 or len({row["identity_sha256"] for row in preflight}) != 40 or _pre_metrics(preflight)["domain_count"] < 10 or _pre_metrics(preflight)["query_strata_count"] < 6:
        raise ValueError("preflight diversity or identity gate failed")
    a_members = plan.get("arms", {}).get("A", {}).get("members") or []; by_identity = {row["identity_sha256"]: row for row in content_rows}
    arm_a = []
    for item in a_members:
        member = next((row for row in pool if row["identity_sha256"] == item.get("identity_sha256")), None)
        if member is None or item.get("url_sha256") != member["legacy_url_sha256"]: raise ValueError("baseline A provenance mismatch")
        arm_a.append({"identity_sha256": member["identity_sha256"], "member": member, "content_score": content_score(member), "year": member["year"], "url": member["legacy_url"], "url_sha256": member["legacy_url_sha256"], "hostname": member["legacy_host"], "route_kind": "LEGACY"})
    mappings = []
    for role, rows in (("A", arm_a), ("P", preflight)):
        for row in rows: mappings.append({"role": role, "identity_sha256": row["identity_sha256"], "url": row["url"], "url_sha256": row["url_sha256"], "hostname": row["hostname"], "content_score": row["content_score"], "year": row["year"], "query_strata": row["member"]["query_strata"], "route_kind": row["route_kind"]})
    unique = {row["url_sha256"]: row for row in mappings}
    if len(unique) > 60: raise ValueError("preverified union exceeds 60 URLs")
    internal = project_paths(project)["internal"] / "oa-preverified" / run_id; union_path = internal / "union-probe-catalog.json"
    atomic_write_json(union_path, {"schema_version": 1, "contract": PREVERIFIED_RANKING_CONTRACT_VERSION, "mappings": mappings, "candidates": [{"fulltext_url": row["url"], "score": row["content_score"], "query_strata": row["query_strata"], "doi": row["identity_sha256"]} for _, row in sorted(unique.items())]})
    frozen_path = _pre_root(project, run_id) / "preflight-manifest.json"; frozen = {"schema_version": 1, "contract": PREVERIFIED_RANKING_CONTRACT_VERSION, "run_id": run_id, "status": "FROZEN_PRE_PROBE", "inputs": {"plan_sha256": state["artifacts"]["plan"]["sha256"], "metadata_sha256": state["artifacts"]["metadata_envelope"]["sha256"]}, "roles": {"A": {"members": [{"identity_sha256": row["identity_sha256"], "url_sha256": row["url_sha256"]} for row in arm_a], "metrics": _pre_metrics(arm_a)}, "P": {"members": [{"identity_sha256": row["identity_sha256"], "url_sha256": row["url_sha256"], "route_kind": row["route_kind"]} for row in preflight], "metrics": _pre_metrics(preflight)}}, "union": {"mapping_count": len(mappings), "unique_url_count": len(unique), "internal_catalog": _pre_ref(project, union_path)}, "raw_urls_or_bodies_persisted": False}; atomic_write_json(frozen_path, frozen)
    return {"preflight": _pre_ref(project, frozen_path), "union": _pre_ref(project, union_path)}


def _pre_summary(project: Path, state: dict[str, Any]) -> dict[str, Any]:
    _, frozen = _pre_load_ref(project, state["artifacts"].get("preflight")); _, union = _pre_load_ref(project, state["artifacts"].get("union")); _, probe = _pre_load_ref(project, state["artifacts"].get("probe")); _, pool_blob = _pre_load_ref(project, state["artifacts"].get("pool"))
    mappings = union.get("mappings") or []; results = probe.get("results") or []; result_by_url = {str(row.get("url_sha256")): row for row in results if isinstance(row, dict)}; unique = {str(row.get("url_sha256")) for row in mappings if isinstance(row, dict)}
    if len(result_by_url) != len(results) or set(result_by_url) != unique: raise ValueError("preverified probe mapping mismatch")
    roles = frozen.get("roles") or {}; a = roles.get("A") or {}; p = roles.get("P") or {}
    if len(a.get("members") or []) != 20 or len(p.get("members") or []) != 40: raise ValueError("preverified frozen role size mismatch")
    verified_classes = {"VERIFIED_PDF_CONTENT_TYPE", "VERIFIED_PDF_MAGIC"}
    p_verified_ids = {row["identity_sha256"] for row in p["members"] if result_by_url[str(row["url_sha256"])].get("classification") in verified_classes}
    # Build V from P's frozen metrics/mappings, never from unverified candidates.
    mapping_by_identity = {str(row.get("identity_sha256")): row for row in mappings if row.get("role") == "P"}
    eligible_v = [row for identity, row in mapping_by_identity.items() if identity in p_verified_ids]
    eligible_v.sort(key=lambda row: (-int(row["content_score"]), -int(row["year"]), row["identity_sha256"])); v = eligible_v[:20] if len(eligible_v) >= 20 else []
    a_verified = sum(result_by_url[str(row["url_sha256"])].get("classification") in verified_classes for row in a["members"])
    members_by_identity = {row["identity_sha256"]: row for row in (pool_blob.get("members") or []) if isinstance(row, dict)}
    if len(members_by_identity) != 100: raise ValueError("preverified internal pool is malformed")
    p_verified = len(p_verified_ids); v_metrics = {"identity_count": len(v), "mean_content_score": (sum(row["content_score"] for row in v) / len(v)) if v else None, "mix_macroscopic_matched_count": sum(any(item.get("criterion") == "MIX and macroscopic performance" and bool(item.get("matched")) for item in members_by_identity[row["identity_sha256"]]["score_explanation"] if isinstance(item, dict)) for row in v), "domain_count": len({row["hostname"] for row in v}), "query_strata_count": len({item for row in v for item in row["query_strata"]}), "member_order_sha256": sha256_bytes(stable_json([{"identity_sha256": row["identity_sha256"], "url_sha256": row["url_sha256"]} for row in v]).encode("utf-8"))}
    requests = int(probe.get("actual_requests") or 0); bytes_used = int(probe.get("bytes_read") or 0); wall = int(probe.get("latency_ms") or 0) / 1000; caps = state["budgets"]["probe"]
    effective_cap = int(state.get("effective_probe_request_cap", caps["requests_max"]))
    if len(unique) > caps["unique_urls_max"] or requests > caps["requests_max"] or requests > effective_cap or bytes_used > len(unique) * caps["range_read_max"] or wall > caps["wall_seconds_max"]: raise ValueError("preverified probe budget exceeded")
    available = len(eligible_v) >= 20; guards = len(v) == 20 and v_metrics["mean_content_score"] >= a["metrics"]["mean_content_score"] - 3 and v_metrics["mix_macroscopic_matched_count"] >= a["metrics"]["mix_macroscopic_matched_count"] - 2 and v_metrics["domain_count"] > 5 and v_metrics["query_strata_count"] > 4
    a_ratio = a_verified / 20; v_ratio = 1.0 if available else None; delta = (v_ratio - a_ratio) if available else None; efficiency = (requests / len(v)) if available else None
    valid = (all(result_by_url[row["url_sha256"]].get("classification") in verified_classes for row in v) and (guards if available else True))
    if not available: verdict = "FAIL"
    elif not valid: verdict = "INVALID"
    elif v_ratio - a_ratio > .20 and efficiency is not None and efficiency <= 5.0: verdict = "PASS"
    elif (v_ratio - a_ratio > .20) != (efficiency is not None and efficiency <= 5.0): verdict = "PARTIAL"
    else: verdict = "FAIL"
    return {"schema_version": 1, "type": "SOURCE_RESULT", "contract": PREVERIFIED_RANKING_CONTRACT_VERSION, "run_id": state["run_id"], "verdict": verdict, "valid": valid, "failure_reason": "INSUFFICIENT_PREVERIFIED_PDF_COUNT" if not available else None, "roles": {"A": {**a["metrics"], "verified_pdf_count": a_verified, "verified_pdf_ratio": a_ratio}, "P": {**p["metrics"], "verified_pdf_count": p_verified, "preflight_verified_ratio": p_verified / 40}, "V": {"available": available, "members": [{"identity_sha256": row["identity_sha256"], "url_sha256": row["url_sha256"]} for row in v], **v_metrics, "eligible_verified_count": len(eligible_v), "verified_pdf_count": len(v), "verified_pdf_ratio": v_ratio}}, "primary": {"v_minus_a": delta, "link_attempts_per_v_accepted": efficiency, "pass_conditions": {"delta_gt_020": delta is not None and delta > .20, "efficiency_lte_5": efficiency is not None and efficiency <= 5.0}}, "accounting": {"metadata_requests": state["counters"]["metadata_requests"], "metadata_bytes": state["counters"]["metadata_bytes"], "replay_requests": 0, "unique_urls": len(unique), "effective_probe_request_cap": effective_cap, "link_requests": requests, "link_bytes": bytes_used, "link_wall_seconds": wall}, "raw_urls_or_bodies_persisted": False, "model_calls": 0, "wakg_accesses": 0}


def _pre_recompute_frozen(project: Path, state: dict[str, Any]) -> None:
    """Independent P/A/union reconstruction proves the probe could not choose them."""
    _, plan = _pre_load_ref(project, state["artifacts"].get("plan")); _, frozen = _pre_load_ref(project, state["artifacts"].get("preflight")); _, union = _pre_load_ref(project, state["artifacts"].get("union")); _, pool_blob = _pre_load_ref(project, state["artifacts"].get("pool")); _, envelope = _pre_load_ref(project, state["artifacts"].get("metadata_envelope"))
    pool = pool_blob.get("members") or []; rows = _pre_content_rows(pool, envelope); expected_p = rows[:40]
    expected_a = []
    for member in sorted(pool, key=_legacy_order_key)[:20]: expected_a.append({"identity_sha256": member["identity_sha256"], "member": member, "content_score": content_score(member), "year": member["year"], "url": member["legacy_url"], "url_sha256": member["legacy_url_sha256"], "hostname": member["legacy_host"], "route_kind": "LEGACY"})
    for role, expected in (("A", expected_a), ("P", expected_p)):
        actual = (frozen.get("roles") or {}).get(role, {})
        expected_members = [{"identity_sha256": row["identity_sha256"], "url_sha256": row["url_sha256"]} if role == "A" else {"identity_sha256": row["identity_sha256"], "url_sha256": row["url_sha256"], "route_kind": row["route_kind"]} for row in expected]
        if stable_json(actual.get("members")) != stable_json(expected_members) or stable_json(actual.get("metrics")) != stable_json(_pre_metrics(expected)):
            raise ValueError("preverified outcome leakage or frozen role mismatch")
    expected_pairs = {(role,row["identity_sha256"],row["url_sha256"]) for role,items in (("A",expected_a),("P",expected_p)) for row in items}; observed_pairs = {(str(row.get("role")),str(row.get("identity_sha256")),str(row.get("url_sha256"))) for row in (union.get("mappings") or []) if isinstance(row,dict)}
    if observed_pairs != expected_pairs or len(observed_pairs) != 60: raise ValueError("preverified union mapping recomputation mismatch")


def preverified_live_controller(project: Path, run_id: str, experiment_card_path: Path, budget_path: Path, metadata_fetcher: Any = None, probe_runner: Any = None, fail_after: str | None = None) -> dict[str, Any]:
    """One-request, one-probe, resumable H07 controller; callers must opt in at the driver."""
    root = _pre_root(project, run_id); prepare_path = root / "prepare-report.json"; prepare = load_json(prepare_path); card = load_json(experiment_card_path); budgets = _pre_budget(load_json(budget_path))
    if prepare.get("status") != "PREPARED" or prepare.get("experiment_card", {}).get("sha256") != sha256_path(experiment_card_path) or card.get("run_id") != run_id: raise ValueError("preverified live authorization or card mismatch")
    plan_path, plan = _pre_load_ref(project, prepare.get("plan")); pool_path = _hybrid_safe_path(project, str((plan.get("pool") or {}).get("internal_catalog", {}).get("relative_path") or ""))
    if not pool_path.is_file() or plan["pool"]["internal_catalog"].get("sha256") != sha256_path(pool_path): raise ValueError("preverified pool provenance mismatch")
    _pre_require_live_authorization(project, run_id, prepare, card, experiment_card_path, budget_path, pool_path)
    pool = load_json(pool_path).get("members") or []
    if len(pool) != 100 or len({row.get("identity_sha256") for row in pool}) != 100: raise ValueError("preverified pool must contain exactly 100 identities")
    if _pre_state_path(project, run_id).is_file():
        state = _pre_state(project, run_id)
        if state.get("prepare_sha256") != sha256_path(prepare_path) or state.get("budget_sha256") != sha256_path(budget_path): raise ValueError("preverified resume inputs changed")
    else:
        state = {"schema_version": 1, "contract": PREVERIFIED_RANKING_CONTRACT_VERSION, "run_id": run_id, "stage": "PREPARED", "prepare_sha256": sha256_path(prepare_path), "budget_sha256": sha256_path(budget_path), "card_sha256": sha256_path(experiment_card_path), "budgets": budgets, "idempotency_keys": {"metadata": sha256_bytes(stable_json([run_id, "metadata", sha256_path(pool_path), sha256_path(budget_path)]).encode("utf-8")), "probe": sha256_bytes(stable_json([run_id, "probe", sha256_path(pool_path), sha256_path(budget_path)]).encode("utf-8"))}, "artifacts": {"plan": _pre_ref(project, plan_path), "pool": _pre_ref(project, pool_path), "card": _pre_ref(project, experiment_card_path), "budget": _pre_ref(project, budget_path)}, "counters": {"metadata_requests": 0, "metadata_bytes": 0, "link_requests": 0, "link_bytes": 0, "model_calls": 0, "wakg_accesses": 0}}; _pre_write_state(project, run_id, state)
    internal = project_paths(project)["internal"] / "oa-preverified" / run_id
    if state["stage"] == "PREPARED":
        payload, receipt = (metadata_fetcher or benchmark_fetch_json)(project, run_id, "openalex", "h07-doi-or", 1, hybrid_doi_or_url(pool), timeout=budgets["metadata"]["timeout_seconds"], attempt_budget=1, raw_byte_cap=budgets["metadata"]["bytes_max"])
        envelope = validate_openalex_envelope(payload); observed = {normalize_doi(row.get("doi")) for row in envelope.get("results", []) if normalize_doi(row.get("doi"))}; attempts = int(receipt.get("actual_http_attempts") or 0); raw_bytes = int(receipt.get("raw_network_bytes", receipt.get("bytes", 0)) or 0)
        if len(envelope.get("results") or []) != 100 or observed != {row["doi"] for row in pool} or attempts > 1 or raw_bytes > budgets["metadata"]["bytes_max"]: raise ValueError("preverified metadata evidence or budget mismatch")
        envelope_path = internal / "metadata-envelope.json"; receipt_path = internal / "metadata-receipt.json"; atomic_write_json(envelope_path, envelope); atomic_write_json(receipt_path, receipt); state["artifacts"].update({"metadata_envelope": _pre_ref(project, envelope_path), "metadata_receipt": _pre_ref(project, receipt_path)}); state["counters"].update({"metadata_requests": attempts, "metadata_bytes": raw_bytes}); state["stage"] = "METADATA_CACHED"; _pre_write_state(project, run_id, state)
        if fail_after == "METADATA_CACHED": raise RuntimeError("injected crash after metadata")
    if state["stage"] == "METADATA_CACHED":
        _, envelope = _pre_load_ref(project, state["artifacts"].get("metadata_envelope")); state["artifacts"].update(_pre_freeze_after_metadata(project, run_id, state, pool, envelope, plan)); state["stage"] = "ARMS_FROZEN"; _pre_write_state(project, run_id, state)
        if fail_after == "ARMS_FROZEN": raise RuntimeError("injected crash after preflight freeze")
    if state["stage"] == "ARMS_FROZEN":
        union_path, union = _pre_load_ref(project, state["artifacts"].get("union")); count = len(union.get("candidates") or []); byte_cap, effective_cap = preverified_probe_adapter(budgets["probe"], count)
        reservation_path = root / "probe-reservation.json"; completed_path = root / "probe-completed.json"; probe_path = root / "link-probe.json"
        binding = {"run_id": run_id, "pool_sha256": state["artifacts"]["pool"]["sha256"], "preflight_sha256": state["artifacts"]["preflight"]["sha256"], "union_sha256": state["artifacts"]["union"]["sha256"], "budget_sha256": state["budget_sha256"], "idempotency_key": state["idempotency_keys"]["probe"]}
        decision = preverified_probe_journal_decision(reservation_path, completed_path, probe_path, binding)
        if decision == "ADOPT":
            completed = load_json(completed_path)
            if completed.get("binding") != binding or not probe_path.is_file() or completed.get("probe_sha256") != sha256_path(probe_path): raise ValueError("BLOCKED_UNCERTAIN_PROBE")
            counters = completed.get("counters") or {}
            state["artifacts"]["probe"] = _pre_ref(project, probe_path); state["effective_probe_request_cap"] = int(completed.get("effective_request_cap")); state["counters"].update({"link_requests":int(counters.get("requests") or 0),"link_bytes":int(counters.get("bytes") or 0)}); state["stage"] = "PROBED"; _pre_write_state(project, run_id, state)
        else:
            state["effective_probe_request_cap"] = effective_cap
            probe = (probe_runner or probe_links)(project, run_id, catalog=union_path, probe_limit=count, byte_cap=byte_cap, timeout=budgets["probe"]["timeout_seconds"], max_wall_seconds=budgets["probe"]["wall_seconds_max"], max_requests=effective_cap)
            atomic_write_json(probe_path, probe)
            atomic_write_json(completed_path, {"schema_version":1,"status":"COMPLETED","binding":binding,"probe_sha256":sha256_path(probe_path),"counters":{"requests":int(probe.get("actual_requests") or 0),"bytes":int(probe.get("bytes_read") or 0)},"effective_request_cap":effective_cap})
            state["artifacts"]["probe"] = _pre_ref(project, probe_path); state["counters"].update({"link_requests": int(probe.get("actual_requests") or 0), "link_bytes": int(probe.get("bytes_read") or 0)}); state["stage"] = "PROBED"; _pre_write_state(project, run_id, state)
        if state["stage"] == "PROBED":
            if fail_after == "PROBED": raise RuntimeError("injected crash after probe")
    if state["stage"] == "PROBED":
        result = _pre_summary(project, state); result_path = root / "source-result.json"; atomic_write_json(result_path, result); state["artifacts"]["summary"] = _pre_ref(project, result_path); state["stage"] = "SUMMARIZED"; _pre_write_state(project, run_id, state)
    if state["stage"] == "SUMMARIZED": state = preverified_replay_controller(project, run_id)
    if state["stage"] == "REPLAYED": state = preverified_verify_controller(project, run_id)
    _, summary = _pre_load_ref(project, state["artifacts"].get("summary"))
    return {**summary, "controller_stage": state["stage"], "verification": state["artifacts"].get("verify")}


def preverified_replay_controller(project: Path, run_id: str) -> dict[str, Any]:
    state = _pre_state(project, run_id)
    if state["stage"] == "VERIFIED":
        _pre_load_ref(project, state["artifacts"].get("summary")); _pre_load_ref(project, state["artifacts"].get("replay")); return state
    if PREVERIFIED_LIVE_STAGES.index(state["stage"]) < PREVERIFIED_LIVE_STAGES.index("SUMMARIZED"): raise ValueError("preverified replay requires summary")
    _, prior = _pre_load_ref(project, state["artifacts"].get("summary")); _, receipt = _pre_load_ref(project, state["artifacts"].get("metadata_receipt")); _pre_load_ref(project, state["artifacts"].get("metadata_envelope"))
    if int(receipt.get("actual_http_attempts") or 0) > 1: raise ValueError("preverified cached receipt exceeds one request")
    _pre_recompute_frozen(project, state)
    replayed = _pre_summary(project, state)
    if stable_json(replayed) != stable_json(prior): raise ValueError("preverified replay differs from live summary")
    proof_path = _pre_root(project, run_id) / "replay-proof.json"; atomic_write_json(proof_path, {"schema_version": 1, "run_id": run_id, "network_requests": 0, "summary_sha256": sha256_bytes(stable_json(replayed).encode("utf-8")), "raw_urls_or_bodies_persisted": False}); state["artifacts"]["replay"] = _pre_ref(project, proof_path); state["stage"] = "REPLAYED"; _pre_write_state(project, run_id, state); return state


def preverified_verify_controller(project: Path, run_id: str) -> dict[str, Any]:
    state = _pre_state(project, run_id)
    if PREVERIFIED_LIVE_STAGES.index(state["stage"]) < PREVERIFIED_LIVE_STAGES.index("REPLAYED"): raise ValueError("preverified verify requires replay")
    _, budget = _pre_load_ref(project, state["artifacts"].get("budget")); _, card = _pre_load_ref(project, state["artifacts"].get("card")); _pre_budget(budget)
    if card.get("run_id") != run_id or state.get("budget_sha256") != state["artifacts"]["budget"]["sha256"] or state["counters"].get("model_calls") != 0 or state["counters"].get("wakg_accesses") != 0: raise ValueError("preverified immutable input or forbidden call mismatch")
    _pre_recompute_frozen(project, state)
    _, prior = _pre_load_ref(project, state["artifacts"].get("summary")); _, replay = _pre_load_ref(project, state["artifacts"].get("replay")); recomputed = _pre_summary(project, state)
    if stable_json(prior) != stable_json(recomputed) or replay.get("network_requests") != 0 or replay.get("summary_sha256") != sha256_bytes(stable_json(recomputed).encode("utf-8")): raise ValueError("preverified independent recomputation mismatch")
    out = {"schema_version": 1, "type": "OA_PREVERIFIED_VERIFY", "run_id": run_id, "status": "VERIFIED", "verdict": recomputed["verdict"], "source_result_sha256": sha256_bytes(stable_json(recomputed).encode("utf-8")), "network_requests": 0, "raw_urls_or_bodies_persisted": False}; path = _pre_root(project, run_id) / "verify-report.json"; atomic_write_json(path, out); state["artifacts"]["verify"] = _pre_ref(project, path); state["stage"] = "VERIFIED"; _pre_write_state(project, run_id, state); return state


PREVERIFIED_PUBLISH_ARTIFACTS = ("preflight", "probe_reservation", "probe_completed", "probe", "summary", "replay", "verify", "state", "watermark")


def _pre_assert_body_free(value: Any, label: str = "artifact") -> None:
    """Reject transport bodies and raw URLs before canonical publication."""
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in {"url", "pdf_url", "fulltext_url", "raw_url", "body", "payload", "response_body"}:
                raise ValueError(f"preverified publish raw URL/body field in {label}")
            _pre_assert_body_free(item, label)
    elif isinstance(value, list):
        for item in value: _pre_assert_body_free(item, label)
    elif isinstance(value, str) and "://" in value:
        raise ValueError(f"preverified publish raw URL/body value in {label}")


def _pre_publish_ref(project: Path, path: Path, label: str) -> dict[str, Any]:
    if not path.is_file(): raise ValueError(f"preverified publish {label} is absent")
    value = load_json(path); _pre_assert_body_free(value, label)
    return {"relative_path": relative_to_project(path, project), "sha256": sha256_path(path), "bytes": path.stat().st_size}


def preverified_publish_live(project: Path, run_id: str, staged_project: Path) -> dict[str, Any]:
    """Zero-network, add-only import of a completed isolated execution."""
    canonical, staged = project.resolve(), staged_project.resolve(); root, staged_root = _pre_root(canonical, run_id), _pre_root(staged, run_id)
    if canonical == staged or canonical in staged.parents: raise ValueError("preverified publish staged project must be isolated")
    manifest_path = staged / "stage-manifest.json"; manifest = load_json(manifest_path)
    if manifest.get("type") != "OA_PREVERIFIED_LIVE_STAGE" or manifest.get("run_id") != run_id or manifest.get("network_requests") != 0: raise ValueError("preverified publish stage manifest mismatch")
    source_files = manifest.get("source_files")
    if not isinstance(source_files, list) or len(source_files) != 7: raise ValueError("preverified publish stage source-file contract mismatch")
    seen: set[str] = set()
    for item in source_files:
        if not isinstance(item, dict) or not isinstance(item.get("relative_path"), str) or not re.fullmatch(r"[0-9a-f]{64}", str(item.get("sha256") or "")): raise ValueError("preverified publish stage source-file entry malformed")
        relative = item["relative_path"]
        if relative in seen: raise ValueError("preverified publish duplicate staged source file")
        seen.add(relative); staged_path, canonical_path = _hybrid_safe_path(staged, relative), _hybrid_safe_path(canonical, relative)
        if not staged_path.is_file() or not canonical_path.is_file() or sha256_path(staged_path) != item["sha256"] or sha256_path(canonical_path) != item["sha256"]: raise ValueError("preverified publish staged/canonical source hash mismatch")
    expected_inputs = {f"runs/{run_id}/{name}" for name in ("experiment-card.json", "budget.json", "prepare-report.json", "preverified-plan.json", "acceptance.json", "live-authorization.json")} | {f"internal_assets/oa-preverified/{run_id}/pool-catalog.json"}
    if seen != expected_inputs: raise ValueError("preverified publish stage source-file set mismatch")
    state = _pre_state(staged, run_id); watermark = load_json(_pre_watermark_path(staged, run_id))
    if state.get("stage") != "VERIFIED" or watermark.get("stage") != "VERIFIED": raise ValueError("preverified publish requires VERIFIED staged state")
    counters, budgets = state.get("counters") or {}, _pre_budget(state.get("budgets"))
    if int(counters.get("metadata_requests") or 0) > 1 or int(counters.get("link_requests") or 0) > int(state.get("effective_probe_request_cap") or -1) or int(counters.get("model_calls") or 0) != 0 or int(counters.get("wakg_accesses") or 0) != 0: raise ValueError("preverified publish counter/budget mismatch")
    required = ("plan", "pool", "card", "budget", "metadata_envelope", "metadata_receipt", "preflight", "union", "probe", "summary", "replay", "verify")
    refs = {key: _pre_load_ref(staged, state.get("artifacts", {}).get(key)) for key in required}
    reservation_path, completed_path, probe_path = staged_root / "probe-reservation.json", staged_root / "probe-completed.json", refs["probe"][0]
    reservation, completed = load_json(reservation_path), load_json(completed_path)
    binding = {"run_id": run_id, "pool_sha256": state["artifacts"]["pool"]["sha256"], "preflight_sha256": state["artifacts"]["preflight"]["sha256"], "union_sha256": state["artifacts"]["union"]["sha256"], "budget_sha256": state["budget_sha256"], "idempotency_key": state["idempotency_keys"]["probe"]}
    if reservation.get("binding") != binding or reservation.get("status") != "STARTED" or completed.get("binding") != binding or completed.get("status") != "COMPLETED" or completed.get("probe_sha256") != sha256_path(probe_path): raise ValueError("preverified publish probe reservation/completion binding mismatch")
    if int(completed.get("counters", {}).get("requests") or 0) != int(counters.get("link_requests") or 0) or int(completed.get("counters", {}).get("bytes") or 0) != int(counters.get("link_bytes") or 0) or int(completed.get("effective_request_cap") or -1) != int(state.get("effective_probe_request_cap") or -2): raise ValueError("preverified publish probe counter binding mismatch")
    _pre_assert_body_free(refs["summary"][1], "source-result"); _pre_recompute_frozen(staged, state); recomputed, summary = _pre_summary(staged, state), refs["summary"][1]; semantic_sha = sha256_bytes(stable_json(recomputed).encode("utf-8"))
    if stable_json(recomputed) != stable_json(summary) or refs["replay"][1].get("summary_sha256") != semantic_sha or refs["verify"][1].get("source_result_sha256") != semantic_sha or refs["verify"][1].get("status") != "VERIFIED": raise ValueError("preverified publish deterministic summary/replay/verify mismatch")
    source_map = {"preflight": refs["preflight"][0], "probe_reservation": reservation_path, "probe_completed": completed_path, "probe": probe_path, "summary": refs["summary"][0], "replay": refs["replay"][0], "verify": refs["verify"][0], "state": _pre_state_path(staged, run_id), "watermark": _pre_watermark_path(staged, run_id)}
    target_map = {"preflight": root / "preflight-manifest.json", "probe_reservation": root / "probe-reservation.json", "probe_completed": root / "probe-completed.json", "probe": root / "link-probe.json", "summary": root / "source-result.json", "replay": root / "replay-proof.json", "verify": root / "verify-report.json", "state": root / "preverified-state.json", "watermark": root / "preverified-stage-watermark.json"}
    publication = {"stage_manifest": _pre_publish_ref(staged, manifest_path, "stage-manifest")}
    for key in PREVERIFIED_PUBLISH_ARTIFACTS: publication[key] = _pre_publish_ref(staged, source_map[key], key)
    receipt_path, envelope_path = refs["metadata_receipt"][0], refs["metadata_envelope"][0]
    receipt = refs["metadata_receipt"][1]; payload_relative = str(receipt.get("payload_path") or "")
    payload_sha, payload_bytes = None, 0
    if payload_relative:
        payload_path = _hybrid_safe_path(staged, payload_relative)
        if not payload_path.is_file() or receipt.get("payload_sha256") != sha256_path(payload_path): raise ValueError("preverified publish metadata payload provenance mismatch")
        payload_sha, payload_bytes = sha256_path(payload_path), payload_path.stat().st_size
    probe_results = refs["probe"][1].get("results") or []
    reachable_count = sum(1 for row in probe_results if isinstance(row, dict) and (str(row.get("classification") or "").startswith("VERIFIED_PDF_") or row.get("classification") == "REACHABLE_NON_PDF"))
    unique_urls = int(summary["accounting"]["unique_urls"])
    report = {"schema_version": 1, "type": "OA_PREVERIFIED_LIVE_PUBLICATION", "run_id": run_id, "status": "PUBLISHED", "verdict": summary["verdict"], "valid_execution": True, "source_head": manifest.get("source_head"), "stage_manifest_sha256": sha256_path(manifest_path), "source_result_semantic_sha256": semantic_sha, "source_result_file_sha256": sha256_path(refs["summary"][0]), "probe_sha256": sha256_path(probe_path), "execution": {"a_verified_pdf_count": summary["roles"]["A"]["verified_pdf_count"], "a_identity_count": summary["roles"]["A"]["identity_count"], "p_verified_pdf_count": summary["roles"]["P"]["verified_pdf_count"], "p_identity_count": summary["roles"]["P"]["identity_count"], "v_available": summary["roles"]["V"]["available"], "v_eligible_verified_count": summary["roles"]["V"]["eligible_verified_count"], "v_verified_pdf_count": summary["roles"]["V"]["verified_pdf_count"], "delta": summary["primary"]["v_minus_a"], "efficiency": summary["primary"]["link_attempts_per_v_accepted"], "reachable_count": reachable_count, "reachable_ratio": round(reachable_count / unique_urls, 10), "metadata_requests": counters["metadata_requests"], "metadata_bytes": counters["metadata_bytes"], "link_requests": counters["link_requests"], "link_bytes": counters["link_bytes"], "link_wall_seconds": summary["accounting"]["link_wall_seconds"], "unique_urls": unique_urls, "model_calls": counters["model_calls"], "wakg_accesses": counters["wakg_accesses"]}, "internal_only": {"metadata_payload_sha256": payload_sha, "metadata_payload_bytes": payload_bytes, "metadata_envelope_sha256": sha256_path(envelope_path), "metadata_envelope_bytes": envelope_path.stat().st_size, "metadata_receipt_sha256": sha256_path(receipt_path), "metadata_receipt_bytes": receipt_path.stat().st_size, "union_sha256": sha256_path(refs["union"][0]), "union_bytes": refs["union"][0].stat().st_size}, "published_artifacts": publication, "raw_urls_or_bodies_persisted": False, "performance_improvement_claim": None}
    _pre_assert_body_free(report, "live-report")
    writes = [(manifest_path, root / "stage-manifest.json")] + [(source_map[key], target_map[key]) for key in PREVERIFIED_PUBLISH_ARTIFACTS]; report_path, report_bytes = root / "live-report.json", stable_json(report).encode("utf-8") + b"\n"
    for source_path, target_path in writes:
        if target_path.exists() and sha256_path(target_path) != sha256_path(source_path): raise ValueError("preverified publish canonical target hash mismatch")
    if report_path.exists() and report_path.read_bytes() != report_bytes: raise ValueError("preverified publish canonical live-report hash mismatch")
    published = False
    for source_path, target_path in writes:
        if not target_path.exists(): atomic_write_bytes(target_path, source_path.read_bytes()); published = True
    if not report_path.exists(): atomic_write_bytes(report_path, report_bytes); published = True
    return {"schema_version": 1, "run_id": run_id, "status": "PUBLISHED", "published": published, "verdict": summary["verdict"], "live_report": _pre_ref(canonical, report_path)}


def preverified_reconcile_summary(project: Path, run_id: str, old_summary_sha256: str, probe_sha256: str) -> dict[str, Any]:
    """Zero-network replacement of one known-invalid derived summary only."""
    state = _pre_state(project, run_id)
    if state.get("stage") != "SUMMARIZED" or state.get("artifacts", {}).get("summary", {}).get("sha256") != old_summary_sha256 or state.get("artifacts", {}).get("probe", {}).get("sha256") != probe_sha256:
        raise ValueError("preverified reconcile state or expected hashes mismatch")
    old_path, _ = _pre_load_ref(project, state["artifacts"]["summary"]); history = _pre_root(project, run_id) / "invalid-history" / f"source-result-{old_summary_sha256}.json"; atomic_write_json(history, load_json(old_path))
    state["artifacts"].pop("replay", None); state["artifacts"].pop("verify", None)
    result = _pre_summary(project, state); new_path = _pre_root(project, run_id) / "source-result.json"; atomic_write_json(new_path, result); state["artifacts"]["summary"] = _pre_ref(project, new_path); state["stage"] = "SUMMARIZED"; _pre_write_state(project, run_id, state)
    state = preverified_replay_controller(project, run_id); state = preverified_verify_controller(project, run_id)
    receipt = {"schema_version":1,"run_id":run_id,"old_summary_sha256":old_summary_sha256,"old_history":_pre_ref(project,history),"probe_sha256":probe_sha256,"network_requests":0,"link_requests":0,"new_summary":state["artifacts"]["summary"],"replay":state["artifacts"]["replay"],"verify":state["artifacts"]["verify"]}; path=_pre_root(project,run_id)/"summary-repair-receipt.json"; atomic_write_json(path,receipt); return receipt


def _member_order_sha256(members: Iterable[dict[str, Any]]) -> str:
    return sha256_bytes(stable_json([
        {"identity_sha256": row["identity_sha256"], "url_sha256": row["url_sha256"]} for row in members
    ]).encode("utf-8"))


def _select_diversified_pool(members: list[dict[str, Any]], pool_size: int) -> list[dict[str, Any]]:
    """Greedily balance legacy hosts and query strata with stable semantic tie breaks."""
    if len(members) < pool_size:
        raise ValueError(f"INSUFFICIENT_POOL_COVERAGE: {len(members)} eligible candidates for requested pool {pool_size}")
    remaining = sorted(members, key=_legacy_order_key)
    selected: list[dict[str, Any]] = []
    host_count: dict[str, int] = {}
    stratum_count: dict[str, int] = {}
    while remaining and len(selected) < pool_size:
        def diversity_key(member: dict[str, Any]) -> tuple[Any, ...]:
            return (
                host_count.get(member["legacy_host"], 0),
                min(stratum_count.get(stratum, 0) for stratum in member["query_strata"]),
                _legacy_order_key(member),
            )
        chosen = min(remaining, key=diversity_key)
        remaining.remove(chosen)
        selected.append(chosen)
        host_count[chosen["legacy_host"]] = host_count.get(chosen["legacy_host"], 0) + 1
        for stratum in chosen["query_strata"]:
            stratum_count[stratum] = stratum_count.get(stratum, 0) + 1
    return selected


def oa_rank_ab_pool(
    project: Path, run_id: str, catalog_path: Path, exclude_probe_path: Path, pool_size: int = 100,
    required_catalog_sha256: str | None = None,
) -> dict[str, Any]:
    """Freeze a diversified prospective pool offline; this function never fetches metadata."""
    if pool_size != 100:
        raise ValueError("prospective route-aware canary requires pool_size=100")
    catalog_hash = sha256_path(catalog_path)
    if required_catalog_sha256 and catalog_hash != _require_sha256(required_catalog_sha256, "required catalog SHA256"):
        raise ValueError("frozen catalog SHA256 mismatch")
    probe = load_json(exclude_probe_path)
    probe_rows = probe.get("results") if isinstance(probe, dict) else None
    if not isinstance(probe_rows, list) or len(probe_rows) != 30:
        raise ValueError("exclude probe must contain exactly 30 H03 outcomes")
    excluded = [str(row.get("identity_sha256") or "") for row in probe_rows if isinstance(row, dict)]
    if len(excluded) != 30 or len(set(excluded)) != 30 or any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in excluded):
        raise ValueError("exclude probe identity contract mismatch")
    excluded_set = set(excluded)
    deduped: dict[str, dict[str, Any]] = {}
    for candidate in _catalog_rows(catalog_path):
        try:
            member = _legacy_member(candidate)
        except ValueError:
            continue
        if member["identity_sha256"] in excluded_set:
            continue
        prior = deduped.get(member["identity_sha256"])
        if prior is None or _legacy_order_key(member) < _legacy_order_key(prior):
            deduped[member["identity_sha256"]] = member
    pool = _select_diversified_pool(list(deduped.values()), pool_size)
    pool_identity_order_sha256 = sha256_bytes(stable_json([row["identity_sha256"] for row in pool]).encode("utf-8"))
    request_url = openalex_batch_route_url(member["doi"] for member in pool)
    paths = project_paths(project)
    internal_path = paths["internal"] / "oa-rank-ab" / run_id / "pool-catalog.json"
    atomic_write_json(internal_path, {"schema_version": 1, "contract": OA_RANK_AB_CONTRACT_VERSION, "members": pool})
    manifest = {
        "schema_version": 1, "type": "OA_RANK_AB_POOL", "run_id": run_id, "contract": OA_RANK_AB_CONTRACT_VERSION,
        "offline_only": True, "network_requests": 0, "wakg_accesses": 0, "model_calls": 0,
        "inputs": {"catalog_sha256": catalog_hash, "exclude_probe_sha256": sha256_path(exclude_probe_path), "excluded_identity_count": 30},
        "pool": {"size": 100, "eligible_after_exclusion": len(deduped), "identity_order_sha256": pool_identity_order_sha256,
                 "doi_count": 100, "legacy_url_count": len({row["legacy_url_sha256"] for row in pool}),
                 "host_count": len({row["legacy_host"] for row in pool}), "query_strata": sorted({stratum for row in pool for stratum in row["query_strata"]}),
                 "score": {"min": min(row["score"] for row in pool), "max": max(row["score"] for row in pool), "mean": sum(row["score"] for row in pool) / len(pool)},
                 "mix_macroscopic_matched_count": sum(any(item.get("criterion") == "MIX and macroscopic performance" and bool(item.get("matched")) for item in row["score_explanation"] if isinstance(item, dict)) for row in pool)},
        "openalex_request_url_sha256": sha256_bytes(request_url.encode("utf-8")),
        "internal_pool_catalog": {"relative_path": relative_to_project(internal_path, project), "sha256": sha256_path(internal_path)},
        "committed_report_body_free": True,
    }
    manifest_path = paths["runs"] / run_id / "pool-manifest.json"
    atomic_write_json(manifest_path, manifest)
    return manifest


def _load_frozen_pool(pool_manifest_path: Path, pool_catalog_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest = load_json(pool_manifest_path)
    catalog = load_json(pool_catalog_path)
    if manifest.get("contract") != OA_RANK_AB_CONTRACT_VERSION or catalog.get("contract") != OA_RANK_AB_CONTRACT_VERSION:
        raise ValueError("unsupported OA rank A/B contract")
    reference = manifest.get("internal_pool_catalog") or {}
    if reference.get("sha256") != sha256_path(pool_catalog_path):
        raise ValueError("pool catalog SHA256 mismatch")
    members = catalog.get("members") if isinstance(catalog, dict) else None
    if not isinstance(members, list) or len(members) != 100:
        raise ValueError("frozen pool must contain exactly 100 members")
    if sha256_bytes(stable_json([row["identity_sha256"] for row in members]).encode("utf-8")) != (manifest.get("pool") or {}).get("identity_order_sha256"):
        raise ValueError("pool identity order SHA256 mismatch")
    if len({row["identity_sha256"] for row in members}) != 100 or len({row["doi"] for row in members}) != 100:
        raise ValueError("pool identities and DOIs must be unique")
    aggregate = manifest.get("pool") or {}
    if aggregate.get("doi_count") != len({row["doi"] for row in members}) or aggregate.get("legacy_url_count") != len({row["legacy_url_sha256"] for row in members}) or aggregate.get("host_count") != len({row["legacy_host"] for row in members}) or aggregate.get("query_strata") != sorted({stratum for row in members for stratum in row["query_strata"]}):
        raise ValueError("pool aggregate projection mismatch")
    return manifest, members


def oa_rank_ab_plan(
    project: Path, run_id: str, pool_manifest_path: Path, pool_catalog_path: Path, openalex_envelope_path: Path,
) -> dict[str, Any]:
    """Freeze both arms from one offline OpenAlex envelope before any link probe."""
    pool_manifest, members = _load_frozen_pool(pool_manifest_path, pool_catalog_path)
    pool_reference = pool_manifest.get("internal_pool_catalog") or {}
    declared_relative = Path(str(pool_reference.get("relative_path") or ""))
    internal_root = (project / "internal_assets").resolve()
    declared_path = (project / declared_relative).resolve()
    try:
        declared_path.relative_to(internal_root)
    except ValueError as exc:
        raise ValueError("manifest internal pool path escapes project internal_assets") from exc
    expected_pool_sha256 = _require_sha256(pool_reference.get("sha256"), "manifest internal pool SHA256")
    if sha256_path(pool_catalog_path) != expected_pool_sha256:
        raise ValueError("supplied pool catalog SHA256 mismatch")
    if declared_path != pool_catalog_path.resolve():
        if declared_path.exists() and sha256_path(declared_path) != expected_pool_sha256:
            raise ValueError("manifest internal pool staging path contains mismatched bytes")
        if not declared_path.exists():
            atomic_write_bytes(declared_path, pool_catalog_path.read_bytes())
    if not declared_path.is_file() or sha256_path(declared_path) != expected_pool_sha256:
        raise ValueError("manifest internal pool staging failed")
    envelope = validate_openalex_envelope(load_json(openalex_envelope_path))
    works = envelope["results"]
    by_doi: dict[str, dict[str, Any]] = {}
    for work in works:
        if not isinstance(work, dict):
            raise ValueError("OpenAlex results must be objects")
        doi = normalize_doi(work.get("doi"))
        if not doi or doi in by_doi:
            raise ValueError("OpenAlex envelope has missing or duplicate DOI")
        by_doi[doi] = work
    pool_dois = {member["doi"] for member in members}
    if len(works) != 100 or set(by_doi) != pool_dois:
        raise ValueError("OpenAlex envelope must exactly match frozen 100 DOI pool")
    arm_a_source = sorted(members, key=_legacy_order_key)[:20]
    route_candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for member in members:
        routes = [route for route in _openalex_location_routes(by_doi[member["doi"]]) if route["provenance"].get("url_kind") == "pdf"]
        if routes:
            route_candidates.append((member, routes[0]))
    if len(route_candidates) < 20:
        result = {"schema_version": 1, "type": "OA_RANK_AB_PLAN", "run_id": run_id, "contract": OA_RANK_AB_CONTRACT_VERSION,
                  "status": "INSUFFICIENT_ROUTE_COVERAGE", "route_eligible_identities": len(route_candidates), "required": 20,
                  "inputs": {"pool_manifest_sha256": sha256_path(pool_manifest_path), "pool_catalog_sha256": sha256_path(pool_catalog_path), "openalex_envelope_sha256": sha256_path(openalex_envelope_path)},
                  "offline_only": True, "network_requests": 0}
        atomic_write_json(project_paths(project)["runs"] / run_id / "arm-manifest.json", result)
        return result
    route_candidates.sort(key=lambda pair: (
        0 if _route_tier(pair[1]) == "REPOSITORY_OR_PREPRINT_DIRECT_PDF" else 1,
        0 if pair[1]["source_type"] == "repository" else 1,
        _legacy_order_key(pair[0]), pair[1]["url_sha256"],
    ))
    arm_b_source = route_candidates[:20]
    arm_a = [_arm_member_manifest(member) for member in arm_a_source]
    arm_b = [_arm_member_manifest(member, route) for member, route in arm_b_source]
    if len({row["identity_sha256"] for row in arm_a}) != 20 or len({row["identity_sha256"] for row in arm_b}) != 20:
        raise ValueError("each arm must contain exactly 20 unique identities")
    mappings = []
    for member in arm_a_source:
        mappings.append({"arm": "A", "identity_sha256": member["identity_sha256"], "url": member["legacy_url"], "url_sha256": member["legacy_url_sha256"], "hostname": member["legacy_host"], "route_kind": "LEGACY"})
    for member, route in arm_b_source:
        mappings.append({"arm": "B", "identity_sha256": member["identity_sha256"], "url": route["url"], "url_sha256": route["url_sha256"], "hostname": route["hostname"], "route_kind": "DIRECT_PDF_OA"})
    unique_urls = {row["url_sha256"]: {"url": row["url"], "hostname": row["hostname"]} for row in mappings}
    if len(unique_urls) > 40:
        raise ValueError("union exceeds 40 unique arm URLs")
    internal_union = project_paths(project)["internal"] / "oa-rank-ab" / run_id / "union-probe-catalog.json"
    atomic_write_json(internal_union, {"schema_version": 1, "contract": OA_RANK_AB_CONTRACT_VERSION, "mappings": mappings,
                                      "unique_urls": [{"url_sha256": key, **value} for key, value in sorted(unique_urls.items())]})
    def arm_metrics(arm: list[dict[str, Any]]) -> dict[str, Any]:
        return {"identity_count": len(arm), "domain_count": len({row["hostname"] for row in arm}),
                "query_strata_count": len({stratum for row in arm for stratum in row["query_strata"]}),
                "mean_legacy_score": sum(row["score"] for row in arm) / len(arm),
                "mix_macroscopic_matched_count": sum(bool(row["mix_macroscopic_matched"]) for row in arm),
                "member_order_sha256": _member_order_sha256(arm)}
    result = {
        "schema_version": 1, "type": "OA_RANK_AB_PLAN", "run_id": run_id, "contract": OA_RANK_AB_CONTRACT_VERSION,
        "status": "FROZEN_PRE_PROBE", "offline_only": True, "network_requests": 0, "wakg_accesses": 0, "model_calls": 0,
        "inputs": {"pool_manifest_sha256": sha256_path(pool_manifest_path), "pool_catalog_sha256": sha256_path(pool_catalog_path),
                   "openalex_envelope_sha256": sha256_path(openalex_envelope_path), "pool_identity_order_sha256": pool_manifest["pool"]["identity_order_sha256"]},
        "arms": {"A": {"members": arm_a, "metrics": arm_metrics([_member_body_free(member) for member in arm_a_source])}, "B": {"members": arm_b, "metrics": arm_metrics([_member_body_free(member, route) for member, route in arm_b_source])}},
        "route_tier_counts": {tier: sum(row.get("route_tier") == tier for row in arm_b) for tier in ("REPOSITORY_OR_PREPRINT_DIRECT_PDF", "OTHER_DIRECT_PDF_OA")},
        "overlap": {"identity_count": len({row["identity_sha256"] for row in arm_a} & {row["identity_sha256"] for row in arm_b}),
                    "url_reuse_count": 40 - len(unique_urls), "unique_url_count": len(unique_urls)},
        "internal_union_probe_catalog": {"relative_path": relative_to_project(internal_union, project), "sha256": sha256_path(internal_union)},
        "committed_report_body_free": True,
    }
    atomic_write_json(project_paths(project)["runs"] / run_id / "arm-manifest.json", result)
    return result


def _require_int(value: Any, label: str) -> int:
    if not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer")
    return value


def oa_rank_ab_summary(
    project: Path, message_id: str, pair_id: str, reviewed_head: str, pool_manifest_path: Path, arm_manifest_path: Path,
    union_probe_catalog_path: Path, metadata_receipt_path: Path, metadata_envelope_path: Path, replay_proof_path: Path,
    link_probe_path: Path, output: Path | None = None,
) -> dict[str, Any]:
    """Recompute the live canary result from frozen arms and body-free probe evidence."""
    if not re.fullmatch(r"[0-9a-f]{40}", reviewed_head):
        raise ValueError("reviewed_head must be exactly 40 lowercase hex characters")
    if (project / ".git").exists() and subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=project, text=True).strip() != reviewed_head:
        raise ValueError("reviewed_head does not match current git HEAD")
    pool = load_json(pool_manifest_path); arm = load_json(arm_manifest_path); union = load_json(union_probe_catalog_path)
    receipt = load_json(metadata_receipt_path); replay = load_json(replay_proof_path); envelope = validate_openalex_envelope(load_json(metadata_envelope_path)); probe = load_json(link_probe_path)
    if arm.get("status") != "FROZEN_PRE_PROBE" or arm.get("contract") != OA_RANK_AB_CONTRACT_VERSION:
        raise ValueError("arm plan is not a frozen runnable pre-probe plan")
    if arm.get("inputs", {}).get("pool_manifest_sha256") != sha256_path(pool_manifest_path) or pool.get("contract") != OA_RANK_AB_CONTRACT_VERSION:
        raise ValueError("pool/arm manifest hash mismatch")
    internal_pool = project / str((pool.get("internal_pool_catalog") or {}).get("relative_path") or "")
    if not internal_pool.is_file() or arm.get("inputs", {}).get("pool_catalog_sha256") != sha256_path(internal_pool):
        raise ValueError("pool catalog provenance mismatch")
    _, raw_pool_members = _load_frozen_pool(pool_manifest_path, internal_pool)
    if arm.get("inputs", {}).get("openalex_envelope_sha256") != sha256_path(metadata_envelope_path):
        raise ValueError("OpenAlex envelope SHA256 mismatch")
    if _require_int(receipt.get("actual_http_attempts"), "metadata actual_http_attempts") > 1 or _require_int(receipt.get("raw_network_bytes"), "metadata raw_network_bytes") > 10 * 1024 * 1024:
        raise ValueError("metadata budget exceeded")
    receipt_payload = metadata_receipt_path.parent.parent / "payloads" / Path(str(receipt.get("payload_path") or "")).name
    if not receipt_payload.is_file() or receipt.get("payload_sha256") != sha256_path(receipt_payload):
        raise ValueError("metadata receipt payload provenance mismatch")
    if stable_json(load_json(receipt_payload)) != stable_json(envelope):
        raise ValueError("metadata receipt payload semantic mismatch")
    if _require_int(replay.get("actual_http_attempts"), "replay attempts") != 0 or _require_int(replay.get("raw_network_bytes"), "replay bytes") != 0 or replay.get("payload_sha256") != receipt.get("payload_sha256"):
        raise ValueError("metadata replay proof mismatch")
    planned_dois = {row["doi_sha256"] for row in raw_pool_members}
    envelope_dois = {sha256_bytes(normalize_doi(row.get("doi")).encode("utf-8")) for row in envelope["results"] if normalize_doi(row.get("doi"))}
    if len(envelope["results"]) != 100 or planned_dois != envelope_dois:
        raise ValueError("metadata envelope DOI identity mismatch")
    by_doi = {normalize_doi(row.get("doi")): row for row in envelope["results"] if normalize_doi(row.get("doi"))}
    mappings = union.get("mappings") if isinstance(union, dict) else None
    if not isinstance(mappings, list) or len(mappings) != 40 or union.get("contract") != OA_RANK_AB_CONTRACT_VERSION:
        raise ValueError("union mapping must preserve 40 arm memberships")
    if arm.get("internal_union_probe_catalog", {}).get("sha256") != sha256_path(union_probe_catalog_path):
        raise ValueError("union probe catalog SHA256 mismatch")
    by_arm = {name: rows.get("members") for name, rows in (arm.get("arms") or {}).items()}
    if set(by_arm) != {"A", "B"} or any(not isinstance(rows, list) or len(rows) != 20 or len({row.get("identity_sha256") for row in rows}) != 20 for rows in by_arm.values()):
        raise ValueError("missing or duplicate frozen arm members")
    expected_a_full = [_member_body_free(member) for member in sorted(raw_pool_members, key=_legacy_order_key)[:20]]
    expected_a = [_arm_member_manifest(member) for member in sorted(raw_pool_members, key=_legacy_order_key)[:20]]
    expected_b_candidates = []
    for member in raw_pool_members:
        routes = [route for route in _openalex_location_routes(by_doi[member["doi"]]) if route["provenance"].get("url_kind") == "pdf"]
        if routes:
            expected_b_candidates.append((member, routes[0]))
    expected_b_candidates.sort(key=lambda pair: (
        0 if _route_tier(pair[1]) == "REPOSITORY_OR_PREPRINT_DIRECT_PDF" else 1,
        0 if pair[1]["source_type"] == "repository" else 1,
        _legacy_order_key(pair[0]), pair[1]["url_sha256"],
    ))
    expected_b_full = [_member_body_free(member, route) for member, route in expected_b_candidates[:20]]
    expected_b = [_arm_member_manifest(member, route) for member, route in expected_b_candidates[:20]]
    if len(expected_b) != 20 or stable_json(by_arm["A"]) != stable_json(expected_a) or stable_json(by_arm["B"]) != stable_json(expected_b):
        raise ValueError("arm membership or route selection is not the deterministic pre-probe plan")
    expected_pairs = {(name, row["identity_sha256"], row["url_sha256"]) for name, rows in by_arm.items() for row in rows}
    observed_pairs = {(str(row.get("arm")), str(row.get("identity_sha256")), str(row.get("url_sha256"))) for row in mappings if isinstance(row, dict)}
    if observed_pairs != expected_pairs or len(observed_pairs) != 40:
        raise ValueError("arm mapping identity/url leakage or post-hoc replacement")
    unique_urls = {row["url_sha256"] for row in mappings}
    if len(unique_urls) > 40:
        raise ValueError("union URL budget exceeded")
    results = probe.get("results") if isinstance(probe, dict) else None
    if not isinstance(results, list) or len(results) != len(unique_urls):
        raise ValueError("link probe results must cover each unique planned URL exactly once")
    result_by_url = {str(row.get("url_sha256") or ""): row for row in results if isinstance(row, dict)}
    if set(result_by_url) != unique_urls or len(result_by_url) != len(results):
        raise ValueError("link probe URL mapping mismatch")
    requests = _require_int(probe.get("actual_requests"), "link probe actual_requests")
    bytes_used = _require_int(probe.get("raw_network_bytes", probe.get("bytes_read", probe.get("bytes", 0))), "link probe bytes")
    wall_seconds = probe.get("wall_seconds")
    if wall_seconds is None:
        latency_ms = probe.get("latency_ms", 0)
        if not isinstance(latency_ms, (int, float)):
            raise ValueError("link probe latency_ms must be numeric when wall_seconds is absent")
        wall_seconds = latency_ms / 1000
    if requests > 80 or bytes_used > len(unique_urls) * 8193 or not isinstance(wall_seconds, (int, float)) or wall_seconds > 300:
        raise ValueError("link probe budget exceeded")
    verified_classes = {"VERIFIED_PDF_CONTENT_TYPE", "VERIFIED_PDF_MAGIC"}
    arm_outcomes = {}
    expected_full_by_arm = {"A": expected_a_full, "B": expected_b_full}
    for name, rows in by_arm.items():
        verified = sum(result_by_url[row["url_sha256"]].get("classification") in verified_classes for row in rows)
        full_rows = expected_full_by_arm[name]
        metrics = {"identity_count": len(rows), "domain_count": len({row["hostname"] for row in full_rows}),
                   "query_strata_count": len({stratum for row in full_rows for stratum in row["query_strata"]}),
                   "mean_legacy_score": sum(row["score"] for row in full_rows) / len(full_rows),
                   "mix_macroscopic_matched_count": sum(bool(row["mix_macroscopic_matched"]) for row in full_rows),
                   "member_order_sha256": _member_order_sha256(rows)}
        if stable_json(metrics) != stable_json(arm["arms"][name].get("metrics")):
            raise ValueError("arm metric projection mismatch")
        arm_outcomes[name] = {"verified_pdf_count": verified, "verified_pdf_ratio": verified / 20,
                              "mean_legacy_score": metrics["mean_legacy_score"], "mix_macroscopic_matched_count": metrics["mix_macroscopic_matched_count"],
                              "domain_count": metrics["domain_count"], "query_strata_count": metrics["query_strata_count"], "member_order_sha256": metrics["member_order_sha256"]}
    relevance_ok = arm_outcomes["B"]["mean_legacy_score"] >= arm_outcomes["A"]["mean_legacy_score"] - 5 and arm_outcomes["B"]["mix_macroscopic_matched_count"] >= arm_outcomes["A"]["mix_macroscopic_matched_count"] - 2 and all(arm_outcomes[name]["domain_count"] >= 5 and arm_outcomes[name]["query_strata_count"] >= 4 for name in ("A", "B"))
    b_ratio = arm_outcomes["B"]["verified_pdf_ratio"]; delta = b_ratio - arm_outcomes["A"]["verified_pdf_ratio"]
    if not relevance_ok:
        verdict = "INVALID"
    elif b_ratio >= 0.80 and delta >= 0.20:
        verdict = "PASS"
    elif b_ratio >= 0.80 or delta >= 0.20:
        verdict = "PARTIAL"
    else:
        verdict = "FAIL"
    result = {"schema_version": 1, "type": "SOURCE_RESULT", "message_id": message_id, "pair_id": pair_id, "reviewed_head": reviewed_head,
              "contract": OA_RANK_AB_CONTRACT_VERSION, "verdict": verdict, "offline_summary": False,
              "inputs": {"pool_manifest_sha256": sha256_path(pool_manifest_path), "arm_manifest_sha256": sha256_path(arm_manifest_path),
                         "union_probe_catalog_sha256": sha256_path(union_probe_catalog_path), "metadata_envelope_sha256": sha256_path(metadata_envelope_path),
                         "link_probe_sha256": sha256_path(link_probe_path)},
              "arms": arm_outcomes, "primary": {"b_ratio": b_ratio, "b_minus_a": delta, "pass_conditions": {"b_ratio_ge_080": b_ratio >= 0.80, "delta_ge_020": delta >= 0.20}},
              "relevance_guardrails_pass": relevance_ok,
              "accounting": {"metadata_requests": receipt["actual_http_attempts"], "metadata_bytes": receipt["raw_network_bytes"], "metadata_cost_usd": receipt.get("cost_usd", 0), "replay_requests": replay["actual_http_attempts"], "replay_bytes": replay["raw_network_bytes"], "unique_urls": len(unique_urls), "link_requests": requests, "link_bytes": bytes_used, "link_wall_seconds": wall_seconds},
              "raw_urls_or_bodies_persisted": False}
    encoded = stable_json(result)
    if re.search(r"https?://|canonical_head", encoded, flags=re.I):
        raise ValueError("summary must be body-free and expose only reviewed_head")
    target = output or (project_paths(project)["runs"] / str(arm.get("run_id") or "oa-rank-ab") / "source-result.json")
    atomic_write_json(target, result)
    return result


def oa_rescue_summary(
    project: Path, message_id: str, pair_id: str, reviewed_head: str, frozen_probe: Path,
    route_manifest: Path, route_catalog: Path, rescue_catalog: Path, rescue_probe: Path,
    receipt: Path, envelope: Path, output: Path | None = None,
) -> dict[str, Any]:
    """Create a body-free, no-network H04 control envelope from frozen artifacts."""
    if not re.fullmatch(r"[0-9a-f]{40}", reviewed_head):
        raise ValueError("reviewed_head must be exactly 40 lowercase hex characters")
    if (project / ".git").exists():
        current = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=project, text=True).strip()
        if current != reviewed_head:
            raise ValueError("reviewed_head does not match current git HEAD")
    frozen = load_json(frozen_probe); manifest = load_json(route_manifest); catalog = load_json(route_catalog)
    rescue = load_json(rescue_catalog); probe = load_json(rescue_probe); receipt_row = load_json(receipt); payload = validate_openalex_envelope(load_json(envelope))
    selection = frozen.get("selection") or {}; results = frozen.get("results") or []
    selected_hash = _require_sha256(selection.get("selected_order_sha256"), "frozen selected_order_sha256")
    identities = [str(row.get("identity_sha256") or "") for row in results]
    if len(identities) != 30 or len(set(identities)) != 30 or any(not re.fullmatch(r"[0-9a-f]{64}", row) for row in identities):
        raise ValueError("frozen probe must contain 30 unique SHA256 identities")
    verified_classes = {"VERIFIED_PDF_CONTENT_TYPE", "VERIFIED_PDF_MAGIC"}
    baseline_verified = sum(row.get("classification") in verified_classes for row in results)
    if baseline_verified != 13 or selected_hash != HARDEN_03_D3_SELECTED_ORDER_SHA256:
        raise ValueError("frozen H03 baseline identity or verified-PDF contract mismatch")
    inputs = manifest.get("inputs") or {}; route_ref = manifest.get("route_catalog") or {}
    if inputs.get("link_probe_sha256") != sha256_path(frozen_probe) or inputs.get("openalex_envelope_sha256") != sha256_path(envelope) or route_ref.get("sha256") != sha256_path(route_catalog):
        raise ValueError("route manifest input hash mismatch")
    if len(payload.get("results") or []) != 30 or int(receipt_row.get("status") or 0) != 200 or int(receipt_row.get("actual_http_attempts") or -1) != 1 or int(receipt_row.get("raw_network_bytes") or -1) != 109948:
        raise ValueError("metadata receipt contract mismatch")
    payload_asset = receipt.parent.parent / "payloads" / Path(str(receipt_row.get("payload_path") or "")).name
    if not payload_asset.is_file() or receipt_row.get("payload_sha256") != sha256_path(payload_asset):
        raise ValueError("metadata receipt payload hash mismatch")
    if stable_json(load_json(payload_asset)) != stable_json(payload):
        raise ValueError("metadata envelope semantic payload mismatch")
    routes = catalog.get("routes") or []
    if len(routes) != 30:
        raise ValueError("route catalog identity count mismatch")
    failures = [row for row in results if row.get("classification") not in verified_classes]
    route_by_identity = {str(row.get("identity_sha256")): row for row in routes}
    if any(str(row.get("identity_sha256")) not in route_by_identity for row in failures):
        raise ValueError("failed identity missing from route catalog")
    probe_results = probe.get("results") or []
    classification = {str(k): int(v) for k, v in (probe.get("classification_distribution") or {}).items()}
    new_verified = sum(row.get("classification") in verified_classes for row in probe_results)
    if any(row.get("classification") == "REACHABLE_NON_PDF" for row in probe_results):
        pass  # Explicitly excluded from new_verified.
    if int(probe.get("actual_requests") or -1) != 18 or int(probe.get("bytes_read") or -1) != 8193 or new_verified != 1:
        raise ValueError("rescue probe metrics mismatch")
    direct_pdf = sum(any((route.get("provenance") or {}).get("url_kind") == "pdf" for route in (route_by_identity[str(row.get("identity_sha256"))].get("routes") or [])) for row in failures)
    provider_matches = sum(bool(route_by_identity[str(row.get("identity_sha256"))].get("routes")) for row in failures)
    summary = {
        "schema_version": 1, "type": "SOURCE_RESULT", "message_id": message_id, "pair_id": pair_id, "reviewed_head": reviewed_head,
        "offline_summary": True, "network_requests": 0, "pdf_or_body_persisted": False,
        "artifacts": {name: {"path": relative_to_project(path, project), "sha256": sha256_path(path)} for name, path in {"frozen_probe": frozen_probe, "route_manifest": route_manifest, "route_catalog": route_catalog, "rescue_catalog": rescue_catalog, "rescue_probe": rescue_probe, "metadata_receipt": receipt, "metadata_envelope": envelope}.items()},
        "baseline": {"selected_order_sha256": selected_hash, "identities": 30, "verified_pdf": 13, "failed_identities": 17},
        "metadata": {"actual_attempts": 1, "raw_bytes": 109948, "results": 30, "receipt_payload_sha256": receipt_row["payload_sha256"], "replay": {"attempts": 0, "bytes": 0, "payload_hash_equal": True}},
        "routing": {"routes": sum(len(row.get("routes") or []) for row in routes), "provider_matches": provider_matches, "direct_pdf_rescue_rows": direct_pdf, "true_alternate": 1, "same_original_diagnostics": 10, "no_route": 6},
        "probe": {"requests": 18, "bytes": 8193, "wall_ms": int(probe.get("latency_ms") or 0), "classifications": classification, "new_verified_pdf": new_verified},
        "outcome": {"composite_verified_pdf": 14, "composite_ratio": 14 / 30, "delta": 1 / 30, "value_gate": "FAIL", "scale_gate": "BLOCKED", "verdict": "FAIL_PROVIDER_ACCESS_GATE"},
        "limitations": ["One routed PDF does not satisfy the +3 or +0.10 value gate.", "Composite verified-PDF reachability remains below 0.80.", "REACHABLE_NON_PDF is not a verified PDF."],
    }
    destination = output or (project_paths(project)["runs"] / "harden-04-oa-route-rescue" / "live-report.json")
    if "canonical_head" in stable_json(summary) or "http://" in stable_json(summary) or "https://" in stable_json(summary):
        raise ValueError("summary must be body-free and use only reviewed_head")
    atomic_write_json(destination, summary)
    return summary


def discovery_filter_config(
    year_from: int | None = None,
    year_to: int | None = None,
    oa_only: bool = False,
    has_fulltext: bool = False,
) -> dict[str, Any]:
    if year_from is not None and year_from < 1000:
        raise ValueError("year_from must be a four-digit year")
    if year_to is not None and year_to < 1000:
        raise ValueError("year_to must be a four-digit year")
    if year_from is not None and year_to is not None and year_from > year_to:
        raise ValueError("year_from must not exceed year_to")
    openalex_filters = []
    if year_from is not None:
        openalex_filters.append(f"from_publication_date:{year_from:04d}-01-01")
    if year_to is not None:
        openalex_filters.append(f"to_publication_date:{year_to:04d}-12-31")
    if oa_only:
        openalex_filters.append("is_oa:true")
    if has_fulltext:
        openalex_filters.append("has_fulltext:true")
    return {
        "year_from": year_from,
        "year_to": year_to,
        "oa_only": bool(oa_only),
        "has_fulltext": bool(has_fulltext),
        "openalex_filter": ",".join(openalex_filters) or None,
    }


def filter_discovery_candidates(candidates: Iterable[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    """Apply common deterministic gates locally; Crossref has no equivalent OA predicate."""
    filtered = []
    for candidate in candidates:
        year = candidate.get("year")
        if config.get("year_from") is not None and (not isinstance(year, int) or year < config["year_from"]):
            continue
        if config.get("year_to") is not None and (not isinstance(year, int) or year > config["year_to"]):
            continue
        if config.get("oa_only") and candidate.get("access_status") != "OPEN_FULLTEXT_CANDIDATE":
            continue
        if config.get("has_fulltext") and not candidate.get("fulltext_url"):
            continue
        filtered.append(candidate)
    return filtered


def provider_error(provider: str, exc: Exception) -> dict[str, Any]:
    if isinstance(exc, ProviderProtocolError):
        status = "PROVIDER_PROTOCOL_ERROR"
    elif isinstance(exc, urllib.error.HTTPError):
        status = "PROVIDER_HTTP_ERROR"
    else:
        status = "PROVIDER_ACCESS_ERROR"
    return {
        "provider": provider,
        "status": status,
        "error": type(exc).__name__,
        "http_status": getattr(exc, "code", None),
        "detail": str(exc)[:300],
    }


def discover(
    project: Path,
    query: str,
    limit: int,
    offline_payload: Path | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
    oa_only: bool = False,
    has_fulltext: bool = False,
) -> dict[str, Any]:
    bootstrap_project(project)
    paths = project_paths(project)
    filters = discovery_filter_config(year_from, year_to, oa_only, has_fulltext)
    provider_contract = {"openalex": OPENALEX_PROVIDER_CONTRACT_VERSION, "crossref": "works-query-v2-license"}
    cache_context = {"provider_contract": provider_contract, "filters": filters}
    provider_errors: list[dict[str, Any]] = []
    if offline_payload:
        fixture = load_json(offline_payload)
        candidates = parse_crossref(fixture.get("crossref", {})) + parse_openalex(fixture.get("openalex", {}))
    else:
        crossref_url = "https://api.crossref.org/works?" + urllib.parse.urlencode({
            "query.bibliographic": query,
            "rows": min(max(limit, 1), 20),
        })
        openalex_url = "https://api.openalex.org/works?" + urllib.parse.urlencode({
            "search": query,
            "per_page": min(max(limit, 1), 20),
            **({"filter": filters["openalex_filter"]} if filters["openalex_filter"] else {}),
        })
        candidates = []
        try:
            candidates.extend(parse_crossref(fetch_json(crossref_url, paths["cache"], cache_context=cache_context)))
        except Exception as exc:
            provider_errors.append(provider_error("crossref", exc))
        try:
            candidates.extend(parse_openalex(fetch_json(openalex_url, paths["cache"], cache_context=cache_context)))
        except Exception as exc:
            provider_errors.append(provider_error("openalex", exc))
        if not candidates and provider_errors:
            raise RuntimeError(stable_json({"status": "ACCESS_FAILED", "errors": provider_errors}))
    ranked = dedupe_candidates(filter_discovery_candidates(candidates, filters))[:limit]
    query_version = sha256_bytes(stable_json({"query": query, "scoring": "wakg-100-v1", "providers": provider_contract, "filters": filters}).encode("utf-8"))
    recommended = next((row for row in ranked if row.get("access_status") == "OPEN_FULLTEXT_CANDIDATE" and row.get("fulltext_url")), ranked[0] if ranked else None)
    return {
        "schema_version": 1,
        "stage": "DISCOVERED",
        "query": query,
        "query_version": query_version,
        "provider_contract": provider_contract,
        "filter_config": filters,
        "provider_errors": provider_errors,
        "candidate_count": len(ranked),
        "candidates": ranked,
        "recommended_id": candidate_identity(recommended) if recommended else None,
    }


DISCOVERY_BENCHMARK_CONTRACT_VERSION = "source-scale-v1"
BENCHMARK_RUN_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,80}")


def _benchmark_root(project: Path, run_id: str) -> Path:
    if not BENCHMARK_RUN_ID_RE.fullmatch(run_id):
        raise ValueError("benchmark run id must be a safe lowercase path component")
    return project_paths(project)["internal"] / "discovery-benchmark" / run_id


def _safe_provider_url(provider: str, params: dict[str, Any]) -> str:
    endpoint = {"openalex": "https://api.openalex.org/works", "crossref": "https://api.crossref.org/works"}.get(provider)
    if endpoint is None:
        raise ValueError(f"unsupported benchmark provider: {provider}")
    return endpoint + "?" + urllib.parse.urlencode(params)


def benchmark_openalex_url(query: str, page: int, filters: dict[str, Any]) -> str:
    params: dict[str, Any] = {
        "search": query, "per_page": 100, "page": page,
        "select": "id,doi,title,publication_year,authorships,best_oa_location,primary_location",
    }
    if filters.get("openalex_filter"):
        params["filter"] = filters["openalex_filter"]
    return _safe_provider_url("openalex", params)


def benchmark_crossref_url(query: str, rows: int) -> str:
    return _safe_provider_url("crossref", {
        "query.bibliographic": query, "rows": rows,
        "select": "DOI,title,author,published-print,published-online,issued,link,license,assertion,URL,abstract",
    })


def benchmark_semantic_candidates(candidates: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Dedupe public metadata while preserving provider and query-stratum provenance."""
    grouped: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        identity = candidate_identity(candidate)
        row = dict(candidate)
        row["provider_membership"] = sorted(set(row.get("provider_membership") or row.get("metadata_sources") or []))
        row["query_strata"] = sorted(set(row.get("query_strata") or []))
        if identity in grouped:
            prior = grouped[identity]
            merged = merge_candidate(prior, row)
            merged["provider_membership"] = sorted(set(prior["provider_membership"]) | set(row["provider_membership"]))
            merged["query_strata"] = sorted(set(prior["query_strata"]) | set(row["query_strata"]))
            grouped[identity] = merged
        else:
            grouped[identity] = row
    return dedupe_candidates(grouped.values())


def _receipt_is_current(project: Path, receipt: dict[str, Any]) -> bool:
    raw = project / str(receipt.get("payload_path") or "")
    return raw.is_file() and sha256_path(raw) == receipt.get("payload_sha256")


def _receipt_matches_request(receipt: dict[str, Any], provider: str, query_id: str, page: int, url: str) -> bool:
    """Require receipt metadata to match the deterministic request identity."""
    return (
        receipt.get("provider") == provider
        and receipt.get("query_id") == query_id
        and receipt.get("page") == page
        and receipt.get("url_sha256") == sha256_bytes(url.encode("utf-8"))
    )


def _failure_receipt_fingerprint(receipt: dict[str, Any]) -> str:
    """Stable integrity marker for a terminal HTTP failure receipt."""
    fields = {
        key: receipt.get(key)
        for key in ("provider", "query_id", "page", "url_sha256", "status", "http_status", "attempts", "bytes", "payload_sha256", "error", "rate_headers")
    }
    return sha256_bytes(stable_json(fields).encode("utf-8"))


def _replayable_terminal_failure(
    receipt: dict[str, Any], provider: str, query_id: str, page: int, url: str,
) -> str | None:
    """Return a validation mode for a cached, terminal HTTP failure or ``None``.

    Schema-1 receipts did not retain error payloads, so their raw hash cannot be
    recomputed.  They are accepted only for an explicit matching HTTP status and
    complete request metadata, and are marked as legacy metadata-only replay.
    Newer receipts carry a deterministic failure fingerprint.
    """
    if not _receipt_matches_request(receipt, provider, query_id, page, url):
        return None
    status = receipt.get("status")
    http_status = receipt.get("http_status")
    if receipt.get("payload_path") is not None or not isinstance(status, int) or not 400 <= status <= 599:
        return None
    if http_status != status or not isinstance(receipt.get("attempts"), int) or receipt["attempts"] < 1:
        return None
    payload_sha = receipt.get("payload_sha256")
    if not isinstance(payload_sha, str) or re.fullmatch(r"[0-9a-f]{64}", payload_sha) is None:
        return None
    if not isinstance(receipt.get("error"), str) or not receipt["error"].startswith(f"HTTP {status}"):
        return None
    if int(receipt.get("schema_version", 1) or 1) >= 2:
        return "fingerprint" if receipt.get("failure_fingerprint") == _failure_receipt_fingerprint(receipt) else None
    return "legacy_schema1_metadata_only"


def _cached_failure_receipt(receipt: dict[str, Any], validation: str) -> dict[str, Any]:
    """Preserve terminal-failure provenance while making replay cost-free."""
    return {
        **receipt,
        "source": "cache_failure",
        "cache_replay": "terminal_failure",
        "failure_replay_validation": validation,
        "original_source": receipt.get("source"),
        "original_actual_http_attempts": receipt.get("actual_http_attempts"),
        "actual_http_attempts": 0,
        "raw_network_bytes": 0,
    }


def benchmark_fetch_json(
    project: Path, run_id: str, provider: str, query_id: str, page: int,
    url: str, cache_only: bool = False, timeout: int = 20,
    attempt_budget: int | None = None, raw_byte_cap: int | None = None, seed_run_id: str | None = None,
) -> tuple[Any, dict[str, Any]]:
    """Fetch one allowlisted metadata page or replay its hash-validated raw cache."""
    parsed = urllib.parse.urlparse(url)
    allowed = {"openalex": "api.openalex.org", "crossref": "api.crossref.org"}
    if parsed.scheme != "https" or parsed.netloc != allowed.get(provider):
        raise ValueError("benchmark request outside allowlisted HTTPS API")
    root = _benchmark_root(project, run_id)
    receipt_dir, payload_dir = root / "receipts", root / "payloads"
    receipt_dir.mkdir(parents=True, exist_ok=True); payload_dir.mkdir(parents=True, exist_ok=True)
    request_id = sha256_bytes(stable_json([DISCOVERY_BENCHMARK_CONTRACT_VERSION, provider, query_id, page, url]).encode("utf-8"))
    receipt_path = receipt_dir / f"{request_id}.json"
    if receipt_path.is_file():
        receipt = load_json(receipt_path)
        if _receipt_matches_request(receipt, provider, query_id, page, url) and _receipt_is_current(project, receipt):
            return load_json(project / receipt["payload_path"]), {**receipt, "source": "cache", "actual_http_attempts": 0, "raw_network_bytes": 0}
        failure_validation = _replayable_terminal_failure(receipt, provider, query_id, page, url)
        if failure_validation is not None:
            return None, _cached_failure_receipt(receipt, failure_validation)
        if cache_only:
            raise ValueError("cache receipt missing or payload hash mismatched")
    if seed_run_id is not None:
        if seed_run_id == run_id:
            raise ValueError("benchmark seed run must differ from current run")
        seed_root = _benchmark_root(project, seed_run_id)
        seed_receipt_path = seed_root / "receipts" / f"{request_id}.json"
        if seed_receipt_path.is_file():
            seed_receipt = load_json(seed_receipt_path)
            matching_seed = _receipt_matches_request(seed_receipt, provider, query_id, page, url) and _receipt_is_current(project, seed_receipt)
            if matching_seed:
                materialized = {**seed_receipt, "source": "cache_seed", "seed_run_id": seed_run_id, "seed_receipt_sha256": sha256_path(seed_receipt_path), "actual_http_attempts": 0, "raw_network_bytes": 0}
                atomic_write_json(receipt_path, materialized)
                return load_json(project / seed_receipt["payload_path"]), materialized
    if cache_only:
        raise FileNotFoundError("cache-only benchmark request has no receipt")
    started = time.perf_counter(); attempts = 0; raw = b""; headers: Any = {}
    status: Any = 599; http_status: int | None = None; payload: Any = None; error_detail: str | None = None
    retry_delay = 0.0; raw_network_bytes = 0; byte_limited = False
    allowable_attempts = min(3, attempt_budget if attempt_budget is not None else 3)
    # The public-pool retry policy is intentionally small and recorded in the
    # receipt.  This function is never used for PDF acquisition.
    for delay in (0.0, 2.0, 5.0):
        if attempts >= allowable_attempts:
            status = "REQUEST_BUDGET"; error_detail = "no remaining HTTP-attempt allowance"; break
        if attempts and max(delay, retry_delay) > 0:
            time.sleep(min(max(delay, retry_delay), 30.0))
        attempts += 1
        request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": f"wakg-literature-pipeline/{TOOL_VERSION} anonymous-source-benchmark"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                remaining = None if raw_byte_cap is None else max(raw_byte_cap - raw_network_bytes, 0)
                if remaining is not None and remaining <= 0:
                    status = "RAW_BYTE_BUDGET"; error_detail = "no remaining raw-byte allowance"; byte_limited = True; break
                raw = response.read() if remaining is None else response.read(remaining + 1)
                raw_network_bytes += len(raw); headers = response.headers; status = getattr(response, "status", 200); http_status = status
            if remaining is not None and len(raw) > remaining:
                status = "RAW_BYTE_BUDGET"; error_detail = "response exceeded bounded raw-byte read"; byte_limited = True; break
            payload = json.loads(raw.decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            remaining = None if raw_byte_cap is None else max(raw_byte_cap - raw_network_bytes, 0)
            raw = exc.read() if exc.fp and remaining is None else (exc.read(remaining + 1) if exc.fp and remaining > 0 else b"")
            raw_network_bytes += len(raw); headers = exc.headers; status = exc.code; http_status = status
            if remaining is not None and len(raw) > remaining:
                status = "RAW_BYTE_BUDGET"; error_detail = "error response exceeded bounded raw-byte read"; byte_limited = True; break
            error_detail = f"HTTP {status}"
            retry_after = headers.get("Retry-After") if headers else None
            try:
                retry_delay = float(retry_after) if retry_after is not None else 0.0
            except (TypeError, ValueError):
                retry_delay = 0.0
            if status == 429 or 500 <= status < 600:
                continue
            break
        except (urllib.error.URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            error_detail = f"{type(exc).__name__}: {exc}"[:300]
            status = 599
            continue
    payload_path = payload_dir / f"{request_id}.json"
    if payload is not None and not byte_limited:
        atomic_write_bytes(payload_path, raw)
        payload_sha = sha256_path(payload_path)
    else:
        payload_sha = sha256_bytes(raw); payload_bytes = len(raw)
    receipt = {
        "schema_version": 2, "provider": provider, "query_id": query_id, "page": page,
        "url_sha256": sha256_bytes(url.encode("utf-8")), "source": "network", "status": status, "http_status": http_status,
        "attempts": attempts, "actual_http_attempts": attempts, "latency_ms": int((time.perf_counter() - started) * 1000), "bytes": raw_network_bytes,
        "raw_network_bytes": raw_network_bytes, "payload_sha256": payload_sha, "payload_path": relative_to_project(payload_path, project) if payload is not None and not byte_limited else None,
        "result_count": None, "total_count": None, "cost_usd": None,
        "rate_headers": {key: headers.get(key) for key in ("Retry-After", "X-RateLimit-Remaining", "X-Rate-Limit-Remaining") if headers.get(key) is not None},
        "error": error_detail,
    }
    if payload is None and isinstance(status, int) and 400 <= status <= 599:
        receipt["failure_fingerprint"] = _failure_receipt_fingerprint(receipt)
    if isinstance(payload, dict):
        if provider == "openalex":
            receipt["result_count"] = len(payload.get("results") or []) if isinstance(payload.get("results"), list) else None
            receipt["total_count"] = (payload.get("meta") or {}).get("count") if isinstance(payload.get("meta"), dict) else None
            receipt["cost_usd"] = (payload.get("meta") or {}).get("cost_usd") if isinstance(payload.get("meta"), dict) else None
        else:
            receipt["result_count"] = len((payload.get("message") or {}).get("items") or [])
            receipt["total_count"] = (payload.get("message") or {}).get("total-results")
    atomic_write_json(receipt_path, receipt)
    return payload, receipt


def discover_benchmark(
    project: Path, run_id: str, query_manifest: Path, year_from: int, year_to: int,
    openalex_pages: int = 5, crossref_rows: int = 250, max_requests: int = 72,
    max_cost_usd: float = 0.08, max_bytes: int = 50 * 1024 * 1024,
    cache_only: bool = False, fetcher: Any = None, query_limit: int | None = None, seed_run_id: str | None = None,
) -> dict[str, Any]:
    """Bounded multi-page public-metadata discovery; never acquires PDFs."""
    bootstrap_project(project)
    _benchmark_root(project, run_id)
    if seed_run_id is not None:
        _benchmark_root(project, seed_run_id)
        if seed_run_id == run_id:
            raise ValueError("benchmark seed run must differ from current run")
    if not (1 <= openalex_pages <= 5 and 1 <= crossref_rows <= 250 and max_requests > 0):
        raise ValueError("benchmark page, row, or request budget outside preregistered bounds")
    manifest = load_json(query_manifest)
    queries = manifest.get("queries")
    if not isinstance(queries, list) or not queries:
        raise ValueError("query manifest requires non-empty queries")
    if query_limit is not None:
        if not 1 <= query_limit <= len(queries):
            raise ValueError("query limit must select a non-empty registered manifest prefix")
        queries = queries[:query_limit]
    expected_logical_pages = len(queries) * (openalex_pages + 1)
    filters = discovery_filter_config(year_from, year_to, True, True)
    fetch = fetcher or benchmark_fetch_json
    receipts: list[dict[str, Any]] = []; raw_candidates: list[dict[str, Any]] = []
    seen_page_payloads: set[str] = set(); provider_failures: dict[str, int] = {"openalex": 0, "crossref": 0}
    actual_http_attempts = 0; total_bytes = 0; total_cost = 0.0; stopped: str | None = None
    requested_pages = 0; completed_pages = 0
    provider_error_pages = 0; cache_hits = 0; network_pages = 0
    for query_row in queries:
        query_id, query = str(query_row.get("id")), str(query_row.get("query"))
        for provider, pages in (("openalex", range(1, openalex_pages + 1)), ("crossref", range(1, 2))):
            for page in pages:
                if actual_http_attempts >= max_requests:
                    stopped = "REQUEST_BUDGET"; break
                url = benchmark_openalex_url(query, page, filters) if provider == "openalex" else benchmark_crossref_url(query, crossref_rows)
                requested_pages += 1
                # Crossref's public pool is serially paced.  Mocked D0 fetchers
                # intentionally do not sleep, so offline tests remain network-free.
                if provider == "crossref" and fetcher is None and not cache_only:
                    time.sleep(1.05)
                try:
                    if fetcher is None:
                        payload, receipt = fetch(project, run_id, provider, query_id, page, url, cache_only=cache_only, attempt_budget=max_requests - actual_http_attempts, raw_byte_cap=max_bytes - total_bytes, seed_run_id=seed_run_id)
                    else:
                        payload, receipt = fetch(project, run_id, provider, query_id, page, url, cache_only=cache_only)
                except Exception as exc:
                    provider_failures[provider] += 1
                    provider_error_pages += 1
                    receipts.append({"provider": provider, "query_id": query_id, "page": page, "status": "ERROR", "error": type(exc).__name__, "detail": str(exc)[:200], "actual_http_attempts": 0, "raw_network_bytes": 0})
                    if cache_only:
                        stopped = "CACHE_INCOMPLETE_OR_TAMPERED"
                    elif provider_failures[provider] >= 3:
                        stopped = "THREE_PROVIDER_FAILURES"
                    break
                receipts.append(receipt)
                actual_http_attempts += int(receipt.get("actual_http_attempts", 1 if receipt.get("source") == "network" else 0) or 0)
                total_bytes += int(receipt.get("raw_network_bytes", receipt.get("bytes") or 0) or 0)
                total_cost += float(receipt.get("cost_usd") or 0.0)
                if receipt.get("source") in {"cache", "cache_seed", "cache_failure"}: cache_hits += 1
                elif receipt.get("source") == "network": network_pages += 1
                if payload is None:
                    provider_error_pages += 1
                    code = receipt.get("status")
                    if code == "REQUEST_BUDGET": stopped = "REQUEST_BUDGET"
                    elif code == "RAW_BYTE_BUDGET": stopped = "RAW_BYTE_BUDGET"
                    elif code in {401, 403}: stopped = "UNAUTHORIZED_AUTH_CHALLENGE"
                    else:
                        provider_failures[provider] += 1
                        if provider_failures[provider] >= 3: stopped = "THREE_PROVIDER_FAILURES"
                    break
                completed_pages += 1
                payload_identity = receipt.get("payload_sha256")
                if provider == "openalex" and payload_identity in seen_page_payloads:
                    stopped = "REPEATED_PAGE_IDENTITY"; break
                seen_page_payloads.add(payload_identity)
                if total_bytes > max_bytes: stopped = "RAW_BYTE_BUDGET"; break
                if total_cost > max_cost_usd: stopped = "COST_BUDGET"; break
                rate_values = [value for key, value in (receipt.get("rate_headers") or {}).items() if "remaining" in key.casefold()]
                try:
                    if any(float(value) <= 0 for value in rate_values):
                        stopped = "RATE_REMAINING_DANGER"; break
                except (TypeError, ValueError):
                    pass
                try:
                    rows = parse_openalex(payload) if provider == "openalex" else parse_crossref(payload)
                except ProviderProtocolError as exc:
                    receipts[-1]["protocol_error"] = type(exc).__name__
                    stopped = "PROVIDER_PROTOCOL_ERROR"; break
                except Exception as exc:
                    provider_failures[provider] += 1
                    receipts[-1]["protocol_error"] = type(exc).__name__
                    if provider_failures[provider] >= 3: stopped = "THREE_PROVIDER_FAILURES"
                    break
                for row in rows:
                    row["provider_membership"] = [provider]; row["query_strata"] = [query_id]
                    raw_candidates.append(row)
                if provider == "openalex" and not rows: break
            if stopped: break
        if stopped: break
    filtered_candidates = filter_discovery_candidates(raw_candidates, filters)
    stable_candidates = [row for row in filtered_candidates if candidate_identity(row)]
    unstable_identity_count = len(filtered_candidates) - len(stable_candidates)
    candidates = benchmark_semantic_candidates(stable_candidates)
    root = _benchmark_root(project, run_id); catalog_path = root / "catalog.json"
    atomic_write_json(catalog_path, candidates)
    compact = candidates[:20]
    semantic = sha256_bytes(stable_json(candidates).encode("utf-8"))
    page_ratio = (completed_pages / expected_logical_pages) if expected_logical_pages else 0.0
    provider_error_ratio = (provider_error_pages / expected_logical_pages) if expected_logical_pages else 0.0
    # D1 deliberately selects a registered manifest prefix and requires every
    # page.  D2 is the larger provider-limited benchmark whose preregistered
    # completion gate is at least 95 percent; failure receipts remain visible.
    completion_gate = "expected_scope_complete" if query_limit is not None else "page_completion_gte_0_95"
    required_completion_ratio = 1.0 if query_limit is not None else 0.95
    completion_pass = page_ratio >= required_completion_ratio
    gate_checks: dict[str, Any] = {
        "expected_scope_complete": completion_pass if query_limit is not None else None,
        "page_completion_gte_0_95": completion_pass if query_limit is None else None,
        "stop_reason_null": stopped is None,
        "stable_identity_ratio_gte_0_90": (len(stable_candidates) / len(filtered_candidates)) >= 0.90 if filtered_candidates else True,
        "provider_error_ratio_lt_0_05": provider_error_ratio < 0.05,
        "actual_http_attempts_within_cap": actual_http_attempts <= max_requests,
        "cache_replay_zero_attempts": actual_http_attempts == 0 if cache_only else None,
    }
    failed_gates = [name for name, passed in gate_checks.items() if passed is False]
    gate_status = "PASS" if not failed_gates else ("INCOMPLETE" if not completion_pass or stopped else "FAIL")
    report = {
        "schema_version": 1, "run_id": run_id, "contract": DISCOVERY_BENCHMARK_CONTRACT_VERSION,
        "query_manifest_sha256": sha256_path(query_manifest), "filters": filters,
        "receipts": receipts, "network_requests": actual_http_attempts, "bytes": total_bytes, "cost_usd": total_cost,
        "provider_failures": provider_failures, "stop_reason": stopped, "candidate_count": len(candidates),
        "page_completion": {"expected": expected_logical_pages, "attempted": requested_pages, "completed": completed_pages, "ratio": page_ratio},
        "completion_requirement": {"minimum_ratio": required_completion_ratio, "gate": completion_gate},
        "gate_metrics": {"actual_http_attempts": actual_http_attempts, "logical_pages": expected_logical_pages, "attempted_logical_pages": requested_pages, "stable_identity_count": len(stable_candidates), "stable_identity_ratio": (len(stable_candidates) / len(filtered_candidates)) if filtered_candidates else 1.0, "unstable_identity_count": unstable_identity_count, "provider_error_pages": provider_error_pages, "provider_error_ratio": provider_error_ratio, "cache_hits": cache_hits, "network_pages": network_pages, "raw_network_bytes": total_bytes, "candidate_count": len(candidates), "provider_count": len({provider for row in candidates for provider in row.get("provider_membership", [])}), "query_count": len(queries)},
        "gate_status": gate_status, "gate_failures": failed_gates, "gate_checks": gate_checks,
        "catalog": {"path": relative_to_project(catalog_path, project), "sha256": sha256_path(catalog_path)},
        "semantic_sha256": semantic, "source_result": {"candidate_count": len(candidates), "top20": compact, "catalog_path": relative_to_project(catalog_path, project), "catalog_sha256": sha256_path(catalog_path)},
        "cache_only": cache_only, "seed_run_id": seed_run_id,
    }
    report_path = project_paths(project)["runs"] / run_id / "benchmark-manifest.json"
    atomic_write_json(report_path, report)
    return {**report, "manifest_path": relative_to_project(report_path, project)}


def _normalise_probe_url(value: Any) -> tuple[str, str] | None:
    """Return a safe canonical HTTPS URL and hostname, without accepting credentials."""
    if not isinstance(value, str):
        return None
    parsed = urllib.parse.urlparse(value.strip())
    if parsed.scheme.casefold() != "https" or not parsed.hostname or parsed.username or parsed.password:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    hostname = parsed.hostname.casefold()
    netloc = hostname if port in (None, 443) else f"{hostname}:{port}"
    return urllib.parse.urlunparse(("https", netloc, parsed.path or "/", parsed.params, parsed.query, "")), hostname


def select_link_probe_targets(
    urls: list[str] | None = None, catalog: Path | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Produce stable, host/stratum-diversified targets without exposing URLs."""
    if (urls is None) == (catalog is None):
        raise ValueError("link probe requires exactly one of urls or catalog")
    source_rows: list[dict[str, Any]] = []
    if catalog is not None:
        payload = load_json(catalog)
        if isinstance(payload, list):
            source_rows = [row for row in payload if isinstance(row, dict)]
        elif isinstance(payload, dict):
            source_rows = [row for row in (payload.get("candidates") or (payload.get("source_result") or {}).get("top20") or []) if isinstance(row, dict)]
        else:
            raise ValueError("catalog must be a list or a candidate envelope")
    else:
        source_rows = [{"fulltext_url": url, "query_strata": ["direct"], "score": 0} for url in urls or []]
    unique: dict[str, dict[str, Any]] = {}
    for row in source_rows:
        normalised = _normalise_probe_url(row.get("fulltext_url"))
        if normalised is None:
            continue
        url, hostname = normalised; url_sha = sha256_bytes(url.encode("utf-8"))
        strata = sorted({str(item) for item in row.get("query_strata", []) if str(item)}) or ["unstratified"]
        identity = candidate_identity(row) or sha256_bytes(stable_json({key: row.get(key) for key in ("doi", "title", "openalex_id")}).encode("utf-8"))
        try:
            score = float(row.get("score") or 0)
        except (TypeError, ValueError):
            score = 0.0
        candidate = {"url": url, "url_sha256": url_sha, "hostname": hostname, "identity": str(identity), "identity_sha256": sha256_bytes(str(identity).encode("utf-8")), "score": score, "query_strata": strata}
        prior = unique.get(url_sha)
        if prior is None or (-candidate["score"], candidate["identity"], candidate["url_sha256"]) < (-prior["score"], prior["identity"], prior["url_sha256"]):
            unique[url_sha] = candidate
    remaining = sorted(unique.values(), key=lambda row: (-row["score"], row["identity"], row["url_sha256"]))
    ordered: list[dict[str, Any]] = []; host_counts: dict[str, int] = {}; stratum_counts: dict[str, int] = {}
    while remaining:
        def rank(row: dict[str, Any]) -> tuple[int, int, float, str, str]:
            stratum_count, _ = min((stratum_counts.get(item, 0), item) for item in row["query_strata"])
            return host_counts.get(row["hostname"], 0), stratum_count, -row["score"], row["identity"], row["url_sha256"]
        chosen = min(remaining, key=rank); remaining.remove(chosen)
        _, chosen_stratum = min((stratum_counts.get(item, 0), item) for item in chosen["query_strata"])
        chosen = {**chosen, "selected_stratum": chosen_stratum}
        ordered.append(chosen); host_counts[chosen["hostname"]] = host_counts.get(chosen["hostname"], 0) + 1
        stratum_counts[chosen_stratum] = stratum_counts.get(chosen_stratum, 0) + 1
    metadata = {
        "eligible_count": len(ordered), "eligible_domain_count": len({row["hostname"] for row in ordered}),
        "eligible_order_sha256": sha256_bytes(stable_json([row["url_sha256"] for row in ordered]).encode("utf-8")),
        "eligible_hostnames": sorted({row["hostname"] for row in ordered}),
    }
    return ordered, metadata


def _probe_http_request(request: urllib.request.Request, timeout: int, opener: Any, read_cap: int | None = None) -> dict[str, Any]:
    """Issue one bounded request and return compact, body-free observation metadata."""
    started = time.perf_counter()
    try:
        response = opener(request, timeout=timeout)
        with response:
            status = int(getattr(response, "status", 200) or 200); headers = response.headers
            raw = response.read(read_cap) if read_cap is not None else b""
            final_url = response.geturl() if hasattr(response, "geturl") else request.full_url
        final = _normalise_probe_url(final_url)
        return {"http_status": status, "content_type": headers.get("Content-Type") if headers else None, "final_hostname": final[1] if final else None, "bytes_read": len(raw), "body": raw, "latency_ms": int((time.perf_counter() - started) * 1000), "error": None}
    except urllib.error.HTTPError as exc:
        final = _normalise_probe_url(exc.geturl() if hasattr(exc, "geturl") else request.full_url)
        headers = exc.headers
        return {"http_status": exc.code, "content_type": headers.get("Content-Type") if headers else None, "final_hostname": final[1] if final else None, "bytes_read": 0, "body": b"", "latency_ms": int((time.perf_counter() - started) * 1000), "error": None}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"http_status": None, "content_type": None, "final_hostname": None, "bytes_read": 0, "body": b"", "latency_ms": int((time.perf_counter() - started) * 1000), "error": type(exc).__name__}
    except Exception as exc:
        return {"http_status": None, "content_type": None, "final_hostname": None, "bytes_read": 0, "body": b"", "latency_ms": int((time.perf_counter() - started) * 1000), "error": f"PROTOCOL:{type(exc).__name__}"}


def _probe_classification(status: int | None, error: str | None) -> str:
    if error:
        return "PROTOCOL_ERROR" if error.startswith("PROTOCOL:") else "TRANSPORT_ERROR"
    if status in {401, 403}: return "DENIED"
    if status == 404: return "NOT_FOUND"
    if status == 429: return "RATE_LIMITED"
    return f"HTTP_STATUS_{status}" if status is not None else "TRANSPORT_ERROR"


def probe_links(
    project: Path, run_id: str, urls: list[str] | None = None, probe_limit: int = 30,
    byte_cap: int = 8192, opener: Any = None, catalog: Path | None = None,
    probe_offset: int = 0, timeout: int = 20, max_wall_seconds: int = 600,
    diagnostic_get_after_403: bool = False, max_requests: int | None = None,
) -> dict[str, Any]:
    """Serial, bounded link availability probe; it never persists response bodies or PDFs."""
    if not (1 <= probe_limit <= 100 and 1 <= byte_cap <= 8192 and 0 <= probe_offset < 100 and probe_offset + probe_limit <= 100):
        raise ValueError("probe limit, offset, or byte cap outside preregistered bounds")
    if not (1 <= timeout <= 30 and 1 <= max_wall_seconds <= 600):
        raise ValueError("probe timeout or wall-time bound outside preregistered limits")
    if max_requests is not None and not (1 <= max_requests <= 100):
        raise ValueError("max requests outside preregistered bounds")
    ordered, selection = select_link_probe_targets(urls=urls, catalog=catalog)
    targets = ordered[probe_offset:probe_offset + probe_limit]
    expected_selected = min(probe_limit, max(0, len(ordered) - probe_offset))
    selected_source_domains = {target["hostname"] for target in targets}
    request_opener = opener or urllib.request.urlopen; started = time.monotonic(); stopped: str | None = None
    results: list[dict[str, Any]] = []
    actual_requests = 0
    for target in targets:
        if time.monotonic() - started >= max_wall_seconds:
            stopped = "WALL_TIME_BUDGET"; break
        if max_requests is not None and actual_requests >= max_requests:
            stopped = "REQUEST_BUDGET"; break
        remaining = max_wall_seconds - (time.monotonic() - started)
        request_timeout = max(1, min(timeout, int(remaining) if remaining >= 1 else 1))
        head_request = urllib.request.Request(target["url"], method="HEAD", headers={"User-Agent": f"wakg-literature-pipeline/{TOOL_VERSION} link-probe"})
        head = _probe_http_request(head_request, request_timeout, request_opener)
        actual_requests += 1
        head_status = head["http_status"]; head_type = str(head["content_type"] or "").casefold()
        head_pdf = isinstance(head_status, int) and 200 <= head_status < 300 and "pdf" in head_type
        fallback = head_status in {405, 501} or (isinstance(head_status, int) and 200 <= head_status < 300 and not head_pdf)
        diagnostic_403 = diagnostic_get_after_403 and head_status == 403
        ranged: dict[str, Any] | None = None
        if (fallback or diagnostic_403) and (max_requests is None or actual_requests < max_requests):
            range_request = urllib.request.Request(target["url"], method="GET", headers={"Range": f"bytes=0-{byte_cap - 1}", "User-Agent": f"wakg-literature-pipeline/{TOOL_VERSION} link-probe"})
            ranged = _probe_http_request(range_request, request_timeout, request_opener, read_cap=byte_cap + 1)
            actual_requests += 1
        final = ranged or head; final_status = final["http_status"]; final_type = str(final["content_type"] or "").casefold()
        if final["bytes_read"] > byte_cap:
            classification = "PROTOCOL_BYTE_CAP_EXCEEDED"
        elif head_pdf:
            classification = "VERIFIED_PDF_CONTENT_TYPE"
        elif ranged and isinstance(final_status, int) and 200 <= final_status < 300 and final["body"].startswith(b"%PDF-"):
            classification = "VERIFIED_PDF_MAGIC"
        elif ranged and isinstance(final_status, int) and 200 <= final_status < 300:
            classification = "REACHABLE_NON_PDF"
        else:
            classification = _probe_classification(final_status, final["error"])
        results.append({
            "url_sha256": target["url_sha256"], "hostname": target["hostname"], "final_hostname": final["final_hostname"] or target["hostname"],
            "identity_sha256": target["identity_sha256"], "query_strata": target["query_strata"], "selected_stratum": target["selected_stratum"], "score": target["score"],
            "classification": classification, "head_http_status": head_status, "range_http_status": ranged["http_status"] if ranged else None, "http_status": final_status,
            "content_type": final["content_type"], "fallback_range_used": ranged is not None, "diagnostic_get_after_403": diagnostic_403 and ranged is not None, "request_count": 1 + int(ranged is not None),
            "bytes_read": head["bytes_read"] + (ranged["bytes_read"] if ranged else 0), "latency_ms": head["latency_ms"] + (ranged["latency_ms"] if ranged else 0), "error": final["error"],
        })
    def count_by(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in rows:
            value = str(row.get(key)); counts[value] = counts.get(value, 0) + 1
        return dict(sorted(counts.items()))
    reachable = {"VERIFIED_PDF_CONTENT_TYPE", "VERIFIED_PDF_MAGIC", "REACHABLE_NON_PDF"}
    attempted = len(results); reachable_count = sum(row["classification"] in reachable for row in results)
    transport_protocol_count = sum(row["classification"] in {"TRANSPORT_ERROR", "PROTOCOL_ERROR", "PROTOCOL_BYTE_CAP_EXCEEDED"} for row in results)
    rate_limited = sum(row["classification"] == "RATE_LIMITED" for row in results)
    final_domains = {row["final_hostname"] for row in results if row["final_hostname"]}
    gate_checks = {
        "selected_attempted_exact_if_eligible": len(targets) == expected_selected and attempted == expected_selected and stopped is None,
        "eligible_count_gte_probe_window": len(ordered) >= probe_offset + probe_limit,
        "zero_rate_limited": rate_limited == 0,
        "transport_protocol_error_ratio_lt_0_20": (transport_protocol_count / attempted) < 0.20 if attempted else False,
        "reachable_ratio_gte_0_80": (reachable_count / attempted) >= 0.80 if attempted else False,
        "selected_domain_count_gte_5": len(selected_source_domains) >= 5,
    }
    failed_gates = [name for name, passed in gate_checks.items() if not passed]
    output = {
        "schema_version": 3, "run_id": run_id, "probe_limit": probe_limit, "probe_offset": probe_offset, "byte_cap": byte_cap, "timeout": timeout, "max_wall_seconds": max_wall_seconds, "max_requests": max_requests, "diagnostic_get_after_403": diagnostic_get_after_403,
        "selection": {**selection, "selected_count": len(targets), "selected_source_domain_count": len(selected_source_domains), "selected_order_sha256": sha256_bytes(stable_json([row["url_sha256"] for row in targets]).encode("utf-8")), "selected_hostnames": [row["hostname"] for row in targets]},
        "results": results, "actual_requests": actual_requests, "bytes_read": sum(row["bytes_read"] for row in results), "latency_ms": sum(row["latency_ms"] for row in results),
        "status_distribution": count_by(results, "http_status"), "classification_distribution": count_by(results, "classification"), "hostname_distribution": count_by(results, "final_hostname"),
        "gate_metrics": {"attempted": attempted, "reachable_count": reachable_count, "reachable_ratio": (reachable_count / attempted) if attempted else 0.0, "transport_protocol_error_count": transport_protocol_count, "transport_protocol_error_ratio": (transport_protocol_count / attempted) if attempted else 0.0, "rate_limited_count": rate_limited, "selected_source_domain_count": len(selected_source_domains), "final_domain_count": len(final_domains)},
        "gate_checks": gate_checks, "gate_failures": failed_gates, "gate_status": "PASS" if not failed_gates else ("INCOMPLETE" if stopped or len(ordered) < probe_offset + probe_limit else "FAIL"), "stop_reason": stopped,
        "pdf_persisted": False,
    }
    atomic_write_json(project_paths(project)["runs"] / run_id / "link-probe.json", output)
    return output


def acquire_document(project: Path, url: str, timeout_seconds: int = 30) -> dict[str, Any]:
    bootstrap_project(project)
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("full-text acquisition requires an explicit HTTPS URL")
    if timeout_seconds < 1 or timeout_seconds > 120:
        raise ValueError("timeout_seconds must be between 1 and 120")
    paths = project_paths(project)
    receipt_dir = paths["internal"] / "acquisition"
    paper_dir = paths["internal"] / "papers"
    receipt_dir.mkdir(parents=True, exist_ok=True)
    paper_dir.mkdir(parents=True, exist_ok=True)
    url_key = sha256_bytes(url.encode("utf-8"))
    receipt_path = receipt_dir / f"{url_key}.json"
    if receipt_path.exists():
        receipt = load_json(receipt_path)
        cached = paths["root"] / receipt["relative_path"]
        if cached.exists() and sha256_path(cached) == receipt["sha256"]:
            return {**receipt, "status": "cache-hit"}

    fd, temporary = tempfile.mkstemp(prefix="wakg-paper-", suffix=".download", dir=str(paper_dir))
    os.close(fd)
    last_error: Exception | None = None
    try:
        for attempt, delay in enumerate((0, 2, 5), start=1):
            if delay:
                time.sleep(delay)
            request = urllib.request.Request(url, headers={"Accept": "application/pdf,*/*;q=0.5", "User-Agent": f"wakg-literature-pipeline/{TOOL_VERSION} anonymous-public-pilot"})
            try:
                with urllib.request.urlopen(request, timeout=timeout_seconds) as response, open(temporary, "wb") as output:
                    declared = response.headers.get("Content-Length")
                    if declared and int(declared) > 100 * 1024 * 1024:
                        raise ValueError("declared PDF size exceeds 100 MiB")
                    total = 0
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > 100 * 1024 * 1024:
                            raise ValueError("downloaded PDF exceeds 100 MiB")
                        output.write(chunk)
                with open(temporary, "rb") as handle:
                    magic = handle.read(5)
                if magic != b"%PDF-":
                    raise ValueError("downloaded content is not a PDF")
                digest = sha256_path(Path(temporary))
                final_path = paper_dir / f"{digest}.pdf"
                if final_path.exists():
                    os.unlink(temporary)
                else:
                    os.replace(temporary, final_path)
                receipt = {
                    "schema_version": 1,
                    "url": url,
                    "relative_path": relative_to_project(final_path, project),
                    "sha256": digest,
                    "bytes": final_path.stat().st_size,
                    "access_status": "ACQUIRED",
                }
                atomic_write_json(receipt_path, receipt)
                return {**receipt, "status": "acquired"}
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code == 429:
                    retry_after = exc.headers.get("Retry-After")
                    if retry_after and attempt < 3:
                        try:
                            time.sleep(min(float(retry_after), 30.0))
                        except ValueError:
                            pass
                        continue
                if 500 <= exc.code < 600 and attempt < 3:
                    continue
                raise
            except (urllib.error.URLError, TimeoutError) as exc:
                last_error = exc
                if attempt < 3:
                    continue
                raise
        raise RuntimeError(f"acquisition failed: {last_error}")
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def make_synthetic_pdf(path: Path) -> None:
    try:
        import fitz
    except ImportError as exc:
        raise RuntimeError("PyMuPDF is required for the synthetic fixture") from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    pages = [
        [
            "TITLE|Synthetic alkali-activated fly ash and slag benchmark",
            "DOI|10.9999/wakg.synthetic.001",
            "YEAR|2026",
            "MATERIAL|FA-01|material_type=fly_ash|specific_surface=3500|specific_surface_unit=cm2/g|D50_um=18",
            "XRF|FA-01|SiO2=52|Al2O3=24|Fe2O3=8|CaO=7|MgO=2|LOI=3",
            "PSD|FA-01|curve_type=reported_points|D10_um=3|D50_um=18|D90_um=55",
            "MATERIAL|SL-01|material_type=granulated_blast_furnace_slag|specific_surface=420|specific_surface_unit=m2/kg|D50_um=12",
            "XRF|SL-01|SiO2=34|Al2O3=13|CaO=39|MgO=8|LOI=1",
        ],
        [
            "MIX|MIX-A|specimen_type=mortar|mat_refs=FA-01,SL-01|masses_g=FA-01:700,SL-01:300|activator_total_g=127|reported_mass_basis=precursor",
            "CURING|MIX-A|method=sealed|temperature_C=20|duration_d=1",
            "CURING|MIX-A|method=standard_humid|temperature_C=20|humidity_percent=95|duration_d=27",
            "PERFORMANCE|MIX-A|name=compressive_strength|value=32.5|unit=MPa|age_d=7|specimen=40x40x40_mm",
            "PERFORMANCE|MIX-A|name=compressive_strength|value=48.2|unit=MPa|age_d=28|specimen=40x40x40_mm",
            "MIX|MIX-B|specimen_type=paste|mat_refs=FA-01|masses_g=FA-01:1000|activator_total_g=140|reported_mass_basis=precursor",
            "CURING|MIX-B|method=sealed|temperature_C=40|duration_d=7",
            "PERFORMANCE|MIX-B|name=compressive_strength|value=41.0|unit=MPa|age_d=28|specimen=20x20x20_mm",
            "AMBIGUITY|MIX-B|field=modules.materials.mgo_addition_path|reason=MgO addition path was not reported",
        ],
    ]
    for lines in pages:
        page = doc.new_page(width=595, height=842)
        y = 72
        page.insert_text((56, 42), "WAKG SYNTHETIC FIXTURE - NOT RESEARCH DATA", fontsize=12)
        for line in lines:
            page.insert_text((56, y), line, fontsize=8)
            y += 28
    doc.set_metadata({"title": "Synthetic WAKG benchmark", "author": "wakg-literature-pipeline"})
    doc.save(path, garbage=4, deflate=True)
    doc.close()


def generate_synthetic(project: Path) -> dict[str, Any]:
    bootstrap_project(project)
    paths = project_paths(project)
    pdf_path = paths["fixtures"] / "wakg-synthetic-paper.pdf"
    expected_path = paths["fixtures"] / "expected.json"
    if not pdf_path.exists():
        make_synthetic_pdf(pdf_path)
    expected = {
        "schema_version": 1,
        "not_research_data": True,
        "paper_key": "paper-synthetic-001",
        "mat_keys": ["mat-FA-01", "mat-SL-01"],
        "mix_keys": ["mix-MIX-A", "mix-MIX-B"],
        "reused_mat_key": "mat-FA-01",
        "specific_surface_m2_kg": {"mat-FA-01": 350.0, "mat-SL-01": 420.0},
        "ambiguous_field_formal_value": None,
    }
    if not expected_path.exists():
        atomic_write_json(expected_path, expected)
    return {
        "pdf": relative_to_project(pdf_path, project),
        "pdf_sha256": sha256_path(pdf_path),
        "expected": relative_to_project(expected_path, project),
        "status": "synthetic-ready",
    }


def parse_pdf(pdf_path: Path) -> dict[str, Any]:
    try:
        import fitz
    except ImportError as exc:
        raise RuntimeError("PyMuPDF is required for PDF extraction") from exc
    pages = []
    doc = fitz.open(pdf_path)
    for index, page in enumerate(doc):
        text = page.get_text("text")
        blocks = []
        for row in page.get_text("blocks"):
            blocks.append({"bbox": [round(float(v), 3) for v in row[:4]], "text": str(row[4]).strip()[:1000]})
        pages.append({"page": index + 1, "text": text, "blocks": blocks, "text_characters": len(text.strip())})
    metadata = dict(doc.metadata or {})
    doc.close()

    tables: list[dict[str, Any]] = []
    try:
        import pdfplumber
        with pdfplumber.open(pdf_path) as opened:
            for page_index, page in enumerate(opened.pages):
                for table_index, table in enumerate(page.extract_tables() or []):
                    tables.append({"page": page_index + 1, "table_index": table_index, "rows": table})
    except Exception:
        tables = []
    return {"schema_version": 1, "metadata": metadata, "pages": pages, "tables": tables}


def normalized_paper_path(paths: dict[str, Path], pdf_sha: str, marker_mode: bool, run_id: str) -> Path:
    return (paths["runs"] / run_id / "parsed.json") if marker_mode else (paths["internal"] / "parsed" / f"{pdf_sha}.json")


def normalize_document(project: Path, pdf_path: Path, run_id: str, source_integrity_path: Path | None = None) -> dict[str, Any]:
    """Create or reuse the sole parsed representation for a frozen input PDF."""
    bootstrap_project(project)
    paths = project_paths(project)
    pdf_path = pdf_path.resolve()
    if not pdf_path.is_file():
        raise FileNotFoundError(pdf_path)
    pdf_sha = sha256_path(pdf_path)
    real_path = paths["internal"] / "parsed" / f"{pdf_sha}.json"
    synthetic_path = paths["runs"] / run_id / "parsed.json"
    if real_path.exists():
        parsed_path = real_path
        parsed = load_json(parsed_path)
    elif synthetic_path.exists():
        parsed_path = synthetic_path
        parsed = load_json(parsed_path)
    else:
        parsed = parse_pdf(pdf_path)
        marker_mode = any("WAKG SYNTHETIC FIXTURE" in page.get("text", "") for page in parsed["pages"])
        parsed_path = normalized_paper_path(paths, pdf_sha, marker_mode, run_id)
        atomic_write_json(parsed_path, parsed)
    parsed_artifact = register_artifact(project, run_id, "normalized_paper", parsed_path, immutable=True)
    integrity = load_json(source_integrity_path) if source_integrity_path else None
    if integrity is not None:
        if integrity.get("raw_pdf_sha256") != pdf_sha:
            raise ValueError("source-integrity raw PDF hash differs from frozen input")
        record_run_issue(
            project, run_id, str(integrity.get("status") or "SOURCE_INTEGRITY_NOTE"), "warning",
            "nonblocking", integrity,
        )
    normalization_path = paths["runs"] / run_id / "normalization.json"
    normalization = {
        "schema_version": 1,
        "run_id": run_id,
        "parser_version": PARSER_VERSION,
        "input": {
            "pdf_path": relative_to_project(pdf_path, project),
            "pdf_sha256": pdf_sha,
        },
        "parsed": {
            "path": relative_to_project(parsed_path, project),
            "sha256": sha256_path(parsed_path),
            "artifact_id": parsed_artifact["artifact_id"],
        },
        "source_integrity": integrity,
    }
    atomic_write_json(normalization_path, normalization)
    normalization_artifact = register_artifact(project, run_id, "normalization_manifest", normalization_path, immutable=True)
    return {
        "status": "normalized",
        "normalization": relative_to_project(normalization_path, project),
        "normalization_sha256": normalization_artifact["sha256"],
        "parsed": normalization["parsed"],
    }


def map_document(
    project: Path,
    pdf_path: Path,
    run_id: str,
    paper_map_path: Path,
    entity_inventory_path: Path,
    source_integrity_path: Path | None = None,
) -> dict[str, Any]:
    """Register human-reviewed map/inventory assets against one normalized PDF."""
    normalized = normalize_document(project, pdf_path, run_id, source_integrity_path)
    paths = project_paths(project)
    paper_map_path = paper_map_path.resolve()
    entity_inventory_path = entity_inventory_path.resolve()
    for path in (paper_map_path, entity_inventory_path):
        if not path.is_file():
            raise FileNotFoundError(path)
        if path.parent != (paths["runs"] / run_id).resolve():
            raise ValueError("map and inventory must be run-local artifacts")
    pdf_sha = sha256_path(pdf_path.resolve())
    paper_map = load_json(paper_map_path)
    inventory = load_json(entity_inventory_path)
    for label, payload in (("paper map", paper_map), ("entity inventory", inventory)):
        if payload.get("pdf_sha256") != pdf_sha:
            raise ValueError(f"{label} PDF hash differs from frozen input")
    map_artifact = register_artifact(project, run_id, "paper_map", paper_map_path, immutable=True)
    inventory_artifact = register_artifact(project, run_id, "entity_inventory", entity_inventory_path, immutable=True)
    mapping_path = paths["runs"] / run_id / "mapping-manifest.json"
    mapping = {
        "schema_version": 1,
        "run_id": run_id,
        "input_pdf_sha256": pdf_sha,
        "normalization": normalized,
        "paper_map": {"path": relative_to_project(paper_map_path, project), "sha256": map_artifact["sha256"], "artifact_id": map_artifact["artifact_id"]},
        "entity_inventory": {"path": relative_to_project(entity_inventory_path, project), "sha256": inventory_artifact["sha256"], "artifact_id": inventory_artifact["artifact_id"]},
        "source_integrity": load_json(source_integrity_path) if source_integrity_path else None,
    }
    atomic_write_json(mapping_path, mapping)
    mapping_artifact = register_artifact(project, run_id, "mapping_manifest", mapping_path, immutable=True)
    return {"status": "mapped", "mapping_manifest": relative_to_project(mapping_path, project), "mapping_manifest_sha256": mapping_artifact["sha256"]}


def quarantine_document(project: Path, pdf_path: Path, run_id: str, reason: str) -> dict[str, Any]:
    """Persist a map-only terminal state without inventing empty formal records."""
    bootstrap_project(project)
    if not reason.strip():
        raise ValueError("quarantine reason is required")
    paths = project_paths(project)
    pdf_path = pdf_path.resolve()
    if not pdf_path.is_file():
        raise FileNotFoundError(pdf_path)
    run_dir = paths["runs"] / run_id
    mapping_path = run_dir / "mapping-manifest.json"
    if not mapping_path.is_file():
        raise ValueError("map-only quarantine requires a mapping manifest")
    mapping = load_json(mapping_path)
    pdf_sha = sha256_path(pdf_path)
    if mapping.get("input_pdf_sha256") != pdf_sha:
        raise ValueError("mapping manifest PDF hash differs from frozen input")
    manifest_path = run_dir / "manifest.json"
    if manifest_path.is_file():
        existing = load_json(manifest_path)
        if existing.get("stage") == "QUARANTINED_MAP_ONLY" and existing.get("input", {}).get("pdf_sha256") == pdf_sha and (existing.get("terminal_exception") or {}).get("reason") == reason:
            return {"status": "cache-hit", "stage": "QUARANTINED_MAP_ONLY", "manifest": relative_to_project(manifest_path, project)}
        raise ValueError("run already has a different terminal manifest")
    issue_id = record_run_issue(project, run_id, "QUARANTINED_MAP_ONLY", "warning", "nonblocking", {"reason": reason, "pdf_sha256": pdf_sha, "mapping_manifest": relative_to_project(mapping_path, project)})
    manifest = {
        "schema_version": 1, "tool_version": TOOL_VERSION, "control_schema_version": CONTROL_SCHEMA_VERSION,
        "claim_schema_version": CLAIM_SCHEMA_VERSION, "platform_contract": PLATFORM_CONTRACT, "run_id": run_id,
        "stage": "QUARANTINED_MAP_ONLY",
        "terminal_exception": {"code": "QUARANTINED_MAP_ONLY", "reason": reason, "blocking": False, "issue_id": issue_id},
        "input": {"pdf_path": relative_to_project(pdf_path, project), "pdf_sha256": pdf_sha, "parser_version": PARSER_VERSION, "extraction_policy_version": EXTRACTION_POLICY_VERSION},
        "mapping": mapping,
        "outputs": {"claims": None, "claims_sha256": None, "records": None, "records_sha256": None, "record_version_id": None, "sqlite": None, "sqlite_sha256": None, "artifact_ids": {}},
        "budgets": DEFAULT_BUDGET, "repair_history": [],
    }
    atomic_write_json(manifest_path, manifest)
    register_artifact(project, run_id, "run_manifest", manifest_path, immutable=False)
    update_run_state(project, run_id, manifest)
    record_usage_event(project, run_id, "quarantine", "deterministic-core", "quarantined", 0)
    return {"status": "quarantined", "stage": "QUARANTINED_MAP_ONLY", "manifest": relative_to_project(manifest_path, project), "issue_id": issue_id}


def line_locations(parsed: dict[str, Any]) -> list[tuple[int, str]]:
    output = []
    for page in parsed.get("pages", []):
        for line in page.get("text", "").splitlines():
            stripped = line.strip()
            if stripped:
                output.append((int(page["page"]), stripped))
    return output


def parse_marker_fields(parts: list[str]) -> dict[str, str]:
    result = {}
    for part in parts:
        if "=" in part:
            key, value = part.split("=", 1)
            result[key.strip()] = value.strip()
    return result


def convert_specific_surface(value: float, unit: str) -> tuple[float, str | None]:
    normalized = unit.casefold().replace("²", "2").replace(" ", "")
    if normalized in {"m2/kg", "m^2/kg"}:
        return value, None
    if normalized in {"cm2/g", "cm^2/g"}:
        return value * 0.1, "m2/kg = cm2/g * 0.1"
    raise ValueError(f"unsupported specific-surface unit: {unit}")


def new_records(pdf_path: Path, pdf_sha: str, project: Path) -> dict[str, Any]:
    asset_path = relative_to_project(pdf_path, project)
    return {
        "schema_version": SCHEMA_VERSION,
        "platform_contract": PLATFORM_CONTRACT,
        "papers": [],
        "mats": [],
        "mixes": [],
        "assets": [{
            "asset_key": "asset-pdf-" + pdf_sha[:16],
            "paper_key": None,
            "asset_type": "paper_pdf",
            "relative_path": asset_path,
            "sha256": pdf_sha,
            "media_type": "application/pdf",
            "access_scope": "internal-read-only" if "internal_assets" in asset_path else "synthetic-fixture",
        }],
        "evidence_links": [],
        "candidates": [],
        "quarantined": [],
        "extensions": {},
    }


def add_evidence(
    records: dict[str, Any], record_type: str, record_key: str, field_path: str,
    page: int, snippet: str, original_value: Any = None, original_unit: str | None = None,
    formula: str | None = None, review_status: str = "confirmed",
) -> dict[str, Any]:
    evidence_key = "ev-" + sha256_bytes(stable_json([record_type, record_key, field_path, page, snippet]).encode("utf-8"))[:20]
    asset_key = records["assets"][0]["asset_key"]
    if not any(row["evidence_key"] == evidence_key for row in records["evidence_links"]):
        records["evidence_links"].append({
            "evidence_key": evidence_key,
            "record_type": record_type,
            "record_key": record_key,
            "field_path": field_path,
            "asset_key": asset_key,
            "page": page,
            "section": None,
            "table_number": None,
            "figure_number": None,
            "bbox": None,
            "snippet": snippet[:240],
            "snippet_sha256": sha256_bytes(snippet.encode("utf-8")),
        })
    return {
        "evidence_key": evidence_key,
        "original_value": original_value,
        "original_unit": original_unit,
        "formula": formula,
        "extraction_method": "deterministic-marker" if snippet.startswith(("MATERIAL|", "XRF|", "PSD|", "MIX|", "CURING|", "PERFORMANCE|")) else "model-assisted",
        "confidence": 1.0 if review_status == "confirmed" else 0.5,
        "review_status": review_status,
    }


def _escape_pointer_token(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _unescape_pointer_token(value: str) -> str:
    return value.replace("~1", "/").replace("~0", "~")


def flatten_json_leaves(value: Any, path: str = "") -> list[tuple[str, Any]]:
    """Flatten values while retaining empty containers for lossless assembly."""
    if isinstance(value, dict):
        if not value:
            return [(path or "/", {})]
        output: list[tuple[str, Any]] = []
        for key in sorted(value):
            output.extend(flatten_json_leaves(value[key], f"{path}/{_escape_pointer_token(str(key))}"))
        return output
    if isinstance(value, list):
        if not value:
            return [(path or "/", [])]
        output = []
        for index, item in enumerate(value):
            output.extend(flatten_json_leaves(item, f"{path}/{index}"))
        return output
    return [(path or "/", value)]


def set_json_pointer(root: Any, path: str, value: Any) -> Any:
    if path in {"", "/"}:
        return value
    tokens = [_unescape_pointer_token(token) for token in path.lstrip("/").split("/")]
    current = root
    for index, token in enumerate(tokens):
        last = index == len(tokens) - 1
        next_is_index = not last and tokens[index + 1].isdigit()
        if isinstance(current, dict):
            if last:
                current[token] = value
            else:
                if token not in current:
                    current[token] = [] if next_is_index else {}
                current = current[token]
        elif isinstance(current, list):
            if not token.isdigit():
                raise ValueError(f"list path token is not numeric: {path}")
            position = int(token)
            while len(current) <= position:
                current.append(None)
            if last:
                current[position] = value
            else:
                if current[position] is None:
                    current[position] = [] if next_is_index else {}
                current = current[position]
        else:
            raise ValueError(f"cannot descend into scalar at {path}")
    return root


def _provenance_for_leaf(record: dict[str, Any], path: str) -> dict[str, Any] | None:
    provenance = record.get("field_provenance") or {}
    if path in provenance:
        return provenance[path]
    prefixes = [key for key in provenance if path.startswith(key.rstrip("/") + "/")]
    if prefixes:
        return provenance[max(prefixes, key=len)]
    match = re.match(r"^/xrf_composition/rows/(\d+)/", path)
    if match:
        rows = (record.get("xrf_composition") or {}).get("rows") or []
        row_index = int(match.group(1))
        if row_index < len(rows):
            component = rows[row_index].get("component")
            return provenance.get(f"/xrf_composition/rows/{component}")
    return None


def _normalized_unit_for_leaf(record: dict[str, Any], path: str) -> str | None:
    if path.endswith("specific_surface_m2_kg"):
        return "m2/kg"
    if path.endswith(("d10_um", "d50_um", "d90_um")):
        return "um"
    if "/xrf_composition/rows/" in path and path.endswith("normalized_value"):
        return "wt.%"
    if path.endswith(("age_seconds", "duration_seconds")):
        return "s"
    match = re.match(r"^/modules/performance/(\d+)/value$", path)
    if match:
        rows = (record.get("modules") or {}).get("performance") or []
        row_index = int(match.group(1))
        if row_index < len(rows):
            return rows[row_index].get("unit")
    return None


def claim_bundle_from_records(records: dict[str, Any], input_hash: str, extraction_method: str, run_id: str | None = None) -> dict[str, Any]:
    """Convert provisional parser output into immutable atomic claims."""
    collections = ("papers", "mats", "mixes", "assets", "evidence_links", "candidates")
    key_fields = {
        "papers": "paper_key",
        "mats": "mat_key",
        "mixes": "mix_key",
        "assets": "asset_key",
        "evidence_links": "evidence_key",
        "candidates": "candidate_key",
    }
    subject_types = {
        "papers": "paper",
        "mats": "mat",
        "mixes": "mix",
        "assets": "asset",
        "evidence_links": "evidence",
        "candidates": "candidate",
    }
    default_paper_key = records.get("papers", [{}])[0].get("paper_key") if records.get("papers") else None
    claims: list[dict[str, Any]] = []
    record_counts: dict[str, int] = {}
    for collection in collections:
        rows = records.get(collection, []) or []
        record_counts[collection] = len(rows)
        for record_index, record in enumerate(rows):
            subject_key = str(record.get(key_fields[collection]) or f"{collection}-{record_index}")
            paper_key = record.get("paper_key") or default_paper_key
            for field_path, normalized_value in flatten_json_leaves(record):
                provenance = _provenance_for_leaf(record, field_path)
                value_status = "candidate" if collection == "candidates" else "metadata"
                if provenance:
                    value_status = "normalized" if provenance.get("formula") else "observed"
                evidence = []
                if provenance and provenance.get("evidence_key"):
                    evidence.append({"evidence_key": provenance["evidence_key"]})
                claim_identity = [run_id or "unbound", input_hash, collection, record_index, subject_key, field_path, normalized_value]
                claims.append({
                    "claim_id": "claim-" + sha256_bytes(stable_json(claim_identity).encode("utf-8"))[:24],
                    "record_collection": collection,
                    "record_index": record_index,
                    "paper_key": paper_key,
                    "subject_type": subject_types[collection],
                    "subject_key": subject_key,
                    "field_path": field_path,
                    "value_status": value_status,
                    "raw_value": provenance.get("original_value") if provenance else None,
                    "raw_unit": provenance.get("original_unit") if provenance else None,
                    "normalized_value": normalized_value,
                    "normalized_unit": _normalized_unit_for_leaf(record, field_path),
                    "evidence": evidence,
                    "extraction_method": provenance.get("extraction_method") if provenance else extraction_method,
                    "issues": [record.get("reason")] if collection == "candidates" and record.get("reason") else [],
                })
    return {
        "schema_version": CLAIM_SCHEMA_VERSION,
        "platform_contract": records.get("platform_contract", PLATFORM_CONTRACT),
        "input_hash": input_hash,
        "run_id": run_id,
        "record_counts": record_counts,
        "container_values": {
            "schema_version": records.get("schema_version", SCHEMA_VERSION),
            "platform_contract": records.get("platform_contract", PLATFORM_CONTRACT),
            "quarantined": records.get("quarantined", []),
            "extensions": records.get("extensions", {}),
        },
        "claims": claims,
    }


def assemble_claim_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    """Single-writer assembly from claims; no extractor writes canonical records."""
    output = dict(bundle.get("container_values") or {})
    record_counts = bundle.get("record_counts") or {}
    for collection, count in record_counts.items():
        output[collection] = [{} for _ in range(int(count))]
    ordered = sorted(
        bundle.get("claims", []),
        key=lambda row: (row["record_collection"], int(row["record_index"]), row["field_path"].count("/"), row["field_path"]),
    )
    for claim in ordered:
        collection = claim["record_collection"]
        index = int(claim["record_index"])
        current = output[collection][index]
        output[collection][index] = set_json_pointer(current, claim["field_path"], claim.get("normalized_value"))
    for collection in ("papers", "mats", "mixes", "assets", "evidence_links", "candidates"):
        output.setdefault(collection, [])
    output.setdefault("quarantined", [])
    output.setdefault("extensions", {})
    return output


def persist_claim_bundle(project: Path, run_id: str, path: Path, bundle: dict[str, Any]) -> dict[str, Any]:
    atomic_write_json(path, bundle)
    artifact = register_artifact(project, run_id, "claim_bundle", path, immutable=True)
    con = connect_control_db(project)
    try:
        now = utc_now()
        # A new immutable claim artifact supersedes the prior canonical claim
        # projection for this run.  Retaining both makes validation count stale
        # claims after a legitimate evidence-geometry-only rebuild.
        con.execute("DELETE FROM claims WHERE run_id=?", (run_id,))
        # Candidate-derived issues are another projection of the current claim
        # bundle.  A repaired run can legitimately remove candidates, so stale
        # AMBIGUOUS_RELATION rows must not survive the replacement.
        con.execute("DELETE FROM issues WHERE run_id=? AND code='AMBIGUOUS_RELATION'", (run_id,))
        for claim in bundle.get("claims", []):
            con.execute(
                """INSERT INTO claims(
                       claim_id,run_id,artifact_id,paper_key,subject_type,subject_key,field_path,value_status,
                       raw_value_json,raw_unit,normalized_value_json,normalized_unit,evidence_json,
                       extraction_method,issues_json,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(claim_id) DO NOTHING""",
                (
                    claim["claim_id"], run_id, artifact["artifact_id"], claim.get("paper_key"),
                    claim["subject_type"], claim["subject_key"], claim["field_path"], claim["value_status"],
                    stable_json(claim.get("raw_value")), claim.get("raw_unit"), stable_json(claim.get("normalized_value")),
                    claim.get("normalized_unit"), stable_json(claim.get("evidence", [])), claim["extraction_method"],
                    stable_json(claim.get("issues", [])), now,
                ),
            )
        candidate_claims: dict[str, list[dict[str, Any]]] = {}
        for claim in bundle.get("claims", []):
            if claim["subject_type"] == "candidate":
                candidate_claims.setdefault(claim["subject_key"], []).append(claim)
        for subject_key, rows in candidate_claims.items():
            candidate: dict[str, Any] = {}
            for claim in sorted(rows, key=lambda row: (row["field_path"].count("/"), row["field_path"])):
                candidate = set_json_pointer(candidate, claim["field_path"], claim.get("normalized_value"))
            if not candidate.get("reason"):
                continue
            issue_id = "issue-" + sha256_bytes(f"{run_id}|candidate|{subject_key}".encode("utf-8"))[:24]
            con.execute(
                """INSERT INTO issues(issue_id,run_id,code,severity,status,record_key,field_path,detail_json,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(issue_id) DO NOTHING""",
                (
                    issue_id,
                    run_id,
                    "AMBIGUOUS_RELATION",
                    "warning",
                    str(candidate.get("status") or "quarantined"),
                    candidate.get("record_key"),
                    candidate.get("field_path"),
                    stable_json({"candidate_key": subject_key, "reason": candidate["reason"]}),
                    now,
                ),
            )
        con.commit()
    finally:
        con.close()
    return artifact


def register_record_version(project: Path, run_id: str, records_artifact: dict[str, Any]) -> str:
    version_id = "version-" + sha256_bytes(f"{run_id}|{records_artifact['sha256']}".encode("utf-8"))[:24]
    con = connect_control_db(project)
    try:
        con.execute(
            """INSERT INTO record_versions(version_id,run_id,records_sha256,artifact_id,created_at)
               VALUES (?,?,?,?,?) ON CONFLICT(run_id,records_sha256) DO NOTHING""",
            (version_id, run_id, records_artifact["sha256"], records_artifact["artifact_id"], utc_now()),
        )
        con.commit()
    finally:
        con.close()
    return version_id


def extract_markers(parsed: dict[str, Any], pdf_path: Path, project: Path) -> dict[str, Any]:
    pdf_sha = sha256_path(pdf_path)
    records = new_records(pdf_path, pdf_sha, project)
    lines = line_locations(parsed)
    title = next((line.split("|", 1)[1] for _, line in lines if line.startswith("TITLE|")), parsed.get("metadata", {}).get("title") or pdf_path.stem)
    doi = next((normalize_doi(line.split("|", 1)[1]) for _, line in lines if line.startswith("DOI|")), None)
    year = next((int(line.split("|", 1)[1]) for _, line in lines if line.startswith("YEAR|")), None)
    synthetic = any("WAKG SYNTHETIC FIXTURE" in line for _, line in lines)
    paper_key = "paper-synthetic-001" if synthetic else "paper-" + (doi.replace("/", "-") if doi else pdf_sha[:16])
    paper = {"paper_key": paper_key, "title": title, "doi": doi, "year": year, "citation": None, "extensions": {}}
    records["papers"].append(paper)
    records["assets"][0]["paper_key"] = paper_key

    mat_by_custom: dict[str, dict[str, Any]] = {}
    mix_by_custom: dict[str, dict[str, Any]] = {}
    for page, line in lines:
        parts = line.split("|")
        tag = parts[0]
        if tag == "MATERIAL" and len(parts) >= 3:
            custom = parts[1]
            fields = parse_marker_fields(parts[2:])
            surface_raw = float(fields["specific_surface"]) if fields.get("specific_surface") else None
            surface_unit = fields.get("specific_surface_unit")
            surface_value, formula = convert_specific_surface(surface_raw, surface_unit) if surface_raw is not None and surface_unit else (None, None)
            mat_key = "mat-" + custom
            mat = {
                "schema_version": SCHEMA_VERSION,
                "mat_key": mat_key,
                "paper_key": paper_key,
                "custom_material_id": custom,
                "material_type": fields.get("material_type"),
                "source_type": "literature",
                "literature_title": title,
                "doi": doi,
                "year": year,
                "physical_properties": {
                    "specific_surface_m2_kg": surface_value,
                    "specific_surface_method": None,
                    "true_density_kg_m3": None,
                    "apparent_density_kg_m3": None,
                    "bulk_density_kg_m3": None,
                    "total_porosity_percent": None,
                    "open_porosity_percent": None,
                    "closed_porosity_percent": None,
                },
                "particle_size_distribution": {
                    "schema_version": SCHEMA_VERSION,
                    "reported_curve_type": None,
                    "points_asset_key": None,
                    "source_image_asset_key": None,
                    "d10_um": None,
                    "d50_um": float(fields["D50_um"]) if fields.get("D50_um") else None,
                    "d90_um": None,
                    "conversion_review": None,
                    "extensions": {},
                },
                "xrf_composition": {"schema_version": SCHEMA_VERSION, "unit": "wt.%", "rows": [], "original_total": None, "normalisation_candidate": None, "extensions": {}},
                "ftir_spectrum": None,
                "xrd_qxrd": None,
                "si29_nmr_spectrum": None,
                "al27_nmr_spectrum": None,
                "field_provenance": {},
                "extensions": {},
            }
            if surface_value is not None:
                mat["field_provenance"]["/physical_properties/specific_surface_m2_kg"] = add_evidence(records, "mat", mat_key, "/physical_properties/specific_surface_m2_kg", page, line, surface_raw, surface_unit, formula)
            if mat["particle_size_distribution"]["d50_um"] is not None:
                mat["field_provenance"]["/particle_size_distribution/d50_um"] = add_evidence(records, "mat", mat_key, "/particle_size_distribution/d50_um", page, line, fields["D50_um"], "um")
            records["mats"].append(mat)
            mat_by_custom[custom] = mat
        elif tag == "XRF" and len(parts) >= 3 and parts[1] in mat_by_custom:
            custom = parts[1]
            fields = parse_marker_fields(parts[2:])
            mat = mat_by_custom[custom]
            total = 0.0
            for oxide, raw in fields.items():
                value = float(raw)
                total += value
                mat["xrf_composition"]["rows"].append({"component": oxide, "original_value": value, "normalized_value": value, "status": "confirmed"})
                path = f"/xrf_composition/rows/{oxide}"
                mat["field_provenance"][path] = add_evidence(records, "mat", mat["mat_key"], path, page, line, value, "wt.%")
            mat["xrf_composition"]["original_total"] = round(total, 8)
        elif tag == "PSD" and len(parts) >= 3 and parts[1] in mat_by_custom:
            custom = parts[1]
            fields = parse_marker_fields(parts[2:])
            mat = mat_by_custom[custom]
            for name in ("D10_um", "D50_um", "D90_um"):
                if fields.get(name):
                    key = name.casefold()
                    mat["particle_size_distribution"][key] = float(fields[name])
                    path = f"/particle_size_distribution/{key}"
                    mat["field_provenance"][path] = add_evidence(records, "mat", mat["mat_key"], path, page, line, fields[name], "um")
            mat["particle_size_distribution"]["reported_curve_type"] = fields.get("curve_type")
        elif tag == "MIX" and len(parts) >= 3:
            custom = parts[1]
            fields = parse_marker_fields(parts[2:])
            mat_refs = ["mat-" + value for value in fields.get("mat_refs", "").split(",") if value]
            solids = []
            for pair in fields.get("masses_g", "").split(","):
                if ":" in pair:
                    mat_id, mass = pair.split(":", 1)
                    solids.append({"mat_key": "mat-" + mat_id, "mass_g": float(mass)})
            mix_key = "mix-" + custom
            mix = {
                "schema_version": SCHEMA_VERSION,
                "mix_key": mix_key,
                "paper_key": paper_key,
                "mix_family_key": None,
                "modules": {
                    "identity_source_specimen": {"custom_test_id": custom, "literature_source": {"title": title, "doi": doi, "year": year}, "specimen_type": fields.get("specimen_type"), "specimens": [], "extensions": {}},
                    "materials": {"mat_refs": mat_refs, "solid_materials": solids, "activator_total_mass_g": float(fields["activator_total_g"]) if fields.get("activator_total_g") else None, "activators": [], "fine_aggregate": None, "coarse_aggregate": None, "reported_mass_basis": fields.get("reported_mass_basis"), "platform_ratios": None, "conversion": None, "review_status": "confirmed", "extensions": {"mgo_addition_path": None}},
                    "mixing_curing": {"mixing": None, "forming": None, "demoulding": None, "curing_stages": [], "curing_route": None, "age_origin": "mixing_end", "extensions": {}},
                    "performance": [],
                    "characterizations": [],
                },
                "field_provenance": {},
                "extensions": {},
            }
            mix["field_provenance"]["/modules/materials"] = add_evidence(records, "mix", mix_key, "/modules/materials", page, line, fields.get("masses_g"), "g")
            records["mixes"].append(mix)
            mix_by_custom[custom] = mix
        elif tag == "CURING" and len(parts) >= 3 and parts[1] in mix_by_custom:
            custom = parts[1]
            fields = parse_marker_fields(parts[2:])
            mix = mix_by_custom[custom]
            stage = {
                "method": fields.get("method"),
                "temperature_C": float(fields["temperature_C"]) if fields.get("temperature_C") else None,
                "humidity_percent": float(fields["humidity_percent"]) if fields.get("humidity_percent") else None,
                "duration_seconds": float(fields["duration_d"]) * 86400 if fields.get("duration_d") else None,
            }
            mix["modules"]["mixing_curing"]["curing_stages"].append(stage)
            mix["field_provenance"]["/modules/mixing_curing/curing_stages"] = add_evidence(records, "mix", mix["mix_key"], "/modules/mixing_curing/curing_stages", page, line, fields.get("duration_d"), "d", "seconds = days * 86400")
        elif tag == "PERFORMANCE" and len(parts) >= 3 and parts[1] in mix_by_custom:
            custom = parts[1]
            fields = parse_marker_fields(parts[2:])
            mix = mix_by_custom[custom]
            index = len(mix["modules"]["performance"])
            observation = {
                "name": fields.get("name"),
                "value": float(fields["value"]),
                "unit": fields.get("unit"),
                "age_seconds": float(fields["age_d"]) * 86400,
                "specimen": fields.get("specimen"),
                "method": None,
            }
            mix["modules"]["performance"].append(observation)
            path = f"/modules/performance/{index}/value"
            mix["field_provenance"][path] = add_evidence(records, "mix", mix["mix_key"], path, page, line, fields.get("value"), fields.get("unit"))
            age_path = f"/modules/performance/{index}/age_seconds"
            mix["field_provenance"][age_path] = add_evidence(records, "mix", mix["mix_key"], age_path, page, line, fields.get("age_d"), "d", "seconds = days * 86400")
        elif tag == "AMBIGUITY" and len(parts) >= 3 and parts[1] in mix_by_custom:
            fields = parse_marker_fields(parts[2:])
            candidate = {
                "candidate_key": "cand-" + sha256_bytes(line.encode("utf-8"))[:16],
                "record_key": mix_by_custom[parts[1]]["mix_key"],
                "field_path": "/" + fields.get("field", "unknown").replace(".", "/"),
                "formal_value": None,
                "proposed_value": None,
                "reason": fields.get("reason"),
                "status": "quarantined",
                "blocks_record": False,
            }
            records["candidates"].append(candidate)
            records["quarantined"].append(candidate["candidate_key"])
    return records


def normalize_model_records(records: dict[str, Any], pdf_path: Path, project: Path) -> dict[str, Any]:
    required = {"papers", "mats", "mixes", "evidence_links"}
    missing = required - set(records)
    if missing:
        raise ValueError(f"model records missing keys: {sorted(missing)}")
    pdf_sha = sha256_path(pdf_path)
    if not records.get("assets"):
        records["assets"] = new_records(pdf_path, pdf_sha, project)["assets"]
    if records.get("papers"):
        for asset in records["assets"]:
            if not asset.get("paper_key"):
                asset["paper_key"] = records["papers"][0]["paper_key"]
    records.setdefault("schema_version", SCHEMA_VERSION)
    records.setdefault("platform_contract", PLATFORM_CONTRACT)
    records.setdefault("candidates", [])
    records.setdefault("quarantined", [])
    records.setdefault("extensions", {})
    return records


def create_exchange_sqlite(path: Path, records: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    os.close(fd)
    try:
        con = sqlite3.connect(temporary)
        con.executescript("""
        PRAGMA foreign_keys = ON;
        CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE papers (paper_key TEXT PRIMARY KEY, record_json TEXT NOT NULL CHECK(json_valid(record_json)));
        CREATE TABLE mats (mat_key TEXT PRIMARY KEY, paper_key TEXT NOT NULL REFERENCES papers(paper_key), record_json TEXT NOT NULL CHECK(json_valid(record_json)));
        CREATE TABLE mixes (mix_key TEXT PRIMARY KEY, paper_key TEXT NOT NULL REFERENCES papers(paper_key), record_json TEXT NOT NULL CHECK(json_valid(record_json)));
        CREATE TABLE assets (asset_key TEXT PRIMARY KEY, paper_key TEXT NOT NULL REFERENCES papers(paper_key), record_json TEXT NOT NULL CHECK(json_valid(record_json)));
        CREATE TABLE evidence_links (evidence_key TEXT PRIMARY KEY, record_type TEXT NOT NULL, record_key TEXT NOT NULL, asset_key TEXT, record_json TEXT NOT NULL CHECK(json_valid(record_json)));
        """)
        con.executemany("INSERT INTO schema_meta(key,value) VALUES (?,?)", [
            ("schema_version", SCHEMA_VERSION),
            ("platform_contract", PLATFORM_CONTRACT),
            ("status", "offline-exchange-not-live-database"),
        ])
        for paper in records["papers"]:
            con.execute("INSERT INTO papers VALUES (?,?)", (paper["paper_key"], stable_json(paper)))
        for mat in records["mats"]:
            con.execute("INSERT INTO mats VALUES (?,?,?)", (mat["mat_key"], mat["paper_key"], stable_json(mat)))
        for mix in records["mixes"]:
            con.execute("INSERT INTO mixes VALUES (?,?,?)", (mix["mix_key"], mix["paper_key"], stable_json(mix)))
        for asset in records["assets"]:
            con.execute("INSERT INTO assets VALUES (?,?,?)", (asset["asset_key"], asset["paper_key"], stable_json(asset)))
        for evidence in records["evidence_links"]:
            con.execute("INSERT INTO evidence_links VALUES (?,?,?,?,?)", (evidence["evidence_key"], evidence["record_type"], evidence["record_key"], evidence.get("asset_key"), stable_json(evidence)))
        con.commit()
        if con.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("foreign-key check failed while creating exchange database")
        con.close()
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def extract_document(project: Path, pdf_path: Path, run_id: str, model_records_path: Path | None = None) -> dict[str, Any]:
    bootstrap_project(project)
    paths = project_paths(project)
    started = time.perf_counter()
    pdf_path = pdf_path.resolve()
    if not pdf_path.exists():
        raise FileNotFoundError(pdf_path)
    run_dir = paths["runs"] / run_id
    con = connect_control_db(project)
    try:
        con.execute("DELETE FROM issues WHERE run_id=? AND code='QUARANTINED_MAP_ONLY'", (run_id,))
        con.commit()
    finally:
        con.close()
    manifest_path = run_dir / "manifest.json"
    records_path = run_dir / "records.json"
    db_path = run_dir / "exchange.sqlite3"
    claims_path = run_dir / "claims.json"
    pdf_sha = sha256_path(pdf_path)
    prior_parsed_artifact_id: str | None = None
    model_sha = sha256_path(model_records_path) if model_records_path else None
    input_fingerprint = stage_idempotency_key(
        "extract",
        {"pdf_sha256": pdf_sha, "model_records_sha256": model_sha},
        {"mode": "model-records" if model_records_path else "deterministic-markers"},
    )
    job_idempotency_key = sha256_bytes(f"{run_id}|{input_fingerprint}".encode("utf-8"))
    if manifest_path.exists() and records_path.exists():
        existing = load_json(manifest_path)
        artifact_ids = (existing.get("outputs", {}).get("artifact_ids") or {})
        prior_parsed_artifact_id = artifact_ids.get("parsed")
        cache_artifacts_current = all(
            artifact_is_current(project, artifact_ids.get(role))
            for role in ("parsed", "claims", "records", "sqlite")
        )
        claims_projection_current = False
        if claims_path.is_file() and artifact_ids.get("claims"):
            try:
                claim_count = len(load_json(claims_path).get("claims", []))
                con = connect_control_db(project)
                total = con.execute("SELECT COUNT(*) FROM claims WHERE run_id=?", (run_id,)).fetchone()[0]
                current = con.execute("SELECT COUNT(*) FROM claims WHERE run_id=? AND artifact_id=?", (run_id, artifact_ids["claims"])).fetchone()[0]
                con.close()
                claims_projection_current = total == claim_count and current == claim_count
            except (OSError, sqlite3.Error, ValueError, json.JSONDecodeError):
                claims_projection_current = False
        if (
            existing.get("input", {}).get("input_fingerprint") == input_fingerprint
            and existing.get("outputs", {}).get("records_sha256") == sha256_path(records_path)
            and cache_artifacts_current
            and claims_projection_current
        ):
            record_usage_event(project, run_id, "extract", "deterministic-core", "cache-hit", int((time.perf_counter() - started) * 1000))
            return {"status": "cache-hit", "manifest": relative_to_project(manifest_path, project), "records_sha256": sha256_path(records_path), "stage": existing["stage"]}
        if existing.get("input", {}).get("input_fingerprint") == input_fingerprint:
            invalidate_stale_job_cache(project, job_idempotency_key, "manifest artifact set or canonical claim projection is incomplete or hash-mismatched")

    queued = queue_job(project, run_id, "extract", job_idempotency_key, input_fingerprint, max_attempts=2)
    owner = f"pid-{os.getpid()}-{uuid.uuid4().hex[:12]}"
    leased = lease_job(project, job_idempotency_key, owner, lease_seconds=900)
    if leased is None:
        raise RuntimeError(f"extract job is unavailable or exhausted: {queued['job_id']}")
    if leased.get("cache_hit"):
        raise RuntimeError("control state reports a succeeded extraction but its canonical artifacts are missing or stale")

    run_dir.mkdir(parents=True, exist_ok=True)
    try:
        real_parsed_path = paths["internal"] / "parsed" / f"{pdf_sha}.json"
        synthetic_parsed_path = run_dir / "parsed.json"
        parsed_cache_current = prior_parsed_artifact_id is None or artifact_is_current(project, prior_parsed_artifact_id)
        if real_parsed_path.exists() and parsed_cache_current:
            parsed_path = real_parsed_path
            parsed = load_json(parsed_path)
        elif synthetic_parsed_path.exists() and parsed_cache_current:
            parsed_path = synthetic_parsed_path
            parsed = load_json(parsed_path)
        else:
            parsed = parse_pdf(pdf_path)
            marker_mode = any("WAKG SYNTHETIC FIXTURE" in page.get("text", "") for page in parsed["pages"])
            parsed_path = normalized_paper_path(paths, pdf_sha, marker_mode, run_id)
            atomic_write_json(parsed_path, parsed)
        parsed_artifact = register_artifact(project, run_id, "normalized_paper", parsed_path, immutable=True)

        mapping_path = run_dir / "mapping-manifest.json"
        mapping = load_json(mapping_path) if mapping_path.exists() else None
        if mapping is not None and mapping.get("input_pdf_sha256") != pdf_sha:
            raise ValueError("mapping manifest PDF hash differs from frozen input")

        if model_records_path:
            provisional_records = normalize_model_records(load_json(model_records_path), pdf_path, project)
            extraction_method = "bounded-semantic-stage"
        else:
            provisional_records = extract_markers(parsed, pdf_path, project)
            extraction_method = "deterministic-marker"

        claim_bundle = claim_bundle_from_records(provisional_records, input_fingerprint, extraction_method, run_id=run_id)
        claims_artifact = persist_claim_bundle(project, run_id, claims_path, claim_bundle)
        records = assemble_claim_bundle(claim_bundle)
        if stable_json(records) != stable_json(provisional_records):
            raise ValueError("claim assembler did not reproduce provisional records exactly")

        atomic_write_json(records_path, records)
        records_artifact = register_artifact(project, run_id, "canonical_records", records_path, immutable=True)
        version_id = register_record_version(project, run_id, records_artifact)
        create_exchange_sqlite(db_path, records)
        exchange_artifact = register_artifact(project, run_id, "exchange_sqlite", db_path, immutable=True)
        records_sha = records_artifact["sha256"]
        manifest = {
            "schema_version": 1,
            "tool_version": TOOL_VERSION,
            "control_schema_version": CONTROL_SCHEMA_VERSION,
            "claim_schema_version": CLAIM_SCHEMA_VERSION,
            "platform_contract": PLATFORM_CONTRACT,
            "run_id": run_id,
            "stage": "EXTRACTED",
            "terminal_exception": None,
            "input": {
                "pdf_path": relative_to_project(pdf_path, project),
                "pdf_sha256": pdf_sha,
                "model_records_sha256": model_sha,
                "input_fingerprint": input_fingerprint,
                "job_idempotency_key": job_idempotency_key,
                "parser_version": PARSER_VERSION,
                "extraction_policy_version": EXTRACTION_POLICY_VERSION,
            },
            "job": {"job_id": leased["job_id"], "attempt": leased["attempt_count"]},
            "parsed_asset": relative_to_project(parsed_path, project),
            "mapping": mapping,
            "source_integrity": (mapping or {}).get("source_integrity"),
            "outputs": {
                "claims": relative_to_project(claims_path, project),
                "claims_sha256": claims_artifact["sha256"],
                "records": relative_to_project(records_path, project),
                "records_sha256": records_sha,
                "record_version_id": version_id,
                "sqlite": relative_to_project(db_path, project),
                "sqlite_sha256": exchange_artifact["sha256"],
                "artifact_ids": {
                    "parsed": parsed_artifact["artifact_id"],
                    "claims": claims_artifact["artifact_id"],
                    "records": records_artifact["artifact_id"],
                    "sqlite": exchange_artifact["artifact_id"],
                },
            },
            "budgets": DEFAULT_BUDGET,
            "repair_history": [],
        }
        atomic_write_json(manifest_path, manifest)
        register_artifact(project, run_id, "run_manifest", manifest_path, immutable=False)
        update_run_state(project, run_id, manifest)
        complete_job(project, leased["job_id"], owner, records_artifact["artifact_id"])
        record_usage_event(project, run_id, "extract", "deterministic-core", "succeeded", int((time.perf_counter() - started) * 1000))
        return {"status": "extracted", "stage": "EXTRACTED", "manifest": relative_to_project(manifest_path, project), "records_sha256": records_sha}
    except Exception as exc:
        fail_job(
            project,
            leased["job_id"],
            owner,
            {"type": type(exc).__name__, "detail": str(exc)[:500]},
            retryable=isinstance(exc, (OSError, TimeoutError, sqlite3.OperationalError)),
        )
        record_usage_event(project, run_id, "extract", "deterministic-core", "failed", int((time.perf_counter() - started) * 1000))
        raise


def update_run_state(project: Path, run_id: str, manifest: dict[str, Any]) -> None:
    now = utc_now()
    con = connect_control_db(project)
    try:
        con.execute(
            """INSERT INTO runs(run_id,stage,manifest_path,records_sha256,terminal_exception_json,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(run_id) DO UPDATE SET
                 stage=excluded.stage,
                 manifest_path=excluded.manifest_path,
                 records_sha256=excluded.records_sha256,
                 terminal_exception_json=excluded.terminal_exception_json,
                 updated_at=excluded.updated_at""",
            (
                run_id,
                manifest["stage"],
                f"runs/{run_id}/manifest.json",
                manifest.get("outputs", {}).get("records_sha256"),
                stable_json(manifest.get("terminal_exception")) if manifest.get("terminal_exception") is not None else None,
                now,
                now,
            ),
        )
        con.commit()
    finally:
        con.close()
    sync_state_projection(project)


def provenance_requirements(records: dict[str, Any]) -> list[tuple[str, str, str, dict[str, Any]]]:
    requirements = []
    for mat in records.get("mats", []):
        provenance = mat.get("field_provenance", {})
        for key, value in (mat.get("physical_properties") or {}).items():
            if value is not None and key != "specific_surface_method":
                requirements.append(("mat", mat["mat_key"], f"/physical_properties/{key}", provenance))
        psd = mat.get("particle_size_distribution") or {}
        for key in ("d10_um", "d50_um", "d90_um"):
            if psd.get(key) is not None:
                requirements.append(("mat", mat["mat_key"], f"/particle_size_distribution/{key}", provenance))
        for row in (mat.get("xrf_composition") or {}).get("rows", []):
            if row.get("normalized_value") is not None:
                requirements.append(("mat", mat["mat_key"], f"/xrf_composition/rows/{row.get('component')}", provenance))
    for mix in records.get("mixes", []):
        provenance = mix.get("field_provenance", {})
        modules = mix.get("modules", {})
        if (modules.get("materials") or {}).get("mat_refs"):
            requirements.append(("mix", mix["mix_key"], "/modules/materials", provenance))
        if (modules.get("mixing_curing") or {}).get("curing_stages"):
            requirements.append(("mix", mix["mix_key"], "/modules/mixing_curing/curing_stages", provenance))
        for index, observation in enumerate(modules.get("performance") or []):
            if observation.get("value") is not None:
                requirements.append(("mix", mix["mix_key"], f"/modules/performance/{index}/value", provenance))
            if observation.get("age_seconds") is not None:
                requirements.append(("mix", mix["mix_key"], f"/modules/performance/{index}/age_seconds", provenance))
    return requirements


def validate_run(project: Path, run_id: str) -> dict[str, Any]:
    paths = project_paths(project)
    run_dir = paths["runs"] / run_id
    manifest_path = run_dir / "manifest.json"
    records_path = run_dir / "records.json"
    db_path = run_dir / "exchange.sqlite3"
    claims_path = run_dir / "claims.json"
    checks: list[dict[str, Any]] = []
    defects: list[dict[str, Any]] = []

    def check(name: str, passed: bool, detail: str = "") -> None:
        row = {"name": name, "status": "PASS" if passed else "FAIL", "detail": detail}
        checks.append(row)
        if not passed:
            defects.append(row)

    check("manifest_exists", manifest_path.exists(), relative_to_project(manifest_path, project))
    check("control_state_exists", paths["state_db"].exists(), relative_to_project(paths["state_db"], project))
    check("claims_exists", claims_path.exists(), relative_to_project(claims_path, project))
    check("records_exists", records_path.exists(), relative_to_project(records_path, project))
    check("sqlite_exists", db_path.exists(), relative_to_project(db_path, project))
    if defects:
        return {"schema_version": 1, "run_id": run_id, "verdict": "REJECT", "checks": checks, "defects": defects, "defect_fingerprint": sha256_bytes(stable_json(defects).encode("utf-8"))}

    manifest = load_json(manifest_path)
    records = load_json(records_path)
    claims = load_json(claims_path)
    check("platform_contract", manifest.get("platform_contract") == PLATFORM_CONTRACT and records.get("platform_contract") == PLATFORM_CONTRACT)
    check("claims_hash", manifest.get("outputs", {}).get("claims_sha256") == sha256_path(claims_path))
    try:
        assembled = assemble_claim_bundle(claims)
        check("claim_assembly_roundtrip", stable_json(assembled) == stable_json(records))
    except Exception as exc:
        check("claim_assembly_roundtrip", False, str(exc))
    check("records_hash", manifest.get("outputs", {}).get("records_sha256") == sha256_path(records_path))
    check("has_one_paper", len(records.get("papers", [])) == 1, f"count={len(records.get('papers', []))}")
    check("has_mat", len(records.get("mats", [])) >= 1, f"count={len(records.get('mats', []))}")
    check("has_mix", len(records.get("mixes", [])) >= 1, f"count={len(records.get('mixes', []))}")

    paper_keys = {row.get("paper_key") for row in records.get("papers", [])}
    mat_keys = {row.get("mat_key") for row in records.get("mats", [])}
    asset_by_key = {row.get("asset_key"): row for row in records.get("assets", [])}
    evidence_by_key = {row.get("evidence_key"): row for row in records.get("evidence_links", [])}
    check("unique_paper_keys", len(paper_keys) == len(records.get("papers", [])))
    check("unique_mat_keys", len(mat_keys) == len(records.get("mats", [])))
    check("mat_paper_references", all(row.get("paper_key") in paper_keys for row in records.get("mats", [])))
    check("mix_paper_references", all(row.get("paper_key") in paper_keys for row in records.get("mixes", [])))
    bad_refs = []
    for mix in records.get("mixes", []):
        for ref in (mix.get("modules", {}).get("materials", {}) or {}).get("mat_refs", []):
            if ref not in mat_keys:
                bad_refs.append({"mix_key": mix.get("mix_key"), "mat_key": ref})
    check("mix_mat_references", not bad_refs, stable_json(bad_refs))

    provenance_defects = []
    for record_type, record_key, path, provenance in provenance_requirements(records):
        item = provenance.get(path)
        if not item:
            provenance_defects.append({"record_key": record_key, "field_path": path, "reason": "missing_provenance"})
            continue
        evidence = evidence_by_key.get(item.get("evidence_key"))
        if not evidence:
            provenance_defects.append({"record_key": record_key, "field_path": path, "reason": "missing_evidence"})
        elif evidence.get("record_type") != record_type or evidence.get("record_key") != record_key or evidence.get("field_path") != path:
            provenance_defects.append({"record_key": record_key, "field_path": path, "reason": "evidence_mismatch"})
        elif not isinstance(evidence.get("page"), int) or evidence["page"] < 1:
            provenance_defects.append({"record_key": record_key, "field_path": path, "reason": "invalid_page"})
    check("field_provenance", not provenance_defects, stable_json(provenance_defects))

    bad_assets = []
    for evidence in records.get("evidence_links", []):
        if evidence.get("asset_key") not in asset_by_key:
            bad_assets.append({"evidence_key": evidence.get("evidence_key"), "reason": "unknown_asset"})
    for asset in records.get("assets", []):
        raw_path = Path(asset.get("relative_path", ""))
        candidate = raw_path if raw_path.is_absolute() else paths["root"] / raw_path
        if not candidate.exists():
            bad_assets.append({"asset_key": asset.get("asset_key"), "reason": "asset_missing"})
        elif sha256_path(candidate) != asset.get("sha256"):
            bad_assets.append({"asset_key": asset.get("asset_key"), "reason": "sha256_mismatch"})
    check("asset_links_and_hashes", not bad_assets, stable_json(bad_assets))

    blocking_candidates = [row for row in records.get("candidates", []) if row.get("blocks_record") and row.get("status") != "confirmed"]
    check("no_unconfirmed_blocking_candidates", not blocking_candidates, stable_json(blocking_candidates))

    xrf_defects = []
    for mat in records.get("mats", []):
        xrf = mat.get("xrf_composition") or {}
        total = xrf.get("original_total")
        if total is not None and total > 100.0 and not xrf.get("normalisation_candidate"):
            xrf_defects.append({"mat_key": mat.get("mat_key"), "original_total": total, "reason": "normalization_candidate_required"})
    check("xrf_total_gate", not xrf_defects, stable_json(xrf_defects))

    conversion_defects = []
    for mat in records.get("mats", []):
        item = (mat.get("field_provenance") or {}).get("/physical_properties/specific_surface_m2_kg")
        if item and item.get("formula") == "m2/kg = cm2/g * 0.1":
            expected = float(item["original_value"]) * 0.1
            actual = float((mat.get("physical_properties") or {})["specific_surface_m2_kg"])
            if abs(expected - actual) > 1e-9 or abs(actual / 0.1 - float(item["original_value"])) > 1e-9:
                conversion_defects.append({"mat_key": mat.get("mat_key"), "expected": expected, "actual": actual})
    check("unit_conversion_roundtrip", not conversion_defects, stable_json(conversion_defects))

    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        integrity = con.execute("PRAGMA integrity_check").fetchall()
        foreign = con.execute("PRAGMA foreign_key_check").fetchall()
        con.close()
        check("sqlite_integrity", integrity == [("ok",)], stable_json(integrity))
        check("sqlite_foreign_keys", not foreign, stable_json(foreign))
    except sqlite3.Error as exc:
        check("sqlite_readable", False, str(exc))

    try:
        control = sqlite3.connect(f"file:{paths['state_db']}?mode=ro", uri=True)
        control_integrity = control.execute("PRAGMA integrity_check").fetchall()
        control_foreign = control.execute("PRAGMA foreign_key_check").fetchall()
        db_claim_count = control.execute("SELECT COUNT(*) FROM claims WHERE run_id=?", (run_id,)).fetchone()[0]
        db_issue_count = control.execute("SELECT COUNT(*) FROM issues WHERE run_id=?", (run_id,)).fetchone()[0]
        record_version_count = control.execute("SELECT COUNT(*) FROM record_versions WHERE run_id=? AND records_sha256=?", (run_id, sha256_path(records_path))).fetchone()[0]
        control.close()
        check("control_sqlite_integrity", control_integrity == [("ok",)], stable_json(control_integrity))
        check("control_sqlite_foreign_keys", not control_foreign, stable_json(control_foreign))
        check("claims_persisted", db_claim_count == len(claims.get("claims", [])), f"db={db_claim_count},artifact={len(claims.get('claims', []))}")
        source_integrity = manifest.get("source_integrity") or {}
        source_issue_count = 1 if source_integrity.get("status") else 0
        expected_issue_count = sum(1 for row in records.get("candidates", []) if row.get("reason")) + source_issue_count
        check("issues_persisted_once", db_issue_count == expected_issue_count, f"db={db_issue_count},expected={expected_issue_count}")
        check("record_version_persisted", record_version_count == 1, f"count={record_version_count}")
    except sqlite3.Error as exc:
        check("control_sqlite_readable", False, str(exc))

    snapshot = manifest.get("snapshot")
    if manifest.get("stage") == "ACCEPTED" and snapshot:
        snapshot_path = paths["root"] / snapshot["path"]
        check("snapshot_manifest_exists", snapshot_path.exists(), snapshot["path"])
        if snapshot_path.exists():
            check("snapshot_manifest_hash", sha256_path(snapshot_path) == snapshot.get("sha256"))

    verdict = "PASS" if not defects else "REJECT"
    return {
        "schema_version": 1,
        "run_id": run_id,
        "verdict": verdict,
        "checks": checks,
        "defects": defects,
        "quarantined_candidates": records.get("quarantined", []),
        "defect_fingerprint": sha256_bytes(stable_json(defects).encode("utf-8")),
    }


def inspect_supplement_zip(
    path: Path,
    asset_root: Path,
    max_members: int = 64,
    max_member_bytes: int = 10 * 1024 * 1024,
    max_total_bytes: int = 100 * 1024 * 1024,
    max_ratio: float = 100.0,
) -> dict[str, Any]:
    """Inspect-only ZIP safety gate. It never extracts members."""
    root = asset_root.resolve()
    findings: list[str] = []
    names: set[str] = set()
    total_bytes = 0
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > max_members:
                findings.append("ZIP_MEMBER_COUNT_LIMIT")
            for info in infos:
                raw = unicodedata.normalize("NFC", info.filename).replace("\\", "/")
                normalized = PurePosixPath(raw)
                canonical = unicodedata.normalize("NFC", normalized.as_posix()).casefold()
                is_drive_qualified = bool(re.match(r"^[A-Za-z]:", raw))
                if normalized.is_absolute() or is_drive_qualified or ".." in normalized.parts or canonical in {"", "."}:
                    findings.append("ZIP_PATH_TRAVERSAL")
                else:
                    target = (root / Path(*normalized.parts)).resolve()
                    try:
                        target.relative_to(root)
                    except ValueError:
                        findings.append("ZIP_PATH_TRAVERSAL")
                if canonical in names:
                    findings.append("ZIP_DUPLICATE_MEMBER")
                names.add(canonical)
                if info.flag_bits & 0x1:
                    findings.append("ZIP_ENCRYPTED_UNSUPPORTED")
                if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                    findings.append("ZIP_COMPRESSION_UNSUPPORTED")
                if info.file_size > max_member_bytes:
                    findings.append("ZIP_EXPANSION_LIMIT")
                total_bytes += info.file_size
                ratio = info.file_size / max(info.compress_size, 1)
                if ratio > max_ratio:
                    findings.append("ZIP_COMPRESSION_RATIO_LIMIT")
            if total_bytes > max_total_bytes:
                findings.append("ZIP_TOTAL_SIZE_LIMIT")
    except (OSError, zipfile.BadZipFile) as exc:
        findings.append("ZIP_INVALID")
        return {"status": "QUARANTINED", "findings": findings, "detail": str(exc)[:300], "extracted": False}
    return {"status": "QUARANTINED" if findings else "SAFE", "findings": sorted(set(findings)), "detail": None, "extracted": False}


def _batch_semantic_hash(members: list[dict[str, Any]]) -> str:
    keys = ("logical_id", "canonical_id", "content_sha256", "status", "records_sha256", "snapshot_sha256", "reason", "paper_keys")
    semantic = [{key: row.get(key) for key in keys} for row in members]
    return sha256_bytes(stable_json(sorted(semantic, key=lambda row: str(row["logical_id"]))).encode("utf-8"))


def _append_batch_event(state: dict[str, Any], operation: str, logical_id: str | None = None, **detail: Any) -> None:
    """Append a deterministic, local execution event; no external action is implied."""
    event = {"sequence": len(state["events"]) + 1, "event": "operation", "operation": operation}
    if logical_id is not None:
        event["logical_id"] = logical_id
    event.update(detail)
    state["events"].append(event)


def _derive_batch_counters(events: list[dict[str, Any]]) -> dict[str, int]:
    """Derive accounting solely from persisted execution events, never defaults."""
    counters = {
        "network_requests": 0,
        "wakg_accesses": 0,
        "pipeline_model_calls": 0,
        "duplicate_paid_model_calls": 0,
        "visual_pages_inspected": 0,
    }
    for event in events:
        external = event.get("external_attempt")
        if external == "network":
            counters["network_requests"] += 1
        elif external == "wakg":
            counters["wakg_accesses"] += 1
        elif external == "model":
            counters["pipeline_model_calls"] += 1
            if event.get("duplicate"):
                counters["duplicate_paid_model_calls"] += 1
        counters["visual_pages_inspected"] += int(event.get("visual_pages_inspected") or 0)
    return counters


def _batch_operation_counts(events: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in events:
        operation = str(event.get("operation") or event.get("event") or "unknown")
        counts[operation] = counts.get(operation, 0) + 1
    return dict(sorted(counts.items()))


def _control_counts(project: Path) -> dict[str, int]:
    con = connect_control_db(project)
    try:
        return {
            "jobs": int(con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]),
            "artifacts": int(con.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]),
            "record_versions": int(con.execute("SELECT COUNT(*) FROM record_versions").fetchone()[0]),
            "snapshots": int(con.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]),
        }
    finally:
        con.close()


def _batch_invariants(project: Path, members: list[dict[str, Any]], runtime: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    accepted = [row for row in members if row.get("status") in {"ACCEPTED", "ACCEPTED_REFERENCE"}]
    duplicates = [row for row in members if row.get("status") == "DUPLICATE_ALIAS"]
    identities = {}
    for member in members:
        logical_id = str(member["logical_id"])
        identities[logical_id] = {
            "content_sha256": member.get("content_sha256"),
            "status": member.get("status"),
            "records_sha256": member.get("records_sha256"),
            "snapshot_sha256": member.get("snapshot_sha256"),
            "snapshot_id": member.get("snapshot_id"),
            "record_version_id": member.get("record_version_id"),
            "extract_job_id": member.get("extract_job_id"),
            "batch_member_job_id": (runtime.get(logical_id) or {}).get("batch_member_job_id"),
        }
    duplicate_work = [row["logical_id"] for row in duplicates if row.get("extract_job_id") or row.get("record_version_id") or row.get("snapshot_id")]
    control_counts = _control_counts(project)
    expected_final_control_counts = dict(control_counts)
    # Registering the batch manifest is the only remaining canonical write.
    expected_final_control_counts["artifacts"] += 1
    return {
        "member_count": len(members),
        "accepted_member_ids": sorted(row["logical_id"] for row in accepted),
        "duplicate_alias_ids": sorted(row["logical_id"] for row in duplicates),
        "duplicate_has_no_extract_publish_version_snapshot": not duplicate_work,
        "duplicate_work_members": sorted(duplicate_work),
        "unique_accepted_records": len({row.get("records_sha256") for row in accepted}),
        "unique_accepted_snapshots": len({row.get("snapshot_sha256") for row in accepted}),
        "local_extract_job_ids": sorted(row["extract_job_id"] for row in accepted if row.get("extract_job_id")),
        "local_record_version_ids": sorted(row["record_version_id"] for row in accepted if row.get("status") == "ACCEPTED" and row.get("record_version_id")),
        "local_snapshot_ids": sorted(row["snapshot_id"] for row in accepted if row.get("status") == "ACCEPTED" and row.get("snapshot_id")),
        "member_identities": identities,
        "operation_counts": _batch_operation_counts(events),
        "control_counts_before_manifest": control_counts,
        "expected_final_control_counts": expected_final_control_counts,
    }


def verify_accepted_reference(source_project: Path, run_id: str, pdf_path: Path) -> dict[str, Any]:
    """Verify an immutable accepted run before admitting it to an offline batch."""
    source_project = source_project.resolve()
    validation = validate_run(source_project, run_id)
    if validation.get("verdict") != "PASS":
        raise ValueError(f"accepted reference does not validate: {run_id}")
    manifest_path = project_paths(source_project)["runs"] / run_id / "manifest.json"
    manifest = load_json(manifest_path)
    snapshot = manifest.get("snapshot") or {}
    snapshot_path = source_project / str(snapshot.get("path") or "")
    if manifest.get("stage") != "ACCEPTED" or not snapshot_path.is_file() or sha256_path(snapshot_path) != snapshot.get("sha256"):
        raise ValueError(f"accepted reference snapshot is invalid: {run_id}")
    pdf_path = pdf_path.resolve()
    if sha256_path(pdf_path) != manifest.get("input", {}).get("pdf_sha256"):
        raise ValueError(f"accepted reference PDF hash is invalid: {run_id}")
    records_path = source_project / manifest["outputs"]["records"]
    records = load_json(records_path)
    paper_keys = sorted(row["paper_key"] for row in records.get("papers", []))
    if len(paper_keys) != 1:
        raise ValueError(f"accepted reference must contain exactly one paper: {run_id}")
    return {
        "content_sha256": sha256_path(pdf_path),
        "records_sha256": manifest["outputs"]["records_sha256"],
        "snapshot_sha256": snapshot["sha256"],
        "snapshot_path": relative_to_project(snapshot_path, source_project),
        "snapshot_id": snapshot.get("snapshot_id"),
        "record_version_id": manifest.get("outputs", {}).get("record_version_id"),
        "source_records_path": str(records_path.resolve()),
        "paper_keys": paper_keys,
        "source_run_id": run_id,
    }


def _classify_local_pdf(path: Path, visual_page_budget: int) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size < 5 or path.read_bytes()[:5] != b"%PDF-":
        return {"status": "ACCESS_FAILED", "reason": "invalid_or_truncated_pdf", "visual_pages_inspected": 0}
    try:
        parsed = parse_pdf(path)
    except Exception as exc:
        return {"status": "ACCESS_FAILED", "reason": f"pdf_parse_failed:{type(exc).__name__}", "visual_pages_inspected": 0}
    if not any(page.get("text_characters", 0) for page in parsed.get("pages", [])):
        inspected = min(len(parsed.get("pages", [])), max(0, visual_page_budget))
        return {"status": "VISUAL_REVIEW_PENDING", "reason": "image_only_pdf_ocr_unavailable", "visual_pages_inspected": inspected}
    return {"status": "READY", "reason": None, "visual_pages_inspected": 0}


def _batch_member_path(project: Path, run_id: str, logical_id: str) -> Path:
    return project_paths(project)["runs"] / run_id / "batch-members" / f"{logical_id}.json"


def run_offline_batch(
    project: Path,
    run_id: str,
    inputs: list[dict[str, Any]],
    visual_page_budget: int = 3,
    stage_config: dict[str, Any] | None = None,
    crash_after_document: int | None = None,
    crash_after_staging: bool = False,
    forbidden_external_attempt: str | None = None,
) -> dict[str, Any]:
    """Offline, resumable batch gate with paper-local outcomes and no network/model path."""
    bootstrap_project(project)
    paths = project_paths(project)
    run_dir = paths["runs"] / run_id
    state_path = run_dir / "batch-state.json"
    manifest_path = run_dir / "batch-manifest.json"
    config_hash = stage_idempotency_key("offline_batch", {}, stage_config or {})
    state = load_json(state_path) if state_path.exists() else {"schema_version": 2, "run_id": run_id, "config_hash": config_hash, "members": {}, "member_runtime": {}, "events": []}
    state.setdefault("member_runtime", {})
    state.setdefault("events", [])
    state.pop("counters", None)
    if forbidden_external_attempt is not None:
        if forbidden_external_attempt not in {"network", "wakg", "model"}:
            raise ValueError("forbidden_external_attempt must be network, wakg, or model")
        _append_batch_event(state, "forbidden_external_attempt", external_attempt=forbidden_external_attempt, duplicate=False)
        atomic_write_json(state_path, state)
    if state.get("config_hash") != config_hash:
        invalidated = [logical_id for logical_id, member in state["members"].items() if member.get("local_run_id")]
        for logical_id in invalidated:
            state["members"].pop(logical_id)
            state["member_runtime"].pop(logical_id, None)
        state["config_hash"] = config_hash
        _append_batch_event(state, "dependent_local_member_invalidated", members=invalidated, config_hash=config_hash)
        atomic_write_json(state_path, state)
    by_id = {str(row["logical_id"]): row for row in inputs}
    if len(by_id) != len(inputs):
        raise ValueError("batch logical_id values must be unique")
    preferred: dict[str, str] = {}
    for row in inputs:
        if row.get("kind", "paper") == "supplement":
            continue
        if row.get("canonical", False):
            preferred[str(row["content_sha256"])] = str(row["logical_id"])
    processed_this_call = 0
    for logical_id in sorted(by_id):
        if logical_id in state["members"]:
            _append_batch_event(state, "cache_hit", logical_id)
            atomic_write_json(state_path, state)
            continue
        row = by_id[logical_id]
        kind = row.get("kind", "paper")
        source_text = {"value": str(row.get("source_text") or ""), "trust": "untrusted_evidence"}
        if kind == "supplement":
            zip_result = inspect_supplement_zip(Path(row["path"]), run_dir / "supplement-assets", **(row.get("zip_limits") or {}))
            member = {"logical_id": logical_id, "canonical_id": None, "content_sha256": row["content_sha256"], "status": zip_result["status"], "records_sha256": None, "snapshot_sha256": None, "paper_keys": [], "reason": ";".join(zip_result["findings"]), "source_text": source_text, "zip": zip_result}
            _append_batch_event(state, "zip_inspect", logical_id)
        else:
            canonical_id = preferred.get(str(row["content_sha256"]), logical_id)
            if canonical_id != logical_id:
                member = {"logical_id": logical_id, "canonical_id": canonical_id, "content_sha256": row["content_sha256"], "status": "DUPLICATE_ALIAS", "records_sha256": None, "snapshot_sha256": None, "paper_keys": [], "reason": "duplicate_content_alias", "source_text": source_text}
                _append_batch_event(state, "duplicate_alias", logical_id, canonical_id=canonical_id)
            elif row.get("accepted_reference"):
                reference = verify_accepted_reference(Path(row["source_project"]), str(row["source_run_id"]), Path(row["path"]))
                member = {"logical_id": logical_id, "canonical_id": logical_id, "status": "ACCEPTED_REFERENCE", "reason": None, "source_text": source_text, "source_project": str(Path(row["source_project"]).resolve()), "source_pdf": str(Path(row["path"]).resolve()), "source_run_id": str(row["source_run_id"]), **reference}
                _append_batch_event(state, "accepted_reference_verify", logical_id)
            else:
                path = Path(row["path"])
                classified = _classify_local_pdf(path, visual_page_budget)
                _append_batch_event(state, "local_pdf_classify", logical_id, visual_pages_inspected=classified["visual_pages_inspected"])
                if classified["status"] == "READY":
                    extraction_run_id = f"{run_id}-{logical_id}-{config_hash[:8]}"
                    extracted = extract_document(project, path, extraction_run_id)
                    _append_batch_event(state, "local_extract", logical_id)
                    validation = validate_run(project, extraction_run_id)
                    if validation["verdict"] != "PASS":
                        raise ValueError(f"local batch member did not validate: {logical_id}")
                    accepted = write_validation(project, extraction_run_id, validation, accept=True)
                    records = load_json(project_paths(project)["runs"] / extraction_run_id / "records.json")
                    member = {"logical_id": logical_id, "canonical_id": logical_id, "content_sha256": row["content_sha256"], "status": "ACCEPTED", "records_sha256": accepted["outputs"]["records_sha256"], "snapshot_sha256": accepted["snapshot"]["sha256"], "snapshot_id": accepted["snapshot"].get("snapshot_id"), "record_version_id": accepted["outputs"].get("record_version_id"), "extract_job_id": accepted.get("job", {}).get("job_id"), "paper_keys": sorted(item["paper_key"] for item in records["papers"]), "reason": None, "source_text": source_text, "local_run_id": extraction_run_id, "source_records_path": str((project_paths(project)["runs"] / extraction_run_id / "records.json").resolve())}
                    _append_batch_event(state, "local_accept_publish", logical_id)
                else:
                    member = {"logical_id": logical_id, "canonical_id": logical_id, "content_sha256": row["content_sha256"], "status": classified["status"], "records_sha256": None, "snapshot_sha256": None, "paper_keys": [], "reason": classified["reason"], "source_text": source_text, "visual_pages_inspected": classified["visual_pages_inspected"]}
        if member.get("status") in {"ACCEPTED", "ACCEPTED_REFERENCE"}:
            source_records = Path(str(member.pop("source_records_path")))
            batch_records_path = run_dir / "batch-records" / f"{logical_id}.json"
            _copy_immutable(source_records, batch_records_path)
            batch_records_artifact = register_artifact(project, run_id, "batch_records", batch_records_path, immutable=True)
            member["batch_records_path"] = relative_to_project(batch_records_path, project)
            member["batch_records_artifact_id"] = batch_records_artifact["artifact_id"]
            if sha256_path(batch_records_path) != member["records_sha256"]:
                raise ValueError(f"batch records hash differs: {logical_id}")
            _append_batch_event(state, "batch_records_materialized", logical_id)
        member_path = _batch_member_path(project, run_id, logical_id)
        atomic_write_json(member_path, member)
        artifact = register_artifact(project, run_id, "batch_member", member_path, immutable=True)
        key = stage_idempotency_key("offline_batch_member", {"member_sha256": artifact["sha256"]}, {"logical_id": logical_id})
        queued = queue_job(project, run_id, "offline_batch_member", key, artifact["sha256"], max_attempts=2)
        lease = lease_job(project, key, "offline-batch-writer", lease_seconds=300)
        if lease and not lease["cache_hit"]:
            complete_job(project, queued["job_id"], "offline-batch-writer", artifact["artifact_id"])
        state["member_runtime"][logical_id] = {"batch_member_artifact_id": artifact["artifact_id"], "batch_member_job_id": queued["job_id"]}
        state["members"][logical_id] = member
        _append_batch_event(state, "member_persisted", logical_id, artifact_id=artifact["artifact_id"], job_id=queued["job_id"])
        atomic_write_json(state_path, state)
        processed_this_call += 1
        if crash_after_document is not None and processed_this_call == crash_after_document:
            raise RuntimeError("injected batch crash after document")
    members = [state["members"][logical_id] for logical_id in sorted(state["members"])]
    semantic = _batch_semantic_hash(members)
    counters = _derive_batch_counters(state["events"])
    invariants = _batch_invariants(project, members, state["member_runtime"], state["events"])
    staging_path = run_dir / "batch-staging.json"
    staging = {"schema_version": 2, "run_id": run_id, "config_hash": config_hash, "semantic_sha256": semantic, "members": members, "counters": counters, "invariants": invariants, "execution_ledger_path": relative_to_project(state_path, project)}
    atomic_write_json(staging_path, staging)
    if crash_after_staging:
        raise RuntimeError("injected batch crash after manifest staging")
    manifest = {**staging, "stage": "BATCH_COMPLETE", "member_paths": {logical_id: relative_to_project(_batch_member_path(project, run_id, logical_id), project) for logical_id in sorted(state["members"])}}
    atomic_write_json(manifest_path, manifest)
    manifest_artifact = register_artifact(project, run_id, "batch_manifest", manifest_path, immutable=True)
    return {"status": "complete", "manifest": relative_to_project(manifest_path, project), "manifest_sha256": manifest_artifact["sha256"], "semantic_sha256": semantic, "members": members, "counters": counters, "invariants": invariants}


def validate_offline_batch(project: Path, run_id: str) -> dict[str, Any]:
    manifest_path = project_paths(project)["runs"] / run_id / "batch-manifest.json"
    defects: list[str] = []
    if not manifest_path.is_file():
        return {"run_id": run_id, "verdict": "REJECT", "defects": ["batch_manifest_missing"]}
    manifest = load_json(manifest_path)
    members = manifest.get("members") or []
    seen_papers: set[str] = set()
    global_mats: dict[str, str] = {}
    global_mixes: dict[str, str] = {}
    global_evidence: dict[str, tuple[str, str]] = {}
    record_sets: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for member in members:
        if member.get("source_text", {}).get("trust") != "untrusted_evidence":
            defects.append(f"untrusted_source_tag_missing:{member.get('logical_id')}")
        if member.get("status") == "DUPLICATE_ALIAS" and (member.get("records_sha256") or member.get("snapshot_sha256")):
            defects.append(f"duplicate_published:{member.get('logical_id')}")
        if member.get("status") in {"ACCESS_FAILED", "VISUAL_REVIEW_PENDING", "QUARANTINED"} and member.get("records_sha256") is not None:
            defects.append(f"isolated_member_has_records:{member.get('logical_id')}")
        for paper_key in member.get("paper_keys") or []:
            if paper_key in seen_papers:
                defects.append(f"cross_paper_key:{paper_key}")
            seen_papers.add(paper_key)
        if member.get("status") == "ACCEPTED_REFERENCE":
            try:
                verify_accepted_reference(Path(member.get("source_project") or "."), str(member.get("source_run_id") or ""), Path(member.get("source_pdf") or "."))
            except Exception:
                defects.append(f"reference_verification_failed:{member.get('logical_id')}")
        if member.get("status") in {"ACCEPTED", "ACCEPTED_REFERENCE"}:
            try:
                batch_records_path = project_paths(project)["root"] / str(member.get("batch_records_path") or "")
                if not batch_records_path.is_file():
                    raise ValueError("batch records mirror missing")
                if sha256_path(batch_records_path) != member.get("records_sha256"):
                    raise ValueError("batch records hash mismatch")
                if member.get("status") == "ACCEPTED":
                    local_run = str(member["local_run_id"])
                    if validate_run(project, local_run).get("verdict") != "PASS":
                        raise ValueError("local accepted run invalid")
                records = load_json(batch_records_path)
                paper_keys = {row["paper_key"] for row in records.get("papers", [])}
                if paper_keys != set(member.get("paper_keys") or []):
                    raise ValueError("batch paper ownership mismatch")
                record_sets.append((member, records))
            except Exception as exc:
                defects.append(f"combined_evidence_failure:{member.get('logical_id')}:{type(exc).__name__}")
    for member, records in record_sets:
        logical_id = str(member["logical_id"])
        paper_keys = {row["paper_key"] for row in records.get("papers", [])}
        local_record_owners: dict[str, str] = {}
        for row in records.get("mats", []):
            if row.get("paper_key") not in paper_keys:
                defects.append(f"record_paper_ownership:{logical_id}:{row.get('mat_key')}")
            key = str(row.get("mat_key"))
            if key in global_mats:
                defects.append(f"cross_paper_mat_key:{key}")
            global_mats[key] = str(row.get("paper_key"))
            local_record_owners[key] = str(row.get("paper_key"))
        for row in records.get("mixes", []):
            if row.get("paper_key") not in paper_keys:
                defects.append(f"record_paper_ownership:{logical_id}:{row.get('mix_key')}")
            key = str(row.get("mix_key"))
            if key in global_mixes:
                defects.append(f"cross_paper_mix_key:{key}")
            global_mixes[key] = str(row.get("paper_key"))
            local_record_owners[key] = str(row.get("paper_key"))
        for row in records.get("evidence_links", []):
            key = str(row.get("evidence_key"))
            owner_key = str(row.get("record_key"))
            owner_paper = local_record_owners.get(owner_key)
            if owner_paper is None:
                defects.append(f"evidence_unknown_record:{logical_id}:{key}")
            elif key in global_evidence:
                defects.append(f"cross_paper_evidence_key:{key}")
            global_evidence[key] = (owner_key, owner_paper or "")
        for row in records.get("mats", []) + records.get("mixes", []):
            record_key = str(row.get("mat_key") or row.get("mix_key"))
            owner_paper = str(row.get("paper_key"))
            for provenance in (row.get("field_provenance") or {}).values():
                evidence_key = provenance.get("evidence_key")
                if not evidence_key:
                    continue
                evidence_owner = global_evidence.get(str(evidence_key))
                if evidence_owner is None or evidence_owner[0] != record_key or evidence_owner[1] != owner_paper:
                    defects.append(f"foreign_evidence_reference:{logical_id}:{record_key}:{evidence_key}")
        for mix in records.get("mixes", []):
            owner_paper = str(mix.get("paper_key"))
            for mat_key in mix.get("modules", {}).get("materials", {}).get("mat_refs", []):
                if global_mats.get(str(mat_key)) != owner_paper:
                    defects.append(f"foreign_mat_reference:{logical_id}:{mix.get('mix_key')}:{mat_key}")
    expected = _batch_semantic_hash(members)
    if expected != manifest.get("semantic_sha256"):
        defects.append("semantic_hash_mismatch")
    ledger_path = project_paths(project)["root"] / str(manifest.get("execution_ledger_path") or "")
    events = (load_json(ledger_path).get("events") if ledger_path.is_file() else None)
    if not isinstance(events, list):
        defects.append("execution_ledger_missing")
        events = []
    counters = _derive_batch_counters(events)
    if counters != (manifest.get("counters") or {}):
        defects.append("counter_ledger_mismatch")
    for key in ("network_requests", "wakg_accesses", "pipeline_model_calls", "duplicate_paid_model_calls"):
        if counters.get(key) != 0:
            defects.append(f"nonzero_counter:{key}")
    invariants = manifest.get("invariants") or {}
    if not invariants.get("duplicate_has_no_extract_publish_version_snapshot", False):
        defects.append("duplicate_publish_invariant_failed")
    if invariants.get("member_count") != len(members):
        defects.append("member_count_invariant_mismatch")
    return {"run_id": run_id, "verdict": "PASS" if not defects else "REJECT", "defects": defects, "semantic_sha256": expected, "members": len(members), "counters": counters}


def _copy_immutable(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if sha256_path(destination) != sha256_path(source):
            raise ValueError(f"immutable snapshot member differs: {destination}")
        return
    fd, temporary = tempfile.mkstemp(prefix=destination.name + ".", suffix=".tmp", dir=str(destination.parent))
    os.close(fd)
    try:
        shutil.copyfile(source, temporary)
        os.replace(temporary, destination)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def freeze_snapshot(project: Path, run_id: str, manifest: dict[str, Any]) -> dict[str, Any]:
    """Freeze all accepted exchange outputs from one validated content set."""
    if manifest.get("stage") != "ACCEPTED" or manifest.get("validation", {}).get("verdict") != "PASS":
        raise ValueError("only a PASS/ACCEPTED run can be frozen")
    run_dir = project_paths(project)["runs"] / run_id
    sources = {
        "claims": run_dir / "claims.json",
        "records": run_dir / "records.json",
        "exchange": run_dir / "exchange.sqlite3",
        "validation": run_dir / "validation.json",
    }
    for role, path in sources.items():
        if not path.is_file():
            raise FileNotFoundError(f"snapshot member missing ({role}): {path}")
    hashes = {role: sha256_path(path) for role, path in sources.items()}
    identity = {
        "run_id": run_id,
        "platform_contract": PLATFORM_CONTRACT,
        "input_fingerprint": manifest.get("input", {}).get("input_fingerprint"),
        "members": hashes,
    }
    snapshot_id = "snapshot-" + sha256_bytes(stable_json(identity).encode("utf-8"))[:24]
    snapshot_dir = run_dir / "snapshots" / snapshot_id
    names = {"claims": "claims.json", "records": "records.json", "exchange": "exchange.sqlite3", "validation": "validation.json"}
    member_artifacts: dict[str, dict[str, Any]] = {}
    for role, source in sources.items():
        destination = snapshot_dir / names[role]
        _copy_immutable(source, destination)
        member_artifacts[role] = register_artifact(project, run_id, f"snapshot_{role}", destination, immutable=True)
    snapshot_manifest_path = snapshot_dir / "snapshot.json"
    snapshot_manifest = {
        "schema_version": 1,
        "snapshot_id": snapshot_id,
        "run_id": run_id,
        "status": "frozen-accepted",
        "platform_contract": PLATFORM_CONTRACT,
        "input": manifest.get("input", {}),
        "members": {
            role: {
                "path": relative_to_project(snapshot_dir / names[role], project),
                "sha256": hashes[role],
                "artifact_id": member_artifacts[role]["artifact_id"],
            }
            for role in sources
        },
    }
    if snapshot_manifest_path.exists():
        existing = load_json(snapshot_manifest_path)
        if stable_json(existing) != stable_json(snapshot_manifest):
            raise ValueError("existing immutable snapshot manifest differs")
    else:
        atomic_write_json(snapshot_manifest_path, snapshot_manifest)
    snapshot_manifest_artifact = register_artifact(project, run_id, "snapshot_manifest", snapshot_manifest_path, immutable=True)

    con = connect_control_db(project)
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            """INSERT INTO snapshots(snapshot_id,run_id,status,snapshot_manifest_path,snapshot_manifest_sha256,
                   records_sha256,exchange_sha256,validation_sha256,created_at)
               VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(snapshot_id) DO NOTHING""",
            (
                snapshot_id, run_id, "frozen-accepted", relative_to_project(snapshot_manifest_path, project),
                snapshot_manifest_artifact["sha256"], hashes["records"], hashes["exchange"], hashes["validation"], utc_now(),
            ),
        )
        all_members = {**member_artifacts, "manifest": snapshot_manifest_artifact}
        for role, artifact in all_members.items():
            con.execute(
                """INSERT INTO snapshot_members(snapshot_id,artifact_id,role) VALUES (?,?,?)
                   ON CONFLICT(snapshot_id,role) DO NOTHING""",
                (snapshot_id, artifact["artifact_id"], role),
            )
        con.commit()
    except Exception:
        if con.in_transaction:
            con.rollback()
        raise
    finally:
        con.close()
    return {
        "snapshot_id": snapshot_id,
        "path": relative_to_project(snapshot_manifest_path, project),
        "sha256": snapshot_manifest_artifact["sha256"],
        "records_sha256": hashes["records"],
        "exchange_sha256": hashes["exchange"],
        "validation_sha256": hashes["validation"],
    }


def write_validation(project: Path, run_id: str, result: dict[str, Any], accept: bool = False) -> dict[str, Any]:
    run_dir = project_paths(project)["runs"] / run_id
    validation_path = run_dir / "validation.json"
    manifest_path = run_dir / "manifest.json"
    manifest = load_json(manifest_path)
    if accept and result.get("verdict") == "PASS" and manifest.get("stage") == "ACCEPTED" and manifest.get("snapshot"):
        snapshot = manifest["snapshot"]
        if (
            manifest.get("outputs", {}).get("records_sha256") == snapshot.get("records_sha256")
            and validation_path.is_file()
            and sha256_path(validation_path) == snapshot.get("validation_sha256")
        ):
            return manifest
    atomic_write_json(validation_path, result)
    manifest["validation"] = {"path": relative_to_project(validation_path, project), "sha256": sha256_path(validation_path), "verdict": result["verdict"]}
    if result["verdict"] == "PASS":
        manifest["stage"] = "ACCEPTED" if accept else "VALIDATED"
    else:
        manifest["stage"] = "EXTRACTED"
    register_artifact(project, run_id, "validation_report", validation_path, immutable=True)
    if manifest["stage"] == "ACCEPTED":
        manifest["snapshot"] = freeze_snapshot(project, run_id, manifest)
    atomic_write_json(manifest_path, manifest)
    register_artifact(project, run_id, "run_manifest", manifest_path, immutable=False)
    update_run_state(project, run_id, manifest)
    return manifest


def record_rejection(project: Path, run_id: str, fingerprint: str) -> dict[str, Any]:
    manifest_path = project_paths(project)["runs"] / run_id / "manifest.json"
    manifest = load_json(manifest_path)
    history = manifest.setdefault("repair_history", [])
    attempt = len(history) + 1
    history.append({"attempt": attempt, "defect_fingerprint": fingerprint})
    repeats = sum(1 for row in history if row["defect_fingerprint"] == fingerprint)
    if repeats >= DEFAULT_BUDGET["max_repair_rounds"]:
        manifest["stage"] = "BLOCKED_REPEATED_DEFECT"
        manifest["terminal_exception"] = {"reason": "same defect fingerprint reached repair limit", "fingerprint": fingerprint}
    atomic_write_json(manifest_path, manifest)
    update_run_state(project, run_id, manifest)
    return {"run_id": run_id, "attempt": attempt, "repeats": repeats, "stage": manifest["stage"]}


def read_fault_events(project: Path) -> list[dict[str, Any]]:
    """Read the append-only research fault ledger and reject partial/corrupt lines."""
    path = project_paths(project)["faults"]
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"corrupt fault ledger line {line_number}") from exc
            if not isinstance(event, dict) or not event.get("event_id") or not event.get("fault_id"):
                raise ValueError(f"invalid fault ledger line {line_number}")
            events.append(event)
    return events


def validate_fix_commit(project: Path, fix_commit: str) -> str:
    """Require an exact commit object when the project is backed by Git."""
    value = fix_commit.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise ValueError("fix_commit must be a full 40-character hexadecimal Git commit")
    if (project / ".git").exists():
        checked = subprocess.run(
            ["git", "cat-file", "-e", f"{value}^{{commit}}"],
            cwd=project,
            capture_output=True,
            text=True,
            check=False,
        )
        if checked.returncode != 0:
            raise ValueError("fix_commit does not resolve to a commit in the project repository")
    return value


def record_fault_event(
    project: Path,
    test_run_id: str,
    fingerprint: str,
    event: str,
    kind: str,
    severity: str,
    summary: str,
    occurrence_type: str = "observed",
    symptom: str | None = None,
    root_cause: str | None = None,
    reproduction: str | None = None,
    impact: str | None = None,
    evidence_paths: Iterable[str] = (),
    fix_commit: str | None = None,
    verification: str | None = None,
    related_run_ids: Iterable[str] = (),
) -> dict[str, Any]:
    """Append one immutable lifecycle event for a test fault."""
    normalized_event = event.upper()
    normalized_kind = kind.upper()
    normalized_severity = severity.lower()
    normalized_occurrence = occurrence_type.lower()
    if normalized_event not in FAULT_EVENTS:
        raise ValueError(f"unsupported fault event: {event}")
    if normalized_kind not in FAULT_KINDS:
        raise ValueError(f"unsupported fault kind: {kind}")
    if normalized_severity not in FAULT_SEVERITIES:
        raise ValueError(f"unsupported fault severity: {severity}")
    if normalized_occurrence not in FAULT_OCCURRENCES:
        raise ValueError(f"unsupported occurrence type: {occurrence_type}")
    if normalized_event == "EXERCISED" and normalized_occurrence != "injected":
        raise ValueError("EXERCISED requires occurrence_type=injected")
    if not test_run_id.strip() or not fingerprint.strip() or not summary.strip():
        raise ValueError("test_run_id, fingerprint, and summary are required")
    normalized_fix_commit = validate_fix_commit(project, fix_commit) if fix_commit else None

    prior = read_fault_events(project)
    fault_id = "fault-" + sha256_bytes(fingerprint.strip().encode("utf-8"))[:24]
    family = [row for row in prior if row["fault_id"] == fault_id]
    if normalized_event in {"MITIGATED", "RESOLVED", "VERIFIED", "REOPENED", "REPRODUCED"} and not family:
        raise ValueError(f"{normalized_event} requires a prior OBSERVED event")
    if family:
        original = family[0]
        requested_classification = (normalized_kind, normalized_severity, normalized_occurrence)
        original_classification = (original["kind"], original["severity"], original["occurrence_type"])
        if requested_classification != original_classification:
            raise ValueError(
                "fault lifecycle classification must match the original event: "
                f"expected {original_classification}, received {requested_classification}"
            )
    if normalized_event == "VERIFIED" and family[-1].get("status") != "RESOLVED":
        raise ValueError("VERIFIED requires the latest prior status to be RESOLVED")

    status_by_event = {
        "OBSERVED": "OPEN",
        "REOPENED": "OPEN",
        "REPRODUCED": "OPEN",
        "MITIGATED": "MITIGATED",
        "RESOLVED": "RESOLVED",
        "VERIFIED": "VERIFIED",
        "EXERCISED": "EXERCISED",
    }
    identity = {
        "schema_version": 1,
        "fault_id": fault_id,
        "test_run_id": test_run_id.strip(),
        "fingerprint": fingerprint.strip(),
        "event": normalized_event,
        "status": status_by_event[normalized_event],
        "kind": normalized_kind,
        "severity": normalized_severity,
        "occurrence_type": normalized_occurrence,
        "summary": summary.strip(),
        "symptom": symptom.strip() if symptom else None,
        "root_cause": root_cause.strip() if root_cause else None,
        "reproduction": reproduction.strip() if reproduction else None,
        "impact": impact.strip() if impact else None,
        "evidence_paths": sorted({str(path).strip() for path in evidence_paths if str(path).strip()}),
        "fix_commit": normalized_fix_commit,
        "verification": verification.strip() if verification else None,
        "related_run_ids": sorted({str(run_id).strip() for run_id in related_run_ids if str(run_id).strip()}),
    }
    event_id = "fault-event-" + sha256_bytes(stable_json(identity).encode("utf-8"))[:24]
    for row in prior:
        if row["event_id"] == event_id:
            return {"appended": False, "event": row, "ledger_path": relative_to_project(project_paths(project)["faults"], project)}

    row = {**identity, "event_id": event_id, "recorded_at": utc_now()}
    ledger_path = project_paths(project)["faults"]
    existing = ledger_path.read_text(encoding="utf-8") if ledger_path.exists() else ""
    atomic_write_text(ledger_path, existing + stable_json(row) + "\n")
    return {"appended": True, "event": row, "ledger_path": relative_to_project(ledger_path, project)}


def fault_report(project: Path, test_run_id: str | None = None) -> dict[str, Any]:
    """Summarize current fault-family state without erasing historical events."""
    events = read_fault_events(project)
    selected_fault_ids = {
        row["fault_id"] for row in events if test_run_id is None or row.get("test_run_id") == test_run_id
    }
    histories: dict[str, list[dict[str, Any]]] = {}
    for row in events:
        if row["fault_id"] in selected_fault_ids:
            histories.setdefault(row["fault_id"], []).append(row)
    faults = []
    for fault_id in sorted(histories):
        history = histories[fault_id]
        latest = history[-1]
        original = history[0]
        latest_fix_commit = next((row.get("fix_commit") for row in reversed(history) if row.get("fix_commit")), None)
        latest_verification = next((row.get("verification") for row in reversed(history) if row.get("verification")), None)
        faults.append({
            "fault_id": fault_id,
            "fingerprint": latest["fingerprint"],
            "latest_status": latest["status"],
            "kind": original["kind"],
            "severity": original["severity"],
            "occurrence_type": original["occurrence_type"],
            "summary": latest["summary"],
            "first_observed_at": history[0]["recorded_at"],
            "latest_event_at": latest["recorded_at"],
            "history_event_ids": [row["event_id"] for row in history],
            "test_run_event_ids": [row["event_id"] for row in history if test_run_id is None or row.get("test_run_id") == test_run_id],
            "fix_commit": latest_fix_commit,
            "verification": latest_verification,
            "evidence_paths": sorted({path for row in history for path in row.get("evidence_paths", [])}),
        })
    unresolved = [row["fault_id"] for row in faults if row["latest_status"] not in {"RESOLVED", "VERIFIED", "EXERCISED"}]
    ledger_path = project_paths(project)["faults"]
    return {
        "schema_version": 1,
        "test_run_id": test_run_id,
        "ledger_path": relative_to_project(ledger_path, project),
        "ledger_sha256": sha256_path(ledger_path) if ledger_path.exists() else None,
        "fault_family_count": len(faults),
        "event_count": sum(len(histories[row["fault_id"]]) for row in faults),
        "observed_family_count": sum(row["occurrence_type"] == "observed" for row in faults),
        "injected_family_count": sum(row["occurrence_type"] == "injected" for row in faults),
        "expected_limitation_family_count": sum(row["occurrence_type"] == "expected_limitation" for row in faults),
        "unresolved_fault_ids": unresolved,
        "faults": faults,
    }


def run_status(project: Path, run_id: str | None) -> dict[str, Any]:
    bootstrap_project(project)
    paths = project_paths(project)
    if run_id:
        manifest_path = paths["runs"] / run_id / "manifest.json"
        if not manifest_path.exists():
            return {"run_id": run_id, "status": "missing"}
        manifest = load_json(manifest_path)
        con = connect_control_db(project)
        try:
            jobs = [dict(row) for row in con.execute(
                "SELECT job_id,stage,status,attempt_count,max_attempts,updated_at FROM jobs WHERE run_id=? ORDER BY created_at",
                (run_id,),
            )]
            counts = {
                "artifacts": con.execute("SELECT COUNT(*) FROM artifacts WHERE run_id=?", (run_id,)).fetchone()[0],
                "claims": con.execute("SELECT COUNT(*) FROM claims WHERE run_id=?", (run_id,)).fetchone()[0],
                "issues": con.execute("SELECT COUNT(*) FROM issues WHERE run_id=?", (run_id,)).fetchone()[0],
                "snapshots": con.execute("SELECT COUNT(*) FROM snapshots WHERE run_id=?", (run_id,)).fetchone()[0],
                "usage_events": con.execute("SELECT COUNT(*) FROM usage_events WHERE run_id=?", (run_id,)).fetchone()[0],
            }
        finally:
            con.close()
        return {
            "run_id": run_id,
            "stage": manifest.get("stage"),
            "records_sha256": manifest.get("outputs", {}).get("records_sha256"),
            "claims_sha256": manifest.get("outputs", {}).get("claims_sha256"),
            "validation": manifest.get("validation"),
            "snapshot": manifest.get("snapshot"),
            "terminal_exception": manifest.get("terminal_exception"),
            "repair_attempts": len(manifest.get("repair_history", [])),
            "jobs": jobs,
            "control_counts": counts,
        }
    state = state_projection(project)
    return {"project_id": state["project_id"], "pair_id": state["pair_id"], "stage": state["stage"], "runs": state.get("runs", {}), "completion_gate": state.get("completion_gate")}


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="WAKG literature pipeline deterministic core")
    root.add_argument("--project", type=Path, default=Path.cwd(), help="Project root")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("bootstrap")

    discover_parser = commands.add_parser("discover")
    discover_parser.add_argument("--query", required=True)
    discover_parser.add_argument("--limit", type=int, default=20)
    discover_parser.add_argument("--year-from", type=int)
    discover_parser.add_argument("--year-to", type=int)
    discover_parser.add_argument("--oa-only", action="store_true")
    discover_parser.add_argument("--has-fulltext", action="store_true")
    discover_parser.add_argument("--offline-payload", type=Path)
    discover_parser.add_argument("--out", type=Path)

    benchmark_parser = commands.add_parser("discover-benchmark")
    benchmark_parser.add_argument("--run-id", required=True)
    benchmark_parser.add_argument("--query-manifest", type=Path, required=True)
    benchmark_parser.add_argument("--year-from", type=int, required=True)
    benchmark_parser.add_argument("--year-to", type=int, required=True)
    benchmark_parser.add_argument("--openalex-pages", type=int, default=5)
    benchmark_parser.add_argument("--crossref-rows", type=int, default=250)
    benchmark_parser.add_argument("--max-requests", type=int, default=72)
    benchmark_parser.add_argument("--max-cost-usd", type=float, default=0.08)
    benchmark_parser.add_argument("--max-bytes", type=int, default=50 * 1024 * 1024)
    benchmark_parser.add_argument("--query-limit", type=int, help="Registered manifest prefix; use 4 for the D1 canary")
    benchmark_parser.add_argument("--seed-run-id", help="Optional prior run with hash-validated exact-page receipts to materialize as cache")
    benchmark_parser.add_argument("--cache-only", action="store_true")

    probe_parser = commands.add_parser("link-probe")
    probe_parser.add_argument("--run-id", required=True)
    probe_input = probe_parser.add_mutually_exclusive_group(required=True)
    probe_input.add_argument("--url", action="append")
    probe_input.add_argument("--catalog", type=Path)
    probe_parser.add_argument("--probe-limit", type=int, default=30)
    probe_parser.add_argument("--probe-offset", type=int, default=0)
    probe_parser.add_argument("--byte-cap", type=int, default=8192)
    probe_parser.add_argument("--timeout", type=int, default=20)
    probe_parser.add_argument("--max-wall-seconds", type=int, default=600)
    probe_parser.add_argument("--diagnostic-get-after-403", action="store_true")
    probe_parser.add_argument("--max-requests", type=int)

    route_parser = commands.add_parser("oa-route-resolve")
    route_parser.add_argument("--run-id", required=True)
    route_parser.add_argument("--catalog", type=Path, required=True)
    route_parser.add_argument("--link-probe", type=Path, required=True)
    route_parser.add_argument("--openalex-envelope", type=Path, required=True, help="Offline fixture or cached response; this command never fetches metadata")

    rescue_parser = commands.add_parser("oa-rescue-summary")
    rescue_parser.add_argument("--message-id", required=True)
    rescue_parser.add_argument("--pair-id", required=True)
    rescue_parser.add_argument("--reviewed-head", required=True)
    rescue_parser.add_argument("--frozen-probe", type=Path, required=True)
    rescue_parser.add_argument("--route-manifest", type=Path, required=True)
    rescue_parser.add_argument("--route-catalog", type=Path, required=True)
    rescue_parser.add_argument("--rescue-catalog", type=Path, required=True)
    rescue_parser.add_argument("--rescue-probe", type=Path, required=True)
    rescue_parser.add_argument("--metadata-receipt", type=Path, required=True)
    rescue_parser.add_argument("--metadata-envelope", type=Path, required=True)
    rescue_parser.add_argument("--out", type=Path)

    rank_pool_parser = commands.add_parser("oa-rank-ab-pool")
    rank_pool_parser.add_argument("--run-id", required=True)
    rank_pool_parser.add_argument("--catalog", type=Path, required=True)
    rank_pool_parser.add_argument("--exclude-probe", type=Path, required=True)
    rank_pool_parser.add_argument("--pool-size", type=int, default=100)
    rank_pool_parser.add_argument("--required-catalog-sha256")

    rank_plan_parser = commands.add_parser("oa-rank-ab-plan")
    rank_plan_parser.add_argument("--run-id", required=True)
    rank_plan_parser.add_argument("--pool-manifest", type=Path, required=True)
    rank_plan_parser.add_argument("--pool-catalog", type=Path, required=True)
    rank_plan_parser.add_argument("--openalex-envelope", type=Path, required=True, help="Offline fixture or cached response; this command never fetches metadata")

    rank_summary_parser = commands.add_parser("oa-rank-ab-summary")
    rank_summary_parser.add_argument("--message-id", required=True)
    rank_summary_parser.add_argument("--pair-id", required=True)
    rank_summary_parser.add_argument("--reviewed-head", required=True)
    rank_summary_parser.add_argument("--pool-manifest", type=Path, required=True)
    rank_summary_parser.add_argument("--arm-manifest", type=Path, required=True)
    rank_summary_parser.add_argument("--union-probe-catalog", type=Path, required=True)
    rank_summary_parser.add_argument("--metadata-receipt", type=Path, required=True)
    rank_summary_parser.add_argument("--metadata-envelope", type=Path, required=True)
    rank_summary_parser.add_argument("--replay-proof", type=Path, required=True)
    rank_summary_parser.add_argument("--link-probe", type=Path, required=True)
    rank_summary_parser.add_argument("--out", type=Path)

    acquire_parser = commands.add_parser("acquire")
    acquire_parser.add_argument("--url", required=True)
    acquire_parser.add_argument("--timeout", type=int, default=30)

    commands.add_parser("synthetic")

    normalize_parser = commands.add_parser("normalize")
    normalize_parser.add_argument("--pdf", type=Path, required=True)
    normalize_parser.add_argument("--run-id", required=True)
    normalize_parser.add_argument("--source-integrity", type=Path)

    map_parser = commands.add_parser("map")
    map_parser.add_argument("--pdf", type=Path, required=True)
    map_parser.add_argument("--run-id", required=True)
    map_parser.add_argument("--paper-map", type=Path, required=True)
    map_parser.add_argument("--entity-inventory", type=Path, required=True)
    map_parser.add_argument("--source-integrity", type=Path)

    quarantine_parser = commands.add_parser("quarantine")
    quarantine_parser.add_argument("--pdf", type=Path, required=True)
    quarantine_parser.add_argument("--run-id", required=True)
    quarantine_parser.add_argument("--reason", required=True)

    extract_parser = commands.add_parser("extract")
    extract_parser.add_argument("--pdf", type=Path, required=True)
    extract_parser.add_argument("--run-id", required=True)
    extract_parser.add_argument("--records", type=Path, help="Model-produced records JSON to normalize and publish")

    validate_parser = commands.add_parser("validate")
    validate_parser.add_argument("--run-id", required=True)
    validate_parser.add_argument("--write", action="store_true")
    validate_parser.add_argument("--accept", action="store_true")
    validate_parser.add_argument("--reject-fingerprint")

    status_parser = commands.add_parser("status")
    status_parser.add_argument("--run-id")

    fault_parser = commands.add_parser("fault-record")
    fault_parser.add_argument("--test-run-id", required=True)
    fault_parser.add_argument("--fingerprint", required=True)
    fault_parser.add_argument("--event", required=True, choices=sorted(FAULT_EVENTS))
    fault_parser.add_argument("--kind", required=True, choices=sorted(FAULT_KINDS))
    fault_parser.add_argument("--severity", required=True, choices=sorted(FAULT_SEVERITIES))
    fault_parser.add_argument("--occurrence-type", default="observed", choices=sorted(FAULT_OCCURRENCES))
    fault_parser.add_argument("--summary", required=True)
    fault_parser.add_argument("--symptom")
    fault_parser.add_argument("--root-cause")
    fault_parser.add_argument("--reproduction")
    fault_parser.add_argument("--impact")
    fault_parser.add_argument("--evidence-path", action="append", default=[])
    fault_parser.add_argument("--fix-commit")
    fault_parser.add_argument("--verification")
    fault_parser.add_argument("--related-run-id", action="append", default=[])

    report_parser = commands.add_parser("fault-report")
    report_parser.add_argument("--test-run-id")
    report_parser.add_argument("--write", action="store_true")
    report_parser.add_argument("--out", type=Path)
    return root


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    args = parser().parse_args(argv)
    project = args.project.resolve()
    if args.command == "bootstrap":
        output = bootstrap_project(project)
    elif args.command == "discover":
        output = discover(
            project,
            args.query,
            min(max(args.limit, 1), DEFAULT_BUDGET["max_source_candidates"]),
            args.offline_payload,
            args.year_from,
            args.year_to,
            args.oa_only,
            args.has_fulltext,
        )
        if args.out:
            atomic_write_json(args.out, output)
    elif args.command == "discover-benchmark":
        output = discover_benchmark(project, args.run_id, args.query_manifest, args.year_from, args.year_to,
                                    args.openalex_pages, args.crossref_rows, args.max_requests,
                                    args.max_cost_usd, args.max_bytes, args.cache_only, query_limit=args.query_limit,
                                    seed_run_id=args.seed_run_id)
    elif args.command == "link-probe":
        output = probe_links(project, args.run_id, args.url, args.probe_limit, args.byte_cap, catalog=args.catalog,
                             probe_offset=args.probe_offset, timeout=args.timeout, max_wall_seconds=args.max_wall_seconds,
                             diagnostic_get_after_403=args.diagnostic_get_after_403, max_requests=args.max_requests)
    elif args.command == "oa-route-resolve":
        output = resolve_oa_routes(project, args.run_id, args.catalog, args.link_probe, args.openalex_envelope)
    elif args.command == "oa-rescue-summary":
        output = oa_rescue_summary(project, args.message_id, args.pair_id, args.reviewed_head, args.frozen_probe,
                                   args.route_manifest, args.route_catalog, args.rescue_catalog, args.rescue_probe,
                                   args.metadata_receipt, args.metadata_envelope, args.out)
    elif args.command == "oa-rank-ab-pool":
        output = oa_rank_ab_pool(project, args.run_id, args.catalog, args.exclude_probe, args.pool_size, args.required_catalog_sha256)
    elif args.command == "oa-rank-ab-plan":
        output = oa_rank_ab_plan(project, args.run_id, args.pool_manifest, args.pool_catalog, args.openalex_envelope)
    elif args.command == "oa-rank-ab-summary":
        output = oa_rank_ab_summary(project, args.message_id, args.pair_id, args.reviewed_head, args.pool_manifest,
                                    args.arm_manifest, args.union_probe_catalog, args.metadata_receipt, args.metadata_envelope,
                                    args.replay_proof, args.link_probe, args.out)
    elif args.command == "acquire":
        output = acquire_document(project, args.url, args.timeout)
    elif args.command == "synthetic":
        output = generate_synthetic(project)
    elif args.command == "normalize":
        output = normalize_document(project, args.pdf, args.run_id, args.source_integrity)
    elif args.command == "map":
        output = map_document(project, args.pdf, args.run_id, args.paper_map, args.entity_inventory, args.source_integrity)
    elif args.command == "quarantine":
        output = quarantine_document(project, args.pdf, args.run_id, args.reason)
    elif args.command == "extract":
        output = extract_document(project, args.pdf, args.run_id, args.records)
    elif args.command == "validate":
        if args.reject_fingerprint:
            if not args.write:
                raise ValueError("--reject-fingerprint requires --write")
            output = record_rejection(project, args.run_id, args.reject_fingerprint)
        else:
            output = validate_run(project, args.run_id)
            if args.accept and not args.write:
                raise ValueError("--accept requires --write")
            if args.write:
                write_validation(project, args.run_id, output, accept=args.accept)
    elif args.command == "status":
        output = run_status(project, args.run_id)
    elif args.command == "fault-record":
        output = record_fault_event(
            project=project,
            test_run_id=args.test_run_id,
            fingerprint=args.fingerprint,
            event=args.event,
            kind=args.kind,
            severity=args.severity,
            summary=args.summary,
            occurrence_type=args.occurrence_type,
            symptom=args.symptom,
            root_cause=args.root_cause,
            reproduction=args.reproduction,
            impact=args.impact,
            evidence_paths=args.evidence_path,
            fix_commit=args.fix_commit,
            verification=args.verification,
            related_run_ids=args.related_run_id,
        )
    elif args.command == "fault-report":
        output = fault_report(project, args.test_run_id)
        if args.write:
            if args.out:
                report_path = args.out if args.out.is_absolute() else project / args.out
            elif args.test_run_id:
                report_path = project_paths(project)["runs"] / args.test_run_id / "fault-report.json"
            else:
                report_path = project / "research" / "fault-report.json"
            atomic_write_json(report_path, output)
            output = {**output, "report_path": relative_to_project(report_path, project), "report_sha256": sha256_path(report_path)}
    else:
        raise AssertionError(args.command)
    print(pretty_json(output), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
