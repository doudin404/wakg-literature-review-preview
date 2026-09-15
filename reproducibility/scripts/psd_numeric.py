"""PSD coverage and percentile calculations; keep source coordinates unchanged."""
import numpy as np

CUMULATIVE={'cumulative_finer','cumulative_coarser'}

def finer_points(item):
    xy=np.asarray(item.get('points',[]),float)
    if xy.ndim!=2 or xy.shape[1]!=2 or len(xy)<2 or not np.isfinite(xy).all():return None
    order=np.argsort(xy[:,0],kind='stable');xy=xy[order].copy()
    if np.any(np.diff(xy[:,0])<=0):return None
    unit=(item.get('y_axis') or {}).get('unit','%')
    if unit in {'fraction','proportion','0-1','1'}:xy[:,1]*=100
    if item.get('curve_type')=='cumulative_coarser':xy[:,1]=100-xy[:,1]
    if xy[:,1].min()<-.5 or xy[:,1].max()>100.5 or np.any(np.diff(xy[:,1])<-.5):return None
    return xy,order

def sufficient_coverage(item):
    data=finer_points(item)
    if data is None or item.get('gap_before_indices'):return False
    xy,_=data
    # Screening for missing shape, not a substitute for source-image verification.
    if len(xy)<5 or xy[0,1]>1 or xy[-1,1]<99:return False
    if np.max(np.diff(xy[:,1]))>25:return False
    x=xy[:,0]
    if (item.get('x_axis') or {}).get('scale')=='log10':
        if np.any(x<=0):return False
        x=np.log10(x)
    core=(xy[:-1,1]<99)&(xy[1:,1]>1)
    return not np.any(np.diff(x)[core]>(x[-1]-x[0])*.25)

def percentiles(item):
    data=finer_points(item)
    if data is None:return {}
    xy,order=data
    axis=item.get('x_axis') or {};factor={'um':1,'μm':1,'µm':1,'mm':1000,'nm':.001}.get(axis.get('unit'))
    if factor is None:return {}
    log=axis.get('scale')=='log10'
    if log and np.any(xy[:,0]<=0):return {}
    result={}
    for p in (10,50,90):
        exact=np.flatnonzero(np.isclose(xy[:,1],p,rtol=0,atol=1e-9))
        if len(exact)==1:result[p]=float(xy[exact[0],0])*factor;continue
        if len(exact)>1:continue
        hits=[i for i in range(len(xy)-1) if xy[i,1]<p<xy[i+1,1]]
        if len(hits)!=1:continue
        i=hits[0]
        if abs(int(order[i+1])-int(order[i]))!=1 or max(order[i],order[i+1]) in item.get('gap_before_indices',[]):continue
        x=np.log10(xy[i:i+2,0]) if log else xy[i:i+2,0]
        value=float(np.interp(p,xy[i:i+2,1],x))
        result[p]=(10**value if log else value)*factor
    return result
