"""Bind panel-local observations to source-specific recipes, not reference rows."""
from main_marker_source_context import bind as bind_source
from source_specimen_variants import digest


def bind(records, report):
    context = bind_source(records, report)
    if context != report['source_context']:
        raise ValueError('main marker source context changed')
    paper = context['formulation_binding']['pdf_asset_key']
    assets = [a for a in records['assets'] if a['asset_key'] == paper]
    if len(assets) != 1:
        raise ValueError('main marker parent asset ambiguous')
    parent = assets[0]['paper_key']
    if [p['paper_key'] for p in records['papers']] != [parent]:
        raise ValueError('main marker parent paper mismatch')
    mixes = {m['mix_key']: m for m in records['mixes']}
    if len(mixes) != len(records['mixes']):
        raise ValueError('main marker duplicate recipe key')
    variants = records.get('extensions', {}).get('specimen_variants', {}).get('assignments', [])
    variant_ids=[(v.get('custom_test_id'),v.get('specimen')) for v in variants]
    if not variants or len(set(variant_ids))!=len(variant_ids):
        raise ValueError('main marker specimen-specific recipes missing')
    panels = {p['panel']: p for p in report['panel_context']}
    if len(panels)!=len(report['panel_context']):
        raise ValueError('main marker duplicate panel context')
    source = {a['x']: a for a in context['formulation_binding']['assignments']}
    if len(source)!=len(context['formulation_binding']['assignments']):
        raise ValueError('main marker ambiguous source coordinate')
    owners = []
    for panel in report['digitization']['panels']:
        specimen = panels[panel['label']]['specimen']
        for series in panel['series']:
            for index, point in enumerate(series['points']):
                assignment = source[point['x']]
                source_id = assignment['source_test_id']
                expected_key = 'mix-specimen-' + digest([parent, source_id, specimen])[:24]
                matches = [v for v in variants if v['custom_test_id'] == source_id and v['specimen'] == specimen]
                if len(matches) != 1 or matches[0]['mix_key'] != expected_key:
                    raise ValueError('main marker source specimen owner missing or changed')
                owner = mixes.get(expected_key)
                if owner is None or owner['paper_key'] != parent:
                    raise ValueError('main marker cross-paper or missing recipe')
                identity = owner['modules']['identity_source_specimen']
                recipe = owner.get('extensions', {}).get('recipe_source_binding', {})
                if (identity['custom_test_id'] != source_id or identity['specimen_type'] != specimen
                        or matches[0]['reference_mix_key'] != assignment['mix_key']
                        or recipe.get('reference_mix_key') != assignment['mix_key']
                        or recipe.get('precursor_mass_percent') != assignment['precursor_mass_percent']
                        or recipe.get('source_statements') != context['formulation_binding']['source_statements']):
                    raise ValueError('main marker specimen recipe relationship mismatch')
                owners.append({'panel': panel['label'], 'series': series['name'], 'point_index': index,
                    'x': point['x'], 'mix_key': expected_key, 'source_test_id': source_id,
                    'specimen': specimen, 'precursor_reference_mix_key': assignment['mix_key']})
    if len(owners) != report['observation_count']:
        raise ValueError('main marker observation owner coverage mismatch')
    return {'schema_version': 1, 'scope': 'source_bound_candidate_owners_not_formal_acceptance',
            'assignments': owners}
