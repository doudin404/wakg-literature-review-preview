"""Source-scoped preparation/curing and independent physical specimen owners."""
import copy
import hashlib
import json
import time
from pathlib import Path
from source_specimen_variants import digest, readable_word
from source_quantity_producer import invoke, obj, number, atomic_json
from formulation_source_plan import index_spans
from relation_curing_stages import normalized_routes


def source_objects(records,binding,paper_key):
    """Resolve frozen supplement objects by content identity, never table position."""
    from supplement_docx import inspect_docx
    assets=[a for a in records['assets'] if a['kind']=='supplement' and a['paper_key']==paper_key]
    if len(assets)!=1:raise ValueError('source supplement missing or ambiguous')
    inventory=inspect_docx(Path(assets[0]['relative_path']))
    tables=[t for t in inventory['tables'] if t['rows_sha256']==binding['source_table_sha256']]
    figures=[f for f in inventory['figures'] if f['label']==binding['figure']]
    if len(tables)!=1 or len(figures)!=1:raise ValueError('source objects missing or ambiguous')
    return tables[0],figures[0]


def prepare(records,binding):
    plan=binding['source_plan']
    if plan.get('schema_version')!=3:raise ValueError('indexed specimen source plan required')
    pdfs=[a for a in records['assets'] if a['kind']=='pdf' and a['sha256']==binding['pdf_sha256']]
    if len(pdfs)!=1 or hashlib.sha256(Path(pdfs[0]['relative_path']).read_bytes()).hexdigest()!=binding['pdf_sha256']:
        raise ValueError('context source PDF changed')
    groups=[f for f in plan['figures'] if f['label']==binding['figure']]
    if len(groups)!=1:raise ValueError('context source group ambiguous')
    targets=[]
    for a in groups[0]['assignments']:
        for specimen in a['specimens']:
            targets.append({'target_id':'target-'+digest([pdfs[0]['paper_key'],a['source_test_id'],specimen])[:20],
                'source_test_id':a['source_test_id'],'specimen':specimen,
                'identity_spans':copy.deepcopy(a['source_spans']),
                'reference_row':copy.deepcopy(a.get('reference_row'))})
    if not targets or len({t['target_id'] for t in targets})!=len(targets):
        raise ValueError('context source specimen scope duplicate or empty')
    blocks=plan['source_statements'];spans=index_spans(blocks,binding['pdf_sha256'])
    tokens=[]
    for block in blocks:
        for word in block['locators']:
            value=number(word['token'])
            if value is not None:
                tokens.append({'token_id':'num-'+digest([binding['pdf_sha256'],word])[:20],
                               'value':value,'locator':word,'block_id':block['block_id']})
    request={'binding_sha256':digest(binding),'pdf_sha256':binding['pdf_sha256'],
        'pdf_asset_key':pdfs[0]['asset_key'],'paper_key':pdfs[0]['paper_key'],
        'source_statements':blocks,'span_index':spans,'tokens':tokens,'targets':targets}
    request['request_sha256']=digest(request)
    return request


def schema(request):
    s={'type':'string'};nullable={'type':['string','null']}
    spans={'type':'array','items':{'type':'string','enum':[v['span_id'] for v in request['span_index']]}}
    targets={'type':'array','items':{'type':'string','enum':[t['target_id'] for t in request['targets']]}}
    prop=obj({'name':{'type':'string','enum':['duration','temperature','relative_humidity']},
        'numeric_token_id':{'type':['string','null'],'enum':[t['token_id'] for t in request['tokens']]+[None]},
        'unit':{'type':'string','enum':['s','min','h','d','degC','%']},
        'relation':{'type':['string','null'],'enum':['=','>','>=','<','<=',None]},
        'missing_reason':{'type':['string','null'],'enum':['NOT_REPORTED','NOT_DETERMINED',None]},
        'source_span_ids':spans})
    stage=obj({'stage_id':s,'after_stage_id':nullable,
        'kind':{'type':'string','enum':['PREPARATION','INITIAL_CURING','CURING','EXPOSURE','STORAGE']},
        'operation':{'type':'string','enum':['mixing','forming','curing','storage','exposure','other']},
        'method_zh':s,'start_event':{'type':'string','enum':['forming_complete','previous_stage_end','unknown']},
        'end_event':{'type':'string','enum':['demoulding','testing','next_stage','unknown']},
        'source_span_ids':spans,'properties':{'type':'array','items':prop}})
    route=obj({'route_id':s,'target_ids':targets,'excluded_target_ids':targets,'scope_span_ids':spans,
        'reason':s,'stages':{'type':'array','items':stage}})
    return obj({'request_sha256':s,'routes':{'type':'array','items':route},
        'unresolved_targets':{'type':'array','items':obj({'target_id':s,'reason':s})}})


PROMPT="""Read-only preparation/curing stage extraction from supplied indexed sources.
Do not run tools, read files or browse. Select source span IDs and numeric token IDs;
do not transcribe source prose or invent values. Existing targets are source specimens,
NOT reference-table rows. A missing reference row must not suppress a known specimen.
Assign each target once to a source-supported route or unresolved_targets. Preserve
experimental series and specimen exclusions; do not apply one passage to all paper rows.
Extract preparation/mixing/forming and curing as ordered stages, using explicit
after_stage_id. Reuse same-document condition definitions only when supported, citing
both the application and definition spans. Distinguish an initial mould stage ending
at demoulding from subsequent curing until testing. Never treat demoulding elapsed
time as total age or invent the unknown until-testing duration. Every stage requires
duration, including null with missing_reason; STAGE_START is imposed by the script.
Keep humidity/temperature comparisons such as > rather than replacing them with =.
Do not inherit unreported conditions into earlier stages. Do not infer test ages from
stage totals. Properties can only use supplied numeric tokens inside cited spans.
Use method_zh for concise Chinese source-supported process descriptions; retain
specimen dimensions in forming description when explicit, with source evidence.
Only stage semantics, not recipe masses or performance values. Return one grouped JSON.
"""


def compile_plan(request,response):
    if digest({k:v for k,v in request.items() if k!='request_sha256'})!=request['request_sha256'] or response['request_sha256']!=request['request_sha256']:
        raise ValueError('context response/request source changed')
    if request['span_index']!=index_spans(request['source_statements'],request['pdf_sha256']):
        raise ValueError('context range index changed')
    spans={s['span_id']:s for s in request['span_index']}
    tokens={t['token_id']:t for t in request['tokens']}
    targets={t['target_id'] for t in request['targets']}
    def facts(ids):
        if not ids or not set(ids)<=set(spans):raise ValueError('context span references invalid')
        return ids
    entities={};metadata={};covered=[]
    for route in response['routes']:
        if (not route['target_ids'] or not set(route['target_ids'])<=targets or
                set(route['target_ids']) & set(route['excluded_target_ids']) or
                not set(route['excluded_target_ids'])<=targets):
            raise ValueError('context inclusion/exclusion scope invalid')
        facts(route['scope_span_ids']);covered.extend(route['target_ids'])
        stages=[]
        for stage in route['stages']:
            fact_ids=facts(stage['source_span_ids'])
            properties={}
            for p in stage['properties']:
                if p['name'] in properties:raise ValueError('duplicate stage property')
                ids=facts(p['source_span_ids']);tid=p['numeric_token_id']
                value=None if tid is None else tokens[tid]['value']
                prop={'value':value,'unit':p['unit'],'fact_ids':ids}
                if p['name']=='duration':prop['time_origin']='STAGE_START'
                if value is None:
                    if p['relation'] is not None or not p['missing_reason']:
                        raise ValueError('unknown stage quantity needs reason and no comparison')
                    prop['missing_reason']=p['missing_reason']
                else:
                    if p['missing_reason'] is not None or tokens[tid]['locator'] not in [w for i in ids for w in spans[i]['locators']]:
                        raise ValueError('stage numeric token outside its evidence')
                    prop['relation']=p['relation'] or '='
                    if prop['relation']!='=':
                        actual={w['token'].strip(' ,;()').replace('≥','>=').replace('≤','<=') for i in ids for w in spans[i]['locators']}
                        if prop['relation'] not in actual:raise ValueError('stage comparison lacks source token')
                properties[p['name']]=prop
            stages.append({'stage_id':stage['stage_id'],'after_stage_id':stage['after_stage_id'],
                           'kind':stage['kind'],'fact_ids':fact_ids,'properties':properties})
        if route['route_id'] in entities:raise ValueError('duplicate context route')
        entities[route['route_id']]={'kind':'curing_route','stages':stages}
        metadata[route['route_id']]=copy.deepcopy(route)
    unresolved=response['unresolved_targets']
    covered.extend(u['target_id'] for u in unresolved)
    if set(covered)!=targets or len(covered)!=len(set(covered)) or any(not u['reason'] for u in unresolved):
        raise ValueError('context targets missing or multiply assigned')
    routes=normalized_routes(entities,spans)
    return {'schema_version':1,'binding_sha256':request['binding_sha256'],'request_sha256':request['request_sha256'],
            'request':request,'response':copy.deepcopy(response),'normalized_routes':routes,'route_metadata':metadata,
            'unresolved_targets':unresolved,'publication_allowed':False}


def visible_request(request):
    blocks={b['block_id']:b for b in request['source_statements']}
    return {'request_sha256':request['request_sha256'],
        'blocks':[{'block_id':k,'words':[w['token'] for w in b['locators']]} for k,b in blocks.items()],
        'spans':[{k:s[k] for k in ('span_id','block_id','start','end')} for s in request['span_index']],
        'tokens':[{'token_id':t['token_id'],'value':t['value'],'block_id':t['block_id'],
                   'word_index':blocks[t['block_id']]['locators'].index(t['locator'])} for t in request['tokens']],
        'targets':[{'target_id':t['target_id'],'source_test_id':t['source_test_id'],'specimen':t['specimen'],
                    'identity_span_ids':[s['span_id'] for s in t['identity_spans']]} for t in request['targets']]}


def produce(records,binding,directory):
    request=prepare(records,binding);directory=Path(directory);started=time.monotonic()
    visible=visible_request(request)
    size=len(json.dumps(visible,ensure_ascii=False).encode())
    if size>100000:raise ValueError('stage visible request exceeds 100000 byte budget')
    response=invoke(request,directory,schema=schema(request),visible=visible,prompt_text=PROMPT)
    plan=compile_plan(request,response)
    atomic_json(directory/'context-plan.json',plan)
    atomic_json(directory/'stage.json',{'elapsed_seconds':time.monotonic()-started,'plan_sha256':digest(plan),'publication_allowed':False})
    return plan


def enrich(records,binding,context_plan=None,operation_plan=None):
    if context_plan is None:raise ValueError('source stage selection plan required')
    if context_plan['binding_sha256']!=digest(binding):raise ValueError('context plan binding changed')
    request=context_plan['request']
    if compile_plan(request,context_plan['response'])!=context_plan:raise ValueError('context plan content changed')
    # Rebuild current source index and target scope, not a stale hand-authored route.
    if prepare(records,binding)!=request:raise ValueError('context source dependencies changed')
    expected_keys={'mix-specimen-'+digest([request['paper_key'],t['source_test_id'],t['specimen']])[:24] for t in request['targets']}
    existing=[m for m in records['mixes'] if m['mix_key'] in expected_keys]
    if existing:
        if len(existing)!=len(expected_keys):raise ValueError('partial context owner set requires reconciliation')
        replay=copy.deepcopy(records)
        replay['mixes']=[m for m in replay['mixes'] if m['mix_key'] not in expected_keys]
        replay['evidence_links']=[e for e in replay['evidence_links'] if e.get('record_key') not in expected_keys]
        replay['extensions']['source_context_owners']=[o for o in replay['extensions']['source_context_owners'] if o['mix_key'] not in expected_keys]
        enrich(replay,binding,context_plan,operation_plan)
        def own(m):
            return {'modules':{k:m['modules'][k] for k in ('identity_source_specimen','materials','mixing_curing')},
                'owner':m['extensions']['source_context_owner'],
                'provenance':{k:v for k,v in m['field_provenance'].items() if not k.startswith(('/modules/performance/','/modules/characterizations/'))}}
        generated={m['mix_key']:m for m in replay['mixes'] if m['mix_key'] in expected_keys}
        if any(own(m)!=own(generated[m['mix_key']]) for m in existing):raise ValueError('context reentry differs from scoped output')
        def links(r):return sorted((e for e in r['evidence_links'] if e.get('record_key') in expected_keys and e.get('extraction_method')=='indexed-source-context-v1'),key=lambda e:e['evidence_key'])
        if links(records)!=links(replay):raise ValueError('context reentry evidence changed')
        def registry(r):return sorted((o for o in r['extensions']['source_context_owners'] if o['mix_key'] in expected_keys),key=lambda o:o['mix_key'])
        if registry(records)!=registry(replay):raise ValueError('context reentry registry changed')
        return
    if operation_plan is not None:
        from source_operation_scope import apply
        context_plan=apply(context_plan,operation_plan)
    staged=copy.deepcopy(records);spans={s['span_id']:s for s in request['span_index']}
    pdf_binding={'pdf_asset_key':request['pdf_asset_key'],'pdf_sha256':request['pdf_sha256']}
    targets={t['target_id']:t for t in request['targets']}
    route_by_target={tid:(route,context_plan['route_metadata'][route['route_id']])
        for route in context_plan['normalized_routes']
        for tid in context_plan['route_metadata'][route['route_id']]['target_ids']}
    cache={}
    def verify_word(word):
        key=digest(word)
        if key not in cache:cache[key]=readable_word(staged,pdf_binding,word)
        return cache[key]
    for route in context_plan['normalized_routes']:
        for stage in route['stages']:
            for word in stage.get('operation_scope',[]):verify_word(word)
    for route in context_plan['route_metadata'].values():
        for stage in route['stages']:
            for prop in stage['properties']:
                if prop['numeric_token_id'] is not None:
                    verify_word(next(t['locator'] for t in request['tokens'] if t['token_id']==prop['numeric_token_id']))
    def evidence(mix,path,locators,fact_ids,original_unit=None,transformation=None):
        if not locators:raise ValueError('context evidence empty')
        first=locators[0]
        key='ev-context-'+digest([mix['mix_key'],path,locators])[:24]
        displayed=[w for w in locators if w['page']==first['page']]
        snippet=' '.join(w['token'] for w in displayed)
        box=[min(w['bbox'][0] for w in displayed),min(w['bbox'][1] for w in displayed),
             max(w['bbox'][2] for w in displayed),max(w['bbox'][3] for w in displayed)]
        staged['evidence_links'].append({'evidence_key':key,'asset_key':request['pdf_asset_key'],
            'paper_key':request['paper_key'],'record_type':'mix','record_key':mix['mix_key'],'field_path':path,
            'page':first['page'],'bbox':box,'snippet':snippet,'snippet_sha256':hashlib.sha256(snippet.encode()).hexdigest(),
            'section':None,'table_number':None,'figure_number':None,'source_locator':{'coordinate_space':'pdf_points'},
            'extraction_method':'indexed-source-context-v1','confidence':None,
            'extensions':{'source_spans':[copy.deepcopy(spans[i]) for i in fact_ids],'support_tokens':locators}})
        mix['field_provenance'][path]={'evidence_key':key,'original_value':snippet,'original_unit':original_unit,
            'transformation':transformation,'review_status':'pending_source_path_review','extraction_method':'indexed-source-context-v1'}
    for target in targets.values():
        key='mix-specimen-'+digest([request['paper_key'],target['source_test_id'],target['specimen']])[:24]
        if any(m['mix_key']==key for m in staged['mixes']):
            raise ValueError('source context owner already exists; explicit reconciliation required')
        source_words=[w for span in target['identity_spans'] for w in span['locators']]
        identity=next((w for w in source_words if w['token'].strip(' ,.;()')==target['source_test_id']),None)
        kind=next((w for w in source_words if w['token'].strip(' ,.;()').lower()==target['specimen']),None)
        if identity is None or kind is None:raise ValueError('source specimen identity/type lacks source token')
        identity,kind=verify_word(identity),verify_word(kind)
        literature=next(m['modules']['identity_source_specimen']['literature_source'] for m in staged['mixes'] if m['paper_key']==request['paper_key'])
        mix={'schema_version':staged['mixes'][0]['schema_version'],'mix_key':key,'paper_key':request['paper_key'],'mix_family_key':None,
            'modules':{'identity_source_specimen':{'custom_test_id':target['source_test_id'],'specimen_type':target['specimen'],
                'specimens':[],'literature_source':copy.deepcopy(literature),'extensions':{}},
              'materials':{'mat_refs':[],'solid_materials':[],'activators':[],'activator_total_mass_g':None,
                'fine_aggregate':None,'coarse_aggregate':None,'reported_mass_basis':None,'platform_ratios':None,
                'conversion':None,'review_status':'pending','extensions':{'reported_parameters':[]}},
              'mixing_curing':{'mixing':None,'forming':None,'demoulding':None,'curing_stages':[],
                               'curing_route':None,'age_origin':None,'extensions':{}},
              'performance':[],'characterizations':[]},'field_provenance':{},
              'extensions':{'source_context_owner':{'target_id':target['target_id'],'context_plan_sha256':digest(context_plan),
                  'reference_row':target['reference_row'],'recipe_status':'not_extracted_for_this_specimen'}}}
        evidence(mix,'/modules/identity_source_specimen/custom_test_id',[identity],[])
        evidence(mix,'/modules/identity_source_specimen/specimen_type',[kind],[])
        if target['target_id'] in route_by_target:
            route,meta=route_by_target[target['target_id']];curing=mix['modules']['mixing_curing']
            curing['extensions']['source_route']=copy.deepcopy(route)
            for stage,description in zip(route['stages'],meta['stages']):
                props=stage['properties'];duration=props['duration']
                raw_spans=[spans[i] for i in stage['fact_ids']]
                if stage['kind']=='PREPARATION':
                    slot=description['operation'] if description['operation'] in ('mixing','forming') else 'mixing'
                    if curing[slot] is None:curing[slot]={'method':'','extensions':{'source_stages':[]}}
                    curing[slot]['method']='；'.join(filter(None,[curing[slot]['method'],description['method_zh']]))
                    curing[slot]['extensions']['source_stages'].append(copy.deepcopy(stage))
                    path='/modules/mixing_curing/'+slot
                    all_facts=list(dict.fromkeys(i for s in curing[slot]['extensions']['source_stages'] for i in s['fact_ids']))
                    raw_spans=[spans[i] for i in all_facts]
                else:
                    item={'method':description['method_zh'],'temperature_C':props.get('temperature',{}).get('value'),
                          'humidity_percent':props.get('relative_humidity',{}).get('value'),'duration_seconds':duration['value'],
                          'extensions':{'stage_id':stage['stage_id'],'stage_start':description['start_event'],
                              'stage_end':description['end_event'],'humidity_relation':props.get('relative_humidity',{}).get('relation'),
                              'duration_label':'until testing' if description['end_event']=='testing' else None}}
                    index=len(curing['curing_stages']);curing['curing_stages'].append(item)
                    path='/modules/mixing_curing/curing_stages/'+str(index)
                    for prop_name,field in (('duration','duration_seconds'),('temperature','temperature_C'),('relative_humidity','humidity_percent')):
                        p=props.get(prop_name)
                        if not p or p['value'] is None:continue
                        original=next(x for x in description['properties'] if x['name']==prop_name)
                        token=next(t['locator'] for t in request['tokens'] if t['token_id']==original['numeric_token_id'])
                        receipt=copy.deepcopy(p['transformation']);receipt['formal']=False
                        evidence(mix,path+'/'+field,[verify_word(token)],p['fact_ids'],p['source']['unit'],receipt)
                        if prop_name=='relative_humidity' and p.get('relation','=')!='=':
                            operator=next(w for i in p['fact_ids'] for w in spans[i]['locators']
                                if w['token'].strip(' ,;()').replace('≥','>=').replace('≤','<=')==p['relation'])
                            evidence(mix,path+'/extensions/humidity_relation',[verify_word(operator)],p['fact_ids'])
                    if description['end_event']=='demoulding':
                        curing['demoulding']={'method':'该阶段结束后脱模','time_after_mixing_seconds':None,
                            'extensions':{'time_origin':'STAGE_START','elapsed_seconds':duration['value']}}
                        evidence(mix,'/modules/mixing_curing/demoulding',[w for s in raw_spans for w in s['locators']],stage['fact_ids'])
                evidence(mix,path,[w for s in raw_spans for w in s['locators']],list(dict.fromkeys(s['span_id'] for s in raw_spans)))
        else:
            mix['modules']['mixing_curing']['extensions']['missing_reason']=next(u['reason'] for u in context_plan['unresolved_targets'] if u['target_id']==target['target_id'])
        staged['mixes'].append(mix)
        staged.setdefault('extensions',{}).setdefault('source_context_owners',[]).append({
            'custom_test_id':target['source_test_id'],'specimen':target['specimen'],'mix_key':key,
            'context_plan_sha256':digest(context_plan)})
    records.clear();records.update(staged)
