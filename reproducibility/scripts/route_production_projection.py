"""Indexed source refinement and atomic projection into existing MIX process fields."""
import argparse,copy,hashlib,json,time
from pathlib import Path
import fitz
from source_relation_graph import validate
from relation_curing_stages import normalized_routes
from source_quantity_producer import invoke,obj,number,atomic_json
from source_specimen_variants import digest
from source_mix_record import empty_source_mix
from formulation_source_plan import index_spans
from consume_curing_relations import identity_locations


def prepare(records,graph,packet,pdf):
    validate(graph,packet,Path(pdf))
    assets=[a for a in records['assets'] if a['kind']=='pdf' and a['sha256']==packet['pdf_sha256']]
    if len(assets)!=1:raise ValueError('foreign production paper')
    paper=assets[0]['paper_key'];entities={e['entity_id']:e for e in graph['entities']}
    anchors={b['source_id']:b for b in packet['source_blocks']}
    pages={anchors[f['source_id']]['page'] for f in graph['facts']}
    links={e['evidence_key']:e for e in records['evidence_links']};targets=[];observations=[];extra_blocks=set()
    label_counts={}
    for edge in graph['relations']:
        if edge['kind']=='curing_route':
            label=entities[edge['subject_id']]['source_label'];label_counts[label]=label_counts.get(label,0)+1
    with fitz.open(pdf) as document:
        # One adjacent page admits a method section continued across the page boundary.
        pages|={p+1 for p in list(pages) if p<len(document)}
        for edge in graph['relations']:
            if edge['kind']!='curing_route':continue
            label=entities[edge['subject_id']]['source_label']
            matches=[m for m in records['mixes'] if m['paper_key']==paper and m['modules']['identity_source_specimen']['custom_test_id']==label]
            independent=len(matches)!=1 or label_counts[label]>1
            mix=(empty_source_mix(paper,'mix-route-'+digest([paper,edge['subject_id'],edge['target_id']])[:24],label,None,None)
                 if independent else matches[0])
            proof=identity_locations(document,label)
            mat_link=links.get(mix.get('field_provenance',{}).get('/modules/materials',{}).get('evidence_key'),{})
            if mat_link.get('page'):proof=[p for p in proof if p[0]['page']==mat_link['page']]
            if not proof:raise ValueError('binder label missing from source')
            pages|={p[0]['page'] for p in proof}
            target={'target_id':'target-'+digest([paper,mix['mix_key'],edge['subject_id']])[:20],
                'mix_key':mix['mix_key'],'source_label':label,'route_id':edge['target_id'],
                'subject_id':edge['subject_id'],'identity_options':[
                    {'location_id':'loc-'+digest(w)[:20],'words':w} for w in proof]}
            targets.append(target)
            if independent:target.update(update_mode='independent',reference_mix_keys=[m['mix_key'] for m in matches])
            for index,row in enumerate(mix['modules']['performance']):
                path='/modules/performance/'+str(index)+'/value'
                link=links.get(mix['field_provenance'].get(path,{}).get('evidence_key'),{})
                raw=str(row.get('extensions',{}).get('original_value',row.get('value')))
                valid=False
                if link.get('asset_key')==assets[0]['asset_key'] and link.get('page') and link.get('bbox'):
                    words=document[link['page']-1].get_text('words',clip=fitz.Rect(link['bbox'])+(-1,-1,1,1))
                    valid=any(w[4].strip(' ,;')==raw for w in words)
                    for path,provenance in mix['field_provenance'].items():
                        if not path.startswith('/modules/performance/'+str(index)+'/'):continue
                        source=links.get(provenance.get('evidence_key'),{})
                        if not source.get('bbox') or not source.get('page'):continue
                        rect=fitz.Rect(source['bbox'])
                        extra_blocks.update(a['source_id'] for a in packet['source_blocks'] if a['page']==source['page'] and fitz.Rect(a['bbox']).intersects(rect))
                observations.append({'observation_id':'obs-'+digest([mix['mix_key'],index])[:20],
                    'target_id':target['target_id'],'index':index,'name':row['name'],'original_value':raw,
                    'unit':row['unit'],'value_source_verified':valid,'value_evidence':link})
        blocks=[]
        for anchor in packet['source_blocks']:
            if anchor['page'] not in pages and anchor['source_id'] not in extra_blocks:continue
            words=[{'page':anchor['page'],'bbox':list(w[:4]),'token':w[4]} for w in document[anchor['page']-1].get_text('words',sort=False)
                   if w[5]==anchor['block_number']]
            if words:blocks.append({'block_id':anchor['source_id'],'locators':words})
    spans=index_spans(blocks,packet['pdf_sha256']);tokens=[]
    for b in blocks:
        for i,w in enumerate(b['locators']):
            value=number(w['token'])
            if value is not None:tokens.append({'token_id':'num-'+digest(w)[:20],'value':value,'block_id':b['block_id'],'word_index':i,'locator':w})
    req={'records_sha256':digest(records),'graph':graph,'packet_sha256':packet['packet_sha256'],
        'pdf_asset':assets[0],'blocks':blocks,'spans':spans,'tokens':tokens,'targets':targets,'observations':observations}
    req['request_sha256']=digest(req)
    return req


def visible(req):
    return {'request_sha256':req['request_sha256'],
        'blocks':[{'block_id':b['block_id'],'words':[w['token'] for w in b['locators']]} for b in req['blocks']],
        'spans':[{k:s[k] for k in ('span_id','block_id','start','end')} for s in req['spans']],
        'numbers':[{k:n[k] for k in ('token_id','value','block_id','word_index')} for n in req['tokens']],
        'routes':[{'route_id':e['entity_id'],'source_label':e['source_label'],
                   'stages':e['stages']} for e in req['graph']['entities'] if e['kind']=='curing_route'],
        'targets':[{k:t[k] for k in ('target_id','source_label','route_id')} | {'identity_options':[
            {'location_id':o['location_id'],'source_text':' '.join(w['token'] for w in o['words'])}
            for o in t['identity_options']]} for t in req['targets']],
        'observations':[{k:o[k] for k in ('observation_id','target_id','name','original_value','unit','value_source_verified')}
                        for o in req['observations']]}


def schema(req):
    string={'type':'string'};strings={'type':'array','items':string}
    spans={'type':'array','items':{'type':'string','enum':[s['span_id'] for s in req['spans']]}}
    limit=obj({'kind':{'type':'string','enum':['passed_sieve','retained_sieve','diameter','height']},
        'number_id':{'type':'string','enum':[n['token_id'] for n in req['tokens']]},
        'unit':{'type':'string','enum':['mm','um']},'source_span_ids':spans})
    process=obj({'process_id':string,'role':{'type':'string','enum':['FORMING','PREPARATION','TEST_PREPARATION']},
        'route_ids':strings,'excluded_route_ids':strings,'after_stage_id':{'type':['string','null']},
        'method_zh':string,'physical_state':{'type':'string','enum':['paste','pulverized_paste','powder','suspension','solution','unknown']},
        'limits':{'type':'array','items':limit},'source_span_ids':spans})
    test=obj({'test_id':string,'test_kind':string,'tested_object_zh':string,'route_ids':strings,
        'process_ids':strings,'source_span_ids':spans,'missing_preparation':{'type':['string','null']}})
    return obj({'request_sha256':{'type':'string','enum':[req['request_sha256']]},'bindings':{'type':'array','items':obj({
        'target_id':string,'route_id':string,'identity_location_id':string,'source_span_ids':spans,
        'specimen_type':{'type':'string','enum':['paste','mortar','concrete','powder']}})},
        'processes':{'type':'array','items':process},
        'qualitative_conditions':{'type':'array','items':obj({'route_ids':strings,'stage_ids':strings,
            'quantity':{'type':'string','enum':['pressure']},'description_zh':string,'source_span_ids':spans})},
        'tests':{'type':'array','items':test},
        'observations':{'type':'array','items':obj({'observation_id':string,'test_id':{'type':['string','null']},
            'source_span_ids':spans,'reason':string})}})


PROMPT='''Use supplied indexed sources only, no tools. Refine existing source-bound routes
for production MIX fields. Do not redo route quantities or infer total age. Bind each
target to its exact table identity and existing route using source span IDs. Report
specimen type from source, not from similar composition. Select source numeric IDs for
sieve passed/retained openings and specimen diameter/height, preserving original units.
No hardcoded thresholds; retained is the lower particle-size bound and passed is upper.
Give forming, post-curing preparation, and TEST-specific preparation separate entries,
with applicable/excluded route IDs and after-stage boundary. Each test branch needs its
own source-supported physical state and process IDs; never apply dissolution/boiling or
powder sieving to every test. Preserve atmospheric pressure qualitatively without a
numeric pressure. Keep unknown times unknown; control label zero is not total storage.
Use concise Chinese method and object descriptions. Bind an existing observation only
if its source value is verified and source table/method identifies its object and route;
otherwise test_id=null with specific missing source reason. Cite both table and method
spans when binding. Do not create numeric observations or copy values. Cover every target
and every observation exactly once. Return a single grouped JSON, no new scientific fields.'''


def verify_transport(req,response,directory):
    directory=Path(directory)
    if json.loads((directory/'request.json').read_bytes())!=req or json.loads((directory/'response.json').read_bytes())!=response:
        raise ValueError('transport input/output differs')
    prompt=PROMPT+'\nSOURCE DATA:\n'+json.dumps(visible(req),ensure_ascii=False)
    if (directory/'prompt.txt').read_text(encoding='utf8')!=prompt:raise ValueError('transport prompt differs')
    events=[json.loads(line) for line in (directory/'events.jsonl').read_text(encoding='utf8').splitlines() if line.startswith('{')]
    messages=[e['item']['text'] for e in events if e.get('item',{}).get('type')=='agent_message']
    usage=json.loads((directory/'usage.json').read_bytes())
    if len(messages)!=1 or json.loads(messages[0])!=response or usage['returncode']!=0 or usage['model_turns']!=1:
        raise ValueError('transport completion evidence differs')
    if any(e.get('item',{}).get('type') in ('command_execution','file_change','mcp_tool_call','web_search') for e in events):raise ValueError('transport exceeded source-only scope')
    return {'request_sha256':digest(req),'response_sha256':digest(response),'prompt_sha256':hashlib.sha256(prompt.encode()).hexdigest(),
        'events_sha256':hashlib.sha256((directory/'events.jsonl').read_bytes()).hexdigest(),
        'echo_matches':response['request_sha256']==req['request_sha256']}


def checked(req,response,transport_directory=None):
    if digest({k:v for k,v in req.items() if k!='request_sha256'})!=req['request_sha256']:
        raise ValueError('projection request changed')
    if response['request_sha256']!=req['request_sha256']:
        if transport_directory is None:raise ValueError('projection request changed')
        verify_transport(req,response,transport_directory)
    spans={s['span_id']:s for s in req['spans']};numbers={n['token_id']:n for n in req['tokens']}
    routes={r['route_id']:r for r in normalized_routes({e['entity_id']:e for e in req['graph']['entities']},{f['fact_id']:f for f in req['graph']['facts']})}
    targets={t['target_id']:t for t in req['targets']}
    def source(ids):
        if not ids or not set(ids)<=set(spans):raise ValueError('missing projection source')
        return [w for i in ids for w in spans[i]['locators']]
    def unique(rows,key,expected=None):
        result={r[key]:r for r in rows}
        if len(result)!=len(rows) or (expected is not None and set(result)!=set(expected)):raise ValueError('projection coverage duplicate or incomplete')
        return result
    bindings=unique(response['bindings'],'target_id',targets)
    for tid,b in bindings.items():
        t=targets[tid];words=source(b['source_span_ids'])
        if b['route_id']!=t['route_id']:raise ValueError('route disagrees with source graph scope')
        options={o['location_id']:o for o in t['identity_options']}
        location=options.get(b['identity_location_id'],{}).get('words',[])
        all_words=[w for block in req['blocks'] for w in block['locators']]
        if (not location or ''.join(w['token'] for w in location)!=''.join(t['source_label'].split()) or
                not all(w in all_words for w in location) or not any(w in words for w in location)):
            raise ValueError('table identity outside selected evidence')
        type_words=list(words)
        for p in response['processes']:
            if p['role']=='FORMING' and b['route_id'] in p['route_ids'] and b['route_id'] not in p['excluded_route_ids'] and p['physical_state']==b['specimen_type']:
                type_words.extend(source(p['source_span_ids']))
        if not any(w['token'].lower().strip(' ,.;')==b['specimen_type'] for w in type_words):raise ValueError('specimen type source missing')
    processes=unique(response['processes'],'process_id')
    for p in processes.values():
        source(p['source_span_ids'])
        if not p['route_ids'] or not set(p['route_ids']+p['excluded_route_ids'])<=set(routes) or set(p['route_ids'])&set(p['excluded_route_ids']):raise ValueError('process scope conflict')
        for rid in p['route_ids']:
            if p['after_stage_id'] is not None and p['after_stage_id'] not in {s['stage_id'] for s in routes[rid]['stages']}:raise ValueError('process boundary missing')
        bounds={}
        for limit in p['limits']:
            n=numbers[limit['number_id']]
            source(limit['source_span_ids'])
            # The selected numeric token is the direct locator. A companion context
            # sentence may be adjacent, but cannot belong to another paragraph.
            if n['block_id'] not in {spans[i]['block_id'] for i in limit['source_span_ids']} or n['value']<0:raise ValueError('process quantity outside source')
            source_words=next(b['locators'] for b in req['blocks'] if b['block_id']==n['block_id'])
            at=source_words.index(n['locator'])
            following={w['token'].replace('μ','u').replace('µ','u').strip(' ()[],;.') for w in source_words[at+1:at+4]}
            if limit['unit'] not in following:raise ValueError('process unit not supported next to numeric token')
            value=n['value']*(1000 if limit['unit']=='mm' else 1)
            if limit['kind'] in bounds:raise ValueError('duplicate process bound')
            bounds[limit['kind']]=value
        if bounds.get('retained_sieve',0)>bounds.get('passed_sieve',float('inf')):raise ValueError('sieving interval reversed')
    for q in response['qualitative_conditions']:
        source(q['source_span_ids'])
        if not q['route_ids'] or not set(q['route_ids'])<=set(routes):raise ValueError('qualitative scope invalid')
        stage_sets=[{s['stage_id'] for s in routes[r]['stages']} for r in q['route_ids']]
        if not q['stage_ids'] or not set(q['stage_ids'])<=set.union(*stage_sets) or any(not set(q['stage_ids']) & s for s in stage_sets):raise ValueError('qualitative stage absent')
    tests=unique(response['tests'],'test_id')
    for t in tests.values():
        source(t['source_span_ids'])
        if not set(t['route_ids'])<=set(routes) or not set(t['process_ids'])<=set(processes):raise ValueError('test scope invalid')
    observations=unique(response['observations'],'observation_id',{o['observation_id'] for o in req['observations']})
    for o in req['observations']:
        choice=observations[o['observation_id']]
        if choice['test_id'] is None:continue
        words=source(choice['source_span_ids']);test=tests.get(choice['test_id'])
        if not test or not o['value_source_verified'] or targets[o['target_id']]['route_id'] not in test['route_ids']:raise ValueError('observation route/source unsupported')
        if not any(w['page']==o['value_evidence']['page'] and w['token'].strip(' ,;')==o['original_value'] for w in words):raise ValueError('observation value source omitted')
    return routes,spans,bindings,processes,tests,observations


def project(records,req,response,transport_directory=None):
    routes,spans,bindings,processes,tests,observations=checked(req,response,transport_directory)
    receipt_key=digest([req['pdf_asset']['sha256'],digest(req['graph'])]); response_hash=digest(response)
    previous=records.get('extensions',{}).get('route_field_projection',{}).get(receipt_key)
    if previous:
        detached=copy.deepcopy(records);del detached['extensions']['route_field_projection'][receipt_key]
        if previous['response_sha256']!=response_hash or digest(detached)!=previous['output_sha256']:raise ValueError('projection reentry output changed')
        return previous
    if digest(records)!=req['records_sha256']:raise ValueError('projection stage input changed')
    staged=copy.deepcopy(records);by_key={m['mix_key']:m for m in staged['mixes']};history=[];bound=0;pending=[]
    numbers={n['token_id']:n for n in req['tokens']};blocks={b['block_id']:b for b in req['blocks']}
    facts={f['fact_id']:f for f in req['graph']['facts']}
    def words(ids):return [w for i in ids for w in spans[i]['locators']]
    def fact_words(ids):
        result=[]
        for fid in ids:
            f=facts[fid];source=blocks[f['source_id']]['locators'];phrase=f['quote'].split()
            matches=[source[i:i+len(phrase)] for i in range(len(source)) if [w['token'] for w in source[i:i+len(phrase)]]==phrase]
            result.extend(matches[0] if len(matches)==1 else source)
        return result
    def evidence(mix,path,locators,formula=None,original_unit=None,transformation=None):
        if not locators:raise ValueError('projection evidence missing')
        page=locators[0]['page'];display=[w for w in locators if w['page']==page]
        text=' '.join(w['token'] for w in display);key='ev-route-'+digest([mix['mix_key'],path,locators])[:24]
        box=[min(w['bbox'][0] for w in display),min(w['bbox'][1] for w in display),max(w['bbox'][2] for w in display),max(w['bbox'][3] for w in display)]
        staged['evidence_links'].append({'evidence_key':key,'asset_key':req['pdf_asset']['asset_key'],'paper_key':mix['paper_key'],
            'record_type':'mix','record_key':mix['mix_key'],'field_path':path,'page':page,'bbox':box,'snippet':text,
            'snippet_sha256':hashlib.sha256(text.encode()).hexdigest(),'extraction_method':'indexed-route-projection-v1',
            'confidence':None,'extensions':{'support_tokens':locators,'review_status':'pending_source_path_review'}})
        mix['field_provenance'][path]={'evidence_key':key,'original_value':text,'original_unit':original_unit,'formula':formula,'transformation':transformation,
            'extraction_method':'indexed-route-projection-v1','review_status':'pending_source_path_review'}
    def process_entry(process):
        item={k:copy.deepcopy(process[k]) for k in ('process_id','role','after_stage_id','method_zh','physical_state','excluded_route_ids')}
        item['limits']=[]
        for lim in process['limits']:
            n=numbers[lim['number_id']];scale=1000 if lim['unit']=='mm' else 1
            item['limits'].append({'kind':lim['kind'],'value':n['value']*scale,'unit':'um',
                'original_value':n['value'],'original_unit':lim['unit'],'scale':scale,'reverse_value':n['value']*scale/scale})
        return item
    def process_words(process):
        locators=words(process['source_span_ids'])
        for limit in process['limits']:
            n=numbers[limit['number_id']]
            if n['locator'] not in words(limit['source_span_ids']):
                for word in blocks[n['block_id']]['locators']:
                    if word not in locators:locators.append(word)
        return locators
    for target in req['targets']:
        if target.get('update_mode')=='independent':
            if target['mix_key'] in by_key:raise ValueError('independent route key already exists')
            created=empty_source_mix(req['pdf_asset']['paper_key'],target['mix_key'],target['source_label'],None,None)
            created['extensions']['source_relation_owner']={'subject_id':target['subject_id'],'route_id':target['route_id'],
                'reference_mix_keys':target['reference_mix_keys'],'recipe_transfer_authorized':False}
            staged['mixes'].append(created);by_key[target['mix_key']]=created
        mix=by_key[target['mix_key']];binding=bindings[target['target_id']];rid=binding['route_id'];route=routes[rid]
        if mix['paper_key']!=req['pdf_asset']['paper_key']:raise ValueError('cross-paper field projection')
        owned_prefixes=('/modules/mixing_curing','/modules/identity_source_specimen/specimen_type')
        history.append({'mix_key':mix['mix_key'],'mixing_curing':copy.deepcopy(mix['modules']['mixing_curing']),
            'specimen_type':mix['modules']['identity_source_specimen']['specimen_type'],
            'provenance':{p:v for p,v in mix['field_provenance'].items() if p.startswith(owned_prefixes)}})
        mix['field_provenance']={p:v for p,v in mix['field_provenance'].items() if not p.startswith(owned_prefixes)}
        curing=empty_source_mix(mix['paper_key'],mix['mix_key'],'',None,None)['modules']['mixing_curing']
        curing['curing_route']=rid;curing['extensions']['review_status']='pending_source_path_review'
        mix['modules']['mixing_curing']=curing
        mix['modules']['identity_source_specimen']['specimen_type']=binding['specimen_type']
        type_sources=words(binding['source_span_ids'])
        for p in processes.values():
            if p['role']=='FORMING' and rid in p['route_ids'] and rid not in p['excluded_route_ids'] and p['physical_state']==binding['specimen_type']:
                type_sources.extend(words(p['source_span_ids']))
        evidence(mix,'/modules/identity_source_specimen/specimen_type',type_sources)
        identity=next(o['words'] for o in target['identity_options'] if o['location_id']==binding['identity_location_id'])
        evidence(mix,'/modules/mixing_curing/curing_route',identity+words(binding['source_span_ids']))
        entries=[]
        for process in processes.values():
            if rid not in process['route_ids'] or process['role']=='TEST_PREPARATION':continue
            entries.append(process_entry(process))
        forming=[p for p in entries if p['role']=='FORMING']
        curing['forming']={'method':'；'.join(p['method_zh'] for p in forming) or None,
            'extensions':{'preparation_stages':entries,'review_status':'pending_source_path_review'}}
        if forming:evidence(mix,'/modules/mixing_curing/forming/method',[w for p in forming for w in process_words(processes[p['process_id']])])
        for i,p in enumerate(entries):
            source=processes[p['process_id']]
            path='/modules/mixing_curing/forming/extensions/preparation_stages/'+str(i)
            evidence(mix,path,process_words(source))
            for j,limit in enumerate(source['limits']):evidence(mix,path+'/limits/'+str(j)+'/value',[numbers[limit['number_id']]['locator']],'um = original * scale',limit['unit'],p['limits'][j])
        for s in route['stages']:
            if s['kind']=='PREPARATION':continue
            props=s['properties'];index=len(curing['curing_stages'])
            quoted=' '.join(facts[f]['quote'].casefold() for f in s['fact_ids'])
            method=('co2 incubator curing' if 'incubator' in quoted and 'co2_concentration' in props else
                    'curing chamber' if 'chamber' in quoted else None)
            item={'method':method,
                'duration_seconds':props['duration']['value'],'temperature_C':props.get('temperature',{}).get('value'),
                'humidity_percent':props.get('relative_humidity',{}).get('value'),
                'extensions':{'stage_id':s['stage_id'],'kind':s['kind'],'time_origin':'STAGE_START',
                    'properties':copy.deepcopy(props),'review_status':'pending_source_path_review'}}
            item['extensions'].update(temperature_tolerance_C=props.get('temperature',{}).get('uncertainty'),
                humidity_tolerance_percent=props.get('relative_humidity',{}).get('uncertainty'),
                humidity_relation={'=':'','>=':'≥','<=':'≤'}.get(props.get('relative_humidity',{}).get('relation'),props.get('relative_humidity',{}).get('relation')),
                co2_percent=props.get('co2_concentration',{}).get('value'),
                co2_tolerance_percent=props.get('co2_concentration',{}).get('uncertainty'),
                duration_unit='d' if props['duration']['source']['unit']=='d' else 'h')
            item['extensions']['qualitative_conditions']=[{'quantity':q['quantity'],'value':None,'description_zh':q['description_zh']}
                for q in response['qualitative_conditions'] if rid in q['route_ids'] and s['stage_id'] in q['stage_ids']]
            curing['curing_stages'].append(item);path='/modules/mixing_curing/curing_stages/'+str(index)
            evidence(mix,path,fact_words(s['fact_ids']))
            if method:evidence(mix,path+'/method',[w for w in fact_words(s['fact_ids']) if w['token'].casefold().strip(' ,.;()') in ('incubator','chamber')])
            selected_conditions=[q for q in response['qualitative_conditions'] if rid in q['route_ids'] and s['stage_id'] in q['stage_ids']]
            for j,q in enumerate(selected_conditions):evidence(mix,path+'/extensions/qualitative_conditions/'+str(j)+'/description_zh',words(q['source_span_ids']))
            for name,field in (('duration','duration_seconds'),('temperature','temperature_C'),('relative_humidity','humidity_percent')):
                prop=props.get(name)
                if not prop or prop['value'] is None:continue
                original=prop['source']['value'];locs=[w for w in fact_words(prop['fact_ids']) if number(w['token'])==original]
                evidence(mix,path+'/'+field,locs,'normalized = original * scale',prop['source']['unit'],prop['transformation'])
            for name,field in (('temperature','temperature_tolerance_C'),('relative_humidity','humidity_tolerance_percent'),('co2_concentration','co2_tolerance_percent')):
                prop=props.get(name,{})
                if prop.get('uncertainty') is None:continue
                candidates=fact_words(prop['fact_ids']);at=next(i for i,w in enumerate(candidates) if number(w['token'])==prop['source']['value'])
                locs=[w for w in candidates[at+1:at+4] if number(w['token'])==prop['uncertainty']]
                evidence(mix,path+'/extensions/'+field,locs,original_unit=prop['source']['unit'])
            co2=props.get('co2_concentration')
            if co2 and co2['value'] is not None:
                evidence(mix,path+'/extensions/co2_percent',[w for w in fact_words(co2['fact_ids']) if number(w['token'])==co2['source']['value']],original_unit=co2['source']['unit'])
        test_entries=[]
        for test in tests.values():
            if rid in test['route_ids']:
                applicable=[p for p in test['process_ids'] if rid in processes[p]['route_ids'] and rid not in processes[p]['excluded_route_ids']]
                test_entries.append({**copy.deepcopy(test),'process_ids':applicable,'processes':[process_entry(processes[p]) for p in applicable]})
        curing['forming']['extensions']['test_preparation_routes']=test_entries
        for i,t in enumerate(test_entries):
            path='/modules/mixing_curing/forming/extensions/test_preparation_routes/'+str(i)
            evidence(mix,path,words(t['source_span_ids']))
            for j,p in enumerate(t['processes']):
                source=processes[p['process_id']];evidence(mix,path+'/processes/'+str(j),process_words(source))
                for k,limit in enumerate(source['limits']):evidence(mix,path+'/processes/'+str(j)+'/limits/'+str(k)+'/value',
                    [numbers[limit['number_id']]['locator']],'um = original * scale',limit['unit'],p['limits'][k])
        for o in req['observations']:
            if o['target_id']!=target['target_id']:continue
            choice=observations[o['observation_id']]
            if choice['test_id'] is None:
                pending.append({'observation_id':o['observation_id'],'reason':choice['reason']});continue
            row=mix['modules']['performance'][o['index']];test=tests[choice['test_id']]
            row['extensions']['source_route_binding']={'route_id':rid,'test_id':test['test_id'],'test_specimen_description_zh':test['tested_object_zh'],
                'observation_object':row['specimen'],
                'review_status':'pending_source_path_review'}
            evidence(mix,'/modules/performance/'+str(o['index'])+'/extensions/source_route_binding',words(choice['source_span_ids']))
            bound+=1
    staged.setdefault('extensions',{}).setdefault('route_field_history',[]).append({'input_sha256':req['records_sha256'],'before':history})
    registry=staged['extensions'].setdefault('route_field_projection',{})
    receipt={'response_sha256':response_hash,'output_sha256':digest(staged),'updated_records':len(req['targets']),
             'bound_observations':bound,'pending_observations':pending,'publication_allowed':False}
    registry[receipt_key]=receipt
    records.clear();records.update(staged)
    return receipt


def run(records_file,candidate,packet_file,pdf,output,response_dir=None):
    started=time.monotonic();output=Path(output)
    records=json.loads(Path(records_file).read_bytes());before=copy.deepcopy(records)
    graph=json.loads(Path(candidate).read_bytes())['graph'];packet=json.loads(Path(packet_file).read_bytes())
    req=prepare(records,graph,packet,pdf);payload=visible(req)
    size=len(json.dumps(payload,ensure_ascii=False).encode())
    if size>100000:raise ValueError('projection source payload exceeds 100000 bytes')
    atomic_json(output/'input-size.json',{'visible_bytes':size,'blocks':len(req['blocks']),'bbox_transmitted':False})
    if response_dir:
        if json.loads((Path(response_dir)/'request.json').read_bytes())!=req:raise ValueError('projection reuse dependencies changed')
        response=json.loads((Path(response_dir)/'response.json').read_bytes())
    else:response=invoke(req,output/'model',schema=schema(req),visible=payload,prompt_text=PROMPT)
    from consume_curing_relations import consume
    transport_directory=Path(response_dir) if response_dir else output/'model'
    transport=verify_transport(req,response,transport_directory);atomic_json(output/'transport-binding.json',transport)
    result=consume(records,graph,packet,pdf,projection_request=req,projection_response=response,transport_directory=transport_directory)
    first=digest(records);project(records,req,response,transport_directory)
    if first!=digest(records):raise ValueError('projection replay differs')
    for old,new in zip(before['mixes'],records['mixes']):
        if old['modules']['materials']!=new['modules']['materials']:raise ValueError('recipe changed')
        for a,b in zip(old['modules']['performance'],new['modules']['performance']):
            if any(a[k]!=b[k] for k in ('name','value','unit','age_seconds','specimen','method')):raise ValueError('observation science changed')
    atomic_json(output/'generated-records.json',records)
    report={**result,'status':'ROUTE_FIELDS_PROJECTED','records_sha256':first,'reentry_identical':True,
        'model_calls':0 if response_dir else 1,'elapsed_seconds':time.monotonic()-started}
    atomic_json(output/'validation.json',report);return report


if __name__=='__main__':
    p=argparse.ArgumentParser()
    for name in ('records','candidate','packet','pdf','output'):p.add_argument('--'+name,required=True)
    p.add_argument('--response-dir');a=p.parse_args()
    try:print(json.dumps(run(a.records,a.candidate,a.packet,a.pdf,a.output,a.response_dir)))
    except Exception as error:
        atomic_json(Path(a.output)/'failure.json',{'status':'PROJECTION_FAILED','error':str(error),'publication_allowed':False})
        raise
