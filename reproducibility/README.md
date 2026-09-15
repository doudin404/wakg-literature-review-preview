# WAKG literature extraction reproduction bundle

This directory contains the source used for the ten-paper extraction and the
GitHub Pages projection at the repository root. The published scientific result
is `../review-assets/queue.json`; its curve CSV and preview images are under
`../review-assets/resume-curves/`.

## Implemented flow

1. `scripts/paper_native_flow.py` reads the PDF text layer, repairs font mappings,
   extracts native table cells, and prepares text, tables, and figure captions.
2. One whole-paper Agent extracts text and table data and creates figure tasks.
   It does not receive figure images during this stage.
3. `scripts/paper_chart_bridge.py` routes the figure tasks. Curves are scanned
   first; the image Agent then selects and labels the scanned candidates. Simple
   non-curve figures can be read directly, with the same scanner available as a
   tool.
4. The assembler writes `generated-records.json`, evidence locations, curve CSV
   files, and overlay geometry.
5. `review-ui/scripts/freeze_web_projection.py` freezes one reviewed projection.
   `review-ui/scripts/update_full10_native_preview.py` publishes either one data
   root or one frozen projection, so historical result directories are not mixed.

The complete Codex Skill and its references are in
`skills/wakg-literature-pipeline/`. The `fixtures/` directory contains the
field mappings, figure routes, and reviewed corpus-specific bindings used by
the included modules.

## Environment

- Python 3.12
- Codex CLI 0.154 or newer, signed in locally
- Packages in `requirements.txt`

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

## Run one supplied paper

```powershell
python scripts/paper_native_flow.py full `
  --pdf C:\path\to\paper.pdf `
  --output C:\path\to\run
```

Outputs include the parsed source packet, Agent response, assembled records,
source locations, figure bindings, curve data, and an assembly report.

To replay the deterministic stages from the saved draft and figure bindings:

```powershell
python scripts/paper_native_flow.py reproduce --output C:\path\to\run
```

## Build and publish the review page

Build from one directory containing the paper result folders:

```powershell
python review-ui/scripts/update_full10_native_preview.py C:\path\to\site `
  --data-root C:\path\to\paper-results
```

Freeze the reviewed page, then reproduce it byte-for-byte:

```powershell
python review-ui/scripts/freeze_web_projection.py C:\path\to\site `
  --records-root C:\path\to\paper-results `
  --output C:\path\to\fixed-projection

python review-ui/scripts/update_full10_native_preview.py C:\path\to\site-copy `
  --projection-root C:\path\to\fixed-projection
```

Run `python verify_bundle.py --site ..` to compile the included Python source and
check that every curve file referenced by the published queue exists.

## Inputs

Supply the paper PDFs and supplements locally. They are intentionally separate
from this source bundle. Model authentication remains in the local Codex CLI.
