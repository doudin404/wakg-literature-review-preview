"""Bind solution doses by a separately evidenced concentration, never dry mass."""
import copy
import hashlib
import math

import fitz

from cached_pdf_text import CachedTextDocument
from source_ingredient_inventory import check_quantity, enrich as enrich_ingredient


def enrich(records, pdf, spec, factory):
    work = copy.deepcopy(records)
    assets = [a for a in work['assets'] if a['kind'] == 'pdf' and a['sha256'] == spec['pdf_sha256']]
    if len(assets) != 1:
        raise ValueError('solution source asset ambiguous')
    asset = assets[0]
    links = {e['evidence_key']: e for e in work['evidence_links']}
    assignments = {item['key']: [] for item in spec['items']}
    proofs = {}
    with fitz.open(pdf) as native:
        document = CachedTextDocument(native)
        cache = {}
        for mix in work['mixes']:
            if mix['paper_key'] != asset['paper_key']:
                continue
            parameters = mix['modules']['materials'].get('extensions', {}).get('reported_parameters', [])
            dose = [p for p in parameters if p.get('parameter_key') == spec['dose_parameter']]
            if not dose:
                continue
            if len(dose) != 1 or dose[0].get('unit') != spec['dose_unit']:
                raise ValueError('solution dose identity or unit mismatch')
            if dose[0].get('value') in (None, 0):
                continue
            selected = [(i, p) for i, p in enumerate(parameters) if p.get('parameter_key') == spec['selector_parameter']]
            if len(selected) != 1:
                raise ValueError('solution concentration missing or ambiguous')
            index, parameter = selected[0]
            value = parameter.get('value')
            if type(value) not in (int, float) or not math.isfinite(value) or parameter.get('unit') != spec['selector_unit']:
                raise ValueError('solution concentration type or unit mismatch')
            evidence = links.get(parameter.get('evidence_key'))
            locator = check_quantity(evidence, f'/modules/materials/extensions/reported_parameters/{index}/value',
                                     mix, asset, parameter, document, cache)
            matches = [item for item in spec['items'] if item['selector_value'] == value]
            if len(matches) != 1:
                raise ValueError('solution concentration has no unique reviewed variant')
            assignments[matches[0]['key']].append(mix['mix_key'])
            proofs[mix['mix_key']] = (copy.deepcopy(parameter), copy.deepcopy(evidence), locator)
    created = []
    bound = 0
    for item in spec['items']:
        keys = assignments[item['key']]
        if not keys:
            continue
        subset = copy.deepcopy(work)
        subset['mixes'] = [m for m in subset['mixes'] if m['mix_key'] in keys]
        recipe = {**spec, 'items': [item]}
        outcome = enrich_ingredient(subset, pdf, recipe, factory)
        linked = next(p for p in subset['mixes'][0]['modules']['materials']['extensions']['reported_parameters']
                      if p['parameter_key'] == spec['dose_parameter'])
        mat = next(m for m in subset['mats'] if m['mat_key'] == linked['mat_key'])
        parameter, evidence, locator = proofs[keys[0]]
        path = '/extensions/solution_specification/value'
        evidence_key = 'ev-solution-spec-' + hashlib.sha256(f"{mat['mat_key']}|{evidence['evidence_key']}".encode()).hexdigest()[:24]
        owned = {**evidence, 'evidence_key': evidence_key, 'record_key': mat['mat_key'], 'record_type': 'mat',
                 'field_path': path, 'bbox': locator['bbox']}
        specification = {'parameter_key': spec['selector_parameter'], 'value': parameter['value'],
                         'unit': parameter['unit'], 'basis': 'solution_concentration_not_dry_reagent_mass'}
        provenance = {'evidence_key': evidence_key, 'original_value': parameter['original_value'],
                      'original_unit': parameter['original_unit'], 'formula': None,
                      'extraction_method': 'source-solution-variants-v1', 'confidence': .99, 'review_status': 'pending'}
        prior = mat['extensions'].get('solution_specification')
        if prior is not None and prior != specification:
            raise ValueError('solution MAT specification drift')
        prior_prov = mat['field_provenance'].get(path)
        if prior_prov is not None and prior_prov != provenance:
            raise ValueError('solution MAT provenance drift')
        mat['extensions']['solution_specification'] = specification
        mat['field_provenance'][path] = provenance
        existing = [e for e in subset['evidence_links'] if e['evidence_key'] == evidence_key]
        if existing and existing != [owned]:
            raise ValueError('solution MAT evidence drift')
        if not existing:
            subset['evidence_links'].append(owned)
        for mix in subset['mixes']:
            parameter, evidence, locator = proofs[mix['mix_key']]
            for destination in ('activators',):
                for row in mix['modules']['materials'].get(destination) or []:
                    if row.get('mat_key') == mat['mat_key']:
                        row['extensions']['solution_selector_evidence_key'] = evidence['evidence_key']
                        row['extensions']['solution_selector_locator'] = locator
        replacements = {m['mix_key']: m for m in subset['mixes']}
        work['mixes'] = [replacements.get(m['mix_key'], m) for m in work['mixes']]
        work['mats'] = subset['mats']
        work['evidence_links'] = subset['evidence_links']
        created.extend(outcome['created'])
        bound += outcome['bound_quantities']
    records.clear()
    records.update(work)
    return {'created': created, 'bound_quantities': bound}
