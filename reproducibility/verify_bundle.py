"""Compile the reproduction source and check the published curve references."""
import argparse
import importlib.util
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--site', type=Path, default=Path('..'))
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    sources = sorted(root.rglob('*.py'))
    for source in sources:
        compile(source.read_bytes(), str(source), 'exec')

    entry = root / 'scripts' / 'build_phase_c_full10.py'
    spec = importlib.util.spec_from_file_location('phase_c_bundle_smoke', entry)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    site = args.site.resolve()
    queue = json.loads((site / 'review-assets/queue.json').read_bytes())
    curves = [curve for paper in queue['papers'] for curve in paper.get('curves', {}).values()]
    missing = []
    for curve in curves:
        for key in ('pointsUrl', 'sourceImageUrl'):
            if not (site / curve[key]).is_file():
                missing.append(curve[key])
    if missing:
        raise SystemExit('Missing published curve assets: ' + ', '.join(missing[:10]))
    print(json.dumps({'pythonFiles': len(sources), 'phaseCImport': 'PASS',
                      'papers': len(queue['papers']),
                      'curves': len(curves), 'missingCurveAssets': 0}))


if __name__ == '__main__':
    main()
