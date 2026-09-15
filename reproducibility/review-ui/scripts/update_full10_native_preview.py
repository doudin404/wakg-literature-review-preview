"""Publish the latest existing ten-paper results through the original review UI."""
import argparse
import copy
import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from source_quantity_producer import atomic_json
from prepare_review_bundle import mat_card, mix_card, field, PARAMETER_DISPLAY_NAMES, PERFORMANCE_DISPLAY_NAMES
from native_chart_review import materialize
from update_native_preview import update
from review_text import apply_review_text, reviewer_text, unresolved_reviewer_terms

SHORT_NAMES = {
    'initial_setting_time':'初凝时间', 'final_setting_time':'终凝时间',
    'porosity':'孔隙率', 'open_porosity':'开口孔隙率', 'total_porosity':'总孔隙率',
    'flow_spread':'流动扩展度', 'flow_diameter_relative_increase':'流动直径增幅',
    'elastic_modulus':'弹性模量', 'thermal_conductivity':'导热系数',
    'specific_gravity':'比重', 'water_content':'含水量', 'loss_on_ignition':'烧失量',
    'mass_loss':'质量损失', 'relative_volume_increase':'体积增幅',
    'pore_solution_pH':'孔溶液 pH', 'pore_size':'孔径',
    'maximum_heat_flow':'最大热流', 'surface_roughness':'表面粗糙度',
    'matrix_evaluation_index':'基体评价指数', 'bleeding_rate':'泌水率',
    'average_particle_size':'平均粒径', 'maximum_particle_size':'最大粒径',
    'final_setting_time_relative_reduction':'终凝时间降幅',
    'initial_setting_time_relative_reduction':'初凝时间降幅',
    'compressive_strength_increase':'抗压强度增量',
    'compressive_strength_decline':'抗压强度降幅',
    'slump_relative_decrease':'坍落度降幅', 'slump_absolute_decrease':'坍落度减少量',
    'viscosity_relative_reduction':'黏度降幅',
    'specific_surface_area_relative_increase':'比表面积增幅',
    'fracture_toughness_relative_change':'断裂韧性相对变化',
    'compressive_strength_relative_increase':'抗压强度增幅',
    'fracture_energy':'断裂能', 'fracture_toughness':'断裂韧性',
    'carbon_emission_min':'碳排放下限', 'carbon_emission_max':'碳排放上限',
    'carbon_emission_index_min':'碳排放指数下限', 'carbon_emission_index_max':'碳排放指数上限',
    'material_cost':'材料成本', 'comprehensive_cost_index':'综合成本指数',
    'specific_surface_area':'比表面积', 'storage_modulus_G_prime':'储能模量',
    'particle_size_cumulative_volume_fraction':'累计粒径分布',
    'particle_size_differential_volume_fraction':'差分粒径分布',
    'particle_size_distribution':'粒径分布',
    'particle_size_distribution_mode':'粒径分布峰值',
    'XRD_relative_intensity':'XRD 相对强度曲线',
    'XRD_phase_peak_position':'XRD 物相峰位',
    'XRD_amorphous_hump_center':'XRD 无定形峰中心',
}

def presentation(card, record):
    for f in card['fields']:
        if f.get('observationCategory'):
            f['observationCategory']=reviewer_text(f['observationCategory']).replace('Mullite','莫来石').replace('Quartz','石英')
        pieces=f['label'].split(' · ')
        base=pieces[0]
        f['displayLabel']=SHORT_NAMES.get(base.replace(' ','_'),base.replace('_',' '))
        if len(pieces)>1 and base!='QXRD':
            f['observationContext']='；'.join(pieces[1:]+([f['observationContext']] if f.get('observationContext') else []))
        elif base=='QXRD':
            f['displayLabel']=f['label'].replace('amorphous_phases','无定形相').replace('quartz','石英').replace('kaolinite','高岭石')
        match=re.match(r'/modules/(performance|characterizations)/(\d+)/',f.get('_fieldPath',''))
        if match:
            group,index=match.group(1),int(match.group(2))
            obs=record['modules'][group][index];name=obs.get('name') or '性能'
            ext=obs.get('extensions') or {}
            qualifier=ext.get('quantity_qualifier')
            identity=json.dumps(obs,sort_keys=True,ensure_ascii=False)
            f['_observationIdentity']=identity
            if re.search(r'range across|range over|reported range after|reported collective range',ext.get('reported_basis') or '',re.I):
                f['_collectiveKey']=identity
            if f.get('semanticRole')!='performance_age' and obs.get('value') is None and qualifier:
                # Preserve the original quantity rather than invent a point estimate.
                text=str(qualifier.get('reported_text') or ext.get('original_value') or '')
                text=re.sub(r'\b(?:approximately|around|about|near)\s*','约 ',text,flags=re.I)
                text=text.replace('~','约 ').replace('>=','≥').replace('<=','≤')
                text=re.sub(r'^below\s+','<',text,flags=re.I)
                if qualifier.get('relation')=='above_instrument_range':text='超量程（原表 max）'
                f.update(value=text,unit=qualifier.get('original_unit'),originalValue=qualifier.get('reported_text'),originalUnit=qualifier.get('original_unit'),sourceExplanationCode='DIRECT_SOURCE_VALUE',sourceExplanation='原文直接报告',sourceFormula=None,transformation=None)
                f['_quantityQualifier']=qualifier
            normalized_name=name.casefold().replace(' ','_')
            f['displayLabel']=SHORT_NAMES.get(name,SHORT_NAMES.get(normalized_name,PERFORMANCE_DISPLAY_NAMES.get(normalized_name,name.replace('_',' '))))
            f['observationContext']='；'.join(f'{label}：{obs[key]}' for key,label in [('specimen','试件'),('method','方法')] if obs.get(key))
            if f.get('semanticRole')=='performance_age':
                f['displayPrefix']='龄期：'
                age=obs.get('age_seconds')
                if isinstance(age,(int,float)) and age%86400==0:
                    f['displayText']=f'{age/86400:g}';f['displayUnit']='d'
            else:
                basis=(obs.get('extensions') or {}).get('reported_basis')
                if basis:
                    for key,label in [('relative increase over ','相对增加，基准：'),('relative decrease versus ','相对降低，基准：')]:basis=basis.replace(key,label)
                    basis=re.sub(r'MIX_M(\d)(\d)_B(\d)_M\b',r'M\1.\2-B\3',basis)
                    f['sourceBasis']=basis
        if f['label']=='养护条件' and f.get('value'):
            text=f['value'].replace('。；','；')
            text=re.sub(r'阶段 (\d+)：方式：',r'\1. ',text)
            text=text.replace('；时长：','，').replace('；温度：','，').replace('；湿度：','，')
            text=text.replace('；温度和湿度未报告。','').replace('；温度和湿度未报告','')
            text=text.replace('至试验龄期，至测试龄期','至测试龄期')
            text=re.sub(r'；(\d+\. )',r'\n\1',text)
            f['displayText']=text
    return apply_review_text(card)


def display_record(record):
    """Expose a legacy duration label's recorded unit without altering source data."""
    result = copy.deepcopy(record)
    stages = result.get('modules', {}).get('mixing_curing', {}).get('curing_stages', [])
    for i, stage in enumerate(stages):
        ext = stage.get('extensions', {})
        if isinstance(ext.get('duration_label'), (int, float)):
            receipt = result.get('field_provenance', {}).get(
                f'/modules/mixing_curing/curing_stages/{i}/extensions/duration_label', {})
            if receipt.get('original_unit'):
                ext['duration_unit'] = receipt['original_unit']
    for group in ('performance', 'characterizations'):
        for i, observation in enumerate(result.get('modules', {}).get(group, [])):
            receipt = result.get('field_provenance', {}).get(f'/modules/{group}/{i}/age_seconds', {})
            age=observation.get('age_seconds');original=receipt.get('original_value')
            scale={'day':86400,'days':86400,'d':86400,'h':3600,'s':1}.get(receipt.get('original_unit'))
            if isinstance(age,(int,float)) and isinstance(original,(int,float)) and scale and abs(age-original*scale)>1e-6:
                observation['age_seconds']=f"{age:g} s（来源记录：{original:g} {receipt['original_unit']}，待核对）"
                continue
            if isinstance(observation.get('age_seconds'), str):
                receipt = result.get('field_provenance', {}).get(f'/modules/{group}/{i}/age_seconds', {})
                text = str(receipt.get('original_value') or observation['age_seconds'])
                unit = receipt.get('original_unit') or ''
                text = text.replace('approximately ', '约 ').replace('around ', '约 ').replace('~', '约 ').replace(' and ', '、')
                observation['age_seconds'] = f'{text} {unit}'.strip()
    return result


def native_card(record, factory):
    """Keep native material observations and preparation fields in the main table."""
    card = factory(display_record(record))
    xrf = record.get('xrf_composition') or {}
    qxrd=record.get('xrd_qxrd') or {}
    if qxrd.get('amorphous_total_percent') is not None:
        card['fields']=[f for f in card['fields'] if not (
            f.get('semanticRole')=='qxrd_phase_fraction' and
            'amorphous' in f['label'].casefold() and
            str(f.get('value'))==str(qxrd['amorphous_total_percent']))]
    for f in card['fields']:
        if f['label']=='QXRD 内标占掺标混合物':f['label']='QXRD 内标质量分数'
        if f['label']=='QXRD 标样方式' and f.get('value')=='internal':f['value']='内标法'
    if xrf.get('rows'):
        card['fields'] = [f for f in card['fields'] if f['label'] != 'XRF 原始合计']
        components = {row['component'] for row in xrf['rows']}
        for f in card['fields']:
            if f['label'] in components:
                f['xrfReview'] = xrf
    ext = record.get('extensions') or {}
    provenance = record.get('field_provenance') or {}

    def append(label, value, unit, path, context=None):
        receipt = provenance.get(path) or {}
        if value is None or not receipt.get('evidence_key'):
            return
        if any(f.get('_fieldPath') == path for f in card['fields']):
            return
        label = PARAMETER_DISPLAY_NAMES.get(label, PERFORMANCE_DISPLAY_NAMES.get(label.casefold(), label.replace('_', ' ')))
        if isinstance(value, str):
            value = value.replace('approximately ', '约 ').replace('about ', '约 ').replace('around ', '约 ').replace('~', '约 ')
            if unit and value.endswith(unit):
                value = value[:-len(unit)].rstrip()
        item = field(label, value, unit, receipt, status='reported')
        item['_fieldPath'] = path
        item['observationContext'] = context
        card['fields'].append(item)

    for name, value in ext.get('reported_properties', {}).items():
        if name == 'loss_on_ignition_percent' and any(row['component'] == 'LOI' for row in xrf.get('rows', [])):
            continue
        path = '/extensions/reported_properties/' + name
        unit = 's' if name.endswith('_seconds') else provenance.get(path, {}).get('original_unit')
        append(name, value, unit, path)
    for i, observation in enumerate(ext.get('reported_observations', [])):
        definition = observation.get('field') or {}
        append(definition.get('name') or '原文观测', observation.get('value'), observation.get('unit'),
               f'/extensions/reported_observations/{i}/value',
               '；'.join(str(definition[k]) for k in ('basis', 'specimen', 'method') if definition.get(k)))
    for i, assignment in enumerate(ext.get('spectral_assignments', [])):
        append(f"{assignment.get('method') or '光谱'} 波数", assignment.get('wavenumber'), assignment.get('unit'),
               f'/extensions/spectral_assignments/{i}/wavenumber', assignment.get('assignment'))
    curing = record.get('modules', {}).get('mixing_curing', {})
    for name, label in [('mixing', '搅拌方法'), ('forming', '成型方法'), ('age_origin', '龄期起点')]:
        append(label, curing.get(name), None, '/modules/mixing_curing/' + name)
    return presentation(card, record)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('site', type=Path)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--data-root', type=Path,
                        help='Build the web projection from records.json files in this one directory')
    source.add_argument('--projection-root', type=Path,
                        help='Publish an already materialized, self-contained web projection')
    parser.add_argument('--only', nargs='+', help='Only update papers whose DOI contains these values')
    args = parser.parse_args()
    args.site = args.site.resolve()
    if args.projection_root:
        projection_root = args.projection_root.resolve()
        frozen_queue = projection_root / 'review-assets/queue.json'
        if args.only:
            parser.error('--only is available with --data-root, not with a fixed projection')
        queue = json.loads(frozen_queue.read_bytes())
        shutil.copy2(frozen_queue, args.site / 'review-assets/queue.json')
        frozen_curves = projection_root / 'review-assets/resume-curves'
        destination = args.site / 'review-assets/resume-curves'
        destination.mkdir(parents=True, exist_ok=True)
        for asset in frozen_curves.iterdir():
            if asset.is_file():
                shutil.copy2(asset, destination / asset.name)
        print(json.dumps({'sourceMode':'fixed_projection','source':str(projection_root),
                          'papers':len(queue['papers']),
                          'mats':sum(len(p['records']['mats']) for p in queue['papers']),
                          'mixes':sum(len(p['records']['mixes']) for p in queue['papers']),
                          'fields':sum(len(r['fields']) for p in queue['papers'] for group in ('mats','mixes') for r in p['records'][group]),
                          'curves':sum(len(p.get('curves',{})) for p in queue['papers'])},ensure_ascii=False))
        return

    data_root = args.data_root.resolve()
    output = ROOT / 'internal_assets/full10-web-current-20260914'
    report = []
    for paper_dir in sorted(data_root.glob('1-s2.0-*-main')):
        source_path = next((paper_dir / name for name in ('records.json', 'generated-records.json')
                            if (paper_dir / name).is_file()), None)
        if source_path is None:
            continue
        data = json.loads(source_path.read_bytes())
        if args.only and not any(value in data['papers'][0]['doi'] for value in args.only):
            continue
        folder = output / paper_dir.name
        cards = {kind: [native_card(record, factory) for record in data[kind]]
                 for kind, factory in [('mats', mat_card), ('mixes', mix_card)]}
        curves = materialize(ROOT, data, cards, folder / 'native-curves', 'native-curves')
        for curve in curves.values():
            parts=curve['label'].split(' · ')
            curve['displayLabel']=' · '.join(reviewer_text(p) for p in parts)
        atomic_json(folder / 'generated-records.json', data)
        atomic_json(folder / 'review-cards.json', cards)
        atomic_json(folder / 'curves.json', curves)
        update(folder, args.site)
        row = json.loads((folder / 'web-projection-report.json').read_bytes())
        row['unresolvedDisplayTerms']=unresolved_reviewer_terms(cards)
        row.update(doi=data['papers'][0]['doi'], source=str(source_path), sourceMode='single_data_root')
        report.append(row)
        atomic_json(output / 'report.json', report)
    print(json.dumps({'papers': len(report), 'mats': sum(r['mats'] for r in report),
                      'mixes': sum(r['mixes'] for r in report),
                      'fields': sum(r['fields'] for r in report),
                      'curves': sum(r['curves'] for r in report)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
