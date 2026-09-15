"""Calibrated image readout with local gaps retained for human review.

Uses existing colour tracing and axis conversion. A semantic binding supplies
labels, axes, plot bounds and swatches; this module measures source pixels.
"""
import argparse
import copy
import csv
import io
from pathlib import Path
import numpy as np
from PIL import Image
from raster_curves import trace_intensity,calibrate_ticks
from raster_bars import extract_bar
from source_quantity_producer import atomic_json,atomic_text
from simple_chart_geometry import route,measure
from selective_chart_policy import backend,normalize
from pdf_thread_access import serialized_pdf

CURVE_KINDS={'curve','psd','ftir','xrd','qxrd','nmr','spectrum'}

def final_reading(item):
    """A whole-panel semantic reading supersedes geometric candidates.

    Also applied when replaying older readout files, which kept the final
    reading beside (rather than instead of) their candidate values.
    """
    if item.get('agent_result') is None:
        return item
    result = copy.deepcopy(item)
    result['raster_candidates'] = result.pop('values', [])
    result['result'] = result.pop('agent_result')
    result.update(route='agent', backend='agent')
    return result

def direct_payload(panel):
    """The same payload is consumed for cached and freshly read panels."""
    payload=copy.deepcopy(panel.get('direct_result') or {})
    series=list(payload.get('series',[]))
    for entry in panel.get('series',[]):
        if entry.get('points') and not any(s.get('owner_key')==entry.get('owner_key') and
                s.get('label',s.get('series'))==entry.get('label',entry.get('series')) for s in series):
            series.append(copy.deepcopy(entry))
    payload['series']=series
    for entry in payload.get('values',[])+series:
        for axis in ('x_axis','y_axis'):
            reference=entry.get(axis+'_ref')
            if reference and reference in panel:
                entry[axis]=copy.deepcopy(panel[reference])
        for key in ('kind','owner_key','property','age_seconds','age_bbox_px','specimen','specimen_type','method','category','curve_type','isotope',
                    'x_axis','y_axis','plot_bbox_px','review_regions'):
            if key not in entry and key in panel:entry[key]=copy.deepcopy(panel[key])
        # Older PSD responses expressed a percentile as (diameter, percent).
        # Its physical value is the diameter, not the horizontal level (50%).
        if panel.get('kind')=='psd' and entry.get('value') is None:
            import re
            match=re.search(r'\bD(10|50|90)\b',str(entry.get('category','')),re.I)
            if match and entry.get('x') is not None and entry.get('y')==int(match[1]):
                entry['value']=entry['x'];entry['unit']=(panel.get('x_axis') or {}).get('unit')
    return payload

@serialized_pdf
def vector_items(pdf_path,page_no,image_path,panel):
    import fitz
    from vector_curves import extract_panel
    with fitz.open(pdf_path) as doc:
        page=doc[page_no-1]
        with Image.open(image_path) as im:sx,sy=page.rect.width/im.width,page.rect.height/im.height
        if panel.get('render_dpi'):sx=sy=72/panel['render_dpi']
        p=copy.deepcopy(panel)
        p['plot_bbox']=[v*(sx if i%2==0 else sy) for i,v in enumerate(p['plot_bbox_px'])]
        p['exclude_boxes']=[[v*(sx if i%2==0 else sy) for i,v in enumerate(b)] for b in p.get('exclude_boxes_px',[])]
        for name,scale in [('x_axis',sx),('y_axis',sy)]:
            p[name]['anchors']=[[x*scale,y] for x,y in p[name]['anchors']]
        drawings=page.get_drawings()
        for s in p['series']:
            s['color']=list(drawings[s['source_path_indices'][0]]['color'])
            s.setdefault('record_type','MAT');s.setdefault('record_label',s['label'])
        result=extract_panel(page,p)
    items=result['series']
    for item in items:
        item.update(kind=panel['kind'],backend='vector',approximate=True,gap_before_indices=[])
        item['pixel_points']=[[x/sx,y/sy] for x,y in item['pdf_points']]
    return items

def make_agent_reader(output):
    """Sol handles panels or local regions not suited to geometric extraction."""
    output=Path(output);counter=0
    def read(image_path,panel):
        nonlocal counter
        directory=output/f'agent-{counter}';counter+=1
        directory.mkdir(parents=True,exist_ok=True)
        from chart_image_input import pack,translate
        box=panel.get('figure_bbox_px')
        if not box and panel.get('plot_bbox_px'):
            a,b,c,d=panel['plot_bbox_px'];box=[a-50,b-35,c+35,d+50]
        cropped,mappings=pack([dict(source_id='failed-panel',image=image_path,bbox_px=box)],directory)
        dx,dy=mappings['failed-panel']['offset_to_page']
        local_panel=translate(panel,-dx,-dy)
        from chart_scan_service import read_image
        panels=read_image(cropped,dict(task={'kind':panel['kind']},panel=local_panel),directory/'reading')
        payload={'values':[],'series':[]}
        for result in panels:
            data=direct_payload(result)
            payload['values'].extend(data.get('values',[]));payload['series'].extend(data.get('series',[]))
        return translate(payload,dx,dy)
    return read


def intervals(values):
    result=[]
    for value in sorted(set(values)):
        if result and value==result[-1][1]+1:result[-1][1]=value
        else:result.append([value,value])
    return result


def curve(rgb,panel,series):
    p=copy.deepcopy(panel)
    p.update(curve_type='intensity',allow_partial_curve=True)
    for axis in ('x_axis','y_axis'):p[axis]=calibrate_ticks(p[axis])
    raw=trace_intensity(rgb,p,series)
    observed={int(point[0]) for point in raw['pixel_points']}
    ambiguous=set(raw['ambiguous_columns'])
    missing=set(range(p['plot_bbox_px'][0],p['plot_bbox_px'][2]))-observed-ambiguous
    issues=[{'reason':reason,'x_pixel_interval':span} for reason,xs in
            [('multiple_strokes',ambiguous),('no_readable_stroke',missing)] for span in intervals(xs)]
    return {'label':series['label'],'kind':panel.get('kind','curve'),
            'mat_key':series.get('mat_key'),
            'record_type':series.get('record_type'),'record_label':series.get('record_label'),
            'approximate':True,'points':raw['points'],'pixel_points':raw['pixel_points'],
            'gap_before_indices':raw['gap_before_indices'],'review_regions':issues,
            'axis_calibration':raw['axis_calibration'],'x_axis':p['x_axis'],'y_axis':p['y_axis'],
            'status':'PARTIAL' if issues else 'READABLE'}


def pie(rgb,panel):
    """Read a flat circular pie by angular colour support; unknown arcs stay unknown."""
    cx,cy=panel['center_px'];radius=panel['radius_px']
    angles=np.linspace(0,2*np.pi,3600,endpoint=False)
    counts=np.zeros((len(panel['series']),len(angles)),dtype=int)
    for fraction in (.45,.6,.75):
        xs=np.rint(cx+radius*fraction*np.cos(angles)).astype(int)
        ys=np.rint(cy+radius*fraction*np.sin(angles)).astype(int)
        if xs.min()<0 or ys.min()<0 or xs.max()>=rgb.shape[1] or ys.max()>=rgb.shape[0]:
            raise ValueError('pie sampling exceeds source image')
        pixels=rgb[ys,xs].astype(float)
        for i,s in enumerate(panel['series']):
            counts[i]+=np.linalg.norm(pixels-np.array(s['rgb']),axis=1)<=s['color_distance']
    supported=counts>=2
    unique=supported.sum(axis=0)==1
    return {'kind':'pie','approximate':True,'values':[
        {'label':s['label'],'value':float(np.count_nonzero(supported[i]&unique)/len(angles)*100),
         'unit':'%','meaning':'observed_angular_share'} for i,s in enumerate(panel['series'])],
        'unreadable_percent':float(np.count_nonzero(~unique)/len(angles)*100),
        'review_regions':[{'reason':'unclassified_or_overlapping_arc','angle_degrees':[a/10,b/10]}
                          for a,b in intervals(np.flatnonzero(~unique))]}


def extract(image_path,specification,output,agent_reader=None):
    from psd_representation import prefer_cumulative
    rgb=np.asarray(Image.open(image_path).convert('RGB'))
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    if agent_reader is None:agent_reader=make_agent_reader(output)
    results=[];printed=[]
    selected_panels=prefer_cumulative(specification['panels'])
    for pi,raw_panel in enumerate(selected_panels):
        if raw_panel.get('skip_auxiliary_psd'):continue
        if (raw_panel.get('direct_result') == {'values': []}
                and raw_panel.get('review_regions') and not raw_panel.get('series')):
            results.append(dict(raw_panel,backend='agent',route='agent',result={'values':[],'series':[]}))
            continue
        panel=normalize(raw_panel);selected=backend(panel)
        if panel.get('direct_result') is not None and selected!='agent':
            printed.append(dict(panel=pi,figure=panel.get('figure'),result=panel['direct_result']))
        def direct(reason=None):
            # Historical planners were allowed to put model-estimated curve
            # points directly in the binding.  A curve binding is a plan, not
            # a completed measurement: always send it through the scan-first
            # reader.  Only chart_scan_service may mark an explicit
            # post-scan fallback as tool_failure_confirmed.
            scan_routed=panel['kind'].lower() in CURVE_KINDS and not panel.get('tool_failure_confirmed')
            if scan_routed:
                result=agent_reader(image_path,panel)
            else:
                result=direct_payload(panel)
            has_curve=any(s.get('points') for s in result.get('series',[]))
            if not scan_routed and ((not result.get('values') and not has_curve) or
                    reason and panel['kind'] in {'curve','psd','ftir','xrd','nmr'} and not has_curve):
                result=agent_reader(image_path,panel)
            return [{'kind':panel['kind'],'route':'agent','backend':'agent','result':result,'fallback_reason':reason}]
        try:
            if selected=='scan_first':
                planned=panel.get('series',[])
                if planned and all(s.get('backend')=='scan_first' for s in planned):
                    items=[dict(s,kind=panel['kind'],x_axis=panel.get('x_axis'),y_axis=panel.get('y_axis'),
                        curve_type=panel.get('curve_type'),isotope=panel.get('isotope'),
                        plot_bbox_px=panel.get('plot_bbox_px')) for s in planned]
                else:
                    items=direct('scan-first route')[0]['result'].get('series',[])
                values=(panel.get('direct_result') or {}).get('values',[])
                if values:items.append(dict(kind=panel['kind'],backend='scan_first',values=values))
            elif selected=='histogram_raster':
                from histogram_agent_readout import run
                items=run(image_path,panel,output/f'histogram-{pi}')
            elif selected=='agent':items=direct()
            elif selected=='vector':
                items=vector_items(specification['pdf'],panel.get('page',specification.get('page')),image_path,panel)
            elif selected=='separated_ftir':
                from separated_ftir import trace
                items=[];failed_series=[];reasons=[]
                for s in panel['series']:
                    try:items.append(trace(rgb,panel,s))
                    except (ValueError,KeyError,IndexError,TypeError) as error:
                        failed_series.append(s);reasons.append(str(error))
                if failed_series:
                    failed=dict(panel,series=failed_series,failed_reason=reasons)
                    failed.pop('direct_result',None)
                    items.append(dict(kind=panel['kind'],route='agent',backend='agent',
                        result=agent_reader(image_path,failed),fallback_reason='; '.join(reasons)))
                values=direct_payload(panel).get('values',[])
                if values:
                    items.append(dict(kind=panel['kind'],route='agent',backend='agent',result={'values':values}))
            elif panel['kind']=='scatter':
                measured=measure(rgb,panel)
                if measured.get('agent_regions'):
                    if panel.get('read_mode')=='script':
                        regions=measured['agent_regions']
                        repair=agent_reader(image_path,dict(panel,candidate_regions=regions,
                            readout_scope='Return only measurements inside candidate_regions. Other markers were already read by the script.'))
                        def inside(v):
                            b=v.get('bbox_px');pixel=v.get('pixel')
                            if b:pixel=[(b[0]+b[2])/2,(b[1]+b[3])/2]
                            return pixel and any(a<=pixel[0]<=c and b<=pixel[1]<=d for a,b,c,d in regions)
                        scanned=[]
                        for v in measured['values']:
                            identity=next((s for s in panel.get('series',[]) if s['label']==v.get('series')),panel)
                            row={k:identity.get(k,panel.get(k)) for k in ('owner_key','property','age_seconds','specimen','method')}
                            row.update(v,value=v['y'],unit=panel['y_axis'].get('unit'),approximate=True)
                            x,y=v['pixel'];row['bbox_px']=[x-3,y-3,x+3,y+3];scanned.append(row)
                        measured['agent_result']={'values':scanned+[v for v in repair.get('values',[]) if inside(v)]}
                    else:
                        measured['agent_result']=agent_reader(image_path,dict(panel,
                            candidate_regions=measured['agent_regions'],
                            readout_scope='Return final measurements for the entire panel, including readable markers; candidates are diagnostic only.'))
                items=[final_reading(measured)]
            elif panel['kind']=='bar':
                items=[dict(extract_bar(rgb,panel,b),kind='bar',approximate=True) for b in panel['bars']]
            else:
                items=[curve(rgb,panel,s) for s in panel['series']]
                if not items[0]['points']:raise ValueError('selected curve has no measured points')
        except (ValueError,KeyError,IndexError,TypeError) as e:
            items=direct(str(e))
        for si,item in enumerate(items):
            if item.get('source_label'):item.setdefault('label',item['source_label'])
            identity=next((s for s in panel.get('series',[])+panel.get('bars',[]) if s.get('label')==item.get('label')),panel)
            for key in ('owner_key','property','age_seconds','age_bbox_px','specimen','specimen_type','method','category','curve_type','isotope'):
                if key in identity or key in panel:item[key]=identity.get(key,panel.get(key))
            item.setdefault('x_axis',panel.get('x_axis'));item.setdefault('y_axis',panel.get('y_axis'))
            item.setdefault('plot_bbox_px',panel.get('plot_bbox_px'))
            for value in item.get('values',[]):
                binding=next((s for s in panel.get('series',[]) if s.get('label')==value.get('series')),panel)
                for key in ('owner_key','property','age_seconds','age_bbox_px','specimen','specimen_type','method','category'):
                    if key in binding:value[key]=binding[key]
                if value.get('pixel'):
                    x,y=value['pixel'];value['bbox_px']=[x-3,y-3,x+3,y+3]
            if item.get('source_bbox_px'):item['bbox_px']=item['source_bbox_px']
            item['source']={'image':str(Path(image_path).resolve()),'panel':pi,
                            'page':panel.get('page',specification.get('page')),'figure':panel.get('figure'),
                            'pdf':specification.get('pdf')}
            item.setdefault('backend',selected)
            if 'points' in item:
                stream=io.StringIO();writer=csv.writer(stream,lineterminator='\n')
                writer.writerow(['x','y','pixel_x','pixel_y','gap_before'])
                for i,(point,pixel) in enumerate(zip(item['points'],item['pixel_points'])):
                    writer.writerow([*point,*pixel,int(i in item['gap_before_indices'])])
                name=f'panel-{pi}-series-{si}.csv';atomic_text(output/name,stream.getvalue());item['point_file']=name
            results.append(item)
    calls=sum(1 for p in output.glob('agent-*/response.json'))
    result={'source_image':str(Path(image_path).resolve()),'results':results,'printed_readings':printed,'model_calls':calls,
            'psd_auxiliary_series':[s for p in selected_panels for s in p.get('psd_auxiliary_series',[])]}
    atomic_json(output/'result.json',result)
    return result

if __name__=='__main__':
    import json
    p=argparse.ArgumentParser();p.add_argument('--image',required=True);p.add_argument('--binding');p.add_argument('--output',required=True)
    p.add_argument('--pdf');p.add_argument('--page',type=int);p.add_argument('--caption',default='')
    a=p.parse_args()
    if a.binding:binding=json.loads(Path(a.binding).read_bytes())
    else:
        from selective_chart_planner import plan
        binding=plan(a.image,a.pdf,a.page,a.caption,Path(a.output)/'planner')
    r=extract(a.image,binding,a.output)
    print({'series':len(r['results']),'points':sum(len(x.get('points',[])) for x in r['results'])})
