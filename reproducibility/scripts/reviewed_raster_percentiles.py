"""Exact reviewed dot derivations for raster projection; never implicit fallback."""
import copy
import hashlib
import json
import tempfile
from pathlib import Path

import raster_dot_percentiles
from raster_curves import atomic_bytes
from raster_dot_routes import artifact_receipt


def resolve(records, specification, run_dir, source_pdf, acceptance):
    requested = acceptance.get('dot_derivations', {})
    if not requested:
        return {}
    if not isinstance(requested, dict):
        raise ValueError('invalid reviewed dot derivations')
    expected = {'dot_derivation_implementation_sha256': hashlib.sha256(Path(raster_dot_percentiles.__file__).read_bytes()).hexdigest(),
                'dot_adapter_implementation_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    if any(acceptance.get(k) != v for k,v in expected.items()):
        raise ValueError('reviewed dot implementation mismatch')
    artifact_receipt(records,run_dir)
    items = records.get('extensions',{}).get('raster_dot_candidates',{}).get('items',[])
    if len(items) != len(requested) or {i['label'] for i in items} != set(requested):
        raise ValueError('reviewed dot series coverage mismatch')
    resolved = {}
    for item in items:
        if requested[item['label']] != item['sha256']:
            raise ValueError('reviewed dot report hash mismatch')
        original = (run_dir/item['path']).read_bytes()
        report = json.loads(original)
        with tempfile.TemporaryDirectory(prefix='wakg-dot-formal-proof-') as temporary:
            temporary = Path(temporary)
            spec = temporary/'spec.json'
            atomic_bytes(spec,json.dumps(specification,sort_keys=True).encode())
            replay = temporary/'replay.json'
            raster_dot_percentiles.build(source_pdf,spec,run_dir/'raster-assets',item['label'],
                                         report['derivation']['max_fragment_width_px'],replay)
            if replay.read_bytes() != original:
                raise ValueError('reviewed dot source replay mismatch')
        resolved[item['label']] = {'percentiles':copy.deepcopy(report['percentiles']),
                                  'pixel_uncertainty':report['effective_pixel_uncertainty'],
                                  'report_path':item['path'],'report_sha256':item['sha256']}
    return resolved
