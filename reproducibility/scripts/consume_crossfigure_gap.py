"""Consume a frozen visual gap response into the same pending science chain."""
import argparse
import copy
import hashlib
import json
import re
from pathlib import Path
import numpy as np
from PIL import Image
from jsonschema import validate
from crossfigure_visual_gap import schema
from crossfigure_structure import relocate_ticks,plot_frame,complete_label_region,join_words
from planned_xrd_curves import bounds
from source_specimen_variants import digest
from source_quantity_producer import atomic_json

VERSION='consume-crossfigure-gap-v1'

def compile_geometry(index,plan,directory):
    """Derived geometry index; preserve original planning bytes and source identities."""
    directory=Path(directory);req=json.loads((directory/'visual-gap/request.json').read_bytes())
    refined=copy.deepcopy(index);compiled=copy.deepcopy(plan);sources={s['source_id']:s for s in refined['sources']}
    for im in req['images']:
        source=sources[im['source_id']]
        source['source_object']['caption_page']=source['source_object'].get('caption_page',source['page'])
        source['page']=im['page'];source['bbox']=im['parent_image_pdf_bbox']
    refined['parent_index_sha256']=index['index_sha256'];refined['index_sha256']=digest({k:v for k,v in refined.items() if k!='index_sha256'})
    compiled['parent_plan_sha256']=plan['plan_sha256'];compiled['index_sha256']=refined['index_sha256'];compiled['plan_sha256']=digest({k:v for k,v in compiled.items() if k!='plan_sha256'})
    atomic_json(directory/'compiled-source-index.json',refined);atomic_json(directory/'compiled-plan.json',compiled)
    return refined,compiled

def normal(text):return re.sub(r'[^a-z0-9<>]','',text.lower().replace('μ','u').replace('µ','u'))

def scalar(text,unit):
    text=re.sub(r'^(?:Mag|EHT)\s*[=:]?\s*','',text,flags=re.I).strip()
    if unit=='x':
        m=re.fullmatch(r'(\d+(?:\.\d+)?)\s*([Kk]?)\s*[Xx×]',text)
        if m:return float(m[1])*(1000 if m[2] else 1)
    suffix={'um':r'[μµu]m','kV':'kV','%':'%'}.get(unit,re.escape(str(unit)))
    m=re.fullmatch(r'(\d+(?:\.\d+)?)\s*'+suffix,text,flags=re.I)
    if not m:raise ValueError('unsupported printed scientific label: '+text)
    return float(m[1])

def consume(records,req,res,structure,known_words=None,refine_labels=False):
    validate(res,schema())
    if digest({k:v for k,v in req.items() if k!='request_sha256'})!=req['request_sha256'] or res['request_sha256']!=req['request_sha256']:raise ValueError('crossfigure request mismatch')
    out=copy.deepcopy(records);mixes={m['mix_key']:m for m in out['mixes']};images={i['image_id']:i for i in req['images']}
    for im in images.values():
        if hashlib.sha256(Path(im['path']).read_bytes()).hexdigest()!=im['sha256']:raise ValueError('gap crop changed')
    targets={t['target_id']:t for t in req['label_targets']};panels={p['panel_id']:p for p in req['panel_targets']};changes=[];repairs=[];new_values=[];unresolved=list(res['unresolved'])
    graph=copy.deepcopy(structure);graph['visual_gap_request_sha256']=req['request_sha256'];graph['visual_panels']=[]
    graph['prior_structure_status']='HISTORICAL_PROPOSALS_SUPERSEDED_WHERE_VISUAL_OVERRIDE_EXISTS'
    graph['active_panel_overrides']=[p['panel_id'] for p in res['panels']]
    def locate(image_id,box,text):
        im=images[image_id];bounds(box,im['size']);o=im['native_origin'];native=[box[i]+o[i%2] for i in range(4)]
        p=im['parent_image_pdf_bbox'];w,h=im['parent_size']
        return {'page':im['page'],'bbox':[p[0]+native[0]/w*(p[2]-p[0]),p[1]+native[1]/h*(p[3]-p[1]),p[0]+native[2]/w*(p[2]-p[0]),p[1]+native[3]/h*(p[3]-p[1])],
            'snippet':text,'image_source_id':im['source_id'],'native_bbox_px':native,'crop_bbox_px':box,'crop_path':im['path'],'crop_sha256':im['sha256'],'parent_sha256':im['parent_sha256']}
    def complete_known(image_id,proposal,text):
        im=images[image_id]
        if not refine_labels:
            loc=locate(image_id,[0,0,*im['size']],text)
            loc.update(navigation_level='parent_panel',localization_method='CONFIRMED_PANEL_AUXILIARY_NAVIGATION',visual_proposal_bbox_px=proposal)
            return loc
        rgb=np.asarray(Image.open(im['path']).convert('RGB'));box=proposal;method='visual_anchor'
        candidates=join_words((known_words or {}).get(im['source_id'],[]),lambda t:text if normal(t)==normal(text) else None)
        distinct={tuple(round(v,0) for v in c['bbox']):c for c in candidates}
        if distinct:
            # Multiple OCR variants of the same printed label may differ slightly.
            vals=list(distinct.values());first=vals[0]['bbox']
            same=all(max(abs(a-b) for a,b in zip(first,c['bbox']))<12 for c in vals)
            if same:
                b=first;o=im['native_origin'];box=[b[i]-o[i%2] for i in range(4)];method='exact_known_label_ocr'
        try:proof=complete_label_region(rgb,box,min_overlap=.25)
        except ValueError:
            proof={'bbox_px':[0,0,*im['size']],'method':'CORRECT_PARENT_PANEL_FALLBACK','recognition_verified':False}
        loc=locate(image_id,proof['bbox_px'],text);loc.update(visual_proposal_bbox_px=proposal,glyph_proof=proof,localization_method=method)
        return loc
    def evidence(m,path,loc,original,unit,formula,tag,support=None):
        pdf=next(a for a in out['assets'] if a.get('kind')=='pdf' and a['paper_key']==m['paper_key'])
        ek='ev-cross-'+digest([req['request_sha256'],m['mix_key'],path,tag])[:24]
        ev={'evidence_key':ek,'record_key':m['mix_key'],'record_type':'mix','paper_key':m['paper_key'],'field_path':path,'asset_key':pdf['asset_key'],
            'page':loc['page'],'bbox':loc['bbox'],'snippet':loc['snippet'],'extraction_method':VERSION,
            'extensions':{**loc,'source_ids':[loc['image_source_id']],'support_locators':support or [],'formal':False,'recognition_status':'VISUAL_SOURCE_CANDIDATE'}}
        if not any(e['evidence_key']==ek for e in out['evidence_links']):out['evidence_links'].append(ev)
        m['field_provenance'][path]={'evidence_key':ek,'original_value':original,'original_unit':unit,'formula':formula,'extraction_method':VERSION,'review_status':'pending','confidence':None}
        return ek
    seen=set()
    for item in res['label_repairs']:
        tid=item['target_id']
        if tid in seen or tid not in targets:raise ValueError('label target duplicated/outside request')
        seen.add(tid);target=targets[tid]
        if item['image_id']!=target['image_id']:raise ValueError('label image mismatch')
        key='sem-'+digest([req['source_requests']['planned-sem-eds'],tid])[:24];m=mixes[target['record_key']]
        matches=[(i,r) for i,r in enumerate(m['modules']['characterizations']) if r.get('extensions',{}).get('source_candidate_key')==key]
        if len(matches)!=1:raise ValueError('target scientific row missing')
        n,row=matches[0]
        if scalar(item['complete_text'],row['unit'])!=row['value']:raise ValueError('label repair changes scientific value')
        glyph={'bbox_px':[0,0,*images[item['image_id']]['size']],'method':'CONFIRMED_LABEL_GROUP_AUXILIARY_NAVIGATION','recognition_verified':False}
        if refine_labels:
            rgb=np.asarray(Image.open(images[item['image_id']]['path']).convert('RGB'))
            try:glyph=complete_label_region(rgb,item['bbox_px'])
            except ValueError:pass
        loc=locate(item['image_id'],glyph['bbox_px'],item['complete_text']);loc['visual_proposal_bbox_px']=item['bbox_px'];loc['glyph_proof']=glyph
        loc['navigation_level']='label_group' if not refine_labels else 'label_region'
        path=f'/modules/characterizations/{n}/value'
        previous=copy.deepcopy(m['field_provenance'][path]);history=row['extensions'].setdefault('locator_history',[])
        if previous.get('extraction_method')!=VERSION:history.append(previous)
        formula=f"{row['value']/1000} * 1000" if row['unit']=='x' and re.search('[Kk]',item['complete_text']) else None
        ek=evidence(m,path,loc,item['complete_text'],row['unit'],formula,tid)
        repairs.append({'target_id':tid,'record_key':m['mix_key'],'field_path':path,'locator':loc})
        changes.append({'record_key':m['mix_key'],'field_path':path,'value':row['value'],'evidence_key':ek,'source_ids':[loc['image_source_id']],'task_ids':['t_sem_eds_characterization']})
    if set(targets)-seen:unresolved.append({'reason':'LABEL_TARGETS_UNRETURNED','target_ids':sorted(set(targets)-seen)})
    seen_claims=set()
    for c in res['mip_printed_values']:
        if c['claim_id'] in seen_claims or c['panel_id'] not in panels:raise ValueError('MIP gap claim identity')
        seen_claims.add(c['claim_id']);target=panels[c['panel_id']]
        if c['image_id']!=target['image_id'] or target['kind']!='porosity_volume_fraction_summary':raise ValueError('MIP gap source mismatch')
        owners=[s for s in target['existing_semantics']['series'] if s['record_key']==c['record_key'] and normal(s['age_label'])==normal(str(int(c['age_days']))+'d')]
        if len(owners)!=1 or normal(c['category_text'])!=normal(owners[0]['source_label']+owners[0]['age_label']):raise ValueError('MIP sample/age label not matched')
        if normal(c['legend_text'])!=normal(c['pore_scope']):raise ValueError('MIP pore class mismatch')
        value=scalar(c['printed_text'],'%')
        if not 0<=value<=100:raise ValueError('MIP printed percent out of range')
        value_loc=complete_known(c['image_id'],c['value_bbox_px'],c['printed_text']);cat=complete_known(c['image_id'],c['category_bbox_px'],c['category_text']);legend=complete_known(c['image_id'],c['legend_bbox_px'],c['legend_text'])
        m=mixes[c['record_key']];rows=m['modules']['characterizations'];key='cross-mip-'+digest([req['request_sha256'],c['claim_id']])[:24]
        row={'name':'pore_volume_fraction','value':value,'unit':'%','age_seconds':c['age_days']*86400,'specimen':None,
            'extensions':{'source_candidate_key':key,'measurement_method':'MIP','pore_scope':c['pore_scope'],'source_request_sha256':req['request_sha256'],
                'reported_precision':'printed_graphic_percentage','source_role':'DIRECT_IMAGE_LABEL','review_status':'pending','formal':False}}
        matches=[i for i,r in enumerate(rows) if r.get('extensions',{}).get('source_candidate_key')==key]
        if matches:
            n=matches[0]
            if rows[n]!=row:raise ValueError('MIP visual projection drift')
        else:n=len(rows);rows.append(row)
        for field,loc,unit,formula in [('value',value_loc,'%',None),('age_seconds',cat,'d',f"{c['age_days']} * 86400")]:
            path=f'/modules/characterizations/{n}/{field}';ek=evidence(m,path,loc,loc['snippet'],unit,formula,c['claim_id']+field,[cat,legend])
            changes.append({'record_key':m['mix_key'],'field_path':path,'value':row[field],'evidence_key':ek,'source_ids':[loc['image_source_id']],'task_ids':['t_mip_characterization']})
        new_values.append({'claim_id':c['claim_id'],'record_key':m['mix_key'],'value':value,'age_days':c['age_days'],'pore_scope':c['pore_scope'],'locator':value_loc,'support':[cat,legend]})
    for p in res['panels']:
        if p['panel_id'] not in panels or panels[p['panel_id']]['image_id']!=p['image_id']:raise ValueError('visual panel escaped request')
        im=images[p['image_id']];rgb=np.asarray(Image.open(im['path']).convert('RGB'));bounds(p['plot_bbox_px'],im['size']);node=copy.deepcopy(p)
        try:frame=plot_frame(rgb,p['plot_bbox_px'])
        except ValueError:frame=p['plot_bbox_px'];node['frame_status']='SOURCE_PROPOSAL_ONLY'
        node['detected_frame_px']=frame
        for axis in node['axes']:
            for tick in axis['ticks']:bounds(tick['label_bbox_px'],im['size'])
            if axis['kind']=='categorical':
                axis['calibration_status']='CATEGORICAL_NO_NUMERIC_FIT';continue
            declared={'label':axis['label'],'unit':axis['unit'],'scale':axis['scale'],'direction':axis['side'],
                'ticks':[t for t in axis['ticks'] if t['value'] is not None and t['position_px'] is not None]}
            if len(declared['ticks'])!=len(axis['ticks']):raise ValueError('numeric axis has null tick')
            labels=[{'text':str(t['value']),'bbox':t['label_bbox_px']} for t in declared['ticks']]
            axis['pixel_check']=relocate_ticks(rgb,frame,declared,0 if axis['orientation']=='x' else 1,labels,axis['side'])
            axis['calibration_status']=axis['pixel_check']['check']['status']
        graph['visual_panels'].append(node)
    return out,{'changes':changes,'sem_label_repairs':repairs,'mip_printed_values':new_values,'graph':graph,'unresolved':unresolved,
        'formal_acceptance':False,'publication_allowed':False}

def run(records,directory):
    directory=Path(directory);load=lambda p:json.loads(Path(p).read_bytes());gap=directory/'visual-gap'
    req=load(gap/'request.json');res=load(gap/'model/response.json');structure=load(directory/'structure-report.json')
    known={}
    for region in load(directory/'region-ocr.json'):
        if ':' in region['region_id']:continue
        known.setdefault(region['source_id'],[]).extend(region['words'])
    out,report=consume(records,req,res,structure,known);graph=report.pop('graph');gh=digest(graph);gp=directory/'objects'/('graph-'+gh[:24]+'.json');atomic_json(gp,graph)
    modules=req['source_requests']
    for mix in out['mixes']:
        for row in mix['modules']['characterizations']:
            ext=row.get('extensions',{})
            if ext.get('source_request_sha256') in list(modules.values())+[req['request_sha256']]:
                ext['crossfigure_graph_ref']={'path':str(gp),'sha256':gh,'formal':False}
    from source_region_candidates import apply_navigation
    report['navigation']=apply_navigation(out,report,req,directory)
    h=digest(out);path=directory/'objects'/(h[:24]+'.json');writes=len(report['changes'])
    if path.exists():
        if digest(load(path))!=h:raise ValueError('crossfigure output collision')
        writes=0
    else:atomic_json(path,out)
    receipt={'input_records_sha256':digest(records),'output_records_sha256':h,'output_path':str(path),'graph_path':str(gp),'graph_sha256':gh,
        **report,'model_calls':0,'scientific_writes':writes,'geometry_status':'SOURCE_GRAPH_AND_LABEL_REPAIRS_PENDING_REVIEW'}
    prior=directory/'result.json'
    if prior.exists():atomic_json(directory/'history'/(digest(load(prior))[:24]+'.json'),load(prior))
    atomic_json(prior,receipt);return out,receipt

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--records',type=Path,required=True);p.add_argument('--directory',type=Path,required=True)
    a=p.parse_args();_,r=run(json.loads(a.records.read_bytes()),a.directory);print(json.dumps({k:v for k,v in r.items() if k not in ('changes','sem_label_repairs','mip_printed_values')},ensure_ascii=False))
