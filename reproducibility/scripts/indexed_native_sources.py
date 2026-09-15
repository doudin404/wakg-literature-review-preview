"""Native PDF and DOCX text locations shared by curing and scalar performance."""
import copy
import hashlib
import re
from pathlib import Path
import fitz
from source_specimen_variants import digest


def assets_for(records,index,source_ids):
    wanted={s['document_sha256'] for s in index['sources'] if s['source_id'] in source_ids}
    paper_keys={m['paper_key'] for m in records['mixes']}
    assets=[]
    for sha in sorted(wanted):
        matches=[a for a in records['assets'] if a.get('sha256')==sha and a.get('paper_key') in paper_keys]
        if len(matches)!=1:raise ValueError('planned native source asset missing or ambiguous')
        asset=matches[0]
        if hashlib.sha256(Path(asset['relative_path']).read_bytes()).hexdigest()!=sha:
            raise ValueError('planned native source changed')
        assets.append(asset)
    return assets


def native_blocks(index,records,source_ids):
    from source_table_cells import native_matrix
    assets=assets_for(records,index,source_ids);bysha={a['sha256']:a for a in assets}
    blocks={};seen=set();pdfs={}
    try:
        for source in sorted((s for s in index['sources'] if s['source_id'] in source_ids),key=lambda s:s['source_id']):
            asset=bysha[source['document_sha256']];sha=asset['sha256']
            def add(group,word):
                word={**word,'asset_key':asset['asset_key'],'document_sha256':sha}
                key=digest(word)
                if key in seen:return
                seen.add(key);block_id='native-'+digest([sha,group])[:16]
                blocks.setdefault(block_id,{'block_id':block_id,'locators':[]})['locators'].append(word)
            if source.get('coordinate_space')=='docx_paragraph':
                body=source['source_object']['body_index']
                for match in re.finditer(r'\S+',source['text']):
                    add(('paragraph',body),{'page':None,'bbox':None,'token':match.group(),
                        'source_locator':{'coordinate_space':'docx_paragraph','body_index':body,
                                          'start':match.start(),'end':match.end()}})
            elif source.get('coordinate_space')=='docx_table':
                table=native_matrix(index,source,index['sources'],assets)
                for row in table['rows']:
                    for cell in row:
                        loc=cell['source_locator'];origin={k:v for k,v in loc.items() if k not in ('display_row','display_column','inherited')}
                        for match in re.finditer(r'\S+',cell['text']):
                            add(('table',loc['table_number'],loc['row']),{'page':None,'bbox':None,'token':match.group(),
                                'source_locator':{**origin,'start':match.start(),'end':match.end()}})
            else:
                if sha not in pdfs:pdfs[sha]=fitz.open(asset['relative_path'])
                page=pdfs[sha][source['page']-1];box=fitz.Rect(source['bbox'])
                if source['kind']=='table':box|=fitz.Rect(native_matrix(index,source,index['sources'],assets)['bbox'])
                for w in page.get_text('words'):
                    if box.contains(fitz.Point((w[0]+w[2])/2,(w[1]+w[3])/2)):
                        add(('pdf',source['page'],w[5]),{'page':source['page'],'bbox':list(w[:4]),'token':w[4],
                            'source_locator':{'coordinate_space':'pdf_points'}})
        result=[]
        for block in blocks.values():
            def order(word):
                loc=word['source_locator']
                if loc['coordinate_space']=='docx_paragraph':return (loc['start'],)
                if loc['coordinate_space']=='docx_table':return (loc['column'],loc['start'])
                return (round(word['bbox'][1],1),word['bbox'][0])
            block['locators'].sort(key=order);result.append(block)
        return sorted(result,key=lambda b:b['block_id']),assets
    finally:
        for doc in pdfs.values():doc.close()


def extend_request(req,index,records,source_ids):
    from source_design_relations import index_spans,source_number
    blocks,assets=native_blocks(index,records,source_ids)
    if not blocks:raise ValueError('planned native text empty')
    tokens=[]
    for block in blocks:
        for i,word in enumerate(block['locators']):
            value=source_number(word['token'])
            if value is not None:
                tokens.append({'token_id':'n-'+digest(word)[:16],'value':value,'block_id':block['block_id'],
                               'word_index':i,'locator':word})
    result=copy.deepcopy(req)
    result.update(blocks=blocks,spans=index_spans(blocks,req['pdf_asset']['sha256']),tokens=tokens,
                  source_assets=assets,records_sha256=digest(records))
    result['request_sha256']=digest({k:v for k,v in result.items() if k!='request_sha256'})
    return result


def docx_text(asset,locator):
    from supplement_docx import inspect_docx
    inventory=inspect_docx(Path(asset['relative_path']))
    if locator['coordinate_space']=='docx_paragraph':
        return next(p['text'] for p in inventory['paragraphs'] if p['body_index']==locator['body_index'])
    table=next(t for t in inventory['tables'] if t['label']==locator['table_number'])
    return table['rows'][locator['row']][locator.get('cell',locator['column'])]


def readable_native_word(records,word):
    matches=[a for a in records['assets'] if a['asset_key']==word['asset_key']]
    if len(matches)!=1:raise ValueError('native word source missing')
    asset=matches[0]
    if asset['sha256']!=word['document_sha256']:raise ValueError('native word source identity differs')
    if word['source_locator']['coordinate_space']=='pdf_points':
        from source_specimen_variants import readable_word
        return readable_word(records,{'pdf_asset_key':asset['asset_key'],'pdf_sha256':asset['sha256']},word)
    raw=Path(asset['relative_path']).read_bytes()
    if hashlib.sha256(raw).hexdigest()!=asset['sha256']:raise ValueError('native DOCX source changed')
    loc=word['source_locator'];text=docx_text(asset,loc)
    if text[loc['start']:loc['end']]!=word['token']:raise ValueError('native DOCX token differs from source')
    return copy.deepcopy(word)


def evidence_location(words,default_asset):
    """Primary location plus complete support tokens retained by the caller."""
    first=words[0];asset=first.get('asset_key',default_asset);loc=first.get('source_locator',{'coordinate_space':'pdf_points'})
    same=[w for w in words if w.get('asset_key',default_asset)==asset and w['page']==first['page']]
    if loc['coordinate_space']=='pdf_points':
        same=[w for w in same if w.get('bbox')]
        box=list(fitz.Rect(same[0]['bbox']))
        for w in same[1:]:box=list(fitz.Rect(box)|fitz.Rect(w['bbox']))
        return {'asset_key':asset,'page':first['page'],'bbox':box,'snippet':' '.join(w['token'] for w in same),
                'source_locator':copy.deepcopy(loc)}
    identity={k:v for k,v in loc.items() if k not in ('start','end')}
    same=[w for w in same if {k:v for k,v in w['source_locator'].items() if k not in ('start','end')}==identity]
    return {'asset_key':asset,'page':None,'bbox':None,'snippet':' '.join(w['token'] for w in same),
            'source_locator':{**identity,'start':min(w['source_locator']['start'] for w in same),
                              'end':max(w['source_locator']['end'] for w in same)}}
