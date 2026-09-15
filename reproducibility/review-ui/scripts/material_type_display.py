"""Presentation-only translations; retain extracted type strings unchanged."""
import json,sys
from pathlib import Path
NAMES={
'precursor':'前驱体','solid activator':'固体激发剂','solid activator retarder':'固体激发剂／缓凝剂','mixing water':'拌合水','fine aggregate':'细骨料','coarse aggregate':'粗骨料','reference mortar binder':'对照砂浆胶凝材料','chemical admixture':'化学外加剂','alkaline activator component':'碱激发剂组分','alkali activator component':'碱激发剂组分','literature-comparison waste-derived activator':'文献对照废物基激发剂','aluminosilicate precursor':'铝硅酸盐前驱体','mixing liquid':'拌合液','precursor/binder':'前驱体／胶凝材料','foaming agent/porogen':'发泡剂／造孔剂','precursor solid waste':'固废前驱体','activator component':'激发剂组分','xrd internal standard':'XRD 内标','reference-concrete binder':'对照混凝土胶凝材料','alkaline activator':'碱激发剂','activator mixing water':'激发剂配制用水','prepared activator blend':'配制的复合激发剂','precursor feedstock/raw clay':'前驱体原料／原黏土','post-treatment reagent':'后处理试剂','aluminosilicate precursor; recycled mixed graded aggregate':'铝硅酸盐前驱体；再生混合级配骨料','amorphous-silica-rich supplementary precursor':'富无定形二氧化硅辅助前驱体','activator solvent and added water':'激发剂溶剂及补加水','mineral filler / solid-waste binder constituent':'矿物填料／固废胶凝组分','alkaline solid-waste binder constituent':'碱性固废胶凝组分','liquid activator component':'液体激发剂组分','prepared alkaline activator':'配制的碱激发剂','precursor/filler':'前驱体／填料','alkali-activated precursor':'碱激发前驱体','alkali-activated precursor/calcium source':'碱激发前驱体／钙源','activator':'激发剂','activator solute':'激发剂溶质','activator solids':'激发剂固体组分','combined fine/coarse aggregate':'细、粗骨料组合','ftir pellet diluent':'FTIR 压片稀释剂','siliceous precursor':'硅质前驱体','calcium-rich precursor':'富钙前驱体','alkaline reagent':'碱性试剂','alkaline activator component with suspended residue':'含悬浮残渣的碱激发剂组分','solid extraction residue/filler':'提取固体残渣／填料','class-f low-calcium fly ash precursor':'F 类低钙粉煤灰前驱体','calcium-rich slag precursor':'富钙矿渣前驱体','waste-derived alkaline activator precursor':'废物基碱激发剂前驱体','literature-comparison precursor':'文献对照前驱体','reactive additive':'反应性添加剂','extraction liquid':'提取液','curing gas':'养护气体','cementitious precursor':'胶凝前驱体','silicate activator solution':'硅酸盐激发剂溶液','superplasticizer':'高效减水剂','calorimetry reference':'量热测试参比材料','reaction-arrest solvent':'终止反应溶剂'}

def apply(queue):
    fields=[f for p in queue['papers'] for group in ('mats','mixes') for r in p['records'][group] for f in r['fields']]
    count=0;unknown=[]
    for f in fields:
        if f.get('label')!='材料类型':continue
        raw=str(f.get('value') or '');key=raw.lower().replace('_',' ').strip()
        if key in NAMES:f['displayText']=NAMES[key];count+=1
        else:unknown.append(raw)
    # Classification is not a directly quoted experimental value.
    code='SYSTEM_PROCESSING_NOTE'
    notes=[f for f in fields if f.get('sourceExplanationCode')==code]
    if notes and all(f.get('label')=='材料类型' for f in notes):
        queue['sourceExplanationContract']['templates'][code]='材料分类'
        for f in notes:f['sourceExplanation']='材料分类';f['statusLabel']='材料分类'
    return {'translated':count,'unknown':unknown}

def finalize(site):
    site=Path(site)
    p=site/'review-assets/queue.json';q=json.loads(p.read_bytes());report=apply(q)
    p.write_text(json.dumps(q,ensure_ascii=False,separators=(',',':')),encoding='utf8')
    js=site/'review-full.js'
    text=js.read_text(encoding='utf8').replace("'SYSTEM_PROCESSING_NOTE':'系统处理说明'","'SYSTEM_PROCESSING_NOTE':'材料分类'")
    js.write_text(text,encoding='utf8')
    return report

if __name__=='__main__':print(finalize(sys.argv[1]))
