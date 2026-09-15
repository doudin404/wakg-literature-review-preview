"""Expand only source-declared joint cases; never form a Cartesian product."""
import copy
import json

CONDITION_UNITS = {'specimen_age': {'s': 1, 'min': 60, 'h': 3600, 'd': 86400},
                   'temperature': {'degC': 1}}


def case_route_id(family, case):
    """Unambiguous identity even when either source identifier contains separators."""
    return 'case:' + json.dumps([family, case], ensure_ascii=False, separators=(',', ':'))


def expand(response, request):
    result = copy.deepcopy(response)
    spans = {s['span_id']: s for s in request['spans']}
    numbers = {n['token_id']: n for n in request['tokens']}
    paper_rows = {r['row_id'] for r in request['rows']}
    case_references = {}; legacy_case_names = {}
    assignments = {}
    expanded = []
    ids = {r['route_id'] for r in response['routes']}
    for route in response['routes']:
        cases = route.get('condition_cases', [])
        parent_scope = {row for d in response['designs'] if route['route_id'] in d['route_ids'] for row in d['row_ids']}
        declaration = route.get('subject_scope')
        scope = parent_scope
        if declaration is not None:
            scope, excluded = set(declaration['row_ids']), set(declaration['excluded_row_ids'])
            if not scope or not scope <= parent_scope or scope & excluded or scope | excluded != paper_rows:
                raise ValueError('test family subject scope incomplete or contradictory')
            if not declaration['source_span_ids'] or not set(declaration['source_span_ids']) <= spans.keys():
                raise ValueError('test family subject scope source missing')
        route = copy.deepcopy(route)
        if declaration is not None:
            route['scope_normalization'] = {'coordinate_system':'PAPER_ROWS',
                'referencing_design_rows':sorted(parent_scope),
                'explicit_other_design_exclusions':sorted(excluded-parent_scope),
                'explanation':'Original paper-wide exclusions retained; included subjects must belong to a referencing design.'}
        mode = route.get('sequence_mode', 'SHARED_PREFIX')
        if mode not in ('SHARED_PREFIX', 'COMPLETE_CASE'):
            raise ValueError('unknown sequence representation')
        if mode == 'COMPLETE_CASE' and (route['stage_ids'] or not cases):
            raise ValueError('complete-case mode requires empty parent and explicit cases')
        if mode == 'SHARED_PREFIX' and not route['stage_ids']:
            raise ValueError('shared-prefix mode requires explicit common sequence')
        if not cases:
            expanded.append(copy.deepcopy(route))
            assignments[route['route_id']] = {row: [route['route_id']] if row in scope else [] for row in parent_scope}
            continue
        mapping = {row: [] for row in parent_scope}
        local_ids = set()
        for case in cases:
            local_id = case['case_id']
            if local_id in local_ids:
                raise ValueError('duplicate case ID within family')
            local_ids.add(local_id)
            cid = case_route_id(route['route_id'], local_id)
            if cid in ids:
                raise ValueError('condition case ID duplicates route or case')
            ids.add(cid)
            case_references[(route['route_id'],local_id)] = cid
            legacy_case_names.setdefault(local_id,[]).append(cid)
            include, exclude = set(case['row_ids']), set(case['excluded_row_ids'])
            if include & exclude or include | exclude != scope or not include:
                raise ValueError('condition case scope incomplete or contradictory')
            if not case['source_span_ids'] or not set(case['source_span_ids']) <= spans.keys():
                raise ValueError('condition case source missing')
            conditions = []
            names = set()
            for c in case['conditions']:
                name, unit = c['name'], c['unit']
                allowed = CONDITION_UNITS
                if name in names or name not in allowed or unit not in allowed[name]:
                    raise ValueError('condition name/unit invalid or repeated')
                names.add(name)
                if not c['source_span_ids'] or not set(c['source_span_ids']) <= spans.keys():
                    raise ValueError('condition source missing')
                n = numbers.get(c['numeric_token_id'])
                ws = [w for s in c['source_span_ids'] for w in spans[s]['locators']]
                if n is None or n['locator'] not in ws:
                    raise ValueError('condition number outside source')
                # Unit may follow a complete parallel list, e.g. 1, 3, 7 and 28 days.
                import re
                text = ' '.join(w['token'] for w in ws)
                pattern = {'s': r'\b(?:s|seconds?)\b', 'min': r'\b(?:min|minutes?)\b',
                           'h': r'\b(?:h|hours?)\b', 'd': r'\b(?:d|days?)\b',
                           'degC': r'(?:[°◦]\s*C|℃|\bC\b)'}[unit]
                if not re.search(pattern, text):
                    raise ValueError('condition unit absent from source')
                conditions.append({**copy.deepcopy(c), 'original_value': n['value'],
                                   'normalized_value': n['value'] * allowed[name][unit],
                                   'normalized_unit': 's' if name == 'specimen_age' else 'degC'})
            # Empty suffix means no additional operation, not a missing operation.
            # COMPLETE_CASE explicitly places the whole sequence in the case.
            if mode == 'COMPLETE_CASE' and not case['stage_ids']:
                raise ValueError('complete-case sequence cannot be empty')
            branch = {**copy.deepcopy(route), 'route_id': cid,
                      'description_zh': case['description_zh'],
                      'stage_ids': route['stage_ids'] + case['stage_ids'],
                      'source_span_ids': list(dict.fromkeys(route['source_span_ids'] + case['source_span_ids'])),
                      'declared_conditions': conditions, 'condition_family': route['route_id'],
                      'local_case_id': local_id}
            branch.pop('condition_cases', None)
            expanded.append(branch)
            for row in include:
                mapping[row].append(cid)
        if any(not mapping[row] for row in scope):
            raise ValueError('condition family leaves row without any declared case')
        assignments[route['route_id']] = mapping
    designs = []
    for d in response['designs']:
        groups = {}
        for row in d['row_ids']:
            routes = tuple(r for parent in d['route_ids'] for r in assignments[parent][row])
            if not routes:
                raise ValueError('row has no applicable test family')
            groups.setdefault(routes, []).append(row)
        for index, (routes, rows) in enumerate(groups.items()):
            designs.append({**copy.deepcopy(d), 'design_id': d['design_id'] if len(groups) == 1 else d['design_id'] + ':scope:' + str(index),
                            'route_ids': list(routes), 'row_ids': rows})
    result['routes'], result['designs'] = expanded, designs
    for decision in result['observations']:
        family, local = decision['route_id'], decision.get('case_id')
        if local is not None:
            if (family,local) not in case_references:
                raise ValueError('unknown family/case observation reference')
            decision['route_id'] = case_references[(family,local)]
        elif family not in ids and family in legacy_case_names:
            if len(legacy_case_names[family]) != 1:
                raise ValueError('ambiguous legacy case reference; family required')
            decision['route_id'] = legacy_case_names[family][0]
    return result
