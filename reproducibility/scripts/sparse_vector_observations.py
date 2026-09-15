"""Source-bound sparse vector candidates; markers and error caps are not extra samples."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import fitz
from vector_curves import coordinate, digest, atomic_bytes

VERSION='sparse-vector-observations-v2-stem-proof'


def connected_stem(drawings,color,point,marker_bbox,cap,bounds):
    """Require the cap to touch a vertical stem reaching the same marker."""
    marker=fitz.Rect(marker_bbox)+(-.12,-.12,.12,.12)
    radius=max(abs(marker_bbox[i]-point[i%2]) for i in range(4))+.12
    envelope=fitz.Rect(point[0]-radius,point[1]-radius,point[0]+radius,point[1]+radius)
    matches=[]
    for index,drawing in enumerate(drawings):
        if drawing['type']!='s' or not same_color(drawing.get('color'),color) or len(drawing['items'])!=1:continue
        item=drawing['items'][0]
        if item[0]!='l':continue
        a,b=item[1:]
        if (not bounds.contains(a) or not bounds.contains(b) or abs(a.x-b.x)>.01
                or abs(a.x-point[0])>.05):continue
        touches_marker=(marker.x0<=a.x<=marker.x1 and max(a.y,b.y)>=marker.y0 and min(a.y,b.y)<=marker.y1)
        reaches_envelope=(envelope.x0<=a.x<=envelope.x1 and max(a.y,b.y)>=envelope.y0 and min(a.y,b.y)<=envelope.y1)
        if reaches_envelope and (abs(a.y-cap[1])<.01 or abs(b.y-cap[1])<.01):
            matches.append({'source_path_index':index,'pdf_segment':[list(a),list(b)],
                            'degenerate_native_segment':math.dist(a,b)<.01,
                            'marker_contact':'direct' if touches_marker else 'within_marker_extent_envelope',
                            'marker_extent_envelope':list(envelope),'gap_interpolated':False})
    if len(matches)!=1:raise ValueError('sparse error cap has no unique connected marker stem')
    return matches[0]


def same_color(actual, expected):
    return actual is not None and len(actual)==3 and max(abs(a-b) for a,b in zip(actual,expected))<.005


def marker_center(drawing):
    rect=drawing['rect'];items=drawing['items']
    if not 1<rect.width<6 or not 1<rect.height<6:return None
    if all(item[0]=='l' for item in items):
        vertices={tuple(p) for item in items for p in item[1:]}
        if len(vertices) not in (3,4):return None
        return [sum(p[i] for p in vertices)/len(vertices) for i in (0,1)]
    if (len(items)==1 and items[0][0]=='re') or (len(items)==4 and all(item[0]=='c' for item in items)):
        return [(rect.x0+rect.x1)/2,(rect.y0+rect.y1)/2]
    return None


def extract(pdf,spec):
    pdf=Path(pdf)
    if hashlib.sha256(pdf.read_bytes()).hexdigest()!=spec['pdf_sha256']:raise ValueError('sparse vector PDF mismatch')
    with fitz.open(pdf) as doc:
        page=doc[spec['page']-1];drawings=page.get_drawings();bounds=fitz.Rect(spec['plot_bbox'])
        for proof in spec['text_evidence']:
            actual=doc[proof['page']-1].get_textbox(fitz.Rect(proof['bbox']))
            if proof['token'] not in actual:raise ValueError('sparse vector axis/legend evidence mismatch')
        output=[]
        for series in spec['series']:
            paths=[]
            for index,drawing in enumerate(drawings):
                if drawing['type']!='s' or drawing.get('fill') is not None or not same_color(drawing.get('color'),series['color']):continue
                items=drawing['items']
                if len(items)!=spec['point_count']-1 or any(item[0]!='l' for item in items):continue
                if not bounds.contains(drawing['rect']) or drawing['rect'].width<bounds.width*.8:continue
                points=[list(items[0][1])]
                for item in items:
                    if math.dist(points[-1],item[1])>.01:raise ValueError('disconnected sparse polyline')
                    points.append(list(item[2]))
                if any(b[0]<=a[0] for a,b in zip(points,points[1:])):raise ValueError('sparse ages not ordered')
                paths.append((index,points))
            if len(paths)!=1:raise ValueError('sparse series path not unique')
            path_index,points=paths[0]
            markers=[]
            for index,drawing in enumerate(drawings):
                if not same_color(drawing.get('fill'),series['color']) or not bounds.contains(drawing['rect']):continue
                center=marker_center(drawing)
                if center is not None:markers.append((index,center,list(drawing['rect'])))
            observations=[]
            for point in points:
                matches=[m for m in markers if math.dist(m[1],point)<.12]
                if len(matches)!=1:raise ValueError('sparse vertex has no unique independent marker')
                marker_index,center,marker_bbox=matches[0]
                cap_candidates=[]
                for index,drawing in enumerate(drawings):
                    if drawing['type']!='s' or not same_color(drawing.get('color'),series['color']) or len(drawing['items'])!=1:continue
                    item=drawing['items'][0]
                    if item[0]!='l':continue
                    a,b=item[1:]
                    if (bounds.contains(a) and bounds.contains(b) and abs(a.y-b.y)<.01 and 1<abs(a.x-b.x)<6 and abs((a.x+b.x)/2-point[0])<.05
                            and abs(a.y-point[1])<10):cap_candidates.append((index,float(a.y),[list(a),list(b)]))
                if len(cap_candidates)!=2 or not min(c[1] for c in cap_candidates)<=point[1]<=max(c[1] for c in cap_candidates):
                    raise ValueError('sparse error caps not unique or do not bracket value')
                stems=[connected_stem(drawings,series['color'],point,marker_bbox,cap,bounds) for cap in cap_candidates]
                age=coordinate(point[0],spec['x_axis']);value=coordinate(point[1],spec['y_axis'])
                observations.append({'source_label':series['label'],'pdf_point':point,'age_from_axis':age,
                    'age_unit':spec['x_axis']['unit'],'value_from_axis':value,'unit':spec['y_axis']['unit'],
                    'source_path_index':path_index,'marker_path_index':marker_index,'marker_bbox':marker_bbox,
                    'marker_center_error_pt':math.dist(center,point),
                    'error_caps':[{'source_path_index':i,'pdf_segment':segment,'value_from_axis':coordinate(y,spec['y_axis'])} for i,y,segment in cap_candidates],
                    'error_stems':stems,
                    'uncertainty_semantics':None,'extraction_method':VERSION})
            output.extend(observations)
    return {'status':'DIGITIZED_CANDIDATES_REQUIRE_SEMANTIC_AND_OVERLAY_ACCEPTANCE','algorithm':VERSION,
            'pdf_sha256':spec['pdf_sha256'],'specification_sha256':digest(spec),'page':spec['page'],
            'figure':spec['figure'],'observations':output,'interpolated_observations':0,'formal_observations':0}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--pdf',type=Path,required=True)
    parser.add_argument('--spec',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();result=extract(args.pdf,json.loads(args.spec.read_text(encoding='utf-8')))
    data=json.dumps(result,ensure_ascii=False,sort_keys=True,indent=2).encode('utf-8')
    if args.output.exists() and args.output.read_bytes()!=data:raise ValueError('immutable sparse candidate output conflict')
    atomic_bytes(args.output,data)
    print(json.dumps({'observations':len(result['observations']),'sha256':hashlib.sha256(data).hexdigest(),'status':result['status']}))
