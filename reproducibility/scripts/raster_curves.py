"""Native-image curve candidates with frozen calibration; never formal promotion.

The uppermost saturated stroke rule is only appropriate for a cumulative curve
above lighter histogram fills. The binding must explicitly select that mode.
No smoothing, isotonic correction, extrapolation or gap filling is performed.
Monotone foreground-path selection preserves discarded raw candidates for audit.
"""
from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import io
import json
import math
import itertools
from pathlib import Path

import fitz
import numpy as np
from PIL import Image, ImageDraw

from vector_curves import coordinate, atomic_bytes, digest

VERSION = 'native-raster-curve-candidate-v1'


def monotone_path(points,tolerance):
    """Select an ordered foreground path; do not alter any measured coordinate."""
    scores=[1]*len(points)
    previous=[None]*len(points)
    for i,point in enumerate(points):
        for j in range(i):
            if point[1] <= points[j][1]+tolerance and scores[j]+1>scores[i]:
                scores[i]=scores[j]+1
                previous[i]=j
    end=max(range(len(points)),key=lambda i:scores[i])
    selected=[]
    while end is not None:
        selected.append(end)
        end=previous[end]
    selected.reverse()
    keep=set(selected)
    return [points[i] for i in selected],[point for i,point in enumerate(points) if i not in keep]


def trace(rgb, panel, series):
    if panel.get('trace_mode') == 'single_stroke_per_column':
        return trace_intensity(rgb,panel,series)
    if panel.get('trace_mode') != 'uppermost_saturated_cumulative_stroke':
        raise ValueError('unsupported raster trace mode')
    if panel.get('curve_type') != 'cumulative_finer':
        raise ValueError('this tracer requires cumulative finer curves')
    x0,y0,x1,y1=panel['plot_bbox_px']
    if not (0 <= x0 < x1 <= rgb.shape[1] and 0 <= y0 < y1 <= rgb.shape[0]):
        raise ValueError('plot exceeds native image')
    target=np.array(series['rgb'],dtype=float)
    distances=np.linalg.norm(rgb.astype(float)-target,axis=2)
    mask=distances <= series['color_distance']
    allowed=np.zeros(mask.shape,dtype=bool)
    allowed[y0:y1,x0:x1]=True
    mask &= allowed
    for a,b,c,d in panel.get('exclude_boxes_px',[]):
        mask[b:d,a:c]=False
    points=[]
    for x in range(x0,x1):
        ys=np.flatnonzero(mask[:,x])
        if not len(ys):
            continue
        groups=np.split(ys,np.flatnonzero(np.diff(ys)>1)+1)
        groups=[g for g in groups if len(g)>=series.get('min_vertical_pixels',2)]
        if groups:
            points.append([x,float(np.median(groups[0]))])
    if len(points)<20:
        raise ValueError('insufficient source stroke pixels')
    raw_points=points
    points,discarded=monotone_path(points,panel.get('pixel_uncertainty',2))
    gaps=[i for i in range(1,len(points)) if points[i][0]-points[i-1][0]>1]
    backwards=[i for i in range(1,len(points)) if points[i][1]-points[i-1][1]>panel.get('pixel_uncertainty',2)]
    return {'label':series['label'],'pixel_points':points,'gap_before_indices':gaps,
            'raw_pixel_candidates':raw_points,'discarded_pixel_candidates':discarded,
            'foreground_selection':'longest_nonincreasing_cumulative_path_with_pixel_tolerance',
            'foreground_ambiguous':len(discarded)/len(raw_points)>0.05,
            'backwards_indices':backwards,
            'points':[[coordinate(x,panel['x_axis']),coordinate(y,panel['y_axis'])] for x,y in points],
            'status':'CANDIDATE_REQUIRES_OVERLAY_AND_SEMANTIC_REVIEW'}


def connected_stroke(mask,box,min_span_fraction):
    """Select a unique long connected stroke; never join fragmented components."""
    from scipy import ndimage
    if not 0<min_span_fraction<=1:raise ValueError('invalid connected stroke span fraction')
    x0,y0,x1,y1=box
    labels,count=ndimage.label(mask[y0:y1,x0:x1],structure=np.ones((3,3)))
    objects=ndimage.find_objects(labels)
    candidates=[];components=[]
    for index,slices in enumerate(objects,1):
        if slices is None:continue
        ys,xs=slices
        span=(xs.stop-xs.start)/(x1-x0)
        components.append({'component':index,'bbox':[x0+xs.start,y0+ys.start,x0+xs.stop,y0+ys.stop],
                           'pixel_count':int(np.count_nonzero(labels[slices]==index)),'span_fraction':span})
        if span>=min_span_fraction:candidates.append(index)
    if len(candidates)!=1:raise ValueError('connected source stroke missing or ambiguous')
    selected=np.zeros(mask.shape,dtype=bool)
    selected[y0:y1,x0:x1]=labels==candidates[0]
    return selected,{'selected_component':candidates[0],'components':components,
                     'min_span_fraction':min_span_fraction,'interpolated_pixels':0}


def fit_colour_mixture(rgb,swatch_box):
    """Fit source background-to-ink direction, including swatch compression noise."""
    samples=rgb[::4,::4].reshape(-1,3)
    colours,counts=np.unique(samples,axis=0,return_counts=True)
    background=colours[np.argmax(counts)].astype(float)
    a,b,c,d=swatch_box;pixels=rgb[b:d,a:c].reshape(-1,3).astype(float)
    # Keep actual coloured swatch pixels, not background/neutral label ink.
    saturation=np.ptp(pixels,axis=1)
    pixels=pixels[saturation>12]
    if len(pixels)<3:raise ValueError('mixture swatch lacks colour samples')
    delta=background-pixels
    lengths=np.linalg.norm(delta,axis=1)
    selected=delta[lengths>=np.quantile(lengths,0.5)]
    _,_,axes=np.linalg.svd(selected,full_matrices=False)
    direction=axes[0]
    if np.dot(direction,selected.mean(axis=0))<0:direction=-direction
    residual=np.linalg.norm(selected-np.outer(selected@direction,direction),axis=1)
    tolerance=max(3.0,float(np.quantile(residual,0.95))*2)
    if tolerance>12:raise ValueError('swatch does not support a single colour mixture')
    return {'background':background.tolist(),'direction':direction.tolist(),
            'residual_tolerance':tolerance,'minimum_signal':3*tolerance,
            'source_samples':len(selected),'model':'source_background_ink_ray_v1'}


def mixture_mask(rgb,model):
    delta=np.asarray(model['background'])-rgb.astype(float)
    direction=np.asarray(model['direction']);signal=delta@direction
    residual=np.linalg.norm(delta-signal[...,None]*direction,axis=2)
    return (signal>=model['minimum_signal']) & (residual<=model['residual_tolerance'])


def trace_intensity(rgb,panel,series):
    """Non-monotone intensity candidates; ambiguous columns remain gaps."""
    if panel.get('curve_type')!='intensity':raise ValueError('intensity mode requires intensity curve')
    for axis in ('x_axis','y_axis'):
        if not panel[axis].get('unit'):raise ValueError('intensity axis unit unresolved')
    x0,y0,x1,y1=panel['plot_bbox_px']
    if not (0<=x0<x1<=rgb.shape[1] and 0<=y0<y1<=rgb.shape[0]):raise ValueError('plot exceeds native image')
    mask=np.linalg.norm(rgb.astype(float)-np.array(series['rgb'],dtype=float),axis=2)<=series['color_distance']
    chromatic_support=None
    if series.get('colour_mixture'):
        mask=mixture_mask(rgb,series['colour_mixture'])
    elif series.get('chromatic_identity'):
        # Brightness changes from antialiasing must not turn a coloured legend
        # into a match for neutral guides or another trace's pale edge.
        colour=np.asarray(series['rgb'],dtype=float)
        reference=colour-colour.mean();norm=float(np.linalg.norm(reference))
        if norm<8:raise ValueError('chromatic selection requires a coloured swatch')
        chroma=rgb.astype(float)-rgb.astype(float).mean(axis=2,keepdims=True)
        lengths=np.linalg.norm(chroma,axis=2)
        cosine=np.sum(chroma*reference,axis=2)/np.maximum(lengths*norm,1e-12)
        chromatic_support=(lengths>=max(8,norm*0.25)) & (cosine>=0.97)
        mask &= chromatic_support
        # The swatch may be a pale edge of a darker stroke. Only actual
        # same-chroma, non-white interior pixels may reconnect its two edges.
        chromatic_support &= rgb.mean(axis=2)<=colour.mean()+series['color_distance']
    for a,b,c,d in panel.get('exclude_boxes_px',[]):mask[b:d,a:c]=False
    component_selection=None
    if 'connected_min_span_fraction' in series:
        mask,component_selection=connected_stroke(mask,panel['plot_bbox_px'],series['connected_min_span_fraction'])
    points=[];ambiguous=[];raw=[];spans=[];ambiguous_spans=[]
    for x in range(x0,x1):
        ys=np.flatnonzero(mask[y0:y1,x])+y0
        if not len(ys):continue
        groups=np.split(ys,np.flatnonzero(np.diff(ys)>1)+1)
        if chromatic_support is not None:
            joined=[]
            for group in groups:
                if joined and np.all(chromatic_support[joined[-1][-1]+1:group[0],x]):
                    joined[-1]=np.arange(joined[-1][0],group[-1]+1)
                else:joined.append(group)
            groups=joined
        groups=[g for g in groups if len(g)>=series.get('min_vertical_pixels',1)]
        candidates=[[x,float(np.median(g))] for g in groups]
        raw.extend(candidates)
        if len(groups)==1:
            points.extend(candidates)
            spans.append({'x':x,'y_min':int(groups[0][0]),'y_max':int(groups[0][-1])})
        elif groups:
            ambiguous.append(x)
            ambiguous_spans.append({'x':x,'segments':[{'y_min':int(g[0]),'y_max':int(g[-1])} for g in groups]})
    if len(points)<20 and not panel.get('allow_partial_curve',False):raise ValueError('insufficient unambiguous intensity pixels')
    calibrated={axis:panel[axis]['unit']!='pixel' for axis in ('x_axis','y_axis')}
    return {'label':series['label'],'pixel_points':points,
            'points':[[coordinate(x,panel['x_axis']) if calibrated['x_axis'] else None,
                       coordinate(y,panel['y_axis']) if calibrated['y_axis'] else None] for x,y in points],
            'axis_calibration':{axis:{'status':'CALIBRATED_CANDIDATE' if calibrated[axis] else 'UNAVAILABLE_PIXEL_GEOMETRY_ONLY',
                                      'unit':panel[axis]['unit'] if calibrated[axis] else None} for axis in calibrated},
            'gap_before_indices':[i for i in range(1,len(points)) if points[i][0]-points[i-1][0]>1],
            'raw_pixel_candidates':raw,'discarded_pixel_candidates':[p for p in raw if p[0] in ambiguous],
            'ambiguous_columns':ambiguous,'foreground_ambiguous':bool(ambiguous),
            'ambiguous_pixel_spans':ambiguous_spans,
            'pixel_stroke_spans':spans,
            'component_selection':component_selection,
            'colour_mixture':series.get('colour_mixture'),
            'representative_scope':'stroke_midpoint_not_peak_height',
            'peak_height_quantified':False,
            'foreground_selection':'unique_contiguous_stroke_per_column_no_monotonic_filter',
            'backwards_indices':[], 'status':'CANDIDATE_REQUIRES_OVERLAY_AND_SEMANTIC_REVIEW'}


def axis_variants(axis):
    bounds=axis.get('anchor_pixel_intervals')
    if bounds is None:
        return [axis]
    if len(bounds)!=2 or any(len(b)!=2 or not all(math.isfinite(v) for v in b) or b[0]>b[1] for b in bounds):
        raise ValueError('invalid calibration intervals')
    if any(not b[0]<=anchor[0]<=b[1] for b,anchor in zip(bounds,axis['anchors'])):
        raise ValueError('nominal anchor outside source interval')
    if not (bounds[0][1]<bounds[1][0] or bounds[1][1]<bounds[0][0]):
        raise ValueError('calibration anchor intervals overlap')
    return [{**axis,'anchors':[[a,axis['anchors'][0][1]],[b,axis['anchors'][1][1]]]}
            for a,b in itertools.product(*bounds)]


def percentile(series,panel,percent):
    if series.get('foreground_ambiguous'):
        return {'status':'QUARANTINED','reason':'EXCESSIVE_FOREGROUND_DISCARD'}
    target=coordinate(percent,panel['y_axis'],reverse=True)
    points=series['pixel_points']
    crossings=[(a,b) for a,b in zip(points,points[1:]) if a[1]>=target>b[1]]
    if len(crossings)!=1:
        return {'status':'QUARANTINED','reason':'NON_UNIQUE_OR_MISSING_CROSSING','crossings':len(crossings)}
    a,b=crossings[0]
    if b[0]-a[0]>panel['max_percentile_gap_px']:
        return {'status':'QUARANTINED','reason':'DASH_GAP_EXCEEDS_LIMIT','bracketing_pixels':[a,b]}
    x=a[0]+(target-a[1])/(b[1]-a[1])*(b[0]-a[0])
    pad=panel.get('pixel_uncertainty',2)
    envelope=[a[0],b[0]]
    x_variants=axis_variants(panel['x_axis'])
    y_variants=axis_variants(panel['y_axis'])
    target_bounds=[coordinate(percent,axis,reverse=True) for axis in y_variants]
    for displaced in (min(target_bounds)-pad,max(target_bounds)+pad):
        edges=[(left,right) for left,right in zip(points,points[1:]) if left[1]>=displaced>right[1]]
        if len(edges)!=1 or edges[0][1][0]-edges[0][0][0]>panel['max_percentile_gap_px']:
            return {'status':'QUARANTINED','reason':'UNCERTAINTY_CROSSING_NOT_BOUNDED'}
        envelope.extend([edges[0][0][0],edges[0][1][0]])
    return {'status':'CANDIDATE','value':coordinate(x,panel['x_axis']),'unit':panel['x_axis']['unit'],
            'bracketing_pixels':[a,b],'intersection_pixel':[x,target],
            'uncertainty_interval':[min(coordinate(edge,axis) for edge in (min(envelope)-pad,max(envelope)+pad) for axis in x_variants),
                                    max(coordinate(edge,axis) for edge in (min(envelope)-pad,max(envelope)+pad) for axis in x_variants)],
            'calibration_uncertainty_included':all('anchor_pixel_intervals' in panel[name] for name in ('x_axis','y_axis')),
            'uncertainty_basis':'pixel, dash-bracket and supplied anchor-bound envelope; not statistical confidence',
            'method':'linear_interpolation_in_printed_log_axis_space',
            'formula':'x_px=a_x+(y_target-a_y)/(b_y-a_y)*(b_x-a_x); diameter=axis_x(x_px)'}


def extract(pdf_path,specification,output):
    if output.exists():
        raise ValueError('immutable candidate output already exists')
    if hashlib.sha256(pdf_path.read_bytes()).hexdigest()!=specification['pdf_sha256']:
        raise ValueError('raster PDF hash mismatch')
    reports=[]
    with fitz.open(pdf_path) as document:
        for panel in specification['panels']:
            page=document[panel['page']-1]
            xref=panel['image_xref']
            placements=page.get_image_rects(xref)
            if len(placements)!=1:
                raise ValueError('native image placement is not unique')
            blob=document.extract_image(xref)['image']
            image=Image.open(io.BytesIO(blob)).convert('RGB')
            if list(image.size)!=panel['native_size']:
                raise ValueError('native raster dimensions changed')
            rgb=np.array(image)
            traces=[]
            for binding in panel['series']:
                candidate=trace(rgb,panel,binding)
                candidate['percentiles']=({f'd{p}_um':percentile(candidate,panel,p) for p in (10,50,90)}
                                           if panel['curve_type']=='cumulative_finer' else {})
                candidate['pixel_geometry_sha256']=digest(candidate['pixel_points'])
                traces.append(candidate)
            reports.append({'panel':panel,'native_image_sha256':hashlib.sha256(blob).hexdigest(),
                            'image_pdf_bbox':list(placements[0]),'series':traces})
    output.mkdir(parents=True)
    assets=[]
    for report in reports:
        panel=report['panel']
        with fitz.open(pdf_path) as document:
            image=Image.open(io.BytesIO(document.extract_image(panel['image_xref'])['image'])).convert('RGB')
        draw=ImageDraw.Draw(image)
        for series in report['series']:
            buffer=io.StringIO(newline='')
            writer=csv.writer(buffer,lineterminator='\n')
            writer.writerow(['x','y','native_pixel_x','native_pixel_y','gap_before'])
            gapset=set(series['gap_before_indices'])
            for i,(point,pixel) in enumerate(zip(series['points'],series['pixel_points'])):
                writer.writerow([*point,*pixel,int(i in gapset)])
                x,y=pixel
                draw.rectangle((x-1,y-1,x+1,y+1),fill=(255,0,255))
            data=buffer.getvalue().encode('utf-8')
            sha=hashlib.sha256(data).hexdigest()
            name=sha+'.csv'
            atomic_bytes(output/name,data)
            assets.append({'label':series['label'],'path':name,'sha256':sha})
        overlay=io.BytesIO();image.save(overlay,format='PNG')
        atomic_bytes(output/('overlay-'+digest(panel)[:12]+'.png'),overlay.getvalue())
    result={'schema_version':1,'algorithm':VERSION,'pdf_sha256':specification['pdf_sha256'],
            'specification_sha256':digest(specification),'panels':reports,'assets':assets,
            'status':'CANDIDATE_NOT_FORMAL','model_calls':0}
    atomic_bytes(output/'report.json',json.dumps(result,ensure_ascii=False,sort_keys=True,indent=2).encode('utf-8'))
    return {'status':result['status'],'series':[{'label':s['label'],'pixels':len(s['pixel_points']),
             'backwards':len(s['backwards_indices']),'percentiles':s['percentiles']} for r in reports for s in r['series']]}


def enrich_candidates(records,pdf_path,specification,run_dir):
    """Production candidate stage. A proof is mandatory, but never a promotion."""
    from verify_raster_curves import verify
    directory=run_dir/'raster-assets'
    if not directory.exists():
        extract(pdf_path,specification,directory)
    proof=verify(pdf_path,specification,directory)
    if proof['verdict']!='PASS':
        raise ValueError(f"raster source replay rejected: {proof['defects']}")
    result=copy.deepcopy(records)
    pdfs=[a for a in result['assets'] if a.get('kind')=='pdf' and a['sha256']==proof['pdf_sha256']]
    if len(pdfs)!=1:
        raise ValueError('raster source asset is not unique')
    source=pdfs[0]
    report=json.loads((directory/'report.json').read_text(encoding='utf-8'))
    assets={a['asset_key']:a for a in result['assets']}
    items=[]
    csvs={a['label']:a for a in report['assets']}
    if len(csvs)!=len(report['assets']):
        raise ValueError('raster labels need explicit panel disambiguation')
    for panel in report['panels']:
        binding=panel['panel']
        for series in panel['series']:
            asset=csvs[series['label']]
            key='asset-raster-'+asset['sha256'][:24]
            owners=[m for m in result['mats'] if m.get('custom_material_id')==series['label']]
            record={'asset_key':key,'paper_key':source['paper_key'],'kind':'csv','mime_type':'text/csv',
                    'relative_path':'raster-assets/'+asset['path'],'sha256':asset['sha256'],
                    'extensions':{'review_status':'candidate','source_pdf_asset_key':source['asset_key'],
                                  'source_report_sha256':proof['report_sha256'],'figure_number':binding['figure'],
                                  'page':binding['page'],'panel_label':binding['panel_label']}}
            if key in assets and assets[key]!=record:
                raise ValueError('raster candidate asset conflict')
            if key not in assets:
                result['assets'].append(record)
                assets[key]=record
            items.append({'label':series['label'],'record_type':'mat','record_key':owners[0]['mat_key'] if len(owners)==1 else None,
                          'owner_status':'EXACT_MATCH' if len(owners)==1 else 'UNRESOLVED',
                          'data_asset_key':key,'source_pdf_asset_key':source['asset_key'],
                          'figure_number':binding['figure'],'page':binding['page'],'panel_label':binding['panel_label'],
                          'pixel_geometry_sha256':series['pixel_geometry_sha256'],
                          'percentiles':series['percentiles'],'status':'CANDIDATE_NOT_FORMAL'})
    receipt={'version':VERSION,'report_path':'raster-assets/report.json',
             'report_sha256':proof['report_sha256'],'specification_sha256':proof['specification_sha256'],
             'source_proof_path':'raster-source-proof.json','items':items,'formal_values_added':0}
    old=result.setdefault('extensions',{}).get('raster_curve_candidates')
    if old is not None and old!=receipt:
        raise ValueError('raster candidate receipt drift')
    atomic_bytes(run_dir/'raster-source-proof.json',json.dumps(proof,sort_keys=True,indent=2).encode('utf-8'))
    result['extensions']['raster_curve_candidates']=receipt
    records.clear();records.update(result)
    return {'series':len(items),'unresolved_owners':sum(i['owner_status']!='EXACT_MATCH' for i in items),'formal_values_added':0}


def calibrate_ticks(axis):
    """Check a declared linear/log axis against all supplied source ticks."""
    ticks=axis.get('ticks')
    if ticks is None:return copy.deepcopy(axis)
    if not isinstance(ticks,list) or len(ticks)<3:raise ValueError('at least three source ticks required')
    if any(not isinstance(t,list) or len(t)!=2 or any(type(v) not in (int,float) or not math.isfinite(v) for v in t) for t in ticks):
        raise ValueError('invalid source tick coordinates')
    ordered=sorted(ticks)
    pixels=np.array([t[0] for t in ordered]);values=np.array([t[1] for t in ordered])
    if np.any(np.diff(pixels)<=0):raise ValueError('duplicate source tick pixel')
    if axis['scale']=='log10':
        if np.any(values<=0):raise ValueError('nonpositive logarithmic tick')
        values=np.log10(values)
    elif axis['scale']!='linear':raise ValueError('unsupported source axis scale')
    if not (np.all(np.diff(values)>0) or np.all(np.diff(values)<0)):
        raise ValueError('source tick values not monotonic')
    tolerance=axis.get('tick_tolerance_px')
    if type(tolerance) not in (int,float) or not math.isfinite(tolerance) or tolerance<=0:
        raise ValueError('explicit positive tick tolerance required')
    # Use actual endpoint ticks, not a fitted replacement of the observations.
    predicted=pixels[0]+(values-values[0])*(pixels[-1]-pixels[0])/(values[-1]-values[0])
    residuals=np.abs(predicted-pixels)
    if np.max(residuals)>tolerance:raise ValueError('source ticks contradict axis calibration')
    result=copy.deepcopy(axis)
    result['anchors']=[ordered[0],ordered[-1]]
    result['tick_check']={'residuals_px':residuals.tolist(),'max_residual_px':float(np.max(residuals)),
                          'status':'CONSISTENT_SOURCE_TICK_CANDIDATES_NOT_SEMANTIC_ACCEPTANCE'}
    return result


def intensity_csv(panel,series):
    buffer=io.StringIO(newline='');writer=csv.writer(buffer,lineterminator='\n')
    writer.writerow(['pixel_x','pixel_y_midpoint','pixel_y_min','pixel_y_max','x','x_unit','intensity','intensity_unit','status','ambiguous_pixel_segments'])
    samples={pixel[0]:(pixel,point,span) for pixel,point,span in zip(series['pixel_points'],series['points'],series['pixel_stroke_spans'])}
    ambiguous=set(series['ambiguous_columns'])
    alternatives={item['x']:item['segments'] for item in series['ambiguous_pixel_spans']}
    for x in range(panel['plot_bbox_px'][0],panel['plot_bbox_px'][2]):
        x_unit=series['axis_calibration']['x_axis']['unit'];y_unit=series['axis_calibration']['y_axis']['unit']
        if x in samples:
            pixel,point,span=samples[x]
            writer.writerow([x,pixel[1],span['y_min'],span['y_max'],point[0],x_unit,point[1],y_unit,'STROKE_CANDIDATE',''])
        else:
            writer.writerow([x,None,None,None,coordinate(x,panel['x_axis']) if x_unit else None,x_unit,None,y_unit,
                             'AMBIGUOUS_STROKES' if x in ambiguous else 'NO_SELECTED_STROKE',
                             json.dumps(alternatives[x],sort_keys=True,separators=(',',':')) if x in alternatives else ''])
    return buffer.getvalue().encode('utf-8')


def extract_intensity_image(image_path,specification,output):
    """Standalone source-image module, including supplement images; no promotion."""
    raw=Path(image_path).read_bytes()
    if hashlib.sha256(raw).hexdigest()!=specification['image_sha256']:
        raise ValueError('intensity source image hash mismatch')
    picture=Image.open(io.BytesIO(raw)).convert('RGB')
    if list(picture.size)!=specification['native_size']:raise ValueError('intensity native size mismatch')
    if output.exists():raise ValueError('immutable intensity output already exists')
    rgb=np.array(picture);reports=[];labels=[]
    for source_panel in specification['panels']:
        panel=copy.deepcopy(source_panel)
        for axis in ('x_axis','y_axis'):panel[axis]=calibrate_ticks(panel[axis])
        if panel.get('curve_type')!='intensity':raise ValueError('image entry requires intensity curves')
        for series in panel['series']:
            if series['label'] in labels:raise ValueError('duplicate intensity series identity')
            labels.append(series['label'])
            result=trace(rgb,panel,series)
            reports.append({'panel':panel,'series':result})
    if not reports:raise ValueError('empty intensity specification')
    overlay=picture.copy();draw=ImageDraw.Draw(overlay)
    for report in reports:
        for span in report['series']['pixel_stroke_spans']:
            draw.line([(span['x'],span['y_min']),(span['x'],span['y_max'])],fill=(0,180,255),width=1)
        for column in report['series']['ambiguous_pixel_spans']:
            for segment in column['segments']:
                draw.line([(column['x'],segment['y_min']),(column['x'],segment['y_max'])],fill=(255,140,0),width=1)
    buffer=io.BytesIO();overlay.save(buffer,format='PNG');overlay_bytes=buffer.getvalue()
    csv_files={};csv_assets=[]
    for item in reports:
        data=intensity_csv(item['panel'],item['series']);sha=hashlib.sha256(data).hexdigest()
        name=digest(item['series']['label'])[:24]+'.csv'
        if name in csv_files:raise ValueError('intensity CSV filename collision')
        csv_files[name]=data
        csv_assets.append({'series':item['series']['label'],'path':name,'sha256':sha})
    report={'schema_version':1,'scope':'INTENSITY_IMAGE_CANDIDATES_NOT_FORMAL',
            'source_image_sha256':specification['image_sha256'],'specification_sha256':digest(specification),
            'implementation_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'panels':reports,'csv_assets':csv_assets,'overlay_sha256':hashlib.sha256(overlay_bytes).hexdigest(),
            'formal_values_added':0,'material_association_status':'UNRESOLVED'}
    # All parsing succeeds before any files are written; report is published last.
    atomic_bytes(output/'overlay.png',overlay_bytes)
    for name,data in csv_files.items():atomic_bytes(output/name,data)
    atomic_bytes(output/'report.json',json.dumps(report,sort_keys=True,ensure_ascii=False,indent=2).encode('utf-8'))
    return {'scope':report['scope'],'series':len(reports),'formal_values_added':0}


def attach_intensity_candidates(records,source_asset_key,specification,run_dir):
    """Attach source-bound image geometry without inventing MAT relationships."""
    sources=[a for a in records['assets'] if a['asset_key']==source_asset_key]
    if len(sources)!=1 or sources[0]['sha256']!=specification['image_sha256']:
        raise ValueError('intensity parent asset missing or changed')
    source=sources[0];run_dir=Path(run_dir)
    associations=material_label_candidates(records,source,specification)
    input_key=digest({'specification':specification,'material_candidates':associations,
                      'implementation':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})
    prior=[r for r in records.get('extensions',{}).get('intensity_image_candidates',[])
           if r['source_image_asset_key']==source_asset_key]
    if prior:
        if len(prior)!=1 or prior[0].get('input_key')!=input_key:
            raise ValueError('intensity candidate input changed; new candidate run required')
        if hashlib.sha256((run_dir/source['relative_path']).read_bytes()).hexdigest()!=source['sha256']:
            raise ValueError('intensity source image hash mismatch')
        for key in [prior[0]['report_asset_key'],prior[0]['overlay_asset_key'],*prior[0]['csv_asset_keys']]:
            assets=[a for a in records['assets'] if a['asset_key']==key]
            if len(assets)!=1:raise ValueError('intensity cached asset missing or ambiguous')
            asset=assets[0];path=(run_dir/asset['relative_path']).resolve()
            if not path.is_relative_to(run_dir.resolve()):raise ValueError('intensity cached output outside run')
            if hashlib.sha256(path.read_bytes()).hexdigest()!=asset['sha256']:
                raise ValueError('intensity cached asset hash mismatch')
        return prior[0]
    output=run_dir/'intensity-assets'/digest(specification)
    extract_intensity_image(run_dir/source['relative_path'],specification,output)
    report=json.loads((output/'report.json').read_bytes())
    added=[]
    for name,mime,kind in [('report.json','application/json','json'),('overlay.png','image/png','image')]+[(a['path'],'text/csv','csv') for a in report['csv_assets']]:
        path=output/name;sha=hashlib.sha256(path.read_bytes()).hexdigest()
        key='asset-intensity-'+sha[:24]
        if any(a['asset_key']==key for a in records['assets']):raise ValueError('intensity artifact identity collision')
        added.append({'asset_key':key,'paper_key':source['paper_key'],'kind':kind,'mime_type':mime,
                      'relative_path':path.relative_to(run_dir).as_posix(),'sha256':sha,
                      'extensions':{'source_image_asset_key':source_asset_key,'review_status':'candidate'}})
    receipt={'source_image_asset_key':source_asset_key,'source_image_sha256':source['sha256'],
             'input_key':input_key,
             'report_asset_key':added[0]['asset_key'],'overlay_asset_key':added[1]['asset_key'],
             'csv_asset_keys':[a['asset_key'] for a in added[2:]],
             'series_count':len(report['panels']),'formal_values_added':0,
             'material_association_status':'CANDIDATES_REQUIRE_SOURCE_CONFIRMATION',
             'material_candidates':associations}
    records['assets'].extend(added)
    records.setdefault('extensions',{}).setdefault('intensity_image_candidates',[]).append(receipt)
    return receipt


def material_label_candidates(records,source,specification):
    """Exact source labels only; no stripping additives or fuzzy material merging."""
    normalize=lambda text:' '.join(str(text).casefold().split())
    width,height=specification['native_size'];candidates=[]
    for panel in specification['panels']:
        for series in panel['series']:
            label=series.get('source_label')
            if not label:
                candidates.append({'series':series['label'],'material_key':None,'status':'SOURCE_LABEL_MISSING'})
                continue
            text=label.get('text');box=label.get('bbox_px')
            if (not isinstance(text,str) or not text.strip() or not isinstance(box,list) or len(box)!=4
                    or any(type(v) not in (int,float) or not math.isfinite(v) for v in box)
                    or not (0<=box[0]<box[2]<=width and 0<=box[1]<box[3]<=height)):
                raise ValueError('invalid intensity source label candidate')
            matches=[m for m in records.get('mats',[]) if m.get('paper_key')==source['paper_key']
                     and normalize(m.get('custom_material_id',''))==normalize(text)]
            candidate={'series':series['label'],'source_text':text,'source_bbox_px':box,
                               'material_key':matches[0]['mat_key'] if len(matches)==1 else None,
                               'status':'EXACT_LABEL_CANDIDATE' if len(matches)==1 else 'NO_UNIQUE_EXACT_MATERIAL',
                               'formal_association':False}
            if not matches:
                prepared=prepared_sample_candidate(records,source,text)
                if prepared is not None:
                    candidate.update(status='PREPARED_SAMPLE_RELATION_CANDIDATE',prepared_sample=prepared)
            candidates.append(candidate)
    return candidates


def prepared_sample_candidate(records,source,text):
    """Keep the spiked specimen distinct; use material-scoped QXRD context only."""
    parts=text.split('+')
    if len(parts)!=2:return None
    normalize=lambda value:' '.join(str(value).casefold().split())
    matches=[]
    for material in records.get('mats',[]):
        if material.get('paper_key')!=source['paper_key'] or normalize(material.get('custom_material_id'))!=normalize(parts[0]):continue
        qxrd=material.get('xrd_qxrd') or {};ext=qxrd.get('extensions') or {}
        basis=ext.get('phase_fraction_basis') or {}
        if (parts[1].strip()!=qxrd.get('standard_formula') or qxrd.get('standard_method')!='internal standard'
                or basis.get('review_status')!='SOURCE_CONTEXT_VERIFIED'
                or normalize(basis.get('reported_for_material'))!=normalize(material['custom_material_id'])
                or not basis.get('source_quotes') or not basis.get('source_pdf_sha256')):continue
        matches.append({'parent_material_key':material['mat_key'],'measurement_label':text,
                        'added_standard_formula':qxrd['standard_formula'],
                        'reported_standard_to_sample_ratio':ext.get('reported_standard_to_sample_ratio'),
                        'source_pdf_sha256':basis['source_pdf_sha256'],'source_page':basis.get('source_page'),
                        'source_quotes':copy.deepcopy(basis['source_quotes']),
                        'phase_fraction_basis':basis.get('standard_included_in_denominator'),
                        'rebasing_applied':False,'material_identity_unchanged':True})
    return matches[0] if len(matches)==1 else None


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    source=parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--pdf',type=Path)
    source.add_argument('--image',type=Path)
    parser.add_argument('--specification',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    fn=extract_intensity_image if args.image else extract
    print(json.dumps(fn(args.image or args.pdf,json.loads(args.specification.read_text(encoding='utf-8')),args.output),ensure_ascii=True))
