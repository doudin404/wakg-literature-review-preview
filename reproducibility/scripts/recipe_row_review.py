"""Content-bound row-role packets; labels and table numbers never imply roles."""
import hashlib
import json


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def packet(table, attachment_sha256, data_start):
    if not attachment_sha256 or len(attachment_sha256) != 64:
        raise ValueError('recipe row packet requires attachment SHA256')
    source = {k: table.get(k) for k in ('caption', 'rows', 'layout_grid')}
    result = {'protocol': 'recipe-row-role-v1', 'attachment_sha256': attachment_sha256,
              'table_sha256': digest(source), 'source': source,
              'rows': [{'row': i, 'row_sha256': digest(row), 'raw': row}
                       for i, row in enumerate(table['rows'])]}
    result['packet_sha256'] = digest(result)
    return result


def classifications(source_packet, review):
    if review is None:
        return {}
    if 'reviews' in review:
        matches = [r for r in review['reviews'] if r.get('packet_sha256') == source_packet['packet_sha256']]
        if len(matches) != 1:
            raise ValueError('recipe review bundle missing or duplicate packet')
        review = matches[0]
    if (review.get('packet_sha256') != source_packet['packet_sha256']
            or review.get('verdict') != 'PASS' or not review.get('reviewer')):
        raise ValueError('recipe row review identity or verdict mismatch')
    expected = {r['row']: r for r in source_packet['rows']}
    result = {}
    for item in review.get('rows', []):
        i = item.get('row')
        if i not in expected or i in result or item.get('row_sha256') != expected[i]['row_sha256']:
            raise ValueError('recipe row review coverage or content mismatch')
        if item.get('role') not in {'HEADER', 'EXPERIMENTAL_FORMULATION', 'REFERENCE', 'SUMMARY', 'UNRESOLVED'}:
            raise ValueError('recipe row role unsupported')
        # The reviewer sees the complete table, not an isolated identity string.
        # Require a retained rationale; its semantics remain independently reviewed.
        if not isinstance(item.get('rationale'), str) or not item['rationale'].strip():
            raise ValueError('recipe row review rationale missing')
        result[i] = item
    if set(result) != set(expected):
        raise ValueError('recipe row review must cover the complete table')
    return result


def load_review(bindings, root):
    from pathlib import Path
    configured = bindings.get('recipe_row_review_file')
    if configured is None:
        return bindings.get('recipe_row_review')
    root = Path(root).resolve()
    path = (root / configured).resolve()
    if not path.is_relative_to(root):
        raise ValueError('recipe review path outside project')
    return json.loads(path.read_text(encoding='utf-8'))


def verify_supporting_sources(review, assets):
    """Invalidate semantic context when a cited main document is absent/changed."""
    from pathlib import Path
    for source in (review or {}).get('supporting_sources', []):
        matches = [a for a in assets if a.get('sha256') == source.get('sha256')
                   and a.get('kind') == source.get('kind')]
        if len(matches) != 1:
            raise ValueError('recipe review supporting source missing or ambiguous')
        if hashlib.sha256(Path(matches[0]['relative_path']).read_bytes()).hexdigest() != source['sha256']:
            raise ValueError('recipe review supporting source content changed')


def main():
    import argparse
    import os
    import re
    import tempfile
    from pathlib import Path
    parser = argparse.ArgumentParser()
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    from supplement_docx import inspect_docx
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads(args.manifest.read_text(encoding='utf-8'))
    packets = []
    for paper in manifest['papers']:
        for member in paper.get('members', []):
            if member.get('kind') != 'docx':
                continue
            path = (root / member['relative_path']).resolve()
            if not path.is_relative_to(root) or hashlib.sha256(path.read_bytes()).hexdigest() != member['sha256']:
                raise ValueError('recipe packet source path or hash mismatch')
            inventory = inspect_docx(path)
            for table in inventory['tables']:
                if re.search(r'mix(?:ture)?\s+proportions?|mix\s+design|formulation|recipe', table.get('caption', ''), re.I):
                    packets.append({'run_id': paper['run_id'], 'source_path': member['relative_path'],
                                    'source_label': table['label'], 'packet': packet(table, member['sha256'], 0)})
    result = {'protocol': 'recipe-row-role-v1', 'packets': packets,
              'instruction': 'Classify every row, including headers, using source context; unresolved is not an experiment.'}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
    fd, temporary = tempfile.mkstemp(dir=args.output.parent, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(raw)
        os.replace(temporary, args.output)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print(json.dumps({'packets': len(packets), 'output': str(args.output)}))


if __name__ == '__main__':
    main()
