"""Generic source graph -> independent route-scoped MIX candidates, no guessed observations."""
import argparse,copy,json,time
from pathlib import Path
import fitz
from source_relation_graph import validate
from relation_curing_stages import normalized_routes
from source_mix_record import empty_source_mix
from source_specimen_variants import digest
from source_quantity_producer import atomic_json


def consume_design(records, request, response, *, request_builder=None):
    """Existing recipe rows with several test routes; no graph-owner duplication."""
    from source_design_relations import consume
    return consume(records, request, response, request_builder=request_builder)


def identity_locations(document,label):
    target=''.join(label.split());found=[]
    for page in document:
        words=page.get_text('words',sort=False)
        for start in range(len(words)):
            text='';selected=[]
            for w in words[start:]:
                text+=w[4];selected.append({'page':page.number+1,'bbox':list(w[:4]),'token':w[4]})
                if not target.startswith(text):break
                if text==target:found.append(selected);break
    return found


def assemble(records,graph,packet,identity_proofs):
    """Caller validates immutable PDF graph first; this boundary is also synthetic-testable."""
    pdfs=[a for a in records['assets'] if a['kind']=='pdf' and a['sha256']==packet['pdf_sha256']]
    if len(pdfs)!=1:raise ValueError('relation graph belongs to another paper')
    paper=pdfs[0]['paper_key'];entities={e['entity_id']:e for e in graph['entities']}
    facts={f['fact_id']:f for f in graph['facts']}
    routes={r['route_id']:r for r in normalized_routes(entities,facts)}
    staged=copy.deepcopy(records);owners=[];pending=copy.deepcopy(graph.get('unresolved_items',[]))
    for edge in graph['relations']:
        if edge['kind']!='curing_route':continue
        subject=edge['subject_id'];route_id=edge['target_id']
        if subject not in edge['scope_specimen_ids'] or subject in edge['excluded_specimen_ids']:
            raise ValueError('route subject outside included scope')
        label=entities[subject]['source_label'];proof=identity_proofs.get(subject)
        if not proof:
            pending.append({'subject_id':subject,'reason':'SOURCE_IDENTITY_NOT_LOCATED'});continue
        key='mix-route-'+digest([paper,subject,route_id])[:24]
        mix=empty_source_mix(paper,key,label,None,None)
        mix['extensions']={'source_relation_owner':{'subject_id':subject,'route_id':route_id,
            'graph_sha256':digest(graph),'source_pdf_sha256':packet['pdf_sha256'],
            'scope_specimen_ids':edge['scope_specimen_ids'],'excluded_specimen_ids':edge['excluded_specimen_ids'],
            'identity_locations':proof,'status':'CANDIDATE_NOT_ACCEPTED',
            'observation_status':'NO_OBSERVATION_TRANSFER_WITHOUT_EXPLICIT_SOURCE_MAPPING'}}
        route=copy.deepcopy(routes[route_id])
        mix['modules']['mixing_curing']['extensions']['source_route']=route
        mix['modules']['mixing_curing']['extensions']['source_facts']=copy.deepcopy(graph['facts'])
        requested={f['source_id'] for f in graph['facts'] if 'source_id' in f}
        mix['modules']['mixing_curing']['extensions']['source_fact_locations']=[copy.deepcopy(b)
            for b in packet.get('source_blocks',[]) if b['source_id'] in requested]
        mix['modules']['mixing_curing']['extensions']['source_packet_sha256']=packet['packet_sha256']
        mix['modules']['mixing_curing']['extensions']['review_status']='pending_source_path_review'
        # Preparations and exposures remain typed stages, never a flattened total age.
        mix['modules']['mixing_curing']['curing_route']=route_id
        existing=[m for m in staged['mixes'] if m['mix_key']==key]
        if existing and (len(existing)!=1 or existing[0]!=mix):raise ValueError('route owner changed; explicit reconciliation required')
        if not existing:staged['mixes'].append(mix)
        owners.append({'subject_id':subject,'route_id':route_id,'mix_key':key})
    receipt={'graph_sha256':digest(graph),'source_pdf_sha256':packet['pdf_sha256'],
        'owners':owners,'pending':pending,'formal_publication_authorized':False,
        'observation_relations_pending':[copy.deepcopy(e) for e in graph['relations'] if e['kind']=='observation_subject']}
    prior=staged.setdefault('extensions',{}).setdefault('source_relation_route_modules',{})
    module_key=digest([paper,packet['packet_sha256']])
    if module_key in prior and prior[module_key]!=receipt:raise ValueError('source route module changed')
    prior[module_key]=receipt
    records.clear();records.update(staged)
    return receipt


def consume(records,graph,packet,pdf,*,projection_request=None,projection_response=None,transport_directory=None):
    validate(graph,packet,Path(pdf))
    if graph['run_id']!=packet['run_id']:raise ValueError('foreign graph run')
    if projection_request is not None or projection_response is not None:
        if projection_request is None or projection_response is None:raise ValueError('projection needs source request and response')
        if projection_request['graph']!=graph or projection_request['packet_sha256']!=packet['packet_sha256']:
            raise ValueError('projection source graph changed')
        from route_production_projection import project
        return project(records,projection_request,projection_response,transport_directory)
    subjects={e['subject_id'] for e in graph['relations'] if e['kind']=='curing_route'}
    with fitz.open(pdf) as document:
        proofs={e['entity_id']:identity_locations(document,e['source_label'])
                for e in graph['entities'] if e['entity_id'] in subjects}
    return assemble(records,graph,packet,proofs)


if __name__=='__main__':
    p=argparse.ArgumentParser()
    for name in ('records','candidate','packet','pdf','output'):p.add_argument('--'+name,required=True)
    args=p.parse_args();started=time.monotonic();records=json.loads(Path(args.records).read_bytes());original=copy.deepcopy(records)
    graph=json.loads(Path(args.candidate).read_bytes())['graph'];packet=json.loads(Path(args.packet).read_bytes())
    receipt=consume(records,graph,packet,args.pdf);first=digest(records)
    consume(records,graph,packet,args.pdf)
    if first!=digest(records) or records['mixes'][:len(original['mixes'])]!=original['mixes']:
        raise ValueError('route reentry or original records changed')
    out=Path(args.output);atomic_json(out/'generated-records.json',records)
    report={'status':'SOURCE_ROUTE_CANDIDATES_CONSUMED','owners':len(receipt['owners']),
        'routes':len({o['route_id'] for o in receipt['owners']}),'records_sha256':first,
        'original_records_unchanged':True,'reentry_identical':True,'new_observations':0,
        'pending':receipt['pending'],'model_calls':0,'publication_allowed':False,'elapsed_seconds':time.monotonic()-started}
    atomic_json(out/'validation.json',report);print(json.dumps(report))
