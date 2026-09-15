# Bounded source-discovery benchmark

Use `discover-benchmark` only with a committed query manifest and explicit year,
request, byte, cost, and (when downscaling) `--query-limit` caps. D1 selects the
first four registered queries with `--query-limit 4`, with eight expected logical
pages and a twelve-attempt cap for bounded retry headroom, rather than treating the
remaining eight D2 queries as a request-budget failure. It queries only anonymous, allowlisted OpenAlex and
Crossref metadata endpoints. OpenAlex uses `per_page=100` and no more than five
pages per query; Crossref uses one `rows=250` response per query and serial public
pool pacing of at least 1.05 seconds. The implementation records a hash-validated
receipt for every page, keeps raw payloads and the full catalogue under ignored
`internal_assets`, and commits at most a compact top-20 source handoff.

The request cap is an actual HTTP-attempt cap: retries and failure receipts consume
it, while hash-validated cache replay consumes zero attempts. Raw response reads use
a remaining byte cap plus one sentinel and oversized data is neither persisted nor
parsed. Default execution resumes only valid cached receipts; `--cache-only` must fail on a
missing or tampered receipt/payload pair and must make zero requests. Stop on page
identity loops, malformed envelopes, three provider failures, exhausted rate
headers, authentication challenges, or any configured request/cost/byte budget.

An explicit terminal HTTP failure receipt (for example, an exhausted 429) is not a
missing page. Cache-only replay accepts it only when its provider, query, page, URL
hash, status, attempt history, and error metadata match the requested page. New
schema receipts also require a deterministic failure fingerprint; legacy schema-1
receipts are replayed only under a clearly marked metadata-only compatibility path.
They replay as `cache_failure`, consume zero attempts and raw bytes, retain the
original failure provenance, and follow the same provider-page break behavior as
the live run. A missing, tampered, or mismatched terminal receipt remains
`CACHE_INCOMPLETE_OR_TAMPERED`, never a valid zero-result page.
Deduplicate by DOI, then nonempty normalized title plus authors, then OpenAlex
identifier; rows without a stable identity are counted and excluded rather than
collapsed. Retain provider and query-stratum membership and record gate metrics for
actual attempts, raw bytes, cache hits, stable identities, and provider errors.

For a bounded expansion such as D2, `--seed-run-id` may name an earlier run in the
same project. Current-run receipts always take priority. On an exact cache miss the
pipeline verifies the seed provider, query, page, URL hash, receipt hash, and payload
hash, then materializes a current-run receipt with `source=cache_seed`; it consumes
zero HTTP attempts and raw bytes. A tampered, mismatched, unsafe, or self seed is
rejected or ignored without a request. After materialization, current-run
`--cache-only` replay remains wholly local.

`link-probe` is a separate bounded availability check. It accepts either explicit
URLs or a catalog, never both. Catalog targets are unique HTTPS `fulltext_url`
entries selected deterministically with host and query-stratum balancing; reports
retain only URL hashes, hostnames, selection-order hashes, and compact identities.
Use `--probe-offset` for disjoint windows, so a later extension never re-probes the
first window. The probe is serial and bounded by request timeout, wall time, and a
maximum range read of `byte_cap + 1`; it never persists a response body or PDF.

Each target receives HEAD first. HTTP 405/501, or a successful non-PDF HEAD, may
use one bounded range GET to distinguish PDF magic, reachable non-PDF content, and
protocol failure. It preserves final HTTP status, redirected hostname, and content
type; it never range-gets after 401, 403, or 429. A D3-30 gate requires 30 selected
and attempted targets when available, zero 429, under 20% transport/protocol
errors, at least 80% reachable endpoints, and at least five selected domains before
an offset-30 extension is authorized. A low direct-PDF fraction is descriptive, not
a software failure. A benchmark report describes source coverage; it never
establishes scientific evidence or an extraction result.

`oa-route-resolve` is an offline-only route-ranking step for an already frozen
link-probe cohort. It consumes a cached or fixture OpenAlex envelope and never
fetches metadata itself. It ranks only HTTPS `location.is_oa=true` routes,
retains all qualified routes internally, and commits only hashes and aggregate
metadata. Repository and preprint OA sources precede publisher sources for
anonymous automation; a direct PDF precedes a landing URL within the same source
class. A Crossref `application/pdf` link stays `METADATA_FULLTEXT_LINK` until a
current recognized open licence and compatible content-version evidence supports
promotion. An opt-in link-probe diagnostic may make one bounded Range GET after
an original 403; it does not bypass access controls and verifies a PDF only from
a 2xx PDF content type or PDF magic response.

After a bounded route rescue, use `oa-rescue-summary` rather than hand-authoring
a `SOURCE_RESULT`. It validates the frozen cohort, reviewed Git head,
receipt/payload hashes, replay-zero accounting, and probe semantics, then emits a
body-free envelope with exactly one `reviewed_head` field.

For a prospective route-aware ranking canary, first run `oa-rank-ab-pool` against
a frozen catalog and an exact previously-probed exclusion set. It freezes a
100-candidate DOI/HTTPS pool with host and query-stratum diversification, but it
does not treat URL presence as verified access. `oa-rank-ab-plan` consumes exactly
one offline OpenAlex DOI envelope and freezes both legacy and direct-PDF-OA arms
before probing; landing-only locations cannot enter the route-aware arm. The
internal union mapping preserves every `(identity, arm, URL hash)` association so
shared identities with different URLs and shared URL reuse cannot be collapsed by
identity. Only `oa-rank-ab-summary` may score the later probe; it validates the
frozen plan, replay proof, caps, and relevance guardrails, treats only PDF
content-type or magic classes as verified, and emits a body-free `SOURCE_RESULT`.
When a source task receives the frozen pool catalogue from outside its temporary
project, `oa-rank-ab-plan` first verifies its exact manifest hash and stages the
same bytes only at the manifest-declared path under that project's
`internal_assets`. A staging path outside that root or mismatched bytes is a hard
failure. A source task that cannot produce the deterministic summary must return
an explicit body-free failure envelope with the fault fingerprint and artifact
hashes; it must not end silently or hand-author a result.

For a successor formulation, run `run_oa_hybrid_canary.py prepare` only against
the frozen previous pool, metadata, arm, and probe evidence. It strips only the
registered `Accessible/recent` component, selects a fixed-grid route bonus
deterministically, and requires viable tuning before any disjoint prospective
pool is frozen. `TUNE_BLOCKED` has no live authority.

The distinct preverified-ranking formulation uses `run_oa_preverified_canary.py`.
It freezes a fresh excluded pool and A first, freezes content-ranked P plus each
pre-probe route before a single deduplicated union probe, and constructs V only
from verified P rows after that probe. Unknown or unverified P rows cannot pad
V. The state, replay, verifier, and failure envelope remain body-free.
