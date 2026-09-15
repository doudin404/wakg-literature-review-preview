"""MAT candidate identity is a source observation, not a model request ID."""
import copy
from source_specimen_variants import digest


def words(candidate):
    return candidate.get('source_tokens') or candidate.get('evidence',{}).get('extensions',{}).get('support_tokens',[])


def anchor(word):
    return {k:word.get(k) for k in ('source_id','page','bbox')} | {'text':word.get('text',word.get('token'))}


def identity(candidate,mat_key,request):
    source=[anchor(w) for w in words(candidate)]
    # A legacy candidate can be attached to this document only when all exact
    # source tokens occur in its frozen request. Never infer from value alone.
    available={digest(anchor(w)) for w in request['tokens']}
    document=candidate.get('source_document_sha256')
    if document is None and source and all(digest(w) in available for w in source):document=request['document']['sha256']
    if document is None:return None
    property_id=candidate.get('field_path') or candidate.get('meaning')
    return 'candidate-mat-'+digest([mat_key,document,property_id,candidate.get('status',candidate.get('reason')),
        candidate.get('unit'),candidate.get('candidate_value'),candidate.get('existing_value'),source])[:24]


def append_candidate(mat,candidate,request):
    if candidate.get('source_document_sha256',request['document']['sha256'])!=request['document']['sha256']:
        raise ValueError('new material candidate belongs to another source document')
    pending=mat.setdefault('extensions',{}).setdefault('raw_material_candidates',[])
    key=identity(candidate,mat['mat_key'],request)
    if key is None:raise ValueError('new material candidate has no frozen source identity')
    matches=[old for old in pending if identity(old,mat['mat_key'],request)==key]
    target=matches[0] if matches else copy.deepcopy(candidate)
    if not matches:pending.append(target)
    target['candidate_key']=key;target['source_document_sha256']=request['document']['sha256']
    history=target.setdefault('source_request_history',[])
    # Retain the original assertion and all request/wording variants, including
    # duplicates already present in an older run; only their identity is merged.
    for assertion in [*matches,candidate]:
        entry={k:copy.deepcopy(v) for k,v in assertion.items() if k not in
            ('candidate_key','source_document_sha256','source_request_history')}
        if entry not in history:history.append(entry)
        for previous in assertion.get('source_request_history',[]):
            if previous not in history:history.append(copy.deepcopy(previous))
    for old in matches[1:]:pending.remove(old)
    return key
