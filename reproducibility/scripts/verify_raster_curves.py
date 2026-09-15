"""Read-only verification of candidate bytes by full replay from immutable PDF.

Writes only an explicitly requested report and isolated disposable replay data.
PASS proves deterministic source replay, not scientific or publication acceptance.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from pathlib import Path

import raster_curves


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify(pdf: Path, specification: dict, candidate: Path):
    errors=[]
    report_path=candidate/'report.json'
    result={'scope':'native raster candidate source replay only; no scientific promotion',
            'pdf_sha256':None,'specification_sha256':raster_curves.digest(specification),
            'implementation_sha256':sha(Path(raster_curves.__file__)),
            'verifier_sha256':sha(Path(__file__)), 'report_sha256':None}
    try:
        if candidate.is_symlink() or report_path.is_symlink():
            raise ValueError('candidate path cannot be a link')
        result['pdf_sha256']=sha(pdf)
        result['report_sha256']=sha(report_path) if report_path.is_file() else None
        if result['pdf_sha256']!=specification['pdf_sha256']:
            raise ValueError('source PDF hash mismatch')
        if not report_path.is_file():
            raise ValueError('candidate report missing')
        stored=json.loads(report_path.read_text(encoding='utf-8'))
        if stored.get('status')!='CANDIDATE_NOT_FORMAL':
            raise ValueError('candidate status is not candidate-only')
        with tempfile.TemporaryDirectory(prefix='wakg-raster-source-proof-') as directory:
            replay=Path(directory)/'replay'
            raster_curves.extract(pdf,specification,replay)
            expected={p.name:sha(p) for p in replay.iterdir() if p.is_file()}
            if any(p.is_dir() or p.is_symlink() for p in candidate.iterdir()):
                raise ValueError('unexpected candidate nested path or link')
            actual={p.name:sha(p) for p in candidate.iterdir() if p.is_file()}
            if set(expected)!=set(actual):
                errors.append({'kind':'MEMBER_SET_MISMATCH','missing':sorted(set(expected)-set(actual)),
                               'extra':sorted(set(actual)-set(expected))})
            for name in sorted(set(expected)&set(actual)):
                if expected[name]!=actual[name]:
                    errors.append({'kind':'SOURCE_REPLAY_MISMATCH','member':name})
            result['member_hashes']=expected
            result['checked_csvs']=sum(name.endswith('.csv') for name in expected)
            result['checked_overlays']=sum(name.endswith('.png') for name in expected)
        result['checked_series']=sum(len(p['series']) for p in stored['panels'])
    except (ValueError,KeyError,TypeError,OSError) as exc:
        errors.append({'kind':'INVALID_SOURCE_OR_CANDIDATE','message':str(exc)})
    result['defects']=errors
    result['verdict']='REJECT' if errors else 'PASS'
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--pdf',type=Path,required=True)
    parser.add_argument('--specification',type=Path,required=True)
    parser.add_argument('--candidate',type=Path,required=True)
    parser.add_argument('--write',type=Path)
    args=parser.parse_args()
    if args.write and args.write.resolve().is_relative_to(args.candidate.resolve()):
        parser.error('proof must be outside immutable candidate directory')
    result=verify(args.pdf,json.loads(args.specification.read_text(encoding='utf-8')),args.candidate)
    if args.write:
        raster_curves.atomic_bytes(args.write,json.dumps(result,sort_keys=True,indent=2).encode('utf-8'))
    print(json.dumps(result,ensure_ascii=True))
    raise SystemExit(0 if result['verdict']=='PASS' else 2)
