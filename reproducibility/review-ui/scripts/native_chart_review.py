"""Materialize native curve assets for the existing curveHtml UI contract."""
import csv,math,shutil
from pathlib import Path
from PIL import Image

def spectra(node):
    if isinstance(node,dict):
        if node.get('points_asset_key') and node.get('source_image_asset_key'):yield node
        for value in node.values():yield from spectra(value)
    elif isinstance(node,list):
        for value in node:yield from spectra(value)

def coordinate(value,axis):
    anchors=axis.get('anchors',[])
    if len(anchors)<2:return None
    (p0,v0),(p1,v1)=anchors[0],anchors[-1]
    if axis.get('scale')=='log10':value,v0,v1=map(math.log10,(value,v0,v1))
    return p0+(value-v0)*(p1-p0)/(v1-v0)

def materialize(project,records,cards,output,url_prefix):
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    assets={a['asset_key']:Path(project)/a['relative_path'] for a in records['assets']}
    owners={r.get('mat_key') or r.get('mix_key'):r for r in records['mats']+records['mixes']}
    curves={}
    for group in ('mats','mixes'):
        for card in cards[group]:
            index={s['points_asset_key']:s for s in spectra(owners[card['key']])}
            for field in card['fields']:
                key=field.get('pointsAssetKey')
                if not key:continue
                curve=index[key];source=assets[curve['source_image_asset_key']]
                with assets[key].open(encoding='utf8',newline='') as handle:rows=list(csv.DictReader(handle))
                curvekey=field['evidenceKey'];csv_name=curvekey+'.csv';image_name=curvekey+'.png'
                shutil.copy2(assets[key],output/csv_name)
                with Image.open(source) as image:
                    source_size=list(image.size)
                    box=curve.get('extensions',{}).get('plot_bbox_px') or [0,0,*image.size]
                    image.crop(box).save(output/image_name)
                path=[]
                for i,row in enumerate(rows):
                    x=float(row['pixel_x']) if row.get('pixel_x') else coordinate(float(row['x']),curve.get('x_axis') or {})
                    y=float(row['pixel_y']) if row.get('pixel_y') else coordinate(float(row['y']),curve.get('y_axis') or {})
                    if x is None or y is None:continue
                    move=not path or row.get('gap_before')=='1'
                    path.append(f'{"M" if move else "L"}{x:.4f},{y:.4f}')
                field['curveKey']=curvekey
                curves[curvekey]=dict(label=field['label'],pointCount=len(rows),sourceBBox=box,
                    sourceImageUrl=url_prefix+'/'+image_name,pointsUrl=url_prefix+'/'+csv_name,
                    sourceImageSize=source_size,overlayPath=' '.join(path),xAxis=curve.get('x_axis'),yAxis=curve.get('y_axis'),
                    valueOrigin='image_intersection_estimate',reviewRegions=field.get('reviewRegions',[]))
    return curves
