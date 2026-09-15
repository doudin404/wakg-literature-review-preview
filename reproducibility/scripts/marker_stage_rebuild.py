"""Explicit immutable replacement boundary for a module-owned figure contribution."""
import copy
from source_specimen_variants import digest


def rebuild(records,plan,label,asset_key,chart,mapping,*,source_caption,docx_asset_key,formulation_binding):
    from supplement_docx import _add_xy_performance,_marker_outputs
    previous=records.get('extensions',{}).get('supplement_marker_transactions',{}).get('figures',{}).get(label)
    if not previous or previous['formulation_binding']==formulation_binding:
        return _add_xy_performance(records,plan,label,asset_key,chart,mapping,
            source_caption=source_caption,docx_asset_key=docx_asset_key,formulation_binding=formulation_binding)
    outputs=_marker_outputs(records,previous['members'])
    if digest(outputs)!=previous['output_sha256']:raise ValueError('old marker output integrity failed')
    old=previous['formulation_binding']
    if any(old.get(k)!=formulation_binding.get(k) for k in ('figure','pdf_sha256','source_table_sha256')):
        raise ValueError('replacement is not the same source object')
    links=[e for o in outputs for e in o['evidence']]
    keys={e['evidence_key'] for e in links}
    if not keys or len(keys)!=len(links) or any(e.get('figure_number')!=label or e['asset_key'] not in (asset_key,docx_asset_key) for e in links):
        raise ValueError('old contribution has foreign or duplicate evidence')
    staged=copy.deepcopy(records);staged_plan=copy.deepcopy(plan)
    owned={}
    for member in previous['members']:owned.setdefault(member['mix_key'],[]).append(member['index'])
    for mix in staged['mixes']:
        indices=sorted(owned.get(mix['mix_key'],[]))
        if indices:
            # Only suffixes can be removed without shifting unrelated field paths.
            if indices!=list(range(indices[0],len(mix['modules']['performance']))):
                raise ValueError('owned contribution is not a suffix; explicit index migration needed')
            mix['modules']['performance']=mix['modules']['performance'][:indices[0]]
            prefixes=tuple('/modules/performance/'+str(i)+'/' for i in indices)
            mix['field_provenance']={p:v for p,v in mix['field_provenance'].items() if not p.startswith(prefixes)}
        if any(p.get('evidence_key') in keys for p in mix.get('field_provenance',{}).values()):
            raise ValueError('old evidence is shared by an unaffected field')
    staged['evidence_links']=[e for e in staged['evidence_links'] if e['evidence_key'] not in keys]
    registry=staged['extensions']['supplement_marker_transactions']['figures']
    del registry[label]
    archive={'figure':label,'previous_transaction':copy.deepcopy(previous),'previous_outputs':outputs,
             'input_records_sha256':digest(records),'source_asset_keys':[asset_key,docx_asset_key]}
    _add_xy_performance(staged,staged_plan,label,asset_key,chart,mapping,
        source_caption=source_caption,docx_asset_key=docx_asset_key,formulation_binding=formulation_binding)
    archive['replacement_transaction_sha256']=digest(staged['extensions']['supplement_marker_transactions']['figures'][label])
    staged['extensions'].setdefault('marker_stage_history',[]).append(archive)
    records.clear();records.update(staged);plan.clear();plan.update(staged_plan)
