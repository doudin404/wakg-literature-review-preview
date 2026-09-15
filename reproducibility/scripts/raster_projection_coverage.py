"""Explicit independently reviewed scalar-versus-curve projection coverage."""
import hashlib
from pathlib import Path

PERCENTILES = ('d10_um', 'd50_um', 'd90_um')


def resolve(acceptance, labels):
    labels = set(labels)
    if 'projection_coverage' not in acceptance:
        return {label: {'percentiles': list(PERCENTILES), 'curve': True} for label in labels}
    if acceptance.get('coverage_implementation_sha256') != hashlib.sha256(Path(__file__).read_bytes()).hexdigest():
        raise ValueError('raster coverage implementation mismatch')
    coverage = acceptance['projection_coverage']
    if not isinstance(coverage, dict) or set(coverage) != labels:
        raise ValueError('raster projection coverage series mismatch')
    for item in coverage.values():
        if not isinstance(item, dict) or set(item) != {'percentiles','curve'}:
            raise ValueError('invalid raster projection coverage fields')
        selected = item['percentiles']
        if (type(item['curve']) is not bool or not isinstance(selected,list)
                or any(not isinstance(p,str) or p not in PERCENTILES for p in selected)
                or len(set(selected)) != len(selected) or not (selected or item['curve'])):
            raise ValueError('invalid raster projection coverage values')
    return coverage
