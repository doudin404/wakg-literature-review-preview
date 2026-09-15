"""Select geometry by its demonstrated scope, not the broad figure name."""
import copy

CURVES={'curve','psd','ftir','xrd','nmr'}

def backend(panel):
    if panel.get('read_mode')=='scanned':return 'scan_first'
    if panel.get('read_mode')=='direct':return 'agent'
    if panel.get('geometry')=='histogram_raster':return 'histogram_raster'
    if panel.get('has_value_labels') and panel['kind'] not in CURVES:
        return 'agent'
    if panel['kind'] in CURVES and panel.get('geometry')=='pdf_vector':
        return 'vector' if all(s.get('source_path_indices') for s in panel.get('series',[])) and panel.get('series') else 'agent'
    if any(panel.get(k) for k in ('overlap','exploded','perspective')):
        return 'agent'
    if panel['kind']=='ftir' and panel.get('geometry')=='separated_ftir_raster':
        boxes=[s.get('trace_bbox_px') for s in panel.get('series',[])]
        if boxes and all(b and len(b)==4 for b in boxes):
            disjoint=all(a[2]<=b[0] or b[2]<=a[0] or a[3]<=b[1] or b[3]<=a[1]
                         for i,a in enumerate(boxes) for b in boxes[i+1:])
            if disjoint:return 'separated_ftir'
        return 'agent'
    if panel['kind'] in CURVES:
        return 'raster' if panel.get('isolated_single_curve') and len(panel.get('series',[]))==1 and panel.get('plot_area_clean') else 'agent'
    if panel['kind']=='bar':
        bars=panel.get('bars',[])
        coloured=bars and all(max(b.get('rgb',[0,0,0]))-min(b.get('rgb',[0,0,0]))>20 for b in bars)
        return 'raster' if panel.get('clear_solid_bars') and coloured else 'agent'
    if panel['kind']=='scatter':
        return 'raster' if panel.get('separated_markers') and panel.get('plot_area_clean') else 'agent'
    return 'agent'

def normalize(panel):
    p=copy.deepcopy(panel)
    for name in ('x_axis','y_axis','value_axis'):
        axis=p.get(name)
        if axis and len(axis.get('anchors',[]))>2 and all(isinstance(a,list) and isinstance(a[0],(int,float)) for a in axis['anchors']):
            ordered=sorted(axis['anchors'])
            axis['source_anchors']=axis['anchors']
            axis['anchors']=[ordered[0],ordered[-1]]
    if 'exclude_boxes' in p and 'exclude_boxes_px' not in p:
        p['exclude_boxes_px']=p['exclude_boxes']
    if 'exclude_boxes_px' in p:
        p['exclude_boxes']=p['exclude_boxes_px']
    return p
