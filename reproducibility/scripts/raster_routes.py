"""PDF-identity routing for source-only raster extraction, not review authority."""
import hashlib
import json
import re
from pathlib import Path

REGISTRY = 'fixtures/raster-routes-v1.json'


def select(root, pdf_sha256):
    root = Path(root).resolve()

    def read(relative):
        if not isinstance(relative, str) or Path(relative).is_absolute():
            raise ValueError('raster route requires project-relative path')
        path = (root / relative).resolve()
        if not path.is_relative_to(root):
            raise ValueError('raster route escapes project')
        raw = path.read_bytes()
        return json.loads(raw), hashlib.sha256(raw).hexdigest()

    registry, registry_hash = read(REGISTRY)
    if registry.get('schema_version') != 1 or not isinstance(registry.get('routes'), list):
        raise ValueError('invalid raster registry')
    selected = None
    seen = set()
    for route in registry['routes']:
        if set(route) not in ({'specification', 'acceptance'}, {'specification', 'acceptance', 'dot_derivations'}):
            raise ValueError('invalid raster route fields')
        dots = route.get('dot_derivations', [])
        if not isinstance(dots, list):
            raise ValueError('dot derivations must be a list')
        labels = set()
        for dot in dots:
            if (not isinstance(dot, dict) or set(dot) != {'label', 'max_fragment_width'} or
                    not isinstance(dot['label'], str) or not dot['label'] or dot['label'] in labels or
                    type(dot['max_fragment_width']) is not int or dot['max_fragment_width'] < 1):
                raise ValueError('invalid or duplicate dot derivation')
            labels.add(dot['label'])
        spec, spec_hash = read(route['specification'])
        identity = spec.get('pdf_sha256')
        if not isinstance(identity, str) or not re.fullmatch('[0-9a-f]{64}', identity):
            raise ValueError('invalid raster source identity')
        if identity in seen:
            raise ValueError('duplicate raster PDF route')
        seen.add(identity)
        acceptance = acceptance_hash = None
        if route['acceptance'] is not None:
            acceptance, acceptance_hash = read(route['acceptance'])
            if acceptance.get('pdf_sha256') != identity:
                raise ValueError('raster approval source mismatch')
            if dots and (not isinstance(acceptance.get('dot_derivations'),dict) or
                         set(acceptance['dot_derivations']) != labels):
                raise ValueError('dot derivations require exact explicit formal review coverage')
        if identity == pdf_sha256:
            selected = {'specification': spec, 'acceptance': acceptance,
                        'specification_path': str(root / route['specification']), 'dot_derivations': dots,
                        'specification_sha256': spec_hash,
                        'acceptance_sha256': acceptance_hash}
    return selected, registry_hash


def input_hashes(root, pdf_sha256):
    route, registry_hash = select(root, pdf_sha256)
    return {'raster_registry_sha256': registry_hash,
            'raster_dot_router_sha256': hashlib.sha256((Path(__file__).parent/'raster_dot_routes.py').read_bytes()).hexdigest(),
            'raster_dot_derivation_sha256': hashlib.sha256((Path(__file__).parent/'raster_dot_percentiles.py').read_bytes()).hexdigest() if route and route['dot_derivations'] else None,
            'raster_dot_formal_adapter_sha256': hashlib.sha256((Path(__file__).parent/'reviewed_raster_percentiles.py').read_bytes()).hexdigest(),
            'raster_projection_coverage_sha256': hashlib.sha256((Path(__file__).parent/'raster_projection_coverage.py').read_bytes()).hexdigest(),
            'raster_router_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'raster_specification_sha256': route['specification_sha256'] if route else None,
            'raster_acceptance_sha256': route['acceptance_sha256'] if route else None}


def execute(root, pdf_sha256, records, pdf, run_dir, extractor, promoter, binder):
    route, _ = select(root, pdf_sha256)
    if route is None:
        return None
    # Candidate-only routes never call a promoter or present review authority.
    result = extractor.enrich_candidates(records, pdf, route['specification'], run_dir)
    if route['dot_derivations']:
        from raster_dot_routes import enrich
        enrich(records, pdf, route, run_dir)
    if route['acceptance'] is not None:
        promoter.promote(records, route['specification'], run_dir, route['acceptance'])
        binder.bind(records, route['specification'])
    return result
