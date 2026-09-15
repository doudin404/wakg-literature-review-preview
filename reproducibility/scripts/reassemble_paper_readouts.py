"""Reassemble a completed native run from its saved draft and figure readouts."""
import argparse
import json
from pathlib import Path
from paper_native_flow import assemble
from paper_chart_bridge import project
from source_quantity_producer import atomic_json


def reassemble(folder):
    folder=Path(folder).resolve()
    packet=json.loads((folder/'source-packet.json').read_bytes())
    draft=json.loads((folder/'assembly-retry/corrected-draft.json').read_bytes())
    records,report=assemble(packet,draft)
    tasks={t['source_id']:t for t in draft.get('figure_tasks',[])}
    figures=dict(processed=[],skipped=[],problems=[],review_fields=[])
    for source in packet['figures']:
        task=tasks.get(source['source_id'],{})
        if task.get('kind')=='skip':
            figures['skipped'].append(task)
            continue
        path=folder/'figures'/source['source_id']/'readout/result.json'
        if not path.exists():
            figures['problems'].append(dict(source_id=source['source_id'],reason='readout missing'))
            continue
        result=json.loads(path.read_bytes())
        bound=dict(source,asset_key='asset-'+source.get('document_sha256',packet['document']['sha256'])[:12])
        fields,errors=project(records,result,path.parent,bound)
        figures['review_fields']+=fields
        figures['problems']+=errors
        figures['processed'].append(source['source_id'])
        binding=json.loads((path.parent.parent/'planner/binding.json').read_bytes())
        for i in binding.get('incomplete_curve_panels',[]):
            figures['problems'].append(dict(source_id=source['source_id'],reason=f'panel {i}: spectrum trajectory missing'))
    records.setdefault('extensions',{})['chart_review']=figures
    report['figures']=figures
    for name,value in [('generated-records.json',records),('assembly-report.json',report)]:
        path=folder/name
        backup=folder/('before-reassembly-'+name)
        if path.exists() and not backup.exists():
            atomic_json(backup,json.loads(path.read_bytes()))
        atomic_json(path,value)
    print(json.dumps(dict(mats=len(records['mats']),mixes=len(records['mixes']),
                         figures=len(figures['processed']),problems=figures['problems']),ensure_ascii=False))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('folder',type=Path)
    reassemble(p.parse_args().folder)
