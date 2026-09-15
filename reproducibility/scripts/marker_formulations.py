"""Source-series/axis bindings with separate reference and specimen identities.

Semantic plans establish relationships; table composition never authorizes
specimen observation merging. Unresolved source plans remain recoverable.
"""
import hashlib
import json
import re
from pathlib import Path

import fitz
from source_mass_precision import composition_bounds


def _text(value):
    return re.sub(r'\s+', ' ', value.replace('\u00ad', '')).strip()


def _one(pattern, text):
    matches = list(re.finditer(pattern, text, flags=re.I))
    if len(matches) != 1:
        raise ValueError('formulation source statement missing or ambiguous')
    return matches[0]


def _numbers(value):
    return [float(x) for x in re.findall(r'\d+(?:\.\d+)?', value)]


def material_columns(headers, component_labels):
    """Bind semantically declared components to unique source headers in order.

    This is exact label matching, not an alias resolver or a material classifier.
    The upstream source planner remains responsible for the component scope.
    """
    def key(value):
        if not isinstance(value, str):
            raise ValueError('material header or component label is not text')
        return ''.join(_text(value).split()).casefold()
    labels = [key(label) for label in component_labels]
    if not labels or not all(labels) or len(labels) != len(set(labels)):
        raise ValueError('component labels missing or duplicate')
    source = [key(header) for header in headers]
    columns = []
    for label in labels:
        matches = [column for column, header in enumerate(source) if header == label]
        if len(matches) != 1:
            raise ValueError('formulation material header missing or ambiguous')
        columns.append(matches[0])
    return columns


def source_columns(table, row_number, logical_columns):
    """Translate grid columns back to original cells without assuming equal widths."""
    grid = table.get('layout_grid')
    row = table['rows'][row_number]
    if grid is None:
        if any(column >= len(row) for column in logical_columns):
            raise ValueError('formulation source component cell missing')
        return logical_columns
    cells = grid['rows'][row_number]
    result = []
    for column in logical_columns:
        matches = [cell for cell in cells if cell['column'] == column]
        if len(matches) != 1:
            raise ValueError('formulation grid column missing or ambiguous')
        cell = matches[0]
        raw_index = cell['raw_cell']
        if (type(raw_index) is not int or not 0 <= raw_index < len(row)
                or cell.get('vertical_inheritance') or cell.get('horizontal_span') != 1
                or cell['raw_text'] != row[raw_index]
                or _text(cell['text']) != _text(row[raw_index])):
            raise ValueError('formulation grid/source cell mismatch or unresolved merged component')
        result.append(raw_index)
    if len(set(result)) != len(result):
        raise ValueError('multiple components map to same source cell')
    return result


def caption_scope(caption):
    specimens = set(re.findall(r'\b(mortar|paste|concrete)\b', caption.lower()))
    if specimens == {'mortar', 'paste'}:
        return 'panel_specific'
    if specimens in ({'mortar'}, {'concrete'}):
        return next(iter(specimens))
    raise ValueError('formulation caption specimen scope unresolved')


class FormulationPending(ValueError):
    """Recoverable source-planning gap; never an authorization to merge observations."""
    def __init__(self, reason, candidate):
        super().__init__(reason)
        self.candidate = candidate


def _groups(plan, figure):
    if not isinstance(plan, dict):
        raise FormulationPending('legacy prose mapping requires a source plan', {'figure':figure})
    matches = [g for g in plan.get('figures', []) if g['label'] == figure]
    if len(matches) != 1 or matches[0]['status'] not in ('mapped','partial'):
        raise FormulationPending('figure series mapping unresolved', {'figure': figure, 'plan': plan})
    return matches[0]


def resolve_formulations(records, table, source_caption, digitization, *, source_plan=None):
    """Bind source IDs by explicit series/axis evidence, never by equal composition."""
    from source_specimen_variants import digest, readable_word
    from source_specimen_recipe_fields import source_quote_content
    pdfs = [a for a in records['assets'] if a['kind'] == 'pdf']
    if len(pdfs) != 1:
        raise ValueError('formulation requires unique source PDF')
    pdf = pdfs[0]
    if hashlib.sha256(Path(pdf['relative_path']).read_bytes()).hexdigest() != pdf['sha256']:
        raise ValueError('formulation main PDF hash mismatch')
    candidate = {'figure': source_caption['label'], 'pdf_sha256': pdf['sha256'],
                 'table_sha256': table['rows_sha256'], 'status': 'SOURCE_PLAN_REQUIRED'}
    if source_plan is None:
        raise FormulationPending('semantic source series plan required', candidate)
    if source_plan.get('schema_version') != 3:
        raise FormulationPending('source plan requires indexed-source protocol', candidate)
    if source_plan.get('pdf_sha256') != pdf['sha256'] or source_plan.get('table_sha256') != table['rows_sha256']:
        raise ValueError('formulation semantic plan source changed')
    if digest(table['rows']) != table['rows_sha256']:
        raise ValueError('formulation source table changed')
    group = _groups(source_plan, source_caption['label'])
    if group['caption'] != source_caption['caption']:
        raise ValueError('formulation figure context changed')
    statements = source_plan['source_statements']
    from formulation_source_plan import index_spans
    source_index={s['span_id']:s for s in index_spans(statements,pdf['sha256'])}
    def proof(spans):
        if not spans:
            raise ValueError('formulation relationship source missing')
        indices = []
        for span in spans:
            index = span['statement_index']
            if type(index) is not int or not 0 <= index < len(statements):
                raise ValueError('formulation span source index invalid')
            if source_index.get(span.get('span_id')) != span:
                raise ValueError('formulation span source location changed')
            if index not in indices: indices.append(index)
        return [statements[i] for i in indices]
    # Figure sampling may be a subset of the declared series; do not infer missing experiments.
    figure_xs = {float(p['x']) for panel in digitization['panels'] for s in panel['series'] for p in s['points']}
    mappings = group['assignments']
    declared = [a['x'] for a in mappings]
    if len(set(declared)) != len(declared) or not figure_xs <= set(declared):
        raise FormulationPending('axis value has no unique source identity', candidate)
    results = []; anchors = []; pending = []
    for assignment in mappings:
        if assignment['x'] not in figure_xs:
            continue
        if not assignment['specimens'] or not set(assignment['specimens']) <= {'paste','mortar','concrete','powder'}:
            raise FormulationPending('source specimen vocabulary unresolved', candidate)
        sources = proof(assignment['source_spans'])
        anchors.extend(s for s in sources if s not in anchors)
        for token in assignment['axis_tokens']:
            if not any(token in s['locators'] for s in sources):
                raise ValueError('axis token not in source relationship')
            readable_word(records, {'pdf_asset_key': pdf['asset_key'], 'pdf_sha256': pdf['sha256']}, token)
        if not any(float(w['token'].strip(' ,;()%').rstrip('.')) == assignment['x']
                   for w in assignment['axis_tokens']):
            raise ValueError('axis value differs from source')
        source_id = assignment['source_test_id']
        reference_id = assignment['reference_test_id']
        variants = list(records.get('extensions', {}).get('specimen_variants', {}).get('assignments', []))
        variants += records.get('extensions', {}).get('source_context_owners', [])
        live_keys = {m['mix_key'] for m in records['mixes'] if m['paper_key'] == pdf['paper_key']
                     and m['modules']['identity_source_specimen']['custom_test_id'] == source_id
                     and m['modules']['identity_source_specimen'].get('specimen_type') in assignment['specimens']}
        targets = {v['mix_key'] for v in variants if v['custom_test_id'] == source_id
                   and v['specimen'] in assignment['specimens'] and v['mix_key'] in live_keys}
        observation_key = next(iter(targets)) if len(targets) == 1 and len(assignment['specimens']) == 1 else None
        if not reference_id:
            if observation_key:
                results.append({'x':assignment['x'],'source_test_id':source_id,'canonical_test_id':source_id,
                    'mix_key':observation_key,'reference_mix_key':None,'observation_mix_key':observation_key,
                    'observation_transfer_authorized':True,'specimen':assignment['specimens'][0],
                    'specimen_types':assignment['specimens'],'precursor_mass_percent':None,
                    'source_table_row':None,'source_identity_column':None,'table_evidence_keys':[],
                    'reference_relation':'unknown','reference_pending':True})
                continue
            pending.append({'source_test_id':source_id,'x':assignment['x'],'reason':'reference identity unresolved',
                            'source_spans':assignment['source_spans']})
            continue
        if source_id != reference_id and assignment['reference_relation'] != 'formulation_only':
            raise FormulationPending('reference identity relation unresolved', candidate)
        # Find the exact identity cell anywhere in the source grid, not row[0] or rows[2:].
        cells = [(ri, ci) for ri,row in enumerate(table['rows']) for ci,value in enumerate(row)
                 if _text(value) == reference_id]
        selected_row=assignment.get('reference_row')
        if selected_row is not None:
            cells=[cell for cell in cells if cell==(selected_row['row'],selected_row['identity_column'])]
        owners = []
        evidence = {e['evidence_key']: e for e in records['evidence_links']}
        for mix in records['mixes']:
            if (mix['paper_key'] != pdf['paper_key'] or mix['modules']['identity_source_specimen']['custom_test_id'] != reference_id
                    or mix.get('extensions', {}).get('source_table') != table['label']):
                continue
            params = mix['modules']['materials'].get('extensions', {}).get('reported_parameters', [])
            links = [evidence.get(p.get('evidence_key'), {}) for p in params]
            rows = {e.get('source_locator', {}).get('row') for e in links
                    if e.get('table_number') == table['label'] and e.get('record_key') == mix['mix_key']}
            hits = [(ri,ci) for ri,ci in cells if ri in rows]
            if len(hits) == 1:
                owners.append((mix, hits[0], params))
        if not owners:
            pending.append({'source_test_id':source_id,'x':assignment['x'],'reason':'reference row not found',
                            'source_spans':assignment['source_spans']})
            continue
        if len(owners) != 1:
            raise FormulationPending('reference row identity ambiguous', candidate)
        owner, (row_number, identity_column), params = owners[0]
        # Reuse the established grid/header component binding, only for reference metadata.
        header_matches = []
        for ri,row in enumerate(table['rows']):
            grid = table.get('layout_grid')
            headers = [c['text'] for c in grid['rows'][ri]] if grid else row
            try:
                cols = material_columns(headers, source_plan['reference_component_labels'])
                source_columns(table, ri, cols)
                header_matches.append(cols)
            except ValueError:
                continue
        if len(header_matches) != 1:
            raise FormulationPending('reference component headers ambiguous', candidate)
        raw_columns = source_columns(table, row_number, header_matches[0])
        keys = []
        for col in raw_columns:
            matches = [p for p in params if (evidence.get(p.get('evidence_key'), {}).get('source_locator') or {}).get('row') == row_number
                       and (evidence.get(p.get('evidence_key'), {}).get('source_locator') or {}).get('column') == col]
            if len(matches) != 1:
                raise ValueError('reference component cell evidence ambiguous')
            link = evidence[matches[0]['evidence_key']]
            if link.get('snippet') != table['rows'][row_number][col]:
                raise ValueError('reference component source cell changed')
            keys.append(matches[0]['evidence_key'])
        # Only an exact source ID AND specimen-specific variant can own observations.
        results.append({'x': assignment['x'], 'source_test_id': source_id, 'canonical_test_id': source_id,
            'mix_key': observation_key or owner['mix_key'], 'reference_mix_key': owner['mix_key'],
            'observation_mix_key': observation_key, 'observation_transfer_authorized': observation_key is not None,
            'specimen': assignment['specimens'][0] if len(assignment['specimens']) == 1 else 'panel_specific',
            'specimen_types': assignment['specimens'], 'precursor_mass_percent': None,
            'source_table_row': row_number, 'source_identity_column': identity_column,
            'table_evidence_keys': keys, 'reference_relation': assignment['reference_relation']})
    if not anchors:
        raise FormulationPending('no sampled source assignments', candidate)
    return {'schema_version': 2, 'method': 'semantic-series-axis-reference-v1',
        'scope': 'precursor_formulation_only_not_aggregate_recipe', 'figure': source_caption['label'],
        'review_status': 'pending_source_path_review', 'pdf_asset_key': pdf['asset_key'], 'pdf_sha256': pdf['sha256'],
        'source_statements': anchors, 'source_table_sha256': table['rows_sha256'], 'assignments': results,
        'source_plan': source_plan, 'declared_levels': sorted(declared), 'sampled_levels': sorted(figure_xs),
        'pending_assignments': pending}
