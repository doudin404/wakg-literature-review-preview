# WAKG V1.1.4 offline contract

This contract is based on the supplied exchange template and a read-only inspection of the WAKG V1.1.4 entry UI. It is an offline interchange contract, not the live database schema.

## Core relationships

```text
papers 1 -- n mats
papers 1 -- n mixes
mats    n -- n mixes through modules.materials.mat_refs
papers 1 -- n assets
record field -- evidence link -- optional asset
```

Create MAT records before MIX records. Every `mat_key` referenced by a MIX must exist in the same accepted snapshot. A material may be reused by multiple MIX records.

## MAT

Keep the UI order: identity/source, physical properties, PSD, XRF, FT-IR, XRD/QXRD, 29Si NMR, and 27Al NMR.

Canonical physical units are m2/kg, kg/m3, and percent. Record the specific-surface method when reported. Preserve D10/D50/D90 in um. Preserve XRF rows, original total, any normalization candidate, and confirmation status. Store spectra or diffraction point sets as assets rather than database blobs.

## MIX

One MIX record contains five modules:

1. `identity_source_specimen`
2. `materials`
3. `mixing_curing`
4. `performance`
5. `characterizations`

The materials module may reference completed MAT records and contains precursor, activator, fine aggregate, coarse aggregate, reported mass basis, platform ratios, conversions, and review state. Performance is an array; each observation carries value, unit, age, specimen, method, and evidence. Curing is a sequence of stages, not one flattened value.

## Evidence and assets

Every accepted scientific value needs field provenance containing its evidence key, original value/unit, transformation formula when applicable, extraction method, confidence, and review status. Evidence links locate a page and optionally a section, figure, table, and bbox.

Assets use relative paths and SHA256. Full papers and supplements remain internal and are not committed or exported by default.

## Live-platform boundary

The current UI has no verified bulk-import interface and does not visibly expose field-level evidence inputs. Do not automate form entry. Preserve the richer offline evidence graph so a later import mapping cannot erase provenance.
