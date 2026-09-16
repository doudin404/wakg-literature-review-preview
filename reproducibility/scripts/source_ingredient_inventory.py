"""Create source-named ingredient MATs and bind existing positive quantities.

Does not convert solution doses, decompose ingredients, or alter measured values.
Source recipes and explicit parameter routes require independent semantic review.
"""
import copy
import hashlib
import json
import math
import unicodedata
from pathlib import Path
import fitz
from source_performance_candidates import locate
from cached_pdf_text import CachedTextDocument

VERSION='source-ingredient-inventory-v1'


def normalized_readback(value):
    """Compare PDF ligatures without changing the preserved source spelling."""
    return ' '.join(unicodedata.normalize('NFKC', value).split())


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def check_quantity(link,path,mix,asset,parameter,document,readback_cache):
    if (not link or link.get('record_key')!=mix['mix_key'] or link.get('asset_key')!=asset['asset_key']
            or link.get('paper_key')!=asset['paper_key'] or link.get('field_path')!=path):
        raise ValueError('ingredient exact quantity evidence mismatch')
    snippet=link.get('snippet','')
    if hashlib.sha256(snippet.encode()).hexdigest()!=link.get('snippet_sha256'):
        raise ValueError('ingredient quantity snippet hash mismatch')
    readback_key=(link['page'],tuple(link['bbox']),snippet)
    if readback_key not in readback_cache:
        page=document[link['page']-1]
        native=fitz.Rect(link['bbox']);bbox=native
        for inset in (.40,.30,.25,.15,0):
            bbox=fitz.Rect(native.x0,native.y0+native.height*inset,native.x1,native.y1-native.height*inset)
            actual=page.get_textbox(bbox).strip()
            if actual==snippet:break
        readback_cache[readback_key]=(actual,list(bbox))
    actual,bbox=readback_cache[readback_key]
    if actual!=snippet or str(parameter.get('original_value'))!=snippet:
        raise ValueError('ingredient quantity source readback mismatch')
    if (parameter.get('transformation') is not None or parameter.get('original_unit')!=parameter.get('unit')
            or float(snippet)!=parameter['value']):
        raise ValueError('ingredient quantity conversion not reviewed by identity binder')
    return {'page':link['page'],'bbox':list(bbox),'snippet':snippet,'source_evidence_key':link['evidence_key']}


def enrich(records,pdf,spec,factory):
    if hashlib.sha256(Path(pdf).read_bytes()).hexdigest()!=spec['pdf_sha256']:
        raise ValueError('ingredient PDF mismatch')
    if not spec.get('mapping_message_id'):raise ValueError('ingredient mapping identity missing')
    result=copy.deepcopy(records)
    assets=[a for a in result['assets'] if a['kind']=='pdf' and a['sha256']==spec['pdf_sha256']]
    if len(assets)!=1:raise ValueError('ingredient source asset ambiguous')
    asset=assets[0];created=[];bound=0
    with fitz.open(pdf) as doc:
        cached=CachedTextDocument(doc)
        readback_cache={}
        for item in spec['items']:
            parts={}
            for field in ('custom_material_id','material_type'):
                recipe=item[field];page=doc[recipe['page']-1]
                clause,raw,words=locate(page.get_text('words'),recipe['clause'],recipe['capture'],True)
                bbox=fitz.Rect(words[0][:4])
                for word in words[1:]:bbox|=fitz.Rect(word[:4])
                snippet=page.get_textbox(bbox).strip()
                if normalized_readback(raw) not in normalized_readback(snippet):
                    raise ValueError('ingredient identity readback failed')
                parts[field]={'raw':raw,'page':recipe['page'],'bbox':list(bbox),'snippet':snippet,'clause':clause}
            expected=factory({'key':item['key'],'label':parts['custom_material_id']['raw'],
                              'material_type':parts['material_type']['raw'],'role':item['role']})
            if expected['paper_key']!=asset['paper_key']:
                raise ValueError('ingredient factory paper differs from PDF asset')
            matches=[m for m in result['mats'] if m['mat_key']==expected['mat_key']]
            if len(matches)>1:raise ValueError('duplicate ingredient MAT')
            if matches:
                mat=matches[0]
                if mat['paper_key']!=expected['paper_key'] or mat.get('extensions',{}).get('ingredient_binding')!=digest(item):
                    raise ValueError('ingredient MAT identity collision')
            else:
                mat=expected;mat['extensions']['ingredient_binding']=digest(item)
                result['mats'].append(mat);created.append(mat['mat_key'])
            for field,part in parts.items():
                if mat[field]!=part['raw']:raise ValueError('ingredient field drift')
                path='/'+field;key='ev-ingredient-'+digest([mat['mat_key'],path,part])[:24]
                link={'evidence_key':key,'asset_key':asset['asset_key'],'paper_key':mat['paper_key'],
                      'record_key':mat['mat_key'],'record_type':'mat','field_path':path,
                      'page':part['page'],'bbox':part['bbox'],'snippet':part['snippet'],
                      'snippet_sha256':hashlib.sha256(part['snippet'].encode()).hexdigest(),
                      'section':'Materials','table_number':None,'figure_number':None,
                      'extraction_method':VERSION,'confidence':.99,
                      'extensions':{'source_clause':part['clause'],'mapping_message_id':spec['mapping_message_id']}}
                prior=[e for e in result['evidence_links'] if e['evidence_key']==key]
                if prior and prior!=[link]:raise ValueError('ingredient evidence drift')
                if not prior:result['evidence_links'].append(link)
                prov={'evidence_key':key,'original_value':part['raw'],'original_unit':None,'formula':None,
                      'extraction_method':VERSION,'confidence':.99,'review_status':'confirmed'}
                old=mat.setdefault('field_provenance',{}).get(path)
                if old is not None and old!=prov:raise ValueError('ingredient provenance drift')
                mat['field_provenance'][path]=prov
            links={e['evidence_key']:e for e in result['evidence_links']}
            for mix in result['mixes']:
                if mix['paper_key']!=mat['paper_key']:continue
                materials=mix['modules']['materials']
                params=materials.get('extensions',{}).get('reported_parameters',[])
                selected=[(i,p) for i,p in enumerate(params) if p.get('parameter_key')==item['parameter_key']]
                if len(selected)>1:raise ValueError('ingredient quantity parameter ambiguous')
                if not selected:continue
                parameter_index,parameter=selected[0];value=parameter.get('value')
                if value is None or value==0:continue
                if type(value) not in (int,float) or not math.isfinite(value) or value<0:raise ValueError('invalid ingredient quantity')
                quantity_evidence=links.get(parameter.get('evidence_key'))
                quantity_locator=check_quantity(quantity_evidence,f'/modules/materials/extensions/reported_parameters/{parameter_index}/value',
                                                mix,asset,parameter,cached,readback_cache)
                rows=[parameter]
                for destination in ('solid_materials','activators','fine_aggregate','coarse_aggregate'):
                    for index,row in enumerate(materials.get(destination) or []):
                        if row.get('extensions',{}).get('source_parameter_key')!=item['parameter_key']:continue
                        path=f'/modules/materials/{destination}/{index}/value'
                        evidence=links.get(row.get('evidence_key'))
                        check_quantity(evidence,path,mix,asset,parameter,cached,readback_cache)
                        prov=mix.get('field_provenance',{}).get(path,{})
                        if (prov.get('evidence_key')!=row.get('evidence_key') or prov.get('original_value')!=parameter.get('original_value')
                                or prov.get('original_unit')!=parameter.get('original_unit')):
                            raise ValueError('ingredient canonical quantity provenance mismatch')
                        rows.append(row)
                for row in rows:
                    if row.get('mat_key') not in (None,mat['mat_key']):raise ValueError('ingredient link conflict')
                    if row.get('value')!=value or row.get('unit')!=parameter.get('unit'):
                        raise ValueError('ingredient canonical quantity drift')
                    row['mat_key']=mat['mat_key']
                    row.setdefault('extensions',{})['ingredient_identity_evidence_key']=mat['field_provenance']['/custom_material_id']['evidence_key']
                    row['extensions']['ingredient_quantity_locator']=quantity_locator
                refs=materials.setdefault('mat_refs',[])
                if mat['mat_key'] not in refs:refs.append(mat['mat_key'])
                bound+=1
    records.clear();records.update(result)
    return {'created':created,'bound_quantities':bound}
