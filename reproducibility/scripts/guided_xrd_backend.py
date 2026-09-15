"""Offline experimental adapter for an unchanged, separately installed WPD core.

No vendored AGPL algorithm, network, source-model invocation or acceptance.
Prepared candidates are add-only alternatives, never replacements for history.
"""
import argparse,copy,csv,hashlib,io,json,subprocess
from pathlib import Path
import numpy as np
from PIL import Image,ImageDraw
from source_specimen_variants import digest
from source_quantity_producer import atomic_json
from vector_curves import atomic_bytes
from planned_xrd_curves import calibrated_axis
from raster_curves import coordinate

VERSION='guided-wpd-xrd-experimental-v1'
def load(p):return json.loads(Path(p).read_bytes())
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()

def bound_review(region,image_path,source_image,plan,reviews):
    review=reviews.get(region['region_id'])
    if review is None:return None
    proposal=next((r for r in plan['regions'] if r['region_id']==region['region_id']),None)
    original=Path(plan['source_image'])
    if sha(source_image)!=plan['source_sha256'] or proposal is None:
        raise ValueError('region review source binding changed')
    with Image.open(original) as a,Image.open(image_path) as b:
        if a.size!=b.size or a.convert('RGB').tobytes()!=b.convert('RGB').tobytes():
            raise ValueError('region review source pixels changed')
    if any(region[k]!=proposal[k] for k in ('series_id','source_label','bbox_panel_px')):
        raise ValueError('region review proposal binding changed')
    return review

def propose(cases,series):
    if len(series)<2:raise ValueError('stacked region needs multiple source series')
    centres=[(s['swatch_bbox_px'][1]+s['swatch_bbox_px'][3])/2-cases[0]['origin'][1] for s in series]
    spacing=float(np.median(np.diff(centres)))
    if spacing<=0:raise ValueError('source legends not in stacked order')
    lower=[];regions=[]
    for c,centre in zip(cases,centres):
        counts=np.bincount(np.asarray(load(c['output_prefix']+'-wpd.json')['mask'],dtype=int)//c['width'],minlength=c['height'])
        lo=max(0,int(centre-spacing/2));hi=min(c['height'],int(centre+spacing/2))
        if hi<=lo or counts[lo:hi].max()<c['width']*.1:raise ValueError('stable baseline not established')
        lower.append(int((np.flatnonzero(counts[lo:hi]>=max(2,c['width']*.01))+lo).max()))
    if any(a>=b for a,b in zip(lower,lower[1:])):raise ValueError('unordered baselines')
    for i,(c,s) in enumerate(zip(cases,series)):
        pad=max(2,int(np.ceil((s['swatch_bbox_px'][3]-s['swatch_bbox_px'][1])/2))+1)
        roi=[0,0 if i==0 else lower[i-1]+pad,c['width'],min(c['height'],lower[i]+pad)]
        regions.append({'region_id':'region-'+hashlib.sha256(json.dumps([c['case_id'],roi]).encode()).hexdigest()[:12],
            'bbox_panel_px':roi,'series_id':s['series_id'],'source_label':s['source_label']})
    return regions

def prepare(root,directory,backend_root):
    root=Path(root);directory=Path(directory);backend_root=Path(backend_root)
    internal=backend_root/'guided-integration';internal.mkdir(exist_ok=True)
    source=root/'shared-curves/packet.json';packet=load(source)
    if digest({k:v for k,v in packet.items() if k!='packet_sha256'})!=packet['packet_sha256']:raise ValueError('source packet changed')
    nodes=[p for p in packet['panels'] if p['module']=='planned-xrd']
    if len(nodes)!=2:raise ValueError('expected existing two-panel source scope')
    algo_files=[backend_root/'wpd/javascript/core'/p for p in ['mathFunctions.js','autoDetection.js','curve_detection/averagingWindowCore.js']]
    runner=Path(__file__).with_name('evaluate_curve_backends.mjs')
    algo={'name':'WebPlotDigitizer','version':'5.3.0','git_head':subprocess.check_output(['git','-C',str(backend_root/'wpd'),'rev-parse','HEAD'],text=True).strip(),
          'license':'AGPL-3.0','distribution':'OFFLINE_EXPERIMENTAL_EXTERNAL_BACKEND_NOT_PUBLISHED',
          'files':{str(p):sha(p) for p in algo_files},'runner':{str(runner):sha(runner)},'parameters':{'color_distance':30,'dx':2,'dy':2}}
    if algo['git_head']!='3a3ecb11606945d0701c8a488777e6861be70056':raise ValueError('unreviewed backend version')
    prior_review=load(backend_root/'guided-region/source-region-check.json')
    prior_plan_path=backend_root/'guided-region/plan.json';prior_plan=load(prior_plan_path)
    review={r['region_id']:r for r in prior_review['regions']};cases=[]
    for node in nodes:
        if sha(node['image']['path'])!=node['image']['sha256']:raise ValueError('source image changed')
        frame=list(map(int,node['plot_bbox_px']));image=Image.open(node['image']['path']).convert('RGBA').crop(frame)
        png=internal/(node['panel_id']+'.png');image.save(png)
        raw=internal/(node['panel_id']+'.rgba');atomic_bytes(raw,image.tobytes())
        allow=Image.new('L',image.size,255);draw=ImageDraw.Draw(allow)
        for s in node['series']:
            b=s['legend_bbox_px'];draw.rectangle([b[0]-frame[0],b[1]-frame[1],b[2]-frame[0],b[3]-frame[1]],fill=0)
        path=internal/(node['panel_id']+'-base.bin');atomic_bytes(path,allow.tobytes())
        anchors={c['source_series_id']:c['rgb'] for c in node['colors'] if c.get('source_series_id')}
        for i,s in enumerate(node['series']):
            if not s.get('record_key') or s['series_id'] not in anchors:raise ValueError('source series selection unestablished')
            cid='planned-xrd-'+node['panel_id']+'-'+str(i)
            cases.append({'case_id':cid,'rgb':anchors[s['series_id']],'width':image.width,'height':image.height,
                'origin':frame[:2],'rgba_path':str(raw),'image_path':str(png),'allowed_path':str(path),
                'output_prefix':str(internal/(cid+'-unguided')),'panel_id':node['panel_id'],'series':s})
    manifest=internal/'manifest.json';atomic_json(manifest,{'cases':cases})
    cmd=['node',str(runner),str(manifest),str(backend_root),'wpd']
    subprocess.run(cmd,check=True,capture_output=True,text=True)
    for node in nodes:
        subset=[c for c in cases if c['panel_id']==node['panel_id']]
        for c,region in zip(subset,propose(subset,node['series'])):
            r=bound_review(region,c['image_path'],node['image']['path'],prior_plan,review);region.update(source_identity_status='EXISTING_SOURCE_SERIES_ID_REUSED',
                geometry_review_status=r['decision'] if r else 'UNESTABLISHED_NO_PRIOR_REGION_SELECTION',
                prior_region_review=r)
            c['region']=region;c['unguided_path']=c['output_prefix']+'-wpd.json'
            # Unreviewed regions produce isolated diagnostics, never reliable values.
            allow=np.frombuffer(Path(c['allowed_path']).read_bytes(),dtype=np.uint8).copy().reshape(c['height'],c['width'])
            _,top,_,bottom=region['bbox_panel_px'];allow[:top,:]=0;allow[bottom:,:]=0
            p=internal/(c['case_id']+'-guided.bin');atomic_bytes(p,allow.tobytes());c['allowed_path']=str(p)
            c['output_prefix']=str(internal/(c['case_id']+'-guided'))
    atomic_json(manifest,{'cases':cases});subprocess.run(cmd,check=True,capture_output=True,text=True)
    targets=[]
    for c in cases:
        node=next(n for n in nodes if n['panel_id']==c['panel_id']);r=load(c['output_prefix']+'-wpd.json');old=load(c['unguided_path'])
        box=c['region']['bbox_panel_px'];inside=lambda p:box[0]<=p['x']<box[2] and box[1]<=p['y']<box[3]
        if not all(inside(p) for p in r['points']):raise ValueError('backend escaped guided mask')
        prior=[p for p in old['points'] if inside(p)]
        columns=sorted({i%c['width'] for i in r['mask']});missing=sorted(set(range(c['width']))-set(columns))
        axis=calibrated_axis(node['source_panel']['x_axis'],node['image']['size'],0)
        if axis is None or node['source_panel']['y_axis']['ticks']:raise ValueError('expected calibrated angle and nonnumeric intensity')
        im=Image.open(c['image_path']).convert('RGB');draw=ImageDraw.Draw(im)
        for p in r['points']:draw.ellipse([p['x']-1,p['y']-1,p['x']+1,p['y']+1],fill='magenta')
        overlay=internal/(c['case_id']+'-overlay.png');im.save(overlay)
        t={'series':c['series'],'panel_id':c['panel_id'],'task_id':node['task_id'],'image':node['image'],
           'origin':c['origin'],'panel_image_path':c['image_path'],'region':c['region'],'x_axis':axis,'points':r['points'],
           'missing_color_columns':missing,'columns':c['width'],'color_mask_path':c['output_prefix']+'-wpd.json',
           'overlay_path':str(overlay),'source_request_sha256':node['source_request_sha256'],
           'checks':{'prior_in_region_points_identical':prior==r['points'],
                     'outside_points_removed':sum(not inside(p) for p in old['points']),
                     'apex_unchanged':bool(prior and r['points']) and min(prior,key=lambda p:p['y'])==min(r['points'],key=lambda p:p['y'])}}
        targets.append(t)
    # Timing is operational, never part of a scientific identity.
    timings=[]
    for path in internal.glob('*-wpd.json'):
        result=load(path);timings.append({'path':str(path),'elapsed_ms':result.pop('elapsed_ms',None)});atomic_json(path,result)
    atomic_json(directory/'timings.json',timings)
    members={str(p):sha(p) for p in internal.iterdir() if p.is_file()}
    bundle={'version':VERSION,'algorithm':algo,'source_packet_path':str(source),'source_packet_sha256':sha(source),
        'prior_plan_path':str(prior_plan_path),'prior_plan_sha256':sha(prior_plan_path),
        'prior_review_path':str(backend_root/'guided-region/source-region-check.json'),'prior_review_sha256':sha(backend_root/'guided-region/source-region-check.json'),
        'members':members,'targets':targets,'formal_acceptance':False}
    if (directory/'bundle.json').exists():atomic_json(directory/'history'/('bundle-'+sha(directory/'bundle.json')[:24]+'.json'),load(directory/'bundle.json'))
    atomic_json(directory/'bundle.json',bundle);return bundle

def checked_bundle(directory):
    b=load(Path(directory)/'bundle.json')
    for group in (b['members'],b['algorithm']['files'],b['algorithm']['runner']):
        for p,h in group.items():
            if sha(p)!=h:raise ValueError('guided dependency changed: '+p)
    for p,h in [(b['source_packet_path'],b['source_packet_sha256']),(b['prior_review_path'],b['prior_review_sha256']),(b['prior_plan_path'],b['prior_plan_sha256'])]:
        if sha(p)!=h:raise ValueError('guided source changed')
    for t in b['targets']:
        if sha(t['image']['path'])!=t['image']['sha256']:raise ValueError('guided image changed')
        if load(t['color_mask_path'])['points']!=t['points']:raise ValueError('guided points changed')
        source=next(p for p in load(b['source_packet_path'])['panels'] if p['module']=='planned-xrd' and p['panel_id']==t['panel_id'])
        if t['series'] not in source['series']:raise ValueError('guided source identity changed')
        reviews={r['region_id']:r for r in load(b['prior_review_path'])['regions']}
        review=bound_review(t['region'],t['panel_image_path'],t['image']['path'],load(b['prior_plan_path']),reviews)
        expected=(review or {}).get('decision','UNESTABLISHED_NO_PRIOR_REGION_SELECTION')
        if t['region']['geometry_review_status']!=expected:raise ValueError('unreviewed region promoted')
    return b

def consume(records,bundle,directory):
    directory=Path(directory);out=copy.deepcopy(records);changes=[];reports=[];mixes={m['mix_key']:m for m in out['mixes']}
    for t in bundle['targets']:
        s=t['series'];mix=mixes[s['record_key']];region=t['region'];cid='guided-xrd-'+digest([VERSION,t['panel_id'],s['series_id'],digest(bundle)])[:24]
        # Lack of a source-confirmed region is not silently promoted by geometry.
        usable=region['geometry_review_status']=='USABLE'
        stream=io.StringIO(newline='');w=csv.writer(stream);w.writerow(['angle_degree','x_panel_px','y_panel_px','intensity','status'])
        if usable:
            for p in t['points']:w.writerow([coordinate(p['x']+t['origin'][0],t['x_axis']),p['x'],p['y'],'','WPD_AVERAGE_ESTIMATE_PENDING'])
        raw=stream.getvalue().encode();h=hashlib.sha256(raw).hexdigest();assetkey='asset-'+digest([cid,h])[:24]
        path=directory/'assets'/(h[:24]+'.csv');atomic_bytes(path,raw)
        asset={'asset_key':assetkey,'paper_key':mix['paper_key'],'kind':'csv','mime_type':'text/csv','relative_path':str(path),'sha256':h,
            'extensions':{'formal':False,'review_status':'pending','algorithm':bundle['algorithm'],
                'region':region,'source_image_sha256':t['image']['sha256'],'diagnostic_points_path':t['color_mask_path'],
                'panel_origin_in_native_image_px':t['origin'],'source_native_image_size':t['image']['size']}}
        old=next((a for a in out['assets'] if a['asset_key']==assetkey),None)
        if old is not None and old!=asset:raise ValueError('guided asset collision')
        if old is None:out['assets'].append(asset)
        ext={'source_candidate_key':cid,'source_request_sha256':t['source_request_sha256'],'source_series':s,
            'recognition_status':'PARTIAL_GRAPHIC_ESTIMATE_PENDING' if usable else 'QUARANTINED_REGION_NOT_CONFIRMED',
            'spectrum':{'schema_version':'1.0','points_asset_key':assetkey,'x_axis':t['x_axis'],
                'y_axis':{'name':'Intensity (a.u.)','unit':'pixel','anchors':[],'calibration_status':'NO_NUMERIC_INTENSITY'},
                'extensions':{'formal':False,'review_status':'pending','source_region':region,'interpolation_applied':False,
                    'point_semantics':'WINDOW_AVERAGE_ESTIMATE_NOT_DIRECT_PIXEL','window_px':[2,2],
                    'resolution_scale_px':1,'uncertainty_note':'pixel/window scale only; no statistical CI or quantified shape error',
                    'normalization_applied':False,'numeric_intensity_available':False,
                    'missing_color_columns':t['missing_color_columns'],'gap_semantics':'NO_SELECTED_COLOR_PIXEL_CODE_OR_OCCLUSION_UNRESOLVED_NOT_SOURCE_ABSENCE',
                    'published_point_count':len(t['points']) if usable else 0,'diagnostic_point_count':len(t['points'])}}}
        row={'name':'xrd_pattern','value':None,'unit':None,'age_seconds':None,'specimen':None,'extensions':ext}
        rows=mix['modules']['characterizations'];found=[i for i,r in enumerate(rows) if r.get('extensions',{}).get('source_candidate_key')==cid]
        if found:
            n=found[0]
            if rows[n]!=row:raise ValueError('guided candidate drift')
        else:n=len(rows);rows.append(row)
        field=f'/modules/characterizations/{n}/extensions/spectrum/points_asset_key';ek='ev-'+cid
        pdf=next(a for a in records['assets'] if a['paper_key']==mix['paper_key'] and a['kind']=='pdf')
        image=t['image'];ev={'evidence_key':ek,'record_key':mix['mix_key'],'record_type':'mix','paper_key':mix['paper_key'],
            'field_path':field,'asset_key':pdf['asset_key'],'page':image['page'],'bbox':image['image_pdf_bbox'],
            'snippet':s['source_label'],'extraction_method':VERSION,'extensions':{'formal':False,'source_ids':[image['source_id']],
                'data_asset_key':assetkey,'navigation_standard':'CORRECT_FIGURE_PARENT','region_id':region['region_id']}}
        old=next((e for e in out['evidence_links'] if e['evidence_key']==ek),None)
        if old is not None and old!=ev:raise ValueError('guided evidence collision')
        if old is None:out['evidence_links'].append(ev)
        mix['field_provenance'][field]={'evidence_key':ek,'original_value':None,'original_unit':'pixel',
            'extraction_method':VERSION,'confidence':None,'review_status':'pending','formula':'WPD dx=2,dy=2 local averaging; x via saved source angle anchors'}
        changes.append({'record_key':mix['mix_key'],'field_path':field,'value':assetkey,'evidence_key':ek,'source_ids':[image['source_id']],'task_ids':[t['task_id']]})
        reports.append({'panel_id':t['panel_id'],'series_id':s['series_id'],'region_status':region['geometry_review_status'],
            'diagnostic_points':len(t['points']),'candidate_points':len(t['points']) if usable else 0,'missing_columns':len(t['missing_color_columns']),'checks':t['checks']})
    return out,{'changes':changes,'series':reports,'model_calls':0,'formal_acceptance':False,'publication_allowed':False}

def run(records,directory):
    directory=Path(directory);bundle=checked_bundle(directory);out,report=consume(records,bundle,directory)
    for k in ('mats','papers'):
        if out[k]!=records[k]:raise ValueError('guided changed untargeted group')
    for k in ('assets','evidence_links'):
        if out[k][:len(records[k])]!=records[k]:raise ValueError('guided changed historical assets/evidence')
    for old,new in zip(records['mixes'],out['mixes']):
        restored=copy.deepcopy(new);restored['modules']['characterizations']=restored['modules']['characterizations'][:len(old['modules']['characterizations'])]
        restored['field_provenance']={k:v for k,v in restored['field_provenance'].items() if k in old['field_provenance']}
        if restored!=old:raise ValueError('guided changed old MIX or 33 EDS observations')
    h=digest(out);path=directory/'objects'/(h[:24]+'.json');writes=0 if path.exists() else len(report['changes'])
    if path.exists() and digest(load(path))!=h:raise ValueError('guided object collision')
    if not path.exists():atomic_json(path,out)
    report.update(input_records_sha256=digest(records),output_records_sha256=h,output_path=str(path),
        scientific_writes=writes,non_target_preserved=True,bundle_sha256=sha(directory/'bundle.json'),geometry_status='SOURCE_GUIDED_PARTIAL_AND_QUARANTINED')
    if (directory/'result.json').exists():atomic_json(directory/'history'/(digest(load(directory/'result.json'))[:24]+'.json'),load(directory/'result.json'))
    atomic_json(directory/'result.json',report);return out,report

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root');p.add_argument('--directory',required=True);p.add_argument('--backend-root');p.add_argument('--records');a=p.parse_args()
    if a.records:
        _,r=run(load(a.records),a.directory);print(json.dumps({k:v for k,v in r.items() if k!='changes'}))
    else:
        b=prepare(a.root,a.directory,a.backend_root);print(json.dumps([{'series':t['series']['series_id'],'region':t['region'],'checks':t['checks']} for t in b['targets']]))
