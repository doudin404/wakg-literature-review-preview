"""Script-owned bar geometry and ID-only source semantics; no paper-specific branches."""
import argparse
import copy
import hashlib
import json
import re
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw
from crossfigure_structure import plot_frame, contiguous_groups
from source_specimen_variants import digest
from source_quantity_producer import atomic_json

VERSION = 'categorical-bars-v1'
def load(p): return json.loads(Path(p).read_bytes())

def prepare(root, directory):
    root, directory = Path(root), Path(directory)
    req = load(root/'planned-sem-eds/source/request.json')
    semantics = load(root/'planned-sem-eds/source/model/response.json')
    graph = load(root/'crossfigure-structure/structure-report.json')
    images = {i['source_id']: i for i in req['images']}
    parents = {p['panel_id']: p for p in semantics['panels']}
    packet = {'version': VERSION, 'source_request_sha256':req['request_sha256'], 'panels':[]}
    directory.mkdir(parents=True, exist_ok=True)
    for node in graph['panels']:
        if node['kind'] != 'EDS_HORIZONTAL_BAR_INSET': continue
        im = images[node['source_id']]
        if hashlib.sha256(Path(im['path']).read_bytes()).hexdigest() != im['sha256']: raise ValueError('image hash')
        rgb = np.asarray(Image.open(im['path']).convert('RGB'))
        frame = plot_frame(rgb, node['bbox_px'])
        x0,y0,x1,y1 = frame
        bars = sorted(node['observed_bar_regions'], key=lambda b:b['bbox_px'][1])
        pitch = float(np.median(np.diff([(b['bbox_px'][1]+b['bbox_px'][3])/2 for b in bars])))
        regions=[]
        def add(kind, box, **extra):
            rid='r-'+digest([im['sha256'],kind,box])[:16]
            regions.append({'region_id':rid,'kind':kind,'bbox_px':box,**extra})
            return rid
        for bar in bars:
            box=bar['bbox_px']; cy=(box[1]+box[3])/2
            label_id=add('category_label',[max(0,x0-pitch*1.2),cy-pitch*.48,x0,cy+pitch*.48])
            add('bar',box,label_region_id=label_id,fill_rgb=bar['rgb'])
        # Segment printed tick-label column groups, not evenly spaced invented ticks.
        top=int(y1+pitch*.22); bottom=min(rgb.shape[0],int(y1+pitch*.9))
        left=max(0,int(x0-pitch*.45)); right=min(rgb.shape[1],int(x1+pitch*.45))
        ink=rgb[top:bottom,left:right].max(2)<170
        groups=contiguous_groups(np.flatnonzero(ink.any(0)))
        merged=[]
        for g in groups:
            if merged and g[0]-merged[-1][-1] <= 3: merged[-1]=np.concatenate([merged[-1],g])
            else: merged.append(g)
        for g in merged:
            if len(g)<2: continue
            a,c=left+int(g[0]),left+int(g[-1])+1
            center=(a+c)/2
            # Actual tick stub adjacent to the printed label; no coordinate from Agent.
            lo=max(0,int(center-pitch*.35)); hi=min(rgb.shape[1],int(center+pitch*.35)+1)
            strip=rgb[int(y1)+1:int(y1)+5,lo:hi].max(2)<180
            scores=strip.sum(0)
            candidates=contiguous_groups(np.flatnonzero(scores>=max(1,scores.max()*.8))+lo)
            if not candidates: continue
            chosen=min(candidates,key=lambda v:abs(float(np.mean(v))-center))
            position=float(np.mean(chosen))
            if not x0-1<=position<=x1+1: continue
            old=next((r for r in regions if r['kind']=='tick_label' and r['position_px']==position),None)
            if old:
                old['bbox_px'][0]=min(a,old['bbox_px'][0]);old['bbox_px'][2]=max(c,old['bbox_px'][2])
                old['region_id']='r-'+digest([im['sha256'],'tick_label',old['bbox_px']])[:16]
            else:add('tick_label',[a,top,c,bottom],position_px=position)
        crop=[max(0,int(x0-pitch*1.6)),max(0,int(y0-pitch*.6)),min(rgb.shape[1],int(x1+pitch*.6)),min(rgb.shape[0],int(y1+pitch*1.2))]
        raw=Image.fromarray(rgb).crop(crop).resize(((crop[2]-crop[0])*3,(crop[3]-crop[1])*3))
        draw=ImageDraw.Draw(raw)
        for n,r in enumerate(regions,1):
            r['number']=n
            b=[(r['bbox_px'][i]-crop[i%2])*3 for i in range(4)]
            draw.rectangle(b,outline='red',width=1)
            # Mark bar IDs only, leaving category/tick text unobscured.
            if r['kind']=='bar': draw.text((b[0]+8,b[1]+2),str(n),fill='black',stroke_width=1,stroke_fill='white')
        path=directory/(digest(node['panel_id'])[:12]+'-regions.png');raw.save(path)
        parent=parents[node['parent_panel_id']]
        packet['panels'].append({'panel_id':node['panel_id'],'parent':parent,'image':im,'frame_px':frame,'regions':regions,
            'numbered_image_path':str(path),'crop_bbox_px':crop,'orientation':'horizontal'})
    packet['packet_sha256']=digest(packet)
    atomic_json(directory/'packet.json',packet)
    return packet

def calibrate(ticks, orientation='horizontal'):
    if orientation not in ('horizontal','vertical'): raise ValueError('unsupported orientation')
    if len(ticks)<3: raise ValueError('at least three observed ticks required')
    p=np.array([t['position_px'] for t in ticks],float); v=np.array([t['value'] for t in ticks],float)
    if not np.isfinite(p).all() or not np.isfinite(v).all() or len(set(p))!=len(p) or len(set(v))!=len(v): raise ValueError('invalid ticks')
    order=np.argsort(p)
    if not (np.all(np.diff(v[order])>0) or np.all(np.diff(v[order])<0)): raise ValueError('nonmonotonic ticks')
    slope,intercept=np.polyfit(p,v,1)
    residual=float(np.max(np.abs((v-intercept)/slope-p)))
    if residual>2: raise ValueError('nonlinear or misbound ticks')
    return {'slope':float(slope),'intercept':float(intercept),'residual_px':residual,'pixel_value':abs(float(slope)),
        'pixel_domain':[float(min(p)),float(max(p))],'value_domain':[float(min(v)),float(max(v))],'ticks':ticks}

def measure(box, axis, orientation, baseline_value=0, endpoint_uncertainty_px=2):
    dim={'horizontal':0,'vertical':1}.get(orientation)
    if dim is None: raise ValueError('unsupported orientation')
    if not np.isfinite(box).all() or box[2]<=box[0] or box[3]<=box[1]: raise ValueError('invalid bar box')
    ends=[float(box[dim]),float(box[dim+2])]
    baseline=(baseline_value-axis['intercept'])/axis['slope']
    start=min(ends,key=lambda p:abs(p-baseline)); end=max(ends,key=lambda p:abs(p-baseline))
    if abs(start-baseline)>3: raise ValueError('bar does not start at declared baseline; stacked/clipped unsupported')
    domain=axis.get('plot_pixel_domain',axis['pixel_domain'])
    if end<domain[0]-2 or end>domain[1]+2: raise ValueError('endpoint outside calibrated range')
    extrapolation=max(axis['pixel_domain'][0]-end,end-axis['pixel_domain'][1],0)
    max_step=max(np.diff(sorted(t['position_px'] for t in axis['ticks'])))
    if extrapolation>max_step: raise ValueError('extrapolation exceeds one observed tick interval')
    value=axis['slope']*end+axis['intercept']
    # Conservative raster scale, not a statistical confidence interval.
    error=(endpoint_uncertainty_px+axis['residual_px'])*axis['pixel_value']
    return {'value':round(float(value),1),'unrounded_estimate':float(value),'error_scale':float(error),
        'endpoint_px':end,'baseline_px':baseline,'baseline_value':baseline_value,'pixel_value':axis['pixel_value'],
        'extrapolation_px':extrapolation,
        'formula':'value = slope * endpoint_px + intercept','method':'RASTER_BAR_DIGITIZATION_ESTIMATE'}

def consume(records,packet,response,source_request):
    if digest({k:v for k,v in packet.items() if k!='packet_sha256'})!=packet['packet_sha256'] or response['packet_sha256']!=packet['packet_sha256']:
        raise ValueError('packet identity mismatch')
    if packet['source_request_sha256']!=source_request['request_sha256']: raise ValueError('source request mismatch')
    out=copy.deepcopy(records); mixes={m['mix_key']:m for m in out['mixes']}; panels={p['panel_id']:p for p in packet['panels']}
    if len(response['panels'])!=len(panels) or {p['panel_id'] for p in response['panels']}!=set(panels): raise ValueError('panel coverage')
    tokens={t['token_id']:t for t in source_request['tokens']}; changes=[]; measurements=[]; unresolved=[]
    for selected in response['panels']:
        p=panels[selected['panel_id']]; im=p['image']; parent=p['parent']; regions={r['region_id']:r for r in p['regions']}
        if hashlib.sha256(Path(im['path']).read_bytes()).hexdigest()!=im['sha256']: raise ValueError('source image changed')
        if selected.get('unresolved'): unresolved.append({'panel_id':p['panel_id'],'reasons':selected['unresolved']}); continue
        if selected['unit']!='wt%' or selected['scale']!='linear': raise ValueError('unsupported source unit/scale')
        used=set();ticks=[]
        for t in selected['ticks']:
            rid=t['region_id']
            if rid in used or rid not in regions or regions[rid]['kind']!='tick_label': raise ValueError('tick region binding')
            used.add(rid);ticks.append({**t,'position_px':regions[rid]['position_px']})
        try: axis=calibrate(ticks,p['orientation'])
        except ValueError as e: unresolved.append({'panel_id':p['panel_id'],'reason':str(e)});continue
        d=0 if p['orientation']=='horizontal' else 1
        axis['plot_pixel_domain']=[p['frame_px'][d],p['frame_px'][d+2]]
        bars={r['region_id'] for r in p['regions'] if r['kind']=='bar'}
        if len(selected['bars'])!=len(bars) or {b['region_id'] for b in selected['bars']}!=bars: raise ValueError('bar coverage')
        if len({b['element'] for b in selected['bars']})!=len(bars): raise ValueError('duplicate element')
        mix=mixes[parent['record_key']]
        # Reuse the frozen caption/source age token, not a value guessed from order.
        match=re.fullmatch(r'(\d+(?:\.\d+)?)d',parent['age_label'])
        if not match: raise ValueError('age format unsupported')
        days=float(match[1]);age=days*86400
        context=[tokens[k] for k in parent['context_token_ids']]
        age_tokens=[t for t in context if re.sub(r'\s','',t['text']).rstrip('.').lower()==parent['age_label'].lower()]
        if len(age_tokens)!=1: raise ValueError('age source not unique')
        age_token=age_tokens[0]
        for b in selected['bars']:
            if not re.fullmatch('[A-Z][a-z]?',b['element']): raise ValueError('invalid element')
            region=regions[b['region_id']]
            try: m=measure(region['bbox_px'],axis,p['orientation'],selected['baseline_value'])
            except ValueError as e: unresolved.append({'panel_id':p['panel_id'],'region_id':b['region_id'],'reason':str(e)});continue
            if not 0<=m['value']<=100: raise ValueError('fraction outside physical range')
            candidate='bar-'+digest([packet['packet_sha256'],p['panel_id'],b])[:24]
            ext={'source_candidate_key':candidate,'source_request_sha256':packet['packet_sha256'],
                'element':b['element'],'panel_id':p['panel_id'],'parent_panel_id':parent['panel_id'],
                'sampling_scope':parent['sampling_scope'],'site_label':parent['site_label'],
                'digitization':{**m,'axis':axis,'bar_region_id':b['region_id'],'category_region_id':region['label_region_id'],
                    'uncertainty_kind':'RASTER_RESOLUTION_SCALE_NOT_STATISTICAL_CI','normalization_applied':False},
                'measurement_method':'SEM_EDS','recognition_status':'GRAPHIC_ESTIMATE_PENDING_REVIEW'}
            row={'name':'eds_element_fraction','value':m['value'],'unit':'wt%','age_seconds':age,'specimen':None,'extensions':ext}
            rows=mix['modules']['characterizations'];matches=[i for i,r in enumerate(rows) if r.get('extensions',{}).get('source_candidate_key')==candidate]
            if matches:
                n=matches[0]
                if rows[n]!=row: raise ValueError('existing bar candidate changed')
            else:n=len(rows);rows.append(row)
            pdf=next(a for a in out['assets'] if a['paper_key']==mix['paper_key'] and a.get('kind')=='pdf')
            bounds=im['image_pdf_bbox']; w,h=im['size']; box=p['frame_px']
            nav=[bounds[0]+box[0]/w*(bounds[2]-bounds[0]),bounds[1]+box[1]/h*(bounds[3]-bounds[1]),bounds[0]+box[2]/w*(bounds[2]-bounds[0]),bounds[1]+box[3]/h*(bounds[3]-bounds[1])]
            for field,value in [('value',m['value']),('age_seconds',age)]:
                path=f'/modules/characterizations/{n}/{field}';ek='ev-'+candidate+'-'+field
                isage=field=='age_seconds';snippet=age_token['text'] if isage else 'Graphical bar estimate; '+b['element']+'; weight (%)'
                ev={'evidence_key':ek,'record_key':mix['mix_key'],'record_type':'mix','paper_key':mix['paper_key'],'field_path':path,
                    'asset_key':pdf['asset_key'],'page':age_token['page'] if isage else im['page'],'bbox':age_token['bbox'] if isage else nav,
                    'snippet':snippet,'extraction_method':VERSION,'extensions':{'source_ids':[im['source_id']],
                        'image_sha256':im['sha256'],'source_packet_sha256':packet['packet_sha256'],'formal':False,
                        'digitization':m if not isage else None,'support_token_ids':parent['context_token_ids'],
                        'navigation_standard':'CORRECT_PARENT_PANEL','bar_region_id':b['region_id']}}
                prior=next((e for e in out['evidence_links'] if e['evidence_key']==ek),None)
                if prior is not None and prior!=ev: raise ValueError('bar evidence collision')
                if prior is None:out['evidence_links'].append(ev)
                mix['field_provenance'][path]={'evidence_key':ek,'original_value':age_token['text'] if isage else None,
                    'original_unit':'d' if isage else 'wt%','extraction_method':VERSION,'review_status':'pending','confidence':None,
                    'formula':f'{days} * 86400' if isage else f"{axis['slope']} * {m['endpoint_px']} + {axis['intercept']}"}
                changes.append({'record_key':mix['mix_key'],'field_path':path,'value':value,'evidence_key':ek,
                    'source_ids':[im['source_id']],'task_ids':[source_request['task']['task_id']]})
            measurements.append({'panel_id':p['panel_id'],'element':b['element'],'record_key':mix['mix_key'],**m})
    return out,{'changes':changes,'measurements':measurements,'unresolved':unresolved,'model_calls':0,
        'formal_acceptance':False,'publication_allowed':False,'geometry_status':'CALIBRATED_BAR_ESTIMATES_PENDING_REVIEW'}

def run(records,directory):
    directory=Path(directory); packet=load(directory/'packet.json'); response=load(directory/'response.json')
    req=load(directory.parent/'planned-sem-eds/source/request.json')
    out,report=consume(records,packet,response,req)
    for group in ('mats','assets','papers'):
        if records[group]!=out[group]:raise ValueError('untargeted group changed')
    if out['evidence_links'][:len(records['evidence_links'])]!=records['evidence_links']:raise ValueError('old evidence changed')
    for old,new in zip(records['mixes'],out['mixes']):
        restored=copy.deepcopy(new);restored['modules']['characterizations']=restored['modules']['characterizations'][:len(old['modules']['characterizations'])]
        restored['field_provenance']={k:v for k,v in restored['field_provenance'].items() if k in old['field_provenance']}
        if restored!=old:raise ValueError('non-target MIX data changed')
    h=digest(out);path=directory/'objects'/(h[:24]+'.json')
    writes=len(report['changes'])
    if path.exists():
        if digest(load(path))!=h:raise ValueError('bar output collision')
        writes=0
    else:atomic_json(path,out)
    report.update(input_records_sha256=digest(records),output_records_sha256=h,output_path=str(path),scientific_writes=writes,
        non_target_data_preserved=True,old_evidence_preserved=True,semantic_agent_response_sha256=digest(response))
    prior=directory/'result.json'
    if prior.exists():atomic_json(directory/'history'/(digest(load(prior))[:24]+'.json'),load(prior))
    atomic_json(prior,report)
    return out,report

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root');p.add_argument('--directory',required=True);p.add_argument('--records')
    a=p.parse_args()
    if a.records:
        _,report=run(load(a.records),a.directory)
        print(json.dumps({k:v for k,v in report.items() if k!='changes'}))
    else:
        if not a.root:p.error('--root required for preparation')
        packet=prepare(a.root,a.directory)
        print(json.dumps([{'panel':v['panel_id'],'frame':v['frame_px'],'ticks':[r for r in v['regions'] if r['kind']=='tick_label'],
            'bars':sum(r['kind']=='bar' for r in v['regions'])} for v in packet['panels']]))
