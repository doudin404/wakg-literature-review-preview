"""Font-scoped annotation requests and reusable visual Unicode labels.

No document text is globally replaced. Labels identify font bytes and glyph ID.
The request directory is the interface to the extraction Agent: read the sheets,
write labels.json, then call import_labels and resume the same source read.
"""
import hashlib
import io
import json
import re
from pathlib import Path

from fontTools.cffLib import CFFFontSet
from fontTools.encodings.StandardEncoding import StandardEncoding

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CACHE = ROOT / 'internal_assets/font-annotations/cache.json'


def suspect_text(text):
    if '\ufffd' in text or any(ord(c) < 32 and c not in '\n\r\t' for c in text):
        return True
    # These letters are legitimate in names/prose; require a numeric/math context.
    return bool(re.search(r'[ðÞþ¼]', text) and
                re.search(r'\d|[=+−]|\b(?:strength|modulus|equation)\b', text, re.I))


def inventory(doc):
    fonts = {f[0]: f for p in doc for f in p.get_fonts(full=True)}
    by_font = {}
    suspicious_fonts = set()
    for page_number, page in enumerate(doc, 1):
        for block in page.get_text('dict')['blocks']:
            for line in block.get('lines', []):
                context = ''.join(s['text'] for s in line['spans'])
                if suspect_text(context):
                    for span in line['spans']:
                        if suspect_text(span['text']) or any(c in span['text'] for c in 'ðÞþ¼'):
                            suspicious_fonts.add(span['font'].split('+')[-1])
        for span in page.get_texttrace():
            name = span['font'].split('+')[-1]
            text = ''.join(chr(c[0]) for c in span['chars'])
            for code, gid, origin, bbox in span['chars']:
                by_font.setdefault(name, []).append({'gid': gid, 'decoded': chr(code),
                    'page': page_number, 'bbox': list(bbox), 'context': text})
    items = []
    for xref, font in fonts.items():
        data = doc.extract_font(xref)[3]
        if not data:
            continue
        name = font[3].split('+')[-1]
        occurrences = by_font.get(name, [])
        missing = doc.xref_get_key(xref, 'ToUnicode')[0] == 'null'
        suspect = name in suspicious_fonts or any(suspect_text(text) for text in {o['context'] for o in occurrences})
        if missing or suspect:
            items.append({'font': name, 'xref': xref,
                          'font_sha256': hashlib.sha256(data).hexdigest(),
                          'reason': 'MISSING_TOUNICODE' if missing else 'SUSPICIOUS_UNICODE',
                          'occurrences': occurrences})
    return items


def load_labels(path=DEFAULT_CACHE):
    path = Path(path)
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}


def code_gids(doc, xref):
    """Resolve PDF one-byte codes to CFF glyph IDs using declared encoding."""
    data = doc.extract_font(xref)[3]
    cff = CFFFontSet()
    cff.decompile(io.BytesIO(data), None)
    top = cff[cff.fontNames[0]]
    names = {name: gid for gid, name in enumerate(top.charset)}
    encoding = top.Encoding
    kind, value = doc.xref_get_key(xref, 'Encoding')
    base = value
    if kind == 'xref':
        base = doc.xref_get_key(int(value.split()[0]), 'BaseEncoding')[1]
    from reportlab.pdfbase import _fontdata
    if base.lstrip('/') in _fontdata.encodings:
        encoding = _fontdata.encodings[base.lstrip('/')]
    if isinstance(encoding, str):
        if doc.xref_get_key(xref, 'Encoding')[1] == '/WinAnsiEncoding':
            from reportlab.pdfbase._fontdata import WinAnsiEncoding
            encoding = WinAnsiEncoding
        else:
            encoding = StandardEncoding
    encoding = list(encoding)
    kind, value = doc.xref_get_key(xref, 'Encoding')
    if kind == 'xref':
        obj = doc.xref_object(int(value.split()[0]))
        diff = re.search(r'/Differences\s*\[(.*?)\]', obj, re.S)
        if diff:
            code = 0
            for token in re.findall(r'/([^\s/\[\]]+)|(\d+)', diff.group(1)):
                if token[1]:
                    code = int(token[1])
                else:
                    encoding[code] = token[0]
                    code += 1
    return {code: names[name] for code, name in enumerate(encoding)
            if name in names and name != '.notdef'}


def create_requests(doc, items, labels, output):
    """Render uncached glyphs once; retain original-page context alongside them."""
    import fitz
    from PIL import Image, ImageDraw
    from render_font_outlines import glyph_pixmap
    programs = {i['font_sha256']: doc.extract_font(i['xref']) for i in items}
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    requests = []
    for item in items:
        seen = set()
        for occ in item['occurrences']:
            key = item['font_sha256'] + ':' + str(occ['gid'])
            if key in labels or key in seen or occ['decoded'].isspace():
                continue
            seen.add(key)
            requests.append({'key': key, 'font': item['font'], 'reason': item['reason'], **occ})
    for start in range(0, len(requests), 20):
        batch = requests[start:start + 20]
        signature = hashlib.sha256(('outlines-v3'+json.dumps(batch, sort_keys=True)).encode()).hexdigest()[:12]
        target = output / f'sheet-{start // 20 + 1}-{signature}.png'
        if target.exists():
            continue
        canvas = Image.new('RGB', (1000, 90 * len(batch)), 'white')
        draw = ImageDraw.Draw(canvas)
        for n, req in enumerate(batch):
            page = doc[req['page'] - 1]
            rect = fitz.Rect(req['bbox']) + (-45, -5, 80, 5)
            pix = page.get_pixmap(clip=rect, matrix=fitz.Matrix(3, 3), alpha=False)
            crop = Image.frombytes('RGB', (pix.width, pix.height), pix.samples)
            mark = ImageDraw.Draw(crop)
            b = fitz.Rect(req['bbox'])
            mark.rectangle(((b.x0 * 3 - pix.x)-2, (b.y0 * 3 - pix.y)-2,
                            (b.x1 * 3 - pix.x)+2, (b.y1 * 3 - pix.y)+2), outline='red', width=1)
            crop.thumbnail((680, 80))
            canvas.paste(crop, (310, n * 90))
            font = programs[req['key'].split(':')[0]]
            if font[1] == 'cff':
                isolated = glyph_pixmap(font[3], req['gid'])
                if isolated:
                    glyph = Image.frombytes('RGB',(isolated.width,isolated.height),isolated.samples)
                    glyph.thumbnail((80,75))
                    canvas.paste(glyph,(215,n*90+10))
            draw.text((5, n * 90 + 5), f'{start+n+1}: {req["font"]} gid {req["gid"]}', fill='black')
        canvas.save(target)
    (output / 'request.json').write_text(json.dumps(requests, ensure_ascii=False, indent=2), encoding='utf-8')
    return requests


def annotate_requests(directory, cache_path=DEFAULT_CACHE):
    """One batched visual Agent call for a cold request; disk replay thereafter."""
    from source_quantity_producer import invoke, obj
    directory = Path(directory)
    requests = json.loads((directory / 'request.json').read_text(encoding='utf-8'))
    if not requests:
        return load_labels(cache_path)
    signature = hashlib.sha256(('outlines-v3' + json.dumps(requests, sort_keys=True)).encode()).hexdigest()[:20]
    run = directory / ('agent-' + signature)
    response_path = run / 'response.json'
    if response_path.exists():
        response = json.loads(response_path.read_text(encoding='utf-8'))
    else:
        sheets = []
        for start in range(0, len(requests), 20):
            digest = hashlib.sha256(('outlines-v3'+json.dumps(requests[start:start+20], sort_keys=True)).encode()).hexdigest()[:12]
            sheets.append(directory / f'sheet-{start//20+1}-{digest}.png')
        string = {'type': 'string'}
        schema = obj({'labels': {'type': 'array', 'items': obj({
            'id': {'type':'integer'}, 'text': string, 'context_sensitive': {'type': 'boolean'}})}})
        compact = [{'id': n+1, 'decoded': r['decoded'], 'context': r['context'][:100]}
                   for n,r in enumerate(requests)]
        response = invoke({'samples': requests}, run, model='gpt-5.6-luna',
            reasoning_effort='medium', schema=schema, visible={'samples': compact},
            prompt_text='Read each isolated glyph next to its numbered label; the red-boxed PDF crop is context. '
            'Return its Unicode text keyed by the numbered integer id on the image. Use the visible glyph, '
            'with neighbouring text to distinguish symbols. If a glyph serves multiple '
            'context-dependent roles (e.g. prime versus closing quote), set context_sensitive true. '
            f'Ordinary correctly decoded letters should retain their text. Return all {len(requests)} labels, ids 1 through {len(requests)}.',
            image_paths=sheets)
    labels = {requests[label['id']-1]['key']: {k: label[k] for k in ('text', 'context_sensitive')}
              for label in response['labels'] if 1 <= label['id'] <= len(requests)}
    missing = {r['key'] for r in requests} - labels.keys()
    if missing:
        raise ValueError(f'Font annotation response omitted {len(missing)} requested glyphs')
    return import_labels(labels, cache_path)


def import_labels(response, cache_path=DEFAULT_CACHE):
    """Merge Agent labels; preserve context-sensitive labels as alternatives."""
    cache = load_labels(cache_path)
    cache.update(response)
    target = Path(cache_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding='utf-8')
    return cache


def cached_corrections(doc, item, labels):
    if doc.extract_font(item['xref'])[1] != 'cff':
        return {}  # Request remains available for formats without a code adapter.
    result = {}
    for code, gid in code_gids(doc, item['xref']).items():
        label = labels.get(item['font_sha256'] + ':' + str(gid))
        if label:
            if label.get('canonical_glyph'):
                result[code] = label['canonical_glyph']
            elif not label.get('context_sensitive'):
                result[code] = label['text']
    return result


def native_baseline(doc, item):
    """Keep the decoder's actual glyph interpretation, including ligatures."""
    observed = {o['gid']: o['decoded'] for o in item['occurrences']
                if o['decoded'] != '\ufffd'}
    return {code: observed[gid] for code, gid in code_gids(doc, item['xref']).items()
            if gid in observed}


if __name__ == '__main__':
    import argparse
    import fitz
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pdf')
    parser.add_argument('--output', default=str(DEFAULT_CACHE.parent / 'manual-request'))
    parser.add_argument('--labels', help='Agent JSON keyed by font_sha256:glyph_id, values {text, context_sensitive}')
    parser.add_argument('--cache', default=str(DEFAULT_CACHE))
    parser.add_argument('--annotate', action='store_true', help='Run the batched visual Agent on the prepared request')
    args = parser.parse_args()
    if args.labels:
        import_labels(json.loads(Path(args.labels).read_text(encoding='utf-8')), args.cache)
    if args.pdf:
        with fitz.open(args.pdf) as doc:
            pending = create_requests(doc, inventory(doc), load_labels(args.cache), args.output)
            print(json.dumps({'pending_glyphs': len(pending), 'request_directory': args.output}))
    if args.annotate:
        annotate_requests(args.output, args.cache)
