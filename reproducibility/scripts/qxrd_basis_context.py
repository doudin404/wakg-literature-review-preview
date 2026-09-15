"""Verify source-bound semantic denominator review without recalculating phases."""
import hashlib
import json
import re
from pathlib import Path
import fitz

REVIEW_PATH = Path(__file__).resolve().parents[1] / 'fixtures/qxrd-p06-basis-context-review-v2.json'
REGISTRY_PATH = Path(__file__).resolve().parents[1] / 'fixtures/qxrd-basis-review-registry-v1.json'


def registered_reviews():
    root = Path(__file__).resolve().parents[1]
    registry = json.loads(REGISTRY_PATH.read_bytes())
    paths = registry['reviews']
    if len(paths) != len(set(paths)):
        raise ValueError('duplicate QXRD review registration')
    result = []
    for name in paths:
        path = (root / name).resolve()
        if not path.is_relative_to(root):
            raise ValueError('QXRD review path outside project')
        raw = path.read_bytes()
        result.append((name, hashlib.sha256(raw).hexdigest(), json.loads(raw)))
    return result


def input_receipt():
    return {'registry': hashlib.sha256(REGISTRY_PATH.read_bytes()).hexdigest(),
            'reviews': {name: sha for name, sha, _ in registered_reviews()}}


def compact(text):
    return re.sub(r'\s+', '', text.replace('\xad', ''))


def verify(review, pdf_path, material):
    if hashlib.sha256(pdf_path.read_bytes()).hexdigest() != review['pdf_sha256']:
        raise ValueError('QXRD basis PDF identity mismatch')
    if material not in review.get('material_reviews', {}):
        raise ValueError('QXRD basis material not reviewed')
    scoped = review['material_reviews'][material]
    recommendation = scoped.get('denominator_recommendation')
    if recommendation not in ('sample_excluding_added_standard', 'as_reported_standard_not_reported', 'as_reported_basis_not_explicit') or not review.get('message_id'):
        raise ValueError('unsupported QXRD basis review')
    with fitz.open(pdf_path) as doc:
        text = compact(doc[review['page']-1].get_text())
    if not scoped.get('quotes') or any(compact(q) not in text for q in scoped['quotes']):
        raise ValueError('QXRD basis quotation mismatch')
    return {'reported_for_material': material, 'value_handling': 'AS_REPORTED_NO_REBASING',
        'standard_included_in_denominator': False if recommendation == 'sample_excluding_added_standard' else None,
        'review_status': 'SOURCE_CONTEXT_VERIFIED',
        'interpretation_kind': scoped['denominator_status'], 'rationale': scoped['rationale'],
        'review_message_id': review['message_id'], 'review_sha256': hashlib.sha256(
            json.dumps(review,sort_keys=True,separators=(',',':')).encode()).hexdigest(),
        'source_pdf_sha256': review['pdf_sha256'], 'source_page': review['page'],
        'source_quotes': scoped['quotes']}


def reviewed_basis(pdf_path, material):
    pdf_hash = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    matches = [review for _, _, review in registered_reviews()
               if review.get('pdf_sha256') == pdf_hash and material in review.get('material_reviews', {})]
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError('QXRD material review ambiguous')
    return verify(matches[0],pdf_path,material)
