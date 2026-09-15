"""Atomic sparse observation projection; independent input-bound reviews required."""
import copy
import hashlib
import fitz
from pathlib import Path
from vector_curves import digest,coordinate
from bind_sparse_performance import bind
from render_sparse_vector_overlay import render as verify_overlay_render
from source_performance_candidates import locate
import re

VERSION='reviewed-sparse-performance-v1'


def tight_method_parts(pdf,part):
    with fitz.open(pdf) as doc:
        page=doc[part['page']-1]
        _,raw,words=locate(page.get_text('words'),re.escape(part['source_clause']),
                           '('+re.escape(part['raw'])+')',True)
        groups=[]
        for word in words:
            if not groups or groups[-1][0][5:7]!=word[5:7]:groups.append([])
            groups[-1].append(word)
        parts=[]
        for group in groups:
            box=fitz.Rect(group[0][:4])
            for word in group[1:]:box|=fitz.Rect(word[:4])
            expected=' '.join(w[4] for w in group)
            for inset in (.3,.2,.1,0):
                narrow=fitz.Rect(box.x0,box.y0+box.height*inset,box.x1,box.y1-box.height*inset)
                snippet=page.get_textbox(narrow).strip()
                if ' '.join(snippet.split())==expected:break
            else:raise ValueError('sparse method line readback failed')
            parts.append({'page':part['page'],'bbox':list(narrow),'snippet':snippet})
        if ' '.join(' '.join(p['snippet'] for p in parts).split())!=' '.join(raw.split()):
            raise ValueError('sparse method split changed source text')
        return parts


def subject(records):
    value=copy.deepcopy(records)
    value.get('extensions',{}).pop('sparse_performance_promotion',None)
    return digest(value)


def promote(records,pdf,geometry,candidates,binding,plan,acceptance):
    identities={'plan_sha256':digest(plan),'binding_sha256':digest(binding),
                'geometry_sha256':digest(geometry),'candidates_sha256':digest(candidates),
                'pdf_sha256':hashlib.sha256(Path(pdf).read_bytes()).hexdigest()}
    if acceptance.get('verdict')!='PASS' or not acceptance.get('message_id') or acceptance.get('defects'):
        raise ValueError('sparse promotion review has not passed')
    if any(acceptance.get(k)!=v for k,v in identities.items()) or not acceptance.get('overlay_sha256'):
        raise ValueError('sparse promotion review input mismatch')
    overlay=Path(acceptance.get('overlay_path') or '')
    if not overlay.is_file() or hashlib.sha256(overlay.read_bytes()).hexdigest()!=acceptance['overlay_sha256']:
        raise ValueError('sparse accepted overlay file missing or changed')
    if verify_overlay_render(pdf,geometry,candidates)['sha256']!=acceptance['overlay_sha256']:
        raise ValueError('sparse overlay differs from current source reconstruction')
    prior=records.get('extensions',{}).get('sparse_performance_promotion')
    if prior:
        if prior.get('identities')!=identities or prior.get('acceptance_sha256')!=digest(acceptance) or prior.get('output_subject_sha256')!=subject(records):
            raise ValueError('sparse promoted records or review drift')
        return {'added':0,'supported':0,'replay':True}
    if bind(records,pdf,geometry,candidates,binding)!=plan:raise ValueError('sparse promotion plan no longer matches source')
    result=copy.deepcopy(records);mixes={m['mix_key']:m for m in result['mixes']}
    method_parts=tight_method_parts(pdf,plan['context_evidence']['method'])
    added=supported=0
    def evidence(mix,path,part,original,unit,formula=None,extra=None):
        key='ev-sparse-'+digest([identities,path,mix['mix_key'],part])[:24]
        snippet=part.get('snippet') or f"{geometry['figure']}: {mix['modules']['identity_source_specimen']['custom_test_id']}"
        link={'evidence_key':key,'asset_key':plan['source_asset_key'],'paper_key':mix['paper_key'],
              'record_key':mix['mix_key'],'record_type':'mix','field_path':path,'page':part['page'],
              'bbox':part['bbox'],'snippet':snippet,'snippet_sha256':hashlib.sha256(snippet.encode()).hexdigest(),
              'section':None,'table_number':None,'figure_number':part.get('figure_number'),
              'extraction_method':VERSION,'confidence':.98,'extensions':extra or {}}
        if any(e['evidence_key']==key for e in result['evidence_links']):raise ValueError('sparse evidence key collision')
        result['evidence_links'].append(link)
        prov={'evidence_key':key,'original_value':original,'original_unit':unit,'formula':formula,
              'extraction_method':VERSION,'confidence':.98,'review_status':'pending'}
        if path in mix.setdefault('field_provenance',{}):raise ValueError('sparse would overwrite field provenance')
        mix['field_provenance'][path]=prov
        return key
    for item in plan['items']:
        mix=mixes[item['mix_key']];rows=mix['modules']['performance'];candidate=item['candidate']
        if item['action']=='SUPPORT_DIRECT_OBSERVATION':
            row=rows[item['existing_observation_index']]
            # Source binding has already checked the direct value and context.
            support={'candidate':candidate,'digitization_interval':item['digitization_interval'],
                     'preferred_source':'direct_prose','plan_sha256':identities['plan_sha256']}
            row.setdefault('extensions',{})['sparse_support']=support;supported+=1
            continue
        if item['action']!='ADD_OBSERVATION':raise ValueError('unknown sparse promotion action')
        index=len(rows);base=f'/modules/performance/{index}'
        if any(o['name']==item['name'] and o.get('age_seconds')==item['age_seconds'] for o in rows):
            raise ValueError('sparse duplicate performance observation')
        row={'name':item['name'],'value':candidate['value_from_axis'],'unit':item['unit'],
             'age_seconds':item['age_seconds'],'specimen':item['specimen'],'method':item['method'],
             'extensions':{'sparse_binding_id':digest([identities,item['source_id'],item['age_seconds']]),
                           'extraction_method':VERSION,'original_value':candidate['pdf_point'][1],'original_unit':'PDF pt',
                           'digitized_value':candidate['value_from_axis'],'digitized_unit':item['unit'],
                           'source_kind':'figure_digitized','digitization_interval':item['digitization_interval'],
                           'digitization_error_basis':item['digitization_error_basis'],
                           'axis_anchor_uncertainty':None,'experimental_error_semantics':None,
                           'source_geometry':candidate,'display_decimal_places':1}}
        point=candidate['pdf_point'];box=[point[0]-2.8,point[1]-2.8,point[0]+2.8,point[1]+2.8]
        reverse=[coordinate(candidate['age_from_axis'],geometry['x_axis'],True),coordinate(candidate['value_from_axis'],geometry['y_axis'],True)]
        if max(abs(a-b) for a,b in zip(reverse,point))>1e-7:raise ValueError('sparse coordinate reverse check failed')
        key=evidence(mix,base+'/value',{'page':geometry['page'],'bbox':box,'figure_number':geometry['figure']},
                     point[1],'PDF pt','value = calibrated_y_axis(y_pdf)',
                     {'evidence_kind':'reviewed_sparse_geometry','candidate':candidate,'axes':{'x':geometry['x_axis'],'y':geometry['y_axis']},
                      'reverse_pdf_point':reverse,'plan_sha256':identities['plan_sha256'],'acceptance_sha256':digest(acceptance)})
        row['extensions']['evidence_key']=key
        mix['field_provenance'][base+'/value']['transformation']={
            'kind':'linear_axis_coordinate','formal':True,'formula':'value = v0 + (y_pdf-p0)/(p1-p0)*(v1-v0)',
            'inputs':{'value':point[1],'unit':'PDF pt','anchors':geometry['y_axis']['anchors']},
            'output':{'value':row['value'],'unit':row['unit']},
            'forward_check':{'computed':coordinate(point[1],geometry['y_axis']),'recorded':row['value'],'tolerance':1e-9},
            'reverse_check':{'computed':reverse[1],'recorded':point[1],'tolerance':1e-7}}
        age_part=plan['context_evidence']['ages']
        evidence(mix,base+'/age_seconds',age_part,item['age_days'],'day','seconds = source_test_days * 86400',
                 {'axis_age':item['age_axis_value'],'nominal_age_days':item['age_days'],'tolerance_days':item['age_tolerance_days']})
        mix['field_provenance'][base+'/age_seconds']['transformation']={
            'kind':'unit_scale','formal':True,'formula':'seconds = days * 86400',
            'inputs':{'value':item['age_days'],'unit':'day','scale':86400},
            'output':{'value':item['age_seconds'],'unit':'s'},
            'forward_check':{'computed':item['age_days']*86400,'recorded':item['age_seconds'],'tolerance':1e-9},
            'reverse_check':{'computed':item['age_seconds']/86400,'recorded':item['age_days'],'tolerance':1e-9}}
        for name in ('specimen','method'):
            part=plan['context_evidence'][name]
            if name!='method':evidence(mix,base+'/'+name,part,part['raw'],None);continue
            path=base+'/method'
            first=evidence(mix,path,method_parts[0],part['raw'],None)
            links=[{**method_parts[0],'evidence_key':first}]
            template=next(e for e in result['evidence_links'] if e['evidence_key']==first)
            for number,piece in enumerate(method_parts[1:],1):
                key='ev-sparse-method-part-'+digest([first,number,piece])[:24]
                result['evidence_links'].append({**copy.deepcopy(template),**piece,'evidence_key':key,
                    'snippet_sha256':hashlib.sha256(piece['snippet'].encode()).hexdigest()})
                links.append({**piece,'evidence_key':key})
            mix['field_provenance'][path]['source_parts']=links
            mix['field_provenance'][path]['source_join_rule']='reading-order whitespace join; no standard-number inference'
        rows.append(row);added+=1
    receipt={'identities':identities,'input_records_sha256':plan['records_sha256'],'acceptance_sha256':digest(acceptance),
             'added':added,'supported':supported,'output_subject_sha256':subject(result)}
    result.setdefault('extensions',{})['sparse_performance_promotion']=receipt
    records.clear();records.update(result)
    return {'added':added,'supported':supported,'replay':False}
