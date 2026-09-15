"""Deterministic task routing and draft fill state; never canonical acceptance."""
import copy
import hashlib
import inspect
import json
import re
from pathlib import Path
import fitz
from source_quantity_producer import atomic_json
from source_specimen_variants import digest
from formulation_source_plan import index_spans
from multimodal_extraction_plan import _table_token_matrix

VERSION='paper-block-dispatch-v1'
IMPLEMENTATION_SHA256=hashlib.sha256(Path(__file__).read_bytes()+
    Path(__file__).with_name('source_design_relations.py').read_bytes()+
    Path(__file__).with_name('planned_material_text.py').read_bytes()+
    Path(__file__).with_name('material_candidate_identity.py').read_bytes()+
    Path(__file__).with_name('source_table_cells.py').read_bytes()+
    Path(__file__).with_name('indexed_native_sources.py').read_bytes()+
    Path(__file__).with_name('planned_xrd_curves.py').read_bytes()).hexdigest()
STATES={'planned','filled','not_reported','ambiguous','failed'}


def pointer(record,path):
    value=record
    for bit in path.strip('/').split('/'):
        bit=bit.replace('~1','/').replace('~0','~')
        value=value[int(bit)] if isinstance(value,list) else value[bit]
    return value


def canonical_pointer(record,path):
    """Resolve older symbolic row identities without inventing a row order."""
    value=record;resolved=[]
    for bit in path.strip('/').split('/'):
        bit=bit.replace('~1','/').replace('~0','~')
        if isinstance(value,list):
            if bit.isdigit():index=int(bit)
            else:
                matches=[i for i,row in enumerate(value) if isinstance(row,dict) and any(row.get(k)==bit for k in ('component','name'))]
                if len(matches)!=1:raise ValueError('symbolic row identity ambiguous')
                index=matches[0]
            value=value[index];resolved.append(str(index))
        else:value=value[bit];resolved.append(bit.replace('~','~0').replace('/','~1'))
    if isinstance(value,dict) and 'original_value' in value:
        value=value['original_value'];resolved.append('original_value')
    return value,'/'+('/'.join(resolved))


def source_matches(link,source):
    def native_slice(text,locator):
        if 'start' not in locator and 'end' not in locator:return text.strip()
        start,end=locator.get('start'),locator.get('end')
        if type(start) is not int or type(end) is not int or not 0<=start<end<=len(text):return None
        return ' '.join(text[start:end].split())
    if source.get('coordinate_space')=='docx_paragraph':
        locator=link.get('source_locator') or {}
        if locator.get('coordinate_space')!='docx_paragraph' or locator.get('body_index')!=source['source_object']['body_index']:return False
        token=native_slice(source['text'],locator)
        return bool(token) and token==' '.join(str(link.get('snippet','')).split())
    if source.get('coordinate_space')=='docx_table':
        locator=link.get('source_locator') or {};table=source['source_object']
        label=link.get('table_number') or locator.get('table_number')
        if locator.get('coordinate_space')!='docx_table' or label!=table['source_label']:return False
        if locator.get('table_number') not in (None,label):return False
        # Native cell identity and exact original token, not a fake page/bbox.
        row,col=locator.get('row'),locator.get('cell',locator.get('column'))
        if type(row) is not int or type(col) is not int or row<0 or col<0:return False
        rows=table['rows']
        if row>=len(rows) or col>=len(rows[row]):return False
        token=native_slice(rows[row][col],locator)
        return bool(token) and token==' '.join(str(link.get('snippet','')).split())
    if not link.get('bbox') or not source.get('bbox'):return False
    a=fitz.Rect(link['bbox']);b=fitz.Rect(source['bbox'])
    if source['kind']=='figure' and source.get('source_object',{}).get('bbox'):
        caption=fitz.Rect(source['source_object']['bbox'])
        overlap=a&caption
        caption_page=source['source_object'].get('caption_page',source['page'])
        if link.get('page')==caption_page and not overlap.is_empty and overlap.get_area()>=.5*a.get_area():return True
    if link.get('page')!=source.get('page'):return False
    if source['kind']=='table':
        # Caption is not the data body. Only use the existing table identity;
        # text geometry otherwise proves the precise value's source block.
        named=str(link.get('table_number','')).casefold()==str(source['source_object']['source_label']).casefold()
        if named:return True
        if not source.get('native_body_sha256'):return False
    intersection=a&b
    return not intersection.is_empty and intersection.get_area()>=.5*a.get_area()


def reusable_values(records,index,task,slots,objects,records_sha256=None):
    entries=[];sources={s['source_id']:s for s in index['sources']};records_sha256=records_sha256 or digest(records)
    assets={a['asset_key']:a for a in records.get('assets',[])}
    links={e['evidence_key']:e for e in records.get('evidence_links',[])}
    all_records={r[key]:r for group,key in [('mats','mat_key'),('mixes','mix_key'),('papers','paper_key')] for r in records.get(group,[])}
    for slot in slots:
        for record_key in objects[slot['object_id']]['existing_keys']:
            record=all_records[record_key]
            method_scopes={}
            for path,prov in record.get('field_provenance',{}).items():
                prefix=slot['field_path']
                if path!=prefix and not (slot['cardinality']=='collection' and path.startswith(prefix+'/')):continue
                if slot['property_name']:
                    parts=path.strip('/').split('/')
                    if len(parts)<3 or parts[:2]!=['modules','performance']:continue
                    if record['modules']['performance'][int(parts[2])]['name']!=slot['property_name']:continue
                source_scope=set(task['source_ids']+slot['source_ids']+slot['context_source_ids'])
                if task['kind']=='text' and prefix=='/modules/performance':
                    parts=path.strip('/').split('/')
                    if len(parts)<3 or not parts[2].isdigit():continue
                    if 'condition_revision_history' in parts:continue
                    from source_design_relations import observation_method_scope
                    i=int(parts[2])
                    if i not in method_scopes:
                        method_scopes[i]=observation_method_scope(records,index,task,slot,record,i,links)
                    scope=method_scopes[i]
                    if not scope['matched']:continue
                    source_scope=set(scope['source_ids'])
                link=links.get(prov.get('evidence_key'))
                if not link or link.get('record_key')!=record_key:continue
                if task['kind']=='text' and prefix=='/modules/performance':
                    linked_route=link.get('extensions',{}).get('route_id')
                    if linked_route is not None and linked_route!=scope['route_id']:continue
                pdf=assets.get(link.get('asset_key'),{})
                matched=[sid for sid in source_scope
                         if sources[sid]['document_sha256']==pdf.get('sha256') and source_matches(link,sources[sid])]
                if not matched:continue
                try:value,resolved_path=canonical_pointer(record,path)
                except (KeyError,IndexError,ValueError,TypeError):continue
                if value is None:continue  # null alone is never a not_reported finding
                try:parent=pointer(record,resolved_path.rsplit('/',1)[0])
                except (KeyError,IndexError,ValueError,TypeError):parent={}
                method=prov.get('extraction_method','')
                directness='derived' if prov.get('formula') else 'estimate' if any(x in method for x in ('raster','digitiz')) else 'direct'
                entries.append({'parent_slot_id':slot['slot_id'],'record_key':record_key,'field_path':resolved_path,'source_field_path':path,
                    'value':copy.deepcopy(value),'unit':(parent.get('unit') if isinstance(parent,dict) else None) or prov.get('original_unit'),
                    'original_value':prov.get('original_value'),'original_unit':prov.get('original_unit'),
                    'source_ids':sorted(matched),'evidence':copy.deepcopy(link),'provenance':copy.deepcopy(prov),
                    'directness':directness,'reused_from_records_sha256':records_sha256,
                    'status':'filled','formal':False})
    return entries


def text_adapter(context):
    if context.get('material_text_directory'):
        from planned_material_text import eligible,producer
        if eligible(context['task'],context['slots']):return producer(context)
    if any(s['document_sha256']!=context['index']['document']['sha256'] or not s.get('bbox') for s in context['sources'] if s['kind']=='text'):
        from indexed_native_sources import native_blocks
        ids=[s['source_id'] for s in context['sources'] if s['kind']=='text']
        blocks,_=native_blocks(context['index'],context['records'],ids)
        return {'values':context['reusable'],'new_slots':[],
            'source_artifact':{'spans':index_spans(blocks,context['index']['document']['sha256'])},
            'remaining_reason':'Source-scoped reuse; remaining semantics require extraction Agent','complete_slots':[]}
    blocks=[]
    with fitz.open(context['index']['document']['path']) as doc:
        for s in context['sources']:
            if s['kind']!='text':continue
            words=doc[s['page']-1].get_text('words',clip=fitz.Rect(s['bbox']))
            blocks.append({'block_id':s['source_id'],'locators':[{'page':s['page'],'bbox':list(w[:4]),'token':w[4]} for w in words]})
    return {'values':context['reusable'],'new_slots':[],
            'source_artifact':{'spans':index_spans(blocks,context['index']['document']['sha256'])},
            'remaining_reason':'Source-scoped reuse only; remaining semantics require extraction Agent', 'complete_slots':[]}


def table_adapter(context):
    if context.get('material_text_directory'):
        from planned_material_text import eligible,producer
        if eligible(context['task'],context['slots']):return producer(context)
    tables=[];by_source={}
    frozen=context['task'].get('native_source_tables')
    if frozen:
        for item in frozen:
            if item['source_id'] not in context['task']['source_ids']:raise ValueError('frozen table escaped task')
            table=item['table']
            if table['table_sha256']!=digest({k:v for k,v in table.items() if k!='table_sha256'}):raise ValueError('frozen native table mutated')
            tables.append(copy.deepcopy(table))
            by_source[item['source_id']]=table
    else:
        from source_table_cells import native_matrix
        for s in context['sources']:
            if s['kind']!='table':continue
            if s['document_sha256']==context['index']['document'].get('sha256') and s.get('bbox'):
                with fitz.open(context['index']['document']['path']) as doc:
                    table=_table_token_matrix(doc[s['page']-1],s['source_object'],
                        [x['source_object'] for x in context['index']['sources'] if x['kind']=='table' and x['document_sha256']==s['document_sha256']])
            else:table=native_matrix(context['index'],s,context['index']['sources'],context['records'].get('assets',[]))
            tables.append(table);by_source[s['source_id']]=table
    # Table captions are not cell locators. Refine geometry through the existing
    # native table parser before matching old field evidence to this source ID.
    refined=copy.deepcopy(context['index'])
    for s in refined['sources']:
        if s.get('coordinate_space')=='docx_table':continue
        table=by_source.get(s['source_id'])
        if table:s.update(bbox=table['bbox'],native_body_sha256=table['table_sha256'])
    values=reusable_values(context['records'],refined,context['task'],context['slots'],context['objects'],context['records_sha256'])
    writes=native_table_writes(context,tables)
    return {'values':values,'scientific_writes':writes,'new_slots':[],'source_artifact':{'tables':tables},
            'remaining_reason':'Native table cells prepared; unexplained headings and rows stay planned','complete_slots':[]}


def native_table_writes(context,tables):
    """Consume explicit source-bound semantics, never infer them from column order."""
    writes=[]
    for binding in context['task'].get('native_bindings',[]):
        binding=copy.deepcopy(binding)
        slot=next(s for s in context['slots'] if s['slot_id']==binding['slot_id'])
        if binding['record_key'] not in context['objects'][slot['object_id']]['existing_keys']:
            raise ValueError('native table object outside plan')
        source=next(s for s in context['sources'] if s['source_id']==binding['source_id'] and s['kind']=='table')
        table=next(t for t in tables if t['table_sha256']==binding['table_sha256'])
        if table['page']!=source['page'] or table['source_label']!=source['source_object']['source_label']:
            raise ValueError('native table source mismatch')
        # Header references can cover several merged/header rows. Their meaning
        # must be supplied by the source planner, not reconstructed by position.
        if not binding.get('header_cells') or not binding.get('semantic_basis'):
            raise ValueError('native table requires explicit header semantics')
        for row,col,token in binding['header_cells']:
            if table['rows'][row][col]['text']!=token:raise ValueError('native header changed')
        cell=table['rows'][binding['cell'][0]][binding['cell'][1]]
        raw=cell['text']
        numeric=re.fullmatch(r'([+-]?(?:\d+(?:\.\d*)?|\.\d+))(%)?([*†‡]+)?',raw.strip())
        if raw.strip() in ('','/','-','–','—','−'):value=None
        elif numeric:
            value=float(numeric[1])
            if numeric[2] and binding['unit'] not in ('%','wt.%','vol.%'):
                binding['unit']={'mass ratio':'wt.%','volume ratio':'vol.%'}.get(binding['unit'],'%')
                binding['row_template'].update(unit=binding['unit'],original_unit=binding['unit'])
        elif (binding.get('row_template',{}).get('semantic_role')=='reported_ratio'
              and re.fullmatch(r'\s*\d+(?:\.\d+)?\s*([/:])\s*\d+(?:\.\d+)?(?:\s*\1\s*\d+(?:\.\d+)?)*\s*',raw)):
            # A component ratio is one source expression, not a scalar quotient.
            # Preserve it verbatim; component conversion belongs to a typed mapping.
            value=raw
        else:raise ValueError('native cell is not an unambiguous numeric token')
        if binding['field_path']!=slot['field_path'] and not binding['field_path'].startswith(slot['field_path']+'/'):
            raise ValueError('native field outside slot')
        writes.append({**copy.deepcopy(binding),'value':value,'original_value':raw,'bbox':cell['bbox'],
            'source_locator':copy.deepcopy(cell.get('source_locator')),
            'page':table['page'],'table_number':table['source_label'],'document_sha256':source['document_sha256']})
    return writes


def apply_native_writes(records,writes):
    """Transactional field writes; complete row templates come from semantics."""
    staged=copy.deepcopy(records);fills=[]
    for w in writes:
        matches=[r for group,key in [('mats','mat_key'),('mixes','mix_key')] for r in staged.get(group,[]) if r[key]==w['record_key']]
        if len(matches)!=1:raise ValueError('native write needs one planned scientific object')
        record=matches[0];path=w['field_path'];parent_path,leaf=path.rsplit('/',1)
        try:parent=pointer(record,parent_path)
        except IndexError:
            array_path,index=parent_path.rsplit('/',1);array=pointer(record,array_path)
            if int(index)!=len(array) or not isinstance(w.get('row_template'),dict):raise ValueError('native new row lacks explicit template')
            array.append(copy.deepcopy(w['row_template']));parent=array[-1]
        if not isinstance(parent,dict):raise ValueError('native destination must be typed field')
        if parent.get(leaf) is not None:
            if parent[leaf]!=w['value']:raise ValueError('native scientific conflict; original preserved')
            if parent.get('unit',w['unit'])!=w['unit']:raise ValueError('native unit conflict')
            continue
        parent[leaf]=w['value']
        assets=[a for a in staged['assets'] if a.get('sha256')==w['document_sha256']]
        if len(assets)!=1:raise ValueError('native source asset ambiguous')
        key='ev-native-'+digest(w)[:24]
        evidence={'evidence_key':key,'record_key':w['record_key'],'field_path':path,
            'asset_key':assets[0]['asset_key'],'page':w['page'],'bbox':w['bbox'],'table_number':w['table_number'],
            'extensions':{'source_ids':[w['source_id']],
                'header_cells':w['header_cells'],'semantic_basis':w['semantic_basis'],
                'semantic_request_sha256':w.get('semantic_request_sha256'),
                'source_cell_id':w.get('source_cell_id'),'source_row_ids':w.get('source_row_ids'),
                'assignment_kind':w.get('assignment_kind','direct_cell'),
                'context_source_ids':w.get('context_source_ids',[]),'formal':False}}
        if w.get('source_locator'):evidence['source_locator']=copy.deepcopy(w['source_locator'])
        evidence['snippet']=w['original_value']
        staged.setdefault('evidence_links',[]).append(evidence)
        if 'parameter_key' in parent:
            parent.update(original_value=w['original_value'],original_unit=w['unit'],evidence_key=key,
                extraction_method='native_table_explicit_semantics')
        record.setdefault('field_provenance',{})[path]={'evidence_key':key,'original_value':w['original_value'],
            'original_unit':w['unit'],'extraction_method':'native_table_explicit_semantics','review_status':'pending'}
        if w['value'] is not None:
            fills.append({'parent_slot_id':w['slot_id'],'record_key':w['record_key'],'field_path':path,
                'value':w['value'],'unit':w['unit'],'source_ids':[w['source_id']],'evidence':evidence,'status':'filled','formal':False})
    return staged,fills


def figure_adapter(context):
    if context.get('xrd_directory'):
        from planned_xrd_curves import eligible,producer
        if eligible(context['task'],context['sources']):return producer(context)
    # Geometry is prepared for the existing digitizers, not interpreted as data.
    from raster_bars import discover_filled_bars
    prepared=[]
    for s in context['sources']:
        if s['kind']!='figure':continue
        matches=[a for a in context['records'].get('assets',[]) if a.get('extensions',{}).get('source_pdf_asset_key') and
                 a['extensions'].get('page')==s['page'] and a['extensions'].get('figure_number')==s['source_object']['source_label']]
        item={'source_id':s['source_id'],'bbox':s['bbox'],'assets':[]}
        for a in matches:
            path=Path(a['relative_path'])
            if not path.is_absolute():path=Path(context['asset_root'])/path
            if not path.is_file():continue
            if hashlib.sha256(path.read_bytes()).hexdigest()!=a['sha256']:raise ValueError('figure asset hash changed')
            asset={'asset_key':a['asset_key'],'path':str(path),'sha256':a['sha256']}
            if path.suffix.lower()=='.png':asset['bar_candidates']=discover_filled_bars(path,include_short_regions=True)
            item['assets'].append(asset)
        prepared.append(item)
    return {'values':context['reusable'],'new_slots':[],'source_artifact':{'figures':prepared},
            'remaining_reason':'Figure geometry/assets prepared; legend and axis semantics require extraction Agent','complete_slots':[]}


ADAPTERS={'text':text_adapter,'table':table_adapter,'figure':figure_adapter}


def extract_pending_counts(index,plan,records,directory,allow_source_call=False):
    """Automatic planned text producer -> consumer -> immutable scientific output."""
    from paper_task_extraction import prepare_text_counts,count_schema,COUNT_PROMPT,consume_text_counts
    from paper_fill_plan import completed_response
    from source_quantity_producer import invoke
    directory=Path(directory)/'text-counts';receipt=directory/'production.json'
    if receipt.exists():
        report=json.loads(receipt.read_bytes())
        if digest(records) not in (report['input_records_sha256'],report['output_records_sha256']):raise ValueError('count stage input changed')
        output=json.loads((directory/'generated-records.json').read_bytes())
        if digest(output)!=report['output_records_sha256']:raise ValueError('count stage output changed')
        return output,{**report,'invocation_model_calls':0}
    req=prepare_text_counts(index,plan,records)
    request_path=directory/'request.json'
    model_calls=0
    if request_path.exists():
        if json.loads(request_path.read_bytes())!=req:raise ValueError('count request changed')
        if not (directory/'model/response.json').exists():raise ValueError('incomplete source attempt; no retry')
        response=completed_response(directory/'model')
    else:
        if not allow_source_call:raise ValueError('count extraction requires bounded source-call opt-in')
        atomic_json(request_path,req)
        visible={k:v for k,v in req.items() if k not in ('document','groups')}
        visible['groups']=[{k:v for k,v in g.items() if k!='targets'} for g in req['groups']]
        for g in visible['groups']:
            g['existing_conditions']=[{k:v for k,v in c.items() if k!='targets'} for c in g['existing_conditions']]
        invoke(req,directory/'model',schema=count_schema(),visible=visible,prompt_text=COUNT_PROMPT,timeout=600)
        model_calls=1
        response=completed_response(directory/'model')
    output,report=consume_text_counts(records,req,response)
    report.update(input_records_sha256=req['records_sha256'],output_records_sha256=digest(output),
                  request_sha256=req['request_sha256'],model_usage=json.loads((directory/'model/usage.json').read_bytes()))
    atomic_json(directory/'generated-records.json',output);atomic_json(receipt,report)
    return output,{**report,'invocation_model_calls':model_calls}


def extract_pending_labels(index,plan,records,directory,allow_source_call=False):
    import planned_figure_labels as labels
    from paper_fill_plan import completed_response
    from source_quantity_producer import invoke
    directory=Path(directory)/'printed-labels';receipt=directory/'production.json'
    if receipt.exists():
        report=json.loads(receipt.read_bytes());output=json.loads((directory/'generated-records.json').read_bytes())
        if digest(records) not in (report['input_records_sha256'],report['output_records_sha256']) or digest(output)!=report['output_records_sha256']:
            raise ValueError('printed-label stage changed')
        return output,{**report,'invocation_model_calls':0}
    request_path=directory/'request.json';model_calls=0
    if request_path.exists():
        request=json.loads(request_path.read_bytes())
        if request['records_sha256']!=digest(records) or request['plan_sha256']!=plan['plan_sha256']:raise ValueError('label input changed')
        if not (directory/'model/response.json').exists():raise ValueError('incomplete label attempt; no retry')
        response=completed_response(directory/'model')
    else:
        if not allow_source_call:raise ValueError('label extraction needs bounded source-call opt-in')
        request=labels.prepare(index,plan,records,directory)
        if not request['targets']:return records,{'invocation_model_calls':0,'status':'NO_PRINTED_LABEL_TARGETS'}
        if len(request['images'])>3:raise ValueError('printed label window exceeds three visual pages')
        atomic_json(request_path,request)
        visible={k:v for k,v in request.items() if k!='document'}
        invoke(request,directory/'model',schema=labels.schema(),visible=visible,prompt_text=labels.PROMPT,
               timeout=600,image_paths=[im['path'] for im in request['images']]);model_calls=1
        response=completed_response(directory/'model')
    output,report=labels.consume(records,request,response)
    report.update(input_records_sha256=request['records_sha256'],output_records_sha256=digest(output),
                  request_sha256=request['request_sha256'],model_usage=json.loads((directory/'model/usage.json').read_bytes()))
    atomic_json(directory/'generated-records.json',output);atomic_json(receipt,report)
    return output,{**report,'invocation_model_calls':model_calls}


def discover_reusable_slots(records,index,task,slots,objects,records_sha256):
    """Expand omitted properties only within Agent-selected objects and sources."""
    new=[];mixes={m['mix_key']:m for m in records.get('mixes',[])}
    for object_id in sorted({s['object_id'] for s in slots}):
        selected=[s for s in slots if s['object_id']==object_id and s['field_path']=='/modules/performance']
        if not selected or any(s['property_name'] is None for s in selected):continue
        covered={s['property_name'] for s in selected}
        names={p['name'] for k in objects[object_id]['existing_keys'] if k in mixes for p in mixes[k]['modules'].get('performance',[])}
        for name in sorted(names-covered):
            seed=selected[0]
            candidate={**copy.deepcopy(seed),'slot_id':'slot-auto-'+digest([object_id,name,seed['field_path'],seed['condition_scope']])[:20],
                       'parent_slot_id':seed['slot_id'],'property_name':name,'expected_count':None,
                       'discovery':'EXISTING_OBJECT_FIELD_WITH_MATCHING_SOURCE_EVIDENCE'}
            if reusable_values(records,index,task,[candidate],objects,records_sha256):new.append(candidate)
    return new


def dispatch(index,plan,records,directory,asset_root,adapters=None,publish_records=True,material_text=False,allow_material_call=False,xrd_curves=False,allow_xrd_call=False):
    if plan['index_sha256']!=index['index_sha256']:raise ValueError('plan source index changed')
    if index['index_sha256']!=digest({k:v for k,v in index.items() if k!='index_sha256'}):raise ValueError('source index mutated')
    if plan['plan_sha256']!=digest({k:v for k,v in plan.items() if k!='plan_sha256'}):raise ValueError('plan mutated')
    records=copy.deepcopy(records)
    directory=Path(directory);adapters=adapters or ADAPTERS;records_sha256=digest(records)
    sources={s['source_id']:s for s in index['sources']};objects={o['object_id']:o for o in plan['objects']}
    slots={s['slot_id']:copy.deepcopy(s) for s in plan['slots']};states={k:{'status':'planned','filled_children':[]} for k in slots}
    task_results={};pending={t['task_id']:t for t in plan['tasks']};hits=0;calls=0;model_calls=0;material_reports=[];xrd_reports=[]
    while pending:
        ready=sorted(k for k,t in pending.items() if set(t['depends_on'])<=task_results.keys())
        if not ready:raise ValueError('unresolved dispatch dependency cycle')
        for tid in ready:
            task=pending.pop(tid);selected=[slots[k] for k in task['slot_ids']]
            discovered=discover_reusable_slots(records,index,task,selected,objects,records_sha256)
            selected=selected+discovered
            reusable=reusable_values(records,index,task,selected,objects,records_sha256)
            cache_key=digest({'version':VERSION,'task':task,'slots':selected,'sources':[sources[k] for k in task['source_ids']],
                              'reusable':[{k:v for k,v in x.items() if k!='reused_from_records_sha256'} for x in reusable],
                              'adapter':task['kind'],'material_text':material_text,'xrd_curves':xrd_curves,'dispatcher_sha256':IMPLEMENTATION_SHA256,
                              'adapter_sha256':hashlib.sha256(Path(inspect.getfile(adapters[task['kind']])).read_bytes()).hexdigest()})
            path=directory/'tasks'/cache_key/'result.json'
            if path.exists():
                envelope=json.loads(path.read_bytes())
                if envelope['sha256']!=digest(envelope['result']):raise ValueError('task cache corrupted')
                result=envelope['result'];hits+=1
            else:
                try:
                    if any(task_results[d]['status']=='failed' for d in task['depends_on']):
                        raise ValueError('context dependency failed')
                    result=adapters[task['kind']]({'task':task,'slots':selected,'sources':[sources[k] for k in task['source_ids']],
                        'reusable':reusable,'index':index,'records':records,'asset_root':asset_root,
                        'objects':objects,'records_sha256':records_sha256,
                        'material_text_directory':directory/'material-sources' if material_text else None,
                        'allow_material_call':allow_material_call,'xrd_directory':directory/'xrd-sources' if xrd_curves else None,
                        'allow_xrd_call':allow_xrd_call})
                    result['new_slots']=discovered+result['new_slots']
                    result['status']='processed';calls+=1
                except Exception as e:
                    result={'status':'failed','values':[],'new_slots':[],'complete_slots':[],
                            'remaining_reason':type(e).__name__+': '+str(e),'source_artifact':{}}
                atomic_json(path,{'result':result,'sha256':digest(result)})
                model_calls+=result.get('invocation_model_calls',0)
            if result.get('material_text'):
                from planned_material_text import consume
                records,new_fills,material_report=consume(records,result['material_text']['request'],result['material_text']['response'])
                material_reports.append({'task_id':tid,**material_report});records_sha256=digest(records)
                result=copy.deepcopy(result);result['values'].extend(new_fills)
            if result.get('xrd_source'):
                from planned_xrd_curves import consume
                records,new_fills,xrd_report=consume(records,result['xrd_source']['request'],result['xrd_source']['response'],directory/'xrd-data')
                changes=[{'record_key':f['record_key'],'field_path':f['field_path'],'value':f['value'],
                    'evidence_key':f['evidence']['evidence_key'],'source_ids':f['source_ids'],'task_ids':[tid]} for f in new_fills]
                xrd_reports.append({'task_id':tid,'changes':changes,**xrd_report});records_sha256=digest(records)
                result=copy.deepcopy(result);result['values'].extend(new_fills)
            if result.get('scientific_writes'):
                records,new_fills=apply_native_writes(records,result['scientific_writes'])
                records_sha256=digest(records)
                result=copy.deepcopy(result);result['values'].extend(new_fills)
            for slot in result['new_slots']:
                if slot.get('parent_slot_id') not in task['slot_ids']:raise ValueError('invalid discovered slot')
                if slot['slot_id'] in slots:
                    if slots[slot['slot_id']]!=slot:raise ValueError('discovered slot identity conflict')
                    continue
                slots[slot['slot_id']]=slot;states[slot['slot_id']]={'status':'planned','filled_children':[]}
            for value in result['values']:
                parent=value['parent_slot_id']
                if parent not in task['slot_ids'] and parent not in {s['slot_id'] for s in result['new_slots']}:raise ValueError('result escaped task slots')
                if value['status'] not in STATES or not set(value['source_ids'])<=sources.keys():raise ValueError('result state/source invalid')
                if value['status']=='filled' and (value.get('value') is None or not value.get('evidence')):raise ValueError('filled requires value and evidence')
                if value['status']=='not_reported' and not value.get('reason'):raise ValueError('absence needs source finding')
                child='fill-'+digest([parent,value.get('record_key'),value.get('field_path')])[:20]
                if child in states:
                    prior=states[child]
                    if prior.get('value')!=value.get('value') or prior.get('unit')!=value.get('unit'):
                        prior['status']='ambiguous';prior.setdefault('alternatives',[]).append(copy.deepcopy(value))
                    else:
                        prior['source_ids']=sorted(set(prior['source_ids'])|set(value['source_ids']))
                        prior.setdefault('supporting_evidence',[]).append(copy.deepcopy(value['evidence']))
                else:states[child]={**copy.deepcopy(value),'parent_slot_id':parent}
                if child not in states[parent]['filled_children']:states[parent]['filled_children'].append(child)
            for key in task['slot_ids']:
                state=states[key];slot=slots[key];n=sum(states[k]['status']=='filled' for k in state['filled_children'])
                # Children are field values, not collection rows: count equality
                # cannot prove that every expected observation was extracted.
                expected=1 if slot['cardinality']=='scalar' else None
                if any(states[k]['status']=='ambiguous' for k in state['filled_children']):state['status']='ambiguous'
                elif result['status']=='failed':state['status']='failed'
                elif expected is not None and n==expected:state['status']='filled'
                elif key in result['complete_slots']:
                    if not n:raise ValueError('empty complete collection is not evidence of absence')
                    state['status']='filled'
                state['remaining_reason']=result['remaining_reason'] if state['status']!='filled' else None
                state['coverage']='complete' if state['status']=='filled' else 'partial' if state['filled_children'] else 'unestablished'
            task_results[tid]={'status':result['status'],'cache_key':cache_key,'artifact':str(path),'filled_values':len(result['values']),
                'native_write_candidates':len(result.get('scientific_writes',[])),
                'slot_states':{k:states[k]['status'] for k in task['slot_ids']},
                'extraction_complete':all(states[k]['status']=='filled' for k in task['slot_ids']),
                'remaining_reason':result['remaining_reason']}
    result={'version':VERSION,'plan_sha256':plan['plan_sha256'],'slots':list(slots.values()),'states':states,
            'tasks':task_results,'scientific_records_sha256':records_sha256,
            'cache_hits':hits,'adapter_calls':calls,'model_calls':model_calls,'material_reports':material_reports,'xrd_reports':xrd_reports,'formal_acceptance':False,
            'review_ready':False,'review_interface':{'needs_complete_plan':True,'defects_return_to':'task_id + slot_id + source_ids'}}
    if publish_records:atomic_json(directory/'generated-records.json',records)
    atomic_json(directory/'draft-fill.json',result);return result
