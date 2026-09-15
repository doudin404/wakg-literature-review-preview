"""Source-bound semantic alias routing, separate from recipe data merging."""
import copy
import hashlib
import math
from pathlib import Path
from recipe_merge_evidence import digest


class RecipeIdentityRequired(ValueError):
    def __init__(self, packet):
        self.packet = packet
        super().__init__('grouped recipe identity review required: ' + packet['packet_sha256'])


def packet(records, table, aliases):
    result = {'protocol': 'recipe-identity-routing-v2', 'records_sha256': digest(records),
              'routing_policy': {'SAME_SPECIMEN': 'candidate full-record merge subject to context/evidence gates',
                                 'SAME_FORMULATION': 'relation only; no values or observations transferred',
                                 'DISTINCT': 'no alias route', 'UNRESOLVED': 'internal repair'},
              'source_table': copy.deepcopy(table), 'aliases': dict(sorted(aliases.items())),
              'specimens': [{'mix_key': m['mix_key'], 'paper_key': m.get('paper_key'),
                             'identity': copy.deepcopy(m.get('modules', {}).get('identity_source_specimen', {}))}
                            for m in records.get('mixes', [])],
              'assets': copy.deepcopy(records.get('assets', [])), 'publication_allowed': False}
    result['packet_sha256'] = digest(result)
    return result


def verify_facts(facts, assets):
    import fitz
    if not isinstance(facts, list) or not facts:
        raise ValueError('identity decision requires source facts')
    for fact in facts:
        matches = [a for a in assets if a['asset_key'] == fact.get('asset_key')]
        if len(matches) != 1:
            raise ValueError('identity evidence asset not unique')
        asset = matches[0]
        path = Path(asset['relative_path'])
        if hashlib.sha256(path.read_bytes()).hexdigest() != asset['sha256']:
            raise ValueError('identity source bytes changed')
        page_number, box, quote = fact.get('page'), fact.get('bbox'), fact.get('quote')
        if type(page_number) is not int or not isinstance(box, list) or len(box) != 4:
            raise ValueError('identity evidence page/bbox invalid')
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in box):
            raise ValueError('identity evidence bbox nonfinite')
        if not isinstance(quote, str) or not quote.strip():
            raise ValueError('identity quote missing')
        with fitz.open(path) as document:
            if not 1 <= page_number <= len(document):
                raise ValueError('identity evidence page outside source')
            page = document[page_number - 1]
            rect = fitz.Rect(box)
            if rect.is_empty or not page.rect.contains(rect):
                raise ValueError('identity bbox outside source')
            text = page.get_textbox(rect)
            if ' '.join(quote.split()) not in ' '.join(text.split()):
                raise ValueError('identity quote outside source bbox')


def resolve(records, table, aliases, review=None):
    return resolve_result(records, table, aliases, review)['approved_aliases']


def resolve_result(records, table, aliases, review=None):
    if not aliases:
        return {'approved_aliases': {}, 'formulation_relations': []}
    request = packet(records, table, aliases)
    result = consume(request, review)
    if result['status'] == 'IDENTITY_UNRESOLVED_INTERNAL_REPAIR':
        error = RecipeIdentityRequired(request)
        error.resolution = result
        raise error
    return result


def reviewed_decisions(request, review):
    """Consume a production-emitted packet; producer rebinds it on replay."""
    if digest({k: v for k, v in request.items() if k != 'packet_sha256'}) != request.get('packet_sha256'):
        raise ValueError('identity request packet hash mismatch')
    if request.get('protocol') != 'recipe-identity-routing-v2':
        raise ValueError('identity request protocol mismatch')
    aliases = request['aliases']
    if review is None:
        raise RecipeIdentityRequired(request)
    if (review.get('packet_sha256') != request['packet_sha256']
            or not isinstance(review.get('reviewer'), str) or not review['reviewer'].strip()):
        raise ValueError('identity review input/reviewer mismatch')
    decisions = {}
    for item in review.get('decisions', []):
        source = item.get('source')
        if source not in aliases or source in decisions or item.get('target') != aliases[source]:
            raise ValueError('identity review pair coverage mismatch')
        if item.get('verdict') not in {'SAME_SPECIMEN', 'SAME_FORMULATION', 'DISTINCT', 'UNRESOLVED'}:
            raise ValueError('identity review verdict invalid')
        if not isinstance(item.get('rationale'), str) or not item['rationale'].strip():
            raise ValueError('identity rationale required')
        if item['verdict'] != 'UNRESOLVED':
            verify_facts(item.get('source_facts'), request['assets'])
        if item['verdict'] == 'SAME_FORMULATION':
            scope = item.get('formulation_scope')
            if (not isinstance(scope, dict) or not isinstance(scope.get('mass_basis'), str)
                    or not scope['mass_basis'].strip() or not isinstance(scope.get('components'), list)
                    or not scope['components'] or any(not isinstance(c, str) or not c.strip() for c in scope['components'])
                    or len(set(scope['components'])) != len(scope['components'])):
                raise ValueError('formulation equivalence requires explicit component and mass scope')
        decisions[source] = item
    if set(decisions) != set(aliases):
        raise ValueError('identity review must cover all candidate aliases')
    return decisions


def formulation_relations(decisions):
    return [{'source': item['source'], 'target': item['target'],
             'scope': copy.deepcopy(item['formulation_scope']),
             'source_facts': copy.deepcopy(item['source_facts']),
             'rationale': item['rationale'], 'transfer_authorized': False,
             'specimen_equivalence_established': False}
            for _, item in sorted(decisions.items()) if item['verdict'] == 'SAME_FORMULATION']


def resolve_packet(request, review):
    decisions = reviewed_decisions(request, review)
    if any(item['verdict'] == 'UNRESOLVED' for item in decisions.values()):
        raise RecipeIdentityRequired(request)
    return {source: request['aliases'][source] for source, item in decisions.items()
            if item['verdict'] == 'SAME_SPECIMEN'}


def load_review(bindings, root):
    import json
    configured = bindings.get('recipe_identity_review_file')
    if configured is None:
        return bindings.get('recipe_identity_review')
    if bindings.get('recipe_identity_review') is not None:
        raise ValueError('multiple recipe identity review sources')
    root = Path(root).resolve()
    path = (root / configured).resolve()
    if not path.is_relative_to(root):
        raise ValueError('recipe identity review outside project')
    artifact = json.loads(path.read_bytes())
    review = artifact.get('review')
    if artifact.get('status') != 'ROUTING_VERIFIED_NOT_ACCEPTANCE' or digest(review) != artifact.get('review_sha256'):
        raise ValueError('recipe identity review artifact not verified')
    # The producer still recomputes request identity and reopens source facts.
    return review


def consume(request, review):
    decisions = reviewed_decisions(request, review)
    relations = formulation_relations(decisions)
    if any(item['verdict'] == 'UNRESOLVED' for item in decisions.values()):
        return {'status': 'IDENTITY_UNRESOLVED_INTERNAL_REPAIR', 'packet_sha256': request['packet_sha256'],
                'review_sha256': digest(review), 'review': copy.deepcopy(review),
                'formulation_relations': relations,
                'publication_allowed': False}
    routes = {source: item['target'] for source, item in decisions.items() if item['verdict'] == 'SAME_SPECIMEN'}
    return {'status': 'ROUTING_VERIFIED_NOT_ACCEPTANCE', 'packet_sha256': request['packet_sha256'],
            'review_sha256': digest(review), 'review': copy.deepcopy(review),
            'approved_aliases': routes, 'formulation_relations': relations, 'publication_allowed': False}


def save_resolution(directory, result):
    """Keep operational relations outside scientific records and preserve revisions."""
    import json
    from vector_curves import atomic_bytes
    if result.get('publication_allowed') is not False:
        raise ValueError('identity resolution must remain internal')
    for name in ('packet_sha256', 'review_sha256'):
        value = result.get(name)
        if not isinstance(value, str) or len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
            raise ValueError('identity resolution requires exact input and review hashes')
    identity = digest([result['packet_sha256'], result['review_sha256']])[:32]
    path = Path(directory) / ('identity-resolution-' + identity + '.json')
    raw = (json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode()
    if path.exists():
        if path.read_bytes() != raw:
            raise ValueError('identity resolution hash path collision')
        return path
    atomic_bytes(path, raw)
    return path


if __name__ == '__main__':
    import argparse
    import json
    from vector_curves import atomic_bytes
    parser = argparse.ArgumentParser()
    parser.add_argument('--packet', type=Path, required=True)
    parser.add_argument('--review', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError('identity routing output must be fresh')
    result = consume(json.loads(args.packet.read_bytes()), json.loads(args.review.read_bytes()))
    atomic_bytes(args.output, (json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode())
    print(json.dumps({'status': result['status'], 'model_calls': 0, 'publication_allowed': False}))
