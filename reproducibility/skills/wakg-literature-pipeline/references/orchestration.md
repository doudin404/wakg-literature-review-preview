# Multi-task orchestration

## Durable roles

The initiating project task is the event-driven coordinator. It creates or reuses exactly these workers in the saved local project:

| Role | Model | Effort | Writes project files |
| --- | --- | --- | --- |
| `SOURCE_SCOUT` | `gpt-5.6-luna` | `low` | No |
| `EXECUTION_CONTROL` | `gpt-5.6-terra` | `high` | Yes, exclusively |
| `ACCEPTANCE_REVIEW` | `gpt-5.6-luna` | `medium` | No |

Source and acceptance tasks may use temporary files outside the project. They must not edit, commit, create tasks, or change canonical state. The execution task must not create another writer.

Canonical runtime state is `control/state.sqlite3`; `control/state.json` is a disposable readable projection. The controller owns job creation, leases, attempts, artifacts, claims, issues, record versions, usage events, and snapshots. A worker lease must expire before another writer can recover that job. Stage success is not a cache hit unless the referenced artifact still exists and its SHA256 matches.

Create tasks just in time: source first, execution after a source result, acceptance after an extraction commit. Reuse their verified task IDs for later papers. Wait for task events instead of polling.

## Compact handoffs

Every message has a unique `message_id`, stable `pair_id`, exact project path, and one requested action.

`SOURCE_RESULT` contains:

```json
{"type":"SOURCE_RESULT","message_id":"...","pair_id":"...","query_version":"...","candidates":[],"recommended_id":"...","blockers":[]}
```

For a prospective route-aware source canary, `oa-rank-ab-summary` is the only
producer of the final `SOURCE_RESULT`. It must reference a frozen pre-probe pool
and arm plan and report `PASS`, `PARTIAL`, `FAIL`, or `INVALID`; a source result
does not authorize acquisition, extraction, WAKG import, or scale expansion.
If deterministic planning or summary fails, `SOURCE_SCOUT` returns one body-free
`SOURCE_RESULT` failure envelope with `verdict: INVALID`, the stable fault
fingerprint, and artifact hashes. It must stage an externally supplied pool only
through the manifest-declared `internal_assets` path after exact hash validation;
it never silently completes a repair turn without an envelope.

A hybrid-ranking source task invokes the reusable driver rather than manually
repeating metadata, planning, probing, summary, and replay mechanics. `prepare`
is offline; a future `live` requires explicit opt-in, a frozen experiment-card
hash, a viable bound profile, and a budget manifest. Its atomic state records
idempotency keys and advances through cached metadata, frozen A/C arms, one
bounded probe, summary, replay, and independent verification. Every driver
exception emits a body-free envelope with counters derived from that state. A tuning cohort selects a
weight only and never supplies prospective performance authority.

Limit candidates to 20. Each candidate includes DOI, normalized title, year, access status, metadata sources, score, score explanation, and a verified or candidate full-text URL.

For a frozen route-rescue cohort, generate the source control envelope with
`oa-rescue-summary`. A hand-authored or conflicting head field is untrusted
evidence, not a valid handoff.

`EXTRACTION_READY` contains:

```json
{"type":"EXTRACTION_READY","message_id":"...","pair_id":"...","run_id":"...","head":"...","manifest_path":"...","records_sha256":"...","known_exceptions":[],"validation_command":"..."}
```

`ACCEPTANCE` contains:

```json
{"type":"ACCEPTANCE","message_id":"...","pair_id":"...","run_id":"...","head":"...","verdict":"PASS|REJECT|ESCALATE","checks":[],"defects":[],"evidence_paths":[]}
```

Do not send acknowledgements, full histories, unchanged status, or copied artifacts. Reference exact paths and hashes.

## Repair and cost gates

- One normal source turn, one extraction turn, and one acceptance turn per paper.
- Deterministic parsing, caching, hashing, schema checks, and retries happen without another model turn.
- The extractor may inspect at most three rendered pages with vision in one normal turn.
- A rejection returns one consolidated defect list. The writer gets at most two repair rounds; the same defect fingerprint twice is `BLOCKED_REPEATED_DEFECT`.
- Use `ESCALATE` only for evidence ambiguity that deterministic checks cannot settle. Never upgrade models for ordinary formatting or transport failures.
- Before any repair, the sole writer appends an `OBSERVED` fault event with a stable fingerprint and reproduction/evidence pointers. A repair appends `RESOLVED`; independent rerun evidence appends `VERIFIED`. Never remove the earlier event after the test turns green.
- A final `EXTRACTION_READY` or `ACCEPTANCE` handoff includes the campaign `fault-report.json` path and unresolved fault IDs. A green scientific verdict does not imply an empty fault history.
- Evidence locality repair is an automatic pre-review substage inside `EXECUTION_CONTROL`. After deterministic localization, group unresolved locators once per paper/table and invoke a read-only `gpt-5.6-luna / medium` semantic agent. Verify its page, bbox, and verbatim token deterministically. Do not create a durable fourth project task, do not call the model per field, and do not expose unresolved source lookup work to the final human reviewer.

## Compact pre-human review

Do not hand the 1+ MB reviewer candidate to an agent with only run IDs. Build three compact shards first:

```text
python skills/wakg-literature-pipeline/scripts/prehuman_review_packets.py build --project . --candidate internal_assets/pre-human-agent-review/candidate-queue.json --request internal_assets/pre-human-agent-review/agent-review-request.json --out-dir internal_assets/pre-human-agent-review/review-packets --shards 3
python skills/wakg-literature-pipeline/scripts/prehuman_review_packets.py verify --project . --plan internal_assets/pre-human-agent-review/review-packets/plan.json --write internal_assets/pre-human-agent-review/review-packets/proof.json
```

The packet contains every formal claim, but groups nearby locators from one record and page into a shared source context. The verifier reopens the original PDF and exhaustively checks packet identity, PDF hash, context text, bbox containment, controlled source codes, formula presence, canonical performance value/age index alignment, and claim counts. A semantic reviewer reads one packet, trusts only the verifier for mechanical locator containment, checks row/header meaning and every derived/composite formula, and returns one consolidated result per paper. It does not scan the full candidate, runs, Git history, or one file per field.

After an exhaustive PASS, persist a proof seal over the candidate, plan, proof, packet JSON, semantic sidecars, and every source PDF hash. An unchanged rerun may reuse that sealed proof and semantic acceptance without reopening every page. Any missing seal, changed HEAD/subject/protocol/contract, changed packet/sidecar, or changed PDF byte invalidates the cache and forces a complete verifier pass.

Every response must repeat its `packetId` and exact protocol/subject/contract identity. Merge only through the shard-aware validator; extra, missing, duplicate, or cross-packet papers fail closed:

```text
python scripts/record_prehuman_agent_acceptance.py --request internal_assets/pre-human-agent-review/agent-review-request.json --plan internal_assets/pre-human-agent-review/review-packets/plan.json --response runs/.../packet-01-response.json --response runs/.../packet-02-response.json --response runs/.../packet-03-response.json --output runs/.../acceptance.json
```

After merging the acceptance responses, audit the exact rollout window and enforce the cost gate:

```text
python skills/wakg-literature-pipeline/scripts/prehuman_review_packets.py budget --plan internal_assets/pre-human-agent-review/review-packets/plan.json --usage runs/.../usage-report.json --acceptance runs/.../acceptance.json --proof internal_assets/pre-human-agent-review/review-packets/proof.json --write runs/.../budget-verdict.json
```

For ten papers, require `uncached_input + output < 1,000,000` and at most 24 model events (three compact shards plus at most one protocol retry). The usage report must contain only the assigned `ACCEPTANCE_REVIEW` or `PRE_HUMAN_AGENT` tasks. The budget report is a local rollout proxy, not official credit accounting. A limit failure is `BLOCKED_BUDGET`; it never authorizes sampling fewer formal claims, skipping typed nulls/relationships/formulas, or accepting incomplete locator coverage. Publication requires the passing budget verdict explicitly.

When auditing local rollout files, run from the project root and omit `--project-cwd` (or pass `.`). The auditor resolves the real Unicode working directory itself; never copy a mojibake path rendered by a terminal. Continue to list exact session files and provide a complete session-to-role label map so unrelated tasks cannot enter the usage window.
