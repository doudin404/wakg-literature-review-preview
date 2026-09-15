"""Scoped additions and method/geometry projection, preserving prior observations."""
import copy
import hashlib
import json
import math
import re
import time
from pathlib import Path
import fitz
import numpy as np
from PIL import Image
from jsonschema import validate
import source_performance_completion as producer
import source_design_relations as design
from source_quantity_producer import atomic_json
from source_specimen_variants import digest
from recipe_performance_consumer import Source,consume_quantities,compile_observations,publish_observations,add_link,UNITS

VERSION='performance-completion-consumer-v1'


def phrase(source,quote,ids):
    scopes=source.scope(ids);words=[]
    for s in scopes:
        for word in s['locators']:
            if word not in words:words.append(word)
    # PDF line wrapping can split a printed hyphenated word. Normalize only
    # this typography, retaining a character-to-source-word correspondence.
    text='';ranges=[]
    for word in words:
        token=word['token'].replace('\u00ad','')
        joiner='' if not text or text.endswith('-') else ' '
        start=len(text)+len(joiner);text+=joiner+token;ranges.append((start,len(text)))
    needle=re.sub(r'(?<=-)\s+','',' '.join(quote.replace('\u00ad','').split()))
    start=text.find(needle)
    if not needle or start<0:raise ValueError('method/specimen quote absent from selected source')
    end=start+len(needle);selected=[]
    for word,(lo,hi) in zip(words,ranges):
        if lo<end and hi>start:selected.append(word)
    if any(w.get('asset_key') for w in selected):
        from indexed_native_sources import readable_native_word,evidence_location
        if len({w['asset_key'] for w in selected})!=1:raise ValueError('field quote spans source documents')
        for word in selected:readable_native_word(source.records,word)
        location=evidence_location(selected,source.req['pdf_asset']['asset_key'])
        location['token']=location.pop('snippet')
        return location,scopes
    if len({w['page'] for w in selected})!=1:raise ValueError('field quote spans pages; split metadata claim')
    box=fitz.Rect(selected[0]['bbox'])
    for word in selected[1:]:box|=fitz.Rect(word['bbox'])
    with fitz.open(source.req['pdf_asset']['relative_path']) as doc:
        snippet=doc[selected[0]['page']-1].get_textbox(box).strip()
    return {'page':selected[0]['page'],'bbox':list(box),'token':snippet},scopes


def image_observations(req,response,source):
    figures={f['figure_id']:f for f in req['figures']};result=[]
    for item in response['image_labels']:
        if item['figure_id'] not in figures:raise ValueError('image label figure unknown')
        figure=figures[item['figure_id']];bars={b['bar_id']:b for b in figure['geometry']['bars']}
        if item['associated_bar_id'] not in bars:raise ValueError('image label has no selected source bar')
        route,age,word=source.route(item['row_id'],item)
        value=float(item['raw_value'].strip().rstrip('%'))
        if not math.isfinite(value) or value<0:raise ValueError('invalid printed image value')
        rgb=np.array(Image.open(figure['path']).convert('RGB'));x0,y0,x1,y1=item['label_bbox_px']
        if not (0<=x0<x1<=rgb.shape[1] and 0<=y0<y1<=rgb.shape[0]):raise ValueError('label bbox outside native image')
        ink=rgb[math.floor(y0):math.ceil(y1),math.floor(x0):math.ceil(x1)].max(axis=2)<200
        if not ink.any():raise ValueError('printed label source region has no ink')
        origin=figure['render_origin_px'];box=[(v+origin[i%2])/2 for i,v in enumerate(item['label_bbox_px'])]
        result.append({'row_id':item['row_id'],'name':item['name'],'value':value,'unit':item['unit'],
            'route':route,'age':age,'age_word':word,'age_unit':item['age_unit'],
            'word':{'page':figure['asset']['extensions']['page'],'bbox':box,'token':None},
            'scope':source.scope(item['source_span_ids']),'source_type':'DIRECT_IMAGE_LABEL',
            'figure':figure['asset']['extensions']['figure_number'],
            'digitization':{'image_asset_key':figure['asset']['asset_key'],'image_sha256':figure['asset']['sha256'],
                'raw_label':item['raw_value'],'native_bbox_px':item['label_bbox_px'],
                'associated_bar_id':item['associated_bar_id'],'reading_method':'source_model_printed_numeric_label',
                'formal':False}})
    return result


def project_metadata(records,source,assignments):
    assigned=set();method_count=0;specimen_count=0;dimension_count=0;condition_count=0;unused=[]
    for a in assignments:
        if len(a['row_ids'])!=len(set(a['row_ids'])) or len(a['route_ids'])!=len(set(a['route_ids'])):
            raise ValueError('metadata scope duplicate')
        method=phrase(source,a['method_quote'],a['method_span_ids']) if a['method_quote'] else None
        specimen=phrase(source,a['specimen_quote'],a['specimen_span_ids']) if a['specimen_quote'] else None
        dimensions=[];conditions=[]
        for dest,items in [(dimensions,a['dimensions']),(conditions,a['conditions'])]:
            if len({d['name'] for d in items})!=len(items):raise ValueError('duplicate test property')
            for d in items:
                allowed={'dimension_1':{'mm'},'dimension_2':{'mm'},'dimension_3':{'mm'},'capacity':{'mL'},
                         'loading_rate':{'N/s','kN/s'},'replicate_count':{'count'},'measurement_elapsed':{'s','min','h'}}
                if d['unit'] not in allowed[d['name']]:raise ValueError('test property unit mismatch')
                value,word=source.number(d['numeric_token_id'],d['source_span_ids'])
                scales={'mm':1,'mL':1,'N/s':1,'kN/s':1000,'count':1,'s':1,'min':60,'h':3600}
                unit={'kN/s':'N/s','min':'s','h':'s'}.get(d['unit'],d['unit']);scale=scales[d['unit']]
                if value<=0:raise ValueError('nonpositive method/geometry parameter')
                dest.append({'name':d['name'],'value':value*scale,'unit':unit,'original_value':word['token'],
                    'original_unit':d['unit'],'word':word,'source_scope':source.scope(d['source_span_ids']),
                    'formula':f'value = original * {scale}','formal':False})
        assignment_targets=0
        for row in a['row_ids']:
            mix=source.owner(row);routes={r['route_id']:r for r in mix['modules']['mixing_curing']['extensions']['source_routes']}
            if not {rid for rid in a['route_ids'] if rid is not None}<=set(routes):raise ValueError('metadata route not owned by row')
            targets=[(i,p) for i,p in enumerate(mix['modules']['performance']) if p['name'] in a['names'] and p['extensions'].get('route_id') in a['route_ids']]
            if not targets:
                unused.append({'row_id':row,'route_ids':a['route_ids'],'names':a['names'],
                               'reason':'method scope declared, no observed result to project'})
                continue
            assignment_targets+=len(targets)
            for i,p in targets:
                key=(mix['mix_key'],i)
                if key in assigned:raise ValueError('two metadata assignments target same observation')
                assigned.add(key);prefix=f'/modules/performance/{i}'
                for field,prepared,quote in [('method',method,a['method_quote']),('specimen',specimen,a['specimen_quote'])]:
                    if not prepared:continue
                    if field=='method' and p.get(field) not in (None,quote):raise ValueError('method projection would overwrite known method')
                    p['extensions'].setdefault('previous_'+field,p.get(field));p[field]=quote
                    word,scope=prepared;add_link(records,mix,prefix+'/'+field,word,source,
                        extensions={'scope_evidence':scope,'route_id':p['extensions']['route_id'],'source_quote':quote})
                    if field=='method':method_count+=1
                    else:specimen_count+=1
                for name,items in [('specimen_geometry',dimensions),('test_conditions',conditions)]:
                    if not items:continue
                    if p['extensions'].get(name):raise ValueError('metadata property already present')
                    p['extensions'][name]=[]
                    for d in items:
                        j=len(p['extensions'][name]);path=f'{prefix}/extensions/{name}/{j}/value'
                        evidence=add_link(records,mix,path,d['word'],source,extensions={'scope_evidence':d['source_scope'],
                            'route_id':p['extensions']['route_id'],'formula':d['formula']})
                        p['extensions'][name].append({k:copy.deepcopy(v) for k,v in d.items() if k not in ('word','source_scope')}|{'evidence_key':evidence})
                        mix['field_provenance'][path].update(original_value=d['original_value'],original_unit=d['original_unit'],formula=d['formula'])
                        if name=='specimen_geometry':dimension_count+=1
                        else:condition_count+=1
                p['extensions']['method_status']='SOURCE_SCOPED_METADATA_PENDING_REVIEW'
        if not assignment_targets:raise ValueError('metadata assignment has no matching observations')
    return {'method_observations':method_count,'specimen_observations':specimen_count,
            'geometry_values':dimension_count,'test_condition_values':condition_count,'metadata_scope_without_observation':unused}


def consume(records,req,response):
    validate(response,producer.schema(req))
    if req['request_sha256']!=digest({k:v for k,v in req.items() if k!='request_sha256'}) or response['request_sha256']!=req['request_sha256'] or digest(records)!=req['records_sha256']:
        raise ValueError('completion immutable input changed')
    staged=copy.deepcopy(records);source=Source(staged,req)
    quantities=consume_quantities(staged,req,response,source)
    observations,bars=compile_observations(req,response,source)
    labels=image_observations(req,response,source)
    added,updated=publish_observations(staged,source,observations+labels,merge_existing=True)
    metadata=project_metadata(staged,source,response['test_metadata'])
    resolved=0;known={q['claim_sha256']:q for q in staged.get('quarantined',[]) if q.get('kind')=='ambiguous_performance_source'}
    processed=set()
    for decision in response['quarantine_resolutions']:
        ids=decision['claim_sha256s']
        if set(ids)&processed or not set(ids)<=set(known):raise ValueError('quarantine resolution identity differs')
        processed.update(ids);source.scope(decision['source_span_ids'])
        if decision['decision']=='RETAIN_UNKNOWN':continue
        if not decision['resolved_targets'] or not decision['figure_ids'] or not set(decision['figure_ids'])<={f['figure_id'] for f in req['figures']}:
            raise ValueError('quarantine resolution lacks figure and target evidence')
        references=[]
        for target in decision['resolved_targets']:
            matching=[o for o in observations if o['row_id']==target['row_id'] and o['name']==target['name']]
            if not any(o['source_type']=='DIRECT_PROSE' for o in matching) or not any(o['source_type']=='DIGITIZED_ESTIMATE' for o in matching):
                raise ValueError('resolved target lacks direct and graphical source comparison')
            for direct in [o for o in matching if o['source_type']=='DIRECT_PROSE']:
                plots=[o for o in matching if o['source_type']=='DIGITIZED_ESTIMATE' and o['route']==direct['route'] and o['age']==direct['age']]
                if len(plots)!=1:raise ValueError('resolved source comparison route ambiguous')
                plot=plots[0];(_,v0),(_,v1)=plot['digitization']['axis']['anchors']
                (p0,_),(p1,_)=plot['digitization']['axis']['anchors']
                tolerance=3*abs((v1-v0)/(p1-p0))+.05
                direct_unit,direct_scale=UNITS[direct['name']][direct['unit']]
                plot_unit,plot_scale=UNITS[plot['name']][plot['unit']]
                if direct_unit!=plot_unit or abs(direct['value']*direct_scale-plot['value']*plot_scale)>tolerance*plot_scale:
                    raise ValueError('resolved prose and plot values disagree')
            references.append({'mix_key':source.rows[target['row_id']]['mix_key'],'name':target['name']})
        for key in ids:
            known[key]['status']='SUPERSEDED_BY_SOURCE_RESOLUTION';known[key]['resolution']={
                'request_sha256':req['request_sha256'],'reason':decision['reason'],'targets':references,
                'source_span_ids':decision['source_span_ids'],'figure_ids':decision['figure_ids'],'formal':False};resolved+=1
    return staged,{'status':'PERFORMANCE_COMPLETION_DEVELOPMENT_CONSUMED','quantities_added':quantities,
        'observations_added':added,'observations_merged':updated,'digitized_bars':len(bars),'printed_labels':len(labels),
        **metadata,'quarantine_claims_resolved':resolved,'unresolved':response['unresolved'],
        'records_sha256':digest(staged),'publication_allowed':False,'independent_acceptance':False}


def production(records,config,directory):
    if not config:return {'status':'NOT_CONFIGURED','model_calls':0}
    directory=Path(directory);path=directory/'production.json'
    if path.exists():
        report=json.loads(path.read_bytes())
        if report['records_sha256']!=digest(records) or report['config_sha256']!=digest(config):raise ValueError('completion reentry changed')
        for item in report['artifacts']:
            if hashlib.sha256(Path(item['path']).read_bytes()).hexdigest()!=item['sha256']:raise ValueError('completion artifact changed')
        return report
    if directory.exists():raise ValueError('completion output must be fresh')
    started=time.monotonic();source=Path(config['source_directory']);req=json.loads((source/'request.json').read_bytes())
    current=producer.prepare(records,req['pages'],config['asset_root'],[f['asset']['asset_key'] for f in req['figures']])
    if current!=req:raise ValueError('completion source request no longer reproduces')
    raw=json.loads((source/'model/response.json').read_bytes());usage=json.loads((source/'model/usage.json').read_bytes())
    events=[json.loads(l) for l in (source/'model/events.jsonl').read_text(encoding='utf8').splitlines() if l.startswith('{')]
    messages=[e['item']['text'] for e in events if e.get('item',{}).get('type')=='agent_message']
    # The stream may contain structured interim messages in the same turn.
    # The persisted final response must match its last completed agent message.
    if not messages or json.loads(messages[-1])!=raw or usage['model_turns']!=1 or usage['returncode']!=0:
        raise ValueError('completion source response has no bound model completion')
    if any(e.get('item',{}).get('type') in ('command_execution','file_change','mcp_tool_call','web_search') for e in events):raise ValueError('source-only scope exceeded')
    response=design.translate_wire(raw,design.wire_maps(req)[1])
    if response!=json.loads((source/'decoded-response.json').read_bytes()):raise ValueError('completion raw response changed')
    result,report=consume(records,req,response)
    atomic_json(directory/'generated-records.json',result)
    report.update(input_records_sha256=digest(records),config_sha256=digest(config),request_sha256=req['request_sha256'],
        raw_response_sha256=digest(raw),source_message_count=len(messages),model_calls=0,elapsed_seconds=time.monotonic()-started,source_usage=usage,
        source_token_proxy=sum(u.get('input_tokens',0)-u.get('cached_input_tokens',0)+u.get('output_tokens',0) for u in usage['usage']))
    report['artifacts']=[{'path':str(p.resolve()),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in
        [directory/'generated-records.json',source/'request.json',source/'decoded-response.json',source/'model/response.json']]
    atomic_json(path,report);records.clear();records.update(result);return report
