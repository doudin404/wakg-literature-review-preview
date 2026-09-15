"""Chinese review copy. Original scientific fields and source text remain intact."""
import re

_LABELS = '''Others|其他
carbon emission factor|碳排放因子
loss on ignition percent|烧失量
price|价格
sodium silicate modulus|硅酸钠模数
Borax|硼砂
Water|水
C-A-S-H Ca to Si ratio|C-A-S-H 钙硅比
H2O content wt percent|水含量
Na2O content wt percent|Na₂O 含量
SiO2 content wt percent|SiO₂ 含量
SiO2/Na2O ratio|SiO₂/Na₂O 比
preparation|制备方法
alkaline content percent|碱含量
chloride ion content percent|氯离子含量
specific gravity 25 C|比重（25 °C）
moisture condition|含水状态
nominal maximum size mm|公称最大粒径
AL/B ratio|碱激发溶液/胶凝材料比
Molarity of SH|氢氧化钠溶液摩尔浓度
SS/SH ratio|硅酸钠溶液/氢氧化钠溶液比
Sand|砂
total binder content|胶凝材料总用量
10 mm coarse aggregate volume fraction|10 mm 粗骨料体积分数
20 mm coarse aggregate volume fraction|20 mm 粗骨料体积分数
fine aggregate volume fraction|细骨料体积分数
SPs content|减水剂用量
alkaline solution silica modulus|碱溶液硅模数
XRD reflection position|XRD 衍射峰位置
XRD relative-intensity trace|XRD 相对强度曲线
DRIFT spectrum|漫反射红外光谱（DRIFT）
DRIFT band position|DRIFT 吸收带位置
27Al chemical shift|²⁷Al 化学位移
27Al MAS-NMR spectrum|²⁷Al 魔角旋转核磁共振谱
drying mass loss percent|干燥失重
Al(VI) chemical shift|六配位铝化学位移
27Al MAS-NMR peak chemical shift|²⁷Al MAS-NMR 峰化学位移
27Al MAS-NMR relative-intensity spectrum|²⁷Al MAS-NMR 相对强度谱
27Al NMR peak chemical shift|²⁷Al NMR 峰化学位移
nmr|核磁共振谱（NMR）
FTIR spectrum, vertically offset relative intensity|FTIR 相对强度谱（纵向平移）
XRD phase-reflection position|XRD 物相衍射峰位置
XRD relative intensity versus 2θ|XRD 相对强度–2θ 曲线
calcination heating rate C min|煅烧升温速率
calcination mass loss percent|煅烧失重
calcination temperature C|煅烧温度
chemical shift|化学位移
relative proportion|相对比例
amorphous hump range|无定形弥散峰范围
quartz reflection|石英衍射峰
Al(IV) chemical shift|四配位铝化学位移
Al(V) chemical shift|五配位铝化学位移
amorphous hump shift|无定形弥散峰位移
SiO2 content|SiO₂ 含量
Na2O content|Na₂O 含量
H2O content|水含量
K2O content|K₂O 含量
stock concentration|原液浓度
maximum particle size um|最大粒径
Alkali ratio X2O/ SiO2:H2O/ X2O|碱配比（X₂O/SiO₂ : H₂O/X₂O）
H2O2 as foaming agent (mass-%)|H₂O₂ 发泡剂掺量
BET specific surface area (SSA)|BET 比表面积
foaming agent dosage range|发泡剂掺量范围
Alkali ratio X2O/ SiO2: H2O/X2O|碱配比（X₂O/SiO₂ : H₂O/X₂O）
compressive strength ratio|抗压强度比
Al(V) signal at 29 ppm|五配位铝信号（29 ppm）
Al(VI) signal at 2 7 ppm|六配位铝信号（2.7 ppm）
post washing drying|洗涤后干燥
reflux acid washing|回流酸洗
shaker acid washing|振荡酸洗
viscosity measurement delay after stirring|搅拌后至黏度测量的间隔
as received particle size range|原样粒径范围
milled particle size limit|研磨后粒径上限
purity|纯度
NaOH solution concentration|NaOH 溶液浓度
activator pH|激发剂 pH
apparent open porosity|表观开口孔隙率
thermal conductivity series range|系列导热系数范围
rectangular specimens fabricated per mix|每组配比制备的矩形试件数
flexural strength fraction of 28 day value|抗折强度与 28 d 值之比
reported density|密度（原文未细分类型）
silicate modulus|硅酸盐模数
purity percent|纯度
water type|用水类型
GGBS replacement ratio|矿渣替代比例
Na2O dosage|Na₂O 掺量
xrd peak position|XRD 峰位置
xrd diffuse hump range|XRD 弥散峰范围
characteristic thermal peak temperature|特征热峰温度
endothermic peak temperature range|吸热峰温度范围
total mercury intrusion volume|总压汞体积
pore throat volume fraction below 10 nm|小于 10 nm 孔喉体积分数
activator modulus|激发剂模数
low temperature mass loss|低温失重
CH related mass loss|氢氧化钙相关失重
total pore surface area|总孔表面积
EDS Al content|EDS 铝含量
EDS Si content|EDS 硅含量
EDS Ca content|EDS 钙含量
fine pore volume below 10 nm|小于 10 nm 细孔体积
coarse pore volume above 100 nm|大于 100 nm 粗孔体积
median pore throat diameter D50|孔喉中位径 D50
average pore throat diameter|平均孔喉直径
CO2 emission factor|CO₂ 排放因子
grinding particle size limit mm|研磨粒径上限
total CO2 emissions|CO₂ 总排放量
total price|总价格
transport distance km|运输距离
unit price|单价
calcination duration seconds|煅烧时长
reactive Al2O3 wt percent|活性 Al₂O₃ 含量
reactive SiO2 wt percent|活性 SiO₂ 含量
H2O wt percent|水含量
Na2O wt percent|Na₂O 含量
SiO2 wt percent|SiO₂ 含量
NaOH wt percent|NaOH 含量
preparation lead time seconds|提前制备时间
sodium silicate solution to NaOH solution mass ratio|硅酸钠溶液/NaOH 溶液质量比
mortar aggregate maximum particle size|砂浆骨料最大粒径
aggregate fraction 0 5 mm|0–5 mm 骨料比例
aggregate fraction 5 10 mm|5–10 mm 骨料比例
aggregate fraction 10 20 mm|10–20 mm 骨料比例
electricity CO2 emission factor|电力 CO₂ 排放因子
electricity unit price|电价
coal CO2 emission factor|煤炭 CO₂ 排放因子
coal energy unit price|煤炭能源单价
chemically bound water|化学结合水
unreacted water|未反应水
unreacted water volume|未反应水体积
large capillary porosity|大毛细孔孔隙率
oven dried density kg m3|烘干密度
Calcined soil|煅烧土
Uncalcined soil|未煅烧土
Sodium silicate solution|硅酸钠溶液
Water in NaOH solution|NaOH 溶液中的水
Aggregate|骨料
CO2 emissions|CO₂ 排放量
cost|成本
CO2 emissions reduction|CO₂ 减排量
Cement|水泥
Fly ash|粉煤灰
TGA final weight loss percent|热重分析最终失重
water content wt percent|含水量
Newtonian viscosity|牛顿黏度
NaOH concentration|NaOH 浓度
RHA dissolution efficiency|稻壳灰溶解效率
dissolved RHA mass|已溶解稻壳灰质量
NaOH pellets|NaOH 颗粒
water|水
other oxides wt percent|其他氧化物含量
unburnt carbon wt percent|未燃碳含量
undissolved residue mass|未溶解残渣质量
Commercial SS|商用硅酸钠溶液
RHA-derived SS — Filtered|稻壳灰制硅酸钠溶液（过滤）
RHA-derived SS — Unfiltered|稻壳灰制硅酸钠溶液（未过滤）
Add. water|额外加水
Liquid/binder|液体/胶凝材料比
Na2O/binder|Na₂O/胶凝材料比
GGBFS content|矿渣用量
LVED critical strain|线性黏弹性域临界应变
onset of rigidification|刚化起点
P wave detection delay|P 波检出延迟
P wave velocity|P 波波速
particle shape|颗粒形状
SEM EDS Si Al ratio|SEM-EDS 硅铝比
SEM EDS Ca Si ratio|SEM-EDS 钙硅比
Slag:FA|矿渣:粉煤灰
FGR/Precursor|烟气残渣/前驱体比
FGR-activator solution/precursor|烟气残渣激发溶液/前驱体比
Na2O/Precursor|Na₂O/前驱体比
FGR/deionized water|烟气残渣/去离子水比
alkali content (Na2O)|碱含量（Na₂O）
mean|均值
minimum|最小值
first quartile|第一四分位数
median|中位数
third quartile|第三四分位数
maximum|最大值
Tukey group|Tukey 检验分组
N-A-S-H phase volume|N-A-S-H 相体积
N–C-A-S-H phase volume|N–C-A-S-H 相体积
C-A-S-H phase volume|C-A-S-H 相体积
unreacted phases volume|未反应相体积
mass loss 40 160 C|40–160 °C 失重
reaction degree indicator mass loss up to 600 C|反应程度指标（截至 600 °C 的失重）
porosity reduction|孔隙率降幅
SiO2 Na2O molar ratio|SiO₂/Na₂O 摩尔比
MgO purity|MgO 纯度
calcination temperature range|煅烧温度范围
Precursor|前驱体
Activator|激发剂
Additive|添加剂
Water to Precursor Ratio|水/前驱体比
Activator to Precursor Ratio|激发剂/前驱体比
Additive to Precursor Ratio|添加剂/前驱体比
Na2O Equivalent*|Na₂O 当量*
CaO/SiO2 Ratio|CaO/SiO₂ 比
MgO/SiO2 Ratio|MgO/SiO₂ 比
weight loss 30 250 C|30–250 °C 失重
weight loss 250 400 C|250–400 °C 失重
weight loss 400 600 C|400–600 °C 失重
weight loss 600 1000 C|600–1000 °C 失重
cubic clustering criterion|立方聚类准则
hardness|硬度
Score Mean Difference|得分均值差
p-Value|p 值
Hodges-Lehmann|Hodges–Lehmann 估计量
Lower CL|置信下限
Upper CL|置信上限
minimum observed pore solution pH|观测到的最低孔溶液 pH
C N A S H cluster member fraction|C-(N)-A-S-H 聚类成员比例
mixture type|混合物类型
calcium carbonate uptake|碳酸钙吸收量
xrf other mass percent|XRF 其他组分质量分数
na2o content|Na₂O 含量
sio2 content|SiO₂ 含量
effective solid content|有效固体含量
fineness modulus|细度模数
acceleration peak onset|加速峰起点
Extra water|额外加水
acceleration stage onset|加速阶段起点
efs dissolved to added|电炉渣溶解量/加入量比
pc dissolved to added|硅酸盐水泥溶解量/加入量比
Ca to Si atomic ratio|Ca/Si 原子比
Al to Si atomic ratio|Al/Si 原子比
Na to Si atomic ratio|Na/Si 原子比
Mg to Al atomic ratio|Mg/Al 原子比
twoCa twoMg K Na to Si atomic ratio range|(2Ca+2Mg+K+Na)/Si 原子比范围
Al to Si atomic ratio range|Al/Si 原子比范围
small gel pore size|小凝胶孔孔径
large gel pore size range|大凝胶孔孔径范围
capillary pore size|毛细孔孔径
compressive strength reduction relative to M1|相对 M1 的抗压强度降幅
penetration yield stress|贯入屈服应力
v funnel discharging time test 1|V 漏斗流出时间（试验 1）
v funnel discharging time test 2|V 漏斗流出时间（试验 2）
v funnel discharging time test 3|V 漏斗流出时间（试验 3）
printed column height|打印柱高度'''
LABELS = dict(line.split('|', 1) for line in _LABELS.splitlines())
LABELS.update(dict(line.split('|',1) for line in '''GGBS|粒化高炉矿渣粉（GGBS）
GGBFS|粒化高炉矿渣粉（GGBFS）
FA|粉煤灰（FA）
QS#1|石英砂 1（QS#1）
QS#2|石英砂 2（QS#2）
pH 25 C|pH（25 °C）
FA/GGBS|粉煤灰/矿渣比
CA 10 mm|10 mm 粗骨料
CA 20 mm|20 mm 粗骨料
LOI|烧失量（LOI）
CDW|建筑拆除废弃物（CDW）
RHA|稻壳灰（RHA）
w/b|水胶比
W/B|水胶比
Ms|激发剂模数（Ms）
Na2O (%)|Na₂O 含量
LP|石灰石粉（LP）
CCR|电石渣（CCR）
% GGBFS|矿渣比例（GGBFS）
NaOH 10 M|10 M NaOH 溶液
w/s|水固比
SEM EDS Na|SEM-EDS 钠含量
SEM EDS Al|SEM-EDS 铝含量
SEM EDS Si|SEM-EDS 硅含量
SEM EDS Ca|SEM-EDS 钙含量
Std Err Dif|差值标准误
Z|Z 统计量
BFS|高炉矿渣（BFS）
PC|硅酸盐水泥（PC）
EFS|电炉渣（EFS）
PNS|聚萘磺酸盐（PNS）'''.splitlines()))
PHASES = dict(line.split('|',1) for line in '''total_crystalline_phases|总晶相
amorphous_phase|无定形相
amorphous phase|无定形相
amorphous SiO2|无定形 SiO₂
crystalline SiO2|晶态 SiO₂
crystalline phases|晶相
amorphous silica|无定形二氧化硅
crystalline silica|晶态二氧化硅
crystalline phases (石英 and cristobalite)|晶相（石英与方石英）
Alite|阿利特
Belite|贝利特
Aluminate|铝酸盐相
Ferrite|铁铝酸盐相
Anhydrite|硬石膏
Calcite|方解石
Åkermanite|镁黄长石
Merwinite|镁硅钙石
Chromite|铬铁矿
Wollastonite|硅灰石
Amorphous and poorly crystalline phases|无定形及低结晶相'''.splitlines())

LABELS.update(dict(line.split('|',1) for line in '''dynamic elastic modulus|动弹性模量
splitting tensile strength|劈裂抗拉强度
P-wave velocity|纵波波速
XRD diffraction peak position|XRD 衍射峰位置
XRD peak position|XRD 峰位置
static yield stress|静态屈服应力
three-point bending P-CMOD curve|三点弯曲荷载–裂缝口张开位移曲线
water absorption|吸水率
Real Density|真密度
FTIR band position|FTIR 吸收带位置
phase angle δ|相位角 δ
storage modulus G′|储能模量 G′
loss modulus G″|损耗模量 G″
paste flowability|浆体流动度
flow value|流动度
slump value|坍落度
curve|曲线
ftir|傅里叶变换红外光谱（FTIR）
FTIR relative intensity spectrum|FTIR 相对强度光谱
mass remaining|剩余质量
slag content|矿渣含量
sodium hydroxide solution molarity|氢氧化钠溶液摩尔浓度
sodium silicate-to-sodium hydroxide ratio|硅酸钠/氢氧化钠比
alkaline activator-to-binder ratio|碱激发溶液/胶凝材料比
dynamic viscosity|动力黏度
shear stress versus shear rate|剪应力–剪切速率曲线
oscillation strain selected for SAOS|小振幅振荡剪切选用应变
XRD intensity versus 2theta|XRD 强度–2θ 曲线
cumulative mass loss|累计失重
mass-loss rate (DTG)|失重速率（DTG）
reported-volume-density mode|体积分布峰值
OOXML chart data|内嵌图表数据
XRD phase-marker position|XRD 物相标记位置
calcium-to-silicon ratio (Ca/Si)|钙硅比（Ca/Si）
reaction degree|反应程度
pore volume, <10 nm|孔体积（小于 10 nm）
pore volume, 10-20 nm|孔体积（10–20 nm）
pore volume, 20-50 nm|孔体积（20–50 nm）
pore volume, 50-200 nm|孔体积（50–200 nm）
pore volume, >200 nm|孔体积（大于 200 nm）
total pore volume|总孔体积
EDS peak energy|EDS 峰能量'''.splitlines()))

def label_text(text):
    if text in LABELS:
        return LABELS[text]
    if text.startswith('QXRD · '):
        phase=text[7:]
        return 'QXRD · '+PHASES.get(phase,phase)
    match=re.fullmatch(r'(total )?([A-Z][a-z]?) concentration',text)
    if match:
        return f'{match[2]} '+('总浓度' if match[1] else '浓度')
    return text

_DISPLAY_EXACT = {
    'scatter':'散点/曲线数据', 'line':'曲线数据', 'xrd':'XRD 衍射曲线',
    'psd':'粒径分布曲线', 'sem scale bar':'SEM 标尺长度',
    'particle size d50':'中位粒径 D50', 'rietveld crystalline phase composition':'Rietveld 定量晶相组成',
    'bet specific surface area':'BET 比表面积', 'bet ssa':'BET 比表面积',
    'volume increase':'体积增长率', 'yield stress':'屈服应力',
    'fesem morphology':'FESEM 形貌', 'elastic modulus centroid':'弹性模量聚类中心',
    'hardness centroid':'硬度聚类中心', 'pore volume':'孔体积',
    'peak cmod':'峰值 CMOD', 'peak load':'峰值荷载',
    'fracture toughness (km)':'断裂韧度 Kₘ', 'ca/si mean':'Ca/Si 平均值',
    'stinitial':'初凝时间', 'stfinal':'终凝时间', 'flowability':'流动度',
    'fluidity':'流动度', 'bulk density':'堆积密度', 'real density':'真密度',
    'apparent porosity':'表观孔隙率', 'storage modulus':'储能模量',
    'heat flow':'热流', 'peak temperature':'峰值温度',
    'ftir peak position':'FTIR 峰位', 'eds peak position':'EDS 峰位',
    'eds spectrum':'EDS 能谱', 'sem-eds spectrum':'SEM–EDS 能谱',
    'sem–eds map-sum spectrum':'SEM–EDS 面扫总谱',
    'nitrogen adsorption desorption isotherm':'氮气吸附–脱附等温线',
    'median pore diameter d50':'中位孔径 D50', 'average pore diameter':'平均孔径',
    'leaching concentration':'浸出浓度', 'total alkali content':'总碱含量',
    'ggbs to fa mass ratio':'GGBS/FA 质量比', 'water to binder ratio':'水胶比',
    'ggbs content':'GGBS 含量', 'alkaline solution modulus':'碱性溶液模数',
    'calcined soil content':'煅烧土含量', 'phase mass fraction':'物相质量分数',
    'chemical composition':'化学组成', 'precursors':'前驱体', 'other':'其他',
}

_DISPLAY_PARTS = (
    (r'particle size D50|D50 particle size', '中位粒径 D50'),
    (r'SEM scale bar', 'SEM 标尺长度'), (r'scale 条形试件', 'SEM 图像'),
    (r'Experimental results', '试验结果'), (r'fracture surface', '断口'),
    (r'cumulative', '累计分布'), (r'versus', '与'),
    (r'peak position', '峰位'), (r'peak intensity', '峰强度'),
    (r'compressive strength', '抗压强度'), (r'flexural strength', '抗折强度'),
    (r'total porosity', '总孔隙率'), (r'flow diameter', '流动直径'),
    (r'setting time', '凝结时间'), (r'reaction degree', '反应程度'),
    (r'mass ratio', '质量比'), (r'EDX point', 'EDX 测点'),
    (r'EDS region', 'EDS 区域'), (r'main cluster', '主聚类'),
    (r'cluster', '聚类'), (r'unhydrated particles', '未水化颗粒'),
    (r'magnification', '放大倍数'), (r'terminal heat flow', '终点热流'),
    (r'endothermic peak temperature', '吸热峰温度'), (r'principal DTG', '主 DTG'),
    (r'legend boundary', '图例边界'), (r'bleeding rate', '泌水率'),
    (r'Flow value', '流动值'), (r'Slump value', '坍落度'),
    (r'total cost', '总成本'), (r'normalized cost', '归一化成本'),
    (r'normalized CO2 emissions', '归一化 CO₂ 排放量'), (r'microcracks', '微裂缝'),
    (r'yield stress', '屈服应力'), (r'plastic viscosity', '塑性黏度'),
    (r'Map Sum Spectrum', '面扫总谱'), (r'Spectrum', '能谱'),
    (r'\bred\b', '红色'), (r'\bgreen\b', '绿色'),
    (r'\bDynamic\b', '动态'), (r'\bStatic\b', '静态'),
)

def reviewer_text(text):
    """Convert extractor keys and mechanical composites into reviewer-facing terms."""
    if text is None:return text
    value=str(text).strip()
    if not value:return value
    exact=_DISPLAY_EXACT.get(value.casefold())
    if exact:return exact
    value=description_text(label_text(value)).replace('_',' ')
    for source,target in _DISPLAY_PARTS:
        value=re.sub(r'(?<![A-Za-z])(?:'+source+r')(?![A-Za-z])',target,value,flags=re.I)
    value=re.sub(r'\bage d:\s*(\d+(?:\.\d+)?)',r'\1 d',value,flags=re.I)
    return re.sub(r'\s*·\s*—\s*$','',value).strip()

def unresolved_reviewer_terms(cards):
    """Return new extractor-like terms for one optional batched semantic fallback."""
    terms=set()
    for group in ('mats','mixes'):
        for card in cards.get(group,[]):
            for field in card.get('fields',[]):
                for value in (field.get('displayLabel'),field.get('observationCategory')):
                    if not isinstance(value,str):continue
                    if '_' in value or (re.search(r'[A-Za-z]{4,}\s+[A-Za-z]{4,}',value) and not re.search(r'[\u4e00-\u9fff]',value)):
                        terms.add(value)
    return sorted(terms)

def apply_review_text(card):
    for field in card['fields']:
        field['displayLabel']=reviewer_text(field.get('displayLabel',field['label']))
        if field.get('observationCategory'):
            field['observationCategory']=reviewer_text(field['observationCategory'])
        for key in ('observationContext','sourceBasis'):
            if field.get(key):field[key]=description_text(field[key])
        if field.get('observationContext'):
            field['observationContext']=field['observationContext'].replace('；方法：','\n方法：')
        if field.get('sourceBasis') and not field['sourceBasis'].startswith(('基准：','相对','依据：')):
            field['sourceBasis']='依据：'+field['sourceBasis']
        value=field.get('displayText',field.get('value'))
        if isinstance(value,str):
            translated=value_text(value)
            citation=re.match(r'^(.*?)\((\w+ et al\., \d{4})\)?$',translated)
            if citation:
                translated=citation[1]
                context=field.get('observationContext','')
                field['observationContext']='；'.join(filter(None,[context,'引文：'+citation[2].replace(' et al.',' 等')]))
            if translated!=value:field['displayText']=translated
        if field.get('sourceFormula'):
            field['displayFormula']=formula_text(field['sourceFormula'])
        unit=field.get('displayUnit',field.get('unit'))
        if unit in UNIT_LABELS:field['displayUnit']=UNIT_LABELS[unit]
    return card

_PHRASES = '''minimum calculated material-level carbon emission|计算得到的材料层面碳排放下限
maximum calculated material-level carbon emission|计算得到的材料层面碳排放上限
calculated material cost|计算得到的材料成本
CEmin divided by 28-day compressive strength|CEmin / 28 d 抗压强度
CEmax divided by 28-day compressive strength|CEmax / 28 d 抗压强度
material cost divided by 28-day compressive strength|材料成本 / 28 d 抗压强度
compressive strength divided by squared matrix fracture toughness|抗压强度 / 基体断裂韧性²
range across the NB-content series|不同 NB 掺量系列的范围
high-Ms endpoint of the reported decrease|原文降幅对应的高 Ms 端点
low-Ms endpoint of the reported decrease|原文降幅对应的低 Ms 端点
total intrudable porosity|总可侵入孔隙率
reported range after NB addition; individual mix values remain in Fig. 15|掺入 NB 后的范围；各配比值见图 15
decrease relative to corresponding 10 M mixture|相对对应 10 M 配比的降幅，基准：
decrease relative to|相对降幅，基准：
increase relative to|相对增幅，基准：
reduction relative to|相对降幅，基准：
interpreted as C20 despite source printing B20|原文标为 B20，按上下文解释为 C20
interpreted as C25 despite source printing B25|原文标为 B25，按上下文解释为 C25
initial paste volume|初始浆体体积
1 M acetic acid reflux washing|1 M 乙酸回流洗涤
1 M acetic acid room-temperature shaker washing|1 M 乙酸室温振荡洗涤
60 °C curing stage|60 °C 养护阶段
additional 105 °C curing stage after 24 h at 60 °C|60 °C 养护 24 h 后追加的 105 °C 养护阶段
deconvoluted coordinated-Al peak area|分峰拟合后的配位铝峰面积
range over all nine formulations|全部九组配方的范围
percentage of corresponding 28-day flexural strength|占对应 28 d 抗折强度的百分比
calculated from bulk density and real density|由体积密度与真实密度计算
range across the 80CDW series; individual Ms values not assigned in prose|80CDW 系列的范围；正文未逐一对应 Ms
range across the 60CDW series; individual Ms values not assigned in prose|60CDW 系列的范围；正文未逐一对应 Ms
calcite reflection|方解石衍射峰
poorly crystalline hydration products diffuse hump|低结晶水化产物弥散峰
portlandite (CH) dehydroxylation|氢氧化钙（CH）脱羟基
calcite decomposition|方解石分解
RO phase|RO 相
dicalcium silicate (C2S)|硅酸二钙（C₂S）
other characteristic steel-slag residual phases|其他特征钢渣残余相
f-CaO-bearing high-calcium residual phases|含游离 CaO 的高钙残余相
portlandite (CH)|氢氧化钙（CH）
representative|代表性
reported collective range after GGBS addition|加入矿渣后的整体范围
oven-dried mass; TGA interval as reported in supplement|烘干质量；TGA 温区按补充材料所报
over-dried density × mass loss|烘干密度 × 失重
initially added water − chemically bound water|初始加水量 − 化学结合水
paste volume|浆体体积
final absorbed-water volume/sample volume|最终吸水体积 / 试样体积
water-absorption open porosity|吸水法开口孔隙率
MIP total intrudable porosity|压汞法总可侵入孔隙率
pores >0.5 μm; MIP|大于 0.5 μm 的孔；压汞法
cradle-to-gate, per m3 concrete|从原料获取至出厂；按每 m³ 混凝土计
per m3 concrete|按每 m³ 混凝土计
relative to assumed 55 MPa fly-ash blended cement concrete; sustainability comparison|以假定强度为 55 MPa 的粉煤灰掺合水泥混凝土为基准，比较可持续性
range across all three mixes|全部三组配比的范围
range across the three pastes; individual values not stated in text|三种浆体的范围；正文未逐项给值
elapsed from first activator–precursor contact|从激发剂与前驱体首次接触时起计
elapsed time before first P-wave transmission|首次 P 波透射前的经过时间
approximate common magnitude for all pastes|所有浆体的近似共同量级
approximate common threshold for all pastes|所有浆体的近似共同阈值
reported major-element EDS composition; unit not printed|原文主要元素 EDS 组成；未标单位
reported EDS elemental ratio|原文 EDS 元素比
row-identified SEM-EDS atomic ratio|按表格行对应的 SEM-EDS 原子比
95% confidence ANOVA followed by Tukey grouping; same letter means no significant difference|95% 置信水平的方差分析后进行 Tukey 分组；相同字母表示无显著差异
phase volume from area of Gaussian-deconvolution curves fitted to nanoindentation elastic-modulus distributions|由纳米压痕弹性模量分布的高斯分峰拟合曲线面积得到物相体积
mass loss over 40–160 ◦C|40–160 °C 失重
mass loss up to 600 ◦C, reported as an indirect reaction-degree indicator|截至 600 °C 的失重，作为间接反应程度指标
SPM scan over 50 μm × 50 μm grid area|SPM 扫描区域为 50 μm × 50 μm 网格
average of six cubes|六个立方体试件的平均值
reconstructed micro-CT volume 5 × 5 × 5 mm3|重建微米 CT 体积为 5 × 5 × 5 mm³
final owner corrected contextually from the repeated FGR-0.24 label|原文重复标记 FGR-0.24，最终归属按上下文修正
relative to|基准：
original powder mass|原始粉末质量
semantic interval corrected to 600–1000 °C from prose|根据正文将温区修正为 600–1000 °C
k-means solution with|k-means 聚类方案，簇数：
clusters|簇
mean ± standard deviation of the (C, N)-A-S-H gel cluster|（C, N）-A-S-H 凝胶簇的均值 ± 标准差
paired Wilcoxon signed-rank comparison|配对 Wilcoxon 符号秩比较
first level minus second level|第一个水平减去第二个水平
confidence limit for Hodges-Lehmann difference|Hodges–Lehmann 差值的置信界
hot-water-extracted pore solution|热水提取的孔溶液
all studied CO2-curing stages|全部研究的 CO₂ 养护阶段
red-cluster points divided by total nanoindentation points|红色簇点数 / 纳米压痕总点数
uptake relative to residual non-CaCO3 powder mass, P/(100−P)×100|相对于残余非 CaCO₃ 粉末质量的吸收量，P/(100−P)×100
normalized per gram of solid binder|按每克固体胶凝材料归一化
Bingham fit to descending flow curve after plug-flow correction|塞流校正后，对下降段流动曲线进行 Bingham 拟合
mean ± reported dispersion from 15 random EDX spots|15 个随机 EDX 测点的均值 ± 原文离散度
average strength of three specimens|三个试件的平均强度
EDX spot distribution shown in Fig. 14|EDX 测点分布见图 14
small gel pore category|小凝胶孔类别
large gel pore category|大凝胶孔类别
capillary pore category|毛细孔类别
at the same age|在相同龄期
individual replicate|单次平行试验
average of three measurements|三次测量的平均值
mortar formulation|砂浆配方
polished hardened mortar|抛光硬化砂浆
BSE point-scan EDS|背散射电子像对应的 EDS 点扫描
notched mortar prism|带缺口砂浆棱柱体
three-point bending|三点弯曲
ASTM E399–19 calculation|按 ASTM E399–19 计算
mortar cube|砂浆立方体
sand-free fresh paste|无砂新拌浆体
hardened paste|硬化浆体
SiO2/Na2O ratio of sodium silicate solution|硅酸钠溶液的 SiO₂/Na₂O 比
fresh AAFS concrete|新拌碱激发粉煤灰–矿渣混凝土
reported comparison of compressive strength|原文抗压强度比较
reported comparison|原文比较
AAFS paste at normal consistency|标准稠度碱激发粉煤灰–矿渣浆体
AAFS paste|碱激发粉煤灰–矿渣浆体
ASTM C191-08 Vicat setting test|按 ASTM C191-08 进行维卡凝结试验
potassium aluminum silicate hydroxide (muscovite)|含羟基钾铝硅酸盐（白云母）
aluminum silicate hydroxide (kaolinite)|含羟基铝硅酸盐（高岭石）
silicon oxide (SiO₂)|氧化硅（SiO₂）
Band marker shared by plotted spectra|图中各光谱共用的吸收带标记
Broad OH-region marker|宽 OH 区域标记
Kaolin OH-band marker|高岭土 OH 吸收带标记
Kaolin band marker|高岭土吸收带标记
Pure kaolin|纯高岭土
calcined kaolin (metakaolin)|煅烧高岭土（偏高岭土）
Calcined kaolin (metakaolin)|煅烧高岭土（偏高岭土）
Kaolin|高岭土
kaolin|高岭土
Metakaolin|偏高岭土
potassium aluminum silicate|钾铝硅酸盐
hexagonal silicon oxide (quartz)|六方氧化硅（石英）
peak chemical shift|峰化学位移
peak position|峰位置
Al(IV) red deconvolution component|四配位铝红色分峰组分
Al(V) blue deconvolution component|五配位铝蓝色分峰组分
Al(V) green deconvolution component|五配位铝绿色分峰组分
Al(VI) green deconvolution component|六配位铝绿色分峰组分
Observed spectrum (gray dashed)|实测光谱（灰色虚线）
Total fit (cyan)|总拟合（青色）
spectral deconvolution using OriginPro|使用 OriginPro 进行光谱分峰拟合
spectral deconvolution|光谱分峰拟合
sealed tube-mold paste|密封管模浆体
sealed-tube volume measurement during curing|养护期间的密封管体积测量
105 °C-cured plate subsample|105 °C 养护的板模子试样
105 °C-cured sealed-tube subsample|105 °C 养护的密封管模子试样
BET nitrogen physisorption|BET 氮气物理吸附
open plate-mold subsample|开放板模子试样
sealed tube-mold subsample|密封管模子试样
compression; equipment capacity 10 MPa|压缩试验；设备量程 10 MPa
compression|压缩试验
Plate-reflux|板模–回流
Plate-shaker|板模–振荡
Tube-reflux|管模–回流
Tube-shaker|管模–振荡
molar ratio; X=Na or K|摩尔比；X=Na 或 K
cured at|养护温度为
asymmetric Si–O–Si/Al–O–Si|非对称 Si–O–Si/Al–O–Si
tetrahedral Al indicator|四配位铝指示信号
O–C–O stretching in carbonates|碳酸盐中的 O–C–O 伸缩振动
molecular water in Al/Si–OH⋅⋅⋅H2O|Al/Si–OH⋅⋅⋅H₂O 中的分子水
mass per batch; commercial sodium silicate solution|每批质量；商用硅酸钠溶液
wt% in the alkaline activator solution|碱激发剂溶液中的质量百分数
ASTM C373-derived water absorption method|基于 ASTM C373 的吸水法
half-bars from flexural test|抗折试验后的半截棱柱体
circular specimen|圆形试件
ISO-8302:1991 heat-flow meter|ISO-8302:1991 热流计法
fractured prism half|折断的半棱柱体
graduated-cylinder method|量筒法
Z6 fracture surface representative region|Z6 断面的代表性区域
SEM–EDS mapping|SEM–EDS 元素面分布
EDS mapping|EDS 元素面分布
bending vibration of Al-OH bond (δAl-OH)|Al–OH 键弯曲振动（δAl–OH）
asymmetric stretching vibration of Si-O-Si bond (υasSi-O-Si)|Si–O–Si 键非对称伸缩振动（υasSi–O–Si）
mass ratio in prepared alkali-activated solution|所配碱激发溶液中的质量比
combined concrete-aggregate mass|混凝土骨料总质量
aggregate mass|骨料质量
sieving/selection|筛分/选取
mortar prism halves|砂浆半棱柱体
mortar prism|砂浆棱柱体
flow table|跳桌法
penetration rod|贯入杆法
calculated from TGA|由 TGA 计算
calculated|计算值
water absorption|吸水法
asymmetric stretching vibration of Si-O-Al|Si–O–Al 非对称伸缩振动
life-cycle calculation|生命周期计算
cost calculation|成本计算
cradle-to-gate assessment|从原料获取至出厂的评价
Si–O–Si bonds in raw RHA and undissolved particles|原始稻壳灰及未溶颗粒中的 Si–O–Si 键
activator solution|激发剂溶液
linear flow sweep|线性流动扫描
silicate monomers|硅酸盐单体
vibration of silicate species|硅酸盐物种振动
condensed silicate species|缩合硅酸盐物种
per hydrothermal suspension batch|每批水热悬浮液
hydrothermal synthesis|水热合成
theoretical weight ratio in synthesis suspension|合成悬浮液中的理论质量比
weight ratio in filtered RHA-derived solution|过滤后的稻壳灰衍生溶液质量比
filtered solution|过滤后溶液
Si–O–Si group of undissolved crystalline silica|未溶晶态二氧化硅的 Si–O–Si 基团
SiO2-to-Na2O weight ratio in activator|激发剂中的 SiO₂/Na₂O 质量比
activator weight ratio|激发剂质量比
precursor binder volume|前驱体胶凝材料体积
precursor binder mass|前驱体胶凝材料质量
flow sweep; Bingham fit|流动扫描；Bingham 拟合
oscillatory strain sweep at 1 Hz|1 Hz 振荡应变扫描
SAOS; peak phase angle|小振幅振荡剪切（SAOS）；相位角峰值
SAOS|小振幅振荡剪切（SAOS）
ultrasonic pulse velocity|超声脉冲波速
bending vibration of water|水的弯曲振动
Si–O–(Si, Al) vibration in growing gels|生长凝胶中的 Si–O–(Si, Al) 振动
Si–O–(Si, Al) band shifted during silicate-network development|硅酸盐网络发展过程中的 Si–O–(Si, Al) 吸收带位移
indicated analysis area|标示的分析区域
SEM-EDS point analysis|SEM-EDS 点分析
slag前驱体主吸收带|矿渣前驱体主吸收带
row-defined TCLP/SPLP leachate concentration or U.S. EPA maximum limit|该行对应的 TCLP/SPLP 浸出浓度或美国 EPA 最高限值
FGR leachate or regulatory reference|烟气残渣浸出液或法规参照
row-defined leaching method/regulatory limit|该行对应的浸出方法/法规限值
total concentration after aqua-regia digestion|王水消解后的总浓度
digested FGR|消解后的烟气残渣
descriptive statistics of SEM-EDS point analysis|SEM-EDS 点分析的描述性统计
Tukey test|Tukey 检验
nanoindentation with Gaussian deconvolution|纳米压痕与高斯分峰拟合
TGA under argon|氩气氛围下的 TGA
scanning probe microscopy|扫描探针显微镜法
loading rate|加载速率
micro-CT at 7 μm|7 μm 分辨率微米 CT
after 72 h heat curing、subsequent 24 h cooling in the oven|热养护 72 h 后，再随炉冷却 24 h
after heat curing|热养护后
reported micro-CT comparison|原文微米 CT 比较
nanoindentation dataset|纳米压痕数据集
cubic clustering criterion analysis|立方聚类准则分析
two-cluster k-means centroid analysis|两簇 k-means 质心分析
nanoindentation elastic-modulus distributions|纳米压痕弹性模量分布
nanoindentation hardness distributions|纳米压痕硬度分布
Wilcoxon signed-rank test|Wilcoxon 符号秩检验
hot-water extraction and pH meter|热水提取与 pH 计测量
dried and vacuum-degassed pulverized paste|干燥并真空脱气的粉化浆体
nitrogen adsorption/desorption; BET|氮气吸附/脱附；BET
pulverized paste nanoindentation grid|粉化浆体纳米压痕网格
k-means clustering|k-means 聚类
quantitative TGA|定量热重分析
isothermal calorimetry|等温量热法
flow curve rheometry/Bingham model|流动曲线流变测量/Bingham 模型
interparticle reaction products in hardened mortar|硬化砂浆中的颗粒间反应产物
reaction products in hardened mortar|硬化砂浆中的反应产物
slow penetration test|慢速贯入试验
fresh self-compacting validation mixture|新拌自密实验证混合物
EN 12350-9 V-Funnel|EN 12350-9 V 漏斗法
square printed column|方形打印柱
robotic extrusion 3D printing|机器人挤出式三维打印
notched|带缺口
polished section|抛光截面
polished|抛光
hardened|硬化
pulverized|粉化
fresh|新拌
combined|混合
paste|浆体
mortar|砂浆
concrete|混凝土
powder|粉末
fragment|碎片
cylinder|圆柱体
cube|立方体
prism|棱柱体
sample|试样
specimen|试件
bar|条形试件
diameter|直径
wide|宽
Vicat|维卡法
vibration|振动
tube|管模
plate|板模
Sample|试样
mass%|质量百分数
Eq.|公式'''
PHRASES=dict(line.split('|',1) for line in _PHRASES.splitlines())
_PATTERN=re.compile(r'(?<![A-Za-z])(?:'+ '|'.join(re.escape(k) for k in sorted(PHRASES,key=len,reverse=True))+r')(?![A-Za-z])')

def description_text(text):
    if text in PHASES:return PHASES[text]
    if text in LABELS:return LABELS[text]
    text=str(text).replace('(C, N)-A-S-H gel in pulverized paste','粉化浆体中的（C, N）-A-S-H 凝胶')
    # Alias verified against the original extraction entity label and specimen_type.
    text=text.replace('MIX_M15_B0_P','M1.5-B0（净浆）')
    text=text.replace('nanoindentation;','纳米压痕；').replace('For SS mixtures','对于 SS 混合物')
    text=re.sub(r'Table (\d+) (H2O2|SPC) series',r'表 \1 的 \2 系列',text)
    text=text.replace('Reference blended cement concrete','掺合水泥混凝土参照组')
    return _PATTERN.sub(lambda m:PHRASES[m[0]],text).replace(';','；')

_VALUES='''solid activator / retarder|固体激发剂/缓凝剂
reference-comparison raw material|对照用原材料
generic costing/emissions category for fine aggregate|细骨料成本/排放通用类别
reference-comparison additive|对照用添加剂
Super-plasticizer|减水剂
Low-calcium fly ash|低钙粉煤灰
Ground granulated blast-furnace slag|粒化高炉矿渣粉
alkaline activator component|碱激发剂组分
Sodium hydroxide solution|氢氧化钠溶液
Sodium silicate solution|硅酸钠溶液
Alkaline activator|碱激发剂
chemical admixture|化学外加剂
Modified polycarboxylate-based superplasticizer|改性聚羧酸系减水剂
coarse aggregate|粗骨料
10 mm crushed-granite coarse aggregate|10 mm 花岗岩碎石粗骨料
20 mm crushed-granite coarse aggregate|20 mm 花岗岩碎石粗骨料
aluminosilicate precursor feedstock|铝硅酸盐前驱体原料
calcined aluminosilicate precursor|煅烧铝硅酸盐前驱体
alkali activator component|碱激发剂组分
Sodium hydroxide pellets|氢氧化钠颗粒
Potassium hydroxide pellets|氢氧化钾颗粒
Potassium silicate solution|硅酸钾溶液
foaming agent/porogen|发泡剂/造孔剂
Hydrogen peroxide|过氧化氢
Sodium percarbonate|过碳酸钠
post-treatment washing reagent|后处理洗涤试剂
DRIFTS baseline material|漫反射红外光谱基线材料
Potassium bromide|溴化钾
aluminosilicate precursor; recycled mixed graded aggregate containing concrete debris and ceramic remains|铝硅酸盐前驱体；含混凝土碎屑和陶瓷残余的再生混合级配骨料
Construction and demolition waste (CDW)|建筑拆除废弃物（CDW）
amorphous-silica-rich aluminosilicate precursor; uncalcined waste ash|富含无定形二氧化硅的铝硅酸盐前驱体；未煅烧废灰
Rice husk ash (RHA)|稻壳灰（RHA）
activator water and added mixing water|激发剂用水与额外拌合水
Commercial sodium silicate solution|商用硅酸钠溶液
solid-waste binder constituent; reactive precursor|固废胶凝材料组分；活性前驱体
Ground granulated blast-furnace slag (GGBS)|粒化高炉矿渣粉（GGBS）
solid-waste binder constituent; limestone filler|固废胶凝材料组分；石灰石填料
Limestone powder (LP)|石灰石粉（LP）
solid-waste binder constituent|固废胶凝材料组分
solid-waste binder constituent; alkaline calcium-bearing additive|固废胶凝材料组分；碱性含钙添加剂
Calcium carbide residue (CCR)|电石渣（CCR）
alkaline-activator component|碱激发剂组分
Analytical-grade sodium hydroxide|分析纯氢氧化钠
prepared additive|配制的添加剂
Prepared alkaline activator|配制的碱激发剂
precursor/filler|前驱体/填料
alkali-activated precursor|碱激发前驱体
activator component|激发剂组分
activator solute|激发剂溶质
prepared activator|配制的激发剂
Alkali-activated solution|碱激发溶液
smaller than 2.5|小于 2.5
Granite crushed stone|花岗岩碎石
combined fine/coarse aggregate|细粗混合骨料
Concrete aggregate|混凝土骨料
XRD internal standard|XRD 内标
reference binder|参照胶凝材料
processing activity|加工活动
Grinding using electricity|用电研磨
Calcination using coal|燃煤煅烧
precursor and sodium-silicate feedstock|前驱体及硅酸钠制备原料
Siliceous fly ash|硅质粉煤灰
Ground granulated blast furnace slag|粒化高炉矿渣粉
alkali feedstock|碱原料
10 M sodium hydroxide solution|10 M 氢氧化钠溶液
mixing and synthesis liquid|拌合与合成用液体
Filtered RHA-derived sodium silicate solution|过滤后的稻壳灰制硅酸钠溶液
alkaline activator component containing residue filler|含残渣填料的碱激发剂组分
Unfiltered RHA-derived sodium silicate suspension|未过滤的稻壳灰制硅酸钠悬浮液
process residue and filler|工艺残渣及填料
Solid residue after RHA hydrothermal extraction|稻壳灰水热提取后的固体残渣
residue-washing agent|残渣洗涤剂
aluminosilicate precursor|铝硅酸盐前驱体
calcium-rich precursor|富钙前驱体
waste-derived alkaline activator raw material|废弃物衍生碱激发剂原料
Flue gas residue|烟气残渣
activator-solution water|激发剂溶液用水
liquid alkaline activator|液态碱激发剂
FGR-based alkaline activator solution|烟气残渣基碱激发剂溶液
alkaline activator|碱激发剂
Powdered sodium metasilicate|粉状偏硅酸钠
Light-burned MgO|轻烧 MgO
adsorption test gas|吸附试验气体
CO2_curing_start|CO₂ 养护开始
precursor: blast-furnace slag|前驱体：高炉矿渣
precursor: electric-furnace slag|前驱体：电炉渣
Portland cement precursor/replacement binder|硅酸盐水泥前驱体/替代胶凝材料
Sodium hydroxide|氢氧化钠
silicate activator solution|硅酸盐激发剂溶液
superplasticizer|减水剂
Polynaphthalene sulfonate|聚萘磺酸盐
inert calorimetry reference|惰性量热参照物
quantitative-XRD internal standard|定量 XRD 内标
reaction-stopping solvent|终止反应的溶剂'''
VALUES=dict(line.split('|',1) for line in _VALUES.splitlines())
VALUES.update(dict(line.split('|',1) for line in '''precursor|前驱体
solid activator|固体激发剂
mixing liquid|拌合液
fine aggregate|细骨料
Quartz sand|石英砂
Distilled water|蒸馏水
activator blend|复配激发剂
Natural sand|天然砂
1 M acetic acid|1 M 乙酸
curing_start|养护开始
mixing_end|搅拌结束
Steel slag (SS)|钢渣（SS）
mixing water|拌合水
Deionized water|去离子水
NaOH solution|NaOH 溶液
River sand|河砂
Zinc oxide|氧化锌
P⋅O 42.5 cement|P·O 42.5 水泥
Transport|运输
Rice husk ash|稻壳灰
Isopropanol|异丙醇
mixing_start|搅拌开始
Slag|矿渣
Class F fly ash|F 类粉煤灰
additive|添加剂
Mixing water|拌合水
test reagent|试验试剂
curing gas|养护气体
Carbon dioxide|二氧化碳
Nitrogen|氮气
Sodium|钠
Tap water|自来水
Silica sand|硅砂
Zincite|红锌矿
intermixing_end|两流混合结束'''.splitlines()))
UNIT_LABELS={'um':'μm','µm':'μm','kg/m3':'kg/m³','g/cm3':'g/cm³','m2/g':'m²/g',
 'cm^-1':'cm⁻¹','cm−1':'cm⁻¹','degree 2θ':'°2θ','2θ (degree)':'°2θ',
 'wt%':'wt.%','wt %':'wt.%','mass-%':'wt.%','mass%':'wt.%','mass %':'wt.%',
 'vol%':'vol.%','times':'倍','◦C':'°C','W/mK':'W/(m·K)','MPa⋅m1/2':'MPa·m½',
 'kgCO2/m3':'kg CO₂/m³','USD/m3':'USD/m³','kg CO2-e/kg':'kg CO₂-e/kg',
 'degrees 2theta':'°2θ','° 2θ':'°2θ','degree':'°','Pa.s':'Pa·s',
 'MPa·m^1/2':'MPa·m½','dimensionless':'无量纲','pH':'',
 'mm CMOD':'mm','kN P':'kN','pixels':'px','grayscale':'灰度'}

def value_text(text):
    if text in VALUES:return VALUES[text]
    if text in LABELS:return LABELS[text]
    result=description_text(text)
    result=re.sub(r'\b(?:approximately|about|around|roughly)\s+', '约 ', result)
    result=re.sub(r'^below\s+', '小于 ', result)
    result=result.replace('FA与slag','FA 与矿渣')
    return result

def formula_text(text):
    if text=='source figure coordinates -> calibrated axes':
        return '换算：原图坐标 → 校准后的坐标轴'
    text=text.replace('range/limit:','范围/限值：').replace('value = original','换算值 = 原值')
    text=re.sub(r'hours = (days|d) \*', '小时数 = 原始天数 ×',text)
    text=text.replace('hours = min *','小时数 = 原始分钟数 ×').replace('hours = s *','小时数 = 原始秒数 ×').replace('hours = h *','小时数 = 原始小时数 ×')
    return text.replace(' * ',' × ')
