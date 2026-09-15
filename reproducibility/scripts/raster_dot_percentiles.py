"""Candidate-only percentiles from bounded printed-dot fragment centroids.

Never alter the source curve or smooth across gaps. Every reduced fragment
retains its source indices and bounds. Formal promotion is a separate review.
"""
import argparse
import copy
import hashlib
import json
import math
from pathlib import Path

from raster_curves import atomic_bytes, percentile
from verify_raster_curves import verify


def dot_centroids(series, pixel_uncertainty, max_fragment_width):
    if (not math.isfinite(pixel_uncertainty) or pixel_uncertainty <= 0 or
            type(max_fragment_width) is not int or max_fragment_width < 1 or
            max_fragment_width > 2 * pixel_uncertainty):
        raise ValueError('fragment bounds must fit declared pixel uncertainty')
    points = series['pixel_points']
    if (not points or any(len(p) != 2 or not all(math.isfinite(v) for v in p) for p in points)
            or any(b[0] <= a[0] for a, b in zip(points, points[1:]))):
        raise ValueError('ordered finite source pixels required')
    groups = []
    start = 0
    for i in range(1, len(points) + 1):
        if i == len(points) or points[i][0] - points[i-1][0] > 1:
            groups.append((start, i))
            start = i
    result = copy.deepcopy(series)
    reduced = []
    fragments = []
    maximum_radius = 0.0
    for start, end in groups:
        group = points[start:end]
        xs, ys = list(zip(*group))
        box = [min(xs), min(ys), max(xs), max(ys)]
        center = [sum(xs)/len(xs), sum(ys)/len(ys)]
        # No large stroke, long plateau, or disconnected dash is merged.
        eligible = (len(group) > 1 and box[2]-box[0] <= max_fragment_width and
                    all(abs(p[k]-center[k]) <= pixel_uncertainty
                        for p in group for k in (0, 1)))
        reduced.extend([center] if eligible else group)
        radius = max(abs(p[k]-center[k]) for p in group for k in (0, 1)) if eligible else 0.0
        maximum_radius = max(maximum_radius, radius)
        fragments.append({'source_index_range': [start, end], 'bbox_px': box,
                          'reduced': eligible, 'centroid_radius_px': radius,
                          'centroid_px': center if eligible else None})
    result['pixel_points'] = reduced
    result['derivation'] = {'method': 'bounded_contiguous_dot_column_centroid_v1',
                            'max_fragment_width_px': max_fragment_width,
                            'pixel_uncertainty': pixel_uncertainty, 'fragments': fragments,
                            'additional_centroid_uncertainty_px': maximum_radius,
                            'source_pixel_points': copy.deepcopy(points),
                            'note': 'Centroids are derived geometry, not new observed pixels.'}
    return result


def build(pdf, spec_path, candidate, label, width, output):
    if output.exists():
        raise ValueError('candidate derivation output must be fresh')
    spec = json.loads(spec_path.read_text(encoding='utf-8'))
    proof = verify(pdf, spec, candidate)
    if proof['verdict'] != 'PASS':
        raise ValueError('original source replay rejected')
    report = json.loads((candidate/'report.json').read_text(encoding='utf-8'))
    matches = [(p, s) for p in report['panels'] for s in p['series'] if s['label'] == label]
    if len(matches) != 1:
        raise ValueError('dot series must be unique')
    panel, series = matches[0]
    binding = panel['panel']
    derived = dot_centroids(series, binding['pixel_uncertainty'], width)
    uncertainty_binding = copy.deepcopy(binding)
    uncertainty_binding['pixel_uncertainty'] += derived['derivation']['additional_centroid_uncertainty_px']
    estimates = {f'd{p}_um': percentile(derived, uncertainty_binding, p) for p in (10, 50, 90)}
    result = {'scope': 'DOT_FRAGMENT_PERCENTILE_CANDIDATE_NOT_FORMAL',
              'source_proof': proof, 'label': label, 'panel': binding,
              'implementation_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'derivation': derived['derivation'], 'derived_pixel_points': derived['pixel_points'],
              'effective_pixel_uncertainty': uncertainty_binding['pixel_uncertainty'],
              'percentiles': estimates, 'formal_values_added': 0,
              'review_required': ['Printed dot identity and fragment width',
                                  'Centroid brackets and uncertainty', 'Canonical MAT ownership']}
    atomic_bytes(output, json.dumps(result, sort_keys=True, indent=2).encode())
    return {'label': label, 'percentiles': estimates, 'formal_values_added': 0}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    for key in ('pdf', 'specification', 'candidate', 'output'):
        parser.add_argument('--'+key, required=True, type=Path)
    parser.add_argument('--label', required=True)
    parser.add_argument('--max-fragment-width', required=True, type=int)
    args = parser.parse_args()
    print(json.dumps(build(args.pdf, args.specification, args.candidate, args.label,
                           args.max_fragment_width, args.output)))
