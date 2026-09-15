"""Deterministic display projection; retain extracted numeric values and assets."""
import re

TERMS={'initial setting time':'初凝时间','final setting time':'终凝时间','initial setting':'初凝','final setting':'终凝','reference position':'参考峰位','cumulative volume':'累计体积分布','differential volume':'差分体积分布','relative intensity':'相对强度','before dissolution':'溶解前','after dissolution':'溶解后','raw':'原始','weight':'质量','Vaterite':'球霰石','Hydrotalcite':'水滑石','5th percentile':'第5百分位','95th percentile':'第95百分位','Fresh':'新拌','Activator':'激发剂','specimen':'试样'}

def text(value):
    if not value:return value
    for a,b in sorted(TERMS.items(),key=lambda x:-len(x[0])):
        value=re.sub(r'\b'+re.escape(a)+r'\b',b,value,flags=re.I)
    return '；'.join(dict.fromkeys(x.strip() for x in value.split('；') if x.strip()))

def normalize(queue):
    queue['sourceExplanationContract']['templates']['SOURCE_FIGURE_ESTIMATE']='图中估读'
    for p in queue['papers']:
        for curve in p.get('curves',{}).values():
            for key in ('xAxis','yAxis'):
                axis=curve.get(key)
                if axis:axis['displayUnit']=text(axis.get('unit','')).replace('um','μm').replace('% 累计体积分布','累计体积 / %').replace('% 差分体积分布','差分体积 / %')
        page_of={e['evidenceKey']:pg['number'] for pg in p['pages'] for e in pg['evidence']}
        for r in p['records']['mats']+p['records']['mixes']:
            identity=next((f for f in r['fields'] if f['label'] in ('文献材料 ID','材料名称','试样') and f.get('value')),None)
            if identity:r['displayName']=identity.get('displayText') or str(identity['value'])
            for f in r['fields']:
                for k in ('observationContext','observationCategory','sourceBasis'):f[k]=text(f.get(k))
                figure=bool(f.get('curveKey') or f.get('digitization') or (f.get('approximate') and re.search('chart|figure',f.get('evidenceKey',''),re.I)))
                if figure:
                    f['sourceExplanationCode']='SOURCE_FIGURE_ESTIMATE';f['sourceExplanation']='图中估读'
                    raw=str(f.get('value',''))
                    if re.fullmatch(r'(?:约|≈)\s*-?\d+(?:\.\d+)?',raw):
                        n=float(re.sub(r'^(约|≈)\s*','',raw));f['displayText']='约 '+format(n,'.3g')
                if figure and re.search(r'粒径 D(10|50|90)',f.get('displayLabel','')):
                    candidates=[]
                    for g in r['fields']:
                        c=p.get('curves',{}).get(g.get('curveKey'))
                        if not c:continue
                        if re.search('differential|reported_volume_density|差分',str(g.get('_fieldPath'))+' '+str(g.get('label')),re.I):continue
                        axis=c.get('yAxis') or {}
                        description=' '.join(str(x) for x in (g.get('_fieldPath'),g.get('label'),axis.get('name'),axis.get('unit')))
                        if 'particle_size_distribution/points_asset_key' not in (g.get('_fieldPath') or '') and not re.search('cumulative|累计',description,re.I):continue
                        if page_of.get(g.get('evidenceKey'))!=page_of.get(f.get('evidenceKey')):continue
                        candidates.append(g)
                    if len(candidates)==1:f['relatedCurveKey']=candidates[0]['curveKey']
    return queue
