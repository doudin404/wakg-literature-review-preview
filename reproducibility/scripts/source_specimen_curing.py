"""Source-local curing for first-group mortar/paste, not concrete or pretreatment."""
import copy,hashlib,re
from pathlib import Path
import fitz
from main_figure_context import normalize
from source_specimen_variants import digest


def scoped_variants(records):
    """Check declared scope by identity, never by a corpus-specific count."""
    variants = records.get('extensions', {}).get('specimen_variants', {}).get('assignments', [])
    if not isinstance(variants, list) or not variants:
        raise ValueError('curing requires explicit nonempty specimen scope')
    mixes = records.get('mixes', [])
    by_key = {m['mix_key']: m for m in mixes}
    if len(by_key) != len(mixes):
        raise ValueError('duplicate curing MIX owner')
    seen = set()
    for variant in variants:
        key = variant.get('mix_key')
        if not key or key in seen or key not in by_key:
            raise ValueError('duplicate or missing curing scope owner')
        seen.add(key)
        identity = by_key[key].get('modules', {}).get('identity_source_specimen', {})
        if (variant.get('specimen') != identity.get('specimen_type') or
                variant.get('custom_test_id') != identity.get('custom_test_id')):
            raise ValueError('curing scope identity differs from declared specimen')
    return variants


def enrich(records):
    variants = scoped_variants(records)
    assets=[a for a in records['assets'] if a['kind']=='pdf']
    if len(assets)!=1:raise ValueError('specimen curing requires unique PDF')
    asset=assets[0]
    if ([p['paper_key'] for p in records['papers']] != [asset['paper_key']] or
            any(m['paper_key']!=asset['paper_key'] for m in records['mixes'])):
        raise ValueError('curing cross-paper ownership mismatch')
    raw=Path(asset['relative_path']).read_bytes()
    if hashlib.sha256(raw).hexdigest()!=asset['sha256']:raise ValueError('curing PDF hash mismatch')
    pattern=(r'Later, the fresh mixtures were poured into molds, vibrated for (\d+) s '
        r'\((\d+) s for paste mixtures\), and sealed using plastic films\. After (\d+) h, '
        r'specimens \(.+?\) were demolded and cured under standard curing conditions '
        r'\(temperature = (\d+) .+?; relative humidity (>) (\d+) %\) until testing\.')
    found=[]
    with fitz.open(stream=raw,filetype='pdf') as doc:
        for page_number,page in enumerate(doc,1):
            words=page.get_text('words');parts=[normalize(w[4]) for w in words];text=' '.join(parts)
            offsets=[];cursor=0
            for part in parts:offsets.append((cursor,cursor+len(part)));cursor+=len(part)+1
            def locate(start,end):
                return [{'page':page_number,'bbox':list(w[:4]),'token':p} for w,p,(a,b) in zip(words,parts,offsets) if a<end and b>start]
            for match in re.finditer(pattern,text):
                groups={i:locate(match.start(i),match.end(i)) for i in range(1,7)}
                if any(len(v)!=1 for v in groups.values()):raise ValueError('curing quantity token ambiguous')
                found.append({'statement':match.group(),'locators':locate(match.start(),match.end()),
                    'tokens':groups,'hours':int(match[3]),'temperature':int(match[4]),'humidity':int(match[6]),'relation':match[5]})
    if len(found)!=1:raise ValueError('curing methods paragraph missing or ambiguous')
    source=found[0];staged=copy.deepcopy(records)
    by_key={m['mix_key']:m for m in staged['mixes']}
    for variant in variants:
        mix=by_key[variant['mix_key']];kind=variant['specimen']
        if kind not in ('mortar','paste') or mix['modules']['identity_source_specimen']['specimen_type']!=kind:
            raise ValueError('curing specimen scope mismatch')
        curing=mix['modules']['mixing_curing']
        if curing['curing_stages'] or curing['demoulding'] is not None:raise ValueError('curing already exists; source reconciliation required')
        seconds=source['hours']*3600
        curing['curing_stages']=[{'method':'sealed mould curing','temperature_C':None,
            'humidity_percent':None,'duration_seconds':seconds,
            'extensions':{'stage_start':'forming_complete','stage_end':'demoulding'}},
            {'method':'standard curing after demoulding','temperature_C':source['temperature'],
             'humidity_percent':source['humidity'],'duration_seconds':None,
             'extensions':{'duration_label':'until testing','humidity_relation':source['relation']}}]
        curing['demoulding']={'method':f"after {source['hours']} h following forming",'time_after_mixing_seconds':None}
        curing['age_origin']=None
        def evidence(path,tokens,formula=None,receipt=None,unit=None):
            snippet=' '.join(t['token'] for t in tokens)
            box=[min(t['bbox'][0] for t in tokens),min(t['bbox'][1] for t in tokens),max(t['bbox'][2] for t in tokens),max(t['bbox'][3] for t in tokens)]
            link={'evidence_key':'ev-curing-'+digest([mix['mix_key'],path,tokens])[:24],
                'asset_key':asset['asset_key'],'paper_key':asset['paper_key'],'record_type':'mix','record_key':mix['mix_key'],
                'field_path':path,'page':tokens[0]['page'],'section':None,'table_number':None,'figure_number':None,
                'bbox':box,'snippet':snippet,'snippet_sha256':hashlib.sha256(snippet.encode()).hexdigest(),
                'extraction_method':'source-specimen-curing-v1','confidence':0.95,
                'source_locator':{'coordinate_space':'pdf_points'},
                'extensions':{'relationship_source_statements':[{'statement':source['statement'],'locators':source['locators']}]}}
            staged['evidence_links'].append(link)
            mix['field_provenance'][path]={'evidence_key':link['evidence_key'],'original_value':snippet,
                'original_unit':'h' if receipt else unit,'formula':formula,'transformation':receipt,
                'extraction_method':link['extraction_method'],'confidence':0.95,'review_status':'pending_source_path_review'}
        prefix='/modules/mixing_curing/curing_stages'
        formula='seconds = reported mould-stage hours * 3600'
        receipt={'kind':'unit_scale','formal':True,'formula':formula,
            'inputs':{'value':source['hours'],'unit':'h','scale':3600},'output':{'value':seconds,'unit':'s'},
            'forward_check':{'computed':seconds,'recorded':seconds,'tolerance':1e-9},
            'reverse_check':{'computed':seconds/3600,'recorded':source['hours'],'tolerance':1e-9}}
        evidence(prefix+'/0/duration_seconds',source['tokens'][3],formula,receipt)
        for field,group,unit in [('temperature_C',4,'\u25e6C'),('humidity_percent',6,'%'),('extensions/humidity_relation',5,None)]:
            evidence(prefix+'/1/'+field,source['tokens'][group],unit=unit)
        evidence(prefix,source['locators'])
        evidence('/modules/mixing_curing/demoulding',source['tokens'][3],unit='h')
        curing['extensions']['source_scope']='first_group_mortar_and_paste_only'
    records.clear();records.update(staged)
