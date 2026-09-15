"""Publish only current MAT/MIX records; prior generations remain in Git history."""
import argparse,json,shutil,re
from pathlib import Path
def clean(site):
    site=Path(site);public=Path(__file__).resolve().parents[1]/'public'
    path=site/'review-assets/queue.json';q=json.loads(path.read_bytes())
    removed=0
    for p in q['papers']:
        for key in ('previousExtraction','additionalExtraction','latestExtractionLabel'):
            p.pop(key,None)
        for group in ('mats','mixes'):
            for r in p['records'][group]:
                for f in r['fields']:
                    for key in ('corroboratingExtractions','newlyExtractedValues','conflictingExtractions','developmentNoValue'):
                        if key in f:removed+=1;f.pop(key)
        # Keep only locators used by the current records.
        keys={f.get('evidenceKey') for g in ('mats','mixes') for r in p['records'][g] for f in r['fields']}
        for pg in p['pages']:pg['evidence']=[e for e in pg['evidence'] if e['evidenceKey'] in keys]
    q['reviewReadiness']='EXTRACTION_PREVIEW';q['previewVersion']='review-surface-20260914'
    for key in ('mergedDevelopmentCommit','restoredFromCommit','queueScope'):q.pop(key,None)
    path.write_text(json.dumps(q,ensure_ascii=False,separators=(',',':'))+'\n',encoding='utf8')
    shutil.copy2(public/'restored-preview.js',site/'development-preview.js')
    index=(site/'index.html').read_text(encoding='utf8').replace('resume-20260914','review-surface-20260914').replace('WAKG 提取结果预览','WAKG 论文数据人工审核台')
    index=re.sub(r'((?:native-preview\.css|development-preview\.js|review-full\.js)\?v=)[^"\s]+',r'\1preview-20260915-terms1',index)
    (site/'index.html').write_text(index,encoding='utf8')
    print(json.dumps({'papers':len(q['papers']),'removedMergeMetadata':removed,'records':[(len(p['records']['mats']),len(p['records']['mixes'])) for p in q['papers']]}))
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('site');clean(p.parse_args().site)
