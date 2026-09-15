"""Local upstream WPD candidates in full-image coordinates."""
import os,json,subprocess
from pathlib import Path
import numpy as np
from scipy import ndimage
from scipy.spatial import cKDTree
from PIL import Image
from source_quantity_producer import atomic_json
from vector_curves import atomic_bytes

ROOT=Path(__file__).resolve().parents[1]

def plot_box(image):
    ink=np.asarray(image.convert('L'))<150
    h=ndimage.binary_opening(ink,structure=np.ones((1,max(30,image.width//3))))
    v=ndimage.binary_opening(ink,structure=np.ones((max(30,image.height//3),1)))
    hy,hx=np.nonzero(h);vy,vx=np.nonzero(v)
    if not len(hx) or not len(vx):return [0,0,image.width,image.height]
    left=int(np.median(vx));bottom=int(np.median(hy))
    if left>=image.width*.5 or bottom<=image.height*.5:return [0,0,image.width,image.height]
    return [left+3,int(vy.min())+2,int(hx.max()),bottom-2]

def scan(image,folder,box=None):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    backend=Path(os.environ.get('WAKG_WPD_ROOT',ROOT/'internal_assets/backend-evaluation-20260910'))
    box=list(map(int,box or plot_box(image)));crop=image.crop(box).convert('RGBA')
    atomic_bytes(folder/'crop.rgba',crop.tobytes())
    rgb=np.asarray(crop.convert('RGB'));hsv=np.asarray(crop.convert('HSV'))
    seeds=[([0,0,0],30),([128,128,128],45)]
    # Discover saturated colours from image ink, not paper-specific labels.
    bins=hsv[:,:,0]//16
    for b in np.unique(bins):
        selected=(bins==b)&(hsv[:,:,1]>80)&(hsv[:,:,2]>80)
        if selected.sum()<10:continue
        color=np.median(rgb[selected],axis=0).astype(int).tolist()
        seeds.append((color,120))
    cases=[]
    for i,(color,tolerance) in enumerate(seeds):
        cases.append(dict(case_id=str(i),rgb=color,color_distance=tolerance,width=crop.width,height=crop.height,
            rgba_path=str((folder/'crop.rgba').resolve()),output_prefix=str((folder/str(i)).resolve())))
    atomic_json(folder/'manifest.json',dict(cases=cases,crop_box=box))
    subprocess.run(['node',str(ROOT/'scripts/evaluate_curve_backends.mjs'),str((folder/'manifest.json').resolve()),str(backend),'wpd'],
                   check=True,capture_output=True,text=True,timeout=60)
    result=[]
    for case in cases:
        data=json.loads(Path(case['output_prefix']+'-wpd.json').read_bytes())
        pts=[[p['x']+box[0],p['y']+box[1]] for p in data['points']]
        if not pts:continue
        parent=list(range(len(pts)))
        def find(i):
            while parent[i]!=i:
                parent[i]=parent[parent[i]];i=parent[i]
            return i
        for i,j in cKDTree(pts).query_pairs(12):parent[find(i)]=find(j)
        groups={}
        for i,pt in enumerate(pts):groups.setdefault(find(i),[]).append(pt)
        for group in groups.values():
            result.append(dict(pixel_points=group,bbox_px=[min(x for x,y in group),min(y for x,y in group),max(x for x,y in group),max(y for x,y in group)],
                backend='WebPlotDigitizer',rgb=case['rgb'],color_distance=case['color_distance'],crop_box=box))
    return result
