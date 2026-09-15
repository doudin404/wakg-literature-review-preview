"""Keep identical precursor IDs separate when their complete recipes differ."""
import copy,hashlib,json,math,re
from pathlib import Path
import fitz


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()


def readable_word(records,binding,word):
    sources=[a for a in records['assets'] if a['asset_key']==binding['pdf_asset_key'] and a['kind']=='pdf']
    if len(sources)!=1:raise ValueError('specimen identity source missing')
    raw=Path(sources[0]['relative_path']).read_bytes()
    if hashlib.sha256(raw).hexdigest()!=binding['pdf_sha256']:
        raise ValueError('specimen identity source hash changed')
    with fitz.open(stream=raw,filetype='pdf') as doc:
        page=doc[word['page']-1];native=fitz.Rect(word['bbox'])
        for inset in (0,.15,.25,.30,.40):
            box=fitz.Rect(native.x0,native.y0+native.height*inset,native.x1,native.y1-native.height*inset)
            if page.get_textbox(box).strip()==word['token']:
                return {**word,'bbox':list(box)}
    raise ValueError('specimen identity locator contains adjacent text')


def declared_specimen_scopes(binding):
    """Read explicit sample-kind statements in the already scoped source packet."""
    if binding.get('method') == 'semantic-series-axis-reference-v1':
        scopes = sorted({s for a in binding['assignments'] for s in a['specimen_types']})
        if not scopes or not set(scopes) <= {'paste', 'mortar', 'concrete', 'powder'}:
            raise ValueError('semantic source specimen scope unresolved')
        return scopes
    scopes = []
    for statement in binding['source_statements']:
        match = re.match(r'^(?:In\s+)?(paste|mortar|concrete|powder)\s+(?:samples|specimens)\b',
                         statement['statement'], re.I)
        if match and match[1].lower() not in scopes:
            scopes.append(match[1].lower())
    if not scopes:
        raise ValueError('source packet has no explicit specimen-kind scope')
    return scopes


def precursor_memberships(records, assignment):
    """Resolve ordered composition components through their source-cell identities."""
    percentages, keys = assignment['precursor_mass_percent'], assignment['table_evidence_keys']
    if len(percentages) != len(keys) or len(set(keys)) != len(keys):
        raise ValueError('precursor composition/source-cell alignment invalid')
    owners = [m for m in records['mixes'] if m['mix_key'] == assignment['mix_key']]
    if len(owners) != 1:
        raise ValueError('precursor reference MIX not unique')
    parameters = owners[0]['modules']['materials']['extensions']['reported_parameters']
    references = []
    fold = lambda s: ''.join(str(s).split()).casefold()
    for value, key in zip(percentages, keys):
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 100:
            raise ValueError('precursor percentage invalid')
        params = [p for p in parameters if p.get('evidence_key') == key]
        links = [e for e in records['evidence_links'] if e['evidence_key'] == key
                 and e.get('record_key') == assignment['mix_key'] and e.get('record_type') == 'mix']
        if len(params) != 1 or len(links) != 1:
            raise ValueError('precursor source cell missing or ambiguous')
        if value == 0:
            continue
        materials = [m for m in records['mats'] if fold(m['custom_material_id']) == fold(params[0]['original_label'])
                     and m['paper_key'] == owners[0]['paper_key']]
        if len(materials) != 1:
            raise ValueError('positive precursor MAT missing or ambiguous')
        references.append(materials[0]['mat_key'])
    return sorted(set(references))


def split(records,binding,quantity_plans=None):
    """Create empty recipe-specific shells; never copy concrete mass quantities.

    Precursor percentages and preparation context remain source-bound metadata
    until their material-field conversion is accepted. Scientific observation
    routing can already use the correct specimen-specific owner.
    """
    if quantity_plans is not None:
        from source_specimen_recipe_fields import attach_quantity_plans
        binding = attach_quantity_plans(binding, quantity_plans)
    if records.get('extensions',{}).get('specimen_variants'):
        raise ValueError('specimen variant split must run once before observations')
    staged=copy.deepcopy(records);created=[];locator_cache={}
    by_key={m['mix_key']:m for m in staged['mixes']}
    if len(by_key)!=len(staged['mixes']):raise ValueError('duplicate existing recipe key')
    specimen_scopes = declared_specimen_scopes(binding)
    for assignment in binding['assignments']:
        base=by_key[assignment['mix_key']]
        series = assignment.get('quantity_plan', {}).get('source_series')
        material_refs=[] if series else precursor_memberships(staged,assignment)
        for specimen in specimen_scopes:
            identity=assignment['source_test_id']
            key='mix-specimen-'+digest([base['paper_key'],identity,specimen])[:24]
            if key in by_key or any(v['mix_key']==key for v in created):
                raise ValueError('duplicate specimen recipe key')
            tokens=[w for s in binding['source_statements'] for w in s['locators']]
            ids=[w for w in tokens if w['token'].strip(' ,.;')==identity]
            kinds=[w for w in tokens if w['token'].lower().strip(' ,.;')==specimen]
            if not ids or not kinds:raise ValueError('variant identity or specimen lacks source token')
            mix={'schema_version':base['schema_version'],'mix_key':key,'paper_key':base['paper_key'],
                'mix_family_key':None,'modules':{
                    'identity_source_specimen':{'custom_test_id':identity,'specimen_type':specimen,
                        'specimens':[],'literature_source':copy.deepcopy(base['modules']['identity_source_specimen']['literature_source']),'extensions':{}},
                    'materials':{'mat_refs':material_refs.copy(),
                        'solid_materials':[],'activator_total_mass_g':None,'activators':[],
                        'fine_aggregate':None,'coarse_aggregate':None,'reported_mass_basis':None,
                        'platform_ratios':None,'conversion':None,'review_status':'pending',
                        'extensions':{'reported_parameters':[]}},
                    'mixing_curing':{'mixing':None,'forming':None,'demoulding':None,
                        'curing_stages':[],'curing_route':None,'age_origin':None,'extensions':{}},
                    'performance':[],'characterizations':[]},'field_provenance':{},
                'extensions':{'source_table':binding.get('source_section'),'source_row':binding['assignments'].index(assignment),
                    'recipe_source_binding':{'scope':'precursor_and_specimen_only',
                        'precursor_mass_percent':None if series else assignment['precursor_mass_percent'],
                        'source_series':copy.deepcopy(series),
                        'source_statements':copy.deepcopy(binding['source_statements']),
                        'reference_mix_key':base['mix_key'],'review_status':'pending_recipe_field_mapping'}}}
            for field,word in (('custom_test_id',ids[0]),('specimen_type',kinds[0])):
                locator_key=digest(word)
                if locator_key not in locator_cache:
                    locator_cache[locator_key]=readable_word(staged,binding,word)
                word=locator_cache[locator_key]
                path='/modules/identity_source_specimen/'+field
                evidence={'evidence_key':'ev-specimen-'+digest([key,path,word])[:24],
                    'asset_key':binding['pdf_asset_key'],'record_type':'mix','record_key':key,
                    'paper_key':base['paper_key'],'field_path':path,'page':word['page'],
                    'section':binding.get('source_section'),'table_number':None,'figure_number':None,'bbox':word['bbox'],
                    'snippet':word['token'],'snippet_sha256':hashlib.sha256(word['token'].encode()).hexdigest(),
                    'extraction_method':'source-specimen-context-v1','confidence':0.95,
                    'source_locator':{'coordinate_space':'pdf_points'}}
                staged['evidence_links'].append(evidence)
                mix['field_provenance'][path]={'evidence_key':evidence['evidence_key'],
                    'original_value':word['token'],'original_unit':None,'formula':None,
                    'extraction_method':evidence['extraction_method'],'confidence':0.95,
                    'review_status':'pending_source_path_review'}
            # A specimen identity locator alone is not a material-relation
            # proof. Preserve the scoped statements and exact inferred MAT
            # membership in a separate owned relation evidence entry.
            identity_path = '/modules/identity_source_specimen/custom_test_id'
            identity_key = mix['field_provenance'][identity_path]['evidence_key']
            origin = next(e for e in staged['evidence_links'] if e['evidence_key'] == identity_key)
            relation_path = '/modules/materials'
            relation_key = 'ev-specimen-materials-' + digest([key, material_refs, binding['source_statements']])[:24]
            relation = copy.deepcopy(origin)
            relation.update(evidence_key=relation_key, field_path=relation_path,
                            extraction_method='source-specimen-material-relation-v1')
            relation['extensions'] = {'relationship_basis': 'context_inheritance',
                'mat_refs': material_refs.copy(), 'specimen_type': specimen,
                'precursor_mass_percent': None if series else copy.deepcopy(assignment['precursor_mass_percent']),
                'source_series': copy.deepcopy(series),
                'source_statements': copy.deepcopy(binding['source_statements']),
                'scope': 'precursor_membership_only_not_complete_recipe',
                'review_status': 'pending_source_path_review'}
            staged['evidence_links'].append(relation)
            mix['field_provenance'][relation_path] = {
                'evidence_key': relation_key, 'original_value': origin['snippet'],
                'original_unit': None, 'formula': None,
                'extraction_method': relation['extraction_method'], 'confidence': .95,
                'review_status': 'pending_source_path_review'}
            staged['mixes'].append(mix)
            # Import here to share the stable digest without an import cycle.
            from source_specimen_recipe_fields import populate
            populate(staged,mix,binding,assignment,specimen)
            if series:
                refs = []
                for parameter in mix['modules']['materials']['extensions']['reported_parameters']:
                    definition = parameter['extensions'].get('component_source_definition')
                    if not definition or parameter['value'] is None or parameter['value'] == 0:
                        continue
                    mat_key = definition['mat_key']
                    if mat_key is None:
                        raise ValueError('positive source component has unresolved MAT relationship')
                    if len([m for m in staged['mats'] if m['mat_key'] == mat_key and m['paper_key'] == base['paper_key']]) != 1:
                        raise ValueError('source series MAT relationship invalid')
                    refs.append(mat_key)
                mix['modules']['materials']['mat_refs'] = sorted(set(refs))
                relation['extensions']['mat_refs'] = sorted(set(refs))
            created.append({'custom_test_id':identity,'specimen':specimen,'mix_key':key,
                            'reference_mix_key':base['mix_key']})
    staged.setdefault('extensions',{})['specimen_variants']={'schema_version':1,'assignments':created}
    records.clear();records.update(staged)
