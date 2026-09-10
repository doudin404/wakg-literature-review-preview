/* Restore historical records with their original source vocabulary, without re-acceptance. */
window.WakgDevelopment = {
  accept(queue) {
    if (queue.reviewReadiness !== 'RESTORED_PREVIEW') return false;
    if (queue.formalAcceptance !== false || queue.preHumanAgentReview || queue.papers?.length !== 10) throw new Error('Invalid restored queue');
    if(!validSourceExplanationContract(queue.sourceExplanationContract))throw new Error('Original source contract missing');
    for(const p of queue.papers){
      if(!p.pages?.length || p.pages.length!==p.pageCount) throw new Error('Missing source pages');
      const keys=new Set(p.pages.flatMap(pg=>pg.evidence.map(e=>e.evidenceKey)));
      const records=[...p.records.mats,...p.records.mixes,...p.additionalExtraction.mats,...p.additionalExtraction.mixes];
      for(const record of records){
        const labels=new Set();
        for(const field of record.fields){
          const displayLabel=field.displayLabel||field.label;
          if(labels.has(displayLabel)) throw new Error('Duplicate display label');
          labels.add(displayLabel);
          if(!validSourceExplanation(field)) throw new Error('Original source explanation mismatch');
          if(field.evidenceKey && !keys.has(field.evidenceKey)) throw new Error('Missing source locator');
          for(const entry of [...(field.corroboratingExtractions||[]),...(field.newlyExtractedValues||[]),...(field.conflictingExtractions||[]),...(field.developmentNoValue||[])]){
            if(!validSourceExplanation(entry)) throw new Error('Merged source explanation mismatch');
            if(entry.evidenceKey && !keys.has(entry.evidenceKey)) throw new Error('Missing merged source locator');
          }
        }
      }
    }
    state.queue=queue;state.decisions={};state.note='';
    document.body.classList.add('development-mode');
    const style=document.createElement('style');
    style.textContent='@media(max-width:1050px){.development-mode .topbar{height:auto;min-height:110px;grid-template-columns:1fr auto;padding-block:10px}.development-mode .paper-nav{display:flex;grid-column:1/-1;justify-content:center;grid-row:2}}.development-mode .matrix-cell details{padding:5px 10px;font-size:10px;max-width:240px;border-top:1px dashed #e5e1da}.development-mode .matrix-cell details p{margin:4px 0;line-height:1.4}.development-mode .merge-ok{color:#26704a}.development-mode .merge-conflict{color:#a13d34}.development-mode .evidence-jump{border:0;background:transparent;color:#b93831;font-size:10px;font-weight:700;cursor:pointer;padding:0}.development-mode .addition-block{margin:6px 14px 18px;padding-top:10px;border-top:2px solid #d8d3c9}.development-mode .addition-head{position:sticky;left:0;margin:0 0 8px}.development-mode .addition-head h3{margin:0 0 4px;font-size:14px}.development-mode .addition-head p{margin:0;color:#697381;font-size:10px}.development-mode .addition-block .matrix-wrap{padding:0}.development-mode .review{font-size:12px}.development-mode .review p{margin:4px 0}';
    document.head.appendChild(style);
    comparisonColumns=function(records){const seen=new Set(),columns=[];records.forEach(record=>record.fields.forEach(field=>{const label=field.displayLabel||field.label;if(!seen.has(label)){seen.add(label);columns.push(label)}}));return columns};
    const originalCell=comparisonCell;
    comparisonCell=function(record,label){
      const f=record.fields.find(field=>(field.displayLabel||field.label)===label);
      const displayRecord=f?.displayLabel?{...record,fields:record.fields.map(field=>field===f?{...field,label:f.displayLabel}:field)}:record;
      let html=originalCell(displayRecord,label);
      if(!f)return html;
      const range=f.estimateRange?.some(v=>v!==null)?`估读范围：${f.estimateRange.map(v=>v===null?'未确定':v).join(' 至 ')} ${f.unit||''}；不是实验误差。`:'';
      const basis=[f.extractionMethod?`提取方式：${f.extractionMethod}`:'',f.observationContext||'',f.sourceBasis||'',f.sourceFormula||'',range].filter(Boolean).map(v=>`<p>${esc(v)}</p>`).join('');
      const evidenceButton=entry=>entry.evidenceKey?` <button class="evidence-jump" data-evidence="${esc(entry.evidenceKey)}">定位本轮依据</button>`:'';
      const corroborating=(f.corroboratingExtractions||[]).map(entry=>`<p class="merge-ok"><strong>本轮复核一致：</strong>${esc(entry.value)} ${esc(entry.unit||'')}；来源含义：${esc(entry.sourceExplanation)}；提取方式：${esc(entry.extractionMethod)}${evidenceButton(entry)}</p>`).join('');
      const newlyExtracted=(f.newlyExtractedValues||[]).map(entry=>`<p class="merge-ok"><strong>本轮补充：</strong>${esc(entry.value)} ${esc(entry.unit||'')}；旧表此处无值；来源含义：${esc(entry.sourceExplanation)}；提取方式：${esc(entry.extractionMethod)}${evidenceButton(entry)}</p>`).join('');
      const conflicts=(f.conflictingExtractions||[]).map(entry=>`<p class="merge-conflict"><strong>本轮结果不同，双方保留：</strong>旧值 ${esc(f.value)} ${esc(f.unit||'')}；本轮值 ${esc(entry.value)} ${esc(entry.unit||'')}；来源含义：${esc(entry.sourceExplanation)}；提取方式：${esc(entry.extractionMethod)}${evidenceButton(entry)}</p>`).join('');
      const noValue=(f.developmentNoValue||[]).map(entry=>`<p><strong>本轮状态：</strong>${esc(entry.extractionMethod)}；旧值继续保留。</p>`).join('');
      if(!(basis||corroborating||newlyExtracted||conflicts||noValue))return html;
      return html.replace('</td>',`<details><summary>条件、提取方式与依据</summary>${basis}${corroborating}${newlyExtracted}${conflicts}${noValue}</details></td>`);
    };
    const originalRecordsHtml=recordsHtml;
    recordsHtml=function(){
      const main=originalRecordsHtml(),p=paper(),extras=p.additionalExtraction[state.tab==='mat'?'mats':'mixes'];
      if(!extras.length)return main;
      return `${main}<section class="addition-block"><div class="addition-head"><h3>新增提取</h3><p>这些字段或记录暂不能与旧表按材料/试样、属性、单位、龄期及条件可靠对应，因此单独保留，不覆盖旧值。</p></div>${recordsTableHtml(extras)}</section>`;
    };
    const originalRender=render;
    render=function(options={}){
      originalRender(options);
      document.title='WAKG 提取结果预览';
      $('.brand h1').textContent='提取结果预览';
      $('.paper-index small').textContent='';
      $('.top-actions').textContent='';
      document.querySelectorAll('.paper-meta .tag').forEach(el=>el.remove());
      $('.data-head h2').textContent='提取数据';
      $('.integrity').remove();
      $('.review-guide').textContent='旧版完整表格为主体；本轮可靠对应的数据已并入复核依据，冲突值同时保留，暂不能对应的内容列在“新增提取”。来源含义沿用旧版模板；“提取方式”单列说明；“约”表示图中估读。点击“定位原文”查看依据。';
      $('.matrix-help span')?.remove();
      $('.doc-foot a').textContent='打开论文来源（DOI） ↗';
      $('.legend').textContent='红框：所选数值或估读图形的来源';
      $('.review').remove();
      document.querySelectorAll('.evidence').forEach(el=>el.setAttribute('aria-label','所选数据的原文来源区域'));
    };
    decide=()=>{};
    render();
    return true;
  }
};
