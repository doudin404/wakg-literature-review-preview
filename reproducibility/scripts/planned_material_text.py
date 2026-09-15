"""Source-only MAT text bindings consumed by the existing block dispatcher."""
import copy
import hashlib
import json
import math
import re
from pathlib import Path
import fitz
from jsonschema import validate
from source_quantity_producer import obj,invoke,atomic_json
from source_specimen_variants import digest
from paper_fill_plan import completed_response
from material_candidate_identity import append_candidate

VERSION='planned-material-text-v1'
FIELDS={
    'specific_surface_m2_kg':('m2/kg',{'m2/kg':1,'m2/g':1000,'cm2/g':0.1}),
    'true_density_kg_m3':('kg/m3',{'kg/m3':1,'g/cm3':1000}),
    'apparent_density_kg_m3':('kg/m3',{'kg/m3':1,'g/cm3':1000}),
    'bulk_density_kg_m3':('kg/m3',{'kg/m3':1,'g/cm3':1000}),
    **{k:('percent',{'%':1,'percent':1}) for k in
       ('total_porosity_percent','open_porosity_percent','closed_porosity_percent')},
    'specific_surface_method':(None,{})}


def eligible(task,slots):
    return task['kind'] in ('text','table') and any(s['field_path'] in ('/custom_material_id','/physical_properties') for s in slots)


def catalogue(context):
    keys={k for s in context['slots'] for k in context['objects'][s['object_id']]['existing_keys']}
    return [{'key':m['mat_key'],'label':m['custom_material_id'],'paper_key':m['paper_key']}
            for m in context['records']['mats'] if m['mat_key'] in keys]


def prepare(context):
    index=context['index'];doc=index['document'];tokens=[]
    if hashlib.sha256(Path(doc['path']).read_bytes()).hexdigest()!=doc['sha256']:
        raise ValueError('raw material PDF changed')
    with fitz.open(doc['path']) as pdf:
        for source in context['sources']:
            if source['kind']=='table':
                from source_table_cells import native_matrix
                table=native_matrix(index,source,index['sources'],context['records']['assets'])
                for r,row in enumerate(table['rows']):
                    for c,cell in enumerate(row):
                        loc=cell.get('source_locator',{'coordinate_space':'pdf_points'})
                        # Native PDF cells are words already; DOCX cells may
                        # contain a phrase and retain its character offsets.
                        parts=list(re.finditer(r'\S+',cell['text'])) if cell['bbox'] is None else [None]
                        for j,part in enumerate(parts):
                            tokens.append({'token_id':source['source_id']+f':r{r}c{c}w{j}',
                                'source_id':source['source_id'],'document_sha256':source['document_sha256'],
                                'page':source.get('page'),'bbox':cell['bbox'],'text':part.group() if part else cell['text'],
                                'source_locator':{**loc,**({'start':part.start(),'end':part.end()} if part else {})}})
                continue
            if source['kind']!='text':continue
            if source.get('coordinate_space')=='docx_paragraph':
                for i,match in enumerate(re.finditer(r'\S+',source['text'])):
                    tokens.append({'token_id':source['source_id']+':w'+str(i),'source_id':source['source_id'],
                        'document_sha256':source['document_sha256'],'page':None,'bbox':None,'text':match.group(),
                        'source_locator':{'coordinate_space':'docx_paragraph','body_index':source['source_object']['body_index'],
                                          'start':match.start(),'end':match.end()}})
                continue
            if source['document_sha256']!=doc['sha256']:
                assets=[a for a in context['records']['assets'] if a['sha256']==source['document_sha256'] and a.get('kind')=='pdf']
                if len(assets)!=1:raise ValueError('material text document asset is not unique')
                with fitz.open(assets[0]['relative_path']) as supplement:
                    words=supplement[source['page']-1].get_text('words',clip=fitz.Rect(source['bbox']))
            else:words=pdf[source['page']-1].get_text('words',clip=fitz.Rect(source['bbox']))
            for i,w in enumerate(words):
                tokens.append({'token_id':source['source_id']+':w'+str(i),'source_id':source['source_id'],
                    'page':source['page'],'bbox':list(w[:4]),'text':w[4],
                    **({'document_sha256':source['document_sha256']} if source['document_sha256']!=doc['sha256'] else {})})
    request={'version':VERSION,'index_sha256':index['index_sha256'],'document':doc,
        'task':context['task'],'slots':context['slots'],
        'objects':[context['objects'][s] for s in sorted({s['object_id'] for s in context['slots']})],
        'identities':catalogue(context),'sources':context['sources'],'tokens':tokens,
        'numeric_token_ids':[t['token_id'] for t in tokens if re.fullmatch(r'[+-]?[0-9]+(?:\.[0-9]+)?',t['text'].strip('(),;.%'))]}
    request['request_sha256']=digest(request);return request


def schema():
    s={'type':'string'};nullable={'type':['string','null']};refs={'type':'array','items':s,'minItems':1}
    return obj({'request_sha256':s,
        'materials':{'type':'array','items':obj({'entity_id':s,'object_id':s,'existing_key':nullable,'identity_token_ids':refs,'reason':s})},
        'claims':{'type':'array','items':obj({'claim_id':s,'entity_id':s,
            'field_path':{'type':['string','null'],'enum':[None]+['/physical_properties/'+k for k in FIELDS]},
            'status':{'type':'string','enum':['REPORTED','MISSING','AMBIGUOUS','OTHER_PROPERTY','OUTSIDE_CONTRACT']},
            'token_ids':refs,'unit':nullable,'meaning':s,'reason':s})},
        'coverage':{'type':'array','items':obj({'token_id':s,'claim_ids':{'type':'array','items':s},'reason':s})},
        'unresolved':{'type':'array','items':s}})


PROMPT='''Read the supplied paper text for this raw-material task and return the schema JSON.
Identify source-defined materials using their names and the supplied identity catalogue.
Use existing_key for a unique match, or null for a new material in its planned object family.
For each property select its field_path, exact numeric token, source unit and source meaning.
Scripts read the numbers and coordinates and perform unit conversions. Method descriptions
use contiguous tokens. Follow the source's material order and respectively relationships.
Units use m2/kg, m2/g, cm2/g, kg/m3, g/cm3 or percent as appropriate.
Keep density type and surface-area method as reported; unspecified kinds stay AMBIGUOUS.
Use MISSING for a missing symbol, and OTHER_PROPERTY with field_path=null for a source fact
belonging to another property group. Keep its tokens for subsequent mapping.
Coverage may be an empty array: source indexing already retains the complete text. Focus
the response on material facts and unresolved material associations.'''


def producer(context):
    directory=Path(context['material_text_directory'])/context['task']['task_id'];request_file=directory/'request.json'
    if request_file.exists():
        req=json.loads(request_file.read_bytes())
        if (req['index_sha256']!=context['index']['index_sha256'] or req['task']!=context['task']
                or req['identities']!=catalogue(context) or req['slots']!=context['slots']):
            raise ValueError('raw material frozen scope changed')
        if not (directory/'model/response.json').exists():raise ValueError('unfinished raw-material attempt; no automatic retry')
        response=completed_response(directory/'model');calls=0
    else:
        if not context.get('allow_material_call'):raise ValueError('raw-material semantics missing; one bounded source call required')
        req=prepare(context);atomic_json(request_file,req)
        visible={k:v for k,v in req.items() if k!='document'}
        visible['tokens']=[{k:t[k] for k in ('token_id','source_id','text')} for t in req['tokens']]
        invoke(req,directory/'model',schema=schema(),visible=visible,prompt_text=PROMPT,timeout=600)
        response=completed_response(directory/'model');calls=1
    return {'values':[],'new_slots':[],'complete_slots':[],
        'material_text':{'request':req,'response':response},'invocation_model_calls':calls,
        'source_artifact':{'request_path':str(request_file),'request_sha256':req['request_sha256']},
        'remaining_reason':'Source-bound MAT subset; ambiguous density and out-of-contract fields remain unresolved'}


def consume(records,request,response):
    """Transactional field write, original values preserved, conflicts kept as candidates."""
    validate(response,schema())
    if request['request_sha256']!=digest({k:v for k,v in request.items() if k!='request_sha256'}):raise ValueError('raw material request changed')
    if response['request_sha256']!=request['request_sha256']:raise ValueError('raw material response mismatch')
    staged=copy.deepcopy(records);tokens={t['token_id']:t for t in request['tokens']}
    mats={m['mat_key']:m for m in staged['mats']};entities={};fills=[];events=[]
    claims={c['claim_id']:c for c in response['claims']}
    if len(claims)!=len(response['claims']):raise ValueError('duplicate raw material claim')
    assets=[a for a in staged['assets'] if a['sha256']==request['document']['sha256'] and a.get('kind')=='pdf']
    if len(assets)!=1:raise ValueError('raw material PDF identity ambiguous')
    pdf=assets[0]
    def pending(mat,item,reason,words=()):
        candidate={**copy.deepcopy(item),'reason':reason,'source_tokens':list(words),
                   'request_sha256':request['request_sha256'],'formal':False}
        if mat is not None:append_candidate(mat,candidate,request)
        else:
            retained=staged.setdefault('extensions',{}).setdefault('unresolved_material_entities',[])
            if candidate not in retained:retained.append(candidate)
        events.append({'record_key':mat['mat_key'] if mat else None,
                       'field_path':item.get('field_path'),'status':'AMBIGUOUS','reason':reason})
    def selected(ids,contiguous=True):
        if not ids or not set(ids)<=tokens.keys():raise ValueError('raw material token outside source')
        words=[tokens[k] for k in ids]
        if len({w['source_id'] for w in words})!=1:raise ValueError('raw material value spans source blocks')
        positions=[next(i for i,t in enumerate(request['tokens']) if t['token_id']==key) for key in ids]
        if positions!=sorted(set(positions)) or (contiguous and positions!=list(range(positions[0],positions[0]+len(positions)))):
            raise ValueError('raw material text tokens must be contiguous and ordered')
        return words
    def evidence(mat,path,words,unit,formula=None):
        text=' '.join(w['text'] for w in words);key='ev-mattext-'+digest([mat['mat_key'],path,request['document']['sha256'],words,unit,formula])[:20]
        shas={w.get('document_sha256',request['document']['sha256']) for w in words}
        assets=[a for a in staged['assets'] if a['sha256'] in shas and a.get('paper_key')==mat['paper_key']]
        if len(shas)!=1 or len(assets)!=1:raise ValueError('raw material evidence document is not unique')
        locator=copy.deepcopy(words[0].get('source_locator',{'coordinate_space':'pdf_points'}))
        if locator['coordinate_space'].startswith('docx'):locator['end']=words[-1]['source_locator']['end']
        bbox=None if any(w['bbox'] is None for w in words) else [min(w['bbox'][0] for w in words),min(w['bbox'][1] for w in words),max(w['bbox'][2] for w in words),max(w['bbox'][3] for w in words)]
        link={'evidence_key':key,'record_key':mat['mat_key'],'record_type':'mat','paper_key':mat['paper_key'],
            'field_path':path,'asset_key':assets[0]['asset_key'],'page':words[0]['page'],
            'bbox':bbox,'source_locator':locator,'table_number':locator.get('table_number'),
            'snippet':text,'snippet_sha256':hashlib.sha256(text.encode()).hexdigest(),
            'extraction_method':VERSION,'extensions':{'source_ids':sorted({w['source_id'] for w in words}),
                'semantic_request_sha256':request['request_sha256'],'semantic_request_sha256s':[request['request_sha256']],
                'support_tokens':copy.deepcopy(words),'formal':False}}
        return link,{'evidence_key':key,'original_value':text,'original_unit':unit,'formula':formula,
            'extraction_method':VERSION,'review_status':'pending','confidence':None}
    def write(mat,object_id,path,value,unit,words,formula=None):
        slots=[s for s in request['slots'] if s['object_id']==object_id and (path==s['field_path'] or path.startswith(s['field_path']+'/'))]
        if len(slots)!=1:
            pending(mat,{'field_path':path,'candidate_value':value},'FIELD_OUTSIDE_PLANNED_OBJECT',words);return
        slot=slots[0];allowed=set(request['task']['source_ids']) & set(slot['source_ids']+slot['context_source_ids'])
        if not {w['source_id'] for w in words}<=allowed:
            pending(mat,{'field_path':path,'candidate_value':value},'SOURCE_OUTSIDE_FIELD_SCOPE',words);return
        parent=mat if path=='/custom_material_id' else mat['physical_properties'];field=path.rsplit('/',1)[1]
        old=parent.get(field);link,prov=evidence(mat,path,words,unit,formula)
        if old is not None and old!=value:
            conflict={'field_path':path,'existing_value':old,'candidate_value':value,'unit':unit,'evidence':link,'provenance':prov,
                'reason':'EXISTING_VALUE_CONFLICT','formal':False}
            conflict['request_sha256']=request['request_sha256']
            append_candidate(mat,conflict,request)
            events.append({'record_key':mat['mat_key'],'field_path':path,'status':'CONFLICT'});return
        parent[field]=value
        previous=mat['field_provenance'].get(path)
        if previous:
            prior=next((e for e in staged['evidence_links'] if e['evidence_key']==previous['evidence_key']),None)
            fields=('record_key','paper_key','field_path','asset_key','page','bbox','snippet')
            if prior and all(prior.get(k)==link.get(k) for k in fields) and all(previous.get(k)==prov.get(k) for k in ('original_value','original_unit','formula')):
                history=prior.setdefault('extensions',{}).setdefault('semantic_request_sha256s',[])
                for h in (prior['extensions'].get('semantic_request_sha256'),request['request_sha256']):
                    if h and h not in history:history.append(h)
                link,prov=prior,copy.deepcopy(previous)
        if previous and previous!=prov:
            history=mat.setdefault('extensions',{}).setdefault('raw_material_provenance_history',[])
            item={'field_path':path,'provenance':copy.deepcopy(previous)}
            if item not in history:history.append(item)
        mat['field_provenance'][path]=prov
        if not any(e['evidence_key']==link['evidence_key'] for e in staged['evidence_links']):staged['evidence_links'].append(link)
        events.append({'record_key':mat['mat_key'],'field_path':path,'status':'REUSED' if old is not None else 'WRITTEN'})
        fills.append({'parent_slot_id':slot['slot_id'],'record_key':mat['mat_key'],'field_path':path,'value':value,'unit':unit,
            'source_ids':link['extensions']['source_ids'],'evidence':link,'provenance':prov,'status':'filled','formal':False})
    objects={o['object_id']:o for o in request['objects']};used=set();new_bindings=[]
    new_anchors={}
    for item in response['materials']:
        if item['existing_key'] is None:
            new_anchors.setdefault(tuple(item['identity_token_ids']),[]).append(item['entity_id'])
    for item in response['materials']:
        if item['entity_id'] in entities:raise ValueError('duplicate raw material entity')
        words=selected(item['identity_token_ids']);label=' '.join(w['text'] for w in words).strip(' (),;.')
        if item['object_id'] not in objects:
            pending(None,item,'NEW_MATERIAL_OUTSIDE_PLANNED_OBJECTS',words)
            entities[item['entity_id']]=(None,item['object_id']);continue
        key=item['existing_key']
        identity_slots=[s for s in request['slots'] if s['object_id']==item['object_id'] and s['field_path']=='/custom_material_id']
        owner_slots=identity_slots or [s for s in request['slots'] if s['object_id']==item['object_id']]
        identity_sources=set(request['task']['source_ids']) & {sid for s in owner_slots for sid in s['source_ids']+s['context_source_ids']}
        if not {w['source_id'] for w in words}<=identity_sources or (key is None and not identity_slots):
            pending(None,item,'MATERIAL_IDENTITY_OUTSIDE_SOURCE_SCOPE',words)
            entities[item['entity_id']]=(None,item['object_id']);continue
        if key is None and len(new_anchors[tuple(item['identity_token_ids'])])>1:
            pending(None,item,'DISTINCT_ENTITIES_SHARE_IDENTITY_ANCHOR',words)
            entities[item['entity_id']]=(None,item['object_id']);continue
        if key is not None:
            if key not in objects[item['object_id']]['existing_keys'] or key not in mats:raise ValueError('material identity escaped catalogue')
            if key in used:raise ValueError('distinct source entities cannot merge into same material')
            mat=mats[key]
        else:
            key='mat-source-'+digest([request['document']['sha256'],item['identity_token_ids']])[:20]
            if key in mats:mat=mats[key]
            else:
                from build_phase_c_full10 import mat_record
                mat=mat_record('user10v2-'+request['document']['sha256'][:12],pdf['paper_key'],
                    {'key':'source','label':None,'role':None})
                mat['mat_key']=key;mats[key]=mat;staged['mats'].append(mat)
            new_bindings.append({'object_id':item['object_id'],'mat_key':key})
        if mat['paper_key']!=pdf['paper_key']:raise ValueError('raw material paper ownership conflict')
        used.add(key);entities[item['entity_id']]=(mat,item['object_id'])
        if identity_slots:write(mat,item['object_id'],'/custom_material_id',label,None,words)
    for claim in response['claims']:
        if claim['entity_id'] not in entities:
            words=selected(claim['token_ids'],contiguous=False)
            reason='UNASSIGNED_CONTEXT_FOR_OTHER_TASK' if claim['field_path'] is None else 'MATERIAL_IDENTITY_UNRESOLVED'
            pending(None,claim,reason,words)
            continue
        mat,object_id=entities[claim['entity_id']];words=selected(claim['token_ids'],contiguous=claim['status']=='REPORTED');path=claim['field_path']
        if mat is None:
            pending(None,claim,'MATERIAL_IDENTITY_UNRESOLVED',words);continue
        if claim['status']!='REPORTED' or path is None:
            candidate={**copy.deepcopy(claim),'source_tokens':words,'request_sha256':request['request_sha256'],'formal':False}
            # Retained older responses used a module-local label as though the
            # entire WAKG contract excluded this property.
            if candidate['status']=='OUTSIDE_CONTRACT':candidate['status']='OTHER_PROPERTY'
            append_candidate(mat,candidate,request)
            events.append({'record_key':mat['mat_key'],'field_path':path,'status':candidate['status']});continue
        field=path.rsplit('/',1)[1];unit,scales=FIELDS[field]
        if field=='specific_surface_method':value=' '.join(w['text'] for w in words);formula=None
        else:
            if len(words)!=1 or claim['unit'] not in scales:
                pending(mat,claim,'NONSCALAR_OR_UNSUPPORTED_UNIT',words);continue
            source=next(s for s in request['sources'] if s['source_id']==words[0]['source_id'])
            normalized=re.sub(r'\s+','',source['text']).translate(str.maketrans({'²':'2','³':'3'}))
            allowed_units=['%','percent'] if claim['unit'] in ('%','percent') else [claim['unit']]
            if not any(u in normalized for u in allowed_units):
                pending(mat,claim,'UNIT_NOT_IN_SOURCE',words);continue
            raw=words[0]['text'].strip('(),;.%')
            if not re.fullmatch(r'[+-]?[0-9]+(?:\.[0-9]+)?',raw):
                pending(mat,claim,'NONSCALAR_SOURCE_VALUE',words);continue
            original=float(raw);scale=scales[claim['unit']];value=original*scale
            if not math.isfinite(value) or not math.isclose(value/scale,original,rel_tol=1e-12):raise ValueError('raw material unit conversion not reversible')
            formula=f'value = original * {scale}'
        write(mat,object_id,path,value,claim['unit'],words,formula)
        if fills and fills[-1]['record_key']==mat['mat_key'] and fills[-1]['field_path']==path:fills[-1]['unit']=unit
    return staged,fills,{'events':events,'new_material_bindings':new_bindings,
        'unresolved':response['unresolved'],'formal_acceptance':False}


def run(index,plan,records,directory,allow_call=False):
    """One planned task, normal dispatcher producer/consumer, retained source response."""
    from paper_block_dispatch import dispatch,IMPLEMENTATION_SHA256
    directory=Path(directory);receipt=directory/'result.json';code=digest([VERSION,IMPLEMENTATION_SHA256,
        hashlib.sha256(Path(__file__).read_bytes()).hexdigest()])
    input_hash=digest(records)
    if receipt.exists():
        prior=json.loads(receipt.read_bytes())
        if prior['input_records_sha256']==input_hash and prior['implementation_sha256']==code:
            output=json.loads(Path(prior['output_path']).read_bytes())
            if digest(output)!=prior['output_records_sha256']:raise ValueError('raw material result changed')
            return output,{**prior,'invocation_model_calls':0,'invocation_scientific_writes':0}
    slots={s['slot_id']:s for s in plan['slots']}
    tasks=[t for t in plan['tasks'] if eligible(t,[slots[k] for k in t['slot_ids']])]
    if len(tasks)!=1 or tasks[0]['depends_on']:raise ValueError('bounded raw material pass requires one dependency-free aggregate task')
    selected=copy.deepcopy(plan);selected.pop('plan_sha256');selected['parent_plan_sha256']=plan['plan_sha256']
    selected['tasks']=tasks;selected['slots']=[slots[k] for k in tasks[0]['slot_ids']]
    selected['plan_sha256']=digest(selected)
    draft=dispatch(index,selected,records,directory/'fill',directory,material_text=True,allow_material_call=allow_call)
    if any(t['status']=='failed' for t in draft['tasks'].values()):raise ValueError('raw material adapter failed: '+str(draft['tasks']))
    output=json.loads((directory/'fill/generated-records.json').read_bytes());out_hash=digest(output)
    output_path=directory/'objects'/(out_hash[:24]+'.json')
    if output_path.exists():
        if digest(json.loads(output_path.read_bytes()))!=out_hash:raise ValueError('raw material content-address collision')
    else:atomic_json(output_path,output)
    reports=draft['material_reports'];writes=sum(e['status']=='WRITTEN' for r in reports for e in r['events'])
    bindings=[b for r in reports for b in r['new_material_bindings']]
    effective=copy.deepcopy(plan)
    if bindings:
        for b in bindings:
            o=next(o for o in effective['objects'] if o['object_id']==b['object_id'])
            if b['mat_key'] not in o['existing_keys']:o['existing_keys'].append(b['mat_key'])
        effective.pop('plan_sha256');effective['parent_plan_sha256']=plan['plan_sha256'];effective['plan_sha256']=digest(effective)
    atomic_json(directory/'compiled-plan.json',effective)
    changes=[{'record_key':s['record_key'],'field_path':s['field_path'],'value':s['value'],
        'evidence_key':s['evidence']['evidence_key'],'source_ids':s['source_ids'],'task_ids':[tasks[0]['task_id']]}
        for s in draft['states'].values() if s['status']=='filled' and s.get('record_key')]
    report={'status':'RAW_MATERIAL_SOURCE_SUBSET_NOT_ACCEPTED','input_records_sha256':input_hash,
        'output_records_sha256':out_hash,'output_path':str(output_path),'implementation_sha256':code,
        'reports':reports,'changes':changes,'compiled_plan_path':str(directory/'compiled-plan.json'),
        'invocation_model_calls':draft['model_calls'],'invocation_scientific_writes':writes,
        'scientific_writes':writes,'formal_acceptance':False}
    if receipt.exists():atomic_json(directory/'history'/(digest(json.loads(receipt.read_bytes()))[:24]+'.json'),json.loads(receipt.read_bytes()))
    atomic_json(receipt,report)
    return output,report
