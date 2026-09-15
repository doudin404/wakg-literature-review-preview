"""Update one paper in the original review surface with current observations."""
import argparse, json, re, shutil, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts'))
from source_quantity_producer import atomic_json

def update(run,site):
    run,site=Path(run),Path(site)
    qpath=site/'review-assets/queue.json'
    queue=json.loads(qpath.read_bytes())
    data=json.loads((run/'generated-records.json').read_bytes())
    cards=json.loads((run/'review-cards.json').read_bytes())
    curves=json.loads((run/'curves.json').read_bytes())
    p=next(p for p in queue['papers'] if p.get('doi')==data['papers'][0]['doi'])
    # Replace the current paper; earlier generations remain in version history.
    templates=queue['sourceExplanationContract']['templates']
    pages={pg['number']:pg for pg in p['pages']}
    bykey={e['evidence_key']:e for e in data['evidence_links']}
    # Embedded supplement plots have their own pixel coordinate space.
    # Expose that original image as a source page rather than guessing a DOCX page.
    image_assets={a['asset_key']:a for a in data['assets']}
    for key,ev in list(bykey.items()):
        imagekey=(ev.get('extensions') or {}).get('image_asset_key')
        if ev.get('page') is not None or not imagekey:
            continue
        asset=image_assets[imagekey]
        pg=next((x for x in p['pages'] if x.get('nativeImageAsset')==imagekey),None)
        if pg is None:
            from PIL import Image
            source=ROOT/asset['relative_path']
            destination=site/'review-assets/resume-curves'/('source-'+imagekey+'.png')
            destination.parent.mkdir(exist_ok=True)
            shutil.copy2(source,destination)
            with Image.open(source) as im:width,height=im.size
            pg=dict(number=max(pages)+1,width=width,height=height,imageUrl='review-assets/resume-curves/'+destination.name,
                    sourceKind='supplement',sourcePage=None,nativeImageAsset=imagekey,evidence=[])
            p['pages'].append(pg);pages[pg['number']]=pg
        bykey[key]={**ev,'page':pg['number']}
    p['pageCount']=len(p['pages'])
    # The queue contains only the current projection, including current boxes.
    for pg in p['pages']:pg['evidence']=[]
    pdfs={}
    # DOCX supplements already have published rendered pages. Reuse their
    # cached PDF to bind table-cell addresses to those same page coordinates.
    supplemental=[e for e in bykey.values() if e.get('page') is None]
    if supplemental:
        import fitz
        from prepare_review_bundle import _supplement_table_locators
        supp_pages=[pg for pg in p['pages'] if '/supplement-pages/' in pg['imageUrl']]
        offset=min(pg['number'] for pg in supp_pages)-1
        groups={}
        for ev in supplemental:
            loc=ev.get('source_locator') or {}
            identity=(ev.get('asset_key'),ev.get('table_number'),loc.get('row'),loc.get('column'),ev.get('snippet'))
            groups.setdefault(identity,[]).append(ev)
        for asset in data['assets']:
            cache=ROOT/'internal_assets/supplement-render-cache'/str(asset.get('sha256'))/'supplement.pdf'
            sources=[items[0] for identity,items in groups.items() if identity[0]==asset['asset_key']]
            if not sources or not cache.exists():continue
            with fitz.open(cache) as document:
                located=_supplement_table_locators(document,sources,offset)
                for ev in sources:
                    loc=ev.get('source_locator') or {}
                    if loc.get('coordinate_space')!='docx_paragraph':continue
                    hits=[(pg.number,hit) for pg in document for hit in pg.search_for(ev['snippet'])]
                    if len(hits)==1:
                        number,box=hits[0]
                        located[ev['evidence_key']]={'page':offset+number+1,'bbox':list(box),'snippet':ev['snippet']}
                retry=[]
                for ev in sources:
                    token=(ev.get('extensions',{}).get('source_ref') or {}).get('token')
                    number=re.fullmatch(r'([+-]?\d+(?:\.\d+)?(?:[×x]10[+-]?\d+)?)\s+[A-Za-zµμ%].*',ev.get('snippet',''))
                    if number:token=number.group(1)
                    if ev['evidence_key'] not in located and token and token in ev.get('snippet',''):
                        retry.append({**ev,'snippet':token})
                if retry:
                    second=_supplement_table_locators(document,[*sources,*retry],offset)
                    for ev in retry:
                        if ev['evidence_key'] in second:located[ev['evidence_key']]=second[ev['evidence_key']]
            for items in groups.values():
                hit=located.get(items[0]['evidence_key'])
                if hit:
                    for ev in items:bykey[ev['evidence_key']]={**ev,**hit}
    added=[];omitted=[]
    for kind in ('mats','mixes'):
        owners={r.get('mat_key') or r.get('mix_key'):r for r in data[kind]}
        for card in cards[kind]:
            owner=owners[card['key']];fields=[]
            for f in card['fields']:
                # These are empty UI placeholders, not extracted observations.
                if f['sourceExplanationCode']=='SOURCE_LOCATED_NOT_TRANSCRIBED' and f['value'] is None:
                    omitted.append([card['key'],f['label']]);continue
                ev=bykey.get(f.get('evidenceKey'));path=f.get('_fieldPath')
                if not ev and not path:
                    matches=[i for i,row in enumerate((owner.get('xrf_composition') or {}).get('rows',[])) if row.get('component')==f['label']]
                    if len(matches)>1:
                        matches=[i for i in matches if str(owner['xrf_composition']['rows'][i].get('normalized_value'))==str(f.get('value'))]
                    if len(matches)==1:path=f'/xrf_composition/rows/{matches[0]}/normalized_value'
                if not ev and path:ev=bykey.get(owner.get('field_provenance',{}).get(path,{}).get('evidence_key'))
                evidence=[ev] if ev else []
                if f['label']=='养护条件' and f['value']:
                    evidence=[bykey[v['evidence_key']] for k,v in owner['field_provenance'].items() if '/curing_stages/' in k and v.get('evidence_key') in bykey]
                    f['sourceExplanationCode']='SOURCE_COMPOSITE_VALUE'
                if evidence:
                    key='resume-'+(f.get('evidenceKey') or evidence[0]['evidence_key'])
                    f['evidenceKey']=key;f['evidenceBlocked']=False
                    if f['sourceExplanationCode']=='SOURCE_EVIDENCE_BLOCKED':f['sourceExplanationCode']='DIRECT_SOURCE_VALUE'
                    f['status']='reported';f['statusLabel']='原文有值';f['reason']=None
                    for ev in evidence:
                        if ev.get('page') not in pages:
                            raise ValueError(f"Source page unresolved: {card['key']} {f['label']} {ev}")
                        locations=ev.get('locations') or [{'page':ev['page'],'bbox':b,'snippet':ev.get('snippet','')} for b in (ev.get('bboxes') or [ev['bbox']])]
                        if f['label']=='养护条件':
                            import fitz
                            quote=(ev.get('extensions',{}).get('source_ref') or {}).get('quote')
                            asset=next((a for a in data['assets'] if a['asset_key']==ev.get('asset_key') and a['kind']=='pdf'),None)
                            if quote and asset:
                                if asset['asset_key'] not in pdfs:pdfs[asset['asset_key']]=fitz.open(ROOT/asset['relative_path'])
                                hits=pdfs[asset['asset_key']][ev['page']-1].search_for(quote)
                                if hits:locations=[{'page':ev['page'],'bbox':list(b),'snippet':quote} for b in hits]
                        for loc in locations:
                            if not loc.get('bbox'):
                                f['evidenceBlocked']=True
                                f['sourceExplanationCode']='SOURCE_EVIDENCE_BLOCKED'
                                continue
                            number=loc.get('page') or ev['page'];pg=pages[number];x0,y0,x1,y1=loc['bbox']
                            entry=dict(evidenceKey=key,recordKey=card['key'],page=number,bbox=[max(0,x0-2),max(0,y0-0.5),min(pg['width'],x1+2),min(pg['height'],y1+0.5)],snippet=loc.get('snippet',ev.get('snippet','')),locator=f"第 {number} 页")
                            if entry not in pg['evidence']:pg['evidence'].append(entry)
                            added.append(entry)
                if f.get('evidenceBlocked'):f['evidenceKey']=None
                f['sourceExplanation']=templates[f['sourceExplanationCode']]
                fields.append(f)
            card['fields']=fields
    for pg in p['pages']:
        merged=[]
        for entry in sorted(pg['evidence'],key=lambda e:(e['evidenceKey'],e['bbox'][1],e['bbox'][0])):
            a=entry['bbox']
            existing=next((e for e in merged if e['evidenceKey']==entry['evidenceKey']
                and abs((e['bbox'][1]+e['bbox'][3])-(a[1]+a[3]))<4
                and e['bbox'][0]<=a[2] and a[0]<=e['bbox'][2]),None)
            if existing:
                b=existing['bbox'];existing['bbox']=[min(a[0],b[0]),min(a[1],b[1]),max(a[2],b[2]),max(a[3],b[3])]
            else:merged.append(entry)
        pg['evidence']=merged
    dest=site/'review-assets/resume-curves';dest.mkdir(exist_ok=True)
    for curve in curves.values():
        for key in ('pointsUrl','sourceImageUrl'):
            source=run/curve[key];shutil.copy2(source,dest/source.name)
            curve[key]='review-assets/resume-curves/'+source.name
    p['records']=cards;p['curves']=curves;p['latestExtractionLabel']='本次重跑结果'
    for document in pdfs.values():document.close()
    queue['previewVersion']='native-resume-20260914';queue['formalAcceptance']=False
    queue.pop('preHumanAgentReview',None)
    from normalize_preview import normalize
    queue=normalize(queue)
    from material_type_display import apply as apply_material_display
    apply_material_display(queue)
    atomic_json(qpath,queue)
    # Use the maintained renderer; add the existing preview adapter at its load boundary.
    js=(ROOT/'review-ui/public/review-full.js').read_text(encoding='utf8')
    needle="then(({queue,queueSha256})=>{"
    js=js.replace(needle,needle+"state.queueSha256=queueSha256;if(window.WakgDevelopment?.accept(queue))return;")
    (site/'review-full.js').write_text(js,encoding='utf8')
    for source,target in [('restored-preview.js','development-preview.js'),('native-preview.css','native-preview.css')]:
        shutil.copy2(ROOT/'review-ui/public'/source,site/target)
    index=(site/'index.html').read_text(encoding='utf8')
    if 'native-preview.css' not in index:
        index=index.replace('</head>','  <link rel="stylesheet" href="native-preview.css?v=resume-20260914">\n</head>')
    index=re.sub(r'((?:native-preview\.css|development-preview\.js|review-full\.js)\?v=)[^"\s]+',r'\1preview-20260915-terms1',index)
    (site/'index.html').write_text(index,encoding='utf8')
    from clean_review_surface import clean
    clean(site)
    from material_type_display import finalize as finalize_material_display
    finalize_material_display(site)
    report=dict(papers=len(queue['papers']),mats=len(cards['mats']),mixes=len(cards['mixes']),fields=sum(len(c['fields']) for g in cards.values() for c in g),curves=len(curves),omittedEmptyPlaceholders=omitted,historyDisplayed=False)
    atomic_json(run/'web-projection-report.json',report)
    print(json.dumps(report,ensure_ascii=False))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('run',type=Path);p.add_argument('site',type=Path);a=p.parse_args();update(a.run,a.site)
