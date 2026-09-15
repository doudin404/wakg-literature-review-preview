"""Lossless figure crops/contact sheets and reversible pixel coordinates."""
import copy,math
from pathlib import Path
from PIL import Image,ImageDraw
from source_quantity_producer import atomic_json

def pack(jobs,output):
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    tiles=[];width=0;height=0;maps={}
    for j in jobs:
        with Image.open(j['image']) as im:
            b=j.get('bbox_px') or [0,0,im.width,im.height]
            box=[max(0,math.floor(b[0])-8),max(0,math.floor(b[1])-8),
                 min(im.width,math.ceil(b[2])+8),min(im.height,math.ceil(b[3])+8)]
            if box[2]<=box[0] or box[3]<=box[1]:raise ValueError('Empty figure crop')
            tile=im.convert('RGB').crop(box)
        key=j['source_id'];maps[key]=dict(source_image=str(Path(j['image']).resolve()),
            source_crop_px=box,tile_bbox_px=[0,height+24,tile.width,height+24+tile.height],
            offset_to_page=[box[0],box[1]-height-24],scale=1)
        tiles.append((key,tile,height));width=max(width,tile.width);height+=tile.height+36
    sheet=Image.new('RGB',(width,height),'white');draw=ImageDraw.Draw(sheet)
    for key,tile,y in tiles:draw.text((5,y+5),key,fill='black');sheet.paste(tile,(0,y+24))
    path=output/'input.png';sheet.save(path)
    atomic_json(output/'input-map.json',maps)
    return path,maps

def translate(value,dx,dy):
    """Translate only pixel coordinates; scientific point sets remain unchanged."""
    boxes={'bbox_px','plot_bbox_px','figure_bbox_px','trace_bbox_px','source_bbox_px','age_bbox_px'}
    boxlists={'exclude_boxes_px','exclude_boxes'}
    points={'pixel','center_px','centroid_px'}
    def anchor(v,delta):
        if isinstance(v,dict):
            if 'pixel_radius' in v:return copy.deepcopy(v)
            pixel=v.get('pixel',v.get('px',v.get('x_px',v.get('y_px'))))
            if isinstance(pixel,list):
                return {**v,'pixel':[pixel[0]+dx,pixel[1]+dy]}
            return [float(pixel)+delta,v.get('value',v.get('category'))]
        return [float(v[0])+delta,*v[1:]]
    def walk(obj,key=None):
        if isinstance(obj,dict):
            out={k:walk(v,k) for k,v in obj.items()}
            if key in {'x_axis','y_axis','value_axis','secondary_y_axis','secondary'}:
                delta=dx if key=='x_axis' or obj.get('orientation')=='horizontal' else dy
                for name in ('anchors','source_anchors','ticks'):
                    if isinstance(obj.get(name),list):
                        out[name]=[anchor(v,delta) for v in obj[name]]
            return out
        if isinstance(obj,list):
            if key in boxes and len(obj)==4 and all(isinstance(v,(int,float)) for v in obj):return [obj[0]+dx,obj[1]+dy,obj[2]+dx,obj[3]+dy]
            if key in boxlists:return [walk(v,'bbox_px') for v in obj]
            if key in points and len(obj)==2:return [obj[0]+dx,obj[1]+dy]
            if key in {'pixel_points','pdf_candidate_pixels'}:return [[p[0]+dx,p[1]+dy] for p in obj]
            if key=='review_regions':return [walk(v,'bbox_px') if isinstance(v,list) else walk(v) for v in obj]
            return [walk(v) for v in obj]
        return obj
    out=walk(copy.deepcopy(value))
    if isinstance(out,dict) and 'baseline_pixel' in out:
        out['baseline_pixel']+=dx if out.get('value_axis',{}).get('orientation')=='horizontal' else dy
    return out

def groups(jobs):
    """Pair genuinely small figures; large figures remain separate, without scaling."""
    small=[]
    for job in jobs:
        b=job.get('bbox_px')
        if b and b[2]-b[0]<=650 and b[3]-b[1]<=450:
            small.append(job)
            if len(small)==2:yield small;small=[]
        else:yield [job]
    if small:yield small
