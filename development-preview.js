/* Explicit development mode for the existing comparison surface, not acceptance. */
window.WakgDevelopment = {
  templates: Object.freeze({DEV_SCRIPT:'提取值',DEV_DIRECT:'原文数值',DEV_ESTIMATE:'图读估算',DEV_UNKNOWN:'待补充',DEV_UNLOCATED:'来源待补充'}),
  accept(queue) {
    if (queue.reviewReadiness !== 'DEVELOPMENT_PREVIEW') return false;
    if (queue.formalAcceptance !== false || queue.preHumanAgentReview || queue.papers?.length !== 10) throw new Error('Invalid development queue');
    const templates=this.templates;
    for(const p of queue.papers){
      if(!p.pages?.length || p.pages.length!==p.pageCount) throw new Error('Missing source pages');
      const keys=new Set(p.pages.flatMap(pg=>pg.evidence.map(e=>e.evidenceKey)));
      for(const record of [...p.records.mats,...p.records.mixes]){
        const labels=new Set();
        for(const field of record.fields){
          if(labels.has(field.label)) throw new Error('Duplicate display label');
          labels.add(field.label);
          if(!templates[field.sourceExplanationCode] || templates[field.sourceExplanationCode]!==field.sourceExplanation) throw new Error('Unknown development explanation');
          if(field.evidenceKey && !keys.has(field.evidenceKey)) throw new Error('Missing source locator');
        }
      }
    }
    state.queue=queue;state.decisions={};state.note='';
    document.body.classList.add('development-mode');
    const style=document.createElement('style');
    style.textContent='@media(max-width:1050px){.development-mode .topbar{height:auto;min-height:110px;grid-template-columns:1fr auto;padding-block:10px}.development-mode .paper-nav{display:flex;grid-column:1/-1;justify-content:center;grid-row:2}}.development-mode .matrix-cell details{padding:4px 10px;font-size:12px;max-width:220px}.development-mode .review{font-size:12px}.development-mode .review p{margin:4px 0}';
    document.head.appendChild(style);
    sourceExplanation=field=>templates[field.sourceExplanationCode];
    const originalCell=comparisonCell;
    comparisonCell=function(record,label){
      let html=originalCell(record,label);const f=record.fields.find(field=>field.label===label);
      if(f && (f.value===null||f.value===undefined||f.value===''))html=html.replace('<strong>无数值</strong>','<strong>待补充</strong>');
      if(!f || !(f.sourceBasis||f.sourceFormula||f.observationContext||f.estimateRange?.some(v=>v!==null)))return html;
      const range=f.estimateRange?.some(v=>v!==null)?`估读范围：${f.estimateRange.map(v=>v===null?'未确定':v).join(' 至 ')} ${f.unit||''}；不是实验误差。`:'';
      return html.replace('</td>',`<details><summary>条件与提取依据</summary><p>${esc(f.observationContext||'')}</p><p>${esc(f.sourceBasis||'')}</p><p>${esc(f.sourceFormula||'')}</p><p>${esc(range)}</p></details></td>`);
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
      $('.review-guide').textContent='MAT：原材料；MIX：配比、养护与性能。点击“定位原文”查看来源；“约”表示图读估算。';
      $('.matrix-help span')?.remove();
      $('.doc-foot a').textContent='打开论文来源（DOI） ↗';
      $('.legend').textContent='红框：所选数值或估读图形的来源';
      $('.review').remove();
      document.querySelectorAll('.evidence').forEach(el=>el.setAttribute('aria-label','所选候选的原文来源区域'));
    };
    decide=()=>{};
    render();
    return true;
  }
};
