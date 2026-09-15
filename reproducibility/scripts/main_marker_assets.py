"""Hash-bound per-panel export of accepted plotted points, never interpolation.

The accepted merge remains the scientific authority. This adapter checks every
published value and source edge against that exact merge and exports both the
graphic value and the preferred formal value, without creating observations.
"""
import copy
import csv
import hashlib
import io
import json
from pathlib import Path

from source_specimen_variants import digest
from supplement_docx import _atomic_bytes
import main_marker_merge
import main_marker_registry

ROOT = Path(__file__).resolve().parents[1]
COVERAGE_PATH = ROOT / 'fixtures/main-marker-p06-coverage-review-v6.json'


def payload(records):
    receipt = records.get('extensions', {}).get('main_figure_merge')
    if not receipt:
        return [], {}, {}, []
    for collection, key in (('mixes', 'mix_key'), ('evidence_links', 'evidence_key'), ('assets', 'asset_key')):
        identities = [item.get(key) for item in records[collection]]
        if not all(isinstance(value, str) and value for value in identities) or len(set(identities)) != len(identities):
            raise ValueError('main marker export missing or duplicate identity: ' + collection)
    plan, acceptance = receipt['plan'], receipt['acceptance']
    review_pair=main_marker_registry.select_review(plan)
    if review_pair is None:raise ValueError('main marker export missing plan-bound coverage review')
    coverage = review_pair['coverage']
    if (digest(plan) != receipt['plan_sha256'] or digest(acceptance) != receipt['acceptance_sha256']
            or acceptance.get('verdict') != 'PASS' or acceptance.get('defects')
            or not acceptance.get('message_id') or acceptance['plan_sha256'] != digest(plan)
            or acceptance['implementation_sha256'] != hashlib.sha256(Path(main_marker_merge.__file__).read_bytes()).hexdigest()
            or coverage['verdict'] != 'PASS' or coverage.get('defects')
            or coverage['plan_sha256'] != digest(plan)
            or coverage['coverage_scope'] != 'complete_plotted_markers'):
        raise ValueError('main marker export lacks exact reviewed merge and coverage')
    for relative, sha in plan['dependencies_sha256'].items():
        if hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() != sha:
            raise ValueError('main marker export dependency drift')
    mixes = {m['mix_key']: m for m in records['mixes']}
    links = {e['evidence_key']: e for e in records['evidence_links']}
    images = [a for a in records['assets'] if a['kind'] == 'main_figure' and a['sha256'] == plan['image_sha256']]
    if len(images) != 1:
        raise ValueError('main marker export source image ambiguous or missing')
    image = images[0]
    pdfs = [a for a in records['assets'] if a['asset_key'] == plan['pdf_asset_key'] and a['sha256'] == plan['pdf_sha256']]
    if len(pdfs) != 1:
        raise ValueError('main marker export PDF identity mismatch')
    if len(receipt['members']) != len(plan['items']):
        raise ValueError('main marker export member coverage mismatch')
    rows = {}; edges = []; used = set()
    for item, member in zip(plan['items'], receipt['members']):
        mix = mixes[item['owner']['mix_key']]
        if (member['mix_key'] != mix['mix_key'] or member['module'] != item['module']
                or member['action'] != item['action']):
            raise ValueError('main marker export owner/member mismatch')
        index = member['index']; module = member['module']
        identity = (mix['mix_key'], module, index)
        if identity in used:
            raise ValueError('main marker export duplicate formal observation')
        used.add(identity)
        row = mix['modules'][module][index]; path = f'/modules/{module}/{index}/value'
        provenance = mix['field_provenance'][path]
        expected = item.get('preferred_value', item.get('value'))
        if row['name'] != item['name'] or row['unit'] != item['unit'] or row['value'] != expected:
            raise ValueError('main marker export formal value mismatch')
        if module == 'performance' and (row['specimen'] != item['owner']['specimen'] or row['age_seconds'] != item['age_seconds']):
            raise ValueError('main marker export test context mismatch')
        key = provenance['evidence_key']
        if item['action'] == 'SUPPORT_DIRECT_VALUE':
            support = row['extensions']['main_figure_support']
            if (index != item['existing_index'] or key != item['preferred_evidence_key']
                    or support['preferred_evidence_key'] != key or support['preferred_value'] != expected
                    or support['digitized_value'] != item['point']['y']):
                raise ValueError('main marker export direct source precedence mismatch')
            key = support['evidence_key']
        elif item['action'] != 'ADD_DIGITIZED_OBSERVATION':
            raise ValueError('main marker export unsupported action')
        link = links[key]; ext = link['extensions']
        if (link['record_key'] != mix['mix_key'] or link['field_path'] != path
                or link['asset_key'] != plan['pdf_asset_key'] or link['page'] != plan['page']
                or link['bbox'] != item['point']['source_pdf_bbox']
                or ext['point'] != item['point'] or ext['owner'] != item['owner']
                or ext['transformation'] != item['transformation'] or ext['plan_sha256'] != digest(plan)):
            raise ValueError('main marker export source evidence mismatch')
        panel = item['owner']['panel']
        rows.setdefault(panel, []).append([
            mix['mix_key'], item['owner']['source_test_id'], item['owner']['specimen'],
            item['name'], item['owner']['x'], item['x_axis']['unit'], item['action'],
            item['point']['y'], expected, item['unit'], item['age_seconds'],
            provenance['evidence_key'], key, *item['point']['source_pdf_bbox'],
            item['point']['uncertainty_y'], 'selected_pixel_strip_not_experimental_error'])
        edges.append({'panel': panel, 'evidence_key': key})
    if set(rows) != set(coverage['panels']):
        raise ValueError('main marker export panel coverage mismatch')
    assets = []; files = {}; panel_assets = {}
    for panel, points in sorted(rows.items()):
        axes=[i['x_axis'] for i in plan['items'] if i['owner']['panel']==panel]
        if not axes or any(axis!=axes[0] for axis in axes):
            raise ValueError('main marker export conflicting panel axis semantics')
        counts = {}
        for row in points: counts[row[3]] = counts.get(row[3], 0) + 1
        specification = coverage['panels'][panel]
        if counts != specification['series']:
            raise ValueError('main marker export series point coverage mismatch')
        out = io.StringIO(newline=''); writer = csv.writer(out, lineterminator='\n')
        writer.writerow(['mix_key', 'source_test_id', 'specimen', 'property', 'x', 'x_unit', 'action',
                         'plotted_value', 'preferred_formal_value', 'value_unit', 'age_seconds',
                         'primary_evidence_key', 'plot_evidence_key', 'pdf_x0', 'pdf_y0', 'pdf_x1', 'pdf_y1',
                         'selected_stroke_half_span', 'uncertainty_scope'])
        writer.writerows(points); data = out.getvalue().encode('utf-8')
        sha = hashlib.sha256(data).hexdigest(); relative = f'main-marker-assets/panel-{panel}-{sha}.csv'
        key = 'asset-main-marker-' + panel + '-' + sha[:24]
        asset = {'asset_key': key, 'paper_key': image['paper_key'], 'kind': 'csv', 'mime_type': 'text/csv',
                 'relative_path': relative, 'sha256': sha, 'extensions': {
                     'role': 'reviewed_plotted_markers', 'source_image_asset_key': image['asset_key'],
                     'source_pdf_asset_key': plan['pdf_asset_key'], 'figure_number': plan['figure_number'],
                     'panel_label': panel, 'figure_type': specification['figure_type'],
                     'review_status': 'confirmed', 'coverage_scope': 'complete_plotted_markers',
                     'expected_points': len(points), 'covered_points': len(points),
                     'series_point_counts': counts, 'plan_sha256': digest(plan),
                     'x_axis': axes[0],
                     'coverage_review_sha256': digest(coverage), 'coverage_review_message_id': coverage['message_id'],
                     'error_bars_visible': coverage['error_bars_visible'], 'interpolated_points': 0,
                     'experimental_error_semantics': None, 'limitations': coverage['limitations']}}
        assets.append(asset); files[relative] = data; panel_assets[panel] = key
    registry = {'schema_version': 1, 'plan_sha256': digest(plan), 'coverage_review': coverage,
                'panel_asset_keys': panel_assets, 'point_count': len(used)}
    return assets, registry, files, edges


def export_assets(records, run_dir):
    assets, registry, files, edges = payload(records)
    if not assets: return
    work = copy.deepcopy(records)
    for asset in assets:
        matches = [a for a in work['assets'] if a['asset_key'] == asset['asset_key']]
        if matches and matches != [asset]: raise ValueError('main marker export asset collision')
        if not matches: work['assets'].append(asset)
    links = {e['evidence_key']: e for e in work['evidence_links']}
    for edge in edges:
        ext = links[edge['evidence_key']]['extensions']; key = registry['panel_asset_keys'][edge['panel']]
        if ext.get('source_data_asset_key', key) != key: raise ValueError('main marker export edge conflict')
        ext['source_data_asset_key'] = key
    prior = work['extensions'].get('main_marker_assets')
    if prior is not None and prior != registry: raise ValueError('main marker export registry conflict')
    for relative, data in files.items():
        path = Path(run_dir) / relative
        if path.exists() and path.read_bytes() != data: raise ValueError('immutable main marker export conflict')
    for relative, data in files.items(): _atomic_bytes(Path(run_dir) / relative, data)
    work['extensions']['main_marker_assets'] = registry
    records.clear(); records.update(work)


def artifact_receipt(records, run_dir):
    if not records.get('extensions', {}).get('main_marker_assets'): return []
    assets, registry, files, edges = payload(records)
    if records['extensions']['main_marker_assets'] != registry: raise ValueError('main marker asset registry drift')
    result = []; links = {e['evidence_key']: e for e in records['evidence_links']}
    for edge in edges:
        if links[edge['evidence_key']]['extensions'].get('source_data_asset_key') != registry['panel_asset_keys'][edge['panel']]:
            raise ValueError('main marker asset evidence edge drift')
    for asset in assets:
        if [a for a in records['assets'] if a['asset_key'] == asset['asset_key']] != [asset]:
            raise ValueError('main marker asset metadata drift')
        path = Path(run_dir) / asset['relative_path']
        if path.read_bytes() != files[asset['relative_path']]: raise ValueError('main marker asset bytes drift')
        result.append({'relative_path': asset['relative_path'], 'size_bytes': path.stat().st_size, 'sha256': asset['sha256']})
    return result


def validated_asset_keys(records):
    """Verify the complete registry before allowing scoped panel completion."""
    if not records.get('extensions', {}).get('main_marker_assets'): return set()
    assets, registry, _, edges = payload(records)
    if records['extensions']['main_marker_assets'] != registry: raise ValueError('main marker coverage registry drift')
    links = {e['evidence_key']: e for e in records['evidence_links']}
    for edge in edges:
        if links[edge['evidence_key']]['extensions'].get('source_data_asset_key') != registry['panel_asset_keys'][edge['panel']]:
            raise ValueError('main marker coverage edge missing')
    for asset in assets:
        if [a for a in records['assets'] if a['asset_key'] == asset['asset_key']] != [asset]:
            raise ValueError('main marker coverage asset metadata mismatch')
    return {a['asset_key'] for a in assets}
