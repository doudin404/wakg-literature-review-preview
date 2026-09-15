"""Source-bound internal relation graph; never a WAKG field extension.

This deterministic gate checks identity, verbatim evidence and scoped edges.
It deliberately does not claim that a quoted passage entails an Agent's claim.
"""
from build_source_relation_inputs import build, verify_blocks, digest
from relation_curing_stages import validate_routes

KINDS = {'material_identity', 'ingredient_addition', 'specimen_recipe',
         'curing_route', 'observation_subject'}
ENTITY_KINDS = {'material', 'specimen', 'recipe', 'curing_route', 'observation'}
ENDPOINTS = {
    'material_identity': ({'material'}, {'material'}),
    'ingredient_addition': ({'material'}, {'recipe'}),
    'specimen_recipe': ({'specimen'}, {'recipe'}),
    'curing_route': ({'specimen'}, {'curing_route'}),
    'observation_subject': ({'observation'}, {'specimen', 'material'}),
}


def unique_index(rows, key):
    result = {}
    for row in rows:
        identifier = row.get(key)
        if not isinstance(identifier, str) or not identifier.strip() or identifier in result:
            raise ValueError('missing or duplicate ' + key)
        result[identifier] = row
    return result


def validate(graph, packet, pdf):
    # Rebuild from source; a self-consistent but edited packet is not trusted.
    fresh = build(pdf, packet['pdf_sha256'], packet['run_id'])
    if fresh != packet:
        raise ValueError('source packet differs from immutable PDF reconstruction')
    if graph.get('run_id') != packet['run_id'] or graph.get('source_packet_sha256') != packet['packet_sha256']:
        raise ValueError('graph source identity mismatch')
    entities = unique_index(graph.get('entities', []), 'entity_id')
    facts = unique_index(graph.get('facts', []), 'fact_id')
    edges = unique_index(graph.get('relations', []), 'relation_id')
    if not entities or not facts or not edges:
        raise ValueError('empty graph is not a completed relationship analysis')
    for entity in entities.values():
        if entity.get('kind') not in ENTITY_KINDS or not isinstance(entity.get('source_label'), str) or not entity['source_label'].strip():
            raise ValueError('invalid entity kind or source label')
    blocks = {b['source_id']: b for b in packet['source_blocks']}
    requested = {fact.get('source_id') for fact in facts.values()}
    if not requested <= set(blocks):
        raise ValueError('unknown source block')
    hydrated = verify_blocks(pdf, [blocks[key] for key in sorted(requested)])
    for fact in facts.values():
        source_id = fact.get('source_id')
        if source_id not in blocks:
            raise ValueError('unknown source block')
        quote = fact.get('quote')
        if not isinstance(quote, str) or not quote.strip():
            raise ValueError('empty evidence quote')
        if quote not in hydrated[source_id]:
            raise ValueError('quote is not verbatim source text')
    validate_routes(entities, facts)
    claims = {}
    for edge in edges.values():
        kind = edge.get('kind')
        subject, target = edge.get('subject_id'), edge.get('target_id')
        if kind not in KINDS or subject not in entities or target not in entities:
            raise ValueError('invalid relation kind or dangling entity')
        allowed_subject, allowed_target = ENDPOINTS[kind]
        if entities[subject]['kind'] not in allowed_subject or entities[target]['kind'] not in allowed_target:
            raise ValueError('relation endpoint type mismatch')
        refs = edge.get('fact_ids')
        if not isinstance(refs, list) or not refs or len(refs) != len(set(refs)) or any(ref not in facts for ref in refs):
            raise ValueError('missing or invalid relation evidence')
        scope, exclusions = edge.get('scope_specimen_ids'), edge.get('excluded_specimen_ids')
        if not isinstance(scope, list) or not isinstance(exclusions, list):
            raise ValueError('scope and exclusions must be explicit lists')
        if len(scope) != len(set(scope)) or len(exclusions) != len(set(exclusions)) or set(scope) & set(exclusions):
            raise ValueError('duplicate or overlapping inheritance scope')
        if any(s not in entities or entities[s]['kind'] != 'specimen' for s in scope + exclusions):
            raise ValueError('scope references unknown specimen')
        if edge.get('basis') not in {'DIRECT', 'CONTEXT_INHERITANCE', 'DERIVED'}:
            raise ValueError('relation basis must be explicit')
        if kind != 'material_identity' and not scope:
            raise ValueError('non-identity relations require explicit specimen scope')
        if entities[subject]['kind'] == 'specimen' and subject not in scope:
            raise ValueError('subject specimen is outside relation scope')
        if edge['basis'] != 'DIRECT' and not str(edge.get('reasoning', '')).strip():
            raise ValueError('inherited or derived relation requires reasoning')
        # A specimen can have one recipe/complete curing route in this graph.
        # Multiple stages belong inside a route, not competing default edges.
        if kind in {'specimen_recipe', 'curing_route'}:
            for specimen in scope:
                key = (kind, specimen)
                if key in claims and claims[key] != target:
                    raise ValueError('conflicting scoped relation; cannot overwrite')
                claims[key] = target
    return {'status': 'SOURCE_BINDINGS_VALID_SEMANTICS_PENDING',
            'graph_sha256': digest(graph), 'source_packet_sha256': packet['packet_sha256'],
            'formal_publication_authorized': False}
