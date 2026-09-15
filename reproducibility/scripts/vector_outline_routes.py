"""Offline candidate-only production adapter; no formal projection authority."""
import copy
import hashlib
import json
from pathlib import Path

from vector_curves import atomic_bytes
import vector_outline_candidates
import vector_marker_candidates

REGISTRY = 'fixtures/vector-outline-routes-v1.json'
DEPENDENCIES = ('vector_outline_routes.py','vector_outline_candidates.py','vector_marker_candidates.py',
                'vector_path_inventory.py','vector_curves.py')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def select(root, identity):
    root=Path(root).resolve()
    registry=json.loads((root/REGISTRY).read_text(encoding='utf-8'))
    if registry.get('schema_version')!=1:
        raise ValueError('invalid outline registry')
    seen=set(); selected=None
    for route in registry['routes']:
        if set(route)!={'specification','marker_legend_index','marker_owner'}:
            raise ValueError('invalid outline route fields')
        relative=Path(route['specification'])
        path=(root/relative).resolve()
        if relative.is_absolute() or not path.is_relative_to(root):
            raise ValueError('outline route escapes project')
        spec=json.loads(path.read_text(encoding='utf-8'))
        key=spec['pdf_sha256']
        if key in seen:
            raise ValueError('duplicate outline PDF route')
        seen.add(key)
        if route['marker_owner'] not in [s['mat_key'] for s in spec['series']] or type(route['marker_legend_index']) is not int or route['marker_legend_index']<0:
            raise ValueError('invalid outline marker binding')
        if key==identity:
            selected=dict(route,spec=spec,specification_sha256=sha(path))
    return selected


def input_hashes(root,identity):
    route=select(root,identity)
    return {'vector_outline_inputs':{'registry':sha(Path(root)/REGISTRY),
            'specification':route['specification_sha256'] if route else None,
            'implementations':{name:sha(Path(__file__).with_name(name)) for name in DEPENDENCIES}}}


def validate_owners(records,spec,source):
    for series in spec['series']:
        owners=[m for m in records['mats'] if m['mat_key']==series['mat_key']]
        if len(owners)!=1:
            raise ValueError('outline artifact owner missing or duplicate')
        if owners[0].get('custom_material_id')!=series['label'] or owners[0].get('paper_key')!=source.get('paper_key') or not source.get('paper_key'):
            raise ValueError('outline owner label or source paper mismatch')


def execute(root,identity,records,pdf,run_dir):
    route=select(root,identity)
    if route is None:
        return None
    mats=records['mats']
    for series in route['spec']['series']:
        if sum(m['mat_key']==series['mat_key'] for m in mats)!=1:
            raise ValueError('outline MAT owner not unique')
    source=[a for a in records['assets'] if a['kind']=='pdf' and a['sha256']==identity]
    if len(source)!=1:
        raise ValueError('outline PDF asset not unique')
    validate_owners(records,route['spec'],source[0])
    # Finish all extraction before touching records or publishing artifacts.
    outline=vector_outline_candidates.extract(pdf,route['spec'])
    markers=vector_marker_candidates.extract(pdf,route['spec'],route['marker_legend_index'])
    payload={'status':'CANDIDATE_NOT_FORMAL','source_pdf_asset_key':source[0]['asset_key'],
             'pdf_sha256':identity,'inputs':input_hashes(root,identity),
             'owners':[s['mat_key'] for s in route['spec']['series']],
             'marker_owner':route['marker_owner'],'outline':outline,'markers':markers}
    data=(json.dumps(payload,sort_keys=True,separators=(',',':'),allow_nan=False)+'\n').encode()
    digest=hashlib.sha256(data).hexdigest()
    relative='vector-outline-assets/'+digest+'.json'
    reference={'path':relative,'sha256':digest,'status':'CANDIDATE_NOT_FORMAL',
               'source_pdf_asset_key':source[0]['asset_key'],'pdf_sha256':identity,
               'owners':payload['owners'],'marker_owner':route['marker_owner']}
    old=records.get('extensions',{}).get('vector_outline_candidates')
    if old is not None and old!=reference:
        raise ValueError('outline candidate receipt drift')
    atomic_bytes(Path(run_dir)/relative,data)
    records.setdefault('extensions',{})['vector_outline_candidates']=reference
    return {'series':len(outline['series']),'markers':len(markers['markers'])}


def artifact_receipt(records,run_dir):
    reference=records.get('extensions',{}).get('vector_outline_candidates')
    if reference is None:
        return []
    root=Path(run_dir).resolve(); relative=Path(reference['path']); path=(root/relative).resolve()
    if relative.is_absolute() or not path.is_relative_to(root/'vector-outline-assets'):
        raise ValueError('outline artifact escapes directory')
    if sha(path)!=reference['sha256']:
        raise ValueError('outline artifact hash mismatch')
    payload=json.loads(path.read_text(encoding='utf-8'))
    if payload['status']!='CANDIDATE_NOT_FORMAL' or reference['status']!=payload['status']:
        raise ValueError('outline candidate authority mismatch')
    for key in ('source_pdf_asset_key','pdf_sha256','owners','marker_owner'):
        if payload[key]!=reference[key]:
            raise ValueError('outline artifact source binding mismatch')
    if payload['outline']['source']['pdf_sha256']!=payload['pdf_sha256'] or payload['markers']['pdf_sha256']!=payload['pdf_sha256']:
        raise ValueError('outline inner PDF binding mismatch')
    if [s['mat_key'] for s in payload['outline']['series']]!=payload['owners']:
        raise ValueError('outline inner owner binding mismatch')
    for key in payload['owners']:
        if sum(m['mat_key']==key for m in records['mats'])!=1:
            raise ValueError('outline artifact owner missing')
    sources=[a for a in records['assets'] if a['asset_key']==payload['source_pdf_asset_key'] and a['kind']=='pdf' and a['sha256']==payload['pdf_sha256']]
    if len(sources)!=1:
        raise ValueError('outline artifact source asset missing')
    route=select(Path(__file__).resolve().parents[1],payload['pdf_sha256'])
    if route is None or payload['outline']['specification']!=route['spec'] or payload['marker_owner']!=route['marker_owner']:
        raise ValueError('outline source specification drift')
    validate_owners(records,route['spec'],sources[0])
    return [copy.deepcopy(reference)]
