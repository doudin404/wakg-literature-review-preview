"""Missing-ToUnicode mappings from deduplicated local OCR lines."""
import hashlib
import importlib.util
import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = 'short-line-ocr-v3'

def cache_root():
    return Path(os.environ.get('WAKG_FONT_CACHE_DIR', ROOT/'internal_assets/font-ocr'))/VERSION

def labels_for_fonts(doc, items, cache_path=None):
    # Separate cache from earlier template/Agent labels, even when callers pass a path.
    directory = Path(cache_path).parent/VERSION if cache_path else cache_root()
    path = directory/'labels.json'
    labels = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    pending = [i for i in items if any(i['font_sha256']+':'+str(o['gid']) not in labels
               for o in i['occurrences'] if not o['decoded'].isspace())]
    if not pending:return labels, []
    identity = hashlib.sha256(''.join(sorted(i['font_sha256'] for i in pending)).encode()).hexdigest()[:20]
    work = directory/'requests'/identity
    # Fresh module instance gives each request independent paths.
    script = ROOT/'scripts/probe_local_line_ocr.py'
    spec = importlib.util.spec_from_file_location('_local_line_request', script)
    worker = importlib.util.module_from_spec(spec);spec.loader.exec_module(worker)
    worker.OUT = work
    worker.prepare(doc, pending)
    runtime = Path(os.environ.get('WAKG_OCR_PYTHON', ROOT/'internal_assets/marker-gpu-env/Scripts/python.exe'))
    subprocess.run([str(runtime), str(script), 'recognize', '--out', str(work)], check=True)
    fresh = worker.make_labels()
    for item in pending:
        for occurrence in item['occurrences']:
            key=item['font_sha256']+':'+str(occurrence['gid'])
            if key not in fresh and not occurrence['decoded'].isspace():
                fresh[key]={'text':occurrence['decoded'],'context_sensitive':True,
                            'method':'local-ocr-unresolved'}
    labels.update(fresh)
    directory.mkdir(parents=True,exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(labels,ensure_ascii=False,indent=2),encoding='utf-8');temp.replace(path)
    return labels, []
