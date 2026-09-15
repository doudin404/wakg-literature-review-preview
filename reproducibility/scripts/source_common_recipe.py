"""Explicit all-mixtures parameters for the frozen calcined-soil paper profile."""
import copy,hashlib,math,re
from pathlib import Path
import fitz
from main_figure_context import normalize
from source_specimen_variants import digest

SPECS=[
 ('silicate_solution_to_naoh_solution_ratio','Sodium silicate solution/NaOH solution','mass ratio',
  r'The alkali-activated solution employed in the tests was .+?The mass ratio of so\s*dium silicate solution to NaOH solution was set at ([0-9.]+)\.'),
 ('na2o_to_precursor_percent','Na2O/precursor','wt.%',
  r'For all mixtures, the Na2O to precursor mass ratio was set at ([0-9.]+) %'),
 ('Ms','Activator modulus (SiO2/Na2O mass ratio)','mass ratio',
  r'For all mixtures, the Na2O to precursor mass ratio was set at [0-9.]+ %, and the activator modulus \(mass ratio of SiO2 to Na2O\) was set at ([0-9.]+)\.'),
 ('activator_solution_to_precursor_ratio','Alkali-activated solution/precursor','mass ratio',
  r'For all mixtures, the mass ratio of alkali-activated solution to pre\s*cursor '
  r'\(i\.e\. soil and GGBS\) was set at ([0-9.]+),'),
 ('water_to_solid_ratio','Water/solid (precursor + sodium silicate + NaOH)','mass ratio',
  r'For all mixtures, the mass ratio of alkali-activated solution to pre\s*cursor '
  r'\(i\.e\. soil and GGBS\) was set at [0-9.]+, which led to a water to solid mass ratio of ([0-9.]+), where the solid included the precursor, sodium silicate and NaOH\.')]


def enrich(records):
    assets=[a for a in records['assets'] if a['kind']=='pdf']
    if len(assets)!=1:raise ValueError('common recipe requires unique PDF')
    asset=assets[0]
    if ([p['paper_key'] for p in records['papers']] != [asset['paper_key']] or
            any(m['paper_key']!=asset['paper_key'] for m in records['mixes'])):
        raise ValueError('common recipe cross-paper ownership mismatch')
    raw=Path(asset['relative_path']).read_bytes()
    if hashlib.sha256(raw).hexdigest()!=asset['sha256']:raise ValueError('common recipe PDF hash mismatch')
    words=[];parts=[];offsets=[];cursor=0
    with fitz.open(stream=raw,filetype='pdf') as doc:
        for page_number,page in enumerate(doc,1):
            for word in page.get_text('words'):
                token=normalize(word[4]);parts.append(token);offsets.append((cursor,cursor+len(token)))
                words.append({'page':page_number,'bbox':list(word[:4]),'token':token});cursor+=len(token)+1
    text=' '.join(parts);claims=[]
    for key,label,unit,pattern in SPECS:
        matches=list(re.finditer(pattern,text))
        if len(matches)!=1:raise ValueError('common recipe statement missing or ambiguous: '+key)
        match=matches[0];value=float(match[1])
        tokens=[w for w,(a,b) in zip(words,offsets) if a<match.end(1) and b>match.start(1)]
        if len(tokens)!=1 or not math.isfinite(value):raise ValueError('common recipe value locator invalid')
        context=[w for w,(a,b) in zip(words,offsets) if a<match.end() and b>match.start()]
        claims.append((key,label,unit,value,tokens[0],{'statement':match.group(),'locators':context}))
    staged=copy.deepcopy(records)
    for mix in staged['mixes']:
        params=mix['modules']['materials']['extensions']['reported_parameters']
        if any(p['parameter_key'] in {s[0] for s in SPECS} for p in params):
            raise ValueError('common recipe parameter already exists; source reconciliation required')
        for key,label,unit,value,token,context in claims:
            path=f'/modules/materials/extensions/reported_parameters/{len(params)}/value'
            evidence={'evidence_key':'ev-common-recipe-'+digest([mix['mix_key'],path,context])[:24],
                'asset_key':asset['asset_key'],'record_type':'mix','record_key':mix['mix_key'],
                'paper_key':mix['paper_key'],'field_path':path,'page':token['page'],'section':'2',
                'table_number':None,'figure_number':None,'bbox':token['bbox'],'snippet':token['token'],
                'snippet_sha256':hashlib.sha256(token['token'].encode()).hexdigest(),
                'extraction_method':'source-common-recipe-v1','confidence':0.95,
                'source_locator':{'coordinate_space':'pdf_points'},'extensions':{'relationship_source_statements':[context]}}
            staged['evidence_links'].append(evidence)
            params.append({'parameter_key':key,'original_label':label,'value':value,'unit':unit,
                'original_value':token['token'],'original_unit':unit,'status':'reported','reason':None,
                'semantic_role':'reported_formulation','transformation':None,
                'evidence_key':evidence['evidence_key'],'extraction_method':evidence['extraction_method'],'confidence':0.95})
            mix['field_provenance'][path]={'evidence_key':evidence['evidence_key'],'original_value':token['token'],
                'original_unit':unit,'formula':None,'extraction_method':evidence['extraction_method'],
                'confidence':0.95,'review_status':'pending_source_path_review'}
    records.clear();records.update(staged)
