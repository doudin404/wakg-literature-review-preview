"""Source-PDF replay before pixel marker extraction; candidate output only."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import fitz
from PIL import Image,ImageDraw
import supplement_docx as extractor
from marker_implementation_identity import identity as marker_identity
from main_figure_context import bind_caption

ROOT=Path(__file__).resolve().parents[1]
def load(path):return json.loads(Path(path).read_text(encoding='utf-8'))
def sha(data):return hashlib.sha256(data).hexdigest()


def source_asset_path(run, relative):
    """Allow frozen project inputs and isolated run assets, not arbitrary paths."""
    run = Path(run).resolve()
    path = (run / relative).resolve()
    if not (path.is_relative_to(run) or path.is_relative_to(ROOT)):
        raise ValueError('main marker asset outside source project and isolated run')
    return path


def extract(run,binding):
    return extract_records(run,load(Path(run)/'generated-records.json'),binding)


def extract_records(run,records,binding):
    """Production entry point: consume current source assets, never prior results."""
    run=Path(run).resolve()
    if run.name!=binding['run_id']:raise ValueError('main marker run identity mismatch')
    matches=[a for a in records['assets'] if a['kind']=='main_figure' and a['extensions']['figure_number']==binding['figure_number']]
    if len(matches)!=1:raise ValueError('main marker figure asset missing or ambiguous')
    image=matches[0];meta=image['extensions']
    pdfs=[a for a in records['assets'] if a['asset_key']==meta['source_pdf_asset_key'] and a['kind']=='pdf']
    if len(pdfs)!=1:raise ValueError('main marker source PDF missing or ambiguous')
    pdf=pdfs[0];pdf_path=source_asset_path(run,pdf['relative_path'])
    raw_pdf=pdf_path.read_bytes()
    if sha(raw_pdf)!=pdf['sha256']:raise ValueError('main marker PDF hash mismatch')
    image_path=source_asset_path(run,image['relative_path'])
    raw=image_path.read_bytes()
    if sha(raw)!=image['sha256'] or image['sha256']!=binding['image_sha256']:
        raise ValueError('main marker image hash mismatch')
    chart=extractor.digitize_xy_markers(raw,binding)
    with fitz.open(stream=raw_pdf,filetype='pdf') as doc:
        page=doc[meta['page']-1]
        pix=page.get_pixmap(matrix=fitz.Matrix(2,2),clip=fitz.Rect(meta['source_region_bbox']),alpha=False)
        if pix.tobytes('png')!=raw:raise ValueError('main marker image does not replay from source PDF')
        origin=[pix.x,pix.y]
        context=bind_caption(page,meta['caption'],chart)
    for panel in chart['panels']:
        for series in panel['series']:
            for point in series['points']:
                x,y=point['pixel']
                point['source_pdf_bbox']=[(origin[0]+x-7)/2,(origin[1]+y-7)/2,
                                          (origin[0]+x+7)/2,(origin[1]+y+7)/2]
    return {'schema_version':1,'scope':'MAIN_FIGURE_MARKER_CANDIDATES_NOT_ACCEPTED',
        'run_id':run.name,'figure_number':binding['figure_number'],'source_pdf_sha256':pdf['sha256'],
        'source_image_sha256':image['sha256'],'page':meta['page'],'caption':meta['caption'],
        'source_region_bbox':meta['source_region_bbox'],'render_origin_px':origin,'render_scale':2,
        'binding_sha256':extractor.digest(binding),'recognizer_sha256':sha((ROOT/'scripts/marker_geometry.py').read_bytes()),
        'digitizer_sha256':marker_identity(),
        'extractor_sha256':sha(Path(__file__).read_bytes()),'digitization':chart,
        'context_parser_sha256':sha((ROOT/'scripts/main_figure_context.py').read_bytes()),
        'panel_context':context,
        'observation_count':sum(len(s['points']) for p in chart['panels'] for s in p['series']),
        'review_status':'pending_source_path_review','formal_values_allowed':False}


def overlay(run,result):
    records=load(Path(run)/'generated-records.json')
    asset=next(a for a in records['assets'] if a['sha256']==result['source_image_sha256'])
    raw=(Path(run)/asset['relative_path']).read_bytes()
    if sha(raw)!=result['source_image_sha256']:raise ValueError('overlay source changed')
    picture=Image.open(io.BytesIO(raw)).convert('RGB');draw=ImageDraw.Draw(picture)
    for panel in result['digitization']['panels']:
        for series in panel['series']:
            for point in series['points']:
                x,y=point['pixel'];draw.rectangle((x-7,y-7,x+7,y+7),outline='red',width=1)
    output=io.BytesIO();picture.save(output,format='PNG');return output.getvalue()


def artifact_receipt(records,run):
    """Verify persisted candidate bytes before resume or output validation."""
    subject=records.get('extensions',{}).get('main_marker_candidates')
    if subject is None:return []
    run=Path(run).resolve()
    path=(run/subject['path']).resolve();path.relative_to(run)
    raw=path.read_bytes()
    if sha(raw)!=subject['sha256']:raise ValueError('main marker candidate artifact hash mismatch')
    report=json.loads(raw)
    if (report['run_id']!=run.name or report['scope']!=subject['scope'] or
            report['observation_count']!=subject['observation_count'] or
            report['formal_values_allowed'] is not False or subject['formal_values_allowed'] is not False):
        raise ValueError('main marker candidate artifact scope mismatch')
    return [{'path':subject['path'],'sha256':subject['sha256']}]


if __name__=='__main__':
    p=argparse.ArgumentParser()
    for key in ('run','binding','write'):p.add_argument('--'+key,type=Path,required=True)
    p.add_argument('--overlay',type=Path);args=p.parse_args()
    result=extract(args.run,load(args.binding))
    extractor._atomic_bytes(args.write,json.dumps(result,sort_keys=True,ensure_ascii=False,indent=2).encode())
    if args.overlay:extractor._atomic_bytes(args.overlay,overlay(args.run,result))
    print(json.dumps({'observation_count':result['observation_count'],'scope':result['scope']}))
