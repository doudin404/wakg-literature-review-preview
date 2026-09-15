/* Original paper/table review surface, using the current extraction only. */
// Optional derived summaries stay in the stored result, not in the main table.
// Keep primary measured proportions, spectra, mixture ratios and required PSD metrics.
const reviewDerivedExtensionLabels=new Set([
  '碳排放下限','碳排放上限','材料成本','碳排放指数下限','碳排放指数上限',
  '综合成本指数','基体评价指数','CO₂ 总排放量','总价格','CO₂ 排放量','成本','CO₂ 减排量',
  '断裂韧性相对变化','抗压强度增幅','流动直径增幅','坍落度降幅','坍落度减少量',
  '初凝时间降幅','终凝时间降幅','抗压强度比','比表面积增幅','黏度降幅',
  '抗折强度与 28 d 值之比','抗压强度增量','孔隙率降幅','抗压强度降幅','相对 M1 的抗压强度降幅',
  '均值','最小值','第一四分位数','中位数','第三四分位数','最大值','Tukey 检验分组',
  '立方聚类准则','得分均值差','差值标准误','Z 统计量','p 值','Hodges–Lehmann 估计量','置信下限','置信上限'
]);
function mainReviewFields(record){
  const seen=new Set();
  return record.fields.filter(f=>{
    if(reviewDerivedExtensionLabels.has(f.displayLabel||f.label))return false;
    if(!f._observationIdentity)return true;
    const key=f._observationIdentity+'|'+f.semanticRole;
    if(seen.has(key))return false;seen.add(key);return true;
  });
}
function isFigureRead(field){
  return Boolean(field.curveKey||field.digitization||
    (field.approximate && /(?:ev-chart|figure|chart)/i.test(field.evidenceKey||'')));
}
function relatedCurve(field){
  if(field.curveKey)return field;
  if(field.relatedCurveKey)return {...field,curveKey:field.relatedCurveKey,evidenceKey:[...paper().records.mats,...paper().records.mixes].flatMap(r=>r.fields).find(f=>f.curveKey===field.relatedCurveKey)?.evidenceKey};
  if(!isFigureRead(field)||!/粒径 D(?:10|50|90)/.test(field.displayLabel||field.label))return null;
  const owner=[...paper().records.mats,...paper().records.mixes].find(r=>r.fields.includes(field));
  const matches=owner?.fields.filter(f=>f.curveKey&&/cumulative|累计/i.test(f.label+' '+f.displayLabel)&&
    (!field.observationCategory||f.observationCategory===field.observationCategory))||[];
  return matches.length===1?matches[0]:null;
}
function curvePointMarks(curve){
  return [...(curve.overlayPath||'').matchAll(/[ML]\s*(-?[\d.]+),\s*(-?[\d.]+)/g)]
    .map(m=>`<circle cx="${m[1]}" cy="${m[2]}" r="1.8" fill="#078947" stroke="white" stroke-width="0.5"/>`).join('');
}
const reviewTermMap=new Map(Object.entries({
  'scatter':'散点/曲线数据','line':'曲线数据','xrd':'XRD 衍射曲线','psd':'粒径分布曲线',
  'sem scale bar':'SEM 标尺长度','particle size d50':'中位粒径 D50',
  'rietveld crystalline phase composition':'Rietveld 定量晶相组成',
  'bet specific surface area':'BET 比表面积','bet ssa':'BET 比表面积',
  'volume increase':'体积增长率','yield stress':'屈服应力','fesem morphology':'FESEM 形貌',
  'elastic modulus centroid':'弹性模量聚类中心','hardness centroid':'硬度聚类中心',
  'pore volume':'孔体积','peak cmod':'峰值 CMOD','peak load':'峰值荷载',
  'fracture toughness (km)':'断裂韧度 Kₘ','ca/si mean':'Ca/Si 平均值',
  'stinitial':'初凝时间','stfinal':'终凝时间','flowability':'流动度','fluidity':'流动度',
  'bulk density':'堆积密度','real density':'真密度','apparent porosity':'表观孔隙率',
  'storage modulus':'储能模量','heat flow':'热流','peak temperature':'峰值温度',
  'ftir peak position':'FTIR 峰位','eds peak position':'EDS 峰位','eds spectrum':'EDS 能谱',
  'sem-eds spectrum':'SEM–EDS 能谱','sem–eds map-sum spectrum':'SEM–EDS 面扫总谱',
  'nitrogen adsorption desorption isotherm':'氮气吸附–脱附等温线',
  'median pore diameter d50':'中位孔径 D50','average pore diameter':'平均孔径',
  'critical strain range':'临界应变范围','pore solution ph range':'孔溶液 pH 范围',
  'calcium carbonate uptake':'碳酸钙吸收量','normalized heat flow':'归一化热流',
  'maximum normalized heat flow':'最大归一化热流','rigidification onset time':'硬化起始时间',
  'leaching concentration':'浸出浓度','total alkali content':'总碱含量',
  'ggbs to fa mass ratio':'GGBS/FA 质量比','water to binder ratio':'水胶比',
  'ggbs content':'GGBS 含量','alkaline solution modulus':'碱性溶液模数',
  'calcined soil content':'煅烧土含量','phase mass fraction':'物相质量分数',
  'energy-dispersive x-ray spectrum':'能量色散 X 射线能谱',
  'eds peak energy and relative intensity':'EDS 峰能量与相对强度',
  'eds elemental weight fraction':'EDS 元素质量分数',
  'eds elemental composition (weight percentage)':'EDS 元素组成（质量百分比）',
  'chemical composition':'化学组成','precursors':'前驱体','other':'其他',
  'activator/precursor':'激发剂/前驱体比','carbon emissions (ce)':'碳排放量（CE）',
  'matrix evaluation indicator (mei)':'基体评价指标（MEI）'
}));
function displayTerm(raw){
  let text=String(raw??'').trim();if(!text)return '—';
  const mapped=reviewTermMap.get(text.toLowerCase());if(mapped)return mapped;
  text=text.replace(/_/g,' ')
    .replace(/particle size D50/ig,'中位粒径 D50')
    .replace(/D50 particle size/ig,'中位粒径 D50')
    .replace(/SEM scale bar/ig,'SEM 标尺长度')
    .replace(/scale 条形试件/ig,'SEM 图像')
    .replace(/\bExperimental results\b/ig,'试验结果')
    .replace(/\bfracture surface\b/ig,'断口')
    .replace(/\bcumulative\b/ig,'累计分布')
    .replace(/\bversus\b/ig,'与')
    .replace(/\bpeak position\b/ig,'峰位')
    .replace(/\bpeak intensity\b/ig,'峰强度')
    .replace(/\bcompressive strength\b/ig,'抗压强度')
    .replace(/\bflexural strength\b/ig,'抗折强度')
    .replace(/\btotal porosity\b/ig,'总孔隙率')
    .replace(/\bflow diameter\b/ig,'流动直径')
    .replace(/\bsetting time\b/ig,'凝结时间')
    .replace(/\breaction degree\b/ig,'反应程度')
    .replace(/\bmass ratio\b/ig,'质量比')
    .replace(/\bcontent\b/ig,'含量')
    .replace(/\bEDX point\b/ig,'EDX 测点')
    .replace(/\bEDS region\b/ig,'EDS 区域')
    .replace(/\bmain cluster\b/ig,'主聚类')
    .replace(/\bcluster\b/ig,'聚类')
    .replace(/\bred\b/ig,'红色')
    .replace(/\bgreen\b/ig,'绿色')
    .replace(/\bunhydrated particles\b/ig,'未水化颗粒')
    .replace(/\bmagnification\b/ig,'放大倍数')
    .replace(/\bterminal heat flow\b/ig,'终点热流')
    .replace(/\bendothermic peak temperature\b/ig,'吸热峰温度')
    .replace(/\bprincipal DTG\b/ig,'主 DTG')
    .replace(/\blegend boundary\b/ig,'图例边界')
    .replace(/\bbleeding rate\b/ig,'泌水率')
    .replace(/\bFlow value\b/ig,'流动值')
    .replace(/\bSlump value\b/ig,'坍落度')
    .replace(/\btotal cost\b/ig,'总成本')
    .replace(/\bnormalized cost\b/ig,'归一化成本')
    .replace(/\bnormalized CO2 emissions\b/ig,'归一化 CO₂ 排放量')
    .replace(/\bmicrocracks\b/ig,'微裂缝')
    .replace(/\byield stress\b/ig,'屈服应力')
    .replace(/\bplastic viscosity\b/ig,'塑性黏度')
    .replace(/\bDynamic\b/ig,'动态')
    .replace(/\bStatic\b/ig,'静态')
    .replace(/\bSpecimen\b/ig,'试件')
    .replace(/\bMap Sum Spectrum\b/ig,'面扫总谱')
    .replace(/\bSpectrum\b/ig,'能谱')
    .replace(/\bage d:\s*(\d+(?:\.\d+)?)/ig,'$1 d');
  return text.replace(/\s*·\s*—\s*$/,'').trim()||'—';
}
function mainReviewRecords(records){
  const groups=new Map();
  const ordinary=records.map(r=>({...r,fields:mainReviewFields(r).filter(f=>{
    if(!f._collectiveKey)return true;
    if(!groups.has(f._collectiveKey))groups.set(f._collectiveKey,{kind:r.kind,_groupTitle:true,fields:[],owners:new Set()});
    const g=groups.get(f._collectiveKey);
    g.owners.add(r.fields.find(x=>x.label==='试样')?.displayText||r.fields.find(x=>x.label==='试样')?.value||r.label);
    if(!g.fields.some(x=>x.semanticRole===f.semanticRole))g.fields.push(f);
    return false;
  })}));
  return [...ordinary,...[...groups.values()].map(g=>({...g,label:'组级范围 · '+[...g.owners].join('、')}))];
}
window.WakgDevelopment={
  accept(queue){
    if(!['RESTORED_PREVIEW','EXTRACTION_PREVIEW'].includes(queue.reviewReadiness))return false;
    if(!validSourceExplanationContract(queue.sourceExplanationContract))throw Error('Source vocabulary missing');
    for(const p of queue.papers){
      const keys=new Set(p.pages.flatMap(pg=>pg.evidence.map(e=>e.evidenceKey)));
      for(const r of [...p.records.mats,...p.records.mixes])for(const f of r.fields){
        if(!validSourceExplanation(f))throw Error('Source vocabulary mismatch');
        if(f.evidenceKey&&!keys.has(f.evidenceKey))throw Error('Source locator missing');
      }
    }
    state.queue=queue;loadDecisions();state.note=decision()?.note||'';
    document.addEventListener('toggle',async event=>{
      const el=event.target;
      if(!el.matches?.('.point-table')||!el.open||el.dataset.loaded)return;
      const body=el.querySelector('tbody');
      try{
        const response=await fetch(el.dataset.url);if(!response.ok)throw Error(response.status);
        const lines=(await response.text()).trim().split(/\r?\n/),headers=lines.shift().split(',');
        const xi=headers.indexOf('x'),yi=headers.indexOf('y');
        body.innerHTML=lines.map(line=>{const row=line.split(',');return `<tr><td>${esc(row[xi])}</td><td>${esc(row[yi])}</td></tr>`}).join('');el.dataset.loaded='1';
      }catch{body.innerHTML='<tr><td colspan="2">读取失败，请重新展开重试。</td></tr>'}
    },true);
    const baseTable=recordsTableHtml;
    recordsTableHtml=records=>{
      const rows=mainReviewRecords(records),basic=[],observations=[],curveRows=[];
      rows.forEach((record,index)=>{
        const groups=new Map(),plain=[];
        for(const f of record.fields){
          if(f.curveKey){curveRows.push({record,index,field:f});continue}
          const root=f._fieldPath?.match(/^(\/modules\/(?:performance|characterizations)\/\d+)\//)?.[1];
          if(root||f.observationContext||f.curveKey){
            const key=root||f._fieldPath||String(groups.size);
            if(!groups.has(key))groups.set(key,[]);groups.get(key).push(f);
          }else plain.push(f);
        }
        // Repeated properties occupy separate rows, never nested cells.
        const lanes=[];
        for(const f of plain){const label=f.displayLabel||f.label;let lane=lanes.find(r=>!r.fields.some(x=>(x.displayLabel||x.label)===label));if(!lane){lane={...record,fields:[]};lanes.push(lane)}lane.fields.push(f)}
        lanes.forEach(r=>{r._tableTitle=reviewerRecordTitle(record,index);basic.push(r)});
        for(const fields of groups.values()){
          const age=fields.find(f=>f.semanticRole==='performance_age');
          for(const value of fields.filter(f=>f!==age))observations.push({record,index,value,age});
        }
      });
      const condition=(f,name)=>{const text=f.observationContext||'';const match=text.match(new RegExp(name+'[：:]([^\\n]*?)(?=[；\\n](?:试件|方法)[：:]|$)'));return match?.[1]||'—'};
      const category=(value,age)=>{
        const raw=String(value.observationCategory||'').trim();
        if(!raw)return '—';
        const compact=s=>String(s||'').replace(/\s+/g,'').toLowerCase();
        const ageText=age?.displayText??age?.value;
        if(/^\d+(?:\.\d+)?\s*(?:d|day|days)$/i.test(raw)||compact(raw)===compact(ageText))return '—';
        if(compact(raw)===compact(value.displayLabel||value.label))return '—';
        if(/^D50 particle size\s*·/i.test(raw))return '累计粒径分布';
        if(/scale 条形试件|SEM scale bar/i.test(raw))return 'SEM 图像';
        return displayTerm(raw);
      };
      const specimen=(value,age)=>{
        let text=condition(value,'试件');
        if(text==='—')return text;
        const ageText=String(age?.displayText??age?.value??'').trim();
        const candidates=[ageText,ageText.replace(/\s+/g,''),ageText.replace(/\s*d(?:ay)?s?$/i,'')];
        for(const candidate of candidates.filter(Boolean))text=text.replace(new RegExp('^\\s*'+candidate.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')+'\\s*','i'),'');
        text=text.replace(/^\s*\d+(?:\.\d+)?\s*(?:d|day(?:s)?)\s*(?=[\u4e00-\u9fffA-Za-z])/i,'');
        return displayTerm(text.trim()||'—');
      };
      const scalar=f=>f?`${esc(f.displayText??f.value??'无数值')}${(f.displayUnit??f.unit)?' '+esc(f.displayUnit??f.unit):''}`:'—';
      const ageCell=age=>age?.evidenceKey?`<button class="matrix-value linked" data-evidence="${esc(age.evidenceKey)}">${scalar(age)}</button>`:scalar(age);
      const hasCategory=observations.some(o=>o.value.observationCategory);
      const observationTable=observations.length?`<h3 class="observation-heading">测量与表征结果</h3><div class="matrix-wrap"><table class="comparison-table observation-table"><thead><tr><th>试样 / 材料</th><th>指标</th>${hasCategory?'<th>系列 / 类别</th>':''}<th>数值</th><th>单位</th><th>龄期</th><th>试件</th><th>方法</th></tr></thead><tbody>${observations.map(({record,index,value,age})=>`<tr><th scope="row">${esc(reviewerRecordTitle(record,index))}</th><td>${esc(displayTerm(value.displayLabel||value.label))}</td>${hasCategory?`<td>${esc(category(value,age))}</td>`:''}${comparisonCell({...record,fields:[{...value,unit:null,displayUnit:null}]},value.displayLabel||value.label)}<td>${esc(value.displayUnit??value.unit??'—')}</td><td>${ageCell(age)}</td><td>${esc(specimen(value,age))}</td><td>${esc(displayTerm(condition(value,'方法')))}</td></tr>`).join('')}</tbody></table></div>`:'';
      const curves=curveRows.length?`<h3 class="observation-heading">曲线与光谱</h3><div class="curve-list">${curveRows.map(({record,index,field})=>`<article class="curve-card"><h4>${esc(reviewerRecordTitle(record,index))} · ${esc(displayTerm(field.displayLabel||field.label))}</h4><button class="btn" data-evidence="${esc(field.evidenceKey)}">查看原图读取点</button>${curveHtml(field)}</article>`).join('')}</div>`:'';
      return (basic.length?baseTable(basic):'')+observationTable+curves;
    };
    const baseTitle=reviewerRecordTitle;
    reviewerRecordTitle=function(record,index){
      if(record._tableTitle)return record._tableTitle;
      if(record._groupTitle)return record.label;
      if(record.displayName)return record.displayName;
      return baseTitle({...record,fields:record.fields.map(f=>f.label==='试样'?{...f,value:f.displayText??f.value}:f)},index);
    };
    const baseCurve=curveHtml;
    curveHtml=function(field){
      if(field._suppressCurve)return '';
      const linked=relatedCurve(field);
      if(!linked)return '';
      const curve=paper().curves?.[linked.curveKey];
      let html=baseCurve(linked);
      html=html.replace(/<path class="curve-overlay"[^>]*\/>/,`<g class="curve-overlay">${curvePointMarks(curve)}</g>`)
        .replace('叠加提取轨迹（绿色）','叠加读取点（绿色）');
      html=html.replace('<details class="curve-detail">','<details class="curve-detail" open>');
      html=html.replace('</details>',`<details class="point-table" data-url="${esc(curve.pointsUrl)}"><summary>查看数据点</summary><div class="point-table-scroll"><table><thead><tr><th>横坐标 (${esc(curve.xAxis?.displayUnit||curve.xAxis?.unit||'—')})</th><th>纵坐标 (${esc(curve.yAxis?.displayUnit||curve.yAxis?.unit||'—')})</th></tr></thead><tbody></tbody></table></div></details></details>`);
      return curve?.displayLabel?html.replace(`aria-label="${esc(curve.label)}：`,`aria-label="${esc(curve.displayLabel)}：`):html;
    };
    function conversion(field){
      if(!field)return '';
      const value=field.displayText??field.value,unit=field.displayUnit??field.unit;
      const original=field.originalValue,originalUnit=field.originalUnit;
      const canonical=u=>String(u||'').replace('µ','μ').replace(/^um$/,'μm').replace(/^(days?|天)$/,'d').replace(/^hours?$/,'h').replace(/^minutes?$/,'min').replace(/^seconds?$/,'s');
      if(original===null||original===undefined||!originalUnit)return '';
      const numeric=v=>Number(String(v).replace(/^(?:约|≈)\s*/,''));
      if(numeric(original)===numeric(value)&&canonical(originalUnit)===canonical(unit))return '';
      if(String(original)===String(value)&&canonical(originalUnit)===canonical(unit))return '';
      if(Number(original)===Number(value)&&canonical(originalUnit)===canonical(unit))return '';
      if(!field.sourceFormula)return '';
      return `${original} ${originalUnit} → ${value} ${unit||''}`;
    }
    const baseCell=comparisonCell;
    comparisonCell=function(record,label){
      const fields=record.fields.filter(f=>(f.displayLabel||f.label)===label);
      const compact=fields[0];
      if(!compact)return '<td class="matrix-cell matrix-cell--missing">—</td>';
      const shown={...compact,_suppressCurve:true,value:compact.displayText??compact.value,unit:compact.displayUnit??compact.unit};
      let cell=baseCell({...record,fields:[shown]},label);
      cell=cell.replace(/<details class="curve-detail"[^>]*>[\s\S]*?<\/details>/g,'').replace(/<small class="digitization-note"[\s\S]*?<\/small>/g,'');
      return cell;
    };
    const baseRender=render;
    render=function(options={}){
      baseRender(options);
      document.title='WAKG 论文数据人工审核台';
      document.querySelectorAll('.paper-meta .tag').forEach(el=>el.remove());
      $('.integrity')?.remove();
      const resultTitle=$('.data-head h2');if(resultTitle)resultTitle.textContent='提取结果预览';
      $('.review-guide').textContent='点击数值查看原文与详情。测量结果按试样、指标和测试条件逐行列出。';
      const help=$('.matrix-help');if(help)help.textContent='基础信息与配比。空格表示该记录没有此字段。';
      const selected=state.activeEvidence&&[...paper().records.mats,...paper().records.mixes].flatMap(r=>r.fields).find(f=>f.evidenceKey===state.activeEvidence);
      if(selected){
        const panel=document.createElement('section');panel.className='selected-field-details';
        const xrf=selected.xrfReview;
        const candidate=xrf?.normalisation_candidate;
        const composition=xrf?`<p>XRF 原始合计：${esc(xrf.original_total??'未计算')} wt.%</p>${candidate?`<p>归一化候选：各项原值 ÷ ${esc(xrf.original_total)} × 100；原值保留。</p><table><thead><tr><th>组分</th><th>原值 / wt.%</th><th>候选 / wt.%</th></tr></thead><tbody>${xrf.rows.map((r,i)=>`<tr><td>${esc(r.component)}</td><td>${esc(r.original_value)}</td><td>${Number(candidate.rows[i]?.value).toFixed(3)}</td></tr>`).join('')}</tbody></table>`:''}`:'';
        panel.innerHTML=`<details open><summary>${esc(selected.displayLabel||selected.label)} · 来源详情</summary>${composition}${selected.sourceBasis?`<p>${esc(selected.sourceBasis)}</p>`:''}${selected.observationContext?`<p>${esc(selected.observationContext)}</p>`:''}${conversion(selected)?`<p>单位换算：${esc(conversion(selected))}</p>`:''}${curveHtml(selected)}</details>`;
        $('.record-scroll').before(panel);
        const linked=relatedCurve(selected),curve=paper().curves?.[linked?.curveKey];
        if(curve){
          const detail=panel.querySelector('.curve-detail');if(detail)detail.open=true;
          const percentile=(selected.displayLabel||selected.label).match(/粒径 D(10|50|90)/)?.[1];
          if(percentile){const note=document.createElement('p');note.textContent=`图中估读：累计分布 ${percentile}% 对应的粒径。`;panel.firstChild.insertBefore(note,panel.firstChild.children[1]||null)}
          const size=curve.sourceImageSize;
          const curvePage=paper().pages.find(pg=>pg.evidence.some(e=>e.evidenceKey===linked.evidenceKey));
          if(size&&curvePage?.number===state.page){
            const overlay=document.createElementNS('http://www.w3.org/2000/svg','svg');
            overlay.setAttribute('viewBox',`0 0 ${size[0]} ${size[1]}`);
            overlay.setAttribute('aria-label','原图上的读取点');
            overlay.style.cssText='position:absolute;inset:0;width:100%;height:100%;pointer-events:none;z-index:2';
            overlay.innerHTML=curvePointMarks(curve);$('.pdf-page').append(overlay);
            panel.querySelector('.curve-toggle input')?.addEventListener('input',event=>{overlay.style.display=event.target.checked?'':'none'});
          }
        }
      }
      $('.doc-foot a').textContent='打开论文来源（DOI） ↗';
      const hint=$('.doc-foot span');if(hint)hint.textContent=hint.textContent.replace('点击右侧“定位原文”','点击右侧数据单元格');
    };
    render();return true;
  }
};
