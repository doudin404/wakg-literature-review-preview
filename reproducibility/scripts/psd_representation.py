"""Prefer a complete cumulative PSD over a duplicate differential rendering."""
import copy
import re
from psd_numeric import sufficient_coverage

def curve_type(series,panel):
    kind=series.get('curve_type',panel.get('curve_type',''))
    label=str(series.get('label','')).lower()
    if kind in {'cumulative_finer','cumulative_coarser'}:return 'cumulative'
    if kind in {'reported_volume_density','differential'}:return 'differential'
    if 'cumulative' in label:return 'cumulative'
    if 'differential' in label:return 'differential'
    return None

def key(series,panel,pi):
    owner=series.get('owner_key',panel.get('owner_key'))
    if not owner:return None
    group=series.get('distribution_id',panel.get('distribution_id'))
    if group:return (owner,group,series.get('age_seconds',panel.get('age_seconds')),
                     series.get('distribution_basis',panel.get('distribution_basis')))
    # Older bindings explicitly name both representations inside the same panel.
    label=str(series.get('label','')).lower()
    basis=next((b for b in ('volume','mass','number') if re.search(r'\b'+b+r'\b',label)),None)
    return (owner,pi,basis,series.get('age_seconds',panel.get('age_seconds'))) if basis else None

def complete(series):
    return sufficient_coverage(series)

def prefer_cumulative(panels):
    result=copy.deepcopy(panels);available=set()
    for pi,p in enumerate(result):
        if p.get('kind')!='psd':continue
        for s in p.get('series',[])+(p.get('direct_result') or {}).get('series',[]):
            k=key(s,p,pi)
            if k and curve_type(s,p)=='cumulative' and complete({**p,**s}):available.add(k)
    for pi,p in enumerate(result):
        if p.get('kind')!='psd':continue
        omitted=[]
        for container in (p,p.get('direct_result') or {}):
            if 'series' not in container:continue
            kept=[]
            for s in container['series']:
                if curve_type(s,p)=='differential' and key(s,p,pi) in available:
                    omitted.append(dict(label=s.get('label'),owner_key=s.get('owner_key'),
                                        reason='Equivalent complete cumulative PSD retained'))
                else:kept.append(s)
            container['series']=kept
        if omitted:
            p['psd_auxiliary_series']=omitted
            p['skip_auxiliary_psd']=not (p.get('series') or (p.get('direct_result') or {}).get('series')
                                         or (p.get('direct_result') or {}).get('values'))
    return result
