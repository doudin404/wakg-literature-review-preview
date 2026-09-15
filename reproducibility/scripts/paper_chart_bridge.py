"""Paper draft -> chart reader -> existing records and reviewer source fields."""
import copy,csv,hashlib,io,json,os
from pathlib import Path
import fitz
import numpy as np
from source_quantity_producer import atomic_json,atomic_text
from selective_chart_planner import plan
from chart_readout import extract,final_reading
from psd_numeric import CUMULATIVE,percentiles
from paper_chart_context import catalogue, methods_context, resolve_owner, apply_task, figure_tasks
from paper_record_normalization import normalize as normalize_records
from pdf_thread_access import serialized_pdf

def digest(x):return hashlib.sha256(json.dumps(x,sort_keys=True,ensure_ascii=False).encode()).hexdigest()[:20]

def category_text(value):
    if isinstance(value,dict):
        return '; '.join(f'{key}: {category_text(part)}' for key,part in value.items())
    if isinstance(value,list):return '; '.join(map(category_text,value))
    return '' if value is None else str(value)

def explicit_chart_rows(items,owners):
    """Expand named categorical points and native-chart compound labels only."""
    labels={}
    for key,owner in owners.items():
        label=owner.get('custom_material_id') or owner.get('modules',{}).get('identity_source_specimen',{}).get('custom_test_id')
        labels.setdefault(label,[]).append(key)
    out=[]
    for original in items:
        item=copy.deepcopy(original)
        if item.get('native_chart_member') and not item.get('owner_key'):
            label=item.get('label');series=item.get('series','')
            suffix=series.split('=',1)[1].strip() if '=' in series else series
            keys=labels.get(f'{label}-{suffix}',[]) if suffix else labels.get(label,[])
            if len(keys)==1:item['owner_key']=keys[0]
            import re
            caption=item.get('property','')
            unit=re.search(r'\((wt\.%|%)\)',caption)
            age=re.search(r'\bat (\d+) days\b',caption)
            if unit:item.setdefault('unit',unit.group(1))
            if age:item.setdefault('age_seconds',int(age.group(1))*86400)
        points=item.get('points') or []
        from paper_chart_context import specimen_type
        specimen=specimen_type(item.get('specimen_type') or item.get('specimen'))
        def named_keys(label):
            qualified=labels.get(f'{label} {specimen}',[]) if specimen else []
            return qualified or labels.get(label,[])
        if item.get('unit') and points and all(isinstance(p[0],str) and len(named_keys(p[0]))==1 for p in points):
            for n,(label,value) in enumerate(points):
                row={k:v for k,v in item.items() if k not in ('points','pixel_points','result','values')}
                row.update(owner_key=named_keys(label)[0],value=value,category=label,
                    unit=item.get('unit') or item.get('y_axis',{}).get('unit'))
                if n<len(item.get('pixel_points',[])):
                    x,y=item['pixel_points'][n];row['bbox_px']=[x-3,y-3,x+3,y+3]
                out.append(row)
        else:out.append(item)
    return out

CURVE_BACKEND_PRIORITY={
    'agent':0, 'agent_bound_histogram':0,
    'scan_first':20, 'component_scan':20, 'colour_scan':20,
    'vector':30, 'vector_outline':30, 'WebPlotDigitizer':30,
}

def prefer_tool_curves(items):
    """Keep the strongest geometry source for the same identified curve."""
    selected={};ordinary=[]
    for order,item in enumerate(items):
        if not item.get('points'):
            ordinary.append(item);continue
        key=(item.get('owner_key'),item.get('kind'),item.get('property'),
             item.get('label') or item.get('series'),item.get('curve_type'))
        rank=CURVE_BACKEND_PRIORITY.get(item.get('backend'),10)
        current=selected.get(key)
        if current is None or (rank,len(item.get('points',[])),-order) > current[0]:
            selected[key]=((rank,len(item.get('points',[])),-order),item)
    return ordinary+[value[1] for value in selected.values()]

def put(record,path,value):
    parts=path.strip('/').split('/');node=record
    for key in parts[:-1]:
        if node.get(key) is None:node[key]={}
        node=node[key]
    old=node.get(parts[-1])
    if parts[-1] in {'x_axis','y_axis'} and isinstance(old,dict) and isinstance(value,dict):
        # Add measured calibration to the existing spectrum's axis template.
        # A second spectrum already has its own path selected by project().
        node[parts[-1]]={**old,**value}
        return
    if old is not None and old!=value:raise ValueError('existing value differs: '+path)
    node[parts[-1]]=value

def project(records,result,folder,source):
    """Keep the canonical ownership supplied by the semantic reader."""
    folder=Path(folder);owners={r.get('mat_key') or r.get('mix_key'):r for r in records['mats']+records['mixes']}
    review=[];problems=[];paper=records['papers'][0]['paper_key']
    def asset(path,kind):
        path=Path(path);sha=hashlib.sha256(path.read_bytes()).hexdigest();key='asset-chart-'+sha[:20]
        if not any(a['asset_key']==key for a in records['assets']):
            records['assets'].append(dict(asset_key=key,paper_key=paper,kind=kind,sha256=sha,
                relative_path=os.path.relpath(path,Path.cwd()),extensions={'source_id':source['source_id']}))
        return key
    imagekey=asset(result['source_image'],'main_figure')
    def attach(owner,path,value,item,formula=None):
        approximate=item.get('approximate',item.get('estimated',True))
        key='ev-chart-'+digest([source['source_id'],owner.get('mat_key') or owner.get('mix_key'),path,value])
        box=item.get('bbox_px') or item.get('plot_bbox_px')
        pdfbox=[v*72/150 for v in box] if box and source.get('page') else box
        link=dict(evidence_key=key,paper_key=paper,record_key=owner.get('mat_key') or owner.get('mix_key'),
            field_path=path,asset_key=source.get('asset_key',imagekey),page=source.get('page'),bbox=pdfbox,
            figure_number=source.get('text'),snippet=item.get('label') or item.get('series',''),
            extraction_method='chart_readout',extensions={'image_asset_key':imagekey,'approximate':approximate})
        if not any(e['evidence_key']==key for e in records['evidence_links']):records['evidence_links'].append(link)
        owner.setdefault('field_provenance',{})[path]=dict(evidence_key=key,original_value=value,
            original_unit=item.get('unit'),formula=formula or ('source figure coordinates -> calibrated axes' if approximate else None),extraction_method='chart_readout',review_status='pending',confidence=None)
        identity=owner.get('custom_material_id') or owner.get('modules',{}).get('identity_source_specimen',{}).get('custom_test_id')
        categories=[]
        for part in (item.get('category'),item.get('label') or item.get('series')):
            part=category_text(part)
            if part and part != identity and part not in categories:categories.append(part)
        review.append(dict(recordKey=link['record_key'],fieldPath=path,label=item.get('property') or item.get('kind'),
            value=value,unit=item.get('unit'),evidenceKey=key,approximate=approximate,reviewRegions=item.get('review_regions',[]),
            series=item.get('label') or item.get('series'),category=' · '.join(categories) or None,page=source.get('page')))
        display=dict(review[-1])
        if isinstance(value,str) and value.startswith('asset-chart-'):
            display.update(value=f"曲线点集（{len(item.get('points',[]))}点）",pointsAssetKey=value)
        owner.setdefault('extensions',{}).setdefault('chart_review_fields',[]).append(display)
    items=[]
    for item in result['results']:
        item=final_reading(item)
        if item.get('route')=='agent':
            payload=item.get('result') or {}
            items.extend([{**item,**v} for v in payload.get('values',[])])
            items.extend([{**item,**s} for s in payload.get('series',[])])
        elif item.get('values'):
            items.extend([{**item,**v} for v in item['values']])
        else:items.append(item)
    items=prefer_tool_curves(explicit_chart_rows(items,owners))
    psd_owners={x.get('owner_key') for x in items if x.get('kind')=='psd' and percentiles(x) and x.get('curve_type') in CUMULATIVE}
    for index,item in enumerate(items):
        try:
            kind=item['kind']
            prop=(item.get('property') or '').lower()
            if (kind.startswith(('sem','bse')) or 'micrograph' in kind) and any(term in prop for term in ('scale', 'magnification')):
                image_asset=next(a for a in records['assets'] if a['asset_key']==imagekey)
                image_asset.setdefault('extensions',{}).setdefault('image_calibration',[]).append(item)
                continue
            if kind in {'process_diagram','schematic','flowchart','histogram_fit','segmentation_image'} or (not item.get('owner_key') and (kind=='scatter' or kind.endswith('_setup'))):
                # Unlabelled correlation points and shared apparatus dimensions
                # describe the figure, not an identified specimen measurement.
                image_asset=next(a for a in records['assets'] if a['asset_key']==imagekey)
                image_asset.setdefault('extensions',{}).setdefault('figure_annotations',[]).append(item)
                continue
            if item.get('owner_key') not in owners:
                import re
                if re.search(r'spearman|pearson|kendall|correlation coefficient|相关系数',prop,re.I) and item.get('series') and item.get('category'):
                    image_asset=next(a for a in records['assets'] if a['asset_key']==imagekey)
                    image_asset.setdefault('extensions',{}).setdefault('figure_statistics',[]).append(
                        dict(item=item,source_id=source['source_id'],page=source.get('page'),
                             scope='variable_pair'))
                    continue
                # A figure-level statistic or unassigned peak is still useful data.
                # Preserve it with its source rather than invent a specimen owner.
                image_asset=next(a for a in records['assets'] if a['asset_key']==imagekey)
                image_asset.setdefault('extensions',{}).setdefault('unassigned_observations',[]).append(
                    dict(item=item,source_id=source['source_id'],page=source.get('page'),
                         reason='No matching material/specimen in saved reading'))
                problems.append(dict(source_id=source['source_id'],item=item,
                    reason='unassigned observation retained at figure',status='retained_unassigned'))
                continue
            owner=resolve_owner(item,owners)
            points=item.get('points')
            if points:
                name=f'curve-{index}.csv';stream=io.StringIO();w=csv.writer(stream)
                w.writerow(['x','y','pixel_x','pixel_y','gap_before'])
                pixels=item.get('pixel_points',[])
                for n,point in enumerate(points):
                    pixel=pixels[n] if n<len(pixels) else [None,None]
                    w.writerow([*point,*pixel,int(n in item.get('gap_before_indices',[]))])
                atomic_text(folder/name,stream.getvalue());data=asset(folder/name,'curve_points')
                curve=dict(schema_version='1.0',points_asset_key=data,source_image_asset_key=imagekey,
                    x_axis=item.get('x_axis'),y_axis=item.get('y_axis'),extensions=dict(approximate=True,
                    geometry_backend=item.get('backend'),
                    plot_bbox_px=item.get('plot_bbox_px'),
                    source_curve_type=item.get('curve_type'),
                    gap_before_indices=item.get('gap_before_indices',[]),review_regions=item.get('review_regions',[])))
                if 'mix_key' in owner:
                    rows=owner['modules'].setdefault('characterizations',[]);n=len(rows)
                    rows.append(dict(name=kind,value=None,unit=None,age_seconds=item.get('age_seconds'),
                        specimen=item.get('specimen'),method=item.get('method'),
                        extensions={'spectrum':curve,'series_label':item.get('label') or item.get('series'),
                                    'series_category':item.get('category')}))
                    path=f'/modules/characterizations/{n}/extensions/spectrum/points_asset_key'
                else:
                    path={'ftir':'/ftir_spectrum/extensions/reported_spectrum','xrd':'/xrd_qxrd/xrd_pattern',
                          'psd':'/particle_size_distribution','nmr':'/si29_nmr_spectrum' if item.get('isotope')=='29Si' else '/al27_nmr_spectrum' if item.get('isotope')=='27Al' else None}.get(kind)
                    if path is None:
                        path='/extensions/chart_spectra/'+digest([source['source_id'],index,kind])
                    if kind=='psd' and item.get('curve_type') not in CUMULATIVE:path+='/extensions/reported_volume_density'
                    node=owner
                    for part in path.strip('/').split('/'):
                        node=node.get(part) or {}
                    if node.get('points_asset_key') not in (None,data):
                        path+='/extensions/additional_spectra/'+digest([source['source_id'],index])
                    for k,v in curve.items():
                        if k=='extensions':
                            for ek,ev in v.items():put(owner,path+'/extensions/'+ek,ev)
                        else:put(owner,path+'/'+k,v)
                    path+='/points_asset_key'
                attach(owner,path,data,item)
                if 'mix_key' in owner and item.get('age_seconds') is not None:
                    age_item=dict(item,property='龄期',unit='s',approximate=False,
                        bbox_px=item.get('age_bbox_px') or item.get('plot_bbox_px'))
                    attach(owner,f'/modules/characterizations/{n}/age_seconds',item['age_seconds'],age_item)
                if kind=='psd' and item.get('curve_type') in CUMULATIVE:
                        for p,value in percentiles(item).items():
                            dest=f'/particle_size_distribution/d{p}_um'
                            existing=(owner.get('particle_size_distribution') or {}).get(f'd{p}_um')
                            if existing is not None:
                                dest=path.rsplit('/',1)[0]+f'/extensions/estimated_percentiles/d{p}_um'
                            put(owner,dest,value)
                            if existing is not None:
                                continue
                            attach(owner,dest,value,dict(item,unit='um',property=f'D{p}'),
                                   ('F(d) = 100 - R(d); interpolate cumulative finer in plotted axis space'
                                    if item.get('curve_type')=='cumulative_coarser' else 'interpolate cumulative curve in plotted axis space'))
            else:
                value=item.get('value')
                if value is None:value=item.get('y')
                if value is None:value=item.get('x')
                if value is None:continue
                prop=(item.get('property') or '').lower()
                import re
                if kind=='psd' and item.get('owner_key') in psd_owners and re.search(r'\bd(?:10|50|90)\b',prop+' '+str(item.get('category','')),re.I):
                    continue
                if 'mix_key' in owner:
                    module='characterizations' if kind in {'xrd','ftir','nmr','micrograph','sem','eds'} else 'performance'
                    rows=owner['modules'].setdefault(module,[]);base='/modules/'+module
                else:
                    rows=owner.setdefault('extensions',{}).setdefault('reported_observations',[])
                    base='/extensions/reported_observations'
                n=len(rows)
                rows.append(dict(name=item.get('property') or item.get('label') or item.get('series'),value=value,
                    unit=item.get('unit'),age_seconds=item.get('age_seconds'),specimen=item.get('specimen'),
                    method=item.get('method'),
                    extensions={'approximate':item.get('approximate',item.get('estimated',True)),
                                'series_category':item.get('category'),
                                'series_label':item.get('series') or item.get('label')}))
                attach(owner,f'{base}/{n}/value',value,item)
                if 'mix_key' in owner and item.get('age_seconds') is not None:
                    age_item=dict(item,property='龄期',unit='s',approximate=False,
                        bbox_px=item.get('age_bbox_px') or item.get('plot_bbox_px') or item.get('bbox_px'))
                    attach(owner,f'{base}/{n}/age_seconds',item['age_seconds'],age_item)
        except (KeyError,ValueError,TypeError) as e:
            problems.append(dict(source_id=source['source_id'],item=item,reason=str(e)))
    return review,problems

@serialized_pdf
def prepare_figure(source,packet,docs,output):
    folder=output/source['source_id'];folder.mkdir(exist_ok=True)
    docsha=source.get('document_sha256',packet['document']['sha256'])
    pdf=docs[docsha];image=folder/'page.png'
    if source.get('coordinate_space')=='docx_image':
        import zipfile
        from PIL import Image
        with zipfile.ZipFile(pdf) as archive:
            Image.open(io.BytesIO(archive.read(source['source_object']['member']))).convert('RGB').save(image)
        pdf=None
    else:
        pages=output/'pages';pages.mkdir(exist_ok=True);image=pages/f'{docsha}-{source["page"]}.png'
        if not image.exists():
            with fitz.open(pdf) as doc:doc[source['page']-1].get_pixmap(matrix=fitz.Matrix(150/72,150/72)).save(image)
    return folder,pdf,image

@serialized_pdf
def figure_pixel_box(source,pdf,image):
    if not pdf or source.get('coordinate_space')=='docx_image':return None
    from PIL import Image
    with fitz.open(pdf) as doc,Image.open(image) as im:
        rect=doc[source['page']-1].rect
        box=source.get('bbox') or source.get('source_object',{}).get('region_bbox')
        if not box:raise ValueError('Figure region is missing')
        return [v*(im.width/rect.width if i%2==0 else im.height/rect.height) for i,v in enumerate(box)]


def integrate(packet,draft,records,output,planner=plan,batch_planning=False,*,saved_only=False,readings=None,executor=None):
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    tasks={t['source_id']:t for t in records.get('extensions',{}).get('chart_review',{}).get('skipped',[])}
    reading_roots=[Path(x) for x in readings] if isinstance(readings,(list,tuple)) else [Path(readings or output)]
    reading_roots=[output]+[x for x in reading_roots if x!=output]
    for root in reversed(reading_roots):tasks.update(figure_tasks(draft,root))
    def saved_reading(source_id):
        return next((p for root in reading_roots if (p:=root/source_id/'readout/result.json').exists()),None)
    catalog=catalogue(records)
    methods=methods_context(packet)
    docs={packet['document']['sha256']:packet['document']['path'],**{s['sha256']:s['relative_path'] for s in packet.get('supplements',[]) if s.get('relative_path')}}
    for source in packet.get('figures',[]):
        if source.get('source_object',{}).get('content_kind')!='chart' or saved_reading(source['source_id']):continue
        import zipfile
        from supplement_docx import parse_native_chart
        from PIL import Image,ImageDraw
        with zipfile.ZipFile(docs[source['document_sha256']]) as archive:
            native=parse_native_chart(archive.read(source['source_object']['member']))
        folder=output/source['source_id']/'readout';folder.mkdir(parents=True,exist_ok=True)
        values=[];lines=['Native chart data: '+source['source_object']['member']]
        for series in native['series']:
            lines.append(series['name'])
            for label,value in zip(series['categories'],series['values']):
                matches=[o for o in catalog if o['label']==label]
                lines.append(f'{label}: {value}')
                values.append(dict(kind='bar',owner_key=matches[0]['owner_key'] if len(matches)==1 else None,
                    label=label,series=series['name'],property=source['text'],value=value,
                    approximate=False,native_chart_member=source['source_object']['member']))
        image=folder/'native-chart.png'
        rendered=Image.new('RGB',(1000,30*(len(lines)+1)),'white');draw=ImageDraw.Draw(rendered)
        for i,line in enumerate(lines):draw.text((15,15+30*i),line,fill='black')
        rendered.save(image)
        atomic_json(folder/'native-data.json',native)
        atomic_json(folder/'result.json',dict(source_image=str(image.resolve()),results=values))
    report=copy.deepcopy(records.get('extensions',{}).get('chart_review') or {})
    for key in ('processed','skipped','problems','review_fields'):report.setdefault(key,[])
    report['pending']=[]
    existing=set(report['processed'])
    prepared={};pending=[]
    for source in packet.get('figures',[]):
        task=tasks.get(source['source_id'],dict(kind='other',context='Read figure using existing study identities'))
        if task['kind']=='skip':continue
        if source['source_id'] in existing:continue
        if saved_reading(source['source_id']):continue
        if saved_only:
            report['pending'].append(source['source_id']);continue
        try:
            folder,pdf,image=prepare_figure(source,packet,docs,output)
            prepared[source['source_id']]=(folder,pdf,image)
            if not (folder/'planner/binding.json').exists():
                pending.append(dict(source_id=source['source_id'],folder=folder,pdf=pdf,image=image,
                                    page=source['page'],caption=source['text'],task=task,methods=methods,
                                    bbox_px=figure_pixel_box(source,pdf,image)))
        except Exception as e:report['problems'].append(dict(source_id=source['source_id'],reason=str(e)))
    planning={}
    if planner is plan and pending:
        from selective_chart_planner import plan_scan_batch as plan_batch
        from chart_image_input import groups
        for index,jobs in enumerate(groups(pending)):
            if executor:
                future=executor.submit(plan_batch,jobs,catalog,output/'input-batches'/str(index))
                for job in jobs:planning[job['source_id']]=future
            else:
                try:plan_batch(jobs,catalog,output/'input-batches'/str(index))
                except Exception as e:
                    for job in jobs:planning[job['source_id']]=e
    def read_figure(source,context):
        folder,pdf,image=prepared[source['source_id']]
        pending_plan=planning.get(source['source_id'])
        if isinstance(pending_plan,Exception):raise pending_plan
        if pending_plan is not None:pending_plan.result()
        bp=folder/'planner/binding.json'
        if planner is plan and not bp.exists():
            raise ValueError('batch planner did not return this figure')
        binding=json.loads(bp.read_bytes()) if bp.exists() else planner(image,pdf,source['page'],source['text'],folder/'planner',context=context)
        from selective_chart_planner import incomplete_curves
        problems=[dict(source_id=source['source_id'],reason=f'panel {i}: spectrum trajectory missing')
                  for i in incomplete_curves(binding['panels'])]
        result=extract(image,binding,folder/'readout')
        atomic_json(folder/'readout/result.json',result)
        return result,problems
    # Workers read independent figures; only this thread mutates paper records.
    contexts={s['source_id']:dict(task=tasks.get(s['source_id'],dict(kind='other',context='Read figure using existing study identities')),
        owners=catalogue(records,tasks.get(s['source_id'],{}),draft),methods=methods)
        for s in packet.get('figures',[]) if s['source_id'] in prepared}
    futures={s['source_id']:executor.submit(read_figure,s,contexts[s['source_id']])
             for s in packet.get('figures',[]) if s['source_id'] in prepared} if executor else {}
    for source in packet.get('figures',[]):
        task=tasks.get(source['source_id'],dict(kind='other',context='Read figure using existing study identities'))
        if task['kind']=='skip':
            if task not in report['skipped']:report['skipped'].append(task)
            continue
        if source['source_id'] in existing:continue
        saved=saved_reading(source['source_id']) if source['source_id'] not in futures else None
        if saved:
            source=dict(source,asset_key='asset-'+source.get('document_sha256',packet['document']['sha256'])[:12])
            destination=output/source['source_id']/'readout';destination.mkdir(parents=True,exist_ok=True)
            fields,problems=project(records,apply_task(json.loads(saved.read_bytes()),task),destination,source)
            report['review_fields']+=fields;report['problems']+=problems;report['processed'].append(source['source_id'])
            continue
        if source['source_id'] not in prepared:continue
        folder,pdf,image=prepared[source['source_id']]
        try:
            result,problems=futures[source['source_id']].result() if executor else read_figure(source,contexts[source['source_id']])
            report['problems']+=problems
            source=dict(source,asset_key='asset-'+source.get('document_sha256',packet['document']['sha256'])[:12])
            fields,problems=project(records,apply_task(result,task),folder/'readout',source)
            report['review_fields']+=fields;report['problems']+=problems;report['processed'].append(source['source_id'])
        except Exception as e:report['problems'].append(dict(source_id=source['source_id'],reason=str(e)))
    records.setdefault('extensions',{})['chart_review']=report
    normalize_records(records)
    atomic_json(output/'integration.json',report)
    return report
