"""Assemble completed planned modules; retain the legacy producer continuation."""
import copy
import hashlib
import json
import time
from pathlib import Path
from paper_block_dispatch import dispatch,pointer
from source_quantity_producer import atomic_json
from source_specimen_variants import digest


def load(path):return json.loads(Path(path).read_bytes())


def assemble_completed(index_path,plan_path,records_path,draft_path,out):
    """Consume normal dispatcher outputs without requiring specific producers.

    Missing tasks are pending. A processed task must have an intact existing
    cache artifact. Scientific records are referenced, never regenerated here.
    """
    started=time.monotonic();out=Path(out)
    index=load(index_path);plan=load(plan_path);records=load(records_path)
    prior=load(draft_path) if draft_path is not None else None
    if prior is not None:
        if prior['plan_sha256']!=plan['plan_sha256']:raise ValueError('completed draft plan mismatch')
        if prior['scientific_records_sha256']!=digest(records):raise ValueError('completed draft records mismatch')
    tasks={t['task_id']:t for t in plan['tasks']}
    prior_tasks=prior['tasks'] if prior else {}
    if set(prior_tasks)-tasks.keys():raise ValueError('completed task outside current plan')
    all_records={r[key]:r for group,key in [('mats','mat_key'),('mixes','mix_key'),('papers','paper_key')] for r in records.get(group,[])}
    links={e['evidence_key']:e for e in records.get('evidence_links',[])}
    source_ids={s['source_id'] for s in index['sources']}
    completed={};execution={}
    for tid,task in tasks.items():
        summary=prior_tasks.get(tid)
        if summary is None or summary['status'] in ('pending','planned'):
            execution[tid]='pending';continue
        if summary['status']=='failed':execution[tid]='failed';continue
        if summary['status']!='processed':raise ValueError('unknown module status: '+summary['status'])
        envelope=load(summary['artifact'])  # Missing/corrupt completed output is an error, never pending.
        if envelope['sha256']!=digest(envelope['result']):raise ValueError('completed module artifact corrupted: '+tid)
        if envelope['result']['status']!='processed':raise ValueError('completed module status mismatch: '+tid)
        new_slots=envelope['result'].get('new_slots',[])
        selected=set(task['slot_ids'])|{s['slot_id'] for s in new_slots}
        values=[]
        for value in prior['states'].values():
            if value.get('parent_slot_id') not in selected:continue
            if value.get('status')!='filled':continue
            key=value['record_key'];path=value['field_path'];ev=value['evidence']
            if digest(pointer(all_records[key],path))!=digest(value['value']):raise ValueError('completed value differs from scientific record')
            if links.get(ev['evidence_key'])!=ev:raise ValueError('completed evidence differs from scientific record')
            if ev['record_key']!=key or not set(value['source_ids'])<=source_ids:raise ValueError('completed source ownership mismatch')
            values.append(copy.deepcopy(value))
        completed[tid]={'values':values,'new_slots':new_slots,
            'complete_slots':envelope['result'].get('complete_slots',[]),
            'source_artifact':{'consumed_artifact':summary['artifact']},
            'remaining_reason':summary.get('remaining_reason') or 'Existing module result only; final review outstanding'}
        execution[tid]='processed'
    for tid,task in tasks.items():
        if execution[tid]=='processed' and any(execution.get(dep)!='processed' for dep in task['depends_on']):
            raise ValueError('completed module has unfinished dependency: '+tid)
    def consume(context):
        tid=context['task']['task_id']
        return copy.deepcopy(completed.get(tid,{'values':[],'new_slots':[],'complete_slots':[],
            'source_artifact':{},'remaining_reason':'Module '+execution[tid]+'; no completed output consumed'}))
    # Keep the existing adapter cache protocol, but segregate this invocation's
    # task cache: completed/pending status is an input, not scientific evidence.
    working=out/'modules'/digest({'execution':execution,'completed':completed})[:24]
    draft=dispatch(index,plan,records,working,Path('.'),adapters={k:consume for k in ('text','table','figure')},publish_records=False)
    for tid,status in execution.items():
        draft['tasks'][tid]['execution_status']=status
        if status!='processed':
            draft['tasks'][tid]['status']=status;draft['tasks'][tid]['extraction_complete']=False
    reference={'path':str(records_path),'sha256':digest(records)}
    draft['scientific_records_ref']=reference
    atomic_json(out/'draft-fill.json',draft)
    result={'status':'EXISTING_ROUTES_ASSEMBLED_NOT_ACCEPTED','scientific_records_ref':reference,
        'task_execution':execution,'absent_task_kinds':sorted({'text','table','figure'}-{t['kind'] for t in tasks.values()}),
        'task_coverage':{tid:'complete' if t['extraction_complete'] else 'partial' if t['filled_values'] else 'unestablished' for tid,t in draft['tasks'].items()},
        'input_files':{k:str(v) if v is not None else None for k,v in [('index',index_path),('plan',plan_path),('records',records_path),('completed_draft',draft_path)]},
        'model_calls':0,'pdf_opens':0,'scientific_writes':0,'formal_acceptance':False,'publication_allowed':False,
        'elapsed_seconds':time.monotonic()-started}
    atomic_json(out/'result.json',result);return result


def projection(context):
    return {'values':context['reusable'],'new_slots':[],'complete_slots':[],
        'source_artifact':{'mode':'existing_scientific_evidence_only'},
        'remaining_reason':'Bounded source results exist; whole task/collection coverage is not established'}


def change_contract(kind,report,records):
    """Same record/field/source contract for text, image and table consumers."""
    mixes={m[key]:m for group,key in [('mats','mat_key'),('mixes','mix_key'),('papers','paper_key')] for m in records.get(group,[])}
    evidence={e['evidence_key']:e for e in records['evidence_links']};changes=[]
    if kind=='text':items=report['fills']
    elif kind=='figure':items=report['corrected_locators']
    else:items=[{**w,'task_id':task['task_id']} for task in report['tasks'] for w in task.get('native_bindings',[])]
    for item in items:
        key=item['record_key'];path=item['field_path'];mix=mixes[key]
        prov=mix['field_provenance'][path];ev=evidence[prov['evidence_key']]
        if item.get('evidence_key') and item['evidence_key']!=ev['evidence_key']:raise ValueError('stage evidence not consumed by final record')
        if ev['record_key']!=key or ev['field_path']!=path:raise ValueError('stage record/field mapping changed')
        source_ids=ev.get('extensions',{}).get('source_ids') or item.get('source_ids') or [item['source_id']]
        if not source_ids:raise ValueError('stage source mapping absent')
        changes.append({'record_key':key,'field_path':path,'value':pointer(mix,path),
            'evidence_key':ev['evidence_key'],'source_ids':source_ids,
            'task_ids':item.get('task_ids') or [item['task_id']]})
    return {'kind':kind,'execution_status':'succeeded','changes':changes,
        'scope_coverage':'complete' if kind=='figure' and not report.get('unresolved_target_ids') else 'partial',
        'whole_task_complete':False,'formal_acceptance':False}


def validate_table_continuation(prior,current):
    for group in ('mats','papers','assets'):
        if prior.get(group)!=current.get(group):raise ValueError('table result changed untargeted '+group)
    old={m['mix_key']:m for m in prior['mixes']};new={m['mix_key']:m for m in current['mixes']}
    for key,mix in old.items():
        other=new[key]
        for name in ('identity_source_specimen','mixing_curing','performance','characterizations'):
            if mix['modules'][name]!=other['modules'][name]:raise ValueError('table result lost earlier route '+name)
        parameters=mix['modules']['materials']['extensions']['reported_parameters']
        if parameters and other.get('extensions',{}).get('native_module_revision_history',[])[-1]['reported_parameters']!=parameters:
            raise ValueError('table prior parameter history mismatch')


def assemble(root,initial_records,**options):
    """Normal completed-task assembly unless legacy producers need continuation."""
    root=Path(root)
    legacy=any((root/'fill/extraction'/name).exists() for name in ('text-counts','printed-labels')) or any(root.glob('table-semantic-rebuild-*'))
    if legacy or any(options.values()):return _assemble_legacy(root,initial_records,**options)
    plan_path=root/'planning/plan.json'
    if not plan_path.exists():plan_path=root/'module-plan.json'
    records_path=root/'fill/generated-records.json'
    out=root/'unified-fill'
    if not records_path.exists():
        records_path=out/'input-records.json'
        if records_path.exists() and load(records_path)!=initial_records:raise ValueError('assembly initial records changed')
        if not records_path.exists():atomic_json(records_path,initial_records)
    draft_path=root/'fill/draft-fill.json'
    return assemble_completed(root/'source-index.json',plan_path,records_path,draft_path if draft_path.exists() else None,out)


def _assemble_legacy(root,initial_records,project_materials=False,extract_material_text=False,allow_material_call=False,extract_xrd=False,allow_xrd_call=False,extract_thermal=False,allow_thermal_call=False,extract_mip=False,allow_mip_call=False,extract_sem=False,allow_sem_call=False):
    started=time.monotonic();root=Path(root);out=root/'unified-fill'
    for directory in root.glob('table-semantic-rebuild-*'):
        if (directory/'fill/draft-fill.json').exists() and not (directory/'fill/generated-records.json').exists():
            raise ValueError('table module output missing: '+str(directory))
    candidates=[p for p in root.glob('table-semantic-rebuild-*') if (p/'fill/generated-records.json').exists()]
    if len(candidates)>1:raise ValueError('ambiguous successful table continuations')
    table=candidates[0] if candidates else None;text=root/'fill/extraction/text-counts';figure=root/'fill/extraction/printed-labels'
    paths={'index':root/'source-index.json','plan':root/'planning/plan.json'}
    for kind,directory in [('text',text),('figure',figure)]:
        if (directory/'production.json').exists():
            paths[kind+'_report']=directory/'production.json';paths[kind+'_records']=directory/'generated-records.json'
    if table is not None:paths.update(table_plan=table/'compiled-plan.json',table_records=table/'fill/generated-records.json')
    fingerprints={k:hashlib.sha256(p.read_bytes()).hexdigest() for k,p in paths.items()}
    material_dir=root/'material-recipe-projection';material_receipt=material_dir/'result.json'
    project_materials=project_materials or material_receipt.exists()
    raw_dir=root/'raw-material-text';extract_material_text=extract_material_text or (raw_dir/'result.json').exists()
    xrd_dir=root/'planned-xrd';extract_xrd=extract_xrd or (xrd_dir/'result.json').exists()
    thermal_dir=root/'planned-thermal';extract_thermal=extract_thermal or (thermal_dir/'result.json').exists()
    mip_dir=root/'planned-mip';extract_mip=extract_mip or (mip_dir/'result.json').exists()
    sem_dir=root/'planned-sem-eds';extract_sem=extract_sem or (sem_dir/'result.json').exists()
    cross_dir=root/'crossfigure-structure';extract_cross=(cross_dir/'result.json').exists()
    bars_dir=root/'categorical-bars';extract_bars=(bars_dir/'response.json').exists()
    guided_dir=root/'guided-xrd';extract_guided=(guided_dir/'bundle.json').exists()
    if extract_guided:
        from guided_xrd_backend import checked_bundle
        checked_bundle(guided_dir)
    material_code=hashlib.sha256(Path(__file__).with_name('canonical_materials.py').read_bytes()).hexdigest()
    cache_key=digest({'inputs':fingerprints,'initial':digest(initial_records),
        'project_materials':project_materials,'material_code':material_code if project_materials else None,
        'extract_material_text':extract_material_text,
        'material_text_code':hashlib.sha256(Path(__file__).with_name('planned_material_text.py').read_bytes()).hexdigest(),
        'extract_xrd':extract_xrd,'xrd_code':hashlib.sha256(Path(__file__).with_name('planned_xrd_curves.py').read_bytes()).hexdigest(),
        'extract_thermal':extract_thermal,'thermal_code':hashlib.sha256(Path(__file__).with_name('planned_thermal.py').read_bytes()).hexdigest(),
        'extract_mip':extract_mip,'mip_code':hashlib.sha256(Path(__file__).with_name('planned_mip.py').read_bytes()).hexdigest(),
        'extract_sem':extract_sem,'sem_code':hashlib.sha256(Path(__file__).with_name('planned_sem_eds.py').read_bytes()).hexdigest(),
        'crossfigure':extract_cross,'crossfigure_code':hashlib.sha256(Path(__file__).with_name('consume_crossfigure_gap.py').read_bytes()).hexdigest(),
        'crossfigure_geometry_code':hashlib.sha256(Path(__file__).with_name('crossfigure_structure.py').read_bytes()).hexdigest(),
        'source_navigation_code':hashlib.sha256(Path(__file__).with_name('source_region_candidates.py').read_bytes()).hexdigest(),
        'crossfigure_inputs':{name:hashlib.sha256((cross_dir/name).read_bytes()).hexdigest() for name in ('visual-gap/request.json','visual-gap/model/response.json','structure-report.json','region-ocr.json')} if extract_cross else {},
        'categorical_bars':extract_bars,'categorical_bar_code':hashlib.sha256(Path(__file__).with_name('categorical_bar_digitization.py').read_bytes()).hexdigest(),
        'categorical_bar_inputs':{name:hashlib.sha256((bars_dir/name).read_bytes()).hexdigest() for name in ('packet.json','response.json')} if extract_bars else {},
        'guided_xrd':extract_guided,'guided_xrd_bundle':hashlib.sha256((guided_dir/'bundle.json').read_bytes()).hexdigest() if extract_guided else None,
        'guided_xrd_code':hashlib.sha256(Path(__file__).with_name('guided_xrd_backend.py').read_bytes()).hexdigest() if extract_guided else None,
        'figure_region_code':hashlib.sha256(Path(__file__).with_name('figure_source_region.py').read_bytes()).hexdigest(),
        'xrd_geometry_dependencies':[hashlib.sha256(Path(__file__).with_name(f).read_bytes()).hexdigest()
            for f in ('raster_curves.py','promote_vector_curves.py')] if extract_xrd else [],
        'assembly_code':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'method_scope_code':hashlib.sha256(Path(__file__).with_name('source_design_relations.py').read_bytes()).hexdigest(),
        'dispatcher_code':hashlib.sha256(Path(__file__).with_name('paper_block_dispatch.py').read_bytes()).hexdigest()})
    if (out/'result.json').exists():
        receipt=load(out/'result.json')
        if (receipt['cache_key']==cache_key and hashlib.sha256((out/'draft-fill.json').read_bytes()).hexdigest()==receipt['draft_file_sha256']
                and hashlib.sha256(Path(receipt['scientific_records_ref']['path']).read_bytes()).hexdigest()==receipt['scientific_records_file_sha256']):
            return {**receipt,'cache_hit':True,'scientific_writes':0,'model_calls':0,'pdf_opens':0,'elapsed_seconds':time.monotonic()-started}
    index=load(paths['index']);plan=load(paths['plan']);records=copy.deepcopy(initial_records);reports=[]
    scientific_path=None
    for kind in ('text','figure'):
        if kind+'_report' not in paths:continue
        report=load(paths[kind+'_report']);current=load(paths[kind+'_records'])
        if report['input_records_sha256']!=digest(records) or report['output_records_sha256']!=digest(current):raise ValueError(kind+' lineage mismatch')
        records=current;scientific_path=paths[kind+'_records'];reports.append((kind,report))
    if table is not None:
        tp=load(paths['table_plan']);current=load(paths['table_records'])
        if tp['parent_plan_sha256']!=plan['plan_sha256']:raise ValueError('table parent planning mismatch')
        validate_table_continuation(records,current)
        records=current;scientific_path=paths['table_records'];reports.append(('table',tp))
    if scientific_path is None:
        scientific_path=out/'input-records.json'
        atomic_json(scientific_path,records)
    contracts=[change_contract(kind,report,records) for kind,report in reports]
    material_report=None;material_writes=0
    if project_materials:
        from canonical_materials import project_explicit_sources
        previous=load(material_receipt) if material_receipt.exists() else None
        source_input_sha=digest(records)
        same_source=previous and previous.get('source_input_records_sha256',previous['input_records_sha256'])==source_input_sha
        if same_source and previous['implementation_sha256']==material_code:
            material_report=previous;scientific_path=Path(previous['output_path']);records=load(scientific_path)
            if digest(records)!=previous['output_records_sha256']:raise ValueError('material projection output changed')
        else:
            if same_source:
                prior_projection=load(previous['output_path'])
                if digest(prior_projection)!=previous['output_records_sha256']:raise ValueError('previous material projection changed')
                records=prior_projection
            records,material_report=project_explicit_sources(records)
            material_writes=sum(x['operation']!='reused' for x in material_report['mapped'])
            scientific_path=material_dir/'objects'/(material_report['output_records_sha256'][:24]+'.json')
            if scientific_path.exists():
                if digest(load(scientific_path))!=material_report['output_records_sha256']:
                    raise ValueError('material content-address prefix collision')
            else:atomic_json(scientific_path,records)
            material_report.update(output_path=str(scientific_path),implementation_sha256=material_code,
                source_input_records_sha256=source_input_sha)
            if previous:atomic_json(material_dir/'history'/(digest(previous)+'.json'),previous)
            atomic_json(material_receipt,material_report)
        changes=[];links={e['evidence_key']:e for e in records['evidence_links']};mixes={m['mix_key']:m for m in records['mixes']}
        for item in material_report['mapped']:
            ev=links[item['evidence_key']];source_ids=ev['extensions']['source_ids']
            tids=[t['task_id'] for t in plan['tasks'] if t['kind']=='table' and set(t['source_ids'])&set(source_ids)]
            if not tids:raise ValueError('material projection has no source task')
            changes.append({'record_key':item['record_key'],'field_path':item['field_path'],
                'value':pointer(mixes[item['record_key']],item['field_path']),'evidence_key':item['evidence_key'],
                'source_ids':source_ids,'task_ids':tids})
        contracts.append({'kind':'material_projection','execution_status':'succeeded','changes':changes,
            'scope_coverage':'partial','whole_task_complete':False,'formal_acceptance':False})
    raw_report=None;source_calls=0
    if extract_material_text:
        from planned_material_text import run as run_material_text
        previous_raw=load(raw_dir/'result.json') if (raw_dir/'result.json').exists() else None
        records,raw_report=run_material_text(index,plan,records,raw_dir,allow_call=allow_material_call)
        scientific_path=Path(raw_report['output_path']);plan=load(raw_report['compiled_plan_path'])
        source_calls=raw_report['invocation_model_calls'];material_writes+=raw_report['invocation_scientific_writes']
        if previous_raw and previous_raw['output_records_sha256']==raw_report['output_records_sha256']:
            material_writes-=raw_report['invocation_scientific_writes']
        contracts.append({'kind':'raw_material_text','execution_status':'succeeded','changes':raw_report['changes'],
            'scope_coverage':'partial','whole_task_complete':False,'formal_acceptance':False})
    if extract_xrd:
        from planned_xrd_curves import run as run_xrd
        records,xrd_report=run_xrd(index,plan,records,xrd_dir,allow_call=allow_xrd_call)
        scientific_path=Path(xrd_report['output_path']);source_calls+=xrd_report['invocation_model_calls']
        material_writes+=xrd_report['invocation_scientific_writes']
        contracts.append({'kind':'xrd_curves','execution_status':'succeeded','changes':xrd_report['changes'],
            'scope_coverage':'partial','whole_task_complete':False,'formal_acceptance':False})
    if extract_thermal:
        from planned_thermal import run as run_thermal
        records,thermal_report=run_thermal(index,plan,records,thermal_dir,allow_call=allow_thermal_call)
        scientific_path=Path(thermal_report['output_path']);source_calls+=thermal_report['model_calls']
        material_writes+=thermal_report['scientific_writes']
        contracts.append({'kind':'thermal','execution_status':'succeeded','changes':thermal_report['changes'],
            'source_report_path':str(thermal_dir/'result.json'),'geometry_status':thermal_report['geometry_status'],
            'scope_coverage':'partial','whole_task_complete':False,'formal_acceptance':False})
    if extract_mip:
        from planned_mip import run as run_mip
        records,mip_report=run_mip(index,plan,records,mip_dir,allow_call=allow_mip_call)
        scientific_path=Path(mip_report['output_path']);source_calls+=mip_report['model_calls'];material_writes+=mip_report['scientific_writes']
        contracts.append({'kind':'mip','execution_status':'succeeded','changes':mip_report['changes'],
            'source_report_path':str(mip_dir/'result.json'),'geometry_status':mip_report['geometry_status'],
            'scope_coverage':'partial','whole_task_complete':False,'formal_acceptance':False})
    if extract_sem:
        from planned_sem_eds import run as run_sem
        records,sem_report=run_sem(index,plan,records,sem_dir,allow_call=allow_sem_call)
        scientific_path=Path(sem_report['output_path']);source_calls+=sem_report['model_calls'];material_writes+=sem_report['scientific_writes']
        contracts.append({'kind':'sem_eds','execution_status':'succeeded','changes':sem_report['changes'],
            'source_report_path':str(sem_dir/'result.json'),'geometry_status':sem_report['geometry_status'],
            'scope_coverage':'partial','whole_task_complete':False,'formal_acceptance':False})
    if extract_cross:
        from consume_crossfigure_gap import run as run_cross,compile_geometry
        records,cross_report=run_cross(records,cross_dir);scientific_path=Path(cross_report['output_path']);material_writes+=cross_report['scientific_writes']
        index,plan=compile_geometry(index,plan,cross_dir)
        contracts.append({'kind':'crossfigure','execution_status':'succeeded','changes':cross_report['changes'],
            'source_report_path':str(cross_dir/'result.json'),'geometry_status':cross_report['geometry_status'],
            'scope_coverage':'partial','whole_task_complete':False,'formal_acceptance':False})
    if extract_bars:
        from categorical_bar_digitization import run as run_bars
        records,bars_report=run_bars(records,bars_dir);scientific_path=Path(bars_report['output_path']);material_writes+=bars_report['scientific_writes']
        contracts.append({'kind':'categorical_bars','execution_status':'succeeded','changes':bars_report['changes'],
            'source_report_path':str(bars_dir/'result.json'),'geometry_status':bars_report['geometry_status'],
            'scope_coverage':'partial','whole_task_complete':False,'formal_acceptance':False})
    if extract_guided:
        from guided_xrd_backend import run as run_guided
        records,guided_report=run_guided(records,guided_dir);scientific_path=Path(guided_report['output_path']);material_writes+=guided_report['scientific_writes']
        contracts.append({'kind':'guided_xrd','execution_status':'succeeded','changes':guided_report['changes'],
            'source_report_path':str(guided_dir/'result.json'),'scope_coverage':'partial','whole_task_complete':False,'formal_acceptance':False})
    from source_design_relations import existing_method_contract
    method_contract=existing_method_contract(records,index,plan)
    contracts.append(method_contract)
    # Frozen source responses are immutable. If an older broad task association
    # fails today's source/route join, quarantine the association, not invent a
    # replacement count or mutate the saved scientific observation.
    scoped={(x['record_key'],x['field_path']):set(x['task_ids']) for x in method_contract['changes']}
    for contract in contracts:
        if contract['kind']!='text':continue
        retained=[];quarantined=[]
        for item in contract['changes']:
            tids=set(item['task_ids']) & scoped.get((item['record_key'],item['field_path']),set())
            if tids:retained.append({**item,'task_ids':sorted(tids)})
            if tids!=set(item['task_ids']):
                quarantined.append({**item,'task_ids':sorted(set(item['task_ids'])-tids),
                    'reason':'METHOD_SOURCE_ROUTE_ASSOCIATION_NOT_ESTABLISHED'})
        contract['changes']=retained;contract['quarantined_associations']=quarantined
    for contract in contracts:
        for c in contract['changes']:
            if not set(c['source_ids'])<={s['source_id'] for s in index['sources']}:raise ValueError('source outside index')
            if not set(c['task_ids'])<={t['task_id'] for t in plan['tasks']}:raise ValueError('task outside plan')
    # Reproject current field paths, never splice the stale pre-table draft.
    draft=dispatch(index,plan,records,out,root,adapters={k:projection for k in ('text','table','figure')},publish_records=False)
    bypath={(v.get('record_key'),v.get('field_path')) for v in draft['states'].values() if v['status']=='filled'}
    for c in contracts:
        for item in c['changes']:
            if item['value'] is not None and (item['record_key'],item['field_path']) not in bypath:
                raise ValueError('successful source change absent from current draft')
    for tid,task in draft['tasks'].items():
        scopes=[{'kind':c['kind'],'coverage':c['scope_coverage']} for c in contracts if any(tid in x['task_ids'] for x in c['changes'])]
        task['consumed_source_scopes']=scopes
        task['coverage']='complete' if task['extraction_complete'] else 'partial' if scopes or task['filled_values'] else 'unestablished'
        task['execution_status']='processed' if scopes else 'pending'
        if not scopes:task['extraction_complete']=False
    draft['scientific_records_ref']={'path':str(scientific_path),'sha256':digest(records)}
    atomic_json(out/'draft-fill.json',draft)
    result={'status':'EXISTING_ROUTES_ASSEMBLED_NOT_ACCEPTED','cache_key':cache_key,'cache_hit':False,
        'input_files':{k:{'path':str(p),'sha256':fingerprints[k]} for k,p in paths.items()},
        'scientific_records_ref':draft['scientific_records_ref'],'contracts':contracts,
        'scientific_records_file_sha256':hashlib.sha256(scientific_path.read_bytes()).hexdigest(),
        'material_projection':None if material_report is None else {'mapped':len(material_report['mapped']),
            'deferred':len(material_report['deferred']),'conflicts':len(material_report['conflicts'])},
        'task_coverage':{k:v['coverage'] for k,v in draft['tasks'].items()},
        'task_execution':{k:v['execution_status'] for k,v in draft['tasks'].items()},
        'absent_task_kinds':sorted({'text','table','figure'}-{t['kind'] for t in plan['tasks']}),
        'model_calls':source_calls,'pdf_opens':source_calls,'scientific_writes':material_writes,'formal_acceptance':False,'publication_allowed':False,
        'draft_file_sha256':hashlib.sha256((out/'draft-fill.json').read_bytes()).hexdigest(),
        'elapsed_seconds':time.monotonic()-started}
    atomic_json(out/'result.json',result)
    return result


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description='Assemble existing planned module outputs without extraction')
    for name in ('index','plan','records','output'):parser.add_argument('--'+name,required=True)
    parser.add_argument('--completed-draft')
    args=parser.parse_args()
    print(json.dumps(assemble_completed(args.index,args.plan,args.records,args.completed_draft,args.output),ensure_ascii=False))
