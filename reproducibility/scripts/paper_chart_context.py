"""Supply existing test context and resolve explicitly named specimen types."""
import re
import copy
import json
from pathlib import Path


def figure_tasks(draft, readings):
    context = Path(readings).parent/'figure-context.json'
    saved = json.loads(context.read_bytes()).get('figure_tasks', []) if context.exists() else []
    tasks = {t['source_id']: t for t in saved}
    for task in draft.get('figure_tasks', []):
        tasks[task['source_id']] = task
    return tasks


def specimen_type(text):
    text = str(text or '').lower()
    groups = [('paste', r'\bpaste\b|净浆|浆体'), ('mortar', r'\bmortar\b|砂浆'),
              ('concrete', r'\bconcrete\b|混凝土')]
    matches = [name for name, pattern in groups if re.search(pattern, text)]
    return matches[0] if len(matches) == 1 else None


def catalogue(records, task=None, draft=None):
    result = []
    for record in records['mats'] + records['mixes']:
        identity = record.get('modules', {}).get('identity_source_specimen', {})
        result.append(dict(owner_key=record.get('mat_key') or record.get('mix_key'),
            label=record.get('custom_material_id') or identity.get('custom_test_id'),
            specimen_type=identity.get('specimen_type')))
    requested=(task or {}).get('owners',[])
    if requested:
        aliases={r['id']:r['label'] for group in ('materials','mixes') for r in (draft or {}).get(group,[])}
        selected=[]
        for key in requested:
            matched=[r for r in result if r['owner_key']==key or r['label']==aliases.get(key)]
            if not matched:return result  # Older drafts may use a different ID vocabulary.
            for row in matched:
                if row not in selected:selected.append(row)
        return selected
    return result


def methods_context(packet):
    # Keep source-linked method sentences, rather than repeat results paragraphs.
    output=[]
    for source in packet.get('sources', []):
        if source.get('kind') != 'text':continue
        sentences=re.split(r'(?<=[.!?])\s+(?=[A-Z])', ' '.join(source.get('text','').split()))
        selected=[s for s in sentences
            if re.search(r'\bpaste\b|\bmortar\b|\bconcrete\b|净浆|浆体|砂浆|混凝土',s,re.I)
            and re.search(r'\b(prepared|cast|tested|measured|used|conducted|performed|determined|investigated)\b',s,re.I)]
        if selected:
            text=' '.join(dict.fromkeys(selected))
            if not any(row['text']==text for row in output):
                output.append(dict(id=source.get('id',source.get('source_id')),page=source.get('page'),text=text))
    return output


def resolve_owner(item, owners):
    """An explicit specimen distinguishes equal labels; property names do not."""
    owner = owners[item['owner_key']]
    identity = owner.get('modules', {}).get('identity_source_specimen', {})
    wanted = specimen_type(item.get('specimen_type') or item.get('specimen'))
    if not identity or wanted is None or wanted == specimen_type(identity.get('specimen_type')):
        return owner
    matches = [r for r in owners.values()
        if r.get('modules', {}).get('identity_source_specimen', {}).get('custom_test_id') == identity.get('custom_test_id')
        and specimen_type(r.get('modules', {}).get('identity_source_specimen', {}).get('specimen_type')) == wanted]
    if len(matches) != 1:
        raise ValueError('specimen does not identify one owner: ' + str(item.get('specimen')))
    item['owner_key'] = matches[0]['mix_key']
    return matches[0]


def apply_task(result, task):
    """Apply source-backed figure context, including to saved reader responses."""
    explicit = task.get('specimen_type')
    if not explicit:
        return result
    result = copy.deepcopy(result)
    def visit(item):
        if isinstance(item, list):
            for child in item: visit(child)
        elif isinstance(item, dict):
            if item.get('owner_key'):
                item['specimen_type'] = explicit
                item.setdefault('specimen', explicit)
                if specimen_type(item.get('specimen')) not in (None, specimen_type(explicit)):
                    item['specimen'] = explicit
                series = item.get('series', item.get('label'))
                if series in task.get('series_specimens', {}):
                    item['specimen'] = task['series_specimens'][series]
            for child in item.values():
                if isinstance(child, (list, dict)): visit(child)
    visit(result['results'])
    return result
