---
name: wakg-literature-pipeline
description: Discover, acquire, extract, and independently validate literature data for WAKG MAT/MIX workflows. Use for resumable multi-task paper pipelines and evidence-backed offline exchange packages; do not use it to write to the live WAKG platform.
---

# WAKG Literature Pipeline

Build an offline, resumable literature record before considering platform import. Preserve missing values as `null`; never turn an access failure into a claim that the paper contains no data.

## Route the work

### Current supplied-paper workflow

For the project's native text/table extraction, use `scripts/paper_native_flow.py full
--pdf PDF --output DIR` or `scripts/run_nonestimate_integration.py --run --output DIR`.
The route is parsing -> one whole-paper extraction Agent -> scripted assembly and
source localization -> human review. Tables remain native cells: the Agent maps
their meanings and returns each source anchor as `{ids: [...], text: original_label}`.
Scripts recover invalid anchors by matching the original label within that table,
choosing the nearest original position when several matches exist. Prose localization
first uses the cited block, then the original quote across the document; repeated
matches use the nearest cited position. An unresolved source is recorded per field
while other records continue.

Use `debug-review` only when investigating results. It produces a problem report,
not an automatic data rewrite or a routine acceptance prerequisite. This supplied-paper
route replaces the automatic Agent acceptance/review stages described in the legacy
workflow below. Compare coverage of basic measurements separately from optional
relative-strength summaries. The source/evidence and null-preservation rules still apply.

### Legacy discovery and acceptance workflows

Use one stable `pair_id` and three project tasks:

- `SOURCE_SCOUT`: read-only, low-cost model. Run deterministic discovery first and return one compact `SOURCE_RESULT`.
- `EXECUTION_CONTROL`: the sole project and canonical-state writer. Acquire, parse, extract, repair, and publish artifacts atomically.
- `ACCEPTANCE_REVIEW`: read-only. Run the validator and compare every critical value with its cited evidence before returning `PASS`, `REJECT`, or `ESCALATE`.

Create tasks just in time and reuse healthy task IDs. Do not keep idle heartbeats or use model turns for cache hits, polling, hashing, parsing, schema checks, or retry timing. Read [references/orchestration.md](references/orchestration.md) before creating or messaging tasks.

## Run the deterministic core

Use `scripts/pipeline.py` for `bootstrap`, `discover`, `discover-benchmark`, `link-probe`, `oa-route-resolve`, `oa-rescue-summary`, `oa-rank-ab-pool`, `oa-rank-ab-plan`, `oa-rank-ab-summary`, `acquire`, `synthetic`, `extract`, `validate`, `status`, `fault-record`, and `fault-report`. Prefer the bundled Codex Python runtime because it already contains PyMuPDF, pdfplumber, pypdf, pandas, NumPy, and Pillow. For a preregistered public-metadata scale test, read [references/source-discovery.md](references/source-discovery.md): benchmark receipts and raw catalogues remain internal, live runs stay serial and bounded, and a link probe is never acquisition.

Use `scripts/run_oa_hybrid_canary.py prepare` to tune a route bonus only on a frozen prior cohort and freeze a disjoint prospective pool. Its future `live` mode requires explicit network opt-in, an exact card hash, a viable bound `PREPARED` report, and a budget manifest. It atomically advances `PREPARED -> METADATA_CACHED -> ARMS_FROZEN -> PROBED -> SUMMARIZED`; `replay` and `verify` are zero-network and recompute cached evidence, arms, budgets, and verdict. Every exception emits a state-derived, body-free envelope. `TUNE_BLOCKED` stops before prospective metadata or probing.

Use `scripts/run_oa_preverified_canary.py prepare` for a separately preregistered preverification formulation. It freezes a fresh pool and legacy A before transport; future `live` freezes content-ranked P before one probe and permits final V only from verified P identities. `V` is never padded with unknown or unverified rows. Its controller shares the atomic state, receipt/hash, replay, and body-free failure conventions above.

Treat `control/state.sqlite3` as canonical runtime state. `control/state.json` is only a human-readable projection. Stage workers publish immutable artifacts and atomic claims; only the deterministic assembler may create `records.json`. An accepted run must expose one immutable `snapshot.json` whose member hashes cover claims, records, exchange SQLite, and validation.

Read [references/wakg-contract.md](references/wakg-contract.md) before mapping fields. Read [references/extraction-policy.md](references/extraction-policy.md) before extracting, repairing, or accepting a record.

Every test campaign must preserve faults in the append-only research ledger, including faults repaired before the final green result. The writer records the initial observation before repair, then appends resolution and verification events; it never rewrites history. Distinguish observed system faults, deliberately injected chaos, and expected evidence/data limitations. Before closing a campaign, generate its `fault-report.json` and carry every unresolved fault ID into the final handoff. Read [references/fault-ledger.md](references/fault-ledger.md) when planning, repairing, reviewing, or closing tests.

A repaired workspace is not proof that the workflow is repaired. Before claiming a process-level extraction or review fix, run two isolated cold rebuilds from the immutable source files and compare the scientific records, evidence graph, and reviewer-facing semantic projection. The cold path must not consume the prior canonical result or share a cache between the two runs. Read [references/reproducibility.md](references/reproducibility.md) before closing such a repair.

Before handing a repaired ten-paper queue to the human reviewer, run `python scripts/run_review_pipeline_soak.py --rounds 3`. It must reproduce one unchanged review-subject hash, pass independent candidate/PDF validation in every round, keep candidate reconstruction and page-cache replay below the registered limits, pass the public UI gate, and pass the renderer/publication fault injections. A soak failure returns to the writer and fault ledger; it is never shown as a human repair task.

PDF page images are an operational review asset, not a reason to repeat extraction or evidence localization. After the exact no-render candidate is independently accepted, pre-render only its selected papers with `scripts/start_review_render.ps1`; the renderer caches pages by PDF SHA256, renderer version, and scale and runs without a model. Publish with `review-ui/scripts/prepare_review_bundle.py --candidate ... --agent-acceptance ... --budget-verdict ...` so the final page consumes the exact reviewed candidate and a passing cost gate instead of rebuilding its fields. A missing or corrupt render cache blocks publication and returns to the deterministic renderer, never to the human reviewer.

Human review has one reviewer-facing surface: the original paper beside the complete published MAT/MIX records, with previous/next navigation and one paper-scoped accept or reject decision. Do not expose a separate anomaly dashboard, cluster-scoped decisions, internal field paths, repair controls, or hidden navigation prerequisites. Internal anomaly clustering may reduce automatic repair work, but all such issues must be repaired or quarantined before the final queue is published. Read [references/human-review.md](references/human-review.md) before building or changing the review queue.

Human review is the final gate, not a source-repair stage. Before exposing a field as `原文找到该值`, require a clickable, independently checked locator. Run deterministic token/cell localization first; cluster the remaining context-dependent gaps and send them once per paper/table to a read-only low-cost semantic evidence agent. The writer may publish the agent result only after verifying that the returned verbatim token lies inside the returned page bbox. `REJECT` routes back to extraction or quarantine; it never becomes a reviewer lookup task.

For the final pre-human review, generate compact PDF-bound packets with `scripts/prehuman_review_packets.py`. The model-free verifier must reopen every immutable PDF and check every bbox/token plus performance value/age path alignment before any semantic agent turn. Reviewers read only their assigned packet, assess grouped row/header semantics, typed nulls, MAT/MIX references, field paths/types, and every derived/composite formula, then return one consolidated result per paper. Every response must repeat its packet ID and exact assigned paper set; merge responses only with `scripts/record_prehuman_agent_acceptance.py --plan ...`, which rejects cross-packet, extra, missing, or duplicate results. Do not ask an agent to rediscover the candidate, scan repository history, or inspect one field per model turn. For a ten-paper campaign, the local rollout proxy gate is strict: uncached input plus output must be below 1,000,000 tokens and model events must not exceed 24; exceeding either produces `BLOCKED_BUDGET`, never a weakened review. Cache semantic acceptance by review-subject hash, protocol hash, and WAKG contract; a HEAD-only change gets a deterministic zero-model rebind.

The stage order is:

`DISCOVERED -> SELECTED -> ACQUIRED -> PARSED -> EXTRACTED -> VALIDATED -> ACCEPTED`

Terminal exceptions are `ACCESS_FAILED`, `QUARANTINED`, and `BLOCKED_BUDGET`. A blocked paper must not stop independent papers.

## Enforce the boundary

- Do not log in to WAKG, fill its forms, call an import endpoint, or store platform credentials.
- Keep full papers and supplements in the ignored internal asset store. Commit only hashes, relative references, synthetic fixtures, and permitted derived records.
- Treat normalization, PSD conversion, FT-IR conversion, image digitization, and inferred MAT/MIX relationships as candidates until their review gates pass.
- Require an immutable input hash, field-level evidence, referential integrity, and an independent acceptance result before `ACCEPTED`.
- Reuse a succeeded idempotency key only when its recorded artifacts still exist and match their hashes. Recover expired leases; never run two writers for the same job concurrently.
- On `REJECT`, send one consolidated defect list to the writer. Stop after two unchanged defect fingerprints and report the blocker.
