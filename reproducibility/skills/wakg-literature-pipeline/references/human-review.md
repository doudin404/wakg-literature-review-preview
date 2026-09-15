# Human review

The human reviewer receives one final, paper-scoped decision surface. Show the cited original page beside the complete published MAT/MIX records, with previous/next paper and page navigation, field-level source location, and one accept or reject decision for the paper.

## Keep automatic triage internal

- Semantic conflicts, broken evidence locality, failed conversions, unresolved relationships, and internal anomaly clusters belong to automatic extraction, repair, quarantine, and independent Agent review.
- Do not publish a second anomaly dashboard, cluster-scoped accept/reject controls, repair requests, internal field paths, coverage-gain counters, or suggested resolutions to the human reviewer.
- Internal clustering may group repeated failures when it saves model calls, but it is diagnostic state rather than a human review product. Preserve its audit trail outside the public review client.
- Generated fields absent from a narrower handoff reference are coverage gains unless an independent contract says the reference is exhaustive. Resolve that distinction automatically before publication.
- Missing values remain typed nulls. Show their registered meaning, such as `not_reported` or `not_applicable`; never present null as zero.

## Keep the final decision obvious

- Acceptance is paper-scoped and applies only to the already published MAT/MIX result shown on the page.
- Rejection requires a reviewer note and remains local and exportable. Bind exported decisions to the queue hash.
- Do not disable acceptance based on which tabs the reviewer visited. Navigation history is not evidence and must not be a hidden prerequisite.
- The only reviewer-facing data tabs are MAT and MIX. Quarantined or unresolved records must stay out of the final public queue rather than appearing as another tab.
- Show only the selected field's evidence box, sized outside the source token so the outline does not cover the value.

## Measure efficiency honestly

Separate machine runtime from human review time. A deterministic benchmark may report the manual baseline, machine duration, papers prepared, fields checked automatically, and final paper decisions remaining. It must label projected human time as a break-even threshold until a person completes a timed review. Browser click latency or scripted navigation is not cognitive review time.

Keep review decisions local and offline unless the user separately authorizes a live WAKG write. An inability to automate visual browser QA is an environment limitation, not evidence that the UI passed or failed.

## Publish previews without exposing source papers

- Treat public hosting as a separate external mutation. A request to build or test the review UI does not authorize repository deletion, renaming, visibility changes, or publication.
- Publish page images and DOI links, never source-paper or supplement bytes. Bind approval to a deterministic hash of the complete sanitized site, not only `queue.json`, then verify every published Git blob against that approved bundle.
- Before sharing a public-repository URL, enumerate every reachable commit and tree for source-PDF paths, require zero public forks, and fail closed on truncated history/tree responses. A clean current branch is insufficient when an older commit still exposes a paper.
- If source papers entered public history, keep the link unshared until the user explicitly authorizes remediation. Prefer moving the exposed repository to a private quarantine and letting a separately verified clean repository assume the public name; preserve the old history privately and keep repository deletion disabled unless separately and explicitly requested.
- After remediation, check every PDF-bearing historical commit and path through the commit API, raw-content route, archive route, current Pages route, and quarantine-repository route. Only explicit `404` or `410` proves anonymous unavailability; `403`, `429`, redirects, and `5xx` are inconclusive or reachable and must block PASS.
- Publish and verify the clean staging repository before changing the exposed repository. Re-read repository visibility, name, fork count, Pages state, branch, full bundle, and historical isolation after mutation. Persist a phase-specific failure artifact; never automatically make quarantined history public as rollback.

## Close evidence gaps before the reviewer

- A non-empty field may display `原文找到该值` only when it has a clickable page and bbox that survived token-in-bbox verification.
- First use deterministic exact-token, table-cell, merged-cell substring, and contextual paragraph localization. These operations do not spend model turns.
- Cluster unresolved fields by paper, page/table, and failure fingerprint. Invoke one read-only semantic evidence agent for the cluster, not one call per field.
- The agent returns only `LOCATED` with page, exact bbox, and a verbatim source token, or `REJECT` with a concise contradiction. The deterministic writer checks PDF hash, bbox bounds, and token presence before publishing.
- `REJECT` returns to extraction for repair or quarantine. Do not ask the final reviewer to search the paper, repair evidence, interpret an internal field path, or compensate for an automatic-stage failure.
- System-generated quarantine reasons and field paths are labeled as system processing notes, never as values found in the paper.
- Treat the reviewer-facing queue as a publication artifact, not a repair dashboard. Publish it atomically only after every automatic evidence/extraction request is closed, every formal reported value has verified clickable evidence, and no field retains `evidence_blocked` or stale repair wording. Keep non-closed request/defect envelopes under ignored internal assets; preserve the previous clean public queue until the new generation passes. The review client must reject any queue without an explicit `FINAL_REVIEW_READY` gate instead of rendering repair controls or asking the reviewer to wait.
- Run the deterministic candidate validator before the independent agent. Then bind the agent request to the exact Git HEAD, immutable PDF hashes, and final review-subject hash. The agent is read-only and must check field names/types, token-level evidence locality, conversions/formulas, MAT/MIX relationships, typed nulls, and scientific context. Any rejection returns to the automatic writer; it must never be shown as a repair task to the human reviewer.

## Keep source explanations deterministic

The reviewer-facing `来源说明` is a closed vocabulary, not free text. Store a stable template code and its exact label; reject publication when the code is unknown or the label differs from the registered template. Use only these meanings: direct source value, source-derived value, composite source value, source dash/no value, not reported, not applicable, located but not transcribed, candidate conversion, system processing note, and evidence blocked.

Keep page, section, table/figure number, verbatim source token, bbox, formula, and internal reason in their dedicated evidence or transformation fields. Never concatenate those dynamic details into `来源说明`, and never render an arbitrary per-field `reason` as a source explanation. The review client must fail closed when any field lacks a valid template code-label pair.

`SOURCE_LOCATED_NOT_TRANSCRIBED` and `SOURCE_EVIDENCE_BLOCKED` are internal workflow templates only. They must never reach final human review. Route the former back to the automatic extraction agent for transcription or evidence-backed reclassification as not reported, and route the latter through evidence repair or quarantine.
