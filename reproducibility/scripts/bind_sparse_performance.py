"""Bind source-replayed sparse candidates to methods and MIXs before promotion."""
import json
import re
from pathlib import Path
import fitz
from sparse_vector_observations import extract
from source_performance_candidates import locate
from vector_curves import coordinate,digest,atomic_bytes
import argparse


def source_part(doc,recipe):
    page=doc[recipe['page']-1]
    clause,raw,words=locate(page.get_text('words'),recipe['clause'],recipe['capture'],True)
    box=fitz.Rect(words[0][:4])
    for word in words[1:]:box|=fitz.Rect(word[:4])
    actual=page.get_textbox(box).strip()
    if ' '.join(raw.split()) not in ' '.join(actual.split()):raise ValueError('sparse method source readback failed')
    return {'page':recipe['page'],'bbox':list(box),'raw':raw,'snippet':actual,'source_clause':clause}


def bind(records,pdf,geometry_spec,candidates,binding):
    if extract(pdf,geometry_spec)!=candidates:raise ValueError('sparse binding source geometry drift')
    assets=[a for a in records['assets'] if a['kind']=='pdf' and a['sha256']==geometry_spec['pdf_sha256']]
    if len(assets)!=1 or binding['pdf_sha256']!=geometry_spec['pdf_sha256']:raise ValueError('sparse binding paper mismatch')
    asset=assets[0]
    with fitz.open(pdf) as doc:
        context={key:source_part(doc,binding[key]) for key in ('ages','specimen','method')}
        ages=[float(x) for x in re.findall(r'\d+(?:\.\d+)?',context['ages']['raw'])]
        if len(ages)!=len(set(ages)) or len(ages)!=geometry_spec['point_count']:raise ValueError('sparse age source set invalid')
        items=[]
        for candidate in candidates['observations']:
            definitions=[x for x in binding['owners'] if x['source_id']==candidate['source_label']]
            if len(definitions)!=1:raise ValueError('sparse source series identity missing')
            definition=definitions[0]
            owners=[m for m in records['mixes'] if m['paper_key']==asset['paper_key']
                    and m['modules']['identity_source_specimen']['custom_test_id']==definition['source_id']
                    and m['extensions'].get('source_table')==definition['source_table']
                    and m['extensions'].get('source_row')==definition['source_row']]
            if len(owners)!=1:raise ValueError('sparse exact concrete MIX binding missing')
            mix=owners[0]
            identity=source_part(doc,{'page':3,'clause':definition['identity_clause'],'capture':'('+re.escape(definition['source_id'])+')'})
            matches=[age for age in ages if abs(age-candidate['age_from_axis'])<=binding['age_tolerance_days']]
            if len(matches)!=1:raise ValueError('sparse age cannot map uniquely to tested age')
            age=matches[0];seconds=age*86400
            drawing=doc[geometry_spec['page']-1].get_drawings()[candidate['source_path_index']]
            pixel_error=max(.3,float(drawing['width'])/2)+candidate['marker_center_error_pt']
            y=candidate['pdf_point'][1]
            interval=sorted(coordinate(v,geometry_spec['y_axis']) for v in (y-pixel_error,y+pixel_error))
            existing=[(i,o) for i,o in enumerate(mix['modules']['performance']) if o['name']==binding['property'] and o.get('age_seconds')==seconds]
            if len(existing)>1:raise ValueError('sparse duplicate existing property/age')
            action='ADD_OBSERVATION';existing_index=None;formal_value=candidate['value_from_axis']
            if existing:
                existing_index,observation=existing[0]
                path=f'/modules/performance/{existing_index}/value';prov=mix.get('field_provenance',{}).get(path,{})
                evidence=next((e for e in records['evidence_links'] if e['evidence_key']==prov.get('evidence_key')),None)
                if (not evidence or evidence['record_key']!=mix['mix_key'] or evidence['asset_key']!=asset['asset_key']
                        or evidence['field_path']!=path or not observation.get('extensions',{}).get('prose_binding_id')
                        or observation['unit']!=candidate['unit'] or observation['specimen']!=context['specimen']['raw']
                        or observation['method']!=context['method']['raw']):raise ValueError('sparse existing observation context not equivalent')
                actual=doc[evidence['page']-1].get_textbox(fitz.Rect(evidence['bbox']))
                if str(prov['original_value']) not in actual or float(prov['original_value'])!=observation['value']:
                    raise ValueError('sparse direct-value evidence readback failed')
                if not interval[0]<=observation['value']<=interval[1]:raise ValueError('sparse/prose values conflict beyond digitization bound')
                action='SUPPORT_DIRECT_OBSERVATION';formal_value=observation['value']
            items.append({'mix_key':mix['mix_key'],'source_id':definition['source_id'],'name':binding['property'],
                'action':action,'existing_observation_index':existing_index,'preferred_value':formal_value,'unit':candidate['unit'],
                'age_seconds':seconds,'age_days':age,'age_axis_value':candidate['age_from_axis'],
                'age_tolerance_days':binding['age_tolerance_days'],'specimen':context['specimen']['raw'],'method':context['method']['raw'],
                'identity_evidence':identity,'digitization_interval':interval,'digitization_error_pdf_pt':pixel_error,
                'digitization_error_basis':'half source stroke width (minimum0.3pt) plus marker-center discrepancy; not experimental SD/SE',
                'candidate':candidate})
    return {'status':'BOUND_PLAN_REQUIRES_PROMOTION_ACCEPTANCE','pdf_sha256':asset['sha256'],
            'source_asset_key':asset['asset_key'],'geometry_specification_sha256':digest(geometry_spec),
            'binding_sha256':digest(binding),'records_sha256':digest(records),'candidates_sha256':digest(candidates),
            'context_evidence':context,'items':items,'formal_observations_added':0}


if __name__=='__main__':
    p=argparse.ArgumentParser()
    for key in ('records','pdf','geometry-spec','candidates','binding','output'):p.add_argument('--'+key,type=Path,required=True)
    a=p.parse_args();load=lambda path:json.loads(path.read_text(encoding='utf-8'))
    result=bind(load(a.records),a.pdf,load(a.geometry_spec),load(a.candidates),load(a.binding))
    data=json.dumps(result,ensure_ascii=False,sort_keys=True,indent=2).encode()
    if a.output.exists() and a.output.read_bytes()!=data:raise ValueError('immutable binding plan conflict')
    atomic_bytes(a.output,data)
    print(json.dumps({'items':len(result['items']),'add':sum(x['action']=='ADD_OBSERVATION' for x in result['items']),
                      'support':sum(x['action']=='SUPPORT_DIRECT_OBSERVATION' for x in result['items'])}))
