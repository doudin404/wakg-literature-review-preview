"""Transactional evidence migration for explicitly routed MIX replacements.

Unresolved differences return one source-bound development packet; they are
never converted into a first-candidate decision or a human-review task.
"""
import copy
import hashlib
import json


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False).encode()).hexdigest()


class RecipeMergeRequired(ValueError):
    def __init__(self, packet):
        self.packet = packet
        super().__init__('recipe merge needs grouped source-context resolution: ' + packet['packet_sha256'])


def value_at(record, path):
    if not isinstance(path, str) or not path.startswith('/'):
        raise ValueError('invalid field pointer')
    value = record
    for token in path[1:].split('/'):
        token = token.replace('~1', '/').replace('~0', '~')
        if isinstance(value, list):
            if not token.isdecimal() or str(int(token)) != token:
                raise ValueError('invalid list index')
            value = value[int(token)]
        else:
            value = value[token]
    return value


def migrated_path(old, new, path):
    for module in ('performance', 'characterizations'):
        prefix = '/modules/' + module + '/'
        if not isinstance(path, str) or not path.startswith(prefix):
            continue
        index, separator, tail = path[len(prefix):].partition('/')
        if not index.isdecimal():
            raise ValueError('observation pointer requires index')
        observation = old['modules'][module][int(index)]
        matches = [i for i, item in enumerate(new['modules'][module])
                   if digest(item) == digest(observation)]
        if len(matches) != 1:
            raise ValueError('observation migration not unique')
        return prefix + str(matches[0]) + (separator + tail if separator else '')
    return path


def retained_source_issues(before, after, old, new):
    """Unchanged identity is not permission to reuse evidence for a new value."""
    old_links = {digest(e) for e in before.get('evidence_links', [])}
    issues = []
    for key in sorted(set(old) & set(new)):
        checks = [(e.get('field_path'), e.get('evidence_key'), 'evidence')
                  for e in after.get('evidence_links', [])
                  if e.get('record_type') == 'mix' and e.get('record_key') == key
                  and digest(e) in old_links]
        prior = old[key].get('field_provenance', {})
        checks += [(path, entry.get('evidence_key'), 'provenance')
                   for path, entry in new[key].get('field_provenance', {}).items()
                   if path in prior and digest(entry) == digest(prior[path])]
        for path, evidence_key, kind in checks:
            try:
                if digest(value_at(old[key], path)) == digest(value_at(new[key], path)):
                    continue
            except (ValueError, KeyError, IndexError, TypeError):
                # An unresolved old field cannot authorize a new field either.
                pass
            issues.append({'source': key, 'target': key, 'path': path,
                           'evidence_key': evidence_key, 'reference_kind': kind,
                           'reason': 'RETAINED_SOURCE_TARGET_CHANGED'})
    return issues


def migrate(before, after, replacements, source_context=None):
    """Plan all changes first. Caller publishes `after` only on success."""
    old = {m['mix_key']: m for m in before.get('mixes', [])}
    new = {m['mix_key']: m for m in after.get('mixes', [])}
    if len(old) != len(before.get('mixes', [])) or len(new) != len(after.get('mixes', [])):
        raise ValueError('duplicate MIX identity')
    retired = {a: b for a, b in replacements.items() if a != b}
    problems = retained_source_issues(before, after, old, new)
    changes, provenance = [], []
    for source, target in sorted(retired.items()):
        if source not in old or target not in new or source in new:
            raise ValueError('invalid MIX replacement map')
        if old[source].get('paper_key') != new[target].get('paper_key'):
            raise ValueError('cross-paper MIX replacement')
        # Compare source contexts before recipe rebuilding changes quantities.
        original_target = old.get(target)
        modules = set(old[source].get('modules', {})) | set((original_target or {}).get('modules', {}))
        for module in sorted(modules - {'performance', 'characterizations'}):
            left = old[source].get('modules', {}).get(module)
            right = (original_target or {}).get('modules', {}).get(module)
            if module == 'identity_source_specimen':
                # The explicit alias route concerns the label only. It cannot
                # authorize different specimen types, dimensions or test roles.
                left = {k: v for k, v in (left or {}).items() if k != 'custom_test_id'}
                right = {k: v for k, v in (right or {}).items() if k != 'custom_test_id'}
            if digest(left) != digest(right):
                problems.append({'source': source, 'target': target,
                                 'path': '/modules/' + module, 'reason': 'SOURCE_CONTEXT_CONFLICT'})
        refs = [e for e in after.get('evidence_links', [])
                if e.get('record_type') == 'mix' and e.get('record_key') == source]
        for link in refs:
            path = link.get('field_path')
            try:
                updated = migrated_path(old[source], new[target], path)
                if digest(value_at(old[source], path)) != digest(value_at(new[target], updated)):
                    raise ValueError('target field changed')
                changes.append((link, target, updated))
            except (ValueError, KeyError, IndexError, TypeError):
                problems.append({'source': source, 'target': target, 'path': path,
                                 'evidence_key': link.get('evidence_key'), 'reason': 'EVIDENCE_TARGET_CHANGED_OR_UNRESOLVED'})
        for path, entry in old[source].get('field_provenance', {}).items():
            try:
                updated = migrated_path(old[source], new[target], path)
                if digest(value_at(old[source], path)) != digest(value_at(new[target], updated)):
                    raise ValueError('provenance field changed')
                existing = new[target].get('field_provenance', {}).get(updated)
                if existing is not None and digest(existing) != digest(entry):
                    raise ValueError('provenance conflict')
                provenance.append((target, updated, entry))
            except (ValueError, KeyError, IndexError, TypeError):
                problems.append({'source': source, 'target': target, 'path': path,
                                 'reason': 'PROVENANCE_MIGRATION_REQUIRES_CONTEXT'})
    if problems:
        affected = set(retired) | set(retired.values()) | {p['source'] for p in problems} | {p['target'] for p in problems}
        packet = {'status': 'AUTOMATIC_CONTEXT_RESOLUTION_REQUIRED',
                  'source_context': copy.deepcopy(source_context),
                  'replacement_map': retired, 'issues': problems,
                  'source_records': [copy.deepcopy(old[k]) for k in sorted(affected & set(old))],
                  'proposed_records': [copy.deepcopy(new[k]) for k in sorted(affected & set(new))],
                  'source_evidence': [copy.deepcopy(e) for e in before.get('evidence_links', [])
                                      if e.get('record_type') == 'mix' and e.get('record_key') in affected],
                  'proposed_evidence': [copy.deepcopy(e) for e in after.get('evidence_links', [])
                                        if e.get('record_type') == 'mix' and e.get('record_key') in affected],
                  'source_assets': copy.deepcopy(before.get('assets', [])),
                  'proposed_assets': copy.deepcopy(after.get('assets', [])),
                  'input_sha256': digest(before), 'proposed_sha256': digest(after),
                  'publication_allowed': False}
        packet['packet_sha256'] = digest(packet)
        raise RecipeMergeRequired(packet)
    for link, target, path in changes:
        source = link['record_key']
        link.update(record_key=target, field_path=path)
        link.setdefault('extensions', {})['previous_record_key'] = source
    for target, path, entry in provenance:
        new[target].setdefault('field_provenance', {})[path] = copy.deepcopy(entry)
    if any(e.get('record_type') == 'mix' and e.get('record_key') in retired
           for e in after.get('evidence_links', [])):
        raise ValueError('retired MIX still has active evidence')
