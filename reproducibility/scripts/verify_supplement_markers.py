"""Reopen source DOCX/PDF and exhaustively verify marker observations.

This is a deterministic pre-review proof, never a semantic acceptance grant.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path

import supplement_docx as source
from transformation_contract import validate_records

ROOT=Path(__file__).resolve().parents[1]
METHOD='source-component-marker-v2'


def load(path):return json.loads(Path(path).read_text(encoding='utf-8'))
def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def verify(run, project=ROOT):
    run=Path(run).resolve();project=Path(project).resolve();run.relative_to(project)
    records=load(run/'generated-records.json');manifest=load(run/'evidence-manifest.json')
    hashes=records['extensions']['input_hashes']
    if manifest['records_sha256']!=source.digest(records) or manifest['input_hashes']!=hashes:
        raise ValueError('marker manifest identity mismatch')
    implementations={'supplement_implementation_sha256':'supplement_docx.py',
        'marker_geometry_implementation_sha256':'marker_geometry.py',
        'marker_formulations_implementation_sha256':'marker_formulations.py'}
    if any(hashes.get(key)!=sha(project/'scripts'/name) for key,name in implementations.items()):
        raise ValueError('marker producer implementation is stale')
    if validate_records(records)['verdict']!='PASS':raise ValueError('marker transformation contract failed')
    assets={a['asset_key']:a for a in records['assets']}
    links={e['evidence_key']:e for e in records['evidence_links']}
    owners={m['mix_key']:m for m in records['mixes']}
    if len(assets)!=len(records['assets']) or len(links)!=len(records['evidence_links']) or len(owners)!=len(records['mixes']):
        raise ValueError('marker duplicate canonical identity')
    spec=next(p for p in load(project/'fixtures/supplement-manifests-v1.json')['papers'] if p['run_id']==run.name)
    member=next(m for m in spec['members'] if m['kind']=='docx')
    path=(project/member['relative_path']).resolve();path.relative_to(project)
    if sha(path)!=member['sha256'] or path.stat().st_size!=member['bytes']:
        raise ValueError('marker original DOCX identity mismatch')
    docxs=[a for a in assets.values() if a['kind']=='supplement' and a['sha256']==member['sha256']]
    if len(docxs)!=1:raise ValueError('marker original DOCX asset not unique')
    docx=docxs[0];inventory=source.inspect_docx(path)
    table=next(t for t in inventory['tables'] if t['label']=='Table S4')
    registry=records['extensions']['supplement_marker_transactions']['figures']
    seen=set();figures={};age_count=0
    for label in ('Fig. S4','Fig. S5'):
        caption=next(f for f in inventory['figures'] if f['label']==label)
        images=[a for a in assets.values() if a['kind']=='supplement_figure' and a['extensions'].get('figure_number')==label]
        if len(images)!=1:raise ValueError('marker source image not unique')
        image=images[0];image_path=(run/image['relative_path']).resolve();image_path.relative_to(project)
        raw=source.read_member(path,caption['member'],caption['sha256'])
        if image_path.read_bytes()!=raw or hashlib.sha256(raw).hexdigest()!=image['sha256']:
            raise ValueError('marker image differs from original DOCX member')
        chart=source.digitize_xy_markers(raw,spec['semantic_bindings'][label])
        binding=source.resolve_formulations(records,table,caption,chart,
            source_plan=registry[label]['formulation_binding'].get('source_plan'))
        receipt=registry[label]
        if receipt['formulation_binding']!=binding:raise ValueError('marker source formulation receipt mismatch')
        mapping={a['x']:a['canonical_test_id'] for a in binding['assignments']}
        # Exercise the actual idempotent path too, without changing canonical data.
        replay=copy.deepcopy(records)
        source._add_xy_performance(replay,{},label,image['asset_key'],chart,mapping,
            source_caption=caption,docx_asset_key=docx['asset_key'],formulation_binding=binding)
        if replay!=records:raise ValueError('marker replay appended or changed records')
        expected=[]
        for panel in chart['panels']:
            for series in panel['series']:
                for point in series['points']:
                    assignment=next(a for a in binding['assignments'] if a['x']==point['x'])
                    expected.append((assignment,panel,series,point))
        if len(receipt['members'])!=len(expected):raise ValueError('marker receipt coverage mismatch')
        for assignment,panel,series,point in expected:
            owner=owners[assignment['mix_key']]
            matches=[(i,r) for i,r in enumerate(owner['modules']['performance'])
                if r.get('name')==series['name'] and r.get('age_seconds')==series.get('age_seconds')
                and r.get('specimen')==assignment['specimen']]
            if len(matches)!=1:raise ValueError('marker observation identity missing or ambiguous')
            i,row=matches[0];identity=(owner['mix_key'],i)
            if identity in seen or {'mix_key':identity[0],'index':i} not in receipt['members']:
                raise ValueError('marker observation membership mismatch')
            seen.add(identity);prefix=f'/modules/performance/{i}'
            prov=owner['field_provenance'][prefix+'/value'];link=links[prov['evidence_key']]
            x,y=point['pixel'];ext=row['extensions'];digital=ext['digitization']
            formula='value = inverse linear pixel calibration on agent-reviewed plot axes'
            transformation=source._pixel_y_transformation(panel,series,point,formula)
            if (row['value']!=point['y'] or row['unit']!=series['y_unit'] or row['method'] is not None
                    or ext['extraction_method']!=METHOD or 'uncertainty' in ext
                    or digital['marker_geometry']!=point['marker_geometry']
                    or digital['x_from_pixel']!=point['x_from_pixel']
                    or digital['selected_stroke_half_span']!=point['uncertainty_y']
                    or digital['experimental_error_semantics'] is not None
                    or digital['includes_axis_calibration_uncertainty'] is not False
                    or digital['review_status']!='pending_source_path_review'
                    or prov['original_value']!=str(y) or prov['original_unit']!='image_px_y'
                    or prov['review_status']!='pending_source_path_review'
                    or prov['formula']!=formula or prov['transformation']!=transformation
                    or ext['transformation']!=transformation or ext['formula']!=formula
                    or ext['original_value']!=y or ext['original_unit']!='image_px_y'
                    or link['record_key']!=owner['mix_key'] or link['field_path']!=prefix+'/value'
                    or link['record_type']!='mix' or link['paper_key']!=owner['paper_key']
                    or link['page']!=1
                    or link['asset_key']!=image['asset_key'] or link['figure_number']!=label
                    or link['extraction_method']!=METHOD or link['bbox']!=[x-7,y-7,x+7,y+7]
                    or link['source_locator']['coordinate_space']!='image_pixels'):
                raise ValueError('marker value, geometry, method, or provenance mismatch')
            ref=ext['formulation_binding']
            if (ref['figure']!=label or binding['assignments'][ref['assignment_index']]!=assignment
                    or ref['scope']!=binding['scope'] or ref['review_status']!='pending_source_path_review'):
                raise ValueError('marker observation formulation reference mismatch')
            tokens={'specimen':assignment['specimen']}
            if series.get('age_seconds') is not None:tokens['age_seconds']='one-day';age_count+=1
            for field,token in tokens.items():
                p=owner['field_provenance'][prefix+'/'+field];e=links[p['evidence_key']]
                if (e['asset_key']!=docx['asset_key'] or e['record_key']!=owner['mix_key']
                        or e['field_path']!=prefix+'/'+field or e['snippet']!=token
                        or e['bbox'] is not None or e['figure_number']!=label
                        or e['extraction_method']!='docx-caption-token-v1'
                        or e['source_locator']['coordinate_space']!='docx_caption'
                        or e['extensions']['caption_text']!=caption['caption']
                        or e['extensions']['caption_body_index']!=caption['body_index']):
                    raise ValueError('marker caption context evidence mismatch')
                if field=='age_seconds':
                    expected_age=source._panel_age_transformation(86400.,token)
                    expected_age['formula']='seconds = one-day (1 day) * 86400'
                    if (p['transformation']!=expected_age or p['original_value']!=token
                            or p['original_unit']!='day' or p['review_status']!='pending_source_path_review'):
                        raise ValueError('marker age derivation differs from source caption')
        figures[label]={'image_sha256':image['sha256'],'digitization_sha256':chart['digitization_sha256'],
            'formulation_sha256':source.digest(binding),'observation_count':len(expected)}
    actual={(m['mix_key'],i) for m in records['mixes'] for i,r in enumerate(m['modules']['performance'])
        if r.get('extensions',{}).get('extraction_method')==METHOD}
    if seen!=actual:raise ValueError('marker unreviewed extra observation')
    return {'schema_version':1,'verdict':'PASS','scope':'CANDIDATE_SOURCE_REPLAY_NOT_SEMANTIC_ACCEPTANCE',
        'records_sha256':sha(run/'generated-records.json'),'source_docx_sha256':member['sha256'],
        'producer_sha256':{key:hashes[key] for key in implementations},'verifier_sha256':sha(__file__),
        'figures':figures,'observation_count':len(seen),'age_count':age_count,'paper_accepted':False}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--run',type=Path,required=True);parser.add_argument('--write',type=Path)
    args=parser.parse_args();proof=verify(args.run)
    if args.write:source._atomic_bytes(args.write,json.dumps(proof,sort_keys=True,ensure_ascii=False,indent=2).encode())
    print(json.dumps(proof,sort_keys=True))
