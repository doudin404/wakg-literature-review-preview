"""Locate explicit study-wide fixed parameters; never infer their mass basis."""
import re
import copy
import hashlib
import json
from pathlib import Path
import fitz

VERSION='source-fixed-mix-parameters-v1'
NUMBER=r'\d+(?:\.\d+)?'
PATTERNS={
    'water-to-binder':rf'water-to-binder\s+ratio\s+of\s+(?P<value>{NUMBER})',
    'sand-to-binder':rf'sand-to-binder\s+ratio\s+of\s+(?P<value>{NUMBER})',
    'total alkali':rf'total\s+alkali\s+content\s+of\s+(?P<value>{NUMBER})\s+(?P<unit>Na2O%)',
    'precursor mass ratio':rf'a\s+(?P<a>[A-Za-z0-9#]+)-to-(?P<b>[A-Za-z0-9#]+)\s+mass\s+ratio\s+of\s+(?P<value>{NUMBER}:{NUMBER})',
    'aggregate blend mass ratio':rf'(?P<a>QS#[0-9]+)\s+and\s+(?P<b>QS#[0-9]+)\s+blended\s+at\s+a\s+mass\s+ratio\s+of\s+(?P<value>{NUMBER}:{NUMBER})',
}


def candidates(document):
    result=[]
    for page in document:
        blocks={}
        for word in page.get_text('words'):
            blocks.setdefault(word[5],[]).append(word)
        for words in blocks.values():
            words.sort(key=lambda w:(w[6],w[7]))
            text='';spans=[]
            for word in words:
                start=len(text);text+=word[4]+' ';spans.append((start,len(text)-1,word))
            marker=re.search(r'In this study,.*?other mix parameters were fixed as follows:',text)
            if not marker:continue
            # Only the fixed-parameter sentence. Later dosage variations are not shared constants.
            end=re.search(r'\.\s+(?=[A-Z])',text[marker.end():])
            stop=marker.end()+end.start()+1 if end else len(text)
            for label,pattern in PATTERNS.items():
                for match in re.finditer(pattern,text[marker.end():stop]):
                    start,finish=match.span('value');start+=marker.end();finish+=marker.end()
                    covering=[w for a,b,w in spans if a<=start and b>=finish]
                    if len(covering)!=1:continue
                    word=covering[0];native=fitz.Rect(word[:4]);box=native
                    for inset in (0,.15,.25,.30):
                        box=fitz.Rect(native.x0,native.y0+native.height*inset,native.x1,native.y1-native.height*inset)
                        if page.get_textbox(box).strip()==word[4]:break
                    snippet=page.get_textbox(box).strip()
                    if snippet!=word[4]:continue
                    raw=match['value'];parts=match.groupdict()
                    result.append({'parameter':label,'raw':raw,'value':raw if ':' in raw else float(raw),
                        'unit':parts.get('unit') or 'mass ratio','components':[parts['a'],parts['b']] if parts.get('a') else None,
                        'page':page.number+1,'bbox':list(box),'snippet':snippet,
                        'source_clause':match.group(),'scope':'explicit fixed mix parameters',
                        'status':'CANDIDATE_SCOPE_AND_BASIS_REVIEW_REQUIRED','extraction_method':VERSION})
    return result


def enrich(records,pdf_path,asset_key,mapping):
    """Project independently mapped reported ratios, without mass conversions."""
    pdf_sha=hashlib.sha256(Path(pdf_path).read_bytes()).hexdigest()
    if mapping.get('verdict')!='PASS' or mapping.get('pdf_sha256')!=pdf_sha or mapping.get('conversion_authorized') is not False:
        raise ValueError('fixed parameter mapping receipt mismatch')
    source=next(a for a in records['assets'] if a['asset_key']==asset_key)
    if source['sha256']!=pdf_sha:raise ValueError('fixed parameter PDF mismatch')
    with fitz.open(pdf_path) as doc: claims=candidates(doc)
    if len(claims)!=len(mapping['parameters']) or {c['parameter'] for c in claims}!=set(mapping['parameters']):
        raise ValueError('fixed parameter source coverage mismatch')
    if any(c['page']!=mapping['source_page'] for c in claims):
        raise ValueError('fixed parameter mapped source page changed')
    result=copy.deepcopy(records);links={e['evidence_key']:e for e in result['evidence_links']}
    mats={m['mat_key']:m for m in result['mats']}
    required_sands=next(c['components'] for c in claims if c['parameter']=='aggregate blend mass ratio')
    normalize=lambda value:re.sub(r'[^a-z0-9]','',str(value).lower())
    count=0
    for mix in result['mixes']:
        if mix['paper_key']!=source['paper_key']:continue
        materials=mix['modules']['materials']
        # The receipt covers the source's mortar mix-design table, not derived
        # sand-free specimen variants. Require positive owned sand quantities.
        aggregate=materials.get('fine_aggregate') or []
        if not aggregate or any(not isinstance(a.get('value'),(float,int)) or isinstance(a['value'],bool) or a['value']<=0 for a in aggregate):
            continue
        identity=mix['modules']['identity_source_specimen']
        if re.search(r'sand[- ]free|\bpaste\b',str(identity.get('specimen_type') or ''),re.I):continue
        actual_sands=[]
        for position,item in enumerate(aggregate):
            evidence=links.get(item.get('evidence_key'))
            mat=mats.get(item.get('mat_key'))
            field=f'/modules/materials/fine_aggregate/{position}/value'
            provenance=mix.get('field_provenance',{}).get(field,{})
            if (not mat or mat['paper_key']!=source['paper_key'] or not evidence or evidence['record_key']!=mix['mix_key']
                    or evidence['field_path']!=field or evidence['asset_key']!=asset_key
                    or evidence['paper_key']!=source['paper_key'] or provenance.get('evidence_key')!=evidence['evidence_key']
                    or float(provenance.get('original_value','nan'))!=item['value']):
                raise ValueError('fixed parameter mortar scope lacks source evidence')
            actual_sands.append(normalize(mat['custom_material_id']))
        if sorted(actual_sands)!=sorted(map(normalize,required_sands)):
            raise ValueError('fixed parameter mortar sand identities mismatch')
        rows=materials.setdefault('platform_ratios',[])
        if rows is None: rows=[];materials['platform_ratios']=rows
        for claim in claims:
            label=claim['parameter'];binding=mapping['parameters'][label]
            name='-to-'.join(claim['components']) if claim['components'] else label
            matches=[(i,r) for i,r in enumerate(rows) if r.get('name')==name]
            if len(matches)>1:raise ValueError('duplicate fixed parameter destination')
            index=matches[0][0] if matches else len(rows)
            field=f'/modules/materials/platform_ratios/{index}/value'
            key='ev-fixed-mix-'+hashlib.sha256(json.dumps([mix['mix_key'],claim],sort_keys=True).encode()).hexdigest()[:24]
            row={'name':name,'mat_key':None,'value':claim['value'],'unit':claim['unit'],'transformation':None,'evidence_key':key,
                 'extensions':{'source_parameter_key':'fixed:'+label,'original_label':name,'status':'reported','reason':None,
                    'basis':binding['basis'],'scope':binding['scope'],'components':claim['components'],'mapping_message_id':mapping['message_id']}}
            evidence={'evidence_key':key,'asset_key':asset_key,'paper_key':mix['paper_key'],'record_type':'mix','record_key':mix['mix_key'],
                'field_path':field,'page':claim['page'],'bbox':claim['bbox'],'snippet':claim['snippet'],
                'snippet_sha256':hashlib.sha256(claim['snippet'].encode()).hexdigest(),'section':mapping['source_section'],
                'figure_number':None,'table_number':None,'extraction_method':VERSION,'confidence':.99,
                'extensions':{'source_clause':claim['source_clause'],'source_scope':claim['scope'],'basis':binding['basis']}}
            provenance={'evidence_key':key,'original_value':claim['raw'],'original_unit':claim['unit'],'formula':None,
                'extraction_method':VERSION,'confidence':.99,'review_status':'confirmed'}
            if matches:
                if matches[0][1]!=row or links.get(key)!=evidence or mix.get('field_provenance',{}).get(field)!=provenance:
                    raise ValueError('fixed parameter canonical conflict')
                continue
            rows.append(row);result['evidence_links'].append(evidence);links[key]=evidence
            materials.setdefault('extensions',{}).setdefault('reported_parameters',[]).append({
                'parameter_key':'fixed:'+label,'original_label':name,'original_value':claim['raw'],'original_unit':claim['unit'],
                'value':claim['value'],'unit':claim['unit'],'evidence_key':key,'extraction_method':VERSION,
                'confidence':.99,'status':'reported','reason':None,'semantic_role':'reported_formulation','transformation':None})
            mix.setdefault('field_provenance',{})[field]=provenance;count+=1
    records.clear();records.update(result)
    return count
