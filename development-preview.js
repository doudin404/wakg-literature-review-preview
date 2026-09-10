/* Explicit development mode for the existing comparison surface, not acceptance. */
window.WakgDevelopment = {
  templates: Object.freeze({DEV_SCRIPT:'脚本提取（未正式验收）',DEV_DIRECT:'Agent 原文数值候选',DEV_ESTIMATE:'Agent 图读估值候选（近似）',DEV_UNKNOWN:'本轮尚未确定',DEV_UNLOCATED:'提取候选（暂无可公开定位）'}),
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
      const html=originalCell(record,label),f=record.fields.find(field=>field.label===label);
      if(!f || !(f.sourceBasis||f.sourceFormula||f.observationContext||f.estimateRange?.some(v=>v!==null)))return html;
      const range=f.estimateRange?.some(v=>v!==null)?`估读范围：${f.estimateRange.map(v=>v===null?'未确定':v).join(' 至 ')} ${f.unit||''}；不是实验误差。`:'';
      return html.replace('</td>',`<details><summary>条件与提取依据</summary><p>${esc(f.observationContext||'')}</p><p>${esc(f.sourceBasis||'')}</p><p>${esc(f.sourceFormula||'')}</p><p>${esc(range)}</p></details></td>`);
    };
    const originalRender=render;
    render=function(options={}){
      originalRender(options);
      document.title='WAKG 十篇论文提取 · 开发预览';
      $('.brand h1').textContent='论文提取结果 · 开发预览';
      $('.paper-index small').textContent='本次十篇 · 未正式验收';
      $('.top-actions').textContent='开发预览，不是最终审核队列';
      document.querySelectorAll('.paper-meta .tag').forEach(el=>el.remove());
      $('.data-head h2').textContent='本次提取结果';
      $('.integrity').textContent='未进行正式独立验收';
      $('.integrity').removeAttribute('title');
      $('.review-guide').innerHTML='<strong>来源说明：</strong>脚本值、Agent 原文数值候选和“≈”图读估值分别标注；均未正式验收。空值表示本轮尚未确定，不代表论文没有数据。<br><strong>查看：</strong>切换 MAT/MIX，左右滚动查看养护、性能和图表提取项；点击“定位原文”。估值红框标出原图区域，不是精确数值词元。';
      $('.matrix-help span')?.remove();
      $('.doc-foot a').textContent='打开论文来源（DOI） ↗';
      $('.legend').textContent='红框：所选数值或估读图形的来源';
      const s=paper().devSummary;
      $('.review').innerHTML=`<p><strong>本篇开发结果：</strong>脚本值 ${s.scriptValues}；原文数值候选 ${s.directCandidates}；图读估值 ${s.visualCandidates}。</p><p>尚未确定字段 ${s.unknownFields}；未解决项 ${s.unresolvedItems}；拒收候选 ${s.rejectedCandidates}；失败模块 ${s.failedModules}。数量不代表完整性。</p><p>此版本仅供查看本次结果，不记录正式接受或拒绝决定。</p>`;
      document.querySelectorAll('.evidence').forEach(el=>el.setAttribute('aria-label','所选候选的原文来源区域'));
    };
    decide=()=>{};
    render();
    return true;
  }
};
