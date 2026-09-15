"""Exhaustive source replay for supplement PSD candidates; no acceptance grant."""
import argparse,hashlib,json,math
from pathlib import Path
import supplement_docx as extractor
from transformation_contract import validate_records

ROOT=Path(__file__).resolve().parents[1]


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def load(path):return json.loads(Path(path).read_text(encoding='utf-8'))


def verify(run,project=ROOT):
    run=Path(run).resolve();project=Path(project).resolve();run.relative_to(project)
    records_path=run/'generated-records.json';records=load(records_path)
    manifest=load(run/'evidence-manifest.json')
    if (manifest['records_sha256']!=extractor.digest(records)
            or manifest['input_hashes']!=records['extensions']['input_hashes']
            or records['extensions']['input_hashes']['supplement_implementation_sha256']!=sha(project/'scripts/supplement_docx.py')):
        raise ValueError('PSD canonical manifest or implementation mismatch')
    if validate_records(records)['verdict']!='PASS':raise ValueError('PSD numerical transformation contract failed')
    asset_rows=records['assets'];assets={a['asset_key']:a for a in asset_rows}
    links={e['evidence_key']:e for e in records['evidence_links']}
    if len(assets)!=len(asset_rows) or len(links)!=len(records['evidence_links']):
        raise ValueError('PSD duplicate canonical identity')
    def asset_bytes(asset):
        path=(run/asset['relative_path']).resolve();path.relative_to(project)
        data=path.read_bytes()
        if hashlib.sha256(data).hexdigest()!=asset['sha256']:raise ValueError('PSD asset hash mismatch')
        return data
    candidates=[a for a in asset_rows if a.get('extensions',{}).get('role')=='digitization_source_report']
    if len(candidates)!=1:raise ValueError('PSD report asset not unique')
    report_asset=candidates[0];report=json.loads(asset_bytes(report_asset))
    if (report_asset['extensions'].get('review_status')!='pending_source_path_review'
            or report_asset['extensions'].get('digitization_sha256')!=report['digitization_sha256']):
        raise ValueError('PSD source report candidate state mismatch')
    image=assets[report_asset['extensions']['source_image_asset_key']];raw=asset_bytes(image)
    spec=next(p for p in load(project/'fixtures/supplement-manifests-v1.json')['papers'] if p['run_id']==run.name)
    member=next(m for m in spec['members'] if m['kind']=='docx');docx_path=(project/member['relative_path']).resolve();docx_path.relative_to(project)
    if sha(docx_path)!=member['sha256'] or docx_path.stat().st_size!=member['bytes']:
        raise ValueError('PSD original DOCX identity mismatch')
    inventory=extractor.inspect_docx(docx_path)
    figure=next(f for f in inventory['figures'] if f['label']=='Fig. S2')
    if extractor.read_member(docx_path,figure['member'],figure['sha256'])!=raw:
        raise ValueError('PSD figure differs from original DOCX member')
    binding=spec['semantic_bindings']['Fig. S2']
    if extractor.digitize_psd(raw,binding)!=report:raise ValueError('PSD source digitization replay mismatch')
    curves={c['material_label']:c for c in report['curves']}
    if len(curves)!=len(report['curves']):raise ValueError('PSD duplicate source curve')
    csvs=[a for a in asset_rows if a['kind']=='csv' and a.get('extensions',{}).get('source_report_asset_key')==report_asset['asset_key']]
    if len(csvs)!=len(curves):raise ValueError('PSD source curve coverage mismatch')
    owners=[];seen=set();count=0
    for asset in csvs:
        ext=asset['extensions'];label=ext['source_label']
        if label not in curves or label in seen:raise ValueError('PSD curve identity mismatch')
        seen.add(label);curve=curves[label]
        if (asset['paper_key']!=image['paper_key'] or report_asset['paper_key']!=image['paper_key']
                or ext['source_image_asset_key']!=image['asset_key']
                or ext['digitization_sha256']!=report['digitization_sha256']
                or ext['missing_percentiles']!=curve['missing_percentiles']
                or ext['segment_count']!=curve['segment_count']
                or ext.get('connect_across_segments') is not False or ext.get('interpolated_points')!=0
                or ext.get('review_status')!='pending_source_path_review'
                or asset_bytes(asset)!=extractor._csv_bytes(curve['points'])):
            raise ValueError('PSD curve asset or gap receipt mismatch')
        if curve['role']=='reference_material_unresolved':
            refs=records.get('extensions',{}).get('supplement_reference_curves',{})
            expected={'source_label':label,'role':curve['role'],'points_asset_key':asset['asset_key'],
                      'source_image_asset_key':image['asset_key'],'figure_number':'Fig. S2'}
            if refs.get('schema_version')!=1 or refs.get('curves',[]).count(expected)!=1:
                raise ValueError('PSD reference curve identity mismatch')
            if any(m.get('particle_size_distribution',{}).get('points_asset_key')==asset['asset_key'] for m in records['mats']):
                raise ValueError('PSD reference curve incorrectly assigned to MAT')
            continue
        matches=[m for m in records['mats'] if m.get('particle_size_distribution',{}).get('points_asset_key')==asset['asset_key']]
        if len(matches)!=1:raise ValueError('PSD material owner not unique')
        owner=matches[0];distribution=owner['particle_size_distribution']
        if (owner['custom_material_id']!=label or owner['paper_key']!=image['paper_key']
                or distribution['source_image_asset_key']!=image['asset_key']
                or distribution['reported_curve_type']!='cumulative_finer'
                or distribution['extensions']['axis']!=report['axis']
                or distribution['extensions']['missing_percentiles']!=curve['missing_percentiles']
                or distribution['extensions'].get('connect_across_segments') is not False
                or distribution['conversion_review']['digitization_sha256']!=report['digitization_sha256']):
            raise ValueError('PSD canonical owner or calibration mismatch')
        owners.append(owner['mat_key'])
        for percentile in (10,50,90):
            name=f'd{percentile}_um';field='/particle_size_distribution/'+name
            if distribution[name]!=curve[name]:raise ValueError('PSD percentile value mismatch')
            if curve[name] is None:continue
            point=next(p for p in curve['points'] if p['cumulative_percent']==percentile)
            prov=owner['field_provenance'][field];link=links[prov['evidence_key']]
            x,y=point['pixel_x'],point['pixel_y'];box=[round(x-7,3),round(y-7,3),round(x+7,3),round(y+7,3)]
            formula=f'D{percentile} = log-axis x at cumulative distribution {percentile}%'
            if (prov['original_value']!=str(x) or prov['original_unit']!='image_px_x'
                    or prov.get('review_status')!='pending_source_path_review'
                    or distribution['conversion_review'].get('status')!='pending_source_path_review'
                    or prov['formula']!=formula or prov['extraction_method']!=report['algorithm']
                    or link['extraction_method']!=report['algorithm'] or link['snippet']!=formula
                    or link['bbox']!=box or link['record_key']!=owner['mat_key']
                    or link['record_type']!='mat' or link['field_path']!=field
                    or link['asset_key']!=image['asset_key'] or link['paper_key']!=image['paper_key']
                    or link['figure_number']!='Fig. S2' or link['page']!=1
                    or link['source_locator']['coordinate_space']!='image_pixels'
                    or prov['transformation']['inputs']['value']!=x):
                raise ValueError('PSD percentile source evidence mismatch')
            count+=1
    return {'verdict':'PASS','scope':'CANDIDATE_SOURCE_REPLAY_NOT_SEMANTIC_ACCEPTANCE','records_sha256':sha(records_path),
            'source_docx_sha256':member['sha256'],'source_image_sha256':image['sha256'],
            'report_sha256':report_asset['sha256'],'digitization_sha256':report['digitization_sha256'],
            'binding_sha256':extractor.digest(binding),'extractor_sha256':sha(project/'scripts/supplement_docx.py'),
            'verifier_sha256':sha(Path(__file__)),'csv_sha256':{a['asset_key']:a['sha256'] for a in csvs},
            'material_keys':owners,'percentile_count':count,'source_curve_count':len(curves),
            'missing_percentiles':{c['material_label']:len(c['missing_percentiles']) for c in curves.values()}}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--run',type=Path,required=True);parser.add_argument('--write',type=Path)
    args=parser.parse_args();result=verify(args.run)
    if args.write:extractor._atomic_bytes(args.write,json.dumps(result,sort_keys=True,ensure_ascii=False,indent=2).encode())
    print(json.dumps(result,ensure_ascii=False,sort_keys=True))
