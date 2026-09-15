"""Read-only native-raster review proof, independent of the projection writer."""
import json
import copy
import math
from pathlib import Path

import fitz

from raster_curves import digest
from verify_raster_curves import verify, sha

METHOD = 'reviewed-raster-contract-v1'


def verified_index(project, subject):
    from review_curve_projection import safe_path, pointer, LABELS
    path = safe_path(project, subject['recordsPath'])
    if sha(path) != subject['recordsSha256']:
        raise ValueError('raster review records changed')
    records = json.loads(path.read_text(encoding='utf-8')); run = path.parent
    import raster_routes
    projection = records['extensions']['raster_curve_projection']
    route, _ = raster_routes.select(project,projection['acceptance']['pdf_sha256'])
    if route is None or route['acceptance'] is None:
        raise ValueError('raster review has no independently accepted route')
    spec, acceptance = route['specification'], route['acceptance']
    assets = {a['asset_key']: a for a in records['assets']}
    source = next(a for a in assets.values() if a['kind']=='pdf' and a['sha256']==spec['pdf_sha256'])
    proof = verify(Path(source['relative_path']), spec, run/'raster-assets')
    bindings = {'pdf_sha256':proof.get('pdf_sha256'), 'specification_sha256':proof.get('specification_sha256'),
                'report_sha256':proof.get('report_sha256'), 'helper_sha256':proof.get('implementation_sha256'),
                'projection_implementation_sha256':sha(project/'scripts/promote_raster_curves.py')}
    if (proof['verdict']!='PASS' or acceptance.get('scope')!='WAKG_RASTER_PSD_FORMAL_PROJECTION'
            or acceptance.get('verdict')!='PASS' or acceptance.get('defects') or not acceptance.get('message_id')
            or any(acceptance.get(k)!=v for k,v in bindings.items()) or acceptance.get('preserve_reported_percentiles') is not True):
        raise ValueError('raster formal source acceptance mismatch')
    if projection['acceptance']!=acceptance or projection['acceptance_sha256']!=digest(acceptance):
        raise ValueError('raster projection acceptance mismatch')
    receipt = records['extensions']['raster_curve_candidates']
    report_path = safe_path(run, receipt['report_path'])
    if sha(report_path)!=receipt['report_sha256'] or receipt['report_sha256']!=proof['report_sha256']:
        raise ValueError('raster report binding mismatch')
    report = json.loads(report_path.read_text(encoding='utf-8'))
    overlays = {p.name:sha(p) for p in (run/'raster-assets').glob('overlay-*.png')}
    if overlays!=acceptance['overlay_sha256']:
        raise ValueError('raster overlay acceptance mismatch')
    candidates = {c['label']:c for c in receipt['items']}
    owners = {m['mat_key']:m for m in records['mats']}
    links = {e['evidence_key']:e for e in records['evidence_links']}
    if len(candidates)!=len(receipt['items']) or {k:v['record_key'] for k,v in candidates.items()}!=acceptance['owners']:
        raise ValueError('raster owner receipt mismatch')
    from raster_projection_coverage import resolve as resolve_coverage
    from reviewed_raster_percentiles import resolve as resolve_dot
    coverage = resolve_coverage(acceptance,candidates)
    dots = resolve_dot(records,spec,run,Path(source['relative_path']),acceptance)
    index = {}; seen = set(); projected_percentiles=set(); preserved_percentiles=set()
    for panel in report['panels']:
        binding = panel['panel']; left,top,right,bottom = panel['image_pdf_bbox']; width,height=binding['native_size']
        def point(p): return [left+p[0]/width*(right-left),top+p[1]/height*(bottom-top)]
        def box(p): return [*point(p[:2]),*point(p[2:])]
        plot = box(binding['plot_bbox_px'])
        for series in panel['series']:
            candidate=candidates[series['label']]; owner=owners[candidate['record_key']]; key=owner['mat_key']
            if (candidate['page']!=binding['page'] or candidate['figure_number']!=binding['figure']
                    or candidate['panel_label']!=binding['panel_label']
                    or candidate['source_pdf_asset_key']!=source['asset_key']):
                raise ValueError('raster canonical source panel binding mismatch')
            if key in seen or owner['custom_material_id']!=series['label'] or owner['paper_key']!=source['paper_key']:
                raise ValueError('raster canonical owner mismatch')
            seen.add(key); curve=owner['particle_size_distribution']; asset_key=candidate['data_asset_key']
            selected = coverage[series['label']]
            asset=assets[asset_key]
            images=[a for a in assets.values() if a['kind']=='main_figure' and a['extensions'].get('page')==candidate['page'] and a['extensions'].get('figure_number')==candidate['figure_number']]
            if len(images)!=1:raise ValueError('raster canonical source image is not unique')
            image=images[0]
            expected_csv=next(a for a in report['assets'] if a.get('label')==series['label'] and a['path'].endswith('.csv'))
            if (candidate['data_asset_key']!=asset_key or asset['sha256']!=expected_csv['sha256']
                    or asset['relative_path']!='raster-assets/'+expected_csv['path']
                    or sha(safe_path(run,asset['relative_path']))!=asset['sha256']
                    or candidate['percentiles']!=series['percentiles'] or candidate['pixel_geometry_sha256']!=series['pixel_geometry_sha256']):
                raise ValueError('raster canonical data mismatch')
            if (sha(safe_path(run,image['relative_path']))!=image['sha256']
                    or image['extensions']['page']!=candidate['page']
                    or image['extensions']['figure_number']!=candidate['figure_number']
                    or not fitz.Rect(image['extensions']['source_region_bbox']).contains(fitz.Rect(plot))):
                raise ValueError('raster canonical image mismatch')
            if selected['curve']:
                if (curve['points_asset_key']!=asset_key or curve['source_image_asset_key']!=image['asset_key']
                        or asset['extensions'].get('review_status')!='confirmed'
                        or curve['extensions']['curve_calibration']!={'x_axis':binding['x_axis'],'y_axis':binding['y_axis'],'coordinate_system':'native_image_pixels'}):
                    raise ValueError('raster canonical calibration mismatch')
            dot=dots.get(series['label'])
            series=copy.deepcopy(series)
            if dot:series['percentiles']=dot['percentiles']
            raw={**series,'pdf_segments':[[point(a),point(b)] for i,(a,b) in enumerate(zip(series['pixel_points'],series['pixel_points'][1:]),1) if i not in series['gap_before_indices']]}
            claims=[('/particle_size_distribution/points_asset_key',asset_key,plot,None)] if selected['curve'] else []
            for name,item in series['percentiles'].items():
                if name not in selected['percentiles']:continue
                field='/particle_size_distribution/'+name; provenance=owner['field_provenance'][field]
                if provenance['extraction_method']!=METHOD:
                    preserved_percentiles.add((key,name))
                    value=pointer(owner,field); link=links[provenance['evidence_key']]
                    with fitz.open(source['relative_path']) as pdf:
                        if (link['record_key']!=key or link['field_path']!=field or link['asset_key']!=source['asset_key']
                                or not 1<=link['page']<=len(pdf) or provenance.get('formula') is not None
                                or float(provenance['original_value'])!=value or float(link['snippet'])!=value
                                or pdf[link['page']-1].get_textbox(fitz.Rect(link['bbox'])).strip()!=link['snippet']
                                or not item['uncertainty_interval'][0]<=value<=item['uncertainty_interval'][1]):
                            raise ValueError('raster preserved reported percentile mismatch')
                    continue
                projected_percentiles.add((key,name))
                a,b=item['bracketing_pixels']; pad=dot['pixel_uncertainty'] if dot else binding['pixel_uncertainty']
                claims.append((field,item['value'],box([min(a[0],b[0])-pad,min(a[1],b[1])-pad,max(a[0],b[0])+pad,max(a[1],b[1])+pad]),item))
            for field,value,bbox,item in claims:
                provenance=owner['field_provenance'][field];link=links[provenance['evidence_key']]
                ext={'evidence_kind':'reviewed_raster_curve_geometry','data_asset_key':asset_key,'geometry_sha256':series['pixel_geometry_sha256'],'acceptance_sha256':digest(acceptance)}
                if (pointer(owner,field)!=value or provenance['extraction_method']!=METHOD or provenance['review_status']!='confirmed'
                        or link['record_key']!=key or link['field_path']!=field or link['bbox']!=bbox
                        or link['page']!=binding['page'] or link['figure_number']!=binding['figure']
                        or link['asset_key']!=source['asset_key'] or link.get('extensions')!=ext):
                    raise ValueError('raster canonical evidence mismatch')
                if item:
                    transform=provenance['transformation'];(p0,v0),(p1,v1)=binding['x_axis']['anchors']
                    if (transform['inputs']!={'value':item['intersection_pixel'][0],'unit':'native_pixel_x','axis_pixel_bounds':[p0,p1],'axis_log10_bounds':[math.log10(v0),math.log10(v1)]}
                            or transform['output']!={'value':value,'unit':'um'} or transform['extensions']['uncertainty_interval_um']!=item['uncertainty_interval']):
                        raise ValueError('raster percentile transformation mismatch')
                    if dot and (transform['extensions'].get('dot_derivation_sha256')!=dot['report_sha256'] or transform['extensions'].get('dot_derivation_report_path')!=dot['report_path']):
                        raise ValueError('raster dot transformation provenance mismatch')
                index[link['evidence_key']]={'recordKey':key,'fieldPath':field,'value':f'{value:g}' if item else '曲线数据','unit':'µm' if item else None,
                    'page':candidate['page'],'bbox':bbox,'pdfSha256':spec['pdf_sha256'],'curveKey':None if item else asset_key,
                    'candidate':candidate,'curve':curve,'image':image,'series':raw,'label':LABELS['cumulative_finer'],'subject':subject,'asset':asset,'run':run}
    if seen!=set(acceptance['owners'].values()):
        raise ValueError('raster canonical curve coverage mismatch')
    expected={(acceptance['owners'][label],name) for label,item in coverage.items() for name in item['percentiles']}
    if projected_percentiles & preserved_percentiles or projected_percentiles | preserved_percentiles != expected:
        raise ValueError('raster percentile role coverage mismatch')
    return index
