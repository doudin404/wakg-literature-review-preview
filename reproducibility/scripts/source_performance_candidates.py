"""Collect source-localized prose performance values for grouped semantic review.

The caller supplies reviewed result pages. No MIX identity, age or performance
type is guessed from a nearby number, and no candidate is a formal observation.
"""
import re
import copy
import hashlib
import json
from pathlib import Path
import fitz

VERSION='source-performance-prose-candidates-v1'
VALUE=re.compile(r'(?<![\w.])(?P<value>\d+(?:\.\d+)?)\s+(?P<unit>MPa(?:[⋅·]m1/2)?|GPa|min|mm|J/m2)(?![\w/])')


def locate(words,line_regex,value_regex,allow_multiple=False):
    """Resolve the numeric token inside the matched clause, never page-wide."""
    words=sorted(words,key=lambda w:(w[5],w[6],w[7]))
    text='';spans=[]
    for word in words:
        start=len(text);text+=str(word[4])+' ';spans.append((start,len(text)-1,word))
    matches=list(re.finditer(line_regex,text,re.I))
    if len(matches)!=1:raise ValueError('performance source clause is absent or ambiguous')
    match=matches[0];values=list(re.finditer(value_regex,match.group(),re.I))
    if len(values)!=1:raise ValueError('performance value expression is absent or ambiguous')
    value=values[0];start,end=value.span(1);start+=match.start();end+=match.start()
    selected=[w for a,b,w in spans if b>start and a<end]
    if not selected or (not allow_multiple and len(selected)!=1):raise ValueError('performance value does not occupy one source token')
    return match.group(),value.group(1),selected if allow_multiple else selected[0]


def enrich(records,pdf_path,binding):
    """Atomic original-field observation projection from reviewed source recipes."""
    digest=lambda value:hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    pdf_sha=hashlib.sha256(Path(pdf_path).read_bytes()).hexdigest()
    if pdf_sha!=binding['pdf_sha256'] or not binding.get('mapping_message_id'):
        raise ValueError('prose performance source mapping mismatch')
    result=copy.deepcopy(records)
    source=next(a for a in result['assets'] if a['kind']=='pdf' and a['sha256']==pdf_sha)
    planned=[]
    with fitz.open(pdf_path) as doc:
        for item in binding['items']:
            item={**binding.get('defaults',{}),**item}
            fresh=item.get('age') is None
            if fresh and (item.get('age_basis')!='fresh_state_not_applicable'
                    or item['name'] not in ('slump','flow_diameter','initial_setting_time','final_setting_time','setting_time')):
                raise ValueError('missing age requires reviewed fresh-state context')
            owners=[m for m in result['mixes'] if m['paper_key']==source['paper_key']
                and m['modules']['identity_source_specimen']['custom_test_id']==item['source_id']
                and (item.get('owner_specimen') is None or m['modules']['identity_source_specimen'].get('specimen_type')==item['owner_specimen'])
                and m['extensions'].get('source_table')==item['source_table'] and m['extensions'].get('source_row')==item['source_row']]
            if len(owners)!=1:raise ValueError('prose performance exact MIX binding missing')
            owner=owners[0];identity_path='/modules/identity_source_specimen/custom_test_id'
            identity_prov=owner.get('field_provenance',{}).get(identity_path,{})
            identity_link=next((e for e in result['evidence_links'] if e['evidence_key']==identity_prov.get('evidence_key')),None)
            if not identity_prov:
                page=doc[binding['identity_page']-1];expression=re.escape(item['source_id'])
                identity_clause=item.get('identity_clause',r'(?<!\S)'+expression+r'(?!\S)')
                _,raw,word=locate(page.get_text('words'),identity_clause,'('+expression+')')
                native=fitz.Rect(word[:4]);box=native;snippet=page.get_textbox(box).strip()
                for inset in (.15,.25,.30):
                    if snippet==raw:break
                    box=fitz.Rect(native.x0,native.y0+native.height*inset,native.x1,native.y1-native.height*inset)
                    snippet=page.get_textbox(box).strip()
                if snippet!=raw:raise ValueError('prose performance original MIX token unreadable')
                key='ev-prose-identity-'+digest([owner['mix_key'],raw,binding['identity_page'],list(box)])[:24]
                identity_link={'evidence_key':key,'asset_key':source['asset_key'],'paper_key':owner['paper_key'],'record_key':owner['mix_key'],
                    'record_type':'mix','field_path':identity_path,'page':binding['identity_page'],'bbox':list(box),'snippet':snippet,
                    'snippet_sha256':hashlib.sha256(snippet.encode()).hexdigest(),'section':None,'table_number':None,'figure_number':None,
                    'extraction_method':VERSION,'confidence':.99}
                result['evidence_links'].append(identity_link)
                owner.setdefault('field_provenance',{})[identity_path]={'evidence_key':key,'original_value':raw,'original_unit':None,
                    'formula':None,'extraction_method':VERSION,'confidence':.99,'review_status':'confirmed'}
            identity_token=identity_link.get('snippet','') if identity_link else ''
            identity_matches=identity_token==item['source_id']
            if item.get('owner_specimen') is not None:
                identity_matches=bool(re.fullmatch(re.escape(item['source_id'])+r'[,.;]?',identity_token))
            if (not identity_link or identity_link['record_key']!=owner['mix_key'] or identity_link['field_path']!=identity_path
                    or identity_link['asset_key']!=source['asset_key'] or not identity_matches
                    or doc[identity_link['page']-1].get_textbox(fitz.Rect(identity_link['bbox'])).strip()!=identity_token):
                raise ValueError('prose performance MIX identity source readback failed')
            fields={}
            for name in ('value',*(() if fresh else ('age',)),'specimen','method',*(['calculation_method'] if 'calculation_method' in item else [])):
                recipe=item[name];page=doc[recipe['page']-1]
                phrase,raw,words=locate(page.get_text('words'),recipe['clause'],recipe['capture'],allow_multiple=True)
                box=fitz.Rect(words[0][:4])
                for word in words[1:]:box|=fitz.Rect(word[:4])
                snippet=page.get_textbox(box).strip()
                if len({tuple(word[5:7]) for word in words})==1:
                    native=fitz.Rect(box)
                    expected=' '.join(str(word[4]) for word in words)
                    for inset in (0,.15,.25,.30,.40):
                        box=fitz.Rect(native.x0,native.y0+native.height*inset,native.x1,native.y1-native.height*inset)
                        snippet=page.get_textbox(box).strip()
                        if snippet==expected:break
                    else:raise ValueError('prose performance line locator contains adjacent text')
                if raw not in re.sub(r'\s+',' ',snippet):
                    raise ValueError('prose performance source span readback failed')
                fields[name]={'raw':raw,'page':recipe['page'],'bbox':list(box),'snippet':snippet,'clause':phrase}
            planned.append((owners[0],item,fields))
    for mix,item,fields in planned:
        age_seconds=float(fields['age']['raw'])*86400 if 'age' in fields else None
        observations=mix['modules']['performance'];identity=digest([binding['mapping_message_id'],item])
        existing=[(i,o) for i,o in enumerate(observations) if o.get('extensions',{}).get('prose_binding_id')==identity]
        if len(existing)>1:raise ValueError('duplicate prose performance binding')
        index=existing[0][0] if existing else len(observations)
        if not existing and any(o['name']==item['name'] and o.get('age_seconds')==age_seconds for o in observations):
            raise ValueError('prose performance would duplicate existing observation')
        observation={'name':item['name'],'value':float(fields['value']['raw']),'unit':item['unit'],
            'age_seconds':age_seconds,'specimen':fields['specimen']['raw'],'method':fields['method']['raw'],
            'extensions':{'prose_binding_id':identity,'mapping_message_id':binding['mapping_message_id'],
                'original_value':fields['value']['raw'],'original_unit':item['unit'],'extraction_method':VERSION,'confidence':.99}}
        if age_seconds is None:observation['extensions']['age_basis']=item['age_basis']
        if 'calculation_method' in fields:
            observation['extensions']['calculation_method']=fields['calculation_method']['raw']
            observation['extensions']['calculation_method_scope']='source-cited formula; not full standard validity certification'
        for name,source_field in fields.items():
            destination='age_seconds' if name=='age' else 'extensions/calculation_method' if name=='calculation_method' else name
            path=f'/modules/performance/{index}/{destination}';key='ev-prose-performance-'+digest([mix['mix_key'],path,source_field])[:24]
            link={'evidence_key':key,'asset_key':source['asset_key'],'paper_key':mix['paper_key'],'record_key':mix['mix_key'],
                'record_type':'mix','field_path':path,'page':source_field['page'],'bbox':source_field['bbox'],
                'snippet':source_field['snippet'],'snippet_sha256':hashlib.sha256(source_field['snippet'].encode()).hexdigest(),
                'section':None,'table_number':None,'figure_number':None,'extraction_method':VERSION,'confidence':.99,
                'extensions':{'source_clause':source_field['clause'],'mapping_message_id':binding['mapping_message_id']}}
            provenance={'evidence_key':key,'original_value':source_field['raw'],'original_unit':'day' if name=='age' else item['unit'] if name=='value' else None,
                'formula':None,'extraction_method':VERSION,'confidence':.99,'review_status':'confirmed'}
            if name=='age':
                provenance['formula']='seconds = days * 86400'
                days=float(source_field['raw']);seconds=observation['age_seconds']
                provenance['transformation']={'kind':'unit_scale','formal':True,'formula':provenance['formula'],
                    'inputs':{'value':days,'unit':'day','scale':86400.},'output':{'value':seconds,'unit':'s'},
                    'forward_check':{'computed':days*86400.,'recorded':seconds,'tolerance':1e-9},
                    'reverse_check':{'computed':seconds/86400.,'recorded':days,'tolerance':1e-9}}
            if name=='value':observation['extensions']['evidence_key']=key
            old=next((e for e in result['evidence_links'] if e['evidence_key']==key),None)
            if existing:
                if old!=link or mix['field_provenance'].get(path)!=provenance:raise ValueError('prose performance evidence drift')
            else:
                if old:raise ValueError('unexpected preexisting prose evidence')
                result['evidence_links'].append(link);mix.setdefault('field_provenance',{})[path]=provenance
        if existing:
            if existing[0][1]!=observation:raise ValueError('prose performance observation drift')
        else:observations.append(observation)
    records.clear();records.update(result)
    return len(planned)


def candidates(document,pages):
    if any(not isinstance(n,int) or isinstance(n,bool) or not 1<=n<=len(document) for n in pages):
        raise ValueError('performance candidate pages outside source')
    output=[]
    for number in sorted(set(pages)):
        page=document[number-1];blocks={}
        for word in page.get_text('words'):blocks.setdefault(word[5],[]).append(word)
        for words in blocks.values():
            words.sort(key=lambda w:(w[6],w[7]));text='';spans=[]
            for word in words:
                start=len(text);text+=word[4]+' ';spans.append((start,len(text)-1,word))
            for match in VALUE.finditer(text):
                parts=[]
                for name in ('value','unit'):
                    start,end=match.span(name)
                    covering=[w for a,b,w in spans if a<=start and b>=end]
                    if len(covering)!=1:break
                    word=covering[0];native=fitz.Rect(word[:4]);box=native
                    for fraction in (0,.15,.25,.30):
                        box=fitz.Rect(native.x0,native.y0+native.height*fraction,native.x1,native.y1-native.height*fraction)
                        if page.get_textbox(box).strip()==word[4]:break
                    snippet=page.get_textbox(box).strip()
                    if snippet!=word[4] or match[name] not in snippet:break
                    parts.append({'part':name,'bbox':list(box),'snippet':snippet})
                if len(parts)!=2:continue
                output.append({'page':number,'value':float(match['value']),'raw':match['value'],'unit':match['unit'],
                    'parts':parts,'context':text[max(0,match.start()-240):match.end()+160].strip(),
                    'status':'CANDIDATE_REQUIRES_PROPERTY_MIX_AGE_SPECIMEN_BINDING','extraction_method':VERSION})
    return output
