"""Complete text/section/object inventory for planning; no scientific extraction."""
import argparse
import copy
import hashlib
import json
import re
from pathlib import Path
import fitz
from multimodal_extraction_plan import _main_caption_items,_section_inventory,_supplement_items
from source_quantity_producer import atomic_json
from source_specimen_variants import digest
from pdf_symbol_text import repair_symbol_encoding

VERSION='full-paper-source-index-v1'


def attach_indexed_assets(index,records):
    """Carry verified acquired sources across the indexing/extraction boundary."""
    result=copy.deepcopy(records)
    owners={a['paper_key'] for a in records.get('assets',[]) if a.get('paper_key') and a.get('sha256')==index['document'].get('sha256')}
    for item in index.get('supplements',[]):
        if item.get('status') in ('NOT_ACQUIRED','HASH_MISMATCH','ACQUIRED_UNPARSED'):continue
        if any(a.get('sha256')==item['sha256'] for a in result['assets']):continue
        if len(owners)!=1:raise ValueError('supplement paper owner is not unique')
        path=Path(item['relative_path'])
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=item['sha256']:
            raise ValueError('indexed supplement content unavailable or changed')
        result['assets'].append({'asset_key':'asset-supplement-'+item['sha256'][:24],
            'paper_key':next(iter(owners)),'kind':'pdf' if path.suffix.lower()=='.pdf' else 'supplement',
            'relative_path':str(path),'sha256':item['sha256'],
            'extensions':{'document_role':'supplement','label':item.get('label'),'source_url':item.get('url')}})
    return result


def index_attachment(item):
    """Inventory acquired native tables without inventing PDF coordinates."""
    attachment={k:item.get(k) for k in ('label','url','relative_path','sha256')}
    file=Path(item['relative_path']) if item.get('relative_path') else None
    sources=[]
    if not file or not file.is_file():attachment['status']='NOT_ACQUIRED'
    elif hashlib.sha256(file.read_bytes()).hexdigest()!=item.get('sha256'):attachment['status']='HASH_MISMATCH'
    elif file.suffix.lower()=='.pdf':
        child=build(file,[]);attachment.update(status='INDEXED',index_sha256=child['index_sha256'],pages=child['pages'])
        sources=[{**s,'document_role':'supplement'} for s in child['sources']]
    elif file.suffix.lower()=='.docx':
        from supplement_docx import inspect_docx
        inventory=inspect_docx(file)
        for table in inventory['tables']:
            source={'kind':'table','document_sha256':item['sha256'],'document_role':'supplement',
                    'coordinate_space':'docx_table','page':None,'bbox':None,
                    'text':table['caption']+'\n'+'\n'.join('\t'.join(row) for row in table['rows']),
                    'source_object':{**table,'source_label':table['label']}}
            source['source_id']='src-'+digest(source)[:20];sources.append(source)
        for paragraph in inventory.get('paragraphs',[]):
            source={'kind':'text','document_sha256':item['sha256'],'document_role':'supplement',
                    'coordinate_space':'docx_paragraph','page':None,'bbox':None,
                    'text':paragraph['text'],'source_object':paragraph}
            source['source_id']='src-'+digest(source)[:20];sources.append(source)
        attachment.update(status='TEXT_TABLES_INDEXED',
                          inventory_sha256=inventory['inventory_sha256'],table_count=len(inventory['tables']),
                          text_count=len(inventory.get('paragraphs',[])),
                          figure_count=len(inventory['figures']))
        for figure in inventory['figures']:
            source=dict(kind='figure',document_sha256=item['sha256'],document_role='supplement',
                page=None,bbox=None,text=figure.get('caption',''),source_object=figure,coordinate_space='docx_image')
            source['source_id']='src-'+digest(source)[:20];sources.append(source)
    else:attachment['status']='ACQUIRED_UNPARSED'
    return attachment,sources


def section_directory(sources):
    """Index short heading blocks without splitting at the outline's first dot."""
    result=[]
    for s in sources:
        if s['kind']!='text':continue
        text=s['text'].splitlines()[0].strip()
        if len(text)>80:continue
        if re.match(r'^\d+\.(?:\d+\.?)*\s+[A-Za-z]',text) or text.casefold() in {'references','acknowledgements','data availability','appendix a. supplementary data'}:
            result.append({'source_id':s['source_id'],'page':s['page'],'bbox':s['bbox'],'text':text,'status':'HEADING_CANDIDATE'})
    return result


def build(pdf_path,supplements=None):
    path=Path(pdf_path).resolve();raw=path.read_bytes();sha=hashlib.sha256(raw).hexdigest()
    sources=[];pages=[]
    with fitz.open(stream=raw,filetype='pdf') as doc:
        repair_symbol_encoding(doc)
        font_annotation = doc.font_annotation
        sections=_section_inventory(doc)
        for pn,page in enumerate(doc,1):
            ids=[]
            for b in page.get_text('blocks',sort=False):
                if len(b)>6 and b[6]!=0:continue
                text=b[4].strip()
                if not text:continue
                item={'kind':'text','document_sha256':sha,'page':pn,'bbox':list(b[:4]),'text':text}
                item['source_id']='src-'+digest(item)[:20];sources.append(item);ids.append(item['source_id'])
            pages.append({'page':pn,'source_ids':ids,'native_text_characters':len(page.get_text()),
                          'text_status':'INDEXED' if ids else 'NO_TEXT_LAYER','embedded_image_count':len(page.get_images())})
        for item in _main_caption_items(doc):
            kind='table' if item['kind']=='MAIN_TABLE' else 'figure'
            source={'kind':kind,'document_sha256':sha,'page':item['page'],
                    'bbox':item.get('region_bbox') or item.get('bbox'),'text':item.get('caption') or item.get('source_label',''),
                    'source_object':item}
            source['source_id']='src-'+digest(source)[:20];sources.append(source)
        references=[r for pn,page in enumerate(doc,1) for r in _supplement_items(page,pn,page.get_text())]
    attachments=[]
    for item in supplements or []:
        attachment,child_sources=index_attachment(item)
        attachments.append(attachment);sources.extend(child_sources)
    result={'version':VERSION,'font_annotation':font_annotation,'document':{'path':str(path),'sha256':sha,'page_count':len(pages)},
            'pages':pages,'sections':section_directory(sources),'sources':sources,'supplement_references':references,
            'supplements':attachments,'supplement_discovery_status':'REFERENCES_REQUIRE_RESOLUTION' if references else 'NO_REFERENCE_DETECTED_NOT_PROOF_OF_ABSENCE',
            'coverage':{'pages_indexed':len(pages),'text_blocks':sum(s['kind']=='text' for s in sources),
                        'text_truncated':False,'unreadable_pages':[p['page'] for p in pages if p['text_status']!='INDEXED']}}
    result['index_sha256']=digest(result);return result


def compact(index,max_bytes=150000):
    """Return every text block. Over-limit papers require explicit segmentation."""
    result={k:index[k] for k in ('version','document','sections','coverage','supplement_references','supplements','index_sha256')}
    result['source_document_default_sha256']=index['document']['sha256']
    result['sources']=[{**{k:s[k] for k in ('source_id','kind','page','text')},
                        **({'document_sha256':s['document_sha256']} if s['document_sha256']!=index['document']['sha256'] else {}),
                        'label':s.get('source_object',{}).get('source_label')} for s in index['sources']]
    result['pages']=[{'page':p['page'],'text_status':p['text_status'],'embedded_image_count':p['embedded_image_count']} for p in index['pages']]
    size=len(json.dumps(result,ensure_ascii=False).encode())
    if size>max_bytes:
        raise ValueError(f'FULLTEXT_SEGMENTATION_REQUIRED: {size} bytes exceed {max_bytes}; no truncation or model call')
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--pdf',required=True);p.add_argument('--output',required=True)
    a=p.parse_args();result=build(a.pdf);atomic_json(Path(a.output),result)
    print(json.dumps(result['coverage']))
