"""Enumerate source runs by declared receipts, never by corpus name prefixes."""
import json
from pathlib import Path


def source_run_dirs(output_root):
    root = Path(output_root).resolve()
    names = set()
    for filename in ('build-summary.json', 'subset-build-summary.json'):
        path = root / filename
        if path.is_file():
            for paper in json.loads(path.read_text(encoding='utf-8'))['papers']:
                name = paper['run_id']
                if not isinstance(name, str) or not name or name in ('.', '..') or '/' in name or '\\' in name or ':' in name:
                    raise ValueError('invalid source run identity')
                names.add(name)
    # Interrupted builds may not yet have published a batch summary.
    for path in root.iterdir():
        if path.is_dir() and (any((path / marker).is_file() for marker in (
            'generated-records.json', 'extraction-plan.json', 'evidence-manifest.json'))
            or any(path.glob('build-failure-*.json'))):
            names.add(path.name)
    result = [root / name for name in sorted(names)]
    if any(not path.resolve().is_relative_to(root) for path in result):
        raise ValueError('source run outside output root')
    if not result:
        raise ValueError('no source runs declared or generated')
    return result
