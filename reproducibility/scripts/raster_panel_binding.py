"""Retain reviewed PSD panel identity after promotion; never approve new data."""
import copy
from raster_curves import digest


def bind(records, specification):
    result=copy.deepcopy(records)
    projection=result['extensions']['raster_curve_projection']
    acceptance=projection['acceptance']
    if (acceptance.get('verdict')!='PASS' or acceptance.get('defects')
            or acceptance.get('specification_sha256')!=digest(specification)
            or projection['acceptance_sha256']!=digest(acceptance)):
        raise ValueError('raster panel binding requires exact promoted specification')
    labels={}
    for panel in specification['panels']:
        if panel.get('curve_type')!='cumulative_finer' or not panel.get('panel_label'):
            raise ValueError('raster panel binding requires explicit PSD panel')
        for series in panel['series']:
            if series['label'] in labels:raise ValueError('ambiguous raster series panel')
            labels[series['label']]=panel
    candidates=result['extensions']['raster_curve_candidates']['items']
    assets={a['asset_key']:a for a in result['assets']}
    if len(assets)!=len(result['assets']):raise ValueError('duplicate raster asset identity')
    if len(candidates)!=len(labels) or {c['label'] for c in candidates}!=set(labels):
        raise ValueError('raster panel candidate coverage mismatch')
    from raster_projection_coverage import resolve
    coverage = resolve(acceptance,labels)
    seen=set()
    bound=0
    for candidate in candidates:
        key=candidate['data_asset_key']
        if key in seen:raise ValueError('duplicate raster candidate data asset')
        seen.add(key)
        if not coverage[candidate['label']]['curve']:
            continue
        panel=labels[candidate['label']];ext=assets[key]['extensions']
        image=assets[ext['source_image_asset_key']]
        if (ext.get('review_status')!='confirmed' or ext.get('acceptance_sha256')!=digest(acceptance)
                or image.get('kind')!='main_figure'
                or image['extensions'].get('figure_number')!=panel['figure']
                or image['extensions'].get('page')!=panel['page']):
            raise ValueError('raster panel promoted source mismatch')
        for name,value in {'figure_type':'PSD','panel_label':panel['panel_label']}.items():
            if ext.get(name) not in (None,value):raise ValueError('raster panel binding conflict')
            ext[name]=value
        bound+=1
    records.clear();records.update(result)
    return bound
