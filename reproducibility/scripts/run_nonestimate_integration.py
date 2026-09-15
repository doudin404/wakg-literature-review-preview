"""Non-estimated pipeline: fresh sources, whole-paper extraction, assembly."""
import argparse,hashlib,json,os,time
from pathlib import Path
from paper_native_flow import prepare,visible,extract,assemble
from draft_error_retry import repair, repair_assembly
from source_quantity_producer import atomic_json
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'internal_assets/nonestimate-integration-20260911'

def main():
    p=argparse.ArgumentParser();p.add_argument('--resume-from',type=Path);p.add_argument('--run',action='store_true');p.add_argument('--text-only',action='store_true');p.add_argument('--only');p.add_argument('--output',type=Path,default=OUT);p.add_argument('--text-draft',type=Path,help='Replay a saved semantic draft; still reparse sources and run image planning');p.add_argument('--batch-figures',action='store_true',help='Experimental whole-paper image batch; not the default after the live timeout');a=p.parse_args()
    output=a.output
    os.environ.setdefault('WAKG_FONT_CACHE_DIR',str(ROOT/'internal_assets/text-ocr-suspicious-20260911/cache'))
    summary=[]
    for pdf in sorted((ROOT/'internal_assets/review_test_corpus').rglob('*.pdf')):
        if a.only and a.only not in pdf.name:continue
        root=output/pdf.stem;root.mkdir(parents=True,exist_ok=True);start=time.monotonic()
        supplements=[]
        stem=pdf.stem.replace('-main','')
        for f in (ROOT/'internal_assets/supplements').rglob(stem+'-mmc*'):
            if f.is_file():supplements.append({'label':f.name,'relative_path':str(f),'sha256':hashlib.sha256(f.read_bytes()).hexdigest()})
        try:
            packet=prepare(pdf,supplements);view=visible(packet)
            atomic_json(root/'source-packet.json',packet);atomic_json(root/'agent-input.json',view)
            result={'paper':pdf.name,'tables':len(packet['tables']),'supplements':len(supplements),'input_chars':len(json.dumps(view,ensure_ascii=False)),'stage':'prepared'}
            if a.run:
                draft_path=a.text_draft
                if not draft_path and a.resume_from:
                    draft_path=next((a.resume_from/x for x in ('assembly-retry/corrected-draft.json',
                        'format-retry/corrected-draft.json','extraction/response.json','extraction/recovered-draft.json')
                        if (a.resume_from/x).exists()),None)
                print(json.dumps({'paper':pdf.name,'stage':'replaying-text' if draft_path else 'extracting'},ensure_ascii=False),flush=True)
                draft=json.loads(draft_path.read_bytes()) if draft_path else extract(packet,root/'extraction')
                result['text_mode']='saved_semantic_draft' if draft_path else 'fresh_model_extraction'
                draft=repair(packet,draft,root/'format-retry')
                draft,records,report=repair_assembly(packet,draft,root/'assembly-retry')
                if not a.text_only:
                    from paper_chart_bridge import integrate
                    print(json.dumps({'paper':pdf.name,'stage':'figure-batch' if a.batch_figures else 'figures'},ensure_ascii=False),flush=True)
                    report['figures']=integrate(packet,draft,records,root/'figures',batch_planning=a.batch_figures,
                        readings=a.resume_from/'figures' if a.resume_from else None)
                atomic_json(root/'generated-records.json',records);atomic_json(root/'assembly-report.json',report)
                result.update(stage='assembled',assembly=report)
            result['seconds']=round(time.monotonic()-start,3)
        except Exception as error:
            result={'paper':pdf.name,'stage':'failed','error':str(error),'seconds':round(time.monotonic()-start,3)}
        atomic_json(root/'integration-status.json',result);summary.append(result);print(json.dumps(result,ensure_ascii=False),flush=True)
    atomic_json(output/('summary-'+(a.only or 'all')+'.json'),summary)

if __name__=='__main__':main()
