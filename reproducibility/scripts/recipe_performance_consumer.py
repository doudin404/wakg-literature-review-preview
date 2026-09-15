"""Consume source-selected quantities and observations transactionally.

No paper IDs, figure numbers, row positions, or material-name routing here.
Semantic selections remain pending independent review; pixels are estimates.
"""
import copy
import hashlib
import math
from pathlib import Path
import fitz
import numpy as np
from PIL import Image
from source_specimen_variants import digest, readable_word
from source_specimen_recipe_fields import populate, attach_quantity_plans
from raster_bars import extract_bar,locate_axis_ticks
from raster_curves import calibrate_ticks
from vector_curves import coordinate

VERSION='indexed-recipe-performance-consumer-v1'
UNITS={'compressive_strength':{'MPa':('MPa',1)},'flexural_strength':{'MPa':('MPa',1)},
       'flow_diameter':{'mm':('mm',1)},'initial_setting_time':{'min':('s',60),'s':('s',1)},
       'final_setting_time':{'min':('s',60),'s':('s',1)},'bleeding_rate':{'%':('%',1)}}
STRESS_UNITS={'MPa':('MPa',1),'GPa':('MPa',1000),'kPa':('MPa',.001)}
for _name in ('compressive_strength','flexural_strength','splitting_tensile_strength',
              'tensile_strength','elastic_modulus','dynamic_elastic_modulus'):
    UNITS[_name]=dict(STRESS_UNITS)
UNITS.update(slump={'mm':('mm',1),'cm':('mm',10)},
             bulk_density={'kg/m3':('kg/m3',1),'g/cm3':('kg/m3',1000)},
             water_absorption={'%':('%',1)},open_porosity={'%':('%',1)},
             total_porosity={'%':('%',1)})
FRESH_PROPERTIES={'flow_diameter','slump','initial_setting_time','final_setting_time','bleeding_rate'}


def unique(rows,key):
    result={r[key]:r for r in rows}
    if len(result)!=len(rows):raise ValueError('duplicate source identity: '+key)
    return result


def observation_route(observation):
    ext=observation.get('extensions',{})
    return ext.get('route_id') or ext.get('source_route_binding',{}).get('route_id')


class Source:
    def __init__(self,records,req):
        self.records=records;self.req=req
        self.rows=unique(req['rows'],'row_id');self.spans=unique(req['spans'],'span_id')
        self.numbers=unique(req['tokens'],'token_id');self.mixes=unique(records['mixes'],'mix_key')
        self.binding={'pdf_asset_key':req['pdf_asset']['asset_key'],'pdf_sha256':req['pdf_asset']['sha256']}
        self.word_cache={}

    def scope(self,ids):
        if not ids or len(ids)!=len(set(ids)) or not set(ids)<=set(self.spans):
            raise ValueError('source scope unknown or duplicate')
        return [copy.deepcopy(self.spans[k]) for k in ids]

    def number(self,key,ids):
        scopes=self.scope(ids)
        if key not in self.numbers:raise ValueError('source number unknown')
        n=self.numbers[key]
        if not any(n['locator'] in s['locators'] for s in scopes):
            raise ValueError('source number outside declared evidence')
        if key not in self.word_cache:
            if n['locator'].get('asset_key'):
                from indexed_native_sources import readable_native_word
                self.word_cache[key]=readable_native_word(self.records,n['locator'])
            else:self.word_cache[key]=readable_word(self.records,self.binding,n['locator'])
        return n['value'],copy.deepcopy(self.word_cache[key])

    def owner(self,row):
        if row not in self.rows:raise ValueError('unknown source row')
        mix=self.mixes[self.rows[row]['mix_key']]
        if mix['paper_key']!=self.req['pdf_asset']['paper_key']:raise ValueError('cross-paper owner')
        return mix

    def route(self,row,claim):
        mix=self.owner(row)
        if claim.get('route_id') is None:
            if claim['age_unit']=='NOT_APPLICABLE':
                if claim.get('name') not in FRESH_PROPERTIES or claim['age_token_id'] is not None:
                    raise ValueError('not-applicable age is for fresh measurements')
                return None,None,None
            if claim['age_unit']=='UNKNOWN' and claim['age_token_id'] is None:
                return None,None,None
            value,word=self.number(claim['age_token_id'],claim['source_span_ids'])
            scales={'d':86400,'h':3600,'s':1,'min':60}
            if claim['age_unit'] not in scales or value<=0:raise ValueError('invalid reported test age')
            return None,value*scales[claim['age_unit']],word
        routes=mix['modules']['mixing_curing'].get('extensions',{}).get('source_routes',[])
        matches=[r for r in routes if r['route_id']==claim['route_id']]
        if len(matches)!=1 or row not in matches[0]['subject_scope']['row_ids']:
            raise ValueError('observation route outside owner scope')
        route=matches[0];age=None;word=None
        if claim['age_unit']=='NOT_APPLICABLE':
            if claim['age_token_id'] is not None or route['endpoint_status']!='NOT_APPLICABLE':
                raise ValueError('unknown age is not a fresh-property age')
        else:
            age_scopes=list(claim['source_span_ids'])
            # The source model can point to the exact upstream method-age token
            # while the value itself lives in a results paragraph or figure.
            for condition in route.get('declared_conditions',[]):
                if condition.get('numeric_token_id')==claim['age_token_id']:
                    age_scopes.extend(s for s in condition['source_span_ids'] if s not in age_scopes)
            value,word=self.number(claim['age_token_id'],age_scopes)
            if claim['age_unit'] not in {'d','h','s'}:raise ValueError('unsupported age unit')
            age=value*{'d':86400,'h':3600,'s':1}[claim['age_unit']]
            if age<=0 or route['endpoint_status']!='KNOWN' or route['specimen_age_seconds']!=age:
                raise ValueError('observation age differs from source route endpoint')
        return route,age,word


def add_link(records,mix,path,word,source,*,figure=None,extensions=None):
    key='ev-indexed-'+digest([mix['mix_key'],path,word,extensions])[:24]
    snippet=word.get('token')
    link={'evidence_key':key,'record_type':'mix','record_key':mix['mix_key'],'paper_key':mix['paper_key'],
          'asset_key':word.get('asset_key',source.req['pdf_asset']['asset_key']),'field_path':path,'page':word['page'],
          'bbox':word['bbox'],'snippet':snippet,'snippet_sha256':hashlib.sha256(snippet.encode()).hexdigest() if snippet else None,
          'section':None,'table_number':word.get('source_locator',{}).get('table_number'),'figure_number':figure,'confidence':None,
          'extraction_method':VERSION,'source_locator':copy.deepcopy(word.get('source_locator',{'coordinate_space':'pdf_points'})),'extensions':extensions or {}}
    if any(e['evidence_key']==key for e in records['evidence_links']):raise ValueError('duplicate new evidence')
    records['evidence_links'].append(link)
    mix.setdefault('field_provenance',{})[path]={'evidence_key':key,'original_value':snippet,
        'original_unit':None,'formula':None,'extraction_method':VERSION,'confidence':None,'review_status':'pending'}
    return key


def consume_quantities(records,req,response,source):
    grouped={};mats=unique(records['mats'],'mat_key')
    for q in response['quantities']:
        source.scope(q['source_span_ids'])
        if not q['row_ids'] or len(set(q['row_ids']))!=len(q['row_ids']):raise ValueError('quantity row scope duplicate or empty')
        if q['destination']=='solid_materials':
            if q['mat_key'] not in mats or mats[q['mat_key']]['paper_key']!=req['pdf_asset']['paper_key']:
                raise ValueError('quantity MAT owner missing')
            if q['unit']!='wt.%':raise ValueError('solid proportion unit unsupported')
        elif q['destination']!='platform_ratios' or q['mat_key'] is not None or q['unit'] not in {'%','molar ratio','mass ratio'}:
            raise ValueError('ratio routing/unit unsupported')
        for row in q['row_ids']:
            source.owner(row);grouped.setdefault(row,[]).append(q)
    count=0
    for row,claims in grouped.items():
        mix=source.owner(row);material=mix['modules']['materials'];existing=material['extensions']['reported_parameters']
        fold=lambda x:''.join(x.split()).casefold()
        if {fold(q['key']) for q in claims}&{fold(p['parameter_key']) for p in existing}:
            raise ValueError('quantity would overwrite existing parameter')
        kinds={q['specimen_type'] for q in claims}
        if len(kinds)!=1:raise ValueError('quantity specimen scope ambiguous')
        specimen=next(iter(kinds));scopes=list(dict.fromkeys(s for q in claims for s in q['source_span_ids']))
        statements=[{'statement':s['quote'],'locators':s['locators']} for s in source.scope(scopes)]
        assignment={'source_test_id':source.rows[row]['label'],'mix_key':mix['mix_key'],'specimen_types':[specimen]}
        binding={**source.binding,'source_statements':statements,'assignments':[assignment]}
        quantities=[];basis=None;mass_basis=None
        for q in claims:
            indices=[scopes.index(s) for s in q['source_span_ids']]
            item={'parameter_key':q['key'],'label':q['label'],'mode':q['mode'],'unit':q['unit'],
                  'source_unit':q['unit'],'specimen_types':[specimen],'source_specimen_types':[specimen],
                  'mass_basis':q['mass_basis'],'scope_statement_indices':indices,'reason':q['reason']}
            if q['mode']=='direct':
                item['value'],_=source.number(q['numeric_token_id'],q['source_span_ids'])
                item['source_token']=source.numbers[q['numeric_token_id']]['locator']
                if q['total_token_id'] is not None:raise ValueError('direct quantity has remainder total')
                if q['component_keys'] and (q['destination']!='solid_materials' or
                    set(q['component_keys'])!={c['key'] for c in claims if c['destination']=='solid_materials'}):
                    raise ValueError('direct composition membership contradicts scoped members')
            elif q['mode']=='remainder':
                total,_=source.number(q['total_token_id'],q['source_span_ids'])
                if basis is not None or total!=100 or q['numeric_token_id'] is not None or q['destination']!='solid_materials':
                    raise ValueError('remainder basis unresolved')
                if set(q['component_keys'])!={c['key'] for c in claims if c['destination']=='solid_materials'}:
                    raise ValueError('complete composition differs from source component set')
                mass_basis=q['mass_basis'];basis={'complete':True,'component_keys':q['component_keys'],
                    'mass_basis':mass_basis,'unit':'wt.%','total_percent':total,'statement_indices':indices}
                item.update(value=None,direct_value_status='absent_in_scoped_source')
            else:raise ValueError('unsupported quantity operation')
            quantities.append(item)
        plan={'schema_version':'scoped-recipe-quantities-v1','source_binding_sha256':digest(binding),
              'specimen_types':[specimen],'scope_statement_indices':list(range(len(scopes))),
              'mass_basis':mass_basis,'composition_basis':basis,'quantities':quantities}
        bound=attach_quantity_plans(binding,{assignment['source_test_id']:plan})
        offset=len(existing);populate(records,mix,bound,bound['assignments'][0],specimen,append_new=True)
        for q,param in zip(claims,existing[offset:]):
            dest=q['destination'];target=material.setdefault(dest,[])
            if any(x.get('name')==q['key'] for x in target):raise ValueError('canonical quantity already exists')
            original=next(e for e in records['evidence_links'] if e['evidence_key']==param['evidence_key'])
            path=f'/modules/materials/{dest}/{len(target)}/value'
            key=add_link(records,mix,path,{'page':original['page'],'bbox':original['bbox'],'token':original['snippet']},source,
                extensions={'source_parameter_key':q['key'],'scope_evidence':source.scope(q['source_span_ids']),
                            'transformation':param['transformation']})
            target.append({'name':q['key'],'mat_key':q['mat_key'],'value':param['value'],'unit':param['unit'],
                'transformation':copy.deepcopy(param['transformation']),'evidence_key':key,
                'extensions':{'source_parameter_key':q['key'],'mass_basis':q['mass_basis'],
                              'status':param['status'],'mapping_version':VERSION}})
            prov=mix['field_provenance'][path];prov['original_unit']=param['unit'];prov['original_value']=param['original_value']
            prov['formula']=(param['transformation'] or {}).get('formula');prov['transformation']=copy.deepcopy(param['transformation'])
        if basis:
            # A complete source-selected solid composition replaces an inherited
            # catch-all solid membership, but does not discard activator/aggregate owners.
            owners={q['mat_key'] for q in claims if q['destination']=='solid_materials'}
            owners.update(x.get('mat_key') for dest in ('activators','fine_aggregate','coarse_aggregate')
                          for x in (material.get(dest) or []) if isinstance(x,dict))
            owners.discard(None)
            material['extensions']['previous_unscoped_mat_refs']=copy.deepcopy(material['mat_refs'])
            material['mat_refs']=sorted(owners)
        material['review_status']='pending';count+=len(claims)
    return count


def compile_observations(req,response,source):
    observations=[];digitized=[]
    for claim in response['prose_observations']:
        route,age,age_word=source.route(claim['row_id'],claim)
        value,word=source.number(claim['numeric_token_id'],claim['source_span_ids'])
        observations.append({'row_id':claim['row_id'],'name':claim['name'],'value':value,'unit':claim['unit'],
            'route':route,'age':age,'age_word':age_word,'age_unit':claim['age_unit'],'word':word,
            'scope':source.scope(claim['source_span_ids']),'source_type':'DIRECT_PROSE','figure':None,
            'value_qualifier':claim.get('value_qualifier')})
    figures=unique(req['figures'],'figure_id')
    if len(response['figures'])!=len(figures) or {f['figure_id'] for f in response['figures']}!=set(figures):
        raise ValueError('figure source coverage differs')
    for selection in response['figures']:
        figure=figures[selection['figure_id']];raw=Path(figure['path']).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=figure['asset']['sha256']:raise ValueError('bar image changed')
        rgb=np.array(Image.open(figure['path']).convert('RGB'))
        if [rgb.shape[1],rgb.shape[0]]!=figure['geometry']['native_size']:raise ValueError('native image size changed')
        bars=unique(figure['geometry']['bars'],'bar_id');used=[]
        for panel in selection['panels']:
            ids=[bid for series in panel['series'] for bid in series['bar_ids']]
            if not set(ids)<=set(bars):raise ValueError('bar identity unknown')
            selected=[bars[bid] for bid in ids]
            absolute=panel.get('endpoint_kind')=='absolute_top'
            axis,baseline,geometry=locate_axis_ticks(rgb,selected,panel['ticks'],panel['unit'],use_full_axis=absolute)
            calibrated=calibrate_ticks(axis)
            config={'baseline_pixel':baseline,'baseline_tolerance_px':3,'value_axis':axis,
                    'endpoint_mode':'absolute_chromatic_top' if absolute else 'chromatic_region_top'}
            for series in panel['series']:
                if len(series['row_ids'])!=len(series['bar_ids']) or len(set(series['row_ids']))!=len(series['row_ids']):
                    raise ValueError('bar owner list mismatch')
                positions=[]
                for row,bid in zip(series['row_ids'],series['bar_ids']):
                    if bid not in bars or bid in used:raise ValueError('bar identity unknown or reused')
                    bar=bars[bid]
                    if bar['color_id']!=series['color_id']:raise ValueError('bar series color mismatch')
                    route,age,age_word=source.route(row,series);positions.append(bar['bbox_px'][0]);used.append(bid)
                    measured=extract_bar(rgb,config,{**bar,'label':source.rows[row]['label'],
                        'minimum_row_coverage':.51,'color_distance':12})
                    native=measured['source_bbox_px'];origin=figure['render_origin_px']
                    box=[(native[i]+origin[i%2])/2 for i in range(4)]
                    datum={'row_id':row,'name':panel['name'],'value':measured['value'],'unit':panel['unit'],
                        'route':route,'age':age,'age_word':age_word,'age_unit':series['age_unit'],
                        'word':{'page':figure['asset']['extensions']['page'],'bbox':box,'token':None},
                        'scope':source.scope(series['source_span_ids']),'source_type':'DIGITIZED_ESTIMATE',
                        'figure':figure['asset']['extensions']['figure_number'],
                        'digitization':{'image_asset_key':figure['asset']['asset_key'],'image_sha256':figure['asset']['sha256'],
                            'bar_id':bid,'axis':calibrated,'measurement':measured,
                            'axis_localization':geometry,
                            'value_interval':sorted(coordinate(y,calibrated) for y in measured['endpoint_interval_px']),
                            'interval_basis':'endpoint pixel only; not experimental error or confidence interval',
                            'pixel_to_pdf':{'scale':2,'origin':origin},'formal':False}}
                    observations.append(datum);digitized.append(datum)
                if positions!=sorted(positions):raise ValueError('bar source order is not left to right')
        excluded=selection['excluded_bar_ids']
        if len(excluded)!=len(set(excluded)) or set(excluded)&set(used) or set(excluded)|set(used)!=set(bars):
            raise ValueError('bar candidates must be used or explicitly excluded')
        if excluded and not selection['exclusion_reason'].strip():raise ValueError('bar exclusion reason missing')
    return observations,digitized


def consume(records,req,response,exclusions=None):
    """All mutations occur in a private copy; failed batches leave input intact."""
    if req['request_sha256']!=digest({k:v for k,v in req.items() if k!='request_sha256'}) or response['request_sha256']!=req['request_sha256']:
        raise ValueError('source request/response identity differs')
    if digest(records)!=req['records_sha256']:raise ValueError('quantity/performance input changed')
    from source_recipe_performance import schema
    from jsonschema import validate
    validate(response,schema(req))
    staged=copy.deepcopy(records);source=Source(staged,req)
    effective=copy.deepcopy(response);isolated=[]
    if exclusions is not None:
        if (exclusions.get('request_sha256')!=req['request_sha256'] or exclusions.get('response_sha256')!=digest(response)
            or exclusions.get('scope')!='DEVELOPMENT_EXCLUSION_ONLY'):
            raise ValueError('development exclusion has different source identity')
        claims={digest(o):o for o in response['prose_observations']}
        decisions=unique(exclusions['prose_observations'],'claim_sha256')
        if not set(decisions)<=set(claims):raise ValueError('exclusion claim not in immutable source response')
        for key,decision in decisions.items():
            if not decision.get('reason'):raise ValueError('source exclusion needs reason')
            claim=claims[key];source.scope(claim['source_span_ids']);source.owner(claim['row_id'])
            isolated.append({'kind':'ambiguous_performance_source','status':'QUARANTINED','formal':False,
                'paper_key':req['pdf_asset']['paper_key'],'mix_key':source.rows[claim['row_id']]['mix_key'],
                'source_claim':copy.deepcopy(claim),'claim_sha256':key,'reason':decision['reason'],
                'source_evidence':source.scope(claim['source_span_ids']),'request_sha256':req['request_sha256']})
        effective['prose_observations']=[o for o in response['prose_observations'] if digest(o) not in decisions]
        staged.setdefault('quarantined',[]).extend(isolated)
    quantities=consume_quantities(staged,req,response,source)
    observations,digitized=compile_observations(req,effective,source)
    added,_=publish_observations(staged,source,observations)
    report={'status':'RECIPE_PERFORMANCE_DEVELOPMENT_CONSUMED','request_sha256':req['request_sha256'],
        'response_sha256':digest(response),'records_sha256':digest(staged),'quantities_added':quantities,
        'observations_added':added,'prose_observations':len(response['prose_observations']),
        'prose_observations_consumed':len(effective['prose_observations']),'prose_observations_quarantined':len(isolated),
        'digitized_bars':len(digitized),'unresolved':copy.deepcopy(response['unresolved']),
        'publication_allowed':False,'independent_acceptance':False}
    return staged,report


def publish_observations(staged,source,observations,merge_existing=False):
    grouped={}
    for obs in observations:
        key=(obs['row_id'],obs['name'],(obs.get('route') or {}).get('route_id'),obs['age'])
        grouped.setdefault(key,[]).append(obs)
    added=0;updated=0
    for key,items in grouped.items():
        direct=[o for o in items if o['source_type'].startswith('DIRECT_')];plots=[o for o in items if o['source_type']=='DIGITIZED_ESTIMATE']
        if len(direct)>1:
            if len(direct)!=2 or {o['source_type'] for o in direct}!={'DIRECT_PROSE','DIRECT_IMAGE_LABEL'} or len({(o['value'],o['unit']) for o in direct})!=1:
                raise ValueError('duplicate or contradictory direct observations')
            direct.sort(key=lambda o:o['source_type']!='DIRECT_PROSE')
        if len(plots)>1:raise ValueError('duplicate observation in same source type')
        obs=(direct or plots)[0];mix=source.owner(obs['row_id']);values=mix['modules']['performance']
        route=obs.get('route') or {};route_id=route.get('route_id')
        prior=[(i,o) for i,o in enumerate(values) if o['name']==obs['name'] and o.get('age_seconds')==obs['age'] and
               observation_route(o) in (None,route_id)]
        if prior and not merge_existing:raise ValueError('new observation would overwrite prior performance')
        if len(prior)>1:raise ValueError('existing observation target ambiguous')
        if prior and observation_route(prior[0][1])!=route_id:
            raise ValueError('existing observation has a different test route')
        try:unit,scale=UNITS[obs['name']][obs['unit']]
        except KeyError as exc:raise ValueError('performance unit not compatible with property') from exc
        value=obs['value']*scale
        if not math.isfinite(value) or value<0:raise ValueError('performance value invalid')
        if prior and prior[0][1]['extensions']['source_type'].startswith('DIRECT_'):
            existing=prior[0][1]
            if direct and (existing['unit']!=unit or existing['value']!=value):raise ValueError('two direct observations conflict')
            if plots:
                plot=plots[0];counterpart=copy.deepcopy(plot['digitization'])
                counterpart.update(value=plot['value'],difference_from_reported=plot['value']-existing['value']/scale,
                                   pdf_locator=plot['word'])
                existing['extensions']['plotted_counterpart']=counterpart
            updated+=1;continue
        specimens=mix['modules']['identity_source_specimen']['specimens']
        specimen=next((s for s in specimens if s['specimen_id']==route_id),{})
        index=prior[0][0] if prior else len(values);path=f'/modules/performance/{index}/value'
        previous_plot=None
        if prior:
            old=prior[0][1]
            if not direct or old['extensions']['source_type']!='DIGITIZED_ESTIMATE':raise ValueError('unsupported observation replacement')
            previous_plot=copy.deepcopy(old['extensions']['digitization'])
            previous_plot.update(value=old['value'],unit=old['unit'],difference_from_reported=old['value']-value)
            alternate=f'/modules/performance/{index}/extensions/plotted_counterpart/value'
            old_key=mix['field_provenance'][path]['evidence_key']
            link=next(e for e in staged['evidence_links'] if e['evidence_key']==old_key)
            link['field_path']=alternate
            mix['field_provenance'][alternate]=mix['field_provenance'].pop(path)
        proof={'scope_evidence':obs['scope'],'route_id':route_id,'source_type':obs['source_type'],
               'digitization':obs.get('digitization')}
        evidence=add_link(staged,mix,path,obs['word'],source,figure=obs['figure'],extensions=proof)
        prov=mix['field_provenance'][path];prov.update(original_value=obs['value'],original_unit=obs['unit'])
        if scale!=1:
            prov['formula']='value = original * '+str(scale)
            prov['transformation']={'kind':'unit_scale','formal':False,'formula':prov['formula'],
                'inputs':{'value':obs['value'],'unit':obs['unit'],'scale':scale},'output':{'value':value,'unit':unit},
                'forward_check':{'computed':value,'recorded':value,'tolerance':0},
                'reverse_check':{'computed':value/scale,'recorded':obs['value'],'tolerance':1e-10}}
        item={'name':obs['name'],'value':value,'unit':unit,'age_seconds':obs['age'],
              'specimen':specimen.get('specimen_type'),'method':None,
              'extensions':{'schema_version':VERSION,'source_type':obs['source_type'],'review_status':'pending',
                  'original_value':obs['value'],'original_unit':obs['unit'],'evidence_key':evidence,
                  'route_id':route_id,'route_description':route.get('description_zh'),
                  'method_status':'SOURCE_ROUTE_BOUND_STANDARD_AND_GEOMETRY_NOT_PROJECTED',
                  'digitization':obs.get('digitization'),'formal':False}}
        if obs.get('value_qualifier'):item['extensions']['value_qualifier']=obs['value_qualifier']
        if len(direct)>1:item['extensions']['direct_image_counterpart']=copy.deepcopy(direct[1]['digitization'])
        if previous_plot:item['extensions']['plotted_counterpart']=previous_plot
        if obs['age_word'] and not prior:
            apath=f'/modules/performance/{index}/age_seconds'
            add_link(staged,mix,apath,obs['age_word'],source,extensions={'route_id':route_id})
            age_scale={'d':86400,'h':3600,'s':1,'min':60}[obs['age_unit']]
            mix['field_provenance'][apath].update(original_unit=obs['age_unit'],formula=f'seconds = original * {age_scale}',
                transformation={'kind':'unit_scale','formal':False,'inputs':{'value':obs['age']/age_scale,'unit':obs['age_unit']},
                    'output':{'value':obs['age'],'unit':'s'},'forward_check':{'computed':obs['age'],'recorded':obs['age'],'tolerance':0},
                    'reverse_check':{'computed':obs['age']/age_scale,'recorded':obs['age']/age_scale,'tolerance':0}})
        elif obs['age'] is None:item['extensions']['age_basis']='NOT_APPLICABLE_FRESH_PROPERTY' if obs['age_unit']=='NOT_APPLICABLE' else 'NOT_REPORTED'
        if direct and plots:
            alternative=plots[0];item['extensions']['plotted_counterpart']=alternative['digitization']
            item['extensions']['plotted_counterpart']['value']=alternative['value']
            item['extensions']['plotted_counterpart']['difference_from_reported']=alternative['value']-obs['value']
            item['extensions']['plotted_counterpart']['pdf_locator']=alternative['word']
        if prior:values[index]=item;updated+=1
        else:values.append(item);added+=1
    return added,updated
