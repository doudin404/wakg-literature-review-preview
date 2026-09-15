"""Bounded preparation-operation refinement; preserve original stage extraction."""
import copy
import json
from pathlib import Path
from source_specimen_variants import digest
from source_quantity_producer import invoke,obj,atomic_json


def prepare(context):
    spans={s['span_id']:s for s in context['request']['span_index']};words={};stages=[]
    for route in context['response']['routes']:
        for stage in route['stages']:
            if stage['kind']!='PREPARATION':continue
            ids=[]
            for sid in stage['source_span_ids']:
                for w in spans[sid]['locators']:
                    key='word-'+digest([context['request']['pdf_sha256'],w])[:20]
                    words[key]=w
                    if key not in ids:ids.append(key)
            stages.append({'route_id':route['route_id'],'stage_id':stage['stage_id'],
                'method_zh':stage['method_zh'],'source_word_ids':ids,
                'duration':next(p for p in stage['properties'] if p['name']=='duration')})
    request={'context_sha256':digest(context),'stages':stages,'words':words}
    request['request_sha256']=digest(request)
    return request


def visible(request):
    return {'request_sha256':request['request_sha256'],
        'words':{k:w['token'] for k,w in request['words'].items()},
        'stages':[{'route_id':s['route_id'],'stage_id':s['stage_id'],'method_zh':s['method_zh'],
            'source_word_ids':s['source_word_ids'],'reported_duration':s['duration']['numeric_token_id'],
            'duration_unit':s['duration']['unit']} for s in request['stages']]}


def schema(request):
    s={'type':'string'}
    operation=obj({'operation_id':s,'method_zh':s,'source_word_ids':{'type':'array','items':{
        'type':'string','enum':list(request['words'])}},'owns_reported_duration':{'type':'boolean'}})
    return obj({'request_sha256':s,'stages':{'type':'array','items':obj({
        'route_id':s,'stage_id':s,'operations':{'type':'array','items':operation}})}})


PROMPT='''Read-only operation scope refinement, no tools. Keep existing quantities and
routes; split each supplied preparation stage into ordered atomic operations supported
by the source. Select exact verb/operation word IDs, not all surrounding sentences.
One operation may own the existing reported duration only when the source applies it
to that operation. Other operations have unknown duration, never zero. Do not spread
a vibration/mixing/etc duration over pouring/sealing/etc, and do not sum durations.
Do not invent additional processes. Return all supplied stages once, retaining IDs.
Chinese method descriptions, one physical operation per entry. The source word IDs
are in reading order; numeric wording appears in the source. No new scientific values.'''


def compile_plan(context,request,response):
    if request!=prepare(context) or response['request_sha256']!=request['request_sha256']:
        raise ValueError('operation source request changed')
    expected={(s['route_id'],s['stage_id']):s for s in request['stages']};seen=set()
    for selected in response['stages']:
        key=(selected['route_id'],selected['stage_id'])
        if key not in expected or key in seen:raise ValueError('operation stage duplicate or foreign')
        seen.add(key);stage=expected[key];operations=selected['operations']
        if not operations or len({o['operation_id'] for o in operations})!=len(operations):
            raise ValueError('operation identity empty or duplicated')
        if sum(o['owns_reported_duration'] for o in operations)!=(stage['duration']['numeric_token_id'] is not None):
            raise ValueError('reported duration must have exactly one operation owner')
        for op in operations:
            if not op['source_word_ids'] or not set(op['source_word_ids'])<=set(stage['source_word_ids']):
                raise ValueError('operation outside stage source')
    if seen!=set(expected):raise ValueError('operation stage omitted')
    return {'request':request,'response':response,'context_sha256':digest(context),'publication_allowed':False}


def produce(context,directory):
    request=prepare(context);payload=visible(request)
    size=len(json.dumps(payload,ensure_ascii=False).encode())
    if size>30000:raise ValueError('operation request exceeds 30000 byte cap')
    atomic_json(Path(directory)/'input-size.json',{'visible_bytes':size,'word_count':len(request['words']),
        'stage_count':len(request['stages']),'bbox_transmitted':False})
    response=invoke(request,Path(directory),schema=schema(request),visible=payload,prompt_text=PROMPT)
    plan=compile_plan(context,request,response);atomic_json(Path(directory)/'operation-plan.json',plan)
    return plan


def apply(context,plan):
    if compile_plan(context,plan['request'],plan['response'])!=plan:raise ValueError('operation plan altered')
    result=copy.deepcopy(context)
    selected={(s['route_id'],s['stage_id']):s for s in plan['response']['stages']}
    for route in result['normalized_routes']:
        meta=result['route_metadata'][route['route_id']];stages=[];descriptions=[];previous=None
        for stage,description in zip(route['stages'],meta['stages']):
            refinement=selected.get((route['route_id'],stage['stage_id']))
            ops=refinement['operations'] if refinement else [None]
            for op in ops:
                child=copy.deepcopy(stage);desc=copy.deepcopy(description)
                child['after_stage_id']=previous
                if op:
                    child['stage_id']=stage['stage_id']+'::'+op['operation_id']
                    child['operation_scope']=[plan['request']['words'][i] for i in op['source_word_ids']]
                    desc['method_zh']=op['method_zh']
                    if not op['owns_reported_duration']:
                        p=child['properties']['duration']
                        p.update(value=None,transformation=None,missing_reason='NOT_REPORTED')
                        p.pop('relation',None)
                        p['source']={'value':None,'unit':'s','time_origin':'STAGE_START','missing_reason':'NOT_REPORTED','fact_ids':p['fact_ids']}
                previous=child['stage_id'];stages.append(child);descriptions.append(desc)
        route['stages']=stages;meta['stages']=descriptions
    result['operation_scope_sha256']=digest(plan)
    return result
