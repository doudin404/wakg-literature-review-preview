"""Bind a semantic verdict to a reopened PSD candidate, without promoting a paper."""
import argparse
import hashlib
import json
from pathlib import Path
import verify_supplement_psd as source

SCOPE = 'RECOVERABLE_PARTIAL_IMAGE_OBSERVATIONS_ONLY'


def accept(run, proof_path, response):
    proof_path = Path(proof_path)
    proof = source.load(proof_path)
    if proof != source.verify(run):
        raise ValueError('PSD proof is stale or does not match source replay')
    proof_hash = source.sha(proof_path)
    if (response.get('verdict') != 'PASS'
            or response.get('scope') != SCOPE
            or response.get('proof_sha256') != proof_hash
            or response.get('records_sha256') != proof['records_sha256']
            or not response.get('message_id')
            or response.get('defects') != []):
        raise ValueError('PSD semantic response identity, scope, or verdict mismatch')
    required = {'curve_identity', 'axis_calibration', 'legend_exclusion',
                'six_percentiles', 'gap_preservation', 'reference_not_mix_ingredient'}
    checks = response.get('checks', {})
    if set(checks) != required or any(value is not True for value in checks.values()):
        raise ValueError('PSD semantic response lacks exhaustive scoped checks')
    return {'schema_version': 1, 'verdict': 'PASS', 'scope': SCOPE,
            'proof_sha256': proof_hash, 'records_sha256': proof['records_sha256'],
            'response_sha256': hashlib.sha256(json.dumps(response, sort_keys=True,
                ensure_ascii=False, separators=(',', ':')).encode()).hexdigest(),
            'percentile_count': proof['percentile_count'],
            'source_curve_count': proof['source_curve_count'],
            'missing_percentiles': proof['missing_percentiles'],
            'complete_curve_accepted': False, 'paper_accepted': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--proof', type=Path, required=True)
    parser.add_argument('--response', type=Path, required=True)
    parser.add_argument('--write', type=Path, required=True)
    args = parser.parse_args()
    result = accept(args.run, args.proof, source.load(args.response))
    source.extractor._atomic_bytes(args.write,
        json.dumps(result, sort_keys=True, ensure_ascii=False, indent=2).encode())
    print(json.dumps(result, ensure_ascii=False))
