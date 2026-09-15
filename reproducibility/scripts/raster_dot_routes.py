"""Persist replayed dot-geometry candidates with exact canonical owner binding."""
import hashlib
import json
import tempfile
from pathlib import Path

from raster_curves import atomic_bytes, digest
from raster_dot_percentiles import build


def enrich(records, pdf, route, run_dir):
    items = []
    candidates = records['extensions']['raster_curve_candidates']['items']
    for config in route['dot_derivations']:
        owners = [i for i in candidates if i['label'] == config['label']]
        if len(owners) != 1 or owners[0]['owner_status'] != 'EXACT_MATCH' or not owners[0]['record_key']:
            raise ValueError('dot candidate requires an exact canonical owner')
        owner = owners[0]
        with tempfile.TemporaryDirectory(prefix='wakg-dot-route-') as temporary:
            output = Path(temporary)/'derivation.json'
            build(pdf, Path(route['specification_path']), run_dir/'raster-assets',
                  config['label'], config['max_fragment_width'], output)
            raw = output.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        relative = 'raster-dot-assets/'+digest+'.json'
        path = run_dir/relative
        if path.exists() and path.read_bytes() != raw:
            raise ValueError('dot candidate artifact conflict')
        if not path.exists():
            atomic_bytes(path, raw)
        report = json.loads(raw)
        items.append({'label': config['label'], 'record_type': 'mat',
                      'record_key': owner['record_key'], 'owner_status': 'EXACT_MATCH',
                      'source_pdf_asset_key': owner['source_pdf_asset_key'],
                      'source_curve_asset_key': owner['data_asset_key'],
                      'source_pixel_geometry_sha256': owner['pixel_geometry_sha256'],
                      'path': relative, 'sha256': digest,
                      'percentiles': report['percentiles'], 'status': 'CANDIDATE_NOT_FORMAL'})
    receipt = {'formal_values_added': 0, 'items': items}
    previous = records['extensions'].get('raster_dot_candidates')
    if previous is not None and previous != receipt:
        raise ValueError('dot candidate receipt conflict')
    records['extensions']['raster_dot_candidates'] = receipt


def artifact_receipt(records, run_dir):
    receipt = records.get('extensions', {}).get('raster_dot_candidates')
    if receipt is None:
        return []
    if receipt.get('formal_values_added') != 0:
        raise ValueError('dot candidate cannot authorize formal values')
    result = []
    seen = set()
    for item in receipt['items']:
        relative = item['path']
        path = (run_dir/relative).resolve()
        if (relative in seen or not path.is_relative_to((run_dir/'raster-dot-assets').resolve())
                or hashlib.sha256(path.read_bytes()).hexdigest() != item['sha256']):
            raise ValueError('dot candidate artifact hash or path mismatch')
        seen.add(relative)
        report = json.loads(path.read_bytes())
        if report['percentiles'] != item['percentiles'] or report['formal_values_added'] != 0:
            raise ValueError('dot candidate payload mismatch')
        originals = records['extensions']['raster_curve_candidates']
        bound = [i for i in originals['items'] if i['label'] == item['label']]
        if len(bound) != 1 or report['label'] != item['label']:
            raise ValueError('dot source label mismatch')
        original = bound[0]
        bindings = {'record_key':'record_key', 'source_pdf_asset_key':'source_pdf_asset_key',
                    'source_curve_asset_key':'data_asset_key',
                    'source_pixel_geometry_sha256':'pixel_geometry_sha256'}
        if any(item[k] != original[v] for k,v in bindings.items()):
            raise ValueError('dot canonical source binding mismatch')
        sources = [a for a in records['assets'] if a['asset_key'] == item['source_pdf_asset_key']]
        if (len(sources) != 1 or sources[0]['kind'] != 'pdf' or
                report['source_proof']['pdf_sha256'] != sources[0]['sha256'] or
                report['source_proof']['report_sha256'] != originals['report_sha256'] or
                report['source_proof']['specification_sha256'] != originals['specification_sha256'] or
                digest(report['derivation']['source_pixel_points']) != item['source_pixel_geometry_sha256']):
            raise ValueError('dot original report or pixel geometry mismatch')
        owners = [m for m in records['mats'] if m['mat_key'] == item['record_key']]
        if len(owners) != 1 or owners[0]['custom_material_id'] != item['label']:
            raise ValueError('dot candidate owner drift')
        result.append({'path': relative, 'sha256': item['sha256']})
    return result
