"""Shared, model-free record preparation for native and figure workflows."""
import copy


def normalize(records):
    for mat in records.get('mats', []):
        composition = mat.get('xrf_composition') or {}
        rows = composition.get('rows', [])
        loi = mat.get('extensions', {}).get('reported_properties', {}).get('loss_on_ignition_percent')
        provenance = mat.get('field_provenance', {})
        if rows and loi is not None and not any(r['component'].lower() in {'loi', 'loss on ignition'} for r in rows):
            rows.append(dict(component='LOI', original_value=loi, normalized_value=loi,
                             status='pending', reason=None, extensions={}))
            receipt = provenance.get('/extensions/reported_properties/loss_on_ignition_percent')
            if receipt:
                for key in ('original_value', 'normalized_value'):
                    provenance[f'/xrf_composition/rows/{len(rows)-1}/{key}'] = copy.deepcopy(receipt)
        if rows and all(isinstance(r.get('original_value'), (int, float)) for r in rows):
            total = sum(r['original_value'] for r in rows)
            composition['original_total'] = round(total, 8)
            if total > 100.000001:
                composition['normalisation_candidate'] = dict(status='pending',
                    formula='original_value / original_total * 100',
                    rows=[dict(component=r['component'], value=r['original_value']/total*100) for r in rows])
    evidence = {e['evidence_key']: e for e in records.get('evidence_links', [])}
    for record in records.get('mats', []) + records.get('mixes', []):
        for receipt in record.get('field_provenance', {}).values():
            if receipt.get('original_value') is None and str(evidence.get(receipt.get('evidence_key'), {}).get('snippet', '')).strip() == '/':
                receipt['original_value'] = '/'
        parameters = record.get('modules', {}).get('materials', {}).get('extensions', {}).get('reported_parameters', [])
        for parameter in parameters:
            if parameter.get('value') is None and str(evidence.get(parameter.get('evidence_key'), {}).get('snippet', '')).strip() == '/':
                parameter.update(original_value='/', reason='source_dash', status='not_reported')
    return records
