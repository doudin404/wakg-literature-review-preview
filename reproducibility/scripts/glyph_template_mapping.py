"""Local font-template mapping for embedded CFF fonts lacking ToUnicode."""
import functools
import io
import json
import os
from pathlib import Path

import fitz
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from scipy.ndimage import distance_transform_edt
from fontTools.ttLib import TTFont
from fontTools.cffLib import CFFFontSet
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.boundsPen import BoundsPen

VERSION = 'shape-font-v1'
ROOT = Path(__file__).resolve().parents[1]
FONTS = [(f, 0) for f in ['times.ttf','timesi.ttf','timesbd.ttf','timesbi.ttf',
    'arial.ttf','ariali.ttf','arialbd.ttf','arialbi.ttf','georgia.ttf','georgiai.ttf',
    'calibri.ttf','calibrii.ttf','seguisym.ttf','euclid.ttf','euclidi.ttf']] + [('cambria.ttc',1)]
CHARS = [chr(c) for a,b in [(33,127),(160,256),(0x370,0x400),(0x2000,0x2070),
    (0x2200,0x2300),(0xfb00,0xfb07)] for c in range(a,b)]

def cache_root():
    return Path(os.environ.get('WAKG_FONT_CACHE_DIR', ROOT/'internal_assets/font-templates'))/VERSION

def canonical(text):
    return text.replace('µ','μ').replace('≧','≥').replace('≦','≤').replace('●','•').replace('ﬁ','fi').replace('ﬂ','fl')

def features(image, baseline=115):
    pixels = np.asarray(image.convert('L')) < 160
    y,x = np.where(pixels)
    if not len(x):
        return None
    x0,x1,y0,y1=x.min(),x.max()+1,y.min(),y.max()+1
    crop=Image.fromarray((pixels[y0:y1,x0:x1]*255).astype('uint8'))
    scale=44/max(crop.size)
    crop=crop.resize((max(1,round(crop.width*scale)),max(1,round(crop.height*scale))),Image.Resampling.LANCZOS)
    tile=Image.new('L',(48,48));tile.paste(crop,((48-crop.width)//2,(48-crop.height)//2))
    return np.asarray(tile)>127, np.array([(x1-x0)/100,(y1-y0)/100,(baseline-y1)/100],dtype=np.float32)

class Matcher:
    def __init__(self, directory):
        import unicodedata
        directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
        path=directory/'templates.npz'
        if not path.exists():
            masks=[];metrics=[];labels=[];names=[]
            font_root=Path(os.environ.get('WAKG_TEMPLATE_FONTS', 'C:/Windows/Fonts'))
            for name,index in FONTS:
                file=font_root/name
                if not file.exists():continue
                tt=TTFont(file,fontNumber=index);cmap=tt.getBestCmap() or {};tt.close()
                font=ImageFont.truetype(str(file),100,index=index)
                for char in CHARS:
                    if ord(char) not in cmap or unicodedata.category(char) in ('Cf','Cc','Zs','Zl','Zp'):continue
                    im=Image.new('L',(200,200),255)
                    ImageDraw.Draw(im).text((20,115),char,font=font,anchor='ls',fill=0)
                    feat=features(im)
                    if feat is None:continue
                    mask,metric=feat
                    masks.append(mask);metrics.append(metric);labels.append(char);names.append(f'{name}:{index}')
            if not labels:raise RuntimeError('No supported template fonts found; set WAKG_TEMPLATE_FONTS')
            temp=directory/'templates.tmp.npz'
            np.savez_compressed(temp,masks=masks,metrics=metrics,labels=labels,fonts=names)
            temp.replace(path)
        with np.load(path) as data:
            masks=data['masks'];self.metrics=data['metrics'];self.labels=data['labels'].tolist();self.names=data['fonts'].tolist()
        self.flat=masks.reshape(len(masks),-1).astype(np.float32)
        self.area=self.flat.sum(1)
        self.distances=np.array([distance_transform_edt(~m).reshape(-1) for m in masks],dtype=np.float32)
        self.memo={}

    def match(self, feat):
        mask,metric=feat;key=(mask.tobytes(),metric.tobytes())
        if key in self.memo:return self.memo[key]
        f=mask.reshape(-1).astype(np.float32)
        dt=distance_transform_edt(~mask).reshape(-1).astype(np.float32)
        scores=(self.distances@f/f.sum()+self.flat@dt/self.area)/2+2*(np.abs(self.metrics-metric)@np.ones(3))
        result=[];seen=set()
        for i in np.argsort(scores):
            text=canonical(self.labels[i])
            if text in seen:continue
            seen.add(text)
            result.append({'text':text,'glyph':self.labels[i],'font':self.names[i],'distance':float(scores[i])})
            if len(result)==5:break
        self.memo[key]=result
        return result

@functools.lru_cache(maxsize=2)
def matcher(directory):
    return Matcher(directory)

def glyph_features(top, gid):
    glyph=top.CharStrings[top.charset[gid]]
    pen=SVGPathPen(None);glyph.draw(pen)
    bounds=BoundsPen(None);glyph.draw(bounds)
    if bounds.bounds is None:return None
    scale=abs(top.FontMatrix[0])*100
    x0,y0,x1,y1=bounds.bounds
    left=min(-10,x0*scale-8);right=max(110,x1*scale+8)
    bottom=min(-35,y0*scale-8);height=max(115,y1*scale+8)
    svg=(f'<svg xmlns="http://www.w3.org/2000/svg" width="{right-left}" height="{height-bottom}">'
         f'<path transform="translate({-left},{height}) scale({scale},{-scale})" d="{pen.getCommands()}"/></svg>').encode()
    with fitz.open(stream=svg,filetype='svg') as vector:
        with fitz.open(stream=vector.convert_to_pdf(),filetype='pdf') as rendered:
            pix=rendered[0].get_pixmap(alpha=False)
            return features(Image.frombytes('RGB',(pix.width,pix.height),pix.samples),height)

def labels_for_fonts(doc, items, cache_path=None):
    directory=Path(cache_path).parent if cache_path else cache_root()
    path=Path(cache_path) if cache_path else directory/'labels.json'
    labels=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    changed=False;unsupported=[]
    for item in items:
        program=doc.extract_font(item['xref'])
        if program[1]!='cff':
            unsupported.append({'font':item['font'],'format':program[1]});continue
        cff=None
        for occ in item['occurrences']:
            key=item['font_sha256']+':'+str(occ['gid'])
            if key in labels or occ['decoded'].isspace():continue
            if cff is None:
                cff=CFFFontSet();cff.decompile(io.BytesIO(program[3]),None)
            feat=glyph_features(cff[cff.fontNames[0]],occ['gid'])
            if feat is None:continue
            candidates=matcher(str(directory)).match(feat)
            labels[key]={'text':candidates[0]['text'],'context_sensitive':False,
                         'method':VERSION,'candidates':candidates}
            changed=True
    if changed:
        directory.mkdir(parents=True,exist_ok=True)
        temp=path.with_suffix('.tmp');temp.write_text(json.dumps(labels,ensure_ascii=False,indent=2),encoding='utf-8');temp.replace(path)
    return labels,unsupported
