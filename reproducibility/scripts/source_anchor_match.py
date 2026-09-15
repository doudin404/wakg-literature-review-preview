"""Resolve labelled table references against original cells, nearest match on ties."""
import copy
import re
from collections import Counter


def positioning_anchors(fields):
    """Keep shared header context, but locate columns using their own labels."""
    uses = Counter(cid for field in fields for cid in set(field['anchors']))
    return [[cid for cid in field['anchors'] if uses[cid] == 1] or field['anchors']
            for field in fields]


def normalized(text):
    return re.sub(r'\s+', ' ', str(text).replace('\u00ad', '')).strip().casefold()


def resolve_anchor(table, anchor):
    cells = {c['id']: c for c in table['cells']}
    if isinstance(anchor, str):
        ids, label = [anchor], ''
    else:
        ids, label = anchor.get('ids', []), anchor.get('text', '')
    if ids and all(cid in cells for cid in ids):
        return ids
    needle = normalized(label)
    if not needle:
        raise ValueError(f'Source reference has no matching cell or original label: {anchor}')
    candidates = [[c] for c in cells.values() if normalized(c['text']) == needle]
    surviving = [cells[cid] for cid in ids if cid in cells]
    if surviving and normalized(' '.join(c['text'] for c in surviving)) == needle:
        candidates.append(surviving)
    rows = {}
    for cell in cells.values():
        rows.setdefault(cell['row'], []).append(cell)
    for row in rows.values():
        row.sort(key=lambda c: c['column'])
        for start in range(len(row)):
            for stop in range(start + 2, len(row) + 1):
                span = row[start:stop]
                text = normalized(' '.join(c['text'] for c in span))
                if text == needle:
                    candidates.append(span)
                if len(text) > len(needle):
                    break
    # Multi-line headers can have other columns between their printed fragments.
    ordered = sorted(cells.values(), key=lambda c:(c['row'],c['column']))
    for start in range(len(ordered)):
        remaining = needle
        span = []
        for cell in ordered[start:]:
            part = normalized(cell['text'])
            if part and (remaining == part or remaining.startswith(part+' ')):
                span.append(cell)
                remaining = remaining[len(part):].lstrip()
                if not remaining:
                    candidates.append(span)
                    break
            elif not span:
                break
    if not candidates:
        candidates = [[c] for c in cells.values() if needle in normalized(c['text'])]
    if not candidates:
        raise ValueError(f'Original label {label!r} not found in {table["id"]}')
    positions = []
    for cid in ids:
        match = re.fullmatch(r'[^:]+:(\d+):(\d+)', cid)
        if match:
            positions.append(tuple(map(int, match.groups())))
    def distance(span):
        if not positions:
            return (0, span[0]['row'], span[0]['column'])
        r,col=positions[0]
        return (abs(span[0]['row']-r)+abs(span[0]['column']-col), span[0]['row'], span[0]['column'])
    return [c['id'] for c in min(candidates, key=distance)]


def resolved_mapping(table, mapping, issues):
    result = copy.deepcopy(mapping)
    def report(part, anchor, error):
        issues.append({'table':table['id'], 'part':part, 'source':anchor, 'reason':str(error)})
    result['owners'] = []
    owner_candidates = []
    for owner in mapping['owners']:
        if mapping.get('orientation') == 'table_record':
            result['owners'].append(copy.deepcopy(owner))
            continue
        try:
            ids = resolve_anchor(table, owner['anchor'])
            owner_candidates.append((owner, ids))
        except ValueError as error:
            report('owner', owner['anchor'], error)
    # Group labels are context shared by several records. The record's own
    # anchor, not the shared label's printed row, selects its numerical row.
    uses = Counter(cid for _, ids in owner_candidates for cid in set(ids))
    for owner, ids in owner_candidates:
        own = [cid for cid in ids if uses[cid] == 1]
        result['owners'].append({**owner, 'anchor':(own or ids)[0], 'context_anchors':ids})
    for field in result['fields']:
        original = field['anchors']
        try:
            field['anchors'] = [cid for anchor in original for cid in resolve_anchor(table, anchor)]
        except ValueError as error:
            field['anchors'] = []
            report('field', original, error)
    result['cells'] = []
    for item in mapping.get('cells', []):
        try:
            ids = resolve_anchor(table, item['ref'])
            result['cells'].append({**item, 'ref':ids[0]})
        except ValueError as error:
            report('explicit_cell', item['ref'], error)
    return result
