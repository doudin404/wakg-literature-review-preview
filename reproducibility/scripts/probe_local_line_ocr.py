"""Standalone missing-ToUnicode short-line OCR/cache experiment; no production edits."""
import argparse
import collections
import hashlib
import html
import json
import os
import re
import time
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'internal_assets/local-line-ocr-probe-20260911'
PDF = next((ROOT/'internal_assets/review_test_corpus').rglob('*S0950061818307980*.pdf'), None)

def write(name, data):
    (OUT/name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')

def load(name):
    return json.loads((OUT/name).read_text(encoding='utf-8'))

def norm(s):
    return unicodedata.normalize('NFKC', s).replace('≧','≥').replace('≦','≤')

def prepare(doc=None, items=None):
    import fitz
    from font_annotation import inventory
    start=time.perf_counter()
    OUT.mkdir(parents=True,exist_ok=True)
    (OUT/'lines').mkdir(exist_ok=True)
    doc=doc if doc is not None else fitz.open(PDF or next((ROOT/'internal_assets/review_test_corpus').rglob('*S0950061818307980*.pdf')))
    items=items if items is not None else [i for i in inventory(doc) if i['reason']=='MISSING_TOUNICODE']
    fonts={i['font']:i for i in items}
    all_keys={i['font_sha256']+':'+str(o['gid']) for i in items for o in i['occurrences'] if not o['decoded'].isspace()}
    lines=[]
    for pn,page in enumerate(doc):
        traced={}
        for span in page.get_texttrace():
            item=fonts.get(span['font'].split('+')[-1])
            for code,gid,origin,bbox in span['chars']:
                traced[(round(origin[0],2),round(origin[1],2))]={
                    'key':item['font_sha256']+':'+str(gid) if item else None,
                    'decoded':chr(code),'bbox':list(bbox)}
        for block in page.get_text('rawdict')['blocks']:
            for line in block.get('lines',[]):
                chars=[]
                for span in line['spans']:
                    for c in span['chars']:
                        rec=traced.get((round(c['origin'][0],2),round(c['origin'][1],2)))
                        chars.append({'text':c['c'],'key':rec['key'] if rec else None,'bbox':list(c['bbox'])})
                keys={c['key'] for c in chars if c['key'] and not c['text'].isspace()}
                if keys:
                    lines.append({'page':pn+1,'bbox':list(line['bbox']),'chars':chars,'keys':sorted(keys)})
    remaining=set(all_keys)
    selected=[]
    while remaining:
        best=max(lines,key=lambda r:(len(set(r['keys']) & remaining),-len(r['chars'])))
        gained=set(best['keys']) & remaining
        if not gained:break
        selected.append(best)
        remaining-=gained
        lines.remove(best)
    for i,line in enumerate(selected):
        line['id']=i
        page=doc[line['page']-1]
        rect=fitz.Rect(line['bbox'])+(-3,-3,3,3)
        rect &= page.rect
        path=OUT/'lines'/f'{i:03d}.png'
        page.get_pixmap(matrix=fitz.Matrix(4,4),clip=rect,alpha=False).save(path)
        line['image']=str(path)
    report={'pdf':str(PDF),'font_count':len(items),'unique_glyphs':len(all_keys),
            'candidate_lines':len(lines)+len(selected),'selected_lines':len(selected),
            'uncovered_keys':sorted(remaining),'seconds':time.perf_counter()-start,'lines':selected}
    write('input.json',report)
    print(json.dumps({k:v for k,v in report.items() if k!='lines'}),flush=True)

def recognize():
    packet=load('input.json')
    if (OUT/'ocr.json').exists():
        print('OCR cache hit: no model loaded, no OCR calls',flush=True)
        return
    os.environ.setdefault('CUDA_VISIBLE_DEVICES','1')
    os.environ.setdefault('MODEL_CACHE_DIR',str(ROOT/'internal_assets/marker-font-probe-20260911/models'))
    import torch
    from PIL import Image
    from surya.foundation import FoundationPredictor
    from surya.recognition import RecognitionPredictor
    torch.set_num_threads(4)
    tick=time.perf_counter()
    model=RecognitionPredictor(FoundationPredictor())
    loaded=time.perf_counter()-tick
    images=[Image.open(line['image']).convert('RGB') for line in packet['lines']]
    boxes=[[[0,0,im.width,im.height]] for im in images]
    tick=time.perf_counter()
    results=model(images,bboxes=boxes,recognition_batch_size=32,math_mode=False)
    seconds=time.perf_counter()-tick
    write('ocr.json',{'model_load_seconds':loaded,'ocr_seconds':seconds,
                      'results':[r.model_dump() for r in results]})
    print(json.dumps({'images':len(images),'load_seconds':loaded,'ocr_seconds':seconds}),flush=True)

def align(a,b):
    # Minimum-edit alignment; unlike greedy matching, m->mu does not shift repeated letters.
    d=[[0]*(len(b)+1) for _ in range(len(a)+1)]
    for i in range(len(a)+1):d[i][0]=i
    for j in range(len(b)+1):d[0][j]=j
    for i in range(1,len(a)+1):
        for j in range(1,len(b)+1):
            d[i][j]=min(d[i-1][j-1]+(a[i-1]!=b[j-1]),d[i-1][j]+1,d[i][j-1]+1)
    i,j=len(a),len(b); pairs=[]
    while i or j:
        if i and j and d[i][j]==d[i-1][j-1]+(a[i-1]!=b[j-1]):
            pairs.append((i-1,j-1));i-=1;j-=1
        elif i and d[i][j]==d[i-1][j]+1:i-=1
        else:j-=1
    return pairs[::-1]

def make_labels():
    packet=load('input.json');ocr=load('ocr.json')
    votes=collections.defaultdict(collections.Counter); evidence=collections.defaultdict(list)
    for line,result in zip(packet['lines'],ocr['results']):
        text=' '.join(l['text'] for l in result['text_lines'])
        text=html.unescape(re.sub('<[^>]+>','',text))
        text=re.sub(r'\\frac\{([^{}]*)\}\{([^{}]*)\}',r'\1/\2',text)
        text=text.replace('\\times','×').replace('\\leq','≤').replace('\\geq','≥')
        text=re.sub(r'[_^{}]','',text)
        b=''.join(c for c in unicodedata.normalize('NFD',text) if not c.isspace())
        source=[];positions=[]
        for pos,c in enumerate(line['chars']):
            source_text=c['text']
            if any('\ufb00' <= ch <= '\ufb06' for ch in source_text):source_text=norm(source_text)
            if source_text=='´':source_text='\u0301'
            for ch in unicodedata.normalize('NFD',source_text):
                if not ch.isspace():source.append(ch);positions.append(pos)
        mapped=collections.defaultdict(list)
        for ai,bi in align(''.join(source),b):mapped[positions[ai]].append((ai,b[bi]))
        for pos,entries in mapped.items():
            c=line['chars'][pos]
            if not c['key']:continue
            label=''.join(v for _,v in sorted(entries))
            original=c['text']
            equivalent=''.join(ch for ch in unicodedata.normalize('NFKD',norm(original)) if not ch.isspace())
            if label==equivalent or (original in '‘’' and label=="'"):
                label=norm(original)
            votes[c['key']][label]+=1
            evidence[c['key']].append({'line':line['id'],'text':label,'ocr_text':text})
    labels={};conflicts=[]
    for key,counts in votes.items():
        ranked=counts.most_common()
        if len(ranked)>1:conflicts.append({'key':key,'votes':dict(counts)})
        if len(ranked)>1 and ranked[0][1]==ranked[1][1]:continue
        labels[key]={'text':ranked[0][0],'context_sensitive':False,'method':'surya-short-line',
                     'votes':dict(counts),'evidence':evidence[key]}
    write('labels.json',labels);write('conflicts.json',conflicts)
    return labels

def apply(doc,items,labels):
    from font_annotation import native_baseline,cached_corrections
    from pdf_symbol_text import unicode_cmap
    for item in items:
        mapping=native_baseline(doc,item)
        mapping.update(cached_corrections(doc,item,labels))
        xref=doc.get_new_xref();doc.update_object(xref,'<<>>')
        doc.update_stream(xref,unicode_cmap(mapping));doc.xref_set_key(item['xref'],'ToUnicode',f'{xref} 0 R')
    return '\n'.join(p.get_text() for p in doc)

def evaluate():
    import fitz
    from font_annotation import inventory
    from glyph_template_mapping import labels_for_fonts
    start=time.perf_counter();labels=make_labels(); mapping_seconds=time.perf_counter()-start
    doc=fitz.open(PDF);items=[i for i in inventory(doc) if i['reason']=='MISSING_TOUNICODE']
    fonts={i['font']:i for i in items}
    start=time.perf_counter()
    old,_=labels_for_fonts(doc,items,OUT/'old-template/labels.json')
    template_seconds=time.perf_counter()-start
    refs=json.loads((ROOT/'internal_assets/ocr-comparison-20260911/manifest.json').read_text(encoding='utf-8'))
    samples=json.loads((ROOT/'internal_assets/font-annotations/requests/2ea9c6b03ba12b5b976b/agent-c99f086ff123ae745adb/request.json').read_text(encoding='utf-8'))['samples']
    samples+=json.loads((ROOT/'internal_assets/glyph-probe-20260911/private-samples.json').read_text(encoding='utf-8'))['samples']
    rows=[]
    for sample,ref in zip(samples,refs):
        item=fonts.get(sample['font'])
        if not item:continue
        key=item['font_sha256']+':'+str(sample['gid'])
        ocr_label=labels.get(key,{}).get('text')
        template_label=old.get(key,{}).get('text')
        rows.append({'id':ref['id'],'key':key,'expected':ref['expected'],'ocr':ocr_label,'template':template_label,
                     'ocr_correct':ocr_label is not None and norm(ocr_label)==norm(ref['expected']),
                     'template_correct':template_label is not None and norm(template_label)==norm(ref['expected'])})
    texts=[];warm_times=[]
    for n in range(2):
        start=time.perf_counter();d=fitz.open(PDF)
        its=[i for i in inventory(d) if i['reason']=='MISSING_TOUNICODE']
        cached=load('labels.json');texts.append(apply(d,its,cached));warm_times.append(time.perf_counter()-start)
        (OUT/f'read-{n+1}.txt').write_text(texts[-1],encoding='utf-8')
    (OUT/'template-read.txt').write_text(apply(doc,items,old),encoding='utf-8')
    groups={}
    for prefix in ['B','G']:
        rs=[r for r in rows if r['id'].startswith(prefix)]
        groups[prefix]={'n':len(rs),'ocr_correct':sum(r['ocr_correct'] for r in rs),
                        'ocr_labeled':sum(r['ocr'] is not None for r in rs),
                        'template_correct':sum(r['template_correct'] for r in rs)}
    report={'groups':groups,'mapping_seconds':mapping_seconds,'template_cold_seconds':template_seconds,
            'cached_full_read_seconds':warm_times,'cache_read_equal':texts[0]==texts[1],
            'label_count':len(labels),'conflict_count':len(load('conflicts.json')),'rows':rows}
    write('comparison.json',report)
    print(json.dumps({k:v for k,v in report.items() if k!='rows'},ensure_ascii=False))

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('stage',choices=['prepare','recognize','evaluate'])
    parser.add_argument('--out',type=Path)
    args=parser.parse_args()
    if args.out:OUT=args.out
    PDF=next((ROOT/'internal_assets/review_test_corpus').rglob('*S0950061818307980*.pdf'))
    globals()[args.stage]()
