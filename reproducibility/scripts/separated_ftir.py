"""Trace continuous, spatially separated black FTIR spectra from source pixels.

The semantic planner supplies a box per series and the common axis calibration.
No dilation, smoothing, intensity normalization or model-estimated points are used.
"""
import copy
import numpy as np
from PIL import Image
from raster_curves import connected_stroke
from vector_curves import coordinate


def trace(rgb, panel, series):
    box = [int(round(v)) for v in series['trace_bbox_px']]
    x0, y0, x1, y1 = box
    if not (0 <= x0 < x1 <= rgb.shape[1] and 0 <= y0 < y1 <= rgb.shape[0]):
        raise ValueError('FTIR series box exceeds source image')
    mask = np.asarray(Image.fromarray(rgb).convert('L')) < 145
    for a, b, c, d in panel.get('exclude_boxes_px', []) + series.get('exclude_boxes_px', []):
        mask[max(0, int(b)):int(d), max(0, int(a)):int(c)] = False
    stroke, selection = connected_stroke(mask, box, .9)
    pixels, gaps, regions = [], [], []
    previous_x = None
    for x in range(x0, x1):
        ys = np.flatnonzero(stroke[:, x])
        if not len(ys):
            continue
        groups = np.split(ys, np.flatnonzero(np.diff(ys) > 1) + 1)
        if len(groups) > 1:
            regions.append(dict(bbox_px=[x, int(ys[0]), x+1, int(ys[-1])+1],
                                reason='multiple_strokes_in_column'))
        # Preserve endpoints of near-vertical ink instead of flattening a narrow
        # trough to its column median. Separate ink runs remain separate segments.
        if pixels and abs(pixels[-1][1]-ys[-1]) < abs(pixels[-1][1]-ys[0]):
            groups = list(reversed(groups))
        for j, group in enumerate(groups):
            values = [float(np.median(group))] if len(group) <= 3 else [float(group[0]), float(group[-1])]
            if pixels and len(values) == 2 and abs(pixels[-1][1]-values[-1]) < abs(pixels[-1][1]-values[0]):
                values.reverse()
            if pixels and (j > 0 or previous_x is not None and x-previous_x > 1):
                gaps.append(len(pixels))
            pixels.extend([[x, y] for y in values])
        previous_x = x
    for original in panel.get('review_regions', []) + series.get('review_regions', []):
        region=copy.deepcopy(original)
        if region.get('series') and region['series']!=series['label']:continue
        if region.get('bbox_px'):
            a,b,c,d=region['bbox_px']
            clipped=[max(a,x0),max(b,y0),min(c,x1),min(d,y1)]
            if clipped[0]>=clipped[2] or clipped[1]>=clipped[3]:continue
            region['bbox_px']=clipped
        if region not in regions:regions.append(region)
    return dict(label=series['label'], kind='ftir', backend='separated_ftir',
        approximate=True, pixel_points=pixels,
        points=[[coordinate(x, panel['x_axis']), coordinate(y, panel['y_axis'])] for x, y in pixels],
        gap_before_indices=gaps, review_regions=regions,
        x_axis=panel['x_axis'], y_axis=panel['y_axis'],
        trace_bbox_px=box, selection=selection)
