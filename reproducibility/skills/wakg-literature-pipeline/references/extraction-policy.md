# Extraction and acceptance policy

## Evidence first

Extract only what the paper or supplement supports. A missing, inaccessible, ambiguous, or structurally inapplicable value remains `null` with a reason; it is never zero. Keep short evidence snippets or hashes and exact page locations. Do not store long copyrighted passages.

For every transformed value retain the original value, original unit, exact formula, and both forward and reverse checks. If the source semantics are uncertain, keep a candidate in `candidates` and block acceptance of that field.

## Automatic operations

The deterministic core may safely perform:

- DOI/title/author/PDF-hash deduplication;
- exact unit conversions with reversible formulas;
- table parsing when headers and row alignment are explicit;
- referential, range, unit, JSON, SQLite, and SHA256 checks;
- extraction of page text, blocks, tables, embedded images, and low-text page previews.

## Review-gated operations

Keep these as unconfirmed candidates unless an exact evidence loop passes:

- converting cumulative PSD to a differential distribution;
- normalizing XRF totals, especially totals over 100 percent;
- converting absorbance and transmittance or assigning semantics to arbitrary intensity;
- image curve digitization and coordinate calibration;
- QXRD amorphous totals, internal/external standard assumptions, and phase identity;
- inferred material addition paths, mass bases, age origins, curing branches, and specimen relationships.

## Acceptance

Deterministic validation must pass before semantic review. The acceptance task then checks every critical field and, for a one-paper pilot, every other non-null scientific field against evidence.

Use exactly three verdicts:

- `PASS`: all formal values are evidence-backed; unresolved information is explicitly null or quarantined.
- `REJECT`: a concrete repair inside the frozen contract exists.
- `ESCALATE`: a scientific or semantic choice is required and no deterministic rule resolves it.

Incomplete but truthful data may pass. Invented, uncited, contradicted, or silently normalized data may not.
