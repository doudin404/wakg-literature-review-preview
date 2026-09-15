"""Diagnostic source/overlay pair reconstructed from calibrated candidates."""
import argparse
import hashlib
import json
from pathlib import Path
import fitz
from PIL import Image,ImageDraw
from sparse_vector_observations import extract
from vector_curves import coordinate,atomic_bytes
import io


def render(pdf,spec,candidates,output=None):
    if extract(pdf,spec)!=candidates:raise ValueError('overlay candidate differs from source reconstruction')
    box=fitz.Rect(spec['plot_bbox'])+(-25,-8,12,26);scale=4
    with fitz.open(pdf) as doc:
        pix=doc[spec['page']-1].get_pixmap(matrix=fitz.Matrix(scale,scale),clip=box,alpha=False)
    original=Image.frombytes('RGB',(pix.width,pix.height),pix.samples)
    overlay=original.copy();draw=ImageDraw.Draw(overlay)
    pixel=lambda p:(p[0]*scale-pix.x,p[1]*scale-pix.y)
    for observation in candidates['observations']:
        p=[coordinate(observation['age_from_axis'],spec['x_axis'],True),coordinate(observation['value_from_axis'],spec['y_axis'],True)]
        x,y=pixel(p)
        draw.ellipse((x-8,y-8,x+8,y+8),outline='#ff8000',width=2)
        for cap in observation['error_caps']:
            y=coordinate(cap['value_from_axis'],spec['y_axis'],True)
            a,b=cap['pdf_segment'];draw.line([pixel([a[0],y]),pixel([b[0],y])],fill='#00b4cc',width=2)
        for stem in observation['error_stems']:
            draw.line([pixel(p) for p in stem['pdf_segment']],fill='#7b00d4',width=1)
    pair=Image.new('RGB',(pix.width*2+20,pix.height+40),'white')
    pair.paste(original,(0,40));pair.paste(overlay,(pix.width+20,40))
    draw=ImageDraw.Draw(pair);draw.text((10,10),'SOURCE',fill='black')
    draw.text((pix.width+30,10),'ORANGE: centers / CYAN: caps / PURPLE: native stems',fill='black')
    buffer=io.BytesIO();pair.save(buffer,format='PNG');data=buffer.getvalue()
    if output is not None:
        if output.exists() and output.read_bytes()!=data:raise ValueError('immutable overlay conflict')
        atomic_bytes(output,data)
    return {'sha256':hashlib.sha256(data).hexdigest(),'points':len(candidates['observations']),
            'caps':sum(len(o['error_caps']) for o in candidates['observations']),'source_pdf_unchanged':True}


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    for name in ('pdf','spec','candidates','output'):parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(render(args.pdf,json.loads(args.spec.read_text(encoding='utf-8')),
                           json.loads(args.candidates.read_text(encoding='utf-8')),args.output)))
