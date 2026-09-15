"""Inventory-first identities: retain sources before consulting plan or review."""
import copy
import hashlib
import json
from collections import Counter


def assemble(inventory, plan):
    attachment = inventory['docx']['sha256']
    objects = []
    for kind, collection, hash_key in [('table', 'tables', 'rows_sha256'),
                                        ('figure', 'figures', 'sha256')]:
        for index, item in enumerate(inventory.get(collection, [])):
            identity = [attachment, kind, index, item.get('body_index'), item.get(hash_key)]
            key = hashlib.sha256(json.dumps(identity, separators=(',', ':')).encode()).hexdigest()
            objects.append({'object_key': key, 'kind': kind, 'inventory_index': index,
                'source_docx_sha256': attachment, 'content_sha256': item.get(hash_key),
                'source_label': item.get('label'), 'source': copy.deepcopy(item)})
    counts = Counter(o['content_sha256'] for o in objects if o['content_sha256'])
    labels = Counter(o['source_label'] for o in objects)
    plans = [s for s in plan.get('source_items', []) if s.get('source_scope') == 'supplement_content']
    for obj in objects:
        matches = [s for s in plans if s.get('source_label') == obj['source_label']
                   and s.get('content_sha256') == obj['content_sha256'] and obj['content_sha256']]
        obj['plan_source_item_ids'] = [s['item_id'] for s in matches]
        obj['issues'] = []
        if labels[obj['source_label']] > 1:
            obj['issues'].append('DUPLICATE_SOURCE_LABEL_AMBIGUOUS')
        if not obj['content_sha256']:
            obj['issues'].append('CONTENT_IDENTITY_MISSING')
        elif counts[obj['content_sha256']] > 1:
            obj['issues'].append('DUPLICATE_SOURCE_CONTENT_AMBIGUOUS')
        if len(matches) != 1:
            obj['issues'].append('PLAN_BINDING_UNRESOLVED')
        obj['status'] = 'QUARANTINED_SOURCE_BINDING' if obj['issues'] else 'BOUND_NOT_ACCEPTED'
    return objects


def unmatched_plan_items(objects, plan):
    bound = {identity for obj in objects for identity in obj['plan_source_item_ids']}
    return [{'source_item': copy.deepcopy(s), 'status': 'PLAN_OBJECT_NOT_IN_INVENTORY'}
            for s in plan.get('source_items', [])
            if s.get('source_scope') == 'supplement_content' and s['item_id'] not in bound]


if __name__ == '__main__':
    import argparse
    from pathlib import Path
    from supplement_docx import inspect_docx
    parser = argparse.ArgumentParser()
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--batch', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    reports = []
    for paper in json.loads(args.manifest.read_bytes())['papers']:
        plan_path = args.batch / paper['run_id'] / 'extraction-plan.json'
        raw = plan_path.read_bytes()
        for member in paper['members']:
            if member['kind'] != 'docx':
                continue
            source = Path(member['relative_path'])
            if hashlib.sha256(source.read_bytes()).hexdigest() != member['sha256']:
                raise ValueError('attachment hash drift')
            objects = assemble(inspect_docx(source), json.loads(raw))
            reports.append({'run_id': paper['run_id'], 'source_sha256': member['sha256'],
                            'plan_sha256': hashlib.sha256(raw).hexdigest(), 'objects': objects})
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump({'status': 'SOURCE_INVENTORY_NOT_EXTRACTION_ACCEPTANCE',
                   'papers': reports, 'publication_allowed': False}, stream, ensure_ascii=False, indent=2)
    print(json.dumps([{'run': r['run_id'], 'objects': len(r['objects']),
                       'unresolved': [o['source_label'] for o in r['objects'] if o['issues']]}
                      for r in reports]))
