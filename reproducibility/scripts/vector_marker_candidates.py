"""Find translated copies of a source legend marker, not visually guessed dots."""
import hashlib
import json
from pathlib import Path

import fitz
import numpy as np

from vector_curves import atomic_bytes, coordinate
from vector_path_inventory import geometry


def signature(drawing):
    if drawing['type'] != 's' or len(drawing['items']) != 4:
        raise ValueError('not a four-line stroked marker')
    items = geometry(drawing['items'])
    lines = np.array([item[1:] for item in items],dtype=float)
    directions = lines[:,1]-lines[:,0]
    lengths = np.linalg.norm(directions,axis=1)
    if np.any(lengths<=0) or not np.isfinite(lines).all():
        raise ValueError('invalid marker line')
    normals = np.column_stack((-directions[:,1],directions[:,0]))/lengths[:,None]
    center,_,rank,_ = np.linalg.lstsq(normals,np.sum(normals*lines[:,0],axis=1),rcond=None)
    residual = float(np.max(np.abs(np.sum(normals*(center-lines[:,0]),axis=1))))
    if rank != 2 or residual > .02:
        raise ValueError('marker lines do not share a center')
    return {'center':center.tolist(),'normalized_lines':(lines-center).tolist(),
            'max_intersection_residual_pt':residual,'width_pt':drawing['width'],
            'color':drawing['color'],'line_cap':list(drawing['lineCap']),
            'geometry_sha256':hashlib.sha256(json.dumps(items,separators=(',',':'),ensure_ascii=True).encode()).hexdigest()}


def extract(pdf,spec,legend_index,tolerance=.04):
    if not 0<tolerance<=.05:
        raise ValueError('marker tolerance outside fixed bound')
    sha = hashlib.sha256(pdf.read_bytes()).hexdigest()
    if sha != spec['pdf_sha256']:
        raise ValueError('source PDF hash mismatch')
    with fitz.open(pdf) as document:
        drawings = document[spec['page']-1].get_drawings()
        reference = signature(drawings[legend_index])
        rows = []
        for index,drawing in enumerate(drawings):
            if index == legend_index:
                continue
            try:
                item = signature(drawing)
            except ValueError:
                continue
            if item['color']!=reference['color'] or item['line_cap']!=reference['line_cap'] or abs(item['width_pt']-reference['width_pt'])>1e-5:
                continue
            delta = float(np.max(np.abs(np.array(item['normalized_lines'])-reference['normalized_lines'])))
            if delta>tolerance:
                continue
            x,y = item['center']; x0,y0,x1,y1 = spec['plot_bbox']
            if not x0<=x<=x1 or not y0<=y<=y1:
                continue
            if any(b[0]<=x<=b[2] and b[1]<=y<=b[3] for b in spec.get('exclude_boxes',[])):
                continue
            rows.append(dict(item,drawing_index=index,signature_max_delta_pt=delta,
                             calibrated_point=[coordinate(x,spec['x_axis']),coordinate(y,spec['y_axis'])]))
    return {'status':'MARKER_CANDIDATE_NOT_ACCEPTED','pdf_sha256':sha,'page':spec['page'],
            'legend_drawing_index':legend_index,'legend_signature':reference,'tolerance_pt':tolerance,
            'identity_authority':'source-legend-shape-match-requires-semantic-review','markers':rows}


if __name__ == '__main__':
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--pdf',type=Path,required=True)
    parser.add_argument('--spec',type=Path,required=True)
    parser.add_argument('--legend-index',type=int,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    report=extract(args.pdf,json.loads(args.spec.read_text(encoding='utf-8')),args.legend_index)
    atomic_bytes(args.output,(json.dumps(report,sort_keys=True,separators=(',',':'),allow_nan=False)+'\n').encode())
    print(json.dumps({'count':len(report['markers']),'indices':[m['drawing_index'] for m in report['markers']]}))
