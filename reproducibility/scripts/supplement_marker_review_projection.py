"""Produce exact source-image locators from independently accepted marker data."""
import argparse
import json
from pathlib import Path
from PIL import Image
import supplement_marker_acceptance as acceptance

MODE='reviewed-supplement-markers'
FILES=('scripts/supplement_marker_review_projection.py','scripts/supplement_marker_acceptance.py',
       'scripts/verify_supplement_markers.py')


def safe(root,path):
    value=(Path(root)/path).resolve();value.relative_to(Path(root).resolve());return value


def subject_for(root,run,proof,response,receipt):
    root=Path(root).resolve()
    paths=dict(zip(('records','proof','response','receipt'),
        (Path(run)/'generated-records.json',proof,response,receipt)))
    return {'protocol':MODE,'files':{k:{'path':safe(root,v).relative_to(root).as_posix(),
        'sha256':acceptance.source.sha(safe(root,v))} for k,v in paths.items()},
        'implementation':{name:acceptance.source.sha(root/name) for name in FILES}}


def verified_index(root,subject):
    root=Path(root).resolve()
    if (subject.get('protocol')!=MODE or set(subject.get('files',{}))!={'records','proof','response','receipt'}
            or subject.get('implementation')!={name:acceptance.source.sha(root/name) for name in FILES}):
        raise ValueError('marker review protocol identity mismatch')
    paths={}
    for name,entry in subject['files'].items():
        path=safe(root,entry['path']);paths[name]=path
        if acceptance.source.sha(path)!=entry['sha256']:raise ValueError('marker review subject hash mismatch')
    run=paths['records'].parent
    expected=acceptance.accept(run,paths['proof'],acceptance.source.load(paths['response']))
    if acceptance.source.load(paths['receipt'])!=expected:raise ValueError('marker acceptance receipt mismatch')
    records=acceptance.source.load(paths['records']);assets={a['asset_key']:a for a in records['assets']}
    pdf=next(a for a in assets.values() if a['kind']=='pdf')
    if acceptance.source.sha(safe(root,run/pdf['relative_path']))!=pdf['sha256']:
        raise ValueError('marker main paper identity mismatch')
    links={e['evidence_key']:e for e in records['evidence_links']};index={}
    for mix in records['mixes']:
        for i,row in enumerate(mix['modules']['performance']):
            if row.get('extensions',{}).get('extraction_method')!=acceptance.source.METHOD:continue
            path=f'/modules/performance/{i}/value';provenance=mix['field_provenance'][path]
            link=links[provenance['evidence_key']];image=assets[link['asset_key']]
            image_path=safe(root,run/image['relative_path'])
            with Image.open(image_path) as pixels:width,height=pixels.size
            locator_key=link['evidence_key']
            if locator_key in index:raise ValueError('marker review evidence key duplicate')
            index[locator_key]={'recordKey':mix['mix_key'],'fieldPath':path,'sourceEvidenceKey':locator_key,
                'value':row['value'],'unit':row['unit'],'displayValue':f"≈ {row['value']:.1f}",
                'bbox':link['bbox'],'sourcePage':1,'coordinateSpace':'image_pixels',
                'formula':provenance['formula'],'originalValue':provenance['original_value'],
                'originalUnit':provenance['original_unit'],'transformation':provenance['transformation'],
                'sourceExplanationCode':'SOURCE_DERIVED_VALUE','pdfSha256':pdf['sha256'],
                'image':{'assetKey':image['asset_key'],'sha256':image['sha256'],'width':width,'height':height,
                    'relativePath':image_path.relative_to(root).as_posix()},
                'subject':subject,'specimen':row['specimen'],'ageSeconds':row['age_seconds'],
                'formulationReference':row['extensions']['formulation_binding']}
    if len(index)!=expected['observation_count']:raise ValueError('marker review field coverage mismatch')
    return index


def verify_locator(index,locator,field,owner,pdf_sha,page):
    expected=index.get(locator.get('sourceEvidenceKey'))
    if expected is None:raise ValueError('marker locator lacks independent source acceptance')
    image=expected['image']
    if (owner!=expected['recordKey'] or pdf_sha!=expected['pdfSha256']
            or locator.get('evidenceMode')!=MODE or locator.get('supplementMarkerSubject')!=expected['subject']
            or any(locator.get(k)!=expected[k] for k in ('recordKey','fieldPath','bbox','sourcePage','coordinateSpace'))
            or locator.get('page')!=page.get('number') or page.get('sourceKind')!='supplement-figure'
            or page.get('sourceArtifactSha256')!=image['sha256'] or page.get('sourcePage')!=1
            or page.get('width')!=image['width'] or page.get('height')!=image['height']
            or field.get('evidenceKey')!=locator.get('evidenceKey')
            or float(field['value'])!=expected['value']
            or any(field.get(k)!=expected[k] for k in ('unit','displayValue','originalValue','originalUnit','transformation','sourceExplanationCode'))
            or field.get('sourceFormula')!=expected['formula']
            or locator.get('sourceToken')!=expected['formula'] or locator.get('snippet')!=expected['formula']):
        raise ValueError('marker reviewer value or source locator mismatch')
    return True


if __name__=='__main__':
    p=argparse.ArgumentParser()
    for key in ('run','proof','response','receipt','write'):p.add_argument('--'+key,type=Path,required=True)
    p.add_argument('--subject-write',type=Path)
    args=p.parse_args();root=acceptance.source.ROOT
    subject=subject_for(root,args.run,args.proof,args.response,args.receipt)
    index=verified_index(root,subject)
    result={'schema_version':1,'scope':acceptance.SCOPE,'fields':list(index.values()),'paperAccepted':False}
    acceptance.source.source._atomic_bytes(args.write,json.dumps(result,sort_keys=True,ensure_ascii=False,indent=2).encode())
    if args.subject_write:acceptance.source.source._atomic_bytes(args.subject_write,json.dumps(subject,sort_keys=True,ensure_ascii=False,indent=2).encode())
    print(json.dumps({'fields':len(index),'scope':acceptance.SCOPE}))
