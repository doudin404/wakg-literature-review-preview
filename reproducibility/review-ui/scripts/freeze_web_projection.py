"""Freeze the currently reviewed web projection as one reproducible input."""
import argparse
import copy
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from source_quantity_producer import atomic_json


def freeze(site, records_root, output):
    site, records_root, output = map(Path, (site, records_root, output))
    queue = json.loads((site / 'review-assets/queue.json').read_bytes())
    public_assets = output / 'review-assets'
    curve_assets = public_assets / 'resume-curves'
    curve_assets.mkdir(parents=True, exist_ok=True)
    shutil.copy2(site / 'review-assets/queue.json', public_assets / 'queue.json')
    for paper in queue['papers']:
        for curve in paper.get('curves', {}).values():
            for key in ('pointsUrl', 'sourceImageUrl'):
                published = site / curve[key]
                shutil.copy2(published, curve_assets / published.name)
    by_doi = {paper['doi']: paper for paper in queue['papers']}
    rows = []
    for source in sorted(records_root.glob('1-s2.0-*-main')):
        records_path = next((source / name for name in ('records.json', 'generated-records.json')
                             if (source / name).is_file()), None)
        if records_path is None:
            continue
        records = json.loads(records_path.read_bytes())
        doi = records['papers'][0]['doi']
        paper = by_doi[doi]
        folder = output / source.name
        assets = folder / 'native-curves'
        assets.mkdir(parents=True, exist_ok=True)
        curves = copy.deepcopy(paper.get('curves', {}))
        for curve in curves.values():
            for key in ('pointsUrl', 'sourceImageUrl'):
                published = site / curve[key]
                target = assets / published.name
                shutil.copy2(published, target)
                curve[key] = 'native-curves/' + target.name
        shutil.copy2(records_path, folder / 'generated-records.json')
        atomic_json(folder / 'review-cards.json', paper['records'])
        atomic_json(folder / 'curves.json', curves)
        rows.append({'doi': doi, 'paper': source.name, 'curves': len(curves)})
    atomic_json(output / 'projection-manifest.json', {
        'source': str(site.resolve()), 'recordsRoot': str(records_root.resolve()),
        'papers': rows, 'purpose': 'Single input for rebuilding the reviewed public preview.'})
    return {'papers': len(rows), 'curves': sum(row['curves'] for row in rows), 'output': str(output.resolve())}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('site', type=Path)
    parser.add_argument('--records-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(freeze(args.site, args.records_root, args.output), ensure_ascii=False))
