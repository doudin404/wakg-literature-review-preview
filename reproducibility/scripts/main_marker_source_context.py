"""Source-methods and precursor ownership for mixed specimen figure panels."""
import hashlib
import re
from pathlib import Path
import fitz
from marker_formulations import resolve_formulations, caption_scope
from main_figure_context import normalize,bind_caption
from supplement_docx import inspect_docx, _normalize_mix_id
from supplement_recipe_selection import select_recipe_table


def paste_method_panel(panel_context):
    labels = [p.get('panel') for p in panel_context]
    if any(not label for label in labels) or len(set(labels)) != len(labels):
        raise ValueError('main marker panel identity missing or duplicate')
    paste = [p for p in panel_context if p.get('specimen') == 'paste']
    mortar = [p for p in panel_context if p.get('specimen') == 'mortar']
    if len(paste) != 1 or len(mortar) != 1 or len(paste) + len(mortar) != len(panel_context):
        raise ValueError('main marker methods and caption specimen disagree')
    return paste[0]['panel']


def bind(records,report,source_plan=None):
    if caption_scope(report['caption']) != 'panel_specific':
        raise ValueError('main marker requires explicit mixed-specimen caption context')
    supplements=[a for a in records['assets'] if a['kind']=='supplement']
    if len(supplements)!=1:raise ValueError('main marker requires unique supplementary source table')
    supplement=supplements[0];path=Path(supplement['relative_path'])
    if hashlib.sha256(path.read_bytes()).hexdigest()!=supplement['sha256']:
        raise ValueError('main marker source supplement hash mismatch')
    inventory=inspect_docx(path)
    identities = [m['modules']['identity_source_specimen']['custom_test_id'] for m in records['mixes']]
    table = select_recipe_table(inventory, identities, _normalize_mix_id)
    formulation=resolve_formulations(records,table,
        {'label':report['figure_number'],'caption':report['caption']},report['digitization'],
        source_plan=source_plan or report.get('source_context',{}).get('formulation_binding',{}).get('source_plan'))
    pdfs=[a for a in records['assets'] if a['kind']=='pdf' and a['sha256']==report['source_pdf_sha256']]
    if len(pdfs)!=1:raise ValueError('main marker context source PDF mismatch')
    raw=Path(pdfs[0]['relative_path']).read_bytes()
    if hashlib.sha256(raw).hexdigest()!=report['source_pdf_sha256']:
        raise ValueError('main marker context PDF bytes changed')
    statements=[]
    with fitz.open(stream=raw,filetype='pdf') as doc:
        if type(report.get('page')) is not int or not 1 <= report['page'] <= len(doc):
            raise ValueError('main marker source page invalid')
        if bind_caption(doc[report['page']-1],report['caption'],report['digitization'])!=report['panel_context']:
            raise ValueError('main marker stored panel context changed')
        for number,page in enumerate(doc,1):
            words=page.get_text('words');parts=[normalize(w[4]) for w in words]
            text=' '.join(parts)
            matches=list(re.finditer(r'Water absorption was measured on cylinder paste specimens '
                r'.+?After (\d+) days of curing, the specimens were dried at (\d+) .+? for (\d+) h\. '
                r'.+?as well as the open porosity, i\.e\.,? the proportion of the final amount '
                r'of water absorbed to the volume of the sample\.',text))
            for match in matches:
                cursor=0;locators=[]
                for word,part in zip(words,parts):
                    end=cursor+len(part)
                    if cursor<match.end() and end>match.start():
                        locators.append({'page':number,'bbox':list(word[:4]),'token':part})
                    cursor=end+1
                statements.append({'statement':match.group(),'locators':locators,
                    'curing_age_days':int(match[1]),'drying_temperature_c':int(match[2]),
                    'drying_duration_hours':int(match[3])})
    if len(statements)!=1:raise ValueError('main marker porosity methods missing or ambiguous')
    methods=statements[0]
    paste_panel = paste_method_panel(report['panel_context'])
    return {'schema_version':1,'scope':'candidate_source_context_not_formal_acceptance',
        'formulation_binding':formulation,'supplement_sha256':supplement['sha256'],
        'porosity_method':{'panel':paste_panel,'name':'water_absorption','specimen':'paste',
            'age_seconds':methods['curing_age_days']*86400,
            'age_semantics':'curing_duration_before_drying_not_total_elapsed_time',
            'source_evidence':methods},'review_status':'pending_source_path_review'}
