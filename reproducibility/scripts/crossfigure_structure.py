"""Frozen-response cross-figure geometry and local OS OCR, without model calls."""
import argparse
import copy
import hashlib
import json
import re
import subprocess
from pathlib import Path
import numpy as np
from PIL import Image
from scipy.ndimage import label,find_objects,binary_opening,minimum_filter1d,maximum_filter1d
from source_quantity_producer import atomic_json
from source_specimen_variants import digest

VERSION='crossfigure-structure-v1'

def ocr_batch(entries,directory):
    directory=Path(directory)
    pending=[e for e in entries if not (directory/'ocr'/f"{e['sha256']}.json").exists()]
    if pending:
        manifest=directory/'ocr-inputs.json';atomic_json(manifest,pending)
        r=subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(Path(__file__).with_name('windows_source_ocr.ps1')),'-Manifest',str(manifest.resolve())],capture_output=True,encoding='utf-8',timeout=240)
        if r.returncode:raise RuntimeError(r.stderr)
        for result in json.loads(r.stdout.lstrip('\ufeff')):atomic_json(directory/'ocr'/f"{result['sha256']}.json",result)
    return {e['sha256']:json.loads((directory/'ocr'/f"{e['sha256']}.json").read_bytes()) for e in entries}

def region_ocr(root,directory,requests):
    regions=[];directory=Path(directory)
    for module,req in requests.items():
        res=json.loads((Path(root)/module/'source/model/response.json').read_bytes());images={i['source_id']:i for i in req['images']}
        specs=[]
        if module=='planned-sem-eds':
            for c in res['claims']:
                loc=c['value_source']
                if loc['image_source_id']:
                    x0,y0,x1,y1=loc['bbox_px'];h=y1-y0
                    specs.append((c['claim_id'],loc['image_source_id'],[x0-2*h,y0-h*.5,x1+2*h,y1+h*.5]))
        else:
            for p in res['panels']:
                x0,y0,x1,y1=p['plot_bbox_px'];w=x1-x0;h=y1-y0
                specs.append((p['panel_id'],p['source_id'],[x0-w*.16,y0-h*.06,x1+w*.13,y1+h*.20]))
                for axis_name in ('x_axis','y_axis'):
                    boxes=[t['label_bbox_px'] for t in p[axis_name]['ticks']]
                    if not boxes:continue
                    xs=[b[0] for b in boxes];ys=[b[1] for b in boxes];xe=[b[2] for b in boxes];ye=[b[3] for b in boxes]
                    margin=float(np.median([b[3]-b[1] for b in boxes]))*.6
                    specs.append((p['panel_id']+':'+axis_name,p['source_id'],[min(xs)-margin,min(ys)-margin,max(xe)+margin,max(ye)+margin]))
                if p.get('kind')=='porosity_volume_fraction_summary':
                    specs.append((p['panel_id']+':binary',p['source_id'],[x0-w*.16,y0-h*.06,x1+w*.13,y1+h*.20]))
        for rid,sid,box in specs:
            im=images[sid];w,h=im['size'];box=[max(0,int(box[0])),max(0,int(box[1])),min(w,int(box[2]+1)),min(h,int(box[3]+1))]
            cropped=Image.open(im['path']).convert('RGB').crop(box)
            if rid.endswith(':binary'):
                array=np.asarray(cropped);mask=(array.min(2)<150).astype(np.uint8)*255
                cropped=Image.fromarray(255-mask).convert('RGB')
            scale=min(4,2500/max(cropped.size));size=[round(v*scale) for v in cropped.size];cropped=cropped.resize(size,Image.Resampling.LANCZOS)
            filename=re.sub(r'[^A-Za-z0-9_.-]','_',module+'-'+rid)+'-'+digest([module,rid])[:8]+'.png'
            path=directory/'search-regions'/filename;path.parent.mkdir(parents=True,exist_ok=True);cropped.save(path)
            regions.append({'module':module,'region_id':rid,'source_id':sid,'source_sha256':im['sha256'],'path':str(path.resolve()),
                'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'source_bbox':box,'scale_xy':[size[0]/(box[2]-box[0]),size[1]/(box[3]-box[1])]})
    results=ocr_batch(regions,directory)
    for region in regions:
        region['words']=[]
        for word in words(results[region['sha256']]):
            b=word['bbox'];origin=region['source_bbox'];sx,sy=region['scale_xy']
            region['words'].append({**word,'bbox':[b[0]/sx+origin[0],b[1]/sy+origin[1],b[2]/sx+origin[0],b[3]/sy+origin[1]]})
    atomic_json(directory/'region-ocr.json',regions);return regions

def ocr_sources(root,directory):
    directory=Path(directory);entries={};requests={}
    for module in ('planned-sem-eds','planned-mip','planned-thermal'):
        req=json.loads((Path(root)/module/'source/request.json').read_bytes());requests[module]=req
        for im in req['images']:
            path=Path(im['path']).resolve()
            if hashlib.sha256(path.read_bytes()).hexdigest()!=im['sha256']:raise ValueError('source image hash changed')
            entries[im['sha256']]={'path':str(path),'sha256':im['sha256']}
    pending=[e for sha,e in entries.items() if not (directory/'ocr'/f'{sha}.json').exists()]
    if pending:
        manifest=directory/'ocr-inputs.json';atomic_json(manifest,pending)
        r=subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(Path(__file__).with_name('windows_source_ocr.ps1')),'-Manifest',str(manifest.resolve())],capture_output=True,encoding='utf-8',timeout=240)
        if r.returncode:raise RuntimeError(r.stderr)
        results=json.loads(r.stdout.lstrip('\ufeff'))
        for result in results:atomic_json(directory/'ocr'/f"{result['sha256']}.json",result)
    return requests,{sha:json.loads((directory/'ocr'/f'{sha}.json').read_bytes()) for sha in entries}

def words(result):
    return [{**word,'line_index':li,'word_index':wi} for li,line in enumerate(result['lines']) for wi,word in enumerate(line['words'])]

def complete_label_region(rgb,box,min_overlap=.5):
    """Follow observed glyph components, not a fixed expansion of the old box."""
    x0,y0,x1,y1=box;h=y1-y0;H,W=rgb.shape[:2]
    top=max(0,int(y0-h));bottom=min(H,int(y1+h+1))
    local=rgb[max(0,int(y0)):min(H,int(y1)+1),max(0,int(x0)):min(W,int(x1)+1)].mean(2)
    threshold=min(205,float(np.median(local))-45)
    ink=rgb[top:bottom].mean(2)<threshold
    ink &= ~binary_opening(ink,structure=np.ones((1,max(12,int(h*2)))))
    cc,n=label(ink);glyphs=[];components=[]
    for sl in find_objects(cc):
        if sl is None:continue
        ys,xs=sl;gy0=ys.start+top;gy1=ys.stop+top;gh=gy1-gy0
        if gh<2 or gh>h*1.6 or xs.stop-xs.start>h*5:continue
        if xs.stop-xs.start>h*1.6 and ink[sl].mean()>.7:continue
        components.append([xs.start,gy0,xs.stop,gy1])
        if max(0,min(gy1,y1)-max(gy0,y0))<min(gh,h)*min_overlap:continue
        glyphs.append([xs.start,gy0,xs.stop,gy1])
    glyphs.sort();seed=[i for i,g in enumerate(glyphs) if g[2]>x0 and g[0]<x1]
    if not seed:raise ValueError('no glyph components at source label')
    a,b=min(seed),max(seed);typical=float(np.median([g[3]-g[1] for g in glyphs[a:b+1]]));gap=max(3,typical*.8)
    while a and glyphs[a][0]-glyphs[a-1][2]<=gap:a-=1
    while b+1<len(glyphs) and glyphs[b+1][0]-glyphs[b][2]<=gap:b+=1
    group=glyphs[a:b+1]
    result=[min(g[0] for g in group),min(g[1] for g in group),max(g[2] for g in group),max(g[3] for g in group)]
    # Reattach detached decimal/percent pieces on the same glyph run, using ink
    # proximity rather than adding a constant margin to the evidence box.
    for g in components:
        vertical_gap=max(0,result[1]-g[3],g[1]-result[3])
        if g[2]>result[0] and g[0]<result[2] and vertical_gap<=typical*.6:
            group.append(g)
    result=[min(g[0] for g in group),min(g[1] for g in group),max(g[2] for g in group),max(g[3] for g in group)]
    return {'bbox_px':result,'glyph_components':group,'method':'connected_glyph_run_from_source_anchor','recognition_verified':False}

def contiguous_groups(values):
    values=np.asarray(values)
    return [] if not len(values) else np.split(values,np.where(np.diff(values)>1)[0]+1)

def filled_rectangles(rgb,box):
    x0,y0,x1,y1=map(int,box);a=rgb[y0:y1,x0:x1];pixels=a.reshape(-1,3)
    colors,counts=np.unique(pixels//16*16,axis=0,return_counts=True);palette=[];rects=[]
    for i in np.argsort(counts)[::-1]:
        if counts[i]<len(pixels)*.005:break
        c=colors[i].astype(float)+7
        if c.max()-c.min()<20 or any(np.linalg.norm(c-old)<25 for old in palette):continue
        palette.append(c)
        mask=np.linalg.norm(a.astype(float)-c,axis=2)<25
        cc,_=label(mask)
        for sl in find_objects(cc):
            if sl is None:continue
            ys,xs=sl;w=xs.stop-xs.start;h=ys.stop-ys.start
            if w<5 or h<5 or w*h<80 or mask[sl].mean()<.7:continue
            rects.append({'bbox_px':[x0+xs.start,y0+ys.start,x0+xs.stop,y0+ys.stop],
                'rgb':np.median(a[sl][mask[sl]],axis=0).tolist()})
    return rects

def join_words(ocr_words,normalizer):
    matches=[]
    for i,w in enumerate(ocr_words):
        for n in range(1,5):
            group=ocr_words[i:i+n]
            if len(group)!=n or any(v['line_index']!=w['line_index'] for v in group):break
            text=''.join(v['text'] for v in group);value=normalizer(text)
            if value is None:continue
            matches.append({'value':value,'text':text,'bbox':[min(v['bbox'][0] for v in group),min(v['bbox'][1] for v in group),
                max(v['bbox'][2] for v in group),max(v['bbox'][3] for v in group)]})
    return matches

def printed_stack_values(panel,rgb,region_words,full_words):
    """Only exact OCR percentages with an actual fill, readable category and legend."""
    box=panel['plot_bbox_px'];rects=filled_rectangles(rgb,box)
    series=panel['series'];categories=[]
    for s in series:
        desired=re.sub(r'[^a-z0-9]','',(s['source_label']+s['age_label']).lower())
        categories+= [{**m,'series':s} for m in join_words(region_words,lambda t:desired if re.sub(r'[^a-z0-9]','',t.lower())==desired else None)]
    classes=join_words(region_words,lambda t:t.replace(' ','') if re.fullmatch(r'(?:<10|10[-–]100|>100)nm',t.replace(' ','')) else None)
    legends=[]
    for c in classes:
        a,b,d,e=c['bbox'];options=[r for r in rects if r['bbox_px'][2]<=a+3 and abs((r['bbox_px'][1]+r['bbox_px'][3])/2-(b+e)/2)<(e-b)*1.2]
        if options:legends.append({**c,'rgb':min(options,key=lambda r:a-r['bbox_px'][2])['rgb']})
    result=[];unresolved=[];seen=set()
    for w in region_words+full_words:
        if not re.fullmatch(r'\d+(?:\.\d+)?%',w['text']):continue
        a,b,c,d=w['bbox'];cx=(a+c)/2;cy=(b+d)/2
        if not(box[0]<cx<box[2] and box[1]<cy<box[3]):continue
        containing=[r for r in rects if r['bbox_px'][0]<=cx<=r['bbox_px'][2] and r['bbox_px'][1]<=cy<=r['bbox_px'][3]]
        if len(containing)!=1:continue
        bar=containing[0];cats=[cat for cat in categories if bar['bbox_px'][0]<sum(cat['bbox'][::2])/2<bar['bbox_px'][2] and cat['bbox'][1]>=box[3]-30]
        unique={(cat['series']['record_key'],cat['series']['age_label']):cat for cat in cats}
        colors=[l for l in legends if np.linalg.norm(np.array(l['rgb'])-bar['rgb'])<20]
        unique_color={l['value']:l for l in colors}
        if len(unique)!=1 or len(unique_color)!=1:
            unresolved.append({'text':w['text'],'bbox':w['bbox'],'reason':'CATEGORY_OR_PORE_CLASS_NOT_UNIQUE'});continue
        category=next(iter(unique.values()));legend=next(iter(unique_color.values()))
        key=(category['series']['record_key'],category['series']['age_label'],legend['value'])
        if key in seen:continue
        seen.add(key);result.append({'value':float(w['text'][:-1]),'unit':'%','raw_label':w['text'],'bbox_px':w['bbox'],
            'record_key':key[0],'age_label':key[1],'pore_scope':key[2],'category_evidence':category,'legend_evidence':legend,
            'bar_region':bar,'source_id':panel['source_id'],'panel_id':panel['panel_id'],'formal':False})
    return result,unresolved

def plot_frame(rgb,box):
    x0,y0,x1,y1=map(int,box);H,W=rgb.shape[:2];w=x1-x0;h=y1-y0
    black=(rgb.max(2)<180)
    opening=lambda axis,size:maximum_filter1d(minimum_filter1d(black,size=size,axis=axis,mode='constant'),size=size,axis=axis,mode='constant')
    horizontal=opening(1,max(20,int(w*.55)))
    vertical=opening(0,max(20,int(h*.55)))
    found=[]
    for dim,guess in ((0,x0),(1,y0),(0,x1),(1,y1)):
        span=w if dim==0 else h;limit=W if dim==0 else H
        lo=max(0,int(guess-span*.12));hi=min(limit,int(guess+span*.12)+1)
        if dim==0:scores=vertical[max(0,y0):min(H,y1),lo:hi].sum(0)
        else:scores=horizontal[lo:hi,max(0,x0):min(W,x1)].sum(1)
        groups=contiguous_groups(np.flatnonzero(scores>=(h if dim==0 else w)*.5)+lo)
        if not groups:raise ValueError('plot border not detected')
        chosen=min(groups,key=lambda g:abs(float(np.mean(g))-guess));found.append(float(np.mean(chosen)))
    return found

def relocate_ticks(rgb,frame,axis,dimension,ocr_words,side='left'):
    """Join declared numeric ticks to OCR labels and actual black axis stubs."""
    from planned_thermal import axis_check
    x0,y0,x1,y1=frame;result=copy.deepcopy(axis);unresolved=[];ticks=[]
    numeric=lambda s:float(s.replace('−','-').replace('—','-'))
    for tick in axis['ticks']:
        candidates=[]
        for word in ocr_words:
            try:v=numeric(word['text'])
            except ValueError:continue
            if abs(v-tick['value'])>1e-8:continue
            a,b,c,d=word['bbox'];cx=(a+c)/2;cy=(b+d)/2
            if dimension==0:
                eligible=x0-15<=cx<=x1+15 and y1<=cy<=y1+(y1-y0)*.18
            elif side=='right':eligible=x1<=cx<=x1+(x1-x0)*.18 and y0-15<=cy<=y1+15
            else:eligible=x0-(x1-x0)*.18<=cx<=x0 and y0-15<=cy<=y1+15
            if eligible:candidates.append(word)
        if len(candidates)!=1:
            unresolved.append({'value':tick['value'],'reason':'OCR_TICK_NOT_UNIQUE','matches':len(candidates)});continue
        word=candidates[0];a,b,c,d=word['bbox'];center=(a+c)/2 if dimension==0 else (b+d)/2
        # Text gives value identity; tick stub supplies coordinate, never text baseline.
        radius=max(6,(c-a if dimension==0 else d-b)*.6)
        lo=max(0,int(center-radius));hi=min(rgb.shape[1 if dimension==0 else 0],int(center+radius)+1)
        black=rgb.max(2)<180
        if dimension==0:
            line=int(round(y1));strip=black[max(0,line+3):min(black.shape[0],line+10),lo:hi];scores=strip.sum(0)
        else:
            line=int(round(x1 if side=='right' else x0));start=line+3 if side=='right' else line-10;end=line+10 if side=='right' else line-3
            strip=black[lo:hi,max(0,start):min(black.shape[1],end)];scores=strip.sum(1)
        if not len(scores) or scores.max()<3:
            unresolved.append({'value':tick['value'],'reason':'TICK_STUB_NOT_FOUND'});continue
        groups=contiguous_groups(np.flatnonzero(scores>=max(3,scores.max()*.8))+lo)
        nearest=sorted(groups,key=lambda g:abs(float(np.mean(g))-center))
        if not nearest:continue
        ticks.append({'value':tick['value'],'position_px':float(np.mean(nearest[0])),'label_bbox_px':word['bbox']})
    result['ticks']=ticks
    check=axis_check(result,[rgb.shape[1],rgb.shape[0]],dimension)
    return {'axis':result,'check':check,'unresolved':unresolved,'declared_tick_count':len(axis['ticks']),'complete':len(ticks)==len(axis['ticks'])}

def inspect_structures(root,directory,requests,regions):
    output={'version':VERSION,'panels':[],'sem_label_repairs':[],'printed_mip_values':[],'unresolved':[]}
    lookup={(r['module'],r['region_id']):r for r in regions}
    for module,req in requests.items():
        res=json.loads((Path(root)/module/'source/model/response.json').read_bytes());images={i['source_id']:i for i in req['images']}
        for p in res['panels']:
            im=images[p['source_id']];rgb=np.asarray(Image.open(im['path']).convert('RGB'))
            node={'module':module,'panel_id':p['panel_id'],'source_id':p['source_id'],'source_sha256':im['sha256'],
                'parent_panel_id':p.get('parent_panel_id'),'kind':p['kind'],'bbox_px':p.get('plot_bbox_px',p.get('bbox_px')),'axes':[],'formal':False}
            if module!='planned-sem-eds':
                region=lookup[(module,p['panel_id'])]
                try:frame=plot_frame(rgb,p['plot_bbox_px']);node['detected_frame_px']=frame
                except ValueError as exc:node['geometry_error']=str(exc);frame=None
                for dim,name in enumerate(('x_axis','y_axis')):
                    axis=p[name];categorical='categorical' in axis['direction'].lower();multiple='left' in axis['label'].lower() and 'right' in axis['label'].lower()
                    entry={'axis_id':p['panel_id']+':'+name,'kind':'categorical' if categorical else 'numeric','source_axis':axis,
                        'categories':p['series'] if categorical else [],'series_binding_status':'SOURCE_RESPONSE_ORDER_ONLY' if categorical else None}
                    if multiple:
                        entry['side']='left';entry['quantity']='pore_volume_fraction'
                        node['axes'].append({'axis_id':p['panel_id']+':y_right','kind':'numeric','side':'right','quantity':'porosity',
                            'unit':'%','scale':'linear','calibration_status':'RIGHT_AXIS_NOT_YET_LOCALIZED','source_reason':p['reason']})
                    tick_words=lookup.get((module,p['panel_id']+':'+name),region)['words']
                    if not categorical and frame:entry['localization']=relocate_ticks(rgb,frame,axis,dim,tick_words)
                    node['axes'].append(entry)
                if p['kind']=='porosity_volume_fraction_summary':
                    all_words=region['words']+lookup.get((module,p['panel_id']+':binary'),{'words':[]})['words']
                    full=json.loads((Path(directory)/'ocr'/f"{im['sha256']}.json").read_bytes())
                    values,issues=printed_stack_values(p,rgb,all_words,words(full));output['printed_mip_values']+=values;output['unresolved']+=issues
            elif p['kind']=='EDS_SPECTRUM':
                rectangles=filled_rectangles(rgb,p['bbox_px']);clusters=[]
                for r in rectangles:
                    match=next((g for g in clusters if abs(g[0]['bbox_px'][0]-r['bbox_px'][0])<6),None)
                    if match is None:clusters.append([r])
                    else:match.append(r)
                if clusters and len(max(clusters,key=len))>=3:
                    bars=max(clusters,key=len);b=[min(r['bbox_px'][0] for r in bars),min(r['bbox_px'][1] for r in bars),max(r['bbox_px'][2] for r in bars),max(r['bbox_px'][3] for r in bars)]
                    child={'module':module,'panel_id':p['panel_id']+':inset','parent_panel_id':p['panel_id'],'source_id':p['source_id'],
                        'source_sha256':im['sha256'],'kind':'EDS_HORIZONTAL_BAR_INSET','bbox_px':b,'observed_bar_regions':bars,
                        'axes':[{'axis_id':p['panel_id']+':inset:x','kind':'numeric','unit':'wt%','calibration_status':'UNLOCALIZED'},
                            {'axis_id':p['panel_id']+':inset:y','kind':'categorical','quantity':'element','categories':[],'binding_status':'UNLOCALIZED'}],
                        'formal':False,'geometry_status':'OBSERVED_FILLED_REGIONS_NOT_DIGITIZED'}
                    output['panels'].append(child)
            output['panels'].append(node)
        if module=='planned-sem-eds':
            for c in res['claims']:
                loc=c['value_source']
                if not loc['image_source_id']:continue
                im=images[loc['image_source_id']];rgb=np.asarray(Image.open(im['path']).convert('RGB'))
                try:
                    proof=complete_label_region(rgb,loc['bbox_px'])
                    output['sem_label_repairs'].append({'claim_id':c['claim_id'],'source_id':loc['image_source_id'],'old_bbox_px':loc['bbox_px'],
                        'source_verbatim':loc['verbatim'],**proof})
                except ValueError as exc:output['unresolved'].append({'claim_id':c['claim_id'],'reason':str(exc)})
    atomic_json(Path(directory)/'structure-report.json',output);return output

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();reqs,ocr=ocr_sources(a.root,a.output)
    regions=region_ocr(a.root,a.output,reqs)
    report=inspect_structures(a.root,a.output,reqs,regions)
    print(json.dumps({'images':len(ocr),'regions':len(regions),'panels':len(report['panels']),'sem_label_repairs':report['sem_label_repairs'],
        'axes':[{k:p[k] for k in ('panel_id','axes')} for p in report['panels'] if p['axes']]},ensure_ascii=False))
