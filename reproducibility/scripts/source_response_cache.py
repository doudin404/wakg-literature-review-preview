"""Verified response reuse across operational plan/index rebinding only.

Never manufacture a new model event/usage receipt. The original completed turn
and exact scientific request content remain the authority for a cache hit.
"""
import copy
import hashlib
import json
from pathlib import Path
from source_specimen_variants import digest
from build_semantic_markdown import atomic_json


def load(path):return json.loads(Path(path).read_bytes())


def signature(request):
    result=copy.deepcopy(request)
    for k in ('request_sha256','index_sha256','plan_sha256','parent_plan_sha256'):result.pop(k,None)
    for task in ([result['task']] if 'task' in result else result.get('tasks',[])):
        task.pop('depends_on',None)
    # Image bytes/geometry must still match; only generated filesystem location
    # is operational. No record, token, owner, unit or source ID is excluded.
    for im in result.get('images',[]):
        path=im.pop('path',None)
        if path and hashlib.sha256(Path(path).read_bytes()).hexdigest()!=im['sha256']:raise ValueError('cached source image changed')
    return digest(result)


def candidates(manifest):return load(manifest)['model_directories'] if manifest else []


def reusable(request,model):
    from paper_fill_plan import completed_response
    model=Path(model)
    if request.get('request_sha256')!=digest({k:v for k,v in request.items() if k!='request_sha256'}):raise ValueError('new request hash mismatch')
    if (model/'reuse.json').exists():return None  # one hop to original completed authority
    old=load(model/'request.json')
    if old.get('request_sha256')!=digest({k:v for k,v in old.items() if k!='request_sha256'}):return None
    if signature(old)!=signature(request):return None
    raw=completed_response(model)
    if raw['request_sha256']!=old['request_sha256']:raise ValueError('cached response belongs to other request')
    return raw


def try_reuse(request,directory,manifest):
    for candidate in candidates(manifest):
        model=Path(candidate)
        response=reusable(request,model)
        if response is None:continue
        directory=Path(directory);old=load(model/'request.json')
        bound=copy.deepcopy(response);bound['request_sha256']=request['request_sha256']
        atomic_json(directory/'request.json',request)
        atomic_json(directory/'response.json',bound)
        atomic_json(directory/'reuse.json',{'origin':str(model.resolve()),'origin_request_sha256':old['request_sha256'],
            'origin_response_sha256':digest(response),'request_sha256':request['request_sha256'],
            'response_sha256':digest(bound),'scientific_signature':signature(request),'new_model_calls':0})
        return bound
    return None


def read_reused(directory):
    directory=Path(directory);receipt=load(directory/'reuse.json');request=load(directory/'request.json')
    response=reusable(request,receipt['origin'])
    if load(Path(receipt['origin'])/'request.json')['request_sha256']!=receipt['origin_request_sha256']:raise ValueError('origin request changed')
    if response is None or digest(response)!=receipt['origin_response_sha256']:raise ValueError('reused source authority changed')
    response=copy.deepcopy(response);response['request_sha256']=request['request_sha256']
    if request['request_sha256']!=receipt['request_sha256'] or digest(response)!=receipt['response_sha256'] or signature(request)!=receipt['scientific_signature'] or response!=load(directory/'response.json'):
        raise ValueError('reused source response changed')
    return response
