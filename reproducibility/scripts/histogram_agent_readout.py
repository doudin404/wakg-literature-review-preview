"""Agent-configured histogram scanning and one batched visual decision pass.

No paper names, material names, colour thresholds or axis coordinates are baked in.
The output directory owns the cached plan and decisions; source data stays unchanged.
"""
import argparse,copy,json
from pathlib import Path
import numpy as np
from scipy import ndimage
from PIL import Image,ImageDraw
from source_quantity_producer import invoke,obj,atomic_json
from selective_chart_policy import normalize
from vector_curves import coordinate

PLAN='''从附图直接返回直方图扫描配置 data_json:{series:[...]}。逐系列给 label、owner_key（沿用输入）、plot_bbox_px（完整绘图区）、rgb（柱体内部填充色）、color_distance、x_axis、y_axis。轴含 scale(linear/log10)、两对 anchors:[[原图像素,物理值],...]、unit，每个系列选择实际纵轴。obstruction_boxes_px仅列图例或文字实际占据的小框。脚本会从基线向上识别填充柱、区分其他颜色与累计虚线，整条分布范围由脚本确定。本次只给配置，全部坐标为原图像素。'''
DECIDE='''逐系列对照原图与带编号的红色候选短段。返回 data_json:{decisions:[{series_id,reject_segment_ids:[],uncertain_segment_ids:[],reason:说明}]}。仅选择明确不在目标柱顶、而在别的系列或柱体内部的错误短段编号。可见目标柱顶保留，即使下方被其他颜色覆盖。不确定短段列 uncertain_segment_ids 并保留。编号线指向该段中点；每张图的编号仅属于本系列。直接返回JSON，无需计算像素坐标。'''

def cached_call(folder,prompt,visible,images):
    path=Path(folder)/'response.json'
    if path.exists():response=json.loads(path.read_bytes())
    else:response=invoke({},folder,model='gpt-5.6-sol',reasoning_effort='medium',timeout=600,
                        schema=obj({'data_json':{'type':'string'}}),visible=visible,
                        image_paths=images,prompt_text=prompt)
    return json.loads(response['data_json'])

def scan(image,raw):
    s=normalize(raw);rgb=np.asarray(image.convert('RGB')).astype(float)
    distances=np.linalg.norm(rgb-np.asarray(s['rgb']),axis=2)
    mask=distances<=s['color_distance']
    support=mask.copy()
    for other in s.get('competing_rgb',[]):
        other_distance=np.linalg.norm(rgb-np.asarray(other),axis=2)
        support |= other_distance<=s['color_distance']
        mask &= distances<=other_distance
    # Grey bin borders and the axes are not coloured fill.
    if max(s['rgb'])-min(s['rgb'])>20:mask &= np.ptp(rgb,axis=2)>12
    for a,b,c,d in s.get('obstruction_boxes_px',[]):
        mask[int(b):int(d),int(a):int(c)]=False
        support[int(b):int(d),int(a):int(c)]=False
    x0,y0,x1,y1=map(int,s['plot_bbox_px'])
    baseline=round(coordinate(0,s['y_axis'],reverse=True))
    y1=min(y1,baseline+1)
    # Bin borders break individual columns, so test support in two dimensions.
    local=support[max(0,y0):min(mask.shape[0],y1),max(0,x0):min(mask.shape[1],x1)]
    connected=ndimage.binary_closing(local,structure=np.ones((3,3)))
    labels,_=ndimage.label(connected)
    base_ids=np.unique(labels[max(0,baseline-y0-5):])
    supported=np.isin(labels,base_ids[base_ids!=0])
    pixels=[]
    for x in range(max(0,x0),min(mask.shape[1],x1)):
        ys=np.flatnonzero(mask[max(0,y0):min(mask.shape[0],y1),x])+max(0,y0)
        if not len(ys):continue
        runs=np.split(ys,np.flatnonzero(np.diff(ys)>2)+1)
        attached=[run for run in runs if len(run)>=4 and np.any(supported[run-y0,x-x0])]
        if attached:
            longest=max(attached,key=len);pixels.append([x,int(longest[0])])
    return s,pixels

def segments(pixels):
    groups=[]
    for p in pixels:
        if not groups or p[0]-groups[-1][0][0]>=10 or p[0]-groups[-1][-1][0]>2:groups.append([])
        groups[-1].append(p)
    return groups

def retain_segments(pixels,decision):
    rejected=set(decision.get('reject_segment_ids',[]))
    return [p for j,g in enumerate(segments(pixels)) if j not in rejected for p in g]

def calibrate_palette(image,series):
    """Use the dominant actual fill colour near each Agent-proposed swatch."""
    rgb=np.asarray(image.convert('RGB')).astype(float)
    hints=np.asarray([s['rgb'] for s in series])
    distances=np.linalg.norm(rgb[:,:,None,:]-hints,axis=3)
    nearest=distances.argmin(axis=2);result=copy.deepcopy(series)
    for i,s in enumerate(result):
        a,b,c,d=map(int,s['plot_bbox_px']);mask=np.zeros(rgb.shape[:2],dtype=bool)
        mask[max(0,b):d,max(0,a):c]=True
        for x0,y0,x1,y1 in s.get('obstruction_boxes_px',[]):mask[int(y0):int(y1),int(x0):int(x1)]=False
        mask &= (nearest==i)&(distances[:,:,i]<80)&(np.ptp(rgb,axis=2)>15)
        samples=rgb[mask]
        if len(samples)<20:continue
        bins=(samples//8).astype(int);values,counts=np.unique(bins,axis=0,return_counts=True)
        dominant=values[counts.argmax()]
        s['agent_rgb']=s['rgb']
        s['rgb']=np.median(samples[np.all(bins==dominant,axis=1)],axis=0).tolist()
    return result

def run(image_path,panel,output,plan_response=None,prepare_only=False):
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    image=Image.open(image_path).convert('RGB')
    request=dict(protocol='histogram-fill-v6',image=str(Path(image_path).resolve()),size=list(image.size),panel=panel)
    request_path=output/'input.json'
    if request_path.exists() and json.loads(request_path.read_bytes())!=request:
        raise ValueError('Input changed: use a new output directory for the new run')
    atomic_json(request_path,request)
    if plan_response:
        prior=Path(plan_response)
        prior_input=json.loads((prior.parent.parent/'input.json').read_bytes())
        if any(prior_input[k]!=request[k] for k in ('image','size','panel')):
            raise ValueError('Reused plan belongs to a different source')
        plan=json.loads(json.loads(prior.read_bytes())['data_json'])
    else:plan=cached_call(output/'plan',PLAN,request,[str(image_path)])
    atomic_json(output/'resolved-plan.json',plan)
    plan['series']=calibrate_palette(image,plan['series'])
    atomic_json(output/'calibrated-plan.json',plan)
    candidates=[];images=[str(image_path)];summaries=[]
    for i,raw in enumerate(plan['series']):
        raw=copy.deepcopy(raw)
        raw['competing_rgb']=[s['rgb'] for j,s in enumerate(plan['series']) if j!=i]
        if panel.get('kind')=='psd':
            raw.update(kind='psd',curve_type='reported_volume_density',
                       property='particle_size_differential_volume_fraction')
        s,pixels=scan(image,raw);candidates.append((s,pixels))
        overlay=image.copy();draw=ImageDraw.Draw(overlay)
        for x,y in pixels:draw.ellipse((x-1,y-1,x+1,y+1),fill='red')
        box=list(map(int,s['plot_bbox_px']));name=output/f'series-{i}-overlay.png'
        crop=overlay.crop(box).resize(((box[2]-box[0])*2,(box[3]-box[1])*2))
        ticks=ImageDraw.Draw(crop)
        groups=segments(pixels)
        for number,group in enumerate(groups):
            x,y=group[len(group)//2];px,py=(x-box[0])*2,(y-box[1])*2
            ly=max(12,py-(26 if number%2 else 45))
            ticks.line((px,py,px,ly+10),fill='black',width=1)
            ticks.text((px-3,ly),str(number),fill='black',stroke_width=2,stroke_fill='white')
        for x in range(((box[0]+19)//20)*20,box[2],20):
            ticks.text(((x-box[0])*2,0),str(x),fill='black',stroke_width=1,stroke_fill='white')
        for y in range(((box[1]+19)//20)*20,box[3],20):
            ticks.text((0,(y-box[1])*2),str(y),fill='black',stroke_width=1,stroke_fill='white')
        crop.save(name)
        images.append(str(name));summaries.append(dict(series_id=i,label=s['label'],
            crop_bbox_px=box,display_scale=2,point_count=len(pixels),
            x_range=[pixels[0][0],pixels[-1][0]] if pixels else None,
            segments=[dict(id=j,points=len(g)) for j,g in enumerate(groups)]))
    atomic_json(output/'candidate-summary.json',summaries)
    if prepare_only:return [dict(label=s['label'],points=pixels) for s,pixels in candidates]
    decision=cached_call(output/'decision',DECIDE,dict(series=summaries),images)
    choices={d['series_id']:d for d in decision['decisions']};results=[]
    for i,(s,pixels) in enumerate(candidates):
        d=choices.get(i,{})
        groups=segments(pixels)
        kept=retain_segments(pixels,d)
        uncertain=[]
        for j in d.get('uncertain_segment_ids',[]):
            if 0<=j<len(groups):
                g=groups[j];xs,ys=zip(*g)
                uncertain.append(dict(bbox_px=[min(xs)-1,min(ys)-1,max(xs)+1,max(ys)+1],reason=d.get('reason','Uncertain segment retained')))
        item=copy.deepcopy(s);item.update(pixel_points=kept,
            points=[[coordinate(x,s['x_axis']),coordinate(y,s['y_axis'])] for x,y in kept],
            gap_before_indices=[j for j in range(1,len(kept)) if kept[j][0]-kept[j-1][0]>2],
            review_regions=uncertain,backend='agent_bound_histogram',approximate=True)
        if not kept:item['extraction_note']='No visually confirmed scan interval'
        final=image.copy();draw=ImageDraw.Draw(final)
        for x,y in kept:draw.ellipse((x-1,y-1,x+1,y+1),fill='red')
        final.crop(list(map(int,s['plot_bbox_px']))).save(output/f'series-{i}-accepted.png')
        results.append(item)
    result=dict(source_image=str(Path(image_path).resolve()),results=results,
                model_calls=sum(1 for p in output.glob('*/response.json')),decision=decision)
    atomic_json(output/'result.json',result)
    return results

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--image',required=True)
    p.add_argument('--panel',required=True);p.add_argument('--output',required=True)
    p.add_argument('--plan-response',help='Reuse an existing plan for this exact source when testing the scanner')
    p.add_argument('--prepare-only',action='store_true',help='Inspect scanner candidates without a decision model call')
    a=p.parse_args();print([(s['label'],len(s['points'])) for s in run(a.image,json.loads(Path(a.panel).read_bytes()),a.output,a.plan_response,a.prepare_only)])
