"""Discover explicitly named fine-aggregate pairs and their reported diameters.

Names, values, types and roles come from the source sentence, not output fixtures.
Uses original MAT slots and never projects aggregate data onto a precursor MAT.
"""
import copy
import hashlib
import json
import re

import fitz

VERSION='source-explicit-aggregate-pair-v1'
PAIR=re.compile(
    r'A blend of (?P<type>quartz sands) (?P<first>[A-Za-z][A-Za-z0-9#_-]*) and '
    r'(?P<second>[A-Za-z][A-Za-z0-9#_-]*), with D(?P<percentile>10|50|90) '
    r'values of (?P<a>\d+(?:\.\d+)?) (?P<ua>[μµu]m) and '
    r'(?P<b>\d+(?:\.\d+)?) (?P<ub>[μµu]m) respectively, was used as fine aggregate\b')


def enrich(records,pdf,asset_key,scopes,geometry,factory):
    result=copy.deepcopy(records)
    additions=[]
    with fitz.open(pdf) as document:
        for number,regions in scopes(document).items():
            page=document[number-1]
            text,positions=geometry._mapped_page_text(page)
            for match in PAIR.finditer(text):
                boxes=[fitz.Rect(b) for b in positions[match.start():match.end()] if b is not None]
                if not boxes or not all(any(r.contains(b) for r in regions) for b in boxes):
                    continue
                if match['first']==match['second']:
                    raise ValueError('aggregate names are not distinct')
                parts={}
                for name in ('type','first','second','percentile','a','b','ua','ub'):
                    start,end=match.span(name)
                    token=match[name]
                    if name=='percentile':
                        start-=1
                        token='D'+token
                    parts[name]={'page':number,'snippet':token,
                                 'bbox':geometry._mapped_bbox(page,positions,(start,end),token)}
                for name,value,unit in (('first','a','ua'),('second','b','ub')):
                    label=match[name]
                    owners=[m for m in result['mats'] if m.get('custom_material_id')==label]
                    if len(owners)>1:
                        raise ValueError('aggregate MAT identity not unique')
                    if owners:
                        mat=owners[0]
                    else:
                        definition={'key':'aggregate-'+hashlib.sha256(label.encode()).hexdigest()[:12],
                                    'label':label,'material_type':None,'role':'fine_aggregate'}
                        mat=factory(definition)
                        if any(m['mat_key']==mat['mat_key'] for m in result['mats']):
                            raise ValueError('aggregate MAT key collision')
                        result['mats'].append(mat)
                        additions.append(mat['mat_key'])
                    field='d'+match['percentile']+'_um'
                    values=[('/custom_material_id',label,parts[name],None),
                            ('/material_type',match['type'],parts['type'],None),
                            ('/particle_size_distribution/'+field,float(match[value]),parts[value],match[unit])]
                    for path,typed,part,original_unit in values:
                        target=mat if path.count('/')==1 else mat['particle_size_distribution']
                        key=path.rsplit('/',1)[-1]
                        if target.get(key) not in (None,typed):
                            raise ValueError('explicit aggregate source conflicts with canonical value')
                        evidence_key='ev-aggregate-'+hashlib.sha256(json.dumps([asset_key,mat['mat_key'],path,part],sort_keys=True).encode()).hexdigest()[:24]
                        link={'evidence_key':evidence_key,'asset_key':asset_key,'paper_key':mat['paper_key'],
                              'record_type':'mat','record_key':mat['mat_key'],'field_path':path,**part,
                              'section':'Materials','table_number':None,'figure_number':None,
                              'snippet_sha256':hashlib.sha256(part['snippet'].encode()).hexdigest(),
                              'extraction_method':VERSION,'confidence':.99,
                              'extensions':{'ordered_material_binding':parts,'context_sha256':hashlib.sha256(match.group().encode()).hexdigest()}}
                        prior=[e for e in result['evidence_links'] if e['evidence_key']==evidence_key]
                        if prior and prior!=[link]:
                            raise ValueError('aggregate source evidence drift')
                        if not prior:
                            result['evidence_links'].append(link)
                        provenance={'evidence_key':evidence_key,'original_value':part['snippet'],'original_unit':original_unit,
                                    'formula':None,'extraction_method':VERSION,'confidence':.99,'review_status':'confirmed'}
                        old=mat.setdefault('field_provenance',{}).get(path)
                        if old is not None and old!=provenance:
                            raise ValueError('aggregate provenance conflict requires reconciliation')
                        mat['field_provenance'][path]=provenance
                        target[key]=typed
    records.clear();records.update(result)
    return {'created':additions}
