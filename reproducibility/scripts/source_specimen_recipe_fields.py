"""Fill recipe quantities from explicit scoped source assertions, not prose heuristics."""
import copy
import hashlib
import math
from source_specimen_variants import digest, readable_word


def source_quote_content(quote):
    """Permit one balanced citation delimiter, never edit the quoted interior."""
    text = quote.strip()
    if len(text) >= 2 and (text[0], text[-1]) in [('“', '”'), ('"', '"'), ('‘', '’')]:
        text = text[1:-1]
    return ' '.join(text.split())


def attach_quantity_plans(binding, plans):
    """Bind semantic plans to immutable source/assignment content without editing it."""
    ids = [a['source_test_id'] for a in binding['assignments']]
    if len(ids) != len(set(ids)) or set(plans) != set(ids):
        raise ValueError('quantity plan assignment coverage differs')
    result = copy.deepcopy(binding)
    for assignment in result['assignments']:
        plan = copy.deepcopy(plans[assignment['source_test_id']])
        if plan.get('source_binding_sha256') != digest(binding):
            raise ValueError('quantity plan source binding changed')
        plan['assignment_sha256'] = digest(assignment)
        assignment['quantity_plan'] = plan
    return result


def statements(binding, indices):
    source = binding['source_statements'] + binding.get('quantity_source_statements', [])
    if not indices or any(type(i) is not int or not 0 <= i < len(source) for i in indices):
        raise ValueError('quantity scope requires bound source statements')
    return [copy.deepcopy(source[i]) for i in indices]


def populate(records, mix, binding, assignment, specimen, append_new=False):
    """Compute before mutation. A semantic assertion is not independent acceptance."""
    plan = assignment.get('quantity_plan')
    if not plan or plan.get('schema_version') != 'scoped-recipe-quantities-v1':
        raise ValueError('explicit quantity plan required; legacy arrays are insufficient')
    bare = {k: v for k, v in assignment.items() if k != 'quantity_plan'}
    if plan.get('assignment_sha256') != digest(bare):
        raise ValueError('quantity assignment changed')
    original_binding = copy.deepcopy(binding)
    for item in original_binding['assignments']:
        item.pop('quantity_plan', None)
    if plan.get('source_binding_sha256') != digest(original_binding):
        raise ValueError('quantity plan source binding changed')
    assets = [a for a in records['assets'] if a['asset_key'] == binding['pdf_asset_key']]
    if len(assets) != 1 or assets[0].get('paper_key') != mix['paper_key']:
        raise ValueError('quantity source paper differs from target')
    identity = mix['modules']['identity_source_specimen']
    specimen_supported=identity['specimen_type']==specimen or any(s.get('specimen_type')==specimen for s in identity.get('specimens',[]))
    if (specimen not in plan['specimen_types'] or not specimen_supported
            or identity['custom_test_id'] != assignment['source_test_id']):
        raise ValueError('quantity outside target specimen scope')
    scope = statements(binding, plan['scope_statement_indices'])
    quantities = [q for q in plan['quantities'] if specimen in q['specimen_types']]
    keys = [q['parameter_key'] for q in quantities]
    if not keys or not all(isinstance(k, str) and k for k in keys) or len(keys) != len(set(keys)):
        raise ValueError('quantity keys missing or duplicate')
    tokens = [w for s in binding['source_statements'] + binding.get('quantity_source_statements', []) for w in s['locators']]
    values, sources, receipts = {}, {}, {}
    for q in quantities:
        key, mode = q['parameter_key'], q['mode']
        if not isinstance(q.get('label'), str) or not q['label'].strip():
            raise ValueError('quantity source label missing')
        if specimen not in q.get('source_specimen_types', []):
            raise ValueError('quantity original specimen scope does not cover target')
        source_key = q.get('component_evidence_key')
        definition = q.get('component_source_definition')
        if definition:
            series = plan.get('source_series', {})
            context = statements(binding, definition['statement_indices'])
            text = ' '.join(s['statement'] for s in context)
            if (definition['series_id'] != series.get('id')
                    or q['label'] not in [c['label'] for c in series.get('components', [])]
                    or q.get('mass_basis') != series.get('mass_basis')
                    or not source_quote_content(definition['quote']) or source_quote_content(definition['quote']) not in ' '.join(text.split())):
                raise ValueError('component lacks source-defined series relationship')
        if source_key is not None:
            owners = [m for m in records['mixes'] if m['mix_key'] == assignment['mix_key']]
            if (source_key not in assignment['table_evidence_keys'] or len(owners) != 1
                    or owners[0]['paper_key'] != mix['paper_key']):
                raise ValueError('quantity component is not bound to reference source cells')
            parameters = owners[0]['modules']['materials']['extensions']['reported_parameters']
            matches = [p for p in parameters if p.get('evidence_key') == source_key]
            fold = lambda value: ''.join(value.split()).casefold()
            if len(matches) != 1 or fold(matches[0]['original_label']) != fold(q['label']):
                raise ValueError('quantity component label differs from bound source cell')
        if mode == 'remainder':
            continue
        if mode in ('missing', 'unknown'):
            if q.get('value') is not None or not q.get('reason'):
                raise ValueError('missing quantity must remain null with reason')
            values[key], sources[key] = None, []
            continue
        if mode != 'direct' or type(q.get('value')) not in (int, float) or not math.isfinite(q['value']):
            raise ValueError('direct quantity must be finite numeric value')
        if not q.get('unit') or q.get('source_unit') != q['unit'] or q['source_token'] not in tokens:
            raise ValueError('direct quantity lacks unit or bound source token')
        word = readable_word(records, binding, q['source_token'])
        try:
            literal = float(word['token'].strip(' ,;()').rstrip('.%'))
        except ValueError as error:
            raise ValueError('direct source token is not numeric') from error
        if literal != q['value']:
            raise ValueError('direct value contradicts source token')
        values[key], sources[key] = q['value'], [word]
    remainder = [q for q in quantities if q['mode'] == 'remainder']
    basis = plan.get('composition_basis')
    if remainder:
        if len(remainder) != 1 or not basis or basis.get('complete') is not True:
            raise ValueError('remainder requires explicit complete composition scope')
        if (remainder[0].get('direct_value_status') != 'absent_in_scoped_source'
                or remainder[0].get('value') is not None):
            raise ValueError('remainder requires explicit absence of a scoped direct observation')
        members = basis['component_keys']; target = remainder[0]['parameter_key']
        if (len(members) < 2 or len(set(members)) != len(members) or target not in members
                or not set(members) <= set(keys) or not plan.get('mass_basis')
                or basis.get('mass_basis') != plan['mass_basis'] or basis.get('unit') != 'wt.%'
                or type(basis.get('total_percent')) not in (int, float) or basis['total_percent'] != 100):
            raise ValueError('remainder mass basis or component membership unresolved')
        basis_evidence = statements(binding, basis['statement_indices'])
        inputs = []
        for member in members:
            q = next(q for q in quantities if q['parameter_key'] == member)
            if q.get('unit') != 'wt.%':
                raise ValueError('remainder requires common percentage units')
            if q.get('mass_basis', plan['mass_basis']) != plan['mass_basis']:
                raise ValueError('remainder component mass bases differ')
            if member != target:
                if q['mode'] != 'direct' or not 0 <= values[member] <= 100:
                    raise ValueError('remainder input must be observed percentage')
                inputs.append({'key': member, 'value': values[member]})
        value = 100 - math.fsum(i['value'] for i in inputs)
        if not 0 <= value <= 100:
            raise ValueError('remainder outside physical range')
        values[target] = value
        sources[target] = [w for i in inputs for w in sources[i['key']]]
        receipts[target] = {'kind': 'mass_balance_complement_percent', 'formal': False,
            'formula': 'target_percent = total_percent - sum(observed_component_percent)',
            'inputs': {'components': inputs, 'total_percent': 100, 'mass_basis': plan['mass_basis']},
            'output': {'value': value, 'unit': 'wt.%'},
            'forward_check': {'computed': value, 'recorded': value, 'tolerance': 0},
            'reverse_check': {'computed': math.fsum([value] + [i['value'] for i in inputs]),
                              'recorded': 100, 'tolerance': 1e-12},
            'source_basis': copy.deepcopy(basis), 'basis_evidence': basis_evidence}
    params = mix['modules']['materials']['extensions']['reported_parameters']
    if params and not append_new:
        raise ValueError('quantity target not empty; overwriting is forbidden')
    if set(keys)&{p['parameter_key'] for p in params}:
        raise ValueError('quantity append would overwrite existing key')
    offset=len(params)
    new_params, links, provenance = [], [], {}
    for q in quantities:
        key = q['parameter_key']; proof = sources[key]; receipt = receipts.get(key)
        path = f'/modules/materials/extensions/reported_parameters/{offset+len(new_params)}/value'
        evidence_key = None
        if proof:
            word = proof[0]; evidence_key = 'ev-recipe-' + digest([mix['mix_key'], path, proof])[:24]
            links.append({'evidence_key': evidence_key, 'record_type': 'mix', 'record_key': mix['mix_key'],
                'paper_key': mix['paper_key'], 'asset_key': binding['pdf_asset_key'], 'field_path': path,
                'page': word['page'], 'bbox': word['bbox'], 'snippet': word['token'],
                'snippet_sha256': hashlib.sha256(word['token'].encode()).hexdigest(),
                'section': binding.get('source_section'), 'table_number': None, 'figure_number': None,
                'source_locator': {'coordinate_space': 'pdf_points'}, 'confidence': .95,
                'extraction_method': 'scoped-recipe-quantity-v2',
                'extensions': {'support_tokens': proof, 'scope_evidence': statements(binding, q['scope_statement_indices']) if q.get('scope_statement_indices') else scope,
                               'composition_basis': copy.deepcopy(basis) if receipt else None}})
        original = proof[0]['token'] if q['mode'] == 'direct' else None
        new_params.append({'parameter_key': key, 'original_label': q['label'], 'value': values[key],
            'unit': q.get('unit'), 'original_value': original, 'original_unit': q.get('unit'),
            'status': 'derived' if receipt else ('reported' if proof else q['mode']), 'reason': q.get('reason'),
            'semantic_role': 'reported_formulation', 'transformation': copy.deepcopy(receipt),
            'extensions': {'mass_basis': q.get('mass_basis', plan.get('mass_basis')),
                           'component_source_definition': copy.deepcopy(q.get('component_source_definition'))},
            'evidence_key': evidence_key, 'extraction_method': 'scoped-recipe-quantity-v2',
            'confidence': .95 if proof else None})
        provenance[path] = {'evidence_key': evidence_key, 'original_value': original,
            'original_unit': q.get('unit'), 'formula': receipt['formula'] if receipt else None,
            'transformation': copy.deepcopy(receipt), 'extraction_method': 'scoped-recipe-quantity-v2',
            'review_status': 'pending_source_path_review' if proof else 'source_unknown'}
    params.extend(new_params); records['evidence_links'].extend(links); mix['field_provenance'].update(provenance)
    if not append_new:mix['modules']['materials']['reported_mass_basis'] = plan.get('mass_basis')
    mix['extensions'].setdefault('recipe_source_binding',{})['review_status'] = 'partial_recipe_fields_pending_source_review'
