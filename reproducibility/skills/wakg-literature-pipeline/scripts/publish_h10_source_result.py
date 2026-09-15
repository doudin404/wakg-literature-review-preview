#!/usr/bin/env python3
"""Publish one hash-bound, body-free H10 metadata failure receipt offline."""
from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".publish-", delete=False) as handle:
        handle.write(data); temporary = Path(handle.name)
    temporary.replace(path)


def publish(project: Path, receipt_path: Path, expected_sha256: str, pair_id: str, run_id: str, accepted_head: str) -> dict[str, dict[str, Any]]:
    if sha256(receipt_path) != expected_sha256:
        raise ValueError("source receipt hash mismatch")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    expected = {"schema_version": 1, "type": "SOURCE_RESULT", "pair_id": pair_id, "run_id": run_id, "accepted_head": accepted_head,
                "request_count": 1, "http_status": 503, "latency_ms": 1736, "response_bytes": 0, "retry_count": 0,
                "outcome": "METADATA_REQUEST_FAILED", "raw_response_persisted": False}
    if not isinstance(receipt, dict) or any(receipt.get(key) != value for key, value in expected.items()):
        raise ValueError("source receipt schema or binding mismatch")
    fresh = receipt.get("fresh")
    counters = receipt.get("counters")
    if fresh != {"count": None, "domains": None, "strata": None, "route_signals": None}:
        raise ValueError("failed metadata receipt must retain null fresh fields")
    if counters != {"model_calls": 0, "wakg_accesses": 0, "pdf_probes": 0, "link_probes": 0}:
        raise ValueError("metadata receipt counter contract mismatch")
    message_id = receipt.get("message_id")
    if not isinstance(message_id, str) or not message_id:
        raise ValueError("source receipt message_id missing")
    source = {"schema_version": 1, "type": "SOURCE_RESULT", "message_id": message_id, "pair_id": pair_id, "run_id": run_id,
              "accepted_preflight_head": accepted_head, "input_receipt_sha256": expected_sha256, "request_count": 1, "http_status": 503,
              "latency_ms": 1736, "response_bytes": 0, "retry_count": 0, "outcome": "METADATA_REQUEST_FAILED",
              "fresh": fresh, "counters": {"network_requests": 1, **counters}, "budget": {"authorized_taken": True, "remaining_requests": 0, "automatic_retry_authorized": False},
              "limitation": "HTTP 503 is an upstream access failure, not evidence of absence of literature.", "raw_response_persisted": False}
    root = project / "runs" / run_id
    source_path = root / "source-result.json"; _write(source_path, source)
    live = {"schema_version": 1, "type": "H10_SOURCE_PUBLICATION", "pair_id": pair_id, "run_id": run_id,
            "status": "PUBLISHED", "accepted_preflight_head": accepted_head, "input_receipt_sha256": expected_sha256,
            "source_result_sha256": sha256(source_path), "outcome": source["outcome"], "request_count": 1,
            "network_requests": 1, "model_calls": 0, "wakg_accesses": 0, "pdf_probes": 0, "link_probes": 0,
            "interpretation": source["limitation"], "raw_response_persisted": False}
    live_path = root / "live-report.json"; _write(live_path, live)
    return {"source_result": source, "live_report": live}


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--project", type=Path, required=True); parser.add_argument("--receipt", type=Path, required=True); parser.add_argument("--expected-sha256", required=True); parser.add_argument("--pair-id", required=True); parser.add_argument("--run-id", required=True); parser.add_argument("--accepted-head", required=True)
    args = parser.parse_args(); result = publish(args.project, args.receipt, args.expected_sha256, args.pair_id, args.run_id, args.accepted_head)
    print(json.dumps({key: hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest() for key, value in result.items()}, sort_keys=True)); return 0


if __name__ == "__main__":
    raise SystemExit(main())
