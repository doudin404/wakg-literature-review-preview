"""Internal, evidence-bound process stages; not additional WAKG fields."""
import math
import copy

UNITS = {'duration': {'s', 'h', 'd', 'min'}, 'temperature': {'degC'},
         'relative_humidity': {'%'}, 'co2_concentration': {'%'},
         'pressure': {'Pa', 'kPa', 'MPa'}}


def references(value, facts):
    refs = value.get('fact_ids')
    if not isinstance(refs, list) or not refs or len(refs) != len(set(refs)) or any(r not in facts for r in refs):
        raise ValueError('stage/property requires valid fact references')


def validate_routes(entities, facts):
    for route in entities.values():
        if route['kind'] != 'curing_route':
            continue
        stages = route.get('stages')
        if not isinstance(stages, list) or not stages:
            raise ValueError('curing route requires structured stages')
        previous = None
        seen = set()
        for stage in stages:
            sid = stage.get('stage_id')
            if not isinstance(sid, str) or not sid or sid in seen:
                raise ValueError('missing or duplicate stage identity')
            if stage.get('after_stage_id') != previous:
                raise ValueError('stage order must explicitly reference its predecessor')
            if stage.get('kind') not in {'INITIAL_CURING', 'PREPARATION', 'EXPOSURE', 'STORAGE', 'CURING', 'TESTING'}:
                raise ValueError('unknown process stage kind')
            references(stage, facts)
            properties = stage.get('properties')
            if not isinstance(properties, dict) or 'duration' not in properties:
                raise ValueError('every stage requires duration, including explicit unknown')
            for name, prop in properties.items():
                if name not in UNITS or not isinstance(prop, dict):
                    raise ValueError('unknown or unstructured stage property')
                if 'value' not in prop or prop.get('unit') not in UNITS[name]:
                    raise ValueError('stage property value/unit missing or invalid')
                value = prop['value']
                if prop.get('relation', '=') not in {'=', '>', '>=', '<', '<='}:
                    raise ValueError('unsupported process comparison')
                if value is None:
                    if 'relation' in prop:
                        raise ValueError('unknown process value cannot carry a comparison')
                    if prop.get('missing_reason') not in {'NOT_REPORTED', 'NOT_DETERMINED'}:
                        raise ValueError('null stage value requires a missing reason')
                else:
                    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                        raise ValueError('stage value must be finite numeric or null')
                    if name != 'temperature' and value < 0:
                        raise ValueError('negative process value')
                    if name in {'relative_humidity', 'co2_concentration'} and value > 100:
                        raise ValueError('percentage exceeds 100')
                    references(prop, facts)
                    if prop.get('missing_reason') is not None:
                        raise ValueError('reported value cannot also be missing')
                if name == 'duration' and prop.get('time_origin') != 'STAGE_START':
                    raise ValueError('stage duration must not be silently treated as total specimen age')
                if 'uncertainty' in prop:
                    uncertainty = prop['uncertainty']
                    if isinstance(uncertainty, bool) or not isinstance(uncertainty, (int, float)) or not math.isfinite(uncertainty) or uncertainty < 0 or value is None:
                        raise ValueError('invalid uncertainty')
            seen.add(sid)
            previous = sid


def normalized_routes(entities, facts):
    """Normalize Agent stage candidates; never infer method, age or acceptance."""
    validate_routes(entities, facts)
    factors = {'duration': {'s': 1, 'h': 3600, 'd': 86400, 'min': 60},
               'pressure': {'Pa': 1, 'kPa': 1000, 'MPa': 1000000}}
    units = {'duration': 's', 'pressure': 'Pa'}
    routes = []
    for key, route in entities.items():
        if route['kind'] != 'curing_route':
            continue
        stages = []
        for stage in route['stages']:
            properties = {}
            for name, source in stage['properties'].items():
                factor = factors.get(name, {}).get(source['unit'], 1)
                value = source['value']
                output = None if value is None else value * factor
                normalized = {'value': output, 'unit': units.get(name, source['unit']),
                              'source': copy.deepcopy(source),
                              'fact_ids': copy.deepcopy(source.get('fact_ids', []))}
                if 'relation' in source:
                    normalized['relation'] = source['relation']
                if 'uncertainty' in source:
                    normalized['uncertainty'] = source['uncertainty'] * factor
                if name == 'duration':
                    normalized['time_origin'] = 'STAGE_START'
                if value is None:
                    normalized['missing_reason'] = source['missing_reason']
                    normalized['transformation'] = None
                else:
                    if not math.isfinite(output):
                        raise ValueError('normalized stage value overflow')
                    normalized['transformation'] = {
                        'formula': 'normalized = original * scale', 'scale': factor,
                        'original_value': value, 'original_unit': source['unit'],
                        'forward_value': output, 'reverse_value': output / factor}
                properties[name] = normalized
            stages.append({'stage_id': stage['stage_id'], 'after_stage_id': stage['after_stage_id'],
                           'kind': stage['kind'], 'fact_ids': copy.deepcopy(stage['fact_ids']),
                           'properties': properties})
        routes.append({'route_id': key, 'stages': stages, 'specimen_age_seconds': None,
                       'status': 'SOURCE_STAGE_CANDIDATE_NOT_ACCEPTED'})
    return routes
