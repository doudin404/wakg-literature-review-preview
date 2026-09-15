"""Immutable PDF evidence index for source-first relation planning.

Lexical cues route source review; they never establish a scientific relation.
No prior extracted records, numeric values or semantic rules are consumed.
"""
import argparse
import hashlib
import json
import re
from pathlib import Path

import fitz
from vector_curves import atomic_bytes

VERSION = 'source-relation-inputs-v1'
CUES = {
    'material_identity': r'\b(materials?|precursors?|fly ash|slag|soil|metakaolin|cement|activators?)\b',
    'specimen_recipe': r'\b(specimens?|mixtures?|mix proportions?|ratios?|labelled|labeled|designated)\b',
    'curing_route': r'\b(cur(?:e|ed|ing)|demould|demold|sealed|humidity|oven)\b',
    'observation_subject': r'\b(tested|testing|strength|age|days?|hours?)\b',
    'supplement_reference': r'\b(supplementary (?:data|information|file)|supporting information|appendix)\b',
}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                      separators=(',', ':')).encode()).hexdigest()


def build(pdf, expected_sha256, run_id):
    raw_hash = hashlib.sha256(pdf.read_bytes()).hexdigest()
    if raw_hash != expected_sha256:
        raise ValueError('PDF hash mismatch')
    blocks = []
    pages = []
    with fitz.open(pdf) as document:
        for page_index, page in enumerate(document):
            page_ids = []
            # Keep native block numbers: reading order across columns is NOT
            # asserted by coordinate sorting or by the PDF extraction order.
            for block in page.get_text('blocks', sort=False):
                if block[6] != 0 or not block[4].strip():
                    continue
                text = block[4]
                anchor = {'pdf_sha256': raw_hash, 'page': page_index + 1,
                          'block_number': block[5], 'bbox': list(block[:4]),
                          'text_sha256': hashlib.sha256(text.encode()).hexdigest()}
                source_id = 'source-block-' + digest(anchor)[:24]
                item = dict(anchor, source_id=source_id, excerpt=text[:200],
                    routing_cues=[name for name, pattern in CUES.items()
                                  if re.search(pattern, text, re.I)],
                    status='SOURCE_TEXT_NOT_SEMANTIC_FACT')
                blocks.append(item)
                page_ids.append(source_id)
            pages.append({'page': page_index + 1, 'width': page.rect.width,
                          'height': page.rect.height, 'source_ids': page_ids,
                          'text_layer_status': 'PRESENT' if page_ids else 'REQUIRES_VISUAL'})
    return {'schema_version': VERSION, 'run_id': run_id, 'pdf_sha256': raw_hash,
            'parser_version': fitz.VersionBind, 'pages': pages, 'source_blocks': blocks,
            'relation_candidates': [], 'semantic_status': 'NOT_REVIEWED',
            'limitations': ['Cues are prioritization only; absence is not evidence of missing data',
                'No automatic cross-column reading order or context inheritance',
                'Supplement contents require their own source index'],
            'packet_sha256': digest([VERSION, run_id, raw_hash, fitz.VersionBind, pages, blocks])}


def verify_block(pdf, anchor):
    return verify_blocks(pdf, [anchor])[anchor['source_id']]


def verify_blocks(pdf, anchors):
    if not anchors:
        return {}
    actual_hash = hashlib.sha256(pdf.read_bytes()).hexdigest()
    if any(actual_hash != anchor['pdf_sha256'] for anchor in anchors):
        raise ValueError('PDF hash mismatch')
    if len({a['source_id'] for a in anchors}) != len(anchors):
        raise ValueError('duplicate requested source block')
    output = {}
    with fitz.open(pdf) as document:
        pages = {}
        for anchor in anchors:
            number = anchor['page']
            if number not in pages:
                pages[number] = document[number-1].get_text('blocks', sort=False)
            matches = [b for b in pages[number] if b[6] == 0 and b[5] == anchor['block_number']]
            if len(matches) != 1:
                raise ValueError('source block missing or ambiguous')
            block = matches[0]
            if list(block[:4]) != anchor['bbox'] or hashlib.sha256(block[4].encode()).hexdigest() != anchor['text_sha256']:
                raise ValueError('source anchor mismatch')
            output[anchor['source_id']] = block[4]
    return output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-catalog', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError('output must be fresh')
    papers = json.loads(args.source_catalog.read_text(encoding='utf-8'))['papers']
    if len(papers) != 10 or len({p['run_id'] for p in papers}) != 10:
        raise ValueError('requires exactly ten unique papers')
    results = []
    for paper in papers:
        packet = build(Path(paper['pdf']), paper['pdf_sha256'], paper['run_id'])
        results.append(packet)
    # Complete all source reads before publishing the batch manifest.
    for packet in results:
        atomic_bytes(args.output / (packet['run_id'] + '.json'),
                     (json.dumps(packet, ensure_ascii=False, indent=2) + '\n').encode())
    manifest = {'status': 'SOURCE_INDEX_ONLY', 'papers': [
        {'run_id': p['run_id'], 'packet_sha256': p['packet_sha256'],
         'blocks': len(p['source_blocks']), 'pages': len(p['pages'])} for p in results]}
    atomic_bytes(args.output / 'manifest.json', (json.dumps(manifest, indent=2) + '\n').encode())
    print(json.dumps(manifest))


if __name__ == '__main__':
    main()
