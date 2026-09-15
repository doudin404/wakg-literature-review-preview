"""One scan-first tool shared by scheduled curves and on-demand image reading."""
import copy,json
from pathlib import Path
import numpy as np
from PIL import Image,ImageDraw
from scipy import ndimage
from source_quantity_producer import invoke,obj,atomic_json
from vector_curves import coordinate
from selective_chart_policy import normalize

CURVES={'curve','psd','ftir','xrd','qxrd','nmr','spectrum'}

def response_json(text):
    value,end=json.JSONDecoder().raw_decode(text.lstrip())
    tail=text.lstrip()[end:].strip()
    if tail and any(c not in ']}' for c in tail):
        raise ValueError('Unexpected content after chart JSON')
    return value

def _fragments(mask,rgb,minimum_span=3):
    labels,_=ndimage.label(mask,np.ones((3,3)));result=[]
    for i,slices in enumerate(ndimage.find_objects(labels),1):
        if not slices:continue
        yy,xx=slices
        ys,xs=np.nonzero(labels[yy,xx]==i);xs+=xx.start;ys+=yy.start
        if len(xs)<3 or max(xx.stop-xx.start,yy.stop-yy.start)<minimum_span:continue
        # Keep elongated short ink strokes, not compact specks/glyph interiors.
        eigen=np.linalg.eigvalsh(np.cov(np.stack([xs,ys])))
        if eigen[-1]<4*max(eigen[0],.1):continue
        points=[[int(x),float(np.median(ys[xs==x]))] for x in np.unique(xs)]
        result.append(dict(bbox_px=[xx.start,yy.start,xx.stop,yy.stop],pixel_points=points,
            rgb=np.median(rgb[ys,xs],axis=0).tolist()))
    return result

def _group_fragments(parts):
    """Associate nearby aligned fragments; never paint pixels across the gap."""
    starts={};ends={}
    for i,a in enumerate(parts):
        pa=np.array(a['pixel_points']);u=pa[-1]-pa[max(0,len(pa)-4)]
        if np.linalg.norm(u)==0:continue
        for j,b in enumerate(parts):
            if i==j:continue
            pb=np.array(b['pixel_points']);gap=pb[0]-pa[-1];v=pb[min(3,len(pb)-1)]-pb[0]
            distance=np.linalg.norm(gap)
            if not 0<gap[0]<=14 or distance>14 or distance<1 or np.linalg.norm(v)==0:continue
            if np.linalg.norm(np.array(a['rgb'])-b['rgb'])>50:continue
            if any(np.dot(w,gap)/(np.linalg.norm(w)*distance)<.7 for w in (u,v)):continue
            if i not in ends or distance<ends[i][0]:ends[i]=(distance,j)
            if j not in starts or distance<starts[j][0]:starts[j]=(distance,i)
    following={i:j for i,(_,j) in ends.items() if starts.get(j,(None,None))[1]==i}
    preceded=set(following.values());out=[]
    for i in range(len(parts)):
        if i in preceded:continue
        chain=[parts[i]]
        while i in following:i=following[i];chain.append(parts[i])
        pts=sorted(pt for p in chain for pt in p['pixel_points'])
        out.append(dict(bbox_px=[min(p['bbox_px'][0] for p in chain),min(p['bbox_px'][1] for p in chain),
            max(p['bbox_px'][2] for p in chain),max(p['bbox_px'][3] for p in chain)],pixel_points=pts,
            fragment_count=len(chain)))
    return out

def scan(image,box=None,threshold=180,enhanced=True,split_straights=False):
    gray=np.asarray(image.convert('L'));mask=gray<threshold
    if box:
        a,b,c,d=[int(round(v)) for v in box];region=np.zeros_like(mask)
        region[max(0,b):min(mask.shape[0],d),max(0,a):min(mask.shape[1],c)]=True;mask &= region
    straight=np.zeros_like(mask)
    if split_straights:
        # Separate long horizontal/vertical ink; retain it as selectable candidates.
        straight=(ndimage.binary_opening(mask,structure=np.ones((1,max(40,image.width//5)))))
        straight |= ndimage.binary_opening(mask,structure=np.ones((max(40,image.height//3),1)))
        mask=mask & ~straight
    labels,_=ndimage.label(mask,np.ones((3,3)));components=[];retained=np.zeros_like(mask)
    for i,slices in enumerate(ndimage.find_objects(labels),1):
        if not slices:continue
        yy,xx=slices;w=xx.stop-xx.start;h=yy.stop-yy.start
        if w<max(18,image.width*.025) or h<3:continue
        ys,xs=np.nonzero(labels[yy,xx]==i);xs+=xx.start;ys+=yy.start
        retained[ys,xs]=True
        pixels=[[int(x),float(np.median(ys[xs==x]))] for x in np.unique(xs)]
        components.append(dict(id=len(components)+1,bbox_px=[xx.start,yy.start,xx.stop,yy.stop],pixel_points=pixels))
    if enhanced:
        rgb=np.asarray(image.convert('RGB'))
        weak=(gray<min(235,threshold+40)) & ~ndimage.binary_dilation(mask,iterations=1)
        if box:weak &= region
        for kind,extra in [('short',mask & ~retained),('faint',weak)]:
            for c in _group_fragments(_fragments(extra,rgb)):
                c.update(id=len(components)+1,pass_kind=kind)
                components.append(c)
    if split_straights:
        labels,_=ndimage.label(straight,np.ones((3,3)))
        for i in range(1,labels.max()+1):
            ys,xs=np.nonzero(labels==i)
            components.append(dict(id=len(components)+1,pass_kind='straight',
                bbox_px=[int(xs.min()),int(ys.min()),int(xs.max()+1),int(ys.max()+1)],
                pixel_points=[[int(x),float(np.median(ys[xs==x]))] for x in np.unique(xs)]))
    return components

def overlay(image,candidates,path):
    out=image.copy();draw=ImageDraw.Draw(out);colors=['red','blue','green','magenta','orange','cyan']
    for c in candidates:
        color=colors[(c['id']-1)%len(colors)]
        for x,y in c['pixel_points']:draw.point((x,y),fill=color)
        a,b,_,_=c['bbox_px'];draw.text((a,max(0,b-12)),str(c['id']),fill=color,stroke_width=1,stroke_fill='white')
    out.save(path)

SEMANTICS='''第一张是原图，第二张是脚本先扫描的编号轨迹，二者同像素坐标。选择有效轨迹、解释归属和坐标轴。
返回panel_json对象：{kind,plot_bbox_px,x_axis:{scale:"linear"或"log10",anchors:[[pixel,value],[pixel,value]],unit},y_axis:{scale,anchors,unit},series:[{label,owner_key,property,candidate_ids:[编号],clip_bbox_px可选}],review_regions:[]}。
同一曲线的多个片段可选多个编号。只选择实际扫描到的轨迹；缺失片段在review_regions记录，不估读曲线点集。
PSD注明curve_type；NMR注明isotope。纵轴无数字刻度时用绘图区上下界定义0到1相对强度并注明。
印刷数字可单独放direct_result.values，不重复提供整条曲线。需要局部重扫时返回rescan:{bbox_px,threshold可选}及原因；仅在看过扫描结果后使用。'''

def assemble(panel,candidates):
    if 'panels' in panel:
        shared={k:v for k,v in panel.items() if k!='panels'}
        return {'panels':[assemble({**shared,**p},candidates) for p in panel['panels']]}
    ya=panel.get('y_axis',{}).get('anchors',[])
    if ya and all(isinstance(a,list) and len(a)==3 for a in ya):
        panels=[]
        for box in dict.fromkeys(tuple(s['clip_bbox_px']) for s in panel.get('series',[]) if s.get('clip_bbox_px')):
            part=copy.deepcopy(panel);part['plot_bbox_px']=list(box)
            part['series']=[s for s in part['series'] if tuple(s.get('clip_bbox_px',[]))==box]
            part['x_axis']['anchors']=[a for a in part['x_axis']['anchors'] if box[0]<=a[0]<=box[2]]
            part['y_axis']['anchors']=[[a[1],a[2]] for a in ya if box[0]<=a[0]<=box[2]]
            # The supplied triples explicitly bind each vertical calibration to a panel x position.
            printed=panel.get('direct_result',{}).get('values',{})
            part.pop('direct_result',None)
            if isinstance(printed,dict):
                rows=[]
                for key,values in printed.items():
                    matches=[s for s in part['series'] if key.startswith(s.get('label','')+'_')]
                    if len(matches)==1:
                        rows.extend(dict(owner_key=matches[0].get('owner_key'),property=key,
                            value=v,unit='cm^-1' if key.endswith('cm^-1') else None) for v in values)
                if rows:part['direct_result']={'values':rows}
            panels.append(assemble(part,candidates))
        return {'panels':panels}
    p=normalize(copy.deepcopy(panel));lookup={c['id']:c for c in candidates};output=[]
    for raw in p.get('series',[]):
        s=copy.deepcopy(raw);pixels=[]
        for cid in s.get('candidate_ids',[]):pixels.extend(lookup.get(cid,{}).get('pixel_points',[]))
        box=s.get('clip_bbox_px',p.get('plot_bbox_px'))
        if box:
            a,b,c,d=box;pixels=[v for v in pixels if a<=v[0]<=c and b<=v[1]<=d]
        pixels=sorted(set(map(tuple,pixels)))
        s['pixel_points']=[list(v) for v in pixels]
        s['points']=[[coordinate(x,p['x_axis']),coordinate(y,p['y_axis'])] for x,y in pixels] if pixels else []
        s['gap_before_indices']=[i for i in range(1,len(pixels)) if pixels[i][0]-pixels[i-1][0]>1]
        s['approximate']=True;s['backend']='scan_first'
        if not pixels:s.setdefault('review_regions',[]).append({'reason':'no_selected_scan_pixels'})
        output.append(s)
    p['series']=output;p['read_mode']='scanned';p['geometry']='candidate_scan'
    return p

def scan_and_read(image_path,context,output,call=None):
    """Scan BEFORE any semantic call. Optional local retry follows observed output."""
    call=call or invoke;output=Path(output);output.mkdir(parents=True,exist_ok=True)
    with Image.open(image_path) as source:image=source.convert('RGB')
    region=None;threshold=180;previous=None
    for attempt in range(2):
        folder=output/str(attempt);folder.mkdir(parents=True,exist_ok=True)
        candidates=scan(image,region,threshold)
        from wpd_candidates import scan as wpd_scan
        for candidate in wpd_scan(image,folder/'wpd',region):
            candidate['id']=len(candidates)+1;candidates.append(candidate)
        atomic_json(folder/'candidates.json',candidates)
        marked=folder/'candidates.png';overlay(image,candidates,marked)
        visible=dict(context=context,candidates=[{k:v for k,v in c.items() if k!='pixel_points'} for c in candidates])
        if previous is not None:visible['previous_response']=previous
        response=call({},folder,model='gpt-5.6-sol',reasoning_effort='low',timeout=300,
            schema=obj({'panel_json':{'type':'string'}}),visible=visible,
            image_paths=[str(Path(image_path).resolve()),str(marked.resolve())],prompt_text=SEMANTICS)
        p=response_json(response['panel_json'])
        if attempt==0 and p.get('rescan'):
            previous=p;region=p['rescan'].get('bbox_px');threshold=p['rescan'].get('threshold',180)
            # Keep first-pass successes; only the requested local area is replaced.
            first=assemble(p,candidates) if p.get('series') else None
            continue
        result=assemble(p,candidates)
        if previous is not None and first:
            replacements={s.get('owner_key') or s.get('label'):s for s in result['series']}
            merged=[]
            for s in first['series']:
                key=s.get('owner_key') or s.get('label');replacement=replacements.pop(key,None)
                if replacement is None or not replacement.get('pixel_points'):
                    merged.append(s);continue
                a,b,c,d=region or [0,0,image.width,image.height]
                retained=[pt for pt in s['pixel_points'] if not (a<=pt[0]<=c and b<=pt[1]<=d)]
                pixels=sorted(set(map(tuple,retained+replacement['pixel_points'])))
                replacement['pixel_points']=[list(pt) for pt in pixels]
                replacement['points']=[[coordinate(x,result['x_axis']),coordinate(y,result['y_axis'])] for x,y in pixels]
                replacement['gap_before_indices']=[i for i in range(1,len(pixels)) if pixels[i][0]-pixels[i-1][0]>1]
                merged.append(replacement)
            result['series']=merged+list(replacements.values())
        def has_curve_points(value):
            panels=value.get('panels',[value])
            return any(s.get('points') for p in panels for s in p.get('series',[]))
        if not has_curve_points(result):
            # The tool has now run (including its one local retry) and did not
            # produce a usable trajectory.  This is the sole curve-estimation
            # entrance: the image Agent receives the observed scan failure,
            # and its answer replaces rather than accompanies the empty scan.
            from selective_chart_planner import DIRECT_PROMPT
            response=call({},output/'tool-failure-fallback',model='gpt-5.6-sol',reasoning_effort='low',timeout=300,
                schema=obj({'decision_json':{'type':'string'}}),
                visible=dict(context=context,scan_failure=result),
                image_paths=[str(Path(image_path).resolve())],
                prompt_text=DIRECT_PROMPT+' 工具已先扫描两轮但未得到可用轨迹。返回decision_json:{action:"direct",panels:[...]}，只估读未能扫描的曲线。')
            decision=response_json(response['decision_json'])
            panels=decision.get('panels',[])
            for fallback in panels:
                fallback['read_mode']='direct'
                fallback['tool_failure_confirmed']=True
                for series in fallback.get('series',[])+fallback.get('direct_result',{}).get('series',[]):
                    series['backend']='agent';series['approximate']=True
            result={'panels':panels,'tool_failure':'scan returned no curve points'}
        atomic_json(output/'panel.json',result);return result

def read_image(image_path,context,output,call=None):
    call=call or invoke;output=Path(output)
    kind=str(context.get('task',{}).get('kind','other')).lower()
    if kind in CURVES:
        result=scan_and_read(image_path,context,output/'scan',call)
        return result.get('panels',[result])
    from selective_chart_planner import DIRECT_PROMPT
    response=call({},output/'direct',model='gpt-5.6-sol',reasoning_effort='low',timeout=300,
        schema=obj({'decision_json':{'type':'string'}}),visible=context,image_paths=[str(Path(image_path).resolve())],
        prompt_text=DIRECT_PROMPT+' 返回decision_json:{action:"direct",panels:[...]}。若看图后识别为curve、PSD、FTIR、XRD、QXRD或NMR，必须返回{action:"scan"}，不直接估读曲线点集。工具先扫描并返回编号结果，首次调用无需扫描参数。')
    decision=response_json(response['decision_json'])
    detected_curve=any(str(p.get('kind','')).lower() in CURVES for p in decision.get('panels',[]))
    if decision['action']=='scan' or detected_curve:
        result=scan_and_read(image_path,dict(context,agent_request=decision),output/'scan',call)
        return result.get('panels',[result])
    panels=decision['panels']
    for p in panels:p['read_mode']='direct'
    return panels
