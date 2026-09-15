"""Source-reopened sparse numeric locators. Never treats geometry as PDF text."""
import hashlib,json,math,re
from pathlib import Path
import fitz
from sparse_vector_observations import extract
from bind_sparse_performance import source_part
from source_sparse_pipeline import artifact_receipt,read_frozen_proof,implementation_review_path
from vector_curves import digest,coordinate
from transformation_contract import validate_records

VERSION='sparse-review-v3-versioned-implementation'


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def load(path):return json.loads(Path(path).read_text(encoding='utf-8'))
def safe(root,path):
    result=(root/path).resolve();result.relative_to(root.resolve());return result


def subject_for(root,path):
    path=Path(path).resolve()
    dependencies=load(path)['extensions']['input_hashes']['sparse_recipe_dependencies']
    return {'recordsPath':path.relative_to(root.resolve()).as_posix(),'recordsSha256':sha(path),
            'protocol':VERSION,'verifierSha256':sha(root/'scripts/sparse_review_projection.py'),
            'proofInputs':dependencies}


def check_subject_inputs(root,subject):
    path=safe(root,subject['recordsPath'])
    records=load(path)
    dependencies=records['extensions']['input_hashes']['sparse_recipe_dependencies']
    if (subject.get('protocol')!=VERSION or subject.get('recordsSha256')!=sha(path)
            or subject.get('verifierSha256')!=sha(root/'scripts/sparse_review_projection.py')
            or subject.get('proofInputs')!=dependencies):
        raise ValueError('sparse review subject mismatch')
    # Execute only installed verifier code, never archived scripts. Code drift
    # requires a new extraction/review; data and approval originals are unnecessary.
    for name,expected in dependencies.items():
        if name.startswith('scripts/') and sha(safe(root,name))!=expected:
            raise ValueError('sparse review implementation drift')
    manifest=load(path.parent/'evidence-manifest.json')
    if (manifest['records_sha256']!=digest(records)
            or manifest.get('input_hashes')!=records['extensions']['input_hashes']
            or manifest.get('sparse_artifacts')!=artifact_receipt(records,path.parent)):
        raise ValueError('sparse cached proof artifacts changed')


def verify_direct_support(owner,row_index,item,links,asset,doc,plan_sha):
    row=owner['modules']['performance'][row_index];path=f'/modules/performance/{row_index}/value'
    prov=owner['field_provenance'][path];source=links[prov['evidence_key']]
    expected={'candidate':item['candidate'],'digitization_interval':item['digitization_interval'],
              'preferred_source':'direct_prose','plan_sha256':plan_sha}
    if (row['extensions'].get('sparse_support')!=expected or not row['extensions'].get('prose_binding_id')
            or row['extensions'].get('sparse_binding_id') or row_index!=item['existing_observation_index']
            or source['record_key']!=owner['mix_key'] or source['paper_key']!=owner['paper_key']
            or source['asset_key']!=asset['asset_key'] or source['field_path']!=path
            or source['evidence_key']!=prov['evidence_key'] or prov['original_unit']!=row['unit']
            or float(prov['original_value'])!=row['value']
            or not item['digitization_interval'][0]<=row['value']<=item['digitization_interval'][1]):
        raise ValueError('sparse direct support source identity mismatch')
    page=doc[source['page']-1];box=fitz.Rect(source['bbox'])
    if not 1<=source['page']<=len(doc) or box.is_empty or not page.rect.contains(box):
        raise ValueError('sparse direct support source box invalid')
    text=page.get_textbox(box)
    if str(prov['original_value']) not in text or source['snippet'] not in text or hashlib.sha256(source['snippet'].encode()).hexdigest()!=source['snippet_sha256']:
        raise ValueError('sparse direct support PDF readback failed')
    return (owner['mix_key'],row_index)


def verified_index(root,subject):
    check_subject_inputs(root,subject)
    path=safe(root,subject['recordsPath']);run=path.parent
    if (subject.get('protocol')!=VERSION or subject.get('recordsSha256')!=sha(path)
            or subject.get('verifierSha256')!=sha(root/'scripts/sparse_review_projection.py')):
        raise ValueError('sparse review subject mismatch')
    records=load(path);manifest=load(run/'evidence-manifest.json')
    if validate_records(records)['verdict']!='PASS':raise ValueError('sparse formal transformation contract failed')
    if manifest['records_sha256']!=digest(records):raise ValueError('sparse canonical manifest mismatch')
    dependencies=records['extensions']['input_hashes']['sparse_recipe_dependencies']
    if manifest['input_hashes']!=records['extensions']['input_hashes']:
        raise ValueError('sparse recipe input drift')
    if manifest.get('sparse_artifacts')!=artifact_receipt(records,run):raise ValueError('sparse proof manifest mismatch')
    frozen=read_frozen_proof(run,dependencies)
    def frozen_json(name):return json.loads(frozen[name])
    geometry=frozen_json('fixtures/sparse-vector-p02-fig3-v1.json')
    binding=frozen_json('fixtures/sparse-performance-p02-fig3-binding-v1.json')
    plan=load(run/'sparse-assets/binding-plan.json');candidates=load(run/'sparse-assets/candidates.json')
    approval=load(run/'sparse-assets/recipe-execution.json')
    promotion=records['extensions']['sparse_performance_promotion']
    ids={'plan_sha256':digest(plan),'candidates_sha256':digest(candidates),'binding_sha256':digest(binding),
         'geometry_sha256':digest(geometry),'pdf_sha256':geometry['pdf_sha256']}
    if promotion['identities']!=ids or any(approval.get(k)!=v for k,v in ids.items()):
        raise ValueError('sparse proof identity mismatch')
    review=frozen_json(implementation_review_path(frozen))
    for path,key in [('scripts/promote_sparse_performance.py','helper_sha256'),
                     ('scripts/transformation_contract.py','transformation_contract_sha256'),
                     ('scripts/render_sparse_vector_overlay.py','overlay_renderer_sha256')]:
        if review.get(key)!=dependencies[path]:
            raise ValueError('sparse frozen implementation approval mismatch')
    overlay=frozen_json('runs/requirements-repair-v1/p02-sparse-overlay-agent-review.json')
    binding_review=frozen_json('runs/requirements-repair-v1/p02-sparse-binding-agent-review.json')
    if (binding_review.get('verdict')!='PASS'
            or binding_review.get('message_id')!='p02-sparse-binding-review-20260908-01'
            or binding_review.get('scope')!='BOUND_PLAN_REQUIRES_PROMOTION_ACCEPTANCE'
            or binding_review.get('binding_file_sha256')!=dependencies['fixtures/sparse-performance-p02-fig3-binding-v1.json']
            or binding_review.get('helper_sha256')!=dependencies['scripts/bind_sparse_performance.py']):
        raise ValueError('sparse binding approval mismatch')
    if (approval.get('verdict')!='PASS' or review.get('verdict')!='PASS' or overlay.get('verdict')!='PASS'
            or approval.get('message_id')!=review['message_id']
            or approval.get('overlay_sha256')!=overlay['overlay_sha256']
            or hashlib.sha256(frozen[approval['overlay_path']]).hexdigest()!=overlay['overlay_sha256']):
        raise ValueError('sparse recipe approval mismatch')
    assets={a['asset_key']:a for a in records['assets']};asset=assets[plan['source_asset_key']]
    pdf=safe(root,str(run/asset['relative_path']))
    if asset['kind']!='pdf' or sha(pdf)!=geometry['pdf_sha256'] or asset['sha256']!=geometry['pdf_sha256']:
        raise ValueError('sparse PDF identity mismatch')
    if extract(pdf,geometry)!=candidates:raise ValueError('sparse native geometry replay mismatch')
    links={e['evidence_key']:e for e in records['evidence_links']}
    if len(links)!=len(records['evidence_links']):raise ValueError('duplicate sparse source keys')
    mixes={m['mix_key']:m for m in records['mixes']};index={};seen=[];supported=set()
    with fitz.open(pdf) as doc:
        context={k:source_part(doc,binding[k]) for k in ('ages','specimen','method')}
        if context!=plan['context_evidence']:raise ValueError('sparse source method context mismatch')
        ages=[float(x) for x in re.findall(r'\d+(?:\.\d+)?',context['ages']['raw'])]
        drawings=doc[geometry['page']-1].get_drawings()
        for item in plan['items']:
            candidate=item['candidate']
            if candidate not in candidates['observations'] or candidate in seen:raise ValueError('sparse candidate missing or duplicated')
            seen.append(candidate);owner=mixes[item['mix_key']]
            definitions=[d for d in binding['owners'] if d['source_id']==candidate['source_label']]
            if len(definitions)!=1:raise ValueError('sparse legend owner ambiguous')
            definition=definitions[0]
            if (owner['paper_key']!=asset['paper_key'] or owner['modules']['identity_source_specimen']['custom_test_id']!=definition['source_id']
                    or owner['extensions']['source_table']!=definition['source_table'] or owner['extensions']['source_row']!=definition['source_row']):
                raise ValueError('sparse canonical MIX owner mismatch')
            identity=source_part(doc,{'page':3,'clause':definition['identity_clause'],'capture':'('+re.escape(definition['source_id'])+')'})
            matched=[age for age in ages if abs(age-candidate['age_from_axis'])<=binding['age_tolerance_days']]
            if len(matched)!=1 or item['identity_evidence']!=identity or item['age_seconds']!=matched[0]*86400:
                raise ValueError('sparse source age or identity mismatch')
            rows=[(i,r) for i,r in enumerate(owner['modules']['performance']) if r['name']==binding['property'] and r.get('age_seconds')==item['age_seconds']]
            if len(rows)!=1:raise ValueError('sparse performance owner not unique')
            row_index,row=rows[0]
            if row['method']!=context['method']['raw'] or row['specimen']!=context['specimen']['raw'] or row['unit']!=candidate['unit']:
                raise ValueError('sparse performance context mismatch')
            point=candidate['pdf_point'];pixel_error=max(.3,float(drawings[candidate['source_path_index']]['width'])/2)+candidate['marker_center_error_pt']
            interval=sorted(coordinate(y,geometry['y_axis']) for y in (point[1]-pixel_error,point[1]+pixel_error))
            if interval!=item['digitization_interval']:raise ValueError('sparse digitization bound mismatch')
            if item['action']=='SUPPORT_DIRECT_OBSERVATION':
                support_key=verify_direct_support(owner,row_index,item,links,asset,doc,ids['plan_sha256'])
                if support_key in supported:raise ValueError('sparse duplicated support')
                supported.add(support_key)
                continue
            if item['action']!='ADD_OBSERVATION':raise ValueError('unknown sparse action')
            ext=row['extensions'];field_path=f'/modules/performance/{row_index}/value'
            prov=owner['field_provenance'][field_path];key=prov['evidence_key'];link=links[key]
            if (row['value']!=candidate['value_from_axis'] or ext['source_geometry']!=candidate
                    or ext['sparse_binding_id']!=digest([ids,item['source_id'],item['age_seconds']])
                    or ext['digitization_interval']!=interval or ext['display_decimal_places']!=1
                    or ext.get('axis_anchor_uncertainty') is not None or ext.get('experimental_error_semantics') is not None
                    or prov['original_value']!=point[1] or prov['original_unit']!='PDF pt'):
                raise ValueError('sparse canonical scalar or precision mismatch')
            box=[point[0]-2.8,point[1]-2.8,point[0]+2.8,point[1]+2.8]
            reverse=[coordinate(candidate['age_from_axis'],geometry['x_axis'],True),coordinate(row['value'],geometry['y_axis'],True)]
            if (link['record_key']!=owner['mix_key'] or link['paper_key']!=owner['paper_key'] or link['asset_key']!=asset['asset_key']
                    or link['field_path']!=field_path or link['page']!=geometry['page'] or link['bbox']!=box
                    or link['figure_number']!=geometry['figure'] or link['extensions']['candidate']!=candidate
                    or link['extensions']['axes']!={'x':geometry['x_axis'],'y':geometry['y_axis']}
                    or link['extensions']['reverse_pdf_point']!=reverse or max(abs(a-b) for a,b in zip(point,reverse))>1e-7):
                raise ValueError('sparse numeric evidence mismatch')
            index[key]={'subject':subject,'owner':owner['mix_key'],'path':field_path,'link':link,
                        'value':row['value'],'unit':row['unit'],'provenance':prov,'interval':interval,'pdf_sha256':asset['sha256']}
    if len(seen)!=len(candidates['observations']) or len(index)!=promotion['added'] or len(supported)!=promotion['supported']:
        raise ValueError('sparse review coverage mismatch')
    return index


def display_metadata(entry):
    return {'displayValue':f"≈ {entry['value']:.1f}",
            'digitization':{'decimalPlaces':1,'interval':entry['interval'],
                            'axisAnchorUncertainty':None,'experimentalErrorSemantics':None}}


def verify_locator(index,locator,field,owner,pdf_sha):
    key=field.get('evidenceKey');entry=index.get(key)
    if entry is None:raise ValueError('sparse locator has no reopened source proof')
    source=entry['link'];prov=entry['provenance']
    if (owner!=entry['owner'] or pdf_sha!=entry['pdf_sha256'] or locator.get('sparseSubject')!=entry['subject']
            or locator.get('evidenceMode')!='reviewed-sparse-geometry'
            or locator.get('sourceToken')!=source['snippet'] or locator.get('snippet')!=source['snippet']
            or any(locator.get(a)!=b for a,b in [('recordKey',owner),('fieldPath',entry['path']),('evidenceKey',key),('sourceEvidenceKey',key),('page',source['page']),('bbox',source['bbox'])])
            or field.get('semanticRole')!='performance_observation' or field.get('unit')!=entry['unit']
            or not math.isclose(float(field['value']),entry['value'],rel_tol=1e-12,abs_tol=1e-9)
            or field.get('originalValue')!=prov['original_value'] or field.get('originalUnit')!='PDF pt'
            or field.get('transformation')!=prov['transformation'] or field.get('sourceFormula')!=prov['formula']
            or field.get('sourceExplanationCode')!='SOURCE_DERIVED_VALUE'
            or any(field.get(k)!=v for k,v in display_metadata(entry).items())):
        raise ValueError('sparse reviewer value locator or precision mismatch')
    return True
