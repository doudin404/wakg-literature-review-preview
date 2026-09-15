"""Bind a reviewed disposition to the exact attachment and source object."""
import hashlib
import json
from pathlib import Path


def configured_reviews(bindings, root):
    """Explicit per-attachment configuration; never fall back to another paper."""
    paths = ([bindings['object_scope_review']] if bindings.get('object_scope_review') else [])
    paths += bindings.get('reference_scope_reviews', [])
    if bindings.get('recipe_row_review_file'):
        paths.append(bindings['recipe_row_review_file'])
    result = []
    root = Path(root).resolve()
    for path in paths:
        resolved = (root / path).resolve()
        if not resolved.is_relative_to(root):
            raise ValueError('scope review must be inside project')
        raw = resolved.read_bytes()
        result.append({'path': path, 'sha256': hashlib.sha256(raw).hexdigest(),
                       'review': json.loads(raw)})
    return result


def reviewed_objects(review, docx_sha256, source_items):
    """Produce decisions only for exact reviewed content in this attachment."""
    if (not review or review.get('docx_sha256') != docx_sha256
            or review.get('verdict') != 'PASS_WITH_EXPLICIT_SCOPE'
            or not review.get('reviewer')):
        return []
    results = []
    hashes = [s.get('content_sha256') for s in source_items if s.get('content_sha256')]
    if len(hashes) != len(set(hashes)):
        raise ValueError('ambiguous duplicate source content')
    for source in source_items:
        matches = [r for r in review.get('items', [])
                   if r.get('content_sha256') == source.get('content_sha256')
                   and r.get('label') == source.get('source_label')
                   and (not r.get('kind') or r['kind'] == source.get('kind'))
                   and r.get('content_sha256')]
        if len(matches) > 1:
            raise ValueError('ambiguous source scope review')
        if matches:
            results.append({'source': source, 'reviewed': matches[0]})
    return results


def decision(review, docx_sha256, label, content_sha256, expected):
    if review.get('verdict') != 'PASS_WITH_EXPLICIT_SCOPE' or not review.get('reviewer'):
        raise ValueError('supplement scope review not passed')
    if review.get('docx_sha256') != docx_sha256:
        raise ValueError('supplement scope attachment changed')
    matches = [item for item in review.get('items', []) if item.get('label') == label]
    if len(matches) != 1:
        raise ValueError('supplement scope object missing or duplicate')
    item = matches[0]
    if item.get('content_sha256') != content_sha256 or item.get('decision') != expected:
        raise ValueError('supplement scope content or decision mismatch')
    return item
