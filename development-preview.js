/* Original paper/table review surface, using the current extraction only. */
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
    const baseTitle=reviewerRecordTitle;
    reviewerRecordTitle=function(record,index){
      return baseTitle({...record,fields:record.fields.map(f=>f.label==='试样'?{...f,value:f.displayText??f.value}:f)},index);
    };
    const baseCurve=curveHtml;
    curveHtml=function(field){
      const html=baseCurve(field),curve=paper().curves?.[field.curveKey];
      return curve?.displayLabel?html.replace(`aria-label="${esc(curve.label)}：`,`aria-label="${esc(curve.displayLabel)}：`):html;
    };
    function conversion(field){
      if(!field)return '';
      const value=field.displayText??field.value,unit=field.displayUnit??field.unit;
      const original=field.originalValue,originalUnit=field.originalUnit;
      const canonical=u=>String(u||'').replace('µ','μ').replace(/^(days?|天)$/,'d').replace(/^hours?$/,'h').replace(/^minutes?$/,'min').replace(/^seconds?$/,'s');
      if(original===null||original===undefined||!originalUnit)return '';
      if(String(original)===String(value)&&canonical(originalUnit)===canonical(unit))return '';
      if(Number(original)===Number(value)&&canonical(originalUnit)===canonical(unit))return '';
      if(!field.sourceFormula)return '';
      return `${original} ${originalUnit} → ${value} ${unit||''}`;
    }
    const baseCell=comparisonCell;
    comparisonCell=function(record,label){
      const fields=record.fields.filter(f=>(f.displayLabel||f.label)===label);
      if(fields.length>1){
        const groups=new Map();
        fields.forEach((f,i)=>{const root=f._fieldPath?.match(/^(\/modules\/(?:performance|characterizations)\/\d+)\/(?:value|age_seconds)$/)?.[1];const key=root||`field-${i}`;if(!groups.has(key))groups.set(key,[]);groups.get(key).push(f)});
        return `<td class="matrix-cell">${[...groups.values()].map(group=>{
          const age=group.find(f=>f.semanticRole==='performance_age'),values=group.filter(f=>f!==age);
          const shown=age&&values.length===1?[{...values[0],_joinedAge:age}]:group;
          return shown.map(f=>`<div class="observation-item matrix-cell ${[f.evidenceKey,f._joinedAge?.evidenceKey].filter(Boolean).includes(state.activeEvidence)?'matrix-cell--active':''}" data-cell-evidence="${esc(f.evidenceKey||'')}">${comparisonCell({...record,fields:[f]},label).replace(/^<td[^>]*>/,'').replace(/<\/td>$/,'')}</div>`).join('');
        }).join('')}</td>`;
      }
      let html=baseCell(record,label);const f=fields[0];
      if(!f)return html;
      if(f.displayText!==undefined)html=html.replace(`<strong>${esc(f.value)}</strong>`,`<strong>${esc(f.displayText)}</strong>`);
      if(f.displayUnit!==undefined&&f.unit)html=html.replace(`<small>${esc(f.unit)}</small>`,`<small>${esc(f.displayUnit)}</small>`);
      if(f.displayPrefix)html=html.replace('<strong>',`<strong><span class="value-prefix">${esc(f.displayPrefix)}</span>`);
      if(f.label==='养护条件')html=html.replace('<strong>','<strong class="curing-text">');
      if(f.sourceBasis)html=html.replace('</button>',`<span class="comparison-basis">${esc(f.sourceBasis)}</span></button>`);
      const age=f._joinedAge;
      if(age){const linked=age.evidenceKey&&!age.evidenceBlocked;
        html=html.replace('</button>',`</button><button class="observation-age" ${linked?`data-evidence="${esc(age.evidenceKey)}"`:'disabled'} title="龄期来源">龄期：${esc(age.displayText??age.value)} ${esc(age.displayUnit??age.unit??'')}${linked?' · ⌖':''}</button>`);
      }
      const context=f.observationContext||age?.observationContext;
      const conversions=[conversion(f),conversion(age)].filter(Boolean);
      const detail=(context?`<details class="source-basis"><summary>测试条件</summary><p>${esc(context)}</p></details>`:'')+conversions.map(c=>`<p class="unit-conversion">单位换算：${esc(c)}</p>`).join('');
      return html.replace('</td>',`${detail}</td>`);
    };
    const baseRender=render;
    render=function(options={}){
      baseRender(options);
      document.title='WAKG 论文数据人工审核台';
      document.querySelectorAll('.paper-meta .tag').forEach(el=>el.remove());
      $('.integrity')?.remove();
      $('.review-guide').textContent='点击数值或龄期查看各自来源；展开测试条件可核对试件和方法。曲线可展开对照原图并下载点集。';
      $('.matrix-help').textContent='每条记录一行。同一性能的不同条件逐项列出，分别保留来源。';
      $('.doc-foot a').textContent='打开论文来源（DOI） ↗';
      const hint=$('.doc-foot span');if(hint)hint.textContent=hint.textContent.replace('点击右侧“定位原文”','点击右侧数据单元格');
    };
    render();return true;
  }
};
