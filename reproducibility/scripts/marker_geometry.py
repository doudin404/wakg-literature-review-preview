"""Marker centers from eroded foreground components, never largest-strip voting."""
import numpy as np
from PIL import Image, ImageFilter


def marker_center(mask, expected_x, top, bottom, excluded_regions=(), erosion_size=3):
    if erosion_size not in (3,5):raise ValueError('unsupported marker erosion size')
    if not 0<=expected_x<mask.shape[1] or not 0<=top<bottom<=mask.shape[0]:
        raise ValueError('invalid marker search bounds')
    clean=mask.copy()
    clean[:top+3]=False;clean[bottom-2:]=False
    for box in excluded_regions:
        x0,y0,x1,y1=box
        if not (0<=x0<x1<=mask.shape[1] and 0<=y0<y1<=mask.shape[0]):
            raise ValueError('invalid marker exclusion bounds')
        clean[y0:y1,x0:x1]=False
    # A 3x3 foreground core removes thin connecting strokes. Coordinates below
    # are independently measured from components, not supplied x-axis values.
    core=np.asarray(Image.fromarray(clean.astype('uint8')*255).filter(ImageFilter.MinFilter(erosion_size)))>0
    left=max(0,int(round(expected_x))-12);right=min(mask.shape[1],int(round(expected_x))+13)
    remaining={(int(y),int(x)+left) for y,x in np.argwhere(core[:,left:right])}
    candidates=[]
    while remaining:
        seed=min(remaining);remaining.remove(seed);stack=[seed];component=[seed]
        while stack:
            y,x=stack.pop()
            for dy in (-1,0,1):
                for dx in (-1,0,1):
                    point=(y+dy,x+dx)
                    if point in remaining:
                        remaining.remove(point);stack.append(point);component.append(point)
        ys=[p[0] for p in component];xs=[p[1] for p in component]
        width=max(xs)-min(xs)+1;height=max(ys)-min(ys)+1
        cx=(min(xs)+max(xs))/2;cy=(min(ys)+max(ys))/2
        if (min(width,height)>=3 and max(width,height)<=20 and len(component)>=9
                and max(width,height)/min(width,height)<=2 and abs(cx-expected_x)<=3
                and min(xs)>left and max(xs)<right-1):
            candidates.append({'pixel':[cx,cy],'core_bbox':[min(xs),min(ys),max(xs)+1,max(ys)+1],
                'core_pixel_count':len(component),'expected_x':expected_x,
                'x_residual_px':cx-expected_x,'stroke_half_span_px':(height+erosion_size-1)/2})
    if len(candidates)!=1:
        raise ValueError(f'marker center missing or ambiguous: {len(candidates)} candidates at x={expected_x}')
    return candidates[0]
