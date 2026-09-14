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
      const scalar=f=>f?`${esc(f.displayText??f.value??'无数值')}${(f.displayUnit??f.unit)?' '+esc(f.displayUnit??f.unit):''}`:'—';
      const ageCell=age=>age?.evidenceKey?`<button class="matrix-value linked" data-evidence="${esc(age.evidenceKey)}">${scalar(age)}</button>`:scalar(age);
      const hasCategory=observations.some(o=>o.value.observationCategory);
      const observationTable=observations.length?`<h3 class="observation-heading">测量与表征结果</h3><div class="matrix-wrap"><table class="comparison-table observation-table"><thead><tr><th>试样 / 材料</th><th>指标</th>${hasCategory?'<th>系列 / 类别</th>':''}<th>数值</th><th>单位</th><th>龄期</th><th>试件</th><th>方法</th></tr></thead><tbody>${observations.map(({record,index,value,age})=>`<tr><th scope="row">${esc(reviewerRecordTitle(record,index))}</th><td>${esc(value.displayLabel||value.label)}</td>${hasCategory?`<td>${esc(value.observationCategory||'—')}</td>`:''}${comparisonCell({...record,fields:[{...value,unit:null,displayUnit:null}]},value.displayLabel||value.label)}<td>${esc(value.displayUnit??value.unit??'—')}</td><td>${ageCell(age)}</td><td>${esc(condition(value,'试件'))}</td><td>${esc(condition(value,'方法'))}</td></tr>`).join('')}</tbody></table></div>`:'';
      const curves=curveRows.length?`<h3 class="observation-heading">曲线与光谱</h3><div class="curve-list">${curveRows.map(({record,index,field})=>`<article class="curve-card"><h4>${esc(reviewerRecordTitle(record,index))} · ${esc(field.displayLabel||field.label)}</h4><button class="btn" data-evidence="${esc(field.evidenceKey)}">查看原图读取点</button>${curveHtml(field)}</article>`).join('')}</div>`:'';
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
      html=html.replace('</details>',`<details class="point-table" data-url="${esc(curve.pointsUrl)}"><summary>查看数据点</summary><div class="point-table-scroll"><table><thead><tr><th>横坐标 (${esc(curve.xAxis?.unit||'—')})</th><th>纵坐标 (${esc(curve.yAxis?.unit||'—')})</th></tr></thead><tbody></tbody></table></div></details></details>`);
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
