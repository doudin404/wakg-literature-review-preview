"""Original-contract PSD projection behind exact source and semantic receipts."""
import copy
import hashlib
import json
import math
from pathlib import Path

import fitz
from raster_curves import coordinate,digest
from verify_raster_curves import verify,sha

VERSION='reviewed-raster-contract-v1'
SCOPE='WAKG_RASTER_PSD_FORMAL_PROJECTION'


def pdf_point(panel,x,y):
    left,top,right,bottom=panel['image_pdf_bbox']
    width,height=panel['panel']['native_size']
    return [left+x/width*(right-left),top+y/height*(bottom-top)]


def pdf_box(panel,box):
    return [*pdf_point(panel,*box[:2]),*pdf_point(panel,*box[2:])]


def assign(target,key,value):
    if target.get(key) is not None and target[key]!=value:
        raise ValueError('raster projection would overwrite existing field: '+key)
    target[key]=value


def promote(records,specification,run_dir,acceptance):
    result=copy.deepcopy(records)
    receipt=result['extensions']['raster_curve_candidates']
    source=next(a for a in result['assets'] if a.get('kind')=='pdf' and a['sha256']==specification['pdf_sha256'])
    proof=verify(Path(source['relative_path']),specification,run_dir/'raster-assets')
    if proof['verdict']!='PASS':
        raise ValueError('raster replay proof rejected')
    expected={'pdf_sha256':proof['pdf_sha256'],'specification_sha256':proof['specification_sha256'],
              'report_sha256':proof['report_sha256'],'helper_sha256':proof['implementation_sha256'],
              'projection_implementation_sha256':sha(Path(__file__))}
    if acceptance.get('verdict')!='PASS' or acceptance.get('scope')!=SCOPE or acceptance.get('defects') or not acceptance.get('message_id'):
        raise ValueError('formal raster semantic acceptance missing')
    if any(acceptance.get(k)!=v for k,v in expected.items()):
        raise ValueError('raster acceptance subject mismatch')
    if receipt['report_sha256']!=proof['report_sha256'] or receipt['specification_sha256']!=proof['specification_sha256']:
        raise ValueError('canonical raster candidate subject mismatch')
    report=json.loads((run_dir/'raster-assets/report.json').read_text(encoding='utf-8'))
    source_csvs={a['label']:a for a in report['assets']}
    overlays={k:v for k,v in proof['member_hashes'].items() if k.endswith('.png')}
    if acceptance.get('overlay_sha256')!=overlays or acceptance.get('preserve_reported_percentiles') is not True:
        raise ValueError('raster overlay or reported-value policy not accepted')
    candidates={i['label']:i for i in receipt['items']}
    if len(candidates)!=len(receipt['items']) or acceptance.get('owners')!={label:c['record_key'] for label,c in candidates.items()}:
        raise ValueError('raster owner acceptance mismatch')
    owners={m['mat_key']:m for m in result['mats']}
    assets={a['asset_key']:a for a in result['assets']}
    links={e['evidence_key']:e for e in result['evidence_links']}
    acceptance_hash=digest(acceptance)
    projected=[];preserved=[]
    from raster_projection_coverage import resolve as resolve_coverage
    coverage = resolve_coverage(acceptance,candidates)
    from reviewed_raster_percentiles import resolve
    dot_derivations = resolve(result,specification,run_dir,Path(source['relative_path']),acceptance)

    def evidence(mat,path,box,original,unit,asset_key,geometry,transform=None):
        key='ev-raster-formal-'+digest([mat['mat_key'],path,acceptance_hash])[:24]
        description='Reviewed raster PSD source geometry'
        link={'evidence_key':key,'asset_key':source['asset_key'],'paper_key':mat['paper_key'],
              'record_type':'mat','record_key':mat['mat_key'],'field_path':path,'page':binding['page'],
              'section':None,'table_number':None,'figure_number':binding['figure'],'bbox':box,
              'snippet':description,'snippet_sha256':hashlib.sha256(description.encode()).hexdigest(),
              'extraction_method':VERSION,'confidence':.98,
              'extensions':{'evidence_kind':'reviewed_raster_curve_geometry','data_asset_key':asset_key,
                            'geometry_sha256':geometry,'acceptance_sha256':acceptance_hash}}
        if key in links and links[key]!=link:
            raise ValueError('raster evidence drift')
        if key not in links:
            result['evidence_links'].append(link);links[key]=link
        provenance={'evidence_key':key,'original_value':original,'original_unit':unit,
                    'formula':transform['formula'] if transform else None,'extraction_method':VERSION,
                    'confidence':.98,'review_status':'confirmed'}
        if transform:provenance['transformation']=transform
        assign(mat.setdefault('field_provenance',{}),path,provenance)

    for panel in report['panels']:
        binding=panel['panel']
        if binding['curve_type']!='cumulative_finer' or binding['x_axis']['scale']!='log10':
            raise ValueError('unsupported raster formal curve type')
        image_matches=[a for a in result['assets'] if a['kind']=='main_figure' and a['extensions'].get('figure_number')==binding['figure'] and a['extensions'].get('page')==binding['page']]
        if len(image_matches)!=1:
            raise ValueError('raster source figure not unique')
        image=image_matches[0]
        image_path=(run_dir/image['relative_path']).resolve()
        if not image_path.is_relative_to(run_dir.resolve()) or sha(image_path)!=image['sha256']:
            raise ValueError('raster source figure asset mismatch')
        plot=pdf_box(panel,binding['plot_bbox_px'])
        if not fitz.Rect(image['extensions']['source_region_bbox']).contains(fitz.Rect(plot)):
            raise ValueError('raster plot outside source figure')
        for series in panel['series']:
            candidate=candidates[series['label']]
            mat=owners.get(candidate['record_key'])
            if mat is None or mat['custom_material_id']!=series['label'] or mat['paper_key']!=source['paper_key']:
                raise ValueError('raster material owner mismatch')
            asset_key=candidate['data_asset_key'];asset=assets[asset_key]
            expected_csv=source_csvs[series['label']]
            if (asset['sha256']!=expected_csv['sha256'] or asset['relative_path']!='raster-assets/'+expected_csv['path']
                    or asset['paper_key']!=source['paper_key'] or candidate['percentiles']!=series['percentiles']
                    or candidate['pixel_geometry_sha256']!=series['pixel_geometry_sha256']
                    or sha(run_dir/asset['relative_path'])!=asset['sha256']):
                raise ValueError('raster canonical CSV hash mismatch')
            psd=mat['particle_size_distribution']
            selected = coverage[series['label']]
            if selected['curve'] and any(not math.isfinite(x) or x <= 0 or not math.isfinite(y) or not 0 <= y <= 100 for x,y in series['points']):
                raise ValueError('complete cumulative raster curve is outside its physical domain')
            # Canonical source CSV/report checks above use the original trace.
            # Only explicitly accepted, independently replayed dot derivations
            # may supply derived percentile geometry; the raw curve stays intact.
            dot = dot_derivations.get(series['label'])
            series = copy.deepcopy(series)
            if dot:
                series['percentiles'] = dot['percentiles']
            intervals={k:v['uncertainty_interval'] for k,v in series['percentiles'].items() if k in selected['percentiles'] and v.get('status')=='CANDIDATE'}
            for name,item in series['percentiles'].items():
                if name not in selected['percentiles']:
                    continue
                if item.get('status')!='CANDIDATE' or not item.get('calibration_uncertainty_included'):
                    raise ValueError('raster percentile uncertainty is not qualified')
                path='/particle_size_distribution/'+name
                old=psd.get(name);provenance=mat.get('field_provenance',{}).get(path,{})
                if old is not None and provenance.get('extraction_method')!=VERSION:
                    link=links.get(provenance.get('evidence_key'))
                    if not link or link['record_key']!=mat['mat_key'] or link['field_path']!=path or provenance.get('formula') is not None:
                        raise ValueError('existing reported PSD value lacks direct owned evidence')
                    if (link['asset_key']!=source['asset_key'] or isinstance(old,bool) or not math.isfinite(old)
                            or float(provenance['original_value'])!=old
                            or str(provenance.get('original_unit')).replace('μ','u').replace('µ','u')!='um'):
                        raise ValueError('reported PSD source value/unit mismatch')
                    with fitz.open(source['relative_path']) as document:
                        if not isinstance(link['page'],int) or not 1<=link['page']<=len(document) or document[link['page']-1].get_textbox(fitz.Rect(link['bbox'])).strip()!=link['snippet'] or float(link['snippet'])!=old:
                            raise ValueError('reported PSD source readback mismatch')
                    if not item['uncertainty_interval'][0]<=old<=item['uncertainty_interval'][1]:
                        raise ValueError('raster estimate conflicts with reported PSD value')
                    preserved.append({'record_key':mat['mat_key'],'field_path':path,'reported_value':old,'raster_candidate':item['value']})
                    continue
                value=item['value'];x,y=item['intersection_pixel']
                (p0,v0),(p1,v1)=binding['x_axis']['anchors']
                transform={'kind':'log_axis_percentile','formal':True,'formula':item['formula'],
                           'inputs':{'value':x,'unit':'native_pixel_x','axis_pixel_bounds':[p0,p1],
                                     'axis_log10_bounds':[math.log10(v0),math.log10(v1)]},
                           'output':{'value':value,'unit':'um'},
                           'forward_check':{'computed':coordinate(x,binding['x_axis']),'recorded':value,'tolerance':1e-8},
                           'reverse_check':{'computed':coordinate(value,binding['x_axis'],reverse=True),'recorded':x,'tolerance':1e-8},
                           'extensions':{'uncertainty_interval_um':item['uncertainty_interval'],'uncertainty_basis':item['uncertainty_basis']}}
                if dot:
                    transform['extensions'].update({'dot_derivation_report_path':dot['report_path'],
                                                     'dot_derivation_sha256':dot['report_sha256']})
                a,b=item['bracketing_pixels'];pad=dot['pixel_uncertainty'] if dot else binding['pixel_uncertainty']
                box=pdf_box(panel,[min(a[0],b[0])-pad,min(a[1],b[1])-pad,max(a[0],b[0])+pad,max(a[1],b[1])+pad])
                assign(psd,name,value)
                evidence(mat,path,box,x,'native_pixel_x',asset_key,series['pixel_geometry_sha256'],transform)
            if selected['curve']:
                for key,value in {'points_asset_key':asset_key,'source_image_asset_key':image['asset_key'],'reported_curve_type':'cumulative_finer'}.items():
                    assign(psd,key,value)
                assign(psd,'conversion_review',{'status':'confirmed','algorithm':VERSION,'acceptance_sha256':acceptance_hash,'uncertainty_intervals_um':intervals})
                assign(psd.setdefault('extensions',{}),'curve_calibration',{'x_axis':binding['x_axis'],'y_axis':binding['y_axis'],'coordinate_system':'native_image_pixels'})
                evidence(mat,'/particle_size_distribution/points_asset_key',plot,series['pixel_geometry_sha256'],'native_raster_geometry',asset_key,series['pixel_geometry_sha256'])
                asset['extensions'].update({'review_status':'confirmed','source_image_asset_key':image['asset_key'],'acceptance_sha256':acceptance_hash})
            projected.append(mat['mat_key'])
    if len(projected)!=len(candidates):
        raise ValueError('raster formal projection incomplete')
    result['extensions']['raster_curve_projection']={'version':VERSION,'acceptance':copy.deepcopy(acceptance),
        'acceptance_sha256':acceptance_hash,'series':len(projected),'preserved_reported_percentiles':preserved,'whole_paper_accepted':False}
    if 'projection_coverage' in acceptance:
        result['extensions']['raster_curve_projection']['coverage'] = copy.deepcopy(coverage)
    records.clear();records.update(result)
    return result['extensions']['raster_curve_projection']
