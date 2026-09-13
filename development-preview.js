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
    const baseCell=comparisonCell;
    comparisonCell=function(record,label){
      const fields=record.fields.filter(f=>(f.displayLabel||f.label)===label);
      if(fields.length>1)return `<td class="matrix-cell">${fields.map(f=>`<div class="observation-item">${comparisonCell({...record,fields:[f]},label).replace(/^<td[^>]*>/,'').replace(/<\/td>$/,'')}</div>`).join('')}</td>`;
      let html=baseCell(record,label);const f=fields[0];
      if(!f)return html;
      const notes=[f.observationContext,f.sourceBasis,f.sourceFormula].filter(Boolean);
      return notes.length?html.replace('</td>',`<details class="source-basis"><summary>条件与计算依据</summary>${notes.map(n=>`<p>${esc(n)}</p>`).join('')}</details></td>`):html;
    };
    const baseRender=render;
    render=function(options={}){
      baseRender(options);
      document.title='WAKG 论文数据人工审核台';
      document.querySelectorAll('.paper-meta .tag').forEach(el=>el.remove());
      $('.integrity')?.remove();
      $('.review-guide').textContent='核对原材料和配比数据，点击“定位原文”查看来源。原表无值显示“无数值”；“约”表示估读。曲线可展开对照原图并下载点集。';
      document.querySelectorAll('.matrix-help span').forEach(el=>el.remove());
      $('.doc-foot a').textContent='打开论文来源（DOI） ↗';
    };
    render();return true;
  }
};
