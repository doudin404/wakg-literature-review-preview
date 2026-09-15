"""Candidate-only center sections of filled vector strokes, with source bounds.

PCA chooses a local scan direction, not the scientific fit. Each section is
the midpoint of two actual polygon intersections. Gaps are never filled in
the point asset; bounded percentile interpolation is a separate candidate.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import math
from pathlib import Path

import numpy as np

from vector_path_inventory import inventory
from vector_curves import coordinate, atomic_bytes

VERSION = 'vector-outline-sections-candidate-v2'


def validate_axis_intervals(axis):
    intervals = axis['anchor_pixel_intervals']
    if len(intervals) != 2 or len(axis['anchors']) != 2:
        raise ValueError('invalid axis anchor uncertainty count')
    for nominal, interval in zip(axis['anchors'], intervals):
        if (len(nominal) != 2 or len(interval) != 2 or
            not all(isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v) for v in [*nominal,*interval]) or
            not interval[0] <= nominal[0] <= interval[1]):
            raise ValueError('invalid axis anchor uncertainty')
    if len(intervals) != 2 or not (intervals[0][1] < intervals[1][0] or intervals[1][1] < intervals[0][0]):
        raise ValueError('axis anchor uncertainty overlaps')
    return intervals


def target_interval(specification, percent):
    axis = specification['y_axis']
    pad = specification['coordinate_uncertainty_points']
    if not math.isfinite(pad) or pad < 0:
        raise ValueError('invalid coordinate uncertainty')
    intervals = validate_axis_intervals(axis)
    values = []
    for a,b in itertools.product(*intervals):
        variant = dict(axis, anchors=[[a,axis['anchors'][0][1]],[b,axis['anchors'][1][1]]])
        values.append(coordinate(percent,variant,reverse=True))
    return [min(values)-pad,max(values)+pad]


def uncertainty_envelope(row, specification, percent, candidate):
    """Conservative source-geometry bounds over the complete y calibration slab.

Includes boundary segments plus nearest source vertices bracketing the slab.
This is an uncertainty envelope, not permission to bridge unprinted gaps.
"""
    low,high = target_interval(specification,percent)
    half_width = row.get('stroke_width_pt',0)/2 if row['path_type']=='s' else 0
    low -= half_width
    high += half_width
    vertices = [point for path in row['subpaths'] for item in path['items'] for point in (item['start'],item['end'])]
    if not vertices or low < min(p[1] for p in vertices) or high > max(p[1] for p in vertices):
        raise ValueError('uncertainty slab exceeds source coverage')
    points = []
    for path in row['subpaths']:
        for item in path['items']:
            (ax,ay),(bx,by) = item['start'],item['end']
            for point in (item['start'],item['end']):
                if low <= point[1] <= high:
                    points.append(point)
            if ay != by:
                for y in (low,high):
                    if min(ay,by) <= y <= max(ay,by):
                        points.append([ax+(y-ay)*(bx-ax)/(by-ay),y])
    for boundary, predicate in ((low,lambda y:y<=low),(high,lambda y:y>=high)):
        eligible = [p for p in vertices if predicate(p[1])]
        distance = min(abs(p[1]-boundary) for p in eligible)
        points.extend(p for p in eligible if abs(abs(p[1]-boundary)-distance)<1e-9)
    # Preserve the original nominal bracket envelope; never shrink it silently.
    box = candidate['source_bbox']
    xs = [p[0] for p in points]+[box[0],box[2]]
    bounds = [min(xs)-half_width,low,max(xs)+half_width,high]
    interval = x_envelope(bounds,specification)
    if not interval[0] <= candidate['value'] <= interval[1]:
        raise ValueError('nominal value outside uncertainty envelope')
    return {'interval_um':interval, 'y_pdf_interval': [low,high], 'source_geometry_bbox':bounds,
            'method':'all-source-segments-in-y-calibration-slab-plus-bracketing-vertices',
            'shape_semantics_review_required':True}


def center_sections(path):
    if not path['closed']:
        raise ValueError('filled contour is not closed')
    vertices = np.array([p['start'] for p in path['items']], dtype=float)
    if len(vertices) < 3 or not np.isfinite(vertices).all():
        raise ValueError('invalid filled contour')
    origin = vertices.mean(axis=0)
    eigenvalues, axes = np.linalg.eigh(np.cov((vertices - origin).T))
    if eigenvalues[0] <= 0:
        raise ValueError('degenerate filled contour')
    longitudinal = axes[:, 1]
    if longitudinal[0] < 0:
        longitudinal = -longitudinal
    transverse = np.array([-longitudinal[1], longitudinal[0]])
    local = np.column_stack(((vertices-origin) @ longitudinal, (vertices-origin) @ transverse))
    aspect = math.sqrt(eigenvalues[1] / eigenvalues[0])
    fractions = [0.5] if aspect < 1.5 else [0.05, 0.25, 0.5, 0.75, 0.95]
    points = []
    for fraction in fractions:
        u = float(local[:, 0].min() + fraction * np.ptp(local[:, 0]))
        hits = []
        for a, b in zip(local, np.roll(local, -1, axis=0)):
            if min(a[0], b[0]) <= u < max(a[0], b[0]):
                hits.append(float(a[1] + (u-a[0]) * (b[1]-a[1]) / (b[0]-a[0])))
        hits.sort()
        if len(hits) != 2:
            raise ValueError('ambiguous polygon section')
        center = origin + u * longitudinal + ((hits[0]+hits[1])/2) * transverse
        points.append({'pdf_point': center.tolist(), 'section_fraction': fraction,
                       'section_width_pt': hits[1]-hits[0]})
    return {'shape': 'compact_outline_identity_unresolved' if aspect < 1.5 else 'elongated_outline',
            'aspect_ratio': aspect, 'points': points, 'source_bbox': path['bbox']}


def x_envelope(bounds, specification):
    pad = specification['coordinate_uncertainty_points']
    axis = specification['x_axis']
    intervals = validate_axis_intervals(axis)
    result = []
    for a, b, x in itertools.product(intervals[0], intervals[1], [bounds[0]-pad, bounds[2]+pad]):
        variant = dict(axis, anchors=[[a, axis['anchors'][0][1]], [b, axis['anchors'][1][1]]])
        result.append(coordinate(x, variant))
    return [min(result), max(result)]


def percentile_candidate(samples, specification, percent):
    target = coordinate(percent, specification['y_axis'], reverse=True)
    ordered = sorted(samples, key=lambda s: (s['pdf_point'][0], s['pdf_point'][1]))
    crossings = [(a,b) for a,b in zip(ordered, ordered[1:])
                 if min(a['pdf_point'][1], b['pdf_point'][1]) <= target < max(a['pdf_point'][1], b['pdf_point'][1])]
    if len(crossings) != 1:
        return {'status': 'QUARANTINED', 'reason': 'intersection_not_unique', 'crossings': len(crossings)}
    a, b = crossings[0]
    ax, ay = a['pdf_point']; bx, by = b['pdf_point']
    if by >= ay:
        return {'status': 'QUARANTINED', 'reason': 'nonmonotone_intersection'}
    gap = math.hypot(bx-ax, by-ay)
    if a['fragment'] != b['fragment'] and gap > specification['max_percentile_gap_points']:
        return {'status': 'QUARANTINED', 'reason': 'fragment_gap_exceeds_limit', 'gap_pt': gap}
    x = ax + (target-ay)*(bx-ax)/(by-ay)
    boxes = [a['source_bbox'], b['source_bbox']]
    bounds = [min(v[0] for v in boxes), min(v[1] for v in boxes), max(v[2] for v in boxes), max(v[3] for v in boxes)]
    return {'status': 'CANDIDATE_NOT_ACCEPTED', 'value': coordinate(x, specification['x_axis']),
            'unit': 'um', 'pdf_intersection': [x,target], 'brackets': [a,b],
            'source_bbox': bounds, 'x_geometry_and_axis_envelope': x_envelope(bounds, specification),
            'uncertainty_status': 'Y_AXIS_AND_SHAPE_REVIEW_REQUIRED',
            'gap_pt': gap, 'method': 'local-polygon-section-midpoints; printed-axis interpolation'}


def direct_outline_intersection(paths, specification, percent):
    """Prefer the actually printed section at the requested ordinate to a gap."""
    target = coordinate(percent, specification['y_axis'], reverse=True)
    sections = []
    for index, path in enumerate(paths):
        if not path['closed']:
            continue
        hits = []
        for item in path['items']:
            (ax,ay),(bx,by) = item['start'],item['end']
            if min(ay,by) <= target < max(ay,by):
                hits.append(ax+(target-ay)*(bx-ax)/(by-ay))
        if hits:
            sections.append({'fragment':index,'hits':sorted(hits),'source_bbox':path['bbox']})
    if not sections:
        return None
    if len(sections) != 1 or len(sections[0]['hits']) != 2:
        return {'status':'QUARANTINED','reason':'ambiguous_direct_outline_section','sections':sections}
    section = sections[0]
    x = sum(section['hits'])/2
    return {'status':'CANDIDATE_NOT_ACCEPTED','value':coordinate(x,specification['x_axis']),
            'unit':'um','pdf_intersection':[x,target],'source_bbox':section['source_bbox'],
            'section':section,'x_geometry_and_axis_envelope':x_envelope(section['source_bbox'],specification),
            'uncertainty_status':'Y_AXIS_AND_SHAPE_REVIEW_REQUIRED','gap_pt':0,
            'method':'midpoint-of-two-original-outline-intersections-at-target-ordinate'}


def extract(pdf, specification):
    if specification['curve_type'] != 'cumulative_finer' or specification['x_axis']['unit'] != 'um' or specification['y_axis']['unit'] != '%':
        raise ValueError('cumulative percent and micrometre axes required')
    source = inventory(pdf, specification)
    results = []
    for row in source['series']:
        samples, fragments, rejected = [], [], []
        for index, path in enumerate(row['subpaths']):
            if row['path_type'] == 's':
                points = [p['start'] for p in path['items']] + [path['items'][-1]['end']]
                for point in points:
                    samples.append({'pdf_point': point, 'fragment': index,
                                    'source_bbox': [*point, *point]})
                continue
            try:
                shape = center_sections(path)
            except ValueError as error:
                rejected.append({'fragment': index, 'source_bbox': path['bbox'], 'reason': str(error)})
                continue
            fragments.append(dict(shape, fragment=index))
            for point in shape['points']:
                samples.append(dict(point, fragment=index, source_bbox=shape['source_bbox']))
        percentiles = {}
        for p in (10,50,90):
            target = coordinate(p, specification['y_axis'], reverse=True)
            nearby = [r for r in rejected if r['source_bbox'][1]-specification['coordinate_uncertainty_points'] <= target <= r['source_bbox'][3]+specification['coordinate_uncertainty_points']]
            direct = direct_outline_intersection(row['subpaths'], specification, p) if row['path_type'] == 'f' else None
            percentiles[f'd{p}_um'] = (direct if direct is not None else
                                      {'status': 'QUARANTINED', 'reason': 'unparsed_geometry_at_target', 'fragments': nearby}
                                      if nearby else percentile_candidate(samples, specification, p))
            value = percentiles[f'd{p}_um']
            if value['status'] == 'CANDIDATE_NOT_ACCEPTED':
                try:
                    value['uncertainty'] = uncertainty_envelope(row,specification,p,value)
                    value['uncertainty_status'] = 'XY_AXIS_GEOMETRY_BOUNDED_SHAPE_REVIEW_REQUIRED'
                except ValueError as error:
                    percentiles[f'd{p}_um'] = {'status':'QUARANTINED','reason':str(error),'nominal_candidate':value}
        results.append({'label': row['label'], 'mat_key': row['mat_key'], 'fragments': fragments,
                        'rejected_fragments': rejected, 'samples': samples, 'percentiles': percentiles})
    return {'algorithm': VERSION, 'status': 'CANDIDATE_NOT_FORMAL', 'source': source,
            'specification': specification,
            'dependency_sha256': {name:hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                                  for name in ('vector_path_inventory.py','vector_curves.py')},
            'implementation_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), 'series': results}


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--pdf', type=Path, required=True)
    parser.add_argument('--spec', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = extract(args.pdf, json.loads(args.spec.read_text(encoding='utf-8')))
    atomic_bytes(args.output, (json.dumps(report, sort_keys=True, separators=(',', ':'), allow_nan=False)+'\n').encode())
    print(json.dumps([{'label': r['label'], 'percentiles': {k:{z:v[z] for z in ('status','value','reason') if z in v} for k,v in r['percentiles'].items()}} for r in report['series']]))
