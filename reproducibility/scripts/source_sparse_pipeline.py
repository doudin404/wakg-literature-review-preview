"""Deterministic source recipe integration; never grants whole-record acceptance."""
import copy
import csv
import hashlib
import json
import io
import zipfile
from pathlib import Path
from sparse_vector_observations import extract
from bind_sparse_performance import bind
from promote_sparse_performance import promote
from vector_curves import digest,atomic_bytes

ROOT=Path(__file__).resolve().parents[1]
IMPLEMENTATION_REVIEW_V1='runs/requirements-repair-v1/p02-sparse-promoter-agent-review.json'
IMPLEMENTATION_REVIEW_V2='runs/requirements-repair-v1/p02-sparse-promoter-agent-review-v2.json'
IMPLEMENTATION_REVIEW_V3='runs/requirements-repair-v1/p02-sparse-promoter-agent-review-v3.json'
IMPLEMENTATION_REVIEWS=(IMPLEMENTATION_REVIEW_V1,IMPLEMENTATION_REVIEW_V2,IMPLEMENTATION_REVIEW_V3)


def implementation_review_path(members=None):
    """Select exactly one known revision; historical archives remain immutable."""
    if members is None:return IMPLEMENTATION_REVIEW_V3
    matches=[p for p in IMPLEMENTATION_REVIEWS if p in members]
    if len(matches)!=1:raise ValueError('sparse implementation review revision ambiguous or missing')
    return matches[0]


def load(path):return json.loads(path.read_text(encoding='utf-8'))


def file_sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def recipe_paths():
    return [
        'scripts/source_sparse_pipeline.py','scripts/sparse_vector_observations.py',
        'scripts/bind_sparse_performance.py','scripts/promote_sparse_performance.py',
        'scripts/transformation_contract.py','scripts/render_sparse_vector_overlay.py',
        'scripts/vector_curves.py','scripts/source_performance_candidates.py',
        'fixtures/sparse-vector-p02-fig3-v1.json',
        'fixtures/sparse-performance-p02-fig3-binding-v1.json',
        implementation_review_path(),
        'runs/requirements-repair-v1/p02-sparse-binding-agent-review.json',
        'runs/requirements-repair-v1/p02-sparse-overlay-agent-review.json',
        'runs/requirements-repair-v1/p02-fig3-sparse-candidates-b.json',
        'runs/requirements-repair-v1/p02-fig3-overlay-b.png',
    ]


def input_receipt():
    """Bind resume to every local recipe, approval and source proof consumed."""
    return {path:file_sha(ROOT/path) for path in recipe_paths()}


def frozen_proof_bytes():
    """Portable immutable recipe inputs; fixed ZIP metadata makes cold runs equal."""
    receipt=input_receipt()
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_STORED) as archive:
        members={name:(ROOT/name).read_bytes() for name in receipt}
        if any(hashlib.sha256(data).hexdigest()!=receipt[name] for name,data in members.items()):
            raise ValueError('sparse recipe input changed during freezing')
        members['MANIFEST.json']=json.dumps(receipt,sort_keys=True,ensure_ascii=False,indent=2).encode()
        for name,data in sorted(members.items()):
            info=zipfile.ZipInfo(name,date_time=(1980,1,1,0,0,0))
            info.create_system=3
            info.external_attr=0o100644 << 16
            archive.writestr(info,data)
    return output.getvalue()


def read_frozen_proof(run_dir,expected):
    """Read only hash-bound archive members; never extract or execute packaged code."""
    try:
        return _read_frozen_proof(run_dir,expected)
    except (zipfile.BadZipFile,zipfile.LargeZipFile,RuntimeError,NotImplementedError,EOFError) as exc:
        raise ValueError('sparse frozen recipe archive invalid') from exc


def _read_frozen_proof(run_dir,expected):
    current=set(recipe_paths())
    revisions=[(current-{implementation_review_path()})|{revision} for revision in IMPLEMENTATION_REVIEWS]
    if not isinstance(expected,dict) or set(expected) not in revisions:
        raise ValueError('sparse frozen recipe receipt missing')
    for name,sha in expected.items():
        if (not isinstance(name,str) or '\\' in name or name.startswith('/')
                or any(p in ('','..','.') for p in name.split('/'))
                or ':' in name or name=='MANIFEST.json' or not isinstance(sha,str)
                or len(sha)!=64 or any(c not in '0123456789abcdef' for c in sha)):
            raise ValueError('sparse frozen recipe receipt invalid')
    path=Path(run_dir)/'sparse-assets/recipe-inputs.zip'
    if path.stat().st_size>32*1024*1024:raise ValueError('sparse frozen recipe archive too large')
    with zipfile.ZipFile(path) as archive:
        info=archive.infolist();names=[i.filename for i in info]
        if (len(names)!=len(set(names)) or set(names)!=set(expected)|{'MANIFEST.json'}
                or any(i.file_size>16*1024*1024 for i in info)
                or sum(i.file_size for i in info)>32*1024*1024):
            raise ValueError('sparse frozen recipe members invalid')
        if json.loads(archive.read('MANIFEST.json'))!=expected:
            raise ValueError('sparse frozen recipe manifest mismatch')
        members={name:archive.read(name) for name in expected}
    if any(hashlib.sha256(data).hexdigest()!=expected[name] for name,data in members.items()):
        raise ValueError('sparse frozen recipe member hash mismatch')
    return members


def artifact_receipt(records,run_dir):
    """Reopen persisted proof; callers must compare this against the manifest."""
    promotion=records.get('extensions',{}).get('sparse_performance_promotion')
    if not promotion:return []
    names={'candidates.json':('identities','candidates_sha256'),
           'binding-plan.json':('identities','plan_sha256'),
           'recipe-execution.json':(None,'acceptance_sha256')}
    receipt=[]
    for name,(parent,key) in names.items():
        relative='sparse-assets/'+name;path=Path(run_dir)/relative
        value=load(path)
        expected=promotion[parent][key] if parent else promotion[key]
        if digest(value)!=expected:raise ValueError('sparse proof identity mismatch: '+name)
        receipt.append({'relative_path':relative,'size_bytes':path.stat().st_size,'sha256':file_sha(path)})
    relative='sparse-assets/recipe-inputs.zip';path=Path(run_dir)/relative
    expected=records.get('extensions',{}).get('input_hashes',{}).get('sparse_recipe_dependencies')
    read_frozen_proof(run_dir,expected)
    receipt.append({'relative_path':relative,'size_bytes':path.stat().st_size,'sha256':file_sha(path)})
    if records.get('extensions',{}).get('sparse_performance_assets') is not None:
        assets,payload,files=sparse_asset_payload(records,run_dir)
        if records['extensions']['sparse_performance_assets']!=payload:
            raise ValueError('sparse export asset registry mismatch')
        for asset in assets:
            matches=[a for a in records['assets'] if a['asset_key']==asset['asset_key']]
            if matches!=[asset]:raise ValueError('sparse export asset identity mismatch')
            path=Path(run_dir)/asset['relative_path']
            if path.read_bytes()!=files[asset['relative_path']]:raise ValueError('sparse export asset bytes mismatch')
            receipt.append({'relative_path':asset['relative_path'],'size_bytes':path.stat().st_size,'sha256':file_sha(path)})
    return receipt


def sparse_asset_payload(records,run_dir):
    """Exact source point table and reviewed overlay, not an interpolated curve."""
    dependencies=records['extensions']['input_hashes']['sparse_recipe_dependencies']
    frozen=read_frozen_proof(run_dir,dependencies)
    plan=load(Path(run_dir)/'sparse-assets/binding-plan.json')
    candidates=load(Path(run_dir)/'sparse-assets/candidates.json')
    approval=load(Path(run_dir)/'sparse-assets/recipe-execution.json')
    promotion=records['extensions']['sparse_performance_promotion']
    if (digest(plan)!=promotion['identities']['plan_sha256']
            or digest(candidates)!=promotion['identities']['candidates_sha256']
            or digest(approval)!=promotion['acceptance_sha256']):
        raise ValueError('sparse export proof identity mismatch')
    if sorted(digest(i['candidate']) for i in plan['items'])!=sorted(digest(i) for i in candidates['observations']):
        raise ValueError('sparse export point coverage mismatch')
    source=next(a for a in records['assets'] if a['asset_key']==plan['source_asset_key'])
    if source['kind']!='pdf' or source['sha256']!=plan['pdf_sha256']:
        raise ValueError('sparse export PDF identity mismatch')
    buffer=io.StringIO(newline='');writer=csv.writer(buffer,lineterminator='\n')
    writer.writerow(['mix_key','source_label','action','property','nominal_age_days','axis_age_days',
                     'figure_value','preferred_record_value','value_unit','pdf_x_pt','pdf_y_pt',
                     'digitization_interval_low','digitization_interval_high','cap_1_axis_value',
                     'cap_2_axis_value','experimental_error_semantics'])
    for item in plan['items']:
        point=item['candidate']
        writer.writerow([item['mix_key'],point['source_label'],item['action'],item['name'],
                         item['age_days'],point['age_from_axis'],point['value_from_axis'],item['preferred_value'],
                         item['unit'],*point['pdf_point'],*item['digitization_interval'],
                         point['error_caps'][0]['value_from_axis'],point['error_caps'][1]['value_from_axis'],
                         point['uncertainty_semantics']])
    overlay=frozen[approval['overlay_path']]
    if hashlib.sha256(overlay).hexdigest()!=approval['overlay_sha256']:
        raise ValueError('sparse export overlay identity mismatch')
    assets=[];files={}
    for role,data,suffix,kind,mime in [('source_points',buffer.getvalue().encode(),'csv','csv','text/csv'),
                                     ('review_overlay',overlay,'png','image','image/png')]:
        sha=hashlib.sha256(data).hexdigest();path=f'sparse-assets/{role}-{sha}.{suffix}'
        asset={'asset_key':'asset-sparse-'+role+'-'+sha[:24],'paper_key':source['paper_key'],
               'kind':kind,'mime_type':mime,'relative_path':path,'sha256':sha,
               'extensions':{'schema_version':1,'role':role,'source_pdf_asset_key':source['asset_key'],
                             'source_pdf_sha256':source['sha256'],'figure_number':candidates['figure'],
                             'plan_sha256':digest(plan),'interpolated_points':0,
                             'experimental_error_semantics':None,'review_status':'final_record_acceptance_pending'}}
        assets.append(asset);files[path]=data
    payload={'schema_version':1,'points_asset_key':assets[0]['asset_key'],'overlay_asset_key':assets[1]['asset_key'],
             'source_pdf_asset_key':source['asset_key'],'point_count':len(plan['items']),
             'plan_sha256':digest(plan),'interpolated_points':0}
    return assets,payload,files


def export_assets(records,run_dir):
    if not records.get('extensions',{}).get('sparse_performance_promotion'):return
    artifact_receipt(records,run_dir)
    assets,payload,files=sparse_asset_payload(records,run_dir)
    work=copy.deepcopy(records)
    for asset in assets:
        matches=[a for a in work['assets'] if a['asset_key']==asset['asset_key']]
        if matches and matches!=[asset]:raise ValueError('sparse export asset collision')
        if not matches:work['assets'].append(asset)
    prior=work['extensions'].get('sparse_performance_assets')
    if prior is not None and prior!=payload:raise ValueError('sparse export registry conflict')
    for relative,data in files.items():
        path=Path(run_dir)/relative
        if path.exists() and path.read_bytes()!=data:raise ValueError('immutable sparse export file conflict')
    for relative,data in files.items():atomic_bytes(Path(run_dir)/relative,data)
    work['extensions']['sparse_performance_assets']=payload
    records.clear();records.update(work)


def enrich(records,pdf,run_dir):
    geometry=load(ROOT/'fixtures/sparse-vector-p02-fig3-v1.json')
    if file_sha(Path(pdf))!=geometry['pdf_sha256']:
        prior=records.get('extensions',{}).get('sparse_performance_promotion')
        if prior or 'sparse_binding_id' in json.dumps(records) or 'sparse_support' in json.dumps(records):
            raise ValueError('existing sparse state belongs to a different source PDF')
        return {'added':0,'supported':0,'applicable':False}
    review_root=ROOT/'runs/requirements-repair-v1'
    implementation=load(ROOT/implementation_review_path())
    expected=[('scripts/promote_sparse_performance.py','helper_sha256'),
              ('scripts/transformation_contract.py','transformation_contract_sha256'),
              ('scripts/render_sparse_vector_overlay.py','overlay_renderer_sha256')]
    if implementation['verdict']!='PASS' or any(file_sha(ROOT/path)!=implementation[key] for path,key in expected):
        raise ValueError('sparse producer implementation differs from independent review')
    binding_review=load(review_root/'p02-sparse-binding-agent-review.json')
    binding_path=ROOT/'fixtures/sparse-performance-p02-fig3-binding-v1.json'
    if (binding_review['verdict']!='PASS' or file_sha(binding_path)!=binding_review['binding_file_sha256']
            or file_sha(ROOT/'scripts/bind_sparse_performance.py')!=binding_review['helper_sha256']):
        raise ValueError('sparse binding differs from source review')
    overlay_review=load(review_root/'p02-sparse-overlay-agent-review.json')
    reviewed_candidates=review_root/'p02-fig3-sparse-candidates-b.json'
    if overlay_review['verdict']!='PASS' or file_sha(reviewed_candidates)!=overlay_review['candidate_sha256']:
        raise ValueError('sparse reviewed candidate file drift')
    candidates=extract(pdf,geometry)
    if candidates!=load(reviewed_candidates):raise ValueError('sparse source replay differs from reviewed candidates')
    work=copy.deepcopy(records);binding=load(binding_path)
    asset_dir=Path(run_dir)/'sparse-assets'
    if work.get('extensions',{}).get('sparse_performance_promotion'):
        plan=load(asset_dir/'binding-plan.json')
    else:plan=bind(work,pdf,geometry,candidates,binding)
    approval={'verdict':'PASS','message_id':implementation['message_id'],
              'scope':'reviewed deterministic recipe execution only; final record acceptance pending',
              'plan_sha256':digest(plan),'binding_sha256':digest(binding),'geometry_sha256':digest(geometry),
              'candidates_sha256':digest(candidates),'pdf_sha256':geometry['pdf_sha256'],
              'overlay_path':(review_root/'p02-fig3-overlay-b.png').relative_to(ROOT).as_posix(),'overlay_sha256':overlay_review['overlay_sha256']}
    outcome=promote(work,pdf,geometry,candidates,binding,plan,approval)
    frozen=frozen_proof_bytes()
    frozen_path=asset_dir/'recipe-inputs.zip'
    if frozen_path.exists() and frozen_path.read_bytes()!=frozen:
        raise ValueError('immutable sparse recipe input conflict')
    atomic_bytes(frozen_path,frozen)
    for name,value in [('candidates.json',candidates),('binding-plan.json',plan),('recipe-execution.json',approval)]:
        path=asset_dir/name;data=json.dumps(value,sort_keys=True,ensure_ascii=False,indent=2).encode()
        if path.exists() and path.read_bytes()!=data:raise ValueError('immutable sparse execution artifact conflict')
        atomic_bytes(path,data)
    records.clear();records.update(work)
    return outcome
