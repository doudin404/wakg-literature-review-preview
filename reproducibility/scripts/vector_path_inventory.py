"""Source-bound vector subpaths; never interpret filled boundaries as samples."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import fitz


def geometry(items):
    result = []
    for item in items:
        if item[0] != 'l':
            raise ValueError('unsupported vector command; no silent flattening')
        result.append(['l', list(item[1]), list(item[2])])
    return result


def split_subpaths(items):
    """Preserve exact discontinuities and closure; no nearest-neighbour joining."""
    groups = []
    current = []
    for index, (_, start, end) in enumerate(geometry(items)):
        if current and current[-1]['end'] != start:
            groups.append(current)
            current = []
        current.append({'item_index': index, 'start': start, 'end': end})
        if end == current[0]['start']:
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    result = []
    for group in groups:
        points = [group[0]['start']] + [item['end'] for item in group]
        xs, ys = zip(*points)
        result.append({'items': group, 'closed': points[0] == points[-1],
                       'bbox': [min(xs), min(ys), max(xs), max(ys)],
                       'role': 'UNCLASSIFIED_SOURCE_GEOMETRY'})
    return result


def inventory(pdf: Path, specification: dict):
    sha = hashlib.sha256(pdf.read_bytes()).hexdigest()
    if sha != specification['pdf_sha256']:
        raise ValueError('source PDF hash mismatch')
    rows = []
    with fitz.open(pdf) as document:
        drawings = document[specification['page'] - 1].get_drawings()
        for series in specification['series']:
            drawing = drawings[series['drawing_index']]
            encoded = json.dumps(geometry(drawing['items']), separators=(',', ':'), ensure_ascii=True).encode()
            if hashlib.sha256(encoded).hexdigest() != series['path_geometry_sha256']:
                raise ValueError('source path geometry mismatch')
            if drawing['type'] != series['path_type']:
                raise ValueError('source path type mismatch')
            paths = split_subpaths(drawing['items'])
            rows.append({'label': series['label'], 'mat_key': series['mat_key'],
                         'drawing_index': series['drawing_index'], 'path_type': drawing['type'],
                         'stroke_width_pt': drawing.get('width', 0) or 0,
                         'subpaths': paths, 'source_item_count': len(drawing['items']),
                         'scientific_samples': None})
    return {'status': 'GEOMETRY_ONLY_NOT_SCIENTIFIC_ACCEPTANCE', 'pdf_sha256': sha,
            'page': specification['page'], 'figure': specification['figure'], 'series': rows}


if __name__ == '__main__':
    import argparse
    from vector_curves import atomic_bytes
    parser = argparse.ArgumentParser()
    parser.add_argument('--pdf', type=Path, required=True)
    parser.add_argument('--spec', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = inventory(args.pdf, json.loads(args.spec.read_text(encoding='utf-8')))
    atomic_bytes(args.output, (json.dumps(report, sort_keys=True, separators=(',', ':')) + '\n').encode())
    print(json.dumps([{'label': row['label'], 'items': row['source_item_count'],
                       'subpaths': len(row['subpaths']),
                       'closed': sum(p['closed'] for p in row['subpaths'])} for row in report['series']]))
