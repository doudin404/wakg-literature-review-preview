"""Bind one independent semantic response to an exhaustive marker source proof."""
import argparse
import json
from pathlib import Path
import verify_supplement_markers as source

SCOPE='SOURCE_BOUND_MARKER_OBSERVATIONS_ONLY'
CHECKS={'marker_geometry','axis_and_series_identity','caption_age_and_specimen',
        'source_formulation_ownership','no_aggregate_recipe_transfer','not_experimental_error'}


def accept(run,proof_path,response):
    proof=source.load(proof_path)
    if proof!=source.verify(run):raise ValueError('marker proof stale or not source-replayed')
    if (response.get('verdict')!='PASS' or response.get('scope')!=SCOPE
            or response.get('proof_sha256')!=source.sha(proof_path)
            or response.get('records_sha256')!=proof['records_sha256']
            or not response.get('message_id') or response.get('defects')!=[]
            or set(response.get('checks',{}))!=CHECKS
            or any(v is not True for v in response['checks'].values())):
        raise ValueError('marker semantic response identity, scope, or checks mismatch')
    return {'schema_version':1,'verdict':'PASS','scope':SCOPE,
        'proof_sha256':source.sha(proof_path),'records_sha256':proof['records_sha256'],
        'response_sha256':source.source.digest(response),'observation_count':proof['observation_count'],
        'age_count':proof['age_count'],'paper_accepted':False,'aggregate_recipe_transfer_accepted':False}


if __name__=='__main__':
    p=argparse.ArgumentParser()
    for name in ('run','proof','response','write'):p.add_argument('--'+name,type=Path,required=True)
    args=p.parse_args();result=accept(args.run,args.proof,source.load(args.response))
    source.source._atomic_bytes(args.write,json.dumps(result,sort_keys=True,ensure_ascii=False,indent=2).encode())
    print(json.dumps(result,sort_keys=True))
