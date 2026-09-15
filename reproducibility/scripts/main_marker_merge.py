"""Source-replayed merge plan: direct values win; plotted duplicates only support."""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import fitz
import extract_main_figure_markers as markers
import main_marker_source_context
import main_marker_owners
import source_performance_candidates as prose
import main_marker_registry
from source_specimen_variants import digest
from property_table_cells import locate_cell, locate_cells
from supplement_docx import inspect_docx, _atomic_bytes, _pixel_y_transformation

ROOT=Path(__file__).resolve().parents[1]
BINDING=ROOT/'fixtures/main-marker-p06-fig1-v1.json'
PROSE=ROOT/'fixtures/prose-performance-p06-endpoints-v1.json'


def measurement_target(name):
    targets = {'porosity': ('open_porosity', 'characterizations'),
               'open_porosity': ('open_porosity', 'characterizations'),
               'compressive_strength': ('compressive_strength', 'performance'),
               'flexural_strength': ('flexural_strength', 'performance')}
    if name not in targets:
        raise ValueError('unresolved plotted measurement semantics: ' + str(name))
    return targets[name]


def axis_semantics(panel):
    axis=panel.get('x_axis',{})
    if not isinstance(axis.get('name'),str) or not axis['name'].strip() or not isinstance(axis.get('unit'),str) or not axis['unit'].strip():
        raise ValueError('main marker x-axis semantics unresolved')
    return {'name':axis['name'],'unit':axis['unit']}


def marker_description(figure,item):
    axis=item['x_axis']
    return f"{figure} panel {item['owner']['panel']}: {item['name']} at {axis['name']} {item['owner']['x']} {axis['unit']}"


def shared_test_reference(references, owner, age, paper_key):
    """Require matching specimen scope and unanimous test context, not an x whitelist."""
    eligible=[entry for entry in references if entry[0]['paper_key']==paper_key
              and entry[2].get('specimen')==owner['specimen']
              and entry[2].get('age_seconds')==age]
    if not eligible:raise ValueError('main marker common test context missing')
    contexts={(entry[2].get('age_seconds'),entry[2].get('specimen'),entry[2].get('method')) for entry in eligible}
    if len(contexts)!=1 or not next(iter(contexts))[2]:
        raise ValueError('main marker common test context ambiguous')
    return sorted(eligible,key=lambda entry:(entry[0]['mix_key'],entry[1]))[0]


def porosity_source_table(inventory, specimen_ids):
    if not specimen_ids:
        raise ValueError('porosity source requires specimen identities')
    matches = []
    for table in inventory.get('tables', []):
        if all(locate_cells(table,specimen,'open_porosity','%') for specimen in specimen_ids):
            matches.append(table)
    if len(matches) != 1:
        raise ValueError('original porosity table missing or ambiguous')
    return matches[0]


def prepare(records, run, report):
    config=main_marker_registry.select(records,run)
    if config is None:raise ValueError('main marker source configuration missing')
    replay=markers.extract_records(run,records,config['binding'])
    replay['source_context']=main_marker_source_context.bind(records,replay)
    replay['observation_owners']=main_marker_owners.bind(records,replay)
    if replay!=report:
        raise ValueError('main merge report does not replay from original source')
    pdf=next(a for a in records['assets'] if a['kind']=='pdf')
    # Re-extract original prose to verify existing preferred values and locators.
    checked=copy.deepcopy(records)
    prose.enrich(checked,pdf['relative_path'],config['prose'])
    if checked!=records:
        raise ValueError('direct source observations must precede main figure merge')
    supplement_hashes=set()
    panels={p['label']:p for p in report['digitization']['panels']}
    mixes={m['mix_key']:m for m in records['mixes']}
    links={e['evidence_key']:e for e in records['evidence_links']}
    items=[]
    for owner in report['observation_owners']['assignments']:
        panel=panels[owner['panel']]
        series=next(s for s in panel['series'] if s['name']==owner['series'])
        point=series['points'][owner['point_index']]
        mix=mixes[owner['mix_key']]
        name,module=measurement_target(series['name'])
        candidates=[(i,o) for i,o in enumerate(mix['modules'][module]) if o['name']==name
                    and (module=='characterizations' or o.get('age_seconds')==series['age_seconds'])]
        if len(candidates)>1:raise ValueError('main merge preferred measurement ambiguous')
        item={'owner':owner,'name':name,'module':module,'point':point,'unit':series['y_unit'],
              'x_axis':axis_semantics(panel),
              'age_seconds':series['age_seconds'],
              'transformation':_pixel_y_transformation(panel,series,point,'value = inverse linear pixel calibration on reviewed plot axes')}
        if candidates:
            index,original=candidates[0]
            if original['unit']!=series['y_unit']:
                raise ValueError('main merge preferred measurement unit mismatch')
            path=f'/modules/{module}/{index}/value'
            provenance=mix['field_provenance'][path]
            evidence=links[provenance['evidence_key']]
            if (evidence['record_key']!=mix['mix_key'] or evidence['field_path']!=path
                    or evidence['paper_key']!=mix['paper_key']):
                raise ValueError('main merge preferred source ownership mismatch')
            if module=='characterizations':
                sources=[a for a in records['assets'] if a['asset_key']==evidence['asset_key'] and a['kind']=='supplement']
                if len(sources)!=1:raise ValueError('preferred characterization source missing or ambiguous')
                supplement=sources[0]
                supplement_hashes.add(supplement['sha256'])
                inventory=inspect_docx(Path(supplement['relative_path']))
                table=porosity_source_table(inventory,{owner['source_test_id']})
                cell=locate_cell(table,owner['source_test_id'],name,series['y_unit'])
                raw=cell['raw']
                if (float(raw)!=original['value'] or provenance['original_value']!=raw
                        or evidence['asset_key']!=supplement['asset_key']):
                    raise ValueError('main merge preferred porosity differs from original table')
                item['preferred_table_cell']={**cell,
                    'rows_sha256':table['rows_sha256']}
            elif original.get('specimen')!=owner['specimen']:
                raise ValueError('main merge preferred strength specimen mismatch')
            # A graphic corroborates within its selected visible marker stroke;
            # this is not a confidence interval or experimental uncertainty.
            difference=abs(original['value']-point['y'])
            if difference>point['uncertainty_y']:
                raise ValueError('main merge graphic contradicts preferred direct value')
            item.update(action='SUPPORT_DIRECT_VALUE',existing_index=index,
                        preferred_value=original['value'],preferred_evidence_key=evidence['evidence_key'],
                        absolute_difference=difference,comparison_scope='within_selected_marker_stroke_not_total_error')
        else:
            if module!='performance':
                raise ValueError('main merge expected preferred direct source missing')
            item.update(action='ADD_DIGITIZED_OBSERVATION',value=point['y'])
            references=[(m,i,o) for m in records['mixes'] for i,o in enumerate(m['modules']['performance'])
                if o['name']==name and o.get('age_seconds')==series['age_seconds']
                and o.get('extensions',{}).get('mapping_message_id')==config['prose']['mapping_message_id']]
            reference,reference_index,source_row=shared_test_reference(references,owner,series['age_seconds'],mix['paper_key'])
            item['test_context']={}
            for field in ('age_seconds','specimen','method'):
                path=f'/modules/performance/{reference_index}/{field}'
                provenance=reference['field_provenance'][path]
                item['test_context'][field]={'value':source_row[field],'provenance':copy.deepcopy(provenance),
                    'evidence':copy.deepcopy(links[provenance['evidence_key']])}
        items.append(item)
    if len(items)!=report['observation_count'] or not items:
        raise ValueError('main figure merge coverage changed')
    return {'schema_version':1,'scope':'source_replayed_merge_plan_pending_acceptance',
            'report_sha256':digest(report),'pdf_asset_key':pdf['asset_key'],
            'pdf_sha256':pdf['sha256'],'image_sha256':report['source_image_sha256'],
            'supplement_sha256':next(iter(supplement_hashes)) if len(supplement_hashes)==1 else None,'page':report['page'],
            'supplement_sha256s':sorted(supplement_hashes),
            'figure_number':report['figure_number'],'panel_context':report['panel_context'],
            'porosity_method':report['source_context']['porosity_method'],
            'items':items,'counts':{'add':sum(i['action']=='ADD_DIGITIZED_OBSERVATION' for i in items),
                                  'support':sum(i['action']=='SUPPORT_DIRECT_VALUE' for i in items)},
            'dependencies_sha256':{**config['dependencies'],**{p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in (
                'scripts/source_performance_candidates.py','scripts/main_marker_owners.py','scripts/property_table_cells.py',
                'scripts/main_marker_source_context.py','scripts/main_marker_registry.py')}},
            'implementation_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


def project(records,run,report,plan,acceptance):
    """Apply an independently accepted source plan atomically on fresh input."""
    if (acceptance.get('verdict')!='PASS' or not acceptance.get('message_id') or acceptance.get('defects')
            or acceptance.get('plan_sha256')!=digest(plan)
            or acceptance.get('implementation_sha256')!=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()):
        raise ValueError('main figure merge lacks current input-bound acceptance')
    if records.get('extensions',{}).get('main_figure_merge'):
        raise ValueError('main figure merge already applied; use validated producer resume')
    if prepare(records,run,report)!=plan:
        raise ValueError('main figure merge plan no longer matches sources')
    staged=copy.deepcopy(records)
    mixes={m['mix_key']:m for m in staged['mixes']}
    links={e['evidence_key']:e for e in staged['evidence_links']}
    members=[]
    def add_link(mix,path,item):
        raw=marker_description(plan['figure_number'],item)
        key='ev-main-marker-'+digest([digest(plan),mix['mix_key'],path,item['point']])[:24]
        link={'evidence_key':key,'asset_key':plan['pdf_asset_key'],'paper_key':mix['paper_key'],
            'record_key':mix['mix_key'],'record_type':'mix','field_path':path,'page':plan['page'],
            'bbox':item['point']['source_pdf_bbox'],'snippet':raw,'snippet_sha256':hashlib.sha256(raw.encode()).hexdigest(),
            'section':None,'table_number':None,'figure_number':plan['figure_number'],
            'extraction_method':'main-figure-reviewed-marker-v1','confidence':.95,
            'source_locator':{'coordinate_space':'pdf_points'},
            'extensions':{'evidence_kind':'main_figure_digitization','point':item['point'],
                'transformation':item['transformation'],'plan_sha256':digest(plan),
                'source_image_sha256':plan['image_sha256'],'owner':item['owner']}}
        if key in links:raise ValueError('main marker evidence key collision')
        staged['evidence_links'].append(link);links[key]=link
        return key
    for item in plan['items']:
        mix=mixes[item['owner']['mix_key']]
        rows=mix['modules'][item['module']]
        if item['action']=='SUPPORT_DIRECT_VALUE':
            index=item['existing_index'];row=rows[index]
            path=f'/modules/{item["module"]}/{index}/value'
            if row['value']!=item['preferred_value'] or row.get('extensions',{}).get('main_figure_support'):
                raise ValueError('main figure support would overwrite existing contribution')
            key=add_link(mix,path,item)
            row.setdefault('extensions',{})['main_figure_support']={
                'evidence_key':key,'digitized_value':item['point']['y'],'preferred_value':row['value'],
                'preferred_evidence_key':item['preferred_evidence_key'],
                'comparison_scope':item['comparison_scope'],'absolute_difference':item['absolute_difference']}
            members.append({'mix_key':mix['mix_key'],'module':item['module'],'index':index,'action':item['action']})
            continue
        if item['action']!='ADD_DIGITIZED_OBSERVATION':raise ValueError('unsupported main figure merge action')
        index=len(rows);base=f'/modules/performance/{index}'
        path=base+'/value';key=add_link(mix,path,item)
        # The standard and 28-day context have been replayed from source prose;
        # reuse source evidence, never a different specimen's measurement value.
        row={'name':item['name'],'value':item['value'],'unit':item['unit'],'age_seconds':item['age_seconds'],
            'specimen':item['owner']['specimen'],'method':item['test_context']['method']['value'],
            'extensions':{'source_kind':'figure_digitized','evidence_key':key,
                'original_value':item['point']['pixel'][1],'original_unit':'image_px_y',
                'extraction_method':'main-figure-reviewed-marker-v1','display_decimal_places':1,
                'digitization':{'selected_stroke_half_span':item['point']['uncertainty_y'],
                    'scope':'selected_pixel_strip_only','includes_axis_calibration_uncertainty':False,
                    'experimental_error_semantics':None}}}
        mix['field_provenance'][path]={'evidence_key':key,'original_value':item['point']['pixel'][1],
            'original_unit':'image_px_y','formula':item['transformation']['formula'],
            'transformation':item['transformation'],'extraction_method':'main-figure-reviewed-marker-v1',
            'confidence':.95,'review_status':'pending_source_path_review'}
        for field in ('age_seconds','specimen','method'):
            prov=copy.deepcopy(item['test_context'][field]['provenance'])
            evidence=copy.deepcopy(item['test_context'][field]['evidence'])
            destination=base+'/'+field
            evidence_key='ev-main-context-'+digest([mix['mix_key'],destination,evidence])[:24]
            if evidence_key in links:raise ValueError('main marker context evidence key collision')
            evidence.update(evidence_key=evidence_key,record_key=mix['mix_key'],field_path=destination)
            evidence.setdefault('extensions',{})['source_context_scope']='common_28_day_mortar_testing'
            prov['evidence_key']=evidence_key
            staged['evidence_links'].append(evidence);links[evidence_key]=evidence
            mix['field_provenance'][destination]=prov
        rows.append(row)
        members.append({'mix_key':mix['mix_key'],'module':'performance','index':index,'action':item['action']})
    staged.setdefault('extensions',{})['main_figure_merge']={'schema_version':1,'plan_sha256':digest(plan),
        'acceptance_sha256':digest(acceptance),'members':members,'counts':plan['counts']}
    records.clear();records.update(staged)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--run',type=Path,required=True)
    parser.add_argument('--write',type=Path,required=True)
    args=parser.parse_args()
    records=markers.load(args.run/'generated-records.json')
    report=markers.load(args.run/'main-marker-assets/candidates.json')
    result=prepare(records,args.run,report)
    _atomic_bytes(args.write,json.dumps(result,sort_keys=True,ensure_ascii=False,indent=2).encode())
    print(json.dumps({'counts':result['counts'],'plan_sha256':digest(result),'scope':result['scope']}))
