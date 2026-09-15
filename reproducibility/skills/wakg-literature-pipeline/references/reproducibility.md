# Cold rebuild reproducibility gate

Use this gate when a user asks whether a fix changes the reusable workflow rather than one saved result, or whenever a test campaign repairs extracted records, evidence locality, reviewer labels, or merge behavior.

## Required isolation

- Start from immutable PDF or supplement bytes plus versioned locator and semantic rules.
- Do not read an earlier canonical `records.json`, reviewer queue, repair output, or accepted result on the generation path.
- Run the complete deterministic path twice in separate temporary directories with separate empty caches.
- Validate the source hashes before either run. A cache hit is not a cold rebuild.

## Compare the decision-bearing layers

Require equality at all three layers:

1. source routing or parsed evidence packets;
2. canonical scientific records and evidence links;
3. reviewer-facing semantic projection, including readable labels and typed null meanings.

Compare canonical JSON or byte hashes after sorting keys and fixing output order. Exclude only declared operational metadata such as elapsed time, temporary paths, and cache counters. Never exclude values, units, relationships, evidence keys, bboxes, statuses, source-explanation codes, or display labels.

Also run the WAKG structural validator and an anti-cheat check proving that changing source content changes the corresponding output while leaving independent papers unchanged. A match between two runs is insufficient if both reused an old result.

For the frozen ten-paper project corpus, use the repository driver:

```text
python scripts/verify_user10v2_clean_rebuild.py
```

It rebuilds Phase A from PDF bytes, rebuilds Phase C source-only WAKG records, compares the review semantic projection, and writes `runs/user10v2-process-reproducibility-v1/clean-rebuild-report.json`. Any failed comparison leaves the campaign unresolved and blocks human publication.

Before human publication, also build the complete ten-paper review candidate twice with `--no-render`. The second run must start from the same immutable PDFs and versioned rules, and the two `subjectSha256` values must match exactly. Run `scripts/validate_review_candidate.py` on both candidate queues. The final candidate's ordered curing projection must also equal `first.curing_projection_sha256` and `second.curing_projection_sha256` in the clean-rebuild report; two independently stable but different projections are a rejection. A matching source-only hash does not replace this final projection check, because field labels, typed nulls, evidence keys, bboxes, source tokens, formulas, source-explanation codes, and complete curing-stage boundaries are part of the decision-bearing review artifact.

Only after this dual-run gate and independent agent acceptance may the writer run the rendering/publication path. The projection command has a single-writer file lock; a concurrent invocation must fail instead of consuming quota or racing to overwrite candidate state.

For the maintained ten-paper review corpus, close the repeat-run loop with:

```powershell
python scripts/run_review_pipeline_soak.py --rounds 3
```

The script is model-free. Each round rebuilds the complete no-render candidate and validates every reported field against the frozen PDF. It then replays all 148 cached pages, validates the current public UI, and runs fault injections for concurrent rendering, terminal failure status, corrupt-cache recovery, and fail-closed publication. The default gates are 60 seconds per ten-paper candidate and 2 seconds per full cache replay; tighten them only after a verified baseline, never relax them to hide a regression. The review-subject hash, rather than the timestamped candidate file hash, must be identical across rounds.

The first run after a scientific, locator, protocol, or Git change must execute the exhaustive proof. Soak repeats may use the proof-seal fast path only after rehashing the candidate, packet files, semantic sidecars, and source PDFs. This fast path is an integrity-checked replay, not a cold rebuild; keep the separate two-run cold-rebuild gate above.

Page rendering is separable from the decision-bearing candidate. Start it in the background for all or a selected subset of candidate run IDs:

```text
powershell -ExecutionPolicy Bypass -File scripts/start_review_render.ps1 -Candidate runs/.../candidate.json -RunId RUN_A,RUN_B -Workers 2
```

The ignored cache key is `(renderer version, PDF SHA256, scale)`. Validate every cached page hash before reuse; an incomplete or corrupt entry is a cache miss. Duplicate workers for one cache key wait for the in-flight render and then reuse it instead of reporting a false failure. The background worker writes a job descriptor, stdout/stderr logs, and an atomic terminal status file under `internal_assets/review-render-jobs/`, including setup or candidate-resolution failures. Independent papers continue when one render fails.

Once all papers in the accepted candidate are cached, publish without rebuilding field evidence:

```text
python review-ui/scripts/prepare_review_bundle.py --candidate runs/.../candidate.json --agent-acceptance runs/.../acceptance.json --budget-verdict runs/.../budget-verdict.json
```

The fast path verifies the candidate/receipt subject hash, exact reviewed Git HEAD, PDF hashes, page routes, page counts, and cache manifests before atomic publication. If Windows prevents the directory swap, restore the previous complete tree and fail closed; never replace run directories one by one under an old queue. Rendering never changes the candidate hash and never substitutes for evidence or agent review.
