"""Filled bar candidates from explicit source regions; no sample-name inference."""
import argparse
import hashlib
import json
from pathlib import Path
import numpy as np
from PIL import Image
from raster_curves import calibrate_ticks
from vector_curves import coordinate,atomic_bytes,digest


def discover_filled_bars(image_path,include_short_regions=False):
    """Color-connected candidates only; the source producer supplies semantics."""
    from scipy.ndimage import label,find_objects
    rgb=np.array(Image.open(image_path).convert('RGB'));pixels=rgb.reshape(-1,3)
    colors,counts=np.unique(pixels,axis=0,return_counts=True);palette=[]
    for index in np.argsort(counts)[::-1]:
        c=colors[index]
        if counts[index]<rgb.shape[0]*rgb.shape[1]*.002:break
        if int(c.max())-int(c.min())<25:continue
        if any(np.linalg.norm(c.astype(float)-np.array(old))<25 for old in palette):continue
        palette.append(c.tolist())
    bars=[]
    for ci,color in enumerate(palette):
        mask=np.linalg.norm(rgb.astype(float)-np.array(color),axis=2)<=12
        labels,n=label(mask)
        for region in find_objects(labels):
            if region is None:continue
            ys,xs=region;w,h=xs.stop-xs.start,ys.stop-ys.start
            if w<4 or h<(3 if include_short_regions else max(10,1.4*w)):continue
            bars.append({'color_id':'C'+str(ci+1),'rgb':color,'bbox_px':[xs.start,ys.start,xs.stop,ys.stop]})
    bars.sort(key=lambda b:(b['bbox_px'][0],b['bbox_px'][1]))
    for i,b in enumerate(bars):b['bar_id']='B'+str(i+1)
    return {'native_size':[rgb.shape[1],rgb.shape[0]],'palette':palette,'bars':bars}


def extract_bar(rgb,panel,bar):
    x0,y0,x1,y1=bar['bbox_px']
    if not (0<=x0<x1<=rgb.shape[1] and 0<=y0<y1<=rgb.shape[0]):raise ValueError('bar region outside image')
    support=bar['minimum_row_coverage']
    if not .5<support<=1:raise ValueError('bar requires explicit majority row support')
    original_y0=y0
    if panel.get('endpoint_mode') in ('chromatic_region_top','absolute_chromatic_top'):
        if max(bar['rgb'])-min(bar['rgb'])<25:raise ValueError('chromatic region requires saturated fill')
        # Component discovery may omit short top fragments cut off by an error
        # cap. Recover only observed majority-color rows across <=2 ink rows;
        # stop at the first larger gap, never scan up into a detached legend.
        above=np.linalg.norm(rgb[:y0,x0:x1].astype(float)-np.array(bar['rgb']),axis=2)<=bar['color_distance']
        gaps=0
        for y in range(y0-1,-1,-1):
            if above[y].mean()>=support:y0=y;gaps=0
            else:
                gaps+=1
                if gaps>2:break
    mask=np.linalg.norm(rgb[y0:y1,x0:x1].astype(float)-np.array(bar['rgb']),axis=2)<=bar['color_distance']
    fractions=mask.mean(axis=1)
    rows=np.flatnonzero(fractions>=support)+y0
    if not len(rows):raise ValueError('no supported filled bar pixels')
    groups=np.split(rows,np.flatnonzero(np.diff(rows)>1)+1)
    gap_limit=panel.get('maximum_internal_occlusion_px',0)
    if type(gap_limit) is not int or not 0<=gap_limit<=2:raise ValueError('invalid internal occlusion limit')
    joined=[];occlusions=[]
    for group in groups:
        if joined and group[0]-joined[-1][-1]-1<=gap_limit:
            occlusions.append([int(joined[-1][-1]+1),int(group[0]-1)])
            joined[-1]=np.concatenate([joined[-1],group])
        else:joined.append(group)
    groups=joined
    baseline=panel['baseline_pixel'];tolerance=panel['baseline_tolerance_px']
    if not 0<=tolerance<=3:raise ValueError('invalid baseline tolerance')
    connected=[g for g in groups if min(abs(g[0]-baseline),abs(g[-1]-baseline))<=tolerance]
    mode=panel.get('endpoint_mode','baseline_connected_rows')
    if mode!='absolute_chromatic_top' and len(connected)!=1:raise ValueError('bar baseline association missing or ambiguous')
    body=connected[0] if connected else rows
    if mode in ('chromatic_region_top','absolute_chromatic_top'):
        # A black error stem/cap can cut an otherwise continuous colored fill
        # into several row groups. The region is the source-selected color
        # component bounding box, not a loose plot crop. Read all majority
        # color rows inside it; black error marks cannot become a color edge.
        if max(bar['rgb'])-min(bar['rgb'])<25:raise ValueError('chromatic region requires saturated fill')
        body=rows
    elif mode!='baseline_connected_rows':raise ValueError('unknown bar endpoint mode')
    if mode!='absolute_chromatic_top' and body[0]<baseline-tolerance and body[-1]>baseline+tolerance:raise ValueError('bar crosses baseline in both directions')
    endpoint=int(body[0] if mode=='absolute_chromatic_top' or abs(body[0]-baseline)>abs(body[-1]-baseline) else body[-1])
    axis=calibrate_ticks(panel['value_axis'])
    if not axis.get('unit') or axis['unit']=='pixel':raise ValueError('bar value axis unit unresolved')
    return {'source_label':bar['label'],'value':coordinate(endpoint,axis),'unit':axis['unit'],
            'endpoint_pixel':endpoint,'endpoint_interval_px':[endpoint-.5,endpoint+.5],
            'source_bbox_px':[x0,int(body[0]),x1,int(body[-1])+1],
            'baseline_pixel':baseline,'row_coverage':fractions.tolist(),
            'internal_occlusion_rows':occlusions,
            'endpoint_mode':mode,
            'source_component_bbox_px':bar['bbox_px'],
            'recovered_top_fragment_px':original_y0-y0,
            'excluded_supported_row_groups':[g.tolist() for g in groups if not np.array_equal(g,body)],
            'error_bar_value':None,'status':'SOURCE_BAR_CANDIDATE_NOT_FORMAL'}


def locate_axis_ticks(rgb,bars,declared_ticks,unit,use_full_axis=False):
    """Locate black tick stubs; retain source-selected tick values and order.

    Pixel coordinates from vision are proposals, not measurements. A unique
    long axis and connected, uniformly calibrated major ticks are required.
    No OCR, value relabeling, or paper/figure-specific calibration is used.
    """
    if not bars:raise ValueError('axis needs source-selected bars')
    boxes=[b['bbox_px'] for b in bars];left=min(b[0] for b in boxes)
    bottom=int(np.median([b[3]-1 for b in boxes]));width=int(np.median([b[2]-b[0] for b in boxes]))
    gray=(rgb.max(axis=2).astype(int)-rgb.min(axis=2).astype(int)<25)&(rgb.max(axis=2)<180)
    start=max(0,left-max(width*8,30))
    limit=rgb.shape[0] if use_full_axis else bottom+2
    scores=gray[:limit,start:left].sum(axis=0)
    columns=np.flatnonzero(scores>=(.5*rgb.shape[0] if use_full_axis else .75*(bottom+1)))+start
    if not len(columns):raise ValueError('native vertical axis not found')
    clusters=np.split(columns,np.flatnonzero(np.diff(columns)>1)+1)
    # A neighboring panel border can fall inside the search window. The
    # selected bars' own y axis is the closest long line to their left;
    # connected ticks below must still prove this choice.
    declared=sorted(declared_ticks,key=lambda p:p[0]);solutions=[]
    # When known bars were omitted from the requested delta, an earlier bar
    # outline can be closer than the true axis. It must also prove tick stubs.
    for cluster in (list(reversed(clusters)) if use_full_axis else [clusters[-1]]):
        x=int(cluster[0]);axis_bottom=bottom
        if use_full_axis:
            ys=np.flatnonzero(gray[:,cluster].any(axis=1))
            groups=np.split(ys,np.flatnonzero(np.diff(ys)>2)+1)
            axis_bottom=int(max(groups,key=len)[-1])
        candidates=[]
        for offset in range(2,max(7,width)):
            if x-offset<0:continue
            ys=np.flatnonzero(gray[:axis_bottom+5,x-offset]);groups=np.split(ys,np.flatnonzero(np.diff(ys)>1)+1)
            ticks=[]
            for g in groups:
                if not len(g):continue
                y=float(np.mean(g))
                if gray[g,x-offset:x+1].mean()<.75:continue
                ticks.append(y)
            if len(ticks)!=len(declared) or ticks[-1]-ticks[0]<.75*axis_bottom:continue
            axis={'scale':'linear','unit':unit,'ticks':[[y,t[1]] for y,t in zip(ticks,declared)],'tick_tolerance_px':2}
            try:calibrate_ticks(axis)
            except ValueError:continue
            candidates.append((offset,axis))
        if candidates:solutions.append((x,candidates))
    if len(solutions)>1:raise ValueError('multiple native axes match source tick labels')
    if solutions:x,candidates=solutions[0]
    if not candidates:raise ValueError('native tick count/calibration does not match source labels')
    # Adjacent columns through the same stubs can differ by subpixel antialiasing.
    best=candidates[-1][1]
    if any(max(abs(a[0]-b[0]) for a,b in zip(axis['ticks'],best['ticks']))>2 for _,axis in candidates):
        raise ValueError('native tick localization has conflicting solutions')
    zero=[t[0] for t in best['ticks'] if t[1]==0]
    if len(zero)!=1:raise ValueError('bar source has no unique zero tick')
    return best,zero[0],{'algorithm':'connected_native_axis_stubs_v1','axis_x':x,
        'declared_ticks':declared_ticks,'native_ticks':best['ticks'],
        'maximum_relocation_px':max(abs(a[0]-b[0]) for a,b in zip(declared,best['ticks'])),
        'source_values_changed':False,'formal':False}


def extract_image(image_path,specification,output):
    raw=Path(image_path).read_bytes()
    if hashlib.sha256(raw).hexdigest()!=specification['image_sha256']:raise ValueError('bar image hash mismatch')
    rgb=np.array(Image.open(image_path).convert('RGB'))
    if [rgb.shape[1],rgb.shape[0]]!=specification['native_size']:raise ValueError('bar image dimensions changed')
    labels=[b['label'] for p in specification['panels'] for b in p['bars']]
    if not labels or len(labels)!=len(set(labels)):raise ValueError('bar labels empty or duplicate')
    candidates=[extract_bar(rgb,p,b) for p in specification['panels'] for b in p['bars']]
    result={'source_image_sha256':specification['image_sha256'],'specification_sha256':digest(specification),
            'candidates':candidates,'formal_values_added':0}
    if Path(output).exists():raise ValueError('immutable bar output exists')
    atomic_bytes(Path(output),json.dumps(result,sort_keys=True,indent=2).encode())
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    for name in ('image','specification','output'):parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args()
    result=extract_image(args.image,json.loads(args.specification.read_bytes()),args.output)
    print(json.dumps({'candidates':len(result['candidates']),'formal_values_added':0}))
