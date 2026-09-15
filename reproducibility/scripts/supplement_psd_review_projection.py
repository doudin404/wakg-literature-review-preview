"""Exact image-coordinate review projection for independently reviewed PSD data."""
import argparse
import json
import math
import shutil
from pathlib import Path
from PIL import Image
import supplement_psd_acceptance as acceptance

MODE = 'reviewed-supplement-psd'
PROTOCOL_FILES = ('scripts/supplement_psd_review_projection.py',
                  'scripts/supplement_psd_acceptance.py', 'scripts/verify_supplement_psd.py')


def safe(root, path):
    value = (Path(root) / path).resolve()
    value.relative_to(Path(root).resolve())
    return value


def subject_for(root, run, proof, response, receipt):
    root = Path(root).resolve()
    paths = dict(zip(('records', 'proof', 'response', 'receipt'),
                     (Path(run) / 'generated-records.json', proof, response, receipt)))
    return {'protocol': MODE, 'files': {name: {
        'path': safe(root, path).relative_to(root).as_posix(),
        'sha256': acceptance.source.sha(safe(root, path))} for name, path in paths.items()},
        'implementation': {name: acceptance.source.sha(root / name) for name in PROTOCOL_FILES}}


def verified_index(root, subject):
    if (subject.get('protocol') != MODE or set(subject.get('files', {})) != {'records', 'proof', 'response', 'receipt'}
            or subject.get('implementation') != {name: acceptance.source.sha(Path(root) / name) for name in PROTOCOL_FILES}):
        raise ValueError('PSD review protocol identity mismatch')
    paths = {}
    for name, entry in subject['files'].items():
        paths[name] = safe(root, entry['path'])
        if acceptance.source.sha(paths[name]) != entry['sha256']:
            raise ValueError('PSD review subject file hash mismatch')
    run = paths['records'].parent
    value = project(run, paths['proof'], paths['response'], paths['receipt'])
    records = acceptance.source.load(paths['records'])
    pdf = next(a for a in records['assets'] if a['kind'] == 'pdf')
    if acceptance.source.sha(safe(root, run / pdf['relative_path'])) != pdf['sha256']:
        raise ValueError('PSD review main paper identity mismatch')
    owners = {m['mat_key']: m for m in records['mats']}
    curves={curve['pointsAssetKey']:curve for curve in value['curves']}
    return {field['sourceEvidenceKey']: {**field, 'subject': subject, 'image': value['image'], 'run':run,
        'curve':curves[owners[field['recordKey']]['particle_size_distribution']['points_asset_key']],
        'asset':next(a for a in records['assets'] if a['asset_key']==owners[field['recordKey']]['particle_size_distribution']['points_asset_key']),
        'pdfSha256': pdf['sha256'], 'provenance': owners[field['recordKey']]['field_provenance'][field['fieldPath']]}
        for field in value['fields']}


def verify_locator(index, locator, field, owner, pdf_sha, page):
    key = locator.get('sourceEvidenceKey')
    expected = index.get(key)
    if expected is None:
        raise ValueError('PSD locator lacks independently reopened source proof')
    image = expected['image']
    if (owner != expected['recordKey'] or pdf_sha != expected['pdfSha256']
            or locator.get('supplementPsdSubject') != expected['subject']
            or locator.get('evidenceMode') != MODE
            or any(locator.get(k) != expected[k] for k in ('recordKey', 'fieldPath', 'bbox', 'sourcePage', 'coordinateSpace'))
            or locator.get('page') != page.get('number')
            or page.get('sourceKind') != 'supplement-figure'
            or page.get('sourceArtifactSha256') != image['sha256']
            or page.get('sourcePage') != 1 or page.get('width') != image['width'] or page.get('height') != image['height']
            or field.get('evidenceKey') != locator.get('evidenceKey')
            or field.get('unit') != 'µm'
            or not math.isclose(float(field['value']), expected['value'], rel_tol=0, abs_tol=1e-10)
            or field.get('originalValue') != expected['originalValue']
            or field.get('originalUnit') != expected['originalUnit']
            or field.get('sourceFormula') != expected['formula']
            or field.get('transformation') != expected['provenance']['transformation']
            or field.get('sourceExplanationCode') != 'SOURCE_DERIVED_VALUE'
            or field.get('displayValue') != f"≈ {expected['value']:.3f}"
            or field.get('curveKey') != (expected['curve']['pointsAssetKey'] if expected['label']=='D50' else None)
            or locator.get('sourceToken') != expected['formula'] or locator.get('snippet') != expected['formula']):
        raise ValueError('PSD reviewer value, source image, or locator mismatch')
    return True


def asset_descriptors(index, run_id):
    result={}
    for entry in index.values():
        if entry['label']!='D50':continue
        curve=entry['curve'];image=entry['image'];asset=entry['asset']
        prefix=f'review-assets/{run_id}/curves/'
        # Independent small circles: sampled points are not an interpolated curve.
        points=[point for segment in curve['segments'] for point in segment['points']]
        path=' '.join(f'M {x-1:.3f} {y:.3f} a 1 1 0 1 0 2 0 a 1 1 0 1 0 -2 0' for x,y in points)
        result[curve['pointsAssetKey']]={'label':'PSD 累计粒径分布（图读有效点）',
            'pointCount':len(points),'sourceBBox':[0,0,image['width'],image['height']],
            'sourceImageUrl':prefix+image['sha256']+Path(image['relativePath']).suffix,
            'sourceImageSha256':image['sha256'],'pointsUrl':prefix+asset['sha256']+'.csv',
            'pointsSha256':asset['sha256'],'overlayPath':path,
            'valueOrigin':'image_intersection_estimate','missingPercentileCount':len(curve['missingPercentiles']),
            'connectAcrossSegments':False,'yAxis':{'unit':'%','name':'cumulative_finer'}}
    return result


def materialize(index, run_assets):
    descriptors=asset_descriptors(index,run_assets.name)
    destination=run_assets/'curves';destination.mkdir(parents=True,exist_ok=True)
    for entry in index.values():
        if entry['label']!='D50':continue
        descriptor=descriptors[entry['curve']['pointsAssetKey']]
        for source,url in ((entry['asset']['relative_path'],descriptor['pointsUrl']),
                           (entry['image']['relativePath'],descriptor['sourceImageUrl'])):
            shutil.copy2(entry['run']/source,destination/Path(url).name)
    return descriptors


def verify_assets(index,paper,site_root):
    for key,descriptor in asset_descriptors(index,paper['runId']).items():
        if paper.get('curves',{}).get(key)!=descriptor:
            raise ValueError('PSD displayed curve differs from source-bound samples')
        for url,hash_key in (('pointsUrl','pointsSha256'),('sourceImageUrl','sourceImageSha256')):
            if acceptance.source.sha(safe(site_root,descriptor[url]))!=descriptor[hash_key]:
                raise ValueError('PSD displayed curve asset hash mismatch')


def project(run, proof, response, receipt):
    run = Path(run)
    verified = acceptance.accept(run, proof, acceptance.source.load(response))
    if verified != acceptance.source.load(receipt):
        raise ValueError('PSD review receipt mismatch')
    records = acceptance.source.load(run / 'generated-records.json')
    assets = {row['asset_key']: row for row in records['assets']}
    report_asset = next(row for row in assets.values()
        if row.get('extensions', {}).get('role') == 'digitization_source_report')
    report = acceptance.source.load(run / report_asset['relative_path'])
    image = assets[report_asset['extensions']['source_image_asset_key']]
    with Image.open(run / image['relative_path']) as source_image:
        width, height = source_image.size
    curves, fields = [], []
    links = {row['evidence_key']: row for row in records['evidence_links']}
    for curve in report['curves']:
        data = next(row for row in assets.values() if row.get('kind') == 'csv'
            and row.get('extensions', {}).get('source_report_asset_key') == report_asset['asset_key']
            and row['extensions']['source_label'] == curve['material_label'])
        # Each contiguous group is independent; never join across a missing percentile.
        segments = []
        for point in curve['points']:
            if not segments or segments[-1]['id'] != point['segment_id']:
                segments.append({'id': point['segment_id'], 'points': []})
            segments[-1]['points'].append([point['pixel_x'], point['pixel_y']])
        curves.append({'sourceLabel': curve['material_label'], 'role': curve['role'],
            'pointsAssetKey': data['asset_key'], 'pointsSha256': data['sha256'],
            'segments': segments, 'missingPercentiles': curve['missing_percentiles'],
            'connectAcrossSegments': False, 'interpolatedPoints': 0,
            'valueOrigin': 'image_intersection_estimate'})
        if curve['role'] == 'reference_material_unresolved':
            continue
        owner = next(mat for mat in records['mats'] if
            mat.get('particle_size_distribution', {}).get('points_asset_key') == data['asset_key'])
        for percentile in (10, 50, 90):
            key = f'd{percentile}_um'
            if curve[key] is None:
                continue
            path = '/particle_size_distribution/' + key
            provenance = owner['field_provenance'][path]
            evidence = links[provenance['evidence_key']]
            fields.append({'recordKey': owner['mat_key'], 'fieldPath': path,
                'label': f'D{percentile}', 'value': curve[key], 'unit': 'um',
                'sourceEvidenceKey': evidence['evidence_key'],
                'sourceImageSha256': image['sha256'], 'sourcePage': 1,
                'coordinateSpace': 'image_pixels', 'bbox': evidence['bbox'],
                'formula': provenance['formula'], 'originalValue': provenance['original_value'],
                'originalUnit': provenance['original_unit'],
                'valueOrigin': 'image_intersection_estimate'})
    return {'schemaVersion': 1, 'scope': acceptance.SCOPE,
        'recordsSha256': verified['records_sha256'],
        'acceptanceSha256': acceptance.source.sha(receipt),
        'image': {'assetKey': image['asset_key'], 'sha256': image['sha256'],
                  'relativePath': image['relative_path'], 'width': width, 'height': height,
                  'coordinateSpace': 'image_pixels', 'sourcePage': 1},
        'fields': fields, 'curves': curves, 'paperAccepted': False}


def verify_projection(value, run, proof, response, receipt):
    expected = project(run, proof, response, receipt)
    if value != expected:
        raise ValueError('PSD reviewer projection differs from source-bound data')
    return expected


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    for name in ('run', 'proof', 'response', 'receipt', 'write'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--subject-write', type=Path)
    args = parser.parse_args()
    value = project(args.run, args.proof, args.response, args.receipt)
    acceptance.source.extractor._atomic_bytes(args.write,
        json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2).encode())
    if args.subject_write:
        subject = subject_for(acceptance.source.ROOT, args.run, args.proof, args.response, args.receipt)
        acceptance.source.extractor._atomic_bytes(args.subject_write,
            json.dumps(subject, sort_keys=True, ensure_ascii=False, indent=2).encode())
    print(json.dumps({'fields': len(value['fields']), 'curves': len(value['curves']),
        'scope': value['scope']}))
