# Test fault research ledger

Use `research/fault-ledger.jsonl` as the project-wide append-only history of failures found during test campaigns. It is a research index, not a replacement for raw logs, manifests, validation reports, or issue rows.

## What to record

Record unexpected software, provider-contract, orchestration, and environment failures. Also record a test gate that cannot be satisfied because source/data coverage is insufficient when that limitation affects scaling decisions.

Set `occurrence_type` accurately:

- `observed`: an unplanned failure of the pipeline, provider integration, environment, or workflow.
- `injected`: a deliberate chaos fixture. Record it once with `EXERCISED`; it proves a guard was exercised but is not a newly discovered product defect.
- `expected_limitation`: evidence or source coverage is genuinely insufficient and the pipeline correctly refuses promotion.

Ordinary paper quarantine is not automatically a software fault. Record it only when it exposes a reusable limitation or failed campaign gate.

## Lifecycle

1. Append `OBSERVED` before changing the implementation. Include a stable fingerprint, concise symptom, reproduction command or conditions, impact, and evidence paths.
2. Append `MITIGATED` when a bounded workaround exists but the root cause remains open.
3. Append `RESOLVED` with root cause and exact fix commit. Do not claim resolution for a workaround.
4. Append `VERIFIED` only after the latest state is `RESOLVED` and a deterministic or independent rerun supplies evidence.
5. Append `REOPENED` when the same fingerprint recurs.
6. Use standalone `EXERCISED` only for a deliberately injected fault whose expected guard passed. It is not counted as unresolved.

Exact duplicate events are idempotent. The original observation remains present after resolution.

## Commands

Record one lifecycle event:

```text
python skills/wakg-literature-pipeline/scripts/pipeline.py fault-record --test-run-id RUN --fingerprint STABLE_NAME --event OBSERVED --kind SOFTWARE --severity high --summary "..." --symptom "..." --reproduction "..." --impact "..." --evidence-path runs/RUN/log.json
```

Generate the campaign report before its final handoff:

```text
python skills/wakg-literature-pipeline/scripts/pipeline.py fault-report --test-run-id RUN --write
```

The default report path is `runs/<RUN>/fault-report.json`. Reviewers verify that known failures appear in the ledger, resolved items have repair and verification evidence, and all unresolved fault IDs are surfaced rather than hidden by the final PASS.
