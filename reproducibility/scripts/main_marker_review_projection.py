"""Reopen source pixels and replay the accepted merge before PDF-point UI use."""
import copy
import hashlib
import json
from pathlib import Path

import main_marker_assets as assets
import main_marker_merge as merge
from extract_main_figure_markers import load, artifact_receipt as candidate_receipt
from source_specimen_variants import digest

MODE = 'reviewed-main-figure-markers'
FILES = ('scripts/main_marker_review_projection.py', 'scripts/main_marker_assets.py',
         'scripts/main_marker_merge.py', 'scripts/extract_main_figure_markers.py',
         'scripts/main_marker_owners.py', 'scripts/main_marker_source_context.py',
         'fixtures/main-marker-p06-coverage-review-v1.json',
         'fixtures/main-marker-p06-merge-acceptance-v1.json')


def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def safe(root, path):
    result = (Path(root)/path).resolve(); result.relative_to(Path(root).resolve()); return result


def subject_for(root, path):
    path = safe(root, path)
    return {'protocol': MODE, 'recordsPath': path.relative_to(Path(root).resolve()).as_posix(),
            'recordsSha256': sha(path), 'implementation': {p: sha(Path(root)/p) for p in FILES}}


def check_subject_inputs(root, subject):
    path = safe(root, subject['recordsPath'])
    if subject != subject_for(root, path): raise ValueError('main marker review subject drift')
    records = load(path); manifest = load(path.parent/'evidence-manifest.json')
    if (manifest['records_sha256'] != digest(records)
            or manifest['input_hashes'] != records['extensions']['input_hashes']
            or manifest.get('main_marker_artifacts') != candidate_receipt(records,path.parent)
            or manifest.get('main_marker_data_artifacts') != assets.artifact_receipt(records,path.parent)):
        raise ValueError('main marker review manifest or asset drift')
    for asset in records['assets']:
        if asset['kind'] in ('pdf','supplement','main_figure'):
            if sha(safe(root,path.parent/asset['relative_path'])) != asset['sha256']:
                raise ValueError('main marker original source bytes drift')
    return path, records


def verified_index(root, subject):
    path, records = check_subject_inputs(root,subject)
    receipt = records['extensions']['main_figure_merge']
    plan = receipt['plan']; acceptance = receipt['acceptance']
    if acceptance != load(Path(root)/'fixtures/main-marker-p06-merge-acceptance-v1.json'):
        raise ValueError('main marker independent acceptance differs')
    # Undo only this named contribution in memory, then rerun the actual source
    # merge. Neither an earlier result nor an archived executable is consumed.
    expected = copy.deepcopy(records)
    for link in expected['evidence_links']:
        if link.get('extensions',{}).get('evidence_kind') == 'main_figure_digitization':
            link['extensions'].pop('source_data_asset_key',None)
    expected['extensions']['main_figure_merge'].pop('plan')
    expected['extensions']['main_figure_merge'].pop('acceptance')
    restored = copy.deepcopy(expected)
    restored['extensions'].pop('main_figure_merge')
    mixes = {m['mix_key']:m for m in restored['mixes']}; removed = set()
    for member in reversed(receipt['members']):
        mix = mixes[member['mix_key']]; module = member['module']; i = member['index']
        row = mix['modules'][module][i]
        if member['action'] == 'SUPPORT_DIRECT_VALUE':
            removed.add(row['extensions'].pop('main_figure_support')['evidence_key'])
        else:
            if i != len(mix['modules'][module])-1: raise ValueError('main marker contribution is not append-only')
            mix['modules'][module].pop()
            for name in [k for k in mix['field_provenance'] if k.startswith(f'/modules/{module}/{i}/')]:
                removed.add(mix['field_provenance'].pop(name)['evidence_key'])
    restored['evidence_links'] = [e for e in restored['evidence_links'] if e['evidence_key'] not in removed]
    report = load(path.parent/'main-marker-assets/candidates.json')
    merge.project(restored,path.parent,report,plan,acceptance)
    if restored != expected: raise ValueError('main marker source replay differs from canonical values/context')
    links = {e['evidence_key']:e for e in records['evidence_links']}
    mixes = {m['mix_key']:m for m in records['mixes']}; index = {}
    for item, member in zip(plan['items'],receipt['members']):
        # Direct prose/table values keep their existing preferred source locator.
        # Their plot support is validated above but is not a second formal row.
        if member['action'] != 'ADD_DIGITIZED_OBSERVATION': continue
        mix = mixes[member['mix_key']]; field_path = f"/modules/performance/{member['index']}/value"
        row = mix['modules']['performance'][member['index']]; prov = mix['field_provenance'][field_path]
        key = prov['evidence_key']; link = links[key]
        index[key] = {'recordKey':mix['mix_key'],'fieldPath':field_path,'sourceEvidenceKey':key,
                      'value':row['value'],'unit':row['unit'],'displayValue':f"≈ {row['value']:.1f}",
                      'page':link['page'],'bbox':link['bbox'],'coordinateSpace':'pdf_points',
                      'formula':prov['formula'],'originalValue':prov['original_value'],
                      'originalUnit':prov['original_unit'],'transformation':prov['transformation'],
                      'sourceExplanationCode':'SOURCE_DERIVED_VALUE','pdfSha256':plan['pdf_sha256'],
                      'subject':subject}
    if len(index) != receipt['counts']['add']: raise ValueError('main marker reviewer claim coverage mismatch')
    return index


def verify_locator(index, locator, field, owner, pdf_sha):
    entry = index.get(locator.get('sourceEvidenceKey'))
    if entry is None: raise ValueError('main marker locator requires source proof')
    if (owner != entry['recordKey'] or pdf_sha != entry['pdfSha256']
            or locator.get('evidenceMode') != MODE or locator.get('mainMarkerSubject') != entry['subject']
            or any(locator.get(k) != entry[k] for k in ('recordKey','fieldPath','page','bbox','coordinateSpace'))
            or field.get('evidenceKey') != locator.get('evidenceKey')
            or locator.get('evidenceKey') != entry['sourceEvidenceKey']
            or float(field['value']) != entry['value']
            or any(field.get(k) != entry[k] for k in ('unit','displayValue','originalValue','originalUnit','transformation','sourceExplanationCode'))
            or field.get('sourceFormula') != entry['formula']
            or locator.get('sourceToken') != entry['formula'] or locator.get('snippet') != entry['formula']):
        raise ValueError('main marker reviewer value or locator mismatch')
    return True
