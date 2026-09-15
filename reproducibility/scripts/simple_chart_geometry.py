"""Geometry for separated marks and filled stacks; complex panels route to Agent."""
import numpy as np
from scipy.ndimage import label,find_objects
from vector_curves import coordinate

def route(panel):
    from selective_chart_policy import backend
    return 'agent' if backend(panel)=='agent' else 'script'

def mask_for(rgb,series):
    return np.linalg.norm(rgb.astype(float)-np.asarray(series['rgb']),axis=2)<=series.get('color_distance',20)

def stacks(rgb,panel):
    results=[]
    for bar in panel['bars']:
        l,t,r,b=bar['bbox_px'];region=rgb[t:b,l:r]
        for s in panel['series']:
            rows=np.flatnonzero(mask_for(region,s).mean(1)>.5)
            if not len(rows):continue
            top,bottom=t+float(rows[0])-.5,t+float(rows[-1])+.5
            results.append(dict(category=bar['label'],series=s['label'],
                value=abs(coordinate(top,panel['value_axis'])-coordinate(bottom,panel['value_axis'])),
                bbox_px=[l,top,r,bottom],unit=panel['value_axis'].get('unit','')))
    return dict(kind='stacked_bar',values=results,approximate=True)

def components(rgb,panel):
    l,t,r,b=panel['plot_bbox_px'];out=[];unresolved=[]
    for s in panel['series']:
        mask=mask_for(rgb[t:b,l:r],s)
        for box in panel.get('exclude_boxes',[]):
            x0,y0,x1,y1=box;mask[max(0,y0-t):max(0,y1-t),max(0,x0-l):max(0,x1-l)]=False
        labels,_=label(mask);objects=find_objects(labels)
        for i,box in enumerate(objects,1):
            if box is None:continue
            yy,xx=np.nonzero(labels[box]==i);area=len(xx)
            if area<panel.get('min_area',6):continue
            ys,xs=box;x0,x1=l+xs.start,l+xs.stop;y0,y1=t+ys.start,t+ys.stop
            if panel['kind']=='scatter':
                if max(x1-x0,y1-y0)>panel.get('max_marker_size',15):
                    unresolved.append([x0,y0,x1,y1]);continue
                px=l+xs.start+float(xx.mean());py=t+ys.start+float(yy.mean())
                out.append(dict(series=s['label'],pixel=[px,py],x=coordinate(px,panel['x_axis']),y=coordinate(py,panel['y_axis'])))
            else:
                if x1-x0<3:continue
                py=y0-.5
                out.append(dict(series=s['label'],bbox_px=[x0,py,x1,y1],
                    x_min=coordinate(x0,panel['x_axis']),x_max=coordinate(x1,panel['x_axis']),
                    y=coordinate(py,panel['y_axis'])))
    return dict(kind=panel['kind'],values=out,agent_regions=unresolved,approximate=True)

def measure(rgb,panel):
    return stacks(rgb,panel) if panel['kind']=='stacked_bar' else components(rgb,panel)
