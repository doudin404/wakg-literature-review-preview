"""Reopen recipe source cells before grouped semantic conflict resolution.

This proves source binding only, never specimen identity or scientific acceptance.
"""
import argparse
import hashlib
import json
from pathlib import Path
from recipe_merge_evidence import digest, value_at


def verify(packet):
    content = {k: v for k, v in packet.items() if k != 'packet_sha256'}
    if digest(content) != packet.get('packet_sha256'):
        raise ValueError('recipe packet hash mismatch')
    context = packet['source_context']
    key = context['source_asset_key']
    assets = [a for a in packet['proposed_assets'] if a['asset_key'] == key]
    if len(assets) != 1:
        raise ValueError('recipe source asset not unique')
    asset = assets[0]
    path = Path(asset['relative_path'])
    if hashlib.sha256(path.read_bytes()).hexdigest() != asset['sha256']:
        raise ValueError('recipe source bytes changed')
    from supplement_docx import inspect_docx
    tables = [t for t in inspect_docx(path)['tables'] if digest(t) == digest(context['source_table'])]
    if len(tables) != 1:
        raise ValueError('recipe table differs from source reconstruction')
    table = tables[0]
    records = {m['mix_key']: m for m in packet['proposed_records']}
    if len(records) != len(packet['proposed_records']):
        raise ValueError('duplicate proposed record')
    verified, unverified, seen = [], [], set()
    for link in packet['proposed_evidence']:
        identity = link['evidence_key']
        if identity in seen:
            raise ValueError('duplicate proposed evidence')
        seen.add(identity)
        if link['asset_key'] != key or link.get('extraction_method') not in {'docx-table-cell-v1', 'docx-table-row-v1'}:
            unverified.append(identity)
            continue
        if link.get('table_number') != table['label']:
            raise ValueError('recipe source table label mismatch')
        locator = link['source_locator']
        row, column = locator.get('row'), locator.get('column')
        if type(row) is not int or not 0 <= row < len(table['rows']):
            raise ValueError('invalid recipe row')
        raw_row = table['rows'][row]
        if link['extraction_method'] == 'docx-table-cell-v1':
            if type(column) is not int or not 0 <= column < len(raw_row):
                raise ValueError('invalid recipe column')
            raw = str(raw_row[column])
        else:
            if column is not None:
                raise ValueError('row evidence must not claim a cell')
            raw = ' | '.join(raw_row)
        if raw != link.get('snippet') or hashlib.sha256(raw.encode()).hexdigest() != link.get('snippet_sha256'):
            raise ValueError('recipe quote differs from source cell')
        value = value_at(records[link['record_key']], link['field_path'])
        verified.append({'evidence_key': identity, 'field_path': link['field_path'],
                         'record_key': link['record_key'], 'row': row, 'column': column,
                         'source_text': raw, 'proposed_value': value,
                         'status': 'SOURCE_TEXT_BOUND_VALUE_SEMANTICS_PENDING'})
    if not verified:
        raise ValueError('no proposed recipe sources verified')
    return {'status': 'RECIPE_SOURCE_BINDINGS_VERIFIED_NOT_ACCEPTANCE',
            'packet_sha256': packet['packet_sha256'], 'source_sha256': asset['sha256'],
            'verified': verified, 'other_evidence_requires_verification': unverified,
            'publication_allowed': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--packet', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError('output must be fresh')
    result = verify(json.loads(args.packet.read_bytes()))
    from vector_curves import atomic_bytes
    atomic_bytes(args.output, (json.dumps(result, ensure_ascii=False, indent=2) + '\n').encode())
    print(json.dumps({'status': result['status'], 'verified_sources': len(result['verified']), 'model_calls': 0}))
