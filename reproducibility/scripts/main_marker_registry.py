"""Select marker configuration by frozen source identity, not run or figure IDs."""
import hashlib
import json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
REGISTRY=ROOT/'fixtures/main-marker-source-registry-v1.json'


def load_entries():
    entries=json.loads(REGISTRY.read_bytes())['sources']
    keys=[entry['pdf_sha256'] for entry in entries]
    if len(set(keys))!=len(keys):raise ValueError('ambiguous main marker source registration')
    result=[]
    for entry in entries:
        loaded={}
        for kind in ('binding','prose'):
            path=(ROOT/entry[kind]).resolve()
            if not path.is_relative_to(ROOT):raise ValueError('marker registry path outside project')
            loaded[kind]=json.loads(path.read_bytes())
        if loaded['prose']['pdf_sha256']!=entry['pdf_sha256']:
            raise ValueError('marker registry prose PDF mismatch')
        result.append((entry,loaded))
    return result


def receipt():
    return {'registry':hashlib.sha256(REGISTRY.read_bytes()).hexdigest(),
            'implementation':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'reviews':review_receipt(),
            'inputs':{entry[k]:hashlib.sha256((ROOT/entry[k]).read_bytes()).hexdigest()
                      for entry,_ in load_entries() for k in ('binding','prose')}}


def select(records,run):
    pdfs=[a for a in records['assets'] if a['kind']=='pdf']
    matches=[(entry,config) for entry,config in load_entries()
             if any(a['sha256']==entry['pdf_sha256'] for a in pdfs)]
    if not matches:return None
    if len(matches)!=1:raise ValueError('multiple main marker sources require separate source jobs')
    entry,config=matches[0]
    if sum(a['sha256']==entry['pdf_sha256'] for a in pdfs)!=1:
        raise ValueError('duplicate marker source PDF')
    config['binding']={**config['binding'],'run_id':Path(run).name}
    config['dependencies']={entry[k]:hashlib.sha256((ROOT/entry[k]).read_bytes()).hexdigest() for k in ('binding','prose')}
    return config


def review_entries():
    result=[]
    for entry in json.loads(REGISTRY.read_bytes()).get('reviews',[]):
        pair={}
        for kind in ('acceptance','coverage'):
            path=(ROOT/entry[kind]).resolve()
            if not path.is_relative_to(ROOT):raise ValueError('marker review path outside project')
            pair[kind]=json.loads(path.read_bytes())
        if not pair['acceptance'].get('plan_sha256') or pair['acceptance']['plan_sha256']!=pair['coverage'].get('plan_sha256'):
            raise ValueError('marker review pair subjects differ')
        result.append((entry,pair))
    subjects=[pair['acceptance']['plan_sha256'] for _,pair in result]
    if len(subjects)!=len(set(subjects)):raise ValueError('duplicate marker review subject')
    return result


def review_receipt():
    return {entry[k]:hashlib.sha256((ROOT/entry[k]).read_bytes()).hexdigest()
            for entry,_ in review_entries() for k in ('acceptance','coverage')}


def select_review(plan):
    from source_specimen_variants import digest
    subject=digest(plan)
    matches=[pair for _,pair in review_entries() if pair['acceptance']['plan_sha256']==subject]
    return matches[0] if matches else None
