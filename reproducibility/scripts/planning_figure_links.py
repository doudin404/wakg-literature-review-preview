"""Resolve explicit figure citations to an existing same-document figure ID."""
import copy
import re


def complete(response,index):
    result=copy.deepcopy(response);sources={s['source_id']:s for s in index['sources']};repairs=[]
    default=index['document'].get('sha256')
    for task in result['tasks']:
        if task['kind']!='figure' or any(sources.get(k,{}).get('kind')=='figure' for k in task['source_ids']):continue
        for sid in list(task['source_ids']):
            source=sources.get(sid)
            if not source or not source.get('document_sha256',default):continue
            refs=re.findall(r'\bFig(?:ure)?s?\.?\s*(S?\d+)\s*[a-z]?',source['text'],re.I)
            for ref in refs:
                matches=[s['source_id'] for s in sources.values() if s['kind']=='figure'
                    and s.get('document_sha256',default)==source.get('document_sha256',default)
                    and re.fullmatch(r'\s*Fig(?:ure)?\.?\s*'+re.escape(ref)+r'\s*',str(s.get('label') or s.get('source_object',{}).get('source_label','')),re.I)]
                if len(matches)==1 and matches[0] not in task['source_ids']:
                    task['source_ids'].append(matches[0]);repairs.append({'task_id':task['task_id'],'context_source_id':sid,'figure_source_id':matches[0]})
    return result,repairs
