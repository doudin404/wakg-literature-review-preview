"""Indexed experimental-design/physical-route consumer for existing source rows.

Rows remain recipes. Physical branches are explicitly scoped specimens, not duplicate
recipes. This stage never creates or changes performance numbers or recipe quantities.
"""
import argparse
import copy
import hashlib
import json
import re
import time
from pathlib import Path
import fitz
from formulation_source_plan import index_spans
from relation_curing_stages import normalized_routes, UNITS
from source_quantity_producer import obj, number, invoke, atomic_json
from source_specimen_variants import digest
from source_mix_record import empty_source_mix
from design_condition_cases import expand as expand_condition_cases, CONDITION_UNITS

VERSION = 'indexed-design-relations-v2'


def observation_method_scope(records,index,task,slot,mix,observation_index,links=None):
    """Join existing source-bound methods and physical routes; never infer a family.

    A null planned property has no meaning on its own. Several properties may
    share a task only when each has its own route-bound method in that task's
    explicit source scope. No labels, objective prose or method keywords route it.
    """
    from paper_block_dispatch import source_matches
    p=mix['modules']['performance'][observation_index];ext=p.get('extensions',{})
    def reject(reason):return {'matched':False,'reason':reason}
    if slot.get('property_name') is not None and p.get('name')!=slot['property_name']:
        return reject('PROPERTY_OUTSIDE_SLOT')
    ids=set(task['source_ids']) & set(slot.get('source_ids',[])+slot.get('context_source_ids',[]))
    if not ids:return reject('METHOD_SOURCE_SCOPE_UNSPECIFIED')
    rid=ext.get('route_id') or ext.get('source_route_binding',{}).get('route_id')
    routes=mix['modules'].get('mixing_curing',{}).get('extensions',{}).get('source_routes',[])
    branches=[r for r in routes if rid is not None and r['route_id']==rid]
    if len(branches)!=1:return reject('PHYSICAL_ROUTE_UNRESOLVED')
    branch=branches[0];binding=ext.get('source_route_binding')
    if binding and binding.get('route_id')!=rid:return reject('ROUTE_BINDING_CONFLICT')
    row=mix['modules'].get('materials',{}).get('extensions',{}).get('design_scope',{}).get('row_id')
    subject=branch.get('subject_scope',{})
    if not row or row not in subject.get('row_ids',[]) or row in subject.get('excluded_row_ids',[]):
        return reject('ROUTE_SUBJECT_SCOPE_UNRESOLVED')
    age=branch.get('specimen_age_seconds')
    if age is not None and p.get('age_seconds')!=age:return reject('ROUTE_AGE_CONFLICT')
    if branch.get('age_scope',{}).get('status')=='UNRESOLVED_TEST_STAGE':
        return reject('ROUTE_TEST_STAGE_UNRESOLVED')
    specimens=mix['modules'].get('identity_source_specimen',{}).get('specimens',[])
    specimen_rows=[s for s in specimens if s['specimen_id']==rid]
    if len(specimen_rows)!=1 or not p.get('specimen'):return reject('PHYSICAL_OBJECT_UNRESOLVED')
    physical_type=specimen_rows[0].get('specimen_type')
    if p['specimen'] in ('paste','mortar','concrete','powder','solution') and physical_type!=p['specimen']:
        return reject('PHYSICAL_OBJECT_CONFLICT')
    links=links if links is not None else {e['evidence_key']:e for e in records['evidence_links']}
    assets={a['asset_key']:a for a in records.get('assets',[])}
    sources={s['source_id']:s for s in index['sources']}
    def bound_link(field):
        path=f'/modules/performance/{observation_index}/'+field
        ev=links.get(mix.get('field_provenance',{}).get(path,{}).get('evidence_key'),{})
        if (ev.get('record_key')!=mix['mix_key'] or ev.get('field_path')!=path
                or ev.get('extensions',{}).get('route_id')!=rid):return None,[]
        pdf=assets.get(ev.get('asset_key'),{})
        matched=[sid for sid in sorted(ids) if sid in sources
            and sources[sid]['document_sha256']==pdf.get('sha256') and source_matches(ev,sources[sid])]
        return ev,matched
    method,matched=bound_link('method')
    if not p.get('method') or not method or not matched:return reject('ROUTE_METHOD_SOURCE_UNRESOLVED')
    # A narrative physical object (e.g. fractured halves) must have its own
    # source evidence. The recipe's generic paste type cannot establish it.
    if p['specimen'] not in ('paste','mortar','concrete','powder','solution'):
        specimen,located=bound_link('specimen')
        if not specimen or not located:return reject('PHYSICAL_OBJECT_SOURCE_UNRESOLVED')
    return {'matched':True,'route_id':rid,'family_id':branch.get('condition_family',rid),
        'source_ids':matched,'method_evidence_key':method['evidence_key'],
        'specimen':p['specimen'],'age_seconds':p.get('age_seconds')}


def existing_method_contract(records,index,plan):
    """Project existing route/preparation/method fields into the shared result contract."""
    from paper_block_dispatch import reusable_values
    objects={o['object_id']:o for o in plan['objects']};slots={s['slot_id']:s for s in plan['slots']}
    links={e['evidence_key']:e for e in records['evidence_links']};changes={};unresolved=[]
    mixes={m['mix_key']:m for m in records['mixes']};record_hash=digest(records)
    for task in plan['tasks']:
        if task['kind']!='text':continue
        selected=[slots[k] for k in task['slot_ids'] if slots[k]['field_path'] in
                  ('/modules/performance','/modules/mixing_curing')]
        if not selected:continue
        for slot in selected:
            if slot['field_path']!='/modules/performance':continue
            for key in objects[slot['object_id']]['existing_keys']:
                mix=mixes[key]
                for i,p in enumerate(mix['modules']['performance']):
                    if slot.get('property_name') is not None and slot['property_name']!=p['name']:continue
                    scope=observation_method_scope(records,index,task,slot,mix,i,links)
                    if not scope['matched']:
                        unresolved.append({'task_id':task['task_id'],'slot_id':slot['slot_id'],
                            'record_key':key,'observation_index':i,'reason':scope['reason']})
        for value in reusable_values(records,index,task,selected,objects,record_hash):
            path=value['field_path']
            if path.startswith('/modules/performance/'):
                suffix=path.split('/',4)[-1]
                if suffix not in ('method','specimen','extensions/source_route_binding') and not suffix.startswith('extensions/test_conditions/'):
                    continue
            key=(value['record_key'],path,value['evidence']['evidence_key'])
            c=changes.setdefault(key,{'record_key':value['record_key'],'field_path':path,'value':value['value'],
                'evidence_key':value['evidence']['evidence_key'],'source_ids':[],'task_ids':[]})
            c['source_ids']=sorted(set(c['source_ids']+value['source_ids']))
            c['task_ids']=sorted(set(c['task_ids']+[task['task_id']]))
    return {'kind':'method_relations','execution_status':'succeeded','changes':list(changes.values()),
        'unresolved':unresolved,'scope_coverage':'partial','whole_task_complete':False,'formal_acceptance':False}


def source_number(text):
    # Strip trailing punctuation AFTER a percent sign too; preserve leading .5.
    return number(text.strip(' ,;()').rstrip('.%'))


def select_source_pages(records):
    """Route section pages and already located recipe tables, not scientific answers."""
    pdfs=[a for a in records['assets'] if a['kind']=='pdf']
    if len(pdfs)!=1:raise ValueError('source page planning requires one PDF')
    with fitz.open(pdfs[0]['relative_path']) as doc:
        starts=[];ends=[]
        for pn,page in enumerate(doc,1):
            for line in page.get_text().splitlines():
                if re.match(r'^\s*\d+\.?\s+(?:Experimental|Materials(?:\s+and\s+\w+)*|Methods?|Methodology)\s*$',line,re.I):starts.append(pn)
                if re.match(r'^\s*\d+\.?\s+Results(?:\s+and\s+discussion)?\s*$',line,re.I):ends.append(pn)
        if not starts:raise ValueError('method section start unresolved; bounded source-page plan required')
        first=min(starts);last=next((p for p in ends if p>=first),None)
        if last is None or last-first>5:raise ValueError('method section boundary exceeds bounded planning scope')
        selected=set(range(first,last+1))
        keys={m['mix_key'] for m in records['mixes']}
        selected.update(e['page'] for e in records['evidence_links'] if e.get('record_key') in keys and e.get('table_number') and e.get('page'))
        if len(selected)>8:raise ValueError('relation source pages exceed bounded batch')
    return sorted(selected)


def prepare(records, pages):
    assets = [a for a in records['assets'] if a['kind'] == 'pdf']
    if len(assets) != 1:
        raise ValueError('design stage needs one immutable PDF')
    asset = assets[0]
    raw = Path(asset['relative_path']).read_bytes()
    if hashlib.sha256(raw).hexdigest() != asset['sha256']:
        raise ValueError('design PDF changed')
    pages = sorted(set(pages)); blocks = []; rows = []; groups = {}
    links = {e['evidence_key']: e for e in records['evidence_links']}
    with fitz.open(stream=raw, filetype='pdf') as doc:
        if not pages or min(pages) < 1 or max(pages) > len(doc):
            raise ValueError('invalid bounded source pages')
        for pn in pages:
            native = {}
            for w in doc[pn-1].get_text('words', sort=False):
                native.setdefault(w[5], []).append({'page': pn, 'bbox': list(w[:4]), 'token': w[4]})
            for locators in native.values():
                blocks.append({'block_id': 'b-'+digest([asset['sha256'], locators])[:16], 'locators': locators})
        for m in records['mixes']:
            if m['paper_key'] != asset['paper_key']:
                raise ValueError('cross-paper row')
            row_id = 'row-'+digest(m['mix_key'])[:16]
            parameters = m['modules']['materials'].get('extensions', {}).get('reported_parameters', [])
            rows.append({'row_id': row_id, 'mix_key': m['mix_key'],
                'label': m['modules']['identity_source_specimen']['custom_test_id'],
                'table': m['extensions'].get('source_table'), 'source_row': m['extensions'].get('source_row'),
                'parameters': [{k: p.get(k) for k in ('parameter_key', 'value', 'unit', 'original_value')} for p in parameters]})
            for i, p in enumerate(m['modules']['performance']):
                signature = {k: p.get(k) for k in ('name', 'specimen', 'method', 'unit')}
                signature.update(table=m['extensions'].get('source_table'), source_header=p.get('extensions', {}).get('source_header'))
                key = 'obs-'+digest(signature)[:16]
                group = groups.setdefault(key, {'group_id': key, **signature, 'occurrences': []})
                link = links.get(m.get('field_provenance', {}).get('/modules/performance/'+str(i)+'/value', {}).get('evidence_key'), {})
                original = str(p.get('extensions', {}).get('original_value', p['value']))
                verified = False
                if link.get('asset_key') == asset['asset_key'] and link.get('page') and link.get('bbox'):
                    native = doc[link['page']-1].get_text('words', clip=fitz.Rect(link['bbox'])+(-1,-1,1,1))
                    verified = any(w[4].strip(' ,;*') == original.strip(' ,;*') for w in native)
                group['occurrences'].append({'row_id': row_id, 'index': i, 'value_source_verified': verified,
                                              'value_evidence_key': link.get('evidence_key')})
    spans = index_spans(blocks, asset['sha256']); tokens = []
    for b in blocks:
        for wi, w in enumerate(b['locators']):
            value = source_number(w['token'])
            if value is not None:
                tokens.append({'token_id': 'n-'+digest(w)[:16], 'value': value,
                               'block_id': b['block_id'], 'word_index': wi, 'locator': w})
    request = {'version': VERSION, 'records_sha256': digest(records), 'pdf_asset': asset, 'pages': pages,
               'blocks': blocks, 'spans': spans, 'tokens': tokens, 'rows': rows, 'observation_groups': list(groups.values())}
    request['request_sha256'] = digest(request)
    return request


def visible(req):
    return {'request_sha256': req['request_sha256'],
        'blocks': [{'block_id': b['block_id'], 'words': [w['token'] for w in b['locators']]} for b in req['blocks']],
        'spans': [{k: s[k] for k in ('span_id', 'block_id', 'start', 'end')} for s in req['spans']],
        'number_columns': ['token_id','value','block_id','word_index'],
        'numbers': [[t[k] for k in ('token_id', 'value', 'block_id', 'word_index')] for t in req['tokens']],
        'rows': [{k: v for k, v in r.items() if k not in ('mix_key','parameters')} |
                 {'parameters': [[p['parameter_key'],p['value'],p['unit']] for p in r['parameters']]} for r in req['rows']],
        'observation_groups': [{k: v for k, v in g.items() if k != 'occurrences'} |
             {'row_labels': sorted({r['label'] for r in req['rows'] if r['row_id'] in {o['row_id'] for o in g['occurrences']}})} for g in req['observation_groups']]}


def schema(req):
    s = {'type': 'string'}; nullable = {'type': ['string', 'null']}
    strings = {'type': 'array', 'items': s}
    spans = {'type': 'array', 'items': {'type': 'string', 'enum': [x['span_id'] for x in req['spans']]}}
    numeric = {'type': ['string', 'null'], 'enum': [x['token_id'] for x in req['tokens']]+[None]}
    prop = obj({'name': {'type': 'string', 'enum': list(UNITS)}, 'numeric_token_id': numeric,
        'uncertainty_token_id': numeric, 'unit': {'type': 'string', 'enum': sorted(set.union(*UNITS.values()))},
        'relation': {'type': ['string', 'null'], 'enum': ['=', '>', '>=', '<', '<=', None]},
        'missing_reason': {'type': ['string', 'null'], 'enum': ['NOT_REPORTED', 'NOT_DETERMINED', None]}, 'source_span_ids': spans})
    stage = obj({'stage_id': s, 'kind': {'type': 'string', 'enum': ['PREPARATION','INITIAL_CURING','CURING','EXPOSURE','STORAGE','TESTING']},
        'operation': {'type': 'string', 'enum': ['mixing','forming','curing','cutting','washing','waiting','testing','other']},
        'method_zh': s, 'source_span_ids': spans, 'properties': {'type': 'array', 'items': prop}})
    route = obj({'route_id': s, 'description_zh': s, 'specimen_type': {'type': ['string','null'], 'enum': ['paste','mortar','concrete','powder','solution',None]},
        'endpoint_status':{'type':'string','enum':['KNOWN','UNKNOWN','NOT_APPLICABLE']},
        'stage_ids': strings, 'source_span_ids': spans})
    condition = {'anyOf':[obj({'name': {'type':'string','enum':[name]},
        'numeric_token_id': {'type':'string','enum':[x['token_id'] for x in req['tokens']]},
        'unit': {'type':'string','enum':list(units)},
        'source_span_ids': {**spans,'minItems':1}}) for name,units in CONDITION_UNITS.items()]}
    case = obj({'case_id':s, 'description_zh':s, 'row_ids':strings, 'excluded_row_ids':strings,
        'conditions':{'type':'array','items':condition}, 'stage_ids':strings, 'source_span_ids':spans})
    route['properties']['condition_cases']={'type':'array','items':case}
    route['required'].append('condition_cases')
    route['properties']['subject_scope']=obj({'row_ids':{**strings,'minItems':1},
        'excluded_row_ids':strings,'source_span_ids':{**spans,'minItems':1}})
    route['properties']['sequence_mode']={'type':'string','enum':['SHARED_PREFIX','COMPLETE_CASE']}
    route['required'].extend(['subject_scope','sequence_mode'])
    design = obj({'design_id': s, 'description_zh': s,
        'row_ids': {'type': 'array', 'items': {'type': 'string', 'enum': [r['row_id'] for r in req['rows']]}},
        'route_ids': strings, 'source_span_ids': spans})
    obs = obj({'group_id': s, 'route_id': nullable, 'case_id':nullable, 'reason': s, 'source_span_ids': spans})
    return obj({'request_sha256': {'type': 'string', 'enum': [req['request_sha256']]},
        'designs': {'type': 'array', 'items': design}, 'stages': {'type': 'array', 'items': stage},
        'routes': {'type': 'array', 'items': route}, 'observations': {'type': 'array', 'items': obs}})


PROMPT = '''Read the supplied indexed methods and tables to describe the experimental design.
Return the schema JSON using source span and numeric token IDs, with concise Chinese
descriptions. Assign each recipe row to its stated design and define its physical/test
routes. A formulation can have several specimen families and measurement routes.
Define reusable stages for mixing, forming, mould curing, later curing, exposure, washing,
cutting, storage and testing. Attach each reported duration, temperature, humidity and
uncertainty to its actual operation; use null with a reason for an unreported duration.
Retain the reported specimen type, geometry, operation order and intermediate preparation.
Each route has its row scope, ordered stage IDs, source spans and endpoint status:
KNOWN for a stated test age, NOT_APPLICABLE for fresh testing, UNKNOWN for an unstated end.
Use condition_cases for source-declared parallel ages, temperatures or specimen conditions.
SHARED_PREFIX means shared parent stages followed by case-specific stages; COMPLETE_CASE
means an empty parent list and a complete sequence in each case. Preserve the actual joint
conditions in the source. Each family's included and excluded rows describe its full scope.
Map existing observation groups to their matching route and case, or null with a source-
based reason. The result describes preparation and relationships; quantities and measured
performance are handled by their own source-value readers.'''


PRODUCTION_PROMPT = PROMPT + '''
This is the production extraction, with no manual correction file. All source sentences
are supplied directly under their PDF block. Select S identifiers; a block's whole_span
selects the entire block when a sentence continues or semantics require joint context.
Numbers use N identifiers with a source sentence and local token context. Rows use R and
observation groups G. Only identifiers are short aliases; scripts restore exact PDF locators.
First determine material type from the preparation: a paste remains a paste when hardened,
cut or tested in compression. Do not call it mortar without source aggregates. An analytical
instrument does not authorize changing its specimen to powder without preparation evidence.
Walk the preparation sequence BEFORE assigning observations. Shared cutting/bisecting,
demoulding or washing must appear on every applicable branch, not just the later branch.
When a specimen is cut and one portion is subsequently post-treated, the retained earlier
portion also underwent the cutting operation. A source diagram's text and prose can support
that sequence; do not infer values from image geometry or branch labels alone.
Use a separate waiting operation for a reported delay after stopping mixing; testing duration
remains unknown unless reported as duration. TESTING is not STORAGE or CURING. An operation's
duration must never be assigned to a combined sequence, specimen age, or the subsequent test.
For measurements described only as during curing, retain the known tube/plate/material and
measurement context in a partial route with endpoint_status=UNKNOWN. Do not assign a specific
temperature or duration solely because another test has that endpoint. A partial route may
bind an observation group: known scope is useful even though its precise endpoint is unknown.
Known endpoint routes use KNOWN; fresh tests without a curing endpoint use NOT_APPLICABLE.
Do not omit reported humidity or tolerance when constructing a stage with a reported temperature.
Do not redo recipe quantities or performance extraction. No acceptance or publication claim.
Before choosing routes, enumerate EVERY explicitly stated parallel experimental condition
in the methods (including every testing age and temperature), with its exact object scope.
FIRST declare each test family's subject_scope independently: include only source-supported
row_ids and list excluded_row_ids. Together they partition ALL supplied paper rows, including
rows of other designs. Included rows must belong to designs referencing this test family.
Do not omit external-design exclusions or include unknown/foreign-paper identifiers.
Mechanical testing scope does not authorize all those rows for characterization. A named
comparative subset must remain a subset. Select its own source spans before any conditions.
Use condition_cases to express alternative JOINT cases, not sequential operations. Cases
partition/overlap only within their family's INCLUDED subjects; case row_ids plus excluded
row_ids partition that included scope, not unrelated excluded subjects. Each case has a
case_id UNIQUE WITHIN THAT FAMILY, source-supported description, and only reported scalar
conditions. Different families may reuse a local case_id; the compiler namespaces it.
Choose one explicit sequence_mode per family: SHARED_PREFIX uses parent stage_ids followed
by each case's stage_ids. Empty case stage_ids means exactly the same complete parent
sequence at that case's condition, with NO additional operations. COMPLETE_CASE requires
parent stage_ids=[] and each case contains its entire nonempty sequence. Never duplicate
operations between parent and case. With no cases use SHARED_PREFIX and a full parent list.
Each condition selects an exact numeric source token and source spans containing its unit.
Unknown or inapplicable dimensions are OMITTED from conditions, never null placeholders.
conditions=[] is legal for a qualitative/source-scoped case without numeric dimensions;
its source description and explicit operation sequence retain the available information.
specimen_age is time since specimen preparation at testing, NEVER test duration or remaining
curing duration. Retain the full source-reported age set, not just its first member. Never
subtract initial curing from age. Parallel temperatures likewise require distinct joint cases.
Do NOT form a Cartesian product of separately mentioned ages, temperatures, or objects.
List only tuples actually declared by the source. Shared preparation may be reused by ID;
alternative testing branches must not occur one after another. Subsequent analyses inherit
only the source-supported mechanical/testing case, not an arbitrarily chosen first age.
If no parallel cases are reported use condition_cases=[]. Existing observation bindings
must use route_id=family ID plus case_id=its local case ID; case_id=null for a non-case route.
Never bind an ambiguous family without choosing its source-supported case. Do not invent cases
to resolve absent observations. Before returning, compare your cases with all parallel
conditions in the source once and include any omitted declared member.
'''


def wire_maps(req):
    forward={}
    for prefix,rows,key in [('S',req['spans'],'span_id'),('N',req['tokens'],'token_id'),
                            ('R',req['rows'],'row_id'),('G',req['observation_groups'],'group_id')]:
        for i,row in enumerate(rows,1):forward[row[key]]=prefix+str(i)
    return forward,{v:k for k,v in forward.items()}


def wire_source(req):
    aliases,_=wire_maps(req);blocks=[]
    for b in req['blocks']:
        spans=[s for s in req['spans'] if s['block_id']==b['block_id']]
        whole=next(s for s in spans if s['start']==0 and s['end']==len(b['locators']))
        sentences=[s for s in spans if s!=whole] or [whole]
        blocks.append({'page':b['locators'][0]['page'],'whole_span':aliases[whole['span_id']],
            'sentences':[[aliases[s['span_id']],s['quote']] for s in sentences]})
    numbers=[]
    for n in req['tokens']:
        candidates=[s for s in req['spans'] if s['block_id']==n['block_id'] and s['start']<=n['word_index']<s['end']]
        source=min(candidates,key=lambda s:s['end']-s['start'])
        block=next(b for b in req['blocks'] if b['block_id']==n['block_id'])
        context=' '.join(w['token'] for w in block['locators'][max(0,n['word_index']-2):n['word_index']+5])
        numbers.append([aliases[n['token_id']],n['value'],aliases[source['span_id']],context])
    return {'request_sha256':req['request_sha256'],'source_blocks':blocks,
        'number_columns':['number_id','value','sentence_id','source_context'],'numbers':numbers,
        'rows':[{'row_id':aliases[r['row_id']],'label':r['label'],'table':r['table'],'source_row':r['source_row'],
                 'parameters':[[p['parameter_key'],p['value'],p['unit']] for p in r['parameters']]} for r in req['rows']],
        'observation_groups':[{k:v for k,v in g.items() if k not in ('group_id','occurrences')} |
            {'group_id':aliases[g['group_id']],'row_ids':sorted({aliases[o['row_id']] for o in g['occurrences']})} for g in req['observation_groups']]}


def translate_wire(value, mapping):
    if isinstance(value,dict):return {k:translate_wire(v,mapping) for k,v in value.items()}
    if isinstance(value,list):return [translate_wire(v,mapping) for v in value]
    return mapping.get(value,value) if isinstance(value,str) else value


def wire_schema(req):
    return translate_wire(schema(req),wire_maps(req)[0])


def compile_plan(req, response):
    original_response_hash = digest(response)
    from curing_scope_references import repair
    response,scope_repairs=repair(req,response)
    response = expand_condition_cases(response, req)
    if digest({k:v for k,v in req.items() if k != 'request_sha256'}) != req['request_sha256'] or response['request_sha256'] != req['request_sha256']:
        raise ValueError('design request changed')
    if index_spans(req['blocks'], req['pdf_asset']['sha256']) != req['spans']:
        raise ValueError('source index changed')
    def unique(rows, key):
        result = {r[key]:r for r in rows}
        if len(result) != len(rows): raise ValueError('duplicate '+key)
        return result
    spans = unique(req['spans'], 'span_id'); numbers = unique(req['tokens'], 'token_id')
    def words(ids):
        if not ids or not set(ids) <= set(spans): raise ValueError('missing source spans')
        return [w for i in ids for w in spans[i]['locators']]
    stages = unique(response['stages'], 'stage_id'); routes = unique(response['routes'], 'route_id')
    designs = unique(response['designs'], 'design_id'); rows = unique(req['rows'], 'row_id')
    covered = []; row_design = {}
    for d in designs.values():
        words(d['source_span_ids'])
        if not d['row_ids'] or not set(d['row_ids']) <= set(rows) or not d['route_ids'] or not set(d['route_ids']) <= set(routes):
            raise ValueError('design scope invalid')
        covered += d['row_ids']; row_design.update({i:d for i in d['row_ids']})
    if sorted(covered) != sorted(rows): raise ValueError('row coverage duplicate or incomplete')
    entities = {}; type_projections = {}
    for rid, r in routes.items():
        words(r['source_span_ids'])
        if not r['stage_ids'] or len(r['stage_ids']) != len(set(r['stage_ids'])) or not set(r['stage_ids']) <= set(stages):
            raise ValueError('route stage order invalid')
        type_projections[rid]={'value':r.get('specimen_type'),'proposal':None}
        if r.get('specimen_type'):
            type_words=words(r['source_span_ids'])+[w for sid in r['stage_ids'] for w in words(stages[sid]['source_span_ids'])]
            if not any(w['token'].casefold().strip(' ,;.()') in {r['specimen_type'],r['specimen_type']+'s'} for w in type_words):
                # Keep source operations and the model proposal without promoting
                # a process-derived or unsupported type into the direct field.
                type_projections[rid]={'value':None,'proposal':{
                    'value':r['specimen_type'],'status':'QUARANTINED',
                    'basis':'MODEL_PROPOSAL_WITHOUT_DIRECT_TYPE_TOKEN',
                    'confirmed':False,'source_span_ids':copy.deepcopy(r['source_span_ids']),
                    'preparation_stage_ids':[sid for sid in r['stage_ids'] if stages[sid]['kind']=='PREPARATION'],
                    'note':'Preparation facts remain available; no operation keyword automatically confirms a material state.'}}
        out = []; previous = None
        for sid in r['stage_ids']:
            s = stages[sid]; words(s['source_span_ids']); props = {}
            if s['operation']=='testing' and s['kind']!='TESTING':raise ValueError('testing operation mislabeled as curing/storage')
            for p in s['properties']:
                if p['name'] in props: raise ValueError('duplicate property')
                ws = words(p['source_span_ids']); tid = p['numeric_token_id']
                if tid is not None and (tid not in numbers or (numbers[tid]['locator'] not in ws and
                        numbers[tid]['block_id'] not in {spans[i]['block_id'] for i in p['source_span_ids']})):
                    raise ValueError('number outside selected source')
                val = None if tid is None else numbers[tid]['value']
                if tid is not None:
                    block=next(b for b in req['blocks'] if numbers[tid]['locator'] in b['locators'])
                    ix=block['locators'].index(numbers[tid]['locator'])
                    nearby=' '.join(w['token'] for w in block['locators'][ix:ix+5])
                    patterns={'s':r'\b(?:s|seconds?)\b','min':r'\b(?:min|minutes?)\b','h':r'\b(?:h|hours?)\b',
                        'd':r'\b(?:d|days?)\b','degC':r'(?:[°◦]\s*C|℃|\bC\b)','%':r'%',
                        'Pa':r'\bPa\b','kPa':r'\bkPa\b','MPa':r'\bMPa\b'}
                    if p['unit'] not in patterns or not re.search(patterns[p['unit']],nearby):raise ValueError('stage unit differs from source')
                # A selected numeric anchor plus companion context in the SAME PDF
                # block is a composite selector, as in the route projection module.
                ids=list(p['source_span_ids'])
                if tid is not None and numbers[tid]['locator'] not in ws:
                    full=next(s for s in req['spans'] if s['block_id']==numbers[tid]['block_id'] and s['start']==0 and s['end']==len(next(b['locators'] for b in req['blocks'] if b['block_id']==s['block_id'])))
                    ids=list(dict.fromkeys(ids+[full['span_id']]))
                value = {'value': val, 'unit': p['unit'], 'fact_ids': ids}
                if p['name'] == 'duration': value['time_origin'] = 'STAGE_START'
                if val is None:
                    if p['relation'] is not None or not p['missing_reason']: raise ValueError('unknown property needs reason')
                    value['missing_reason'] = p['missing_reason']
                else:
                    if p['missing_reason'] is not None: raise ValueError('reported property marked missing')
                    value['relation'] = p['relation'] or '='
                    if value['relation'] != '=' and value['relation'] not in {w['token'].replace('≥','>=').replace('≤','<=') for w in ws}:
                        raise ValueError('comparison source missing')
                uid = p['uncertainty_token_id']
                if uid is not None:
                    if uid not in numbers or numbers[uid]['locator'] not in ws or not any('±' in w['token'] for w in ws):
                        raise ValueError('uncertainty source missing')
                    value['uncertainty'] = numbers[uid]['value']
                props[p['name']] = value
            out.append({'stage_id':sid,'after_stage_id':previous,'kind':s['kind'],'fact_ids':s['source_span_ids'],'properties':props})
            previous = sid
        entities[rid] = {'kind':'curing_route','stages':out}
    normalized = normalized_routes(entities, spans)
    groups = unique(req['observation_groups'], 'group_id'); decisions = unique(response['observations'], 'group_id')
    if set(groups) != set(decisions): raise ValueError('observation groups incomplete')
    for gid, d in decisions.items():
        if not d['reason']: raise ValueError('observation explanation missing')
        if d['route_id'] is None: continue
        words(d['source_span_ids'])
        if d['route_id'] not in routes: raise ValueError('unknown observation route')
        for o in groups[gid]['occurrences']:
            if d['route_id'] not in row_design[o['row_id']]['route_ids']: raise ValueError('observation crosses design scope')
            if not o['value_source_verified']: raise ValueError('observation original number unverified')
    return {'version':VERSION,'request_sha256':req['request_sha256'],'response_sha256':original_response_hash,
            'scope_repairs':scope_repairs,
            'normalized_routes':normalized,'type_projections':type_projections,
            'response':copy.deepcopy(response),'publication_allowed':False}


def consume(records, req, response, *, request_builder=None):
    """Production entry: exact source reconstruction, atomic fields, no scientific copying."""
    receipt = records.get('extensions', {}).get('source_design_relations')
    if receipt:
        check = copy.deepcopy(records); check['extensions'].pop('source_design_relations')
        if receipt['request_sha256'] != req['request_sha256'] or receipt['response_sha256'] != digest(response) or digest(check) != receipt['output_sha256']:
            raise ValueError('design reentry output changed')
        return receipt
    if (request_builder or prepare)(records, req['pages']) != req: raise ValueError('design production inputs changed')
    original_response_hash = digest(response)
    plan = compile_plan(req, response); response = plan['response']; staged = copy.deepcopy(records)
    spans = {s['span_id']:s for s in req['spans']}; tokens = {t['token_id']:t for t in req['tokens']}
    stages = {s['stage_id']:s for s in response['stages']}; routes = {r['route_id']:r for r in response['routes']}
    normalized = {r['route_id']:r for r in plan['normalized_routes']}
    type_projections=plan['type_projections']
    designs = {row:d for d in response['designs'] for row in d['row_ids']}
    rows = {r['row_id']:r for r in req['rows']}; mixes = {m['mix_key']:m for m in staged['mixes']}
    def evidence(mix,path,ids,locators=None,transformation=None,original_unit=None):
        ws = locators if locators is not None else [w for i in ids for w in spans[i]['locators']]
        if not ws: raise ValueError('empty field evidence')
        key = 'ev-design-'+digest([mix['mix_key'],path,ws])[:24]; first = ws[0]
        from indexed_native_sources import evidence_location
        location=evidence_location(ws,req['pdf_asset']['asset_key']);snippet=location['snippet']
        staged['evidence_links'].append({'evidence_key':key,'asset_key':req['pdf_asset']['asset_key'],
            'paper_key':mix['paper_key'],'record_type':'mix','record_key':mix['mix_key'],'field_path':path,
            **location,
            'snippet':snippet,'snippet_sha256':hashlib.sha256(snippet.encode()).hexdigest(),
            'section':None,'table_number':location['source_locator'].get('table_number'),'figure_number':None,'extensions':{'support_tokens':ws,'source_span_ids':ids},'extraction_method':VERSION})
        mix['field_provenance'][path]={'evidence_key':key,'original_value':snippet,'original_unit':original_unit,
            'transformation':transformation,'review_status':'pending_source_path_review','extraction_method':VERSION}
    def route_fields(rid):
        route=normalized[rid]; result=[]
        for s in route['stages']:
            props=s['properties']; meta=stages[s['stage_id']]
            result.append({'method':meta['method_zh'],'duration_seconds':props['duration']['value'],
                'temperature_C':props.get('temperature',{}).get('value'),'humidity_percent':props.get('relative_humidity',{}).get('value'),
                'extensions':{'stage_id':s['stage_id'],'kind':s['kind'],'operation':meta['operation'],
                    'properties':copy.deepcopy(props),'temperature_tolerance_C':props.get('temperature',{}).get('uncertainty'),
                    'humidity_relation':props.get('relative_humidity',{}).get('relation'),'time_origin':'STAGE_START'}})
        return result
    def age_endpoint_unresolved(rid):
        # An age alone does not identify which test it belongs to when a route
        # contains intervening preparation/storage. Preserve the source age,
        # but do not promote it to the age of the final analytical endpoint.
        sequence=[stages[s] for s in routes[rid]['stage_ids']]
        tests=[i for i,s in enumerate(sequence) if s['kind']=='TESTING']
        return bool(tests and any(s['kind']!='TESTING' for s in sequence[tests[0]:tests[-1]+1]))
    def stage_evidence(m,path,sid):
        s=stages[sid]; evidence(m,path,s['source_span_ids'])
        for p in s['properties']:
            tid=p['numeric_token_id']
            if tid is None: continue
            field={'duration':'duration_seconds','temperature':'temperature_C','relative_humidity':'humidity_percent'}.get(p['name'])
            if field:
                norm=next(s['properties'][p['name']] for route in normalized.values() for s in route['stages'] if s['stage_id']==sid)
                evidence(m,path+'/'+field,p['source_span_ids'],[tokens[tid]['locator']],norm['transformation'],p['unit'])
    for row_id,row in rows.items():
        m=mixes[row['mix_key']]; d=designs[row_id]; old=m['modules']['mixing_curing']
        m['extensions'].setdefault('source_design_history',[]).append({'mixing_curing':copy.deepcopy(old),
            'identity':copy.deepcopy(m['modules']['identity_source_specimen']),
            'provenance':{p:copy.deepcopy(v) for p,v in m['field_provenance'].items() if p.startswith(('/modules/mixing_curing','/modules/identity_source_specimen/specimen'))}})
        for p in list(m['field_provenance']):
            if p.startswith(('/modules/mixing_curing','/modules/identity_source_specimen/specimen')): del m['field_provenance'][p]
        identity=m['modules']['identity_source_specimen']; kinds={type_projections[r]['value'] for r in d['route_ids']}
        identity['specimen_type']=next(iter(kinds)) if len(kinds)==1 else None
        identity['specimens']=[{'specimen_id':r,'description_zh':routes[r]['description_zh'],'specimen_type':type_projections[r]['value'],
            **({'extensions':{'specimen_type_proposal':copy.deepcopy(type_projections[r]['proposal'])}} if type_projections[r]['proposal'] else {})} for r in d['route_ids']]
        for i,r in enumerate(d['route_ids']): evidence(m,'/modules/identity_source_specimen/specimens/'+str(i),routes[r]['source_span_ids'])
        if identity['specimen_type'] is not None: evidence(m,'/modules/identity_source_specimen/specimen_type',list(dict.fromkeys(i for r in d['route_ids'] for i in routes[r]['source_span_ids'])))
        m['modules']['materials']['extensions']['design_scope']={'design_id':d['design_id'],'description_zh':d['description_zh'],'row_id':row_id,'quantity_transfer':False}
        evidence(m,'/modules/materials/extensions/design_scope',d['source_span_ids'])
        curing=empty_source_mix(m['paper_key'],m['mix_key'],'',None,None)['modules']['mixing_curing']
        m['modules']['mixing_curing']=curing
        all_ids=[routes[r]['stage_ids'] for r in d['route_ids']]; common=[]
        for items in zip(*all_ids):
            if len(set(items))!=1: break
            common.append(items[0])
        curing['extensions']['source_routes']=[]
        for rid in d['route_ids']:
            branch={'route_id':rid,'description_zh':routes[rid]['description_zh'],'stages':route_fields(rid),'specimen_age_seconds':None,
                    'endpoint_status':routes[rid].get('endpoint_status','UNKNOWN')}
            conditions=routes[rid].get('declared_conditions',[])
            if 'condition_family' in routes[rid]:
                branch['declared_conditions']=copy.deepcopy(conditions)
                branch['condition_family']=routes[rid]['condition_family']
                branch['local_case_id']=routes[rid]['local_case_id']
                age=next((c for c in conditions if c['name']=='specimen_age'),None)
                if age:
                    if age_endpoint_unresolved(rid):
                        branch['age_scope']={'status':'UNRESOLVED_TEST_STAGE',
                            'reason':'Multiple tests separated by operations; source condition retained without assigning final endpoint age.'}
                    else:branch['specimen_age_seconds']=age['normalized_value']
            bi=len(curing['extensions']['source_routes']);curing['extensions']['source_routes'].append(branch)
            if type_projections[rid]['proposal']:
                branch['specimen_type_proposal']=copy.deepcopy(type_projections[rid]['proposal'])
            if routes[rid].get('subject_scope'):
                branch['subject_scope']=copy.deepcopy(routes[rid]['subject_scope'])
                branch['scope_normalization']=copy.deepcopy(routes[rid]['scope_normalization'])
                evidence(m,f'/modules/mixing_curing/extensions/source_routes/{bi}/subject_scope',
                    branch['subject_scope']['source_span_ids'])
            for ci,c in enumerate(conditions):
                evidence(m,f'/modules/mixing_curing/extensions/source_routes/{bi}/declared_conditions/{ci}/normalized_value',
                    c['source_span_ids'],[tokens[c['numeric_token_id']]['locator']],
                    'source unit normalization; not operation duration',c['unit'])
                if c['name']=='specimen_age' and branch['specimen_age_seconds'] is not None:
                    evidence(m,f'/modules/mixing_curing/extensions/source_routes/{bi}/specimen_age_seconds',
                        c['source_span_ids'],[tokens[c['numeric_token_id']]['locator']],
                        'source age converted to seconds; no duration subtraction',c['unit'])
            for si,sid in enumerate(routes[rid]['stage_ids']): stage_evidence(m,f'/modules/mixing_curing/extensions/source_routes/{bi}/stages/{si}',sid)
        for sid in common:
            item=next(v for v in route_fields(d['route_ids'][0]) if v['extensions']['stage_id']==sid)
            if item['extensions']['kind']=='TESTING':
                ix=len(curing['extensions'].setdefault('test_stages',[]));curing['extensions']['test_stages'].append(item)
                stage_evidence(m,f'/modules/mixing_curing/extensions/test_stages/{ix}',sid)
            elif item['extensions']['kind']=='PREPARATION':
                slot='forming' if item['extensions']['operation']=='forming' else 'mixing'
                if curing[slot] is None:curing[slot]={'method':'','extensions':{'source_stages':[]}}
                curing[slot]['method']='；'.join(filter(None,[curing[slot]['method'],item['method']]))
                curing[slot]['extensions']['source_stages'].append(item)
                ix=len(curing[slot]['extensions']['source_stages'])-1
                stage_evidence(m,f'/modules/mixing_curing/{slot}/extensions/source_stages/{ix}',sid)
            else:
                ix=len(curing['curing_stages']);curing['curing_stages'].append(item)
                stage_evidence(m,f'/modules/mixing_curing/curing_stages/{ix}',sid)
        curing['curing_route']=d['route_ids'][0] if len(d['route_ids'])==1 else None
        curing['extensions']['route_selection']='observation_specific' if len(d['route_ids'])>1 else 'single_source_route'
    decisions={d['group_id']:d for d in response['observations']}; bound=0;pending=[];partial=0
    for g in req['observation_groups']:
        d=decisions[g['group_id']]
        for o in g['occurrences']:
            m=mixes[rows[o['row_id']]['mix_key']]; value=m['modules']['performance'][o['index']]
            value.setdefault('extensions',{})['source_route_binding']={'route_id':d['route_id'],'reason':d['reason'],
                'observation_object':value.get('specimen'),'review_status':'pending_source_path_review','design_id':designs[o['row_id']]['design_id']}
            if d['route_id'] is not None and routes[d['route_id']].get('condition_family'):
                value['extensions']['source_route_binding'].update(family_id=routes[d['route_id']]['condition_family'],
                    case_id=routes[d['route_id']]['local_case_id'])
            if d['route_id'] is not None:
                status=routes[d['route_id']].get('endpoint_status','UNKNOWN')
                value['extensions']['source_route_binding']['endpoint_status']=status
                if status=='UNKNOWN':partial+=1
                evidence(m,f"/modules/performance/{o['index']}/extensions/source_route_binding",d['source_span_ids']);bound+=1
            else:pending.append({'mix_key':m['mix_key'],'index':o['index'],'reason':d['reason']})
    receipt={'request_sha256':req['request_sha256'],'response_sha256':original_response_hash,'updated_records':len(rows),
        'bound_observations':bound,'partial_scope_observations':partial,'pending_observations':pending,'publication_allowed':False}
    receipt['unconfirmed_type_routes']=sum(p['proposal'] is not None for p in type_projections.values())
    receipt['unresolved_age_endpoint_routes']=sum(
        any(c['name']=='specimen_age' for c in r.get('declared_conditions',[])) and age_endpoint_unresolved(rid)
        for rid,r in routes.items())
    staged.setdefault('extensions',{})
    receipt['output_sha256']=digest(staged);staged['extensions']['source_design_relations']=receipt
    records.clear();records.update(staged)
    return receipt


def refine(req, raw, review):
    """Apply one source-bound Agent development correction set; retain raw response."""
    result=copy.deepcopy(raw)
    if review:
        if review['response_sha256']!=digest(raw) or review['pdf_sha256']!=req['pdf_asset']['sha256']:
            raise ValueError('development review bound to another response/source')
        span_ids={s['span_id'] for s in req['spans']}
        allowed={'stages':{'kind','operation','method_zh','properties','source_span_ids'},
                 'routes':{'specimen_type','description_zh','stage_ids','source_span_ids'},
                 'observations':{'route_id','reason','source_span_ids'}}
        keys={'stages':'stage_id','routes':'route_id','observations':'group_id'}
        touched=set()
        expanded=[]
        for e in review['edits']:
            expanded.extend([{**e,'id':i} for i in e.get('ids',[e.get('id')])])
        for edit in expanded:
            collection=edit['collection'];field=edit['field'];target=(collection,edit['id'],field)
            if collection not in allowed or field not in allowed[collection] or target in touched:
                raise ValueError('invalid or duplicate development edit')
            if not edit['reason'] or not edit['source_span_ids'] or not set(edit['source_span_ids'])<=span_ids:
                raise ValueError('development edit lacks source basis')
            matches=[r for r in result[collection] if r[keys[collection]]==edit['id']]
            if len(matches)!=1:raise ValueError('development target ambiguous')
            matches[0][field]=copy.deepcopy(edit['value'])
            if field!='source_span_ids':
                matches[0]['source_span_ids']=list(dict.fromkeys(matches[0]['source_span_ids']+edit['source_span_ids']))
            touched.add(target)
    result['request_sha256']=req['request_sha256']
    return result


def run(records_path,pages,out,response_dir=None,review_path=None):
    started=time.monotonic();out=Path(out)
    records=json.loads(Path(records_path).read_bytes());request=prepare(records,pages)
    payload=visible(request)
    if len(json.dumps(payload,ensure_ascii=False).encode())>100000:raise ValueError('bounded design payload exceeds budget')
    if response_dir:
        rd=Path(response_dir)
        saved=json.loads((rd/'request.json').read_bytes())
        # Numeric lexer upgrades may expose previously invisible source tokens;
        # require identical sources/rows and every old token unchanged.
        if {k:v for k,v in saved.items() if k not in ('tokens','request_sha256')}!={k:v for k,v in request.items() if k not in ('tokens','request_sha256')} or any(t not in request['tokens'] for t in saved['tokens']):
            raise ValueError('replay input changed')
        raw_response=json.loads((rd/'response.json').read_bytes())
        if raw_response['request_sha256']!=saved['request_sha256']:raise ValueError('raw response input mismatch')
    else:raw_response=invoke(request,out/'model',schema=schema(request),visible=payload,prompt_text=PROMPT)
    review=None if review_path is None else json.loads(Path(review_path).read_bytes())
    response=refine(request,raw_response,review)
    atomic_json(out/'bound-response.json',response)
    atomic_json(out/'response-binding.json',{'raw_response_sha256':digest(raw_response),'compiled_response_sha256':digest(response),
        'request_sha256':request['request_sha256'],'review_sha256':None if review is None else digest(review),'publication_allowed':False})
    try:
        from consume_curing_relations import consume_design
        original=copy.deepcopy(records); report=consume_design(records,request,response)
        again=copy.deepcopy(records);consume_design(again,request,response)
        assert again==records
        for old,new in zip(original['mixes'],records['mixes']):
            a=copy.deepcopy(new['modules']['materials']);a['extensions'].pop('design_scope')
            assert a==old['modules']['materials']
            for p,q in zip(old['modules']['performance'],new['modules']['performance']):
                q=copy.deepcopy(q);q['extensions'].pop('source_route_binding');assert p==q
        atomic_json(out/'generated-records.json',records)
        report={**report,'status':'SOURCE_DESIGN_FIELDS_CONSUMED','records_sha256':digest(records),'reentry_identical':True,
                'model_calls':0 if response_dir else 1,'elapsed_seconds':time.monotonic()-started}
        atomic_json(out/'validation.json',report);return report
    except Exception as exc:
        atomic_json(out/'failure.json',{'error':str(exc),'request_sha256':request['request_sha256'],'response_sha256':digest(response)})
        raise


def production(records, config, directory, runner=None):
    """Scheduler path: prepare -> one grouped producer -> compile -> atomic consume.

    Only raw responses from this protocol are replayable. Development review files
    and decoded/corrected outputs are not inputs to this path.
    """
    if not config or not config.get('enabled'):return {'status':'NOT_REQUESTED','model_calls':0}
    if set(config)-{'enabled','pages','response_directory'}:raise ValueError('unsupported production design configuration')
    directory=Path(directory);started=time.monotonic();model_dir=Path(config.get('response_directory') or directory/'model')
    if records.get('extensions',{}).get('source_design_relations'):
        if not (directory/'request.json').exists() or not (directory/'decoded-response.json').exists():raise ValueError('production reentry metadata missing')
        req=json.loads((directory/'request.json').read_bytes());response=json.loads((directory/'decoded-response.json').read_bytes())
        raw=json.loads((directory/'raw-response.json').read_bytes())
        if translate_wire(raw,wire_maps(req)[1])!=response:raise ValueError('production decoded response differs from raw producer')
        if compile_plan(req,response)!=json.loads((directory/'plan.json').read_bytes()):raise ValueError('production plan changed')
        if hashlib.sha256(Path(req['pdf_asset']['relative_path']).read_bytes()).hexdigest()!=req['pdf_asset']['sha256']:raise ValueError('reentry PDF changed')
        receipt=consume(records,req,response)
        return {**receipt,'status':'SOURCE_DESIGN_PRODUCTION_REUSED','model_calls':0,'elapsed_seconds':time.monotonic()-started}
    req=prepare(records,config.get('pages') or select_source_pages(records));payload=wire_source(req)
    if len(json.dumps(payload,ensure_ascii=False).encode())>100000:raise ValueError('source production payload exceeds budget')
    atomic_json(directory/'request.json',req)
    model_calls=0
    if (model_dir/'response.json').exists():
        if json.loads((model_dir/'request.json').read_bytes())!=req:raise ValueError('raw production response has different source request')
        raw=json.loads((model_dir/'response.json').read_bytes())
    else:
        if config.get('response_directory'):raise ValueError('requested production response absent')
        if (model_dir/'request.json').exists():raise ValueError('prior producer attempt incomplete; no automatic second model call')
        raw=(runner or invoke)(req,model_dir,schema=wire_schema(req),visible=payload,prompt_text=PRODUCTION_PROMPT)
        model_calls=1
    token_proxy=0
    if runner is None:
        events=[json.loads(line) for line in (model_dir/'events.jsonl').read_text(encoding='utf8').splitlines() if line.startswith('{')]
        messages=[e['item']['text'] for e in events if e.get('item',{}).get('type')=='agent_message']
        usage=json.loads((model_dir/'usage.json').read_bytes())
        if len(messages)!=1 or json.loads(messages[0])!=raw or usage['returncode']!=0 or usage['model_turns']!=1:
            raise ValueError('raw producer completion does not bind response')
        if any(e.get('item',{}).get('type') in ('command_execution','file_change','mcp_tool_call','web_search') for e in events):raise ValueError('producer source-only boundary exceeded')
        if model_calls:token_proxy=sum(u.get('input_tokens',0)-u.get('cached_input_tokens',0)+u.get('output_tokens',0) for u in usage['usage'])
    response=translate_wire(raw,wire_maps(req)[1])
    try:
        plan=compile_plan(req,response)
        staged=copy.deepcopy(records);receipt=consume(staged,req,response)
        atomic_json(directory/'decoded-response.json',response)
        atomic_json(directory/'raw-response.json',raw)
        atomic_json(directory/'plan.json',plan)
        report={**receipt,'status':'SOURCE_DESIGN_PRODUCTION_CONSUMED','model_calls':model_calls,
            'elapsed_seconds':time.monotonic()-started,'source_pages':req['pages'],
            'raw_response_sha256':digest(raw),'development_review_used':False,'token_proxy':token_proxy,
            'visible_bytes':len(json.dumps(payload,ensure_ascii=False).encode())}
        atomic_json(directory/'production.json',report)
        records.clear();records.update(staged)
        return report
    except Exception as exc:
        atomic_json(directory/'failure.json',{'error':str(exc),'request_sha256':req['request_sha256'],'raw_response_sha256':digest(raw),'model_calls':model_calls})
        raise


def artifact_receipt(records, run_dir):
    if not records.get('extensions',{}).get('source_design_relations'):return []
    result=[]
    for name in ('request.json','raw-response.json','decoded-response.json','plan.json','production.json'):
        relative=Path('source-design-stage')/name;path=Path(run_dir)/relative
        if not path.is_file():raise ValueError('design production artifact missing: '+name)
        result.append({'path':relative.as_posix(),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--records',required=True);p.add_argument('--pages',type=int,nargs='+',required=True)
    p.add_argument('--output',required=True);p.add_argument('--response-dir');p.add_argument('--development-review');a=p.parse_args()
    report=run(a.records,a.pages,a.output,a.response_dir,a.development_review)
    print(json.dumps({**report,'pending_observations':len(report['pending_observations'])},ensure_ascii=False))
