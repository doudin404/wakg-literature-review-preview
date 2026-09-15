"""Join split negative quantities using intact negatives in the same column."""
import copy
import re

def propose(cells, boundaries=()):
    proposals=[]
    rows={}
    for c in cells:rows.setdefault(c['row'],[]).append(c)
    for row in rows.values():
        row=sorted(row,key=lambda c:c['column'])
        for a,b in zip(row,row[1:]):
            if a['text'] not in ('-','−','–') or not re.fullmatch(r'\d+(?:\.\d+)?',b['text']):continue
            if not a.get('bbox') or not b.get('bbox'):continue
            x,y,r,z=a['bbox'];bx,by,br,bz=b['bbox']
            if abs(y-by)>.5 or abs(z-bz)>.5 or not 0<=bx-r<=.25*(bz-by):continue
            if any(r<=edge<=bx for edge in boundaries):continue
            peers=[c for c in cells if c['row']!=a['row'] and c.get('bbox')
                   and re.fullmatch(r'[-−–]\d+(?:\.\d+)?',c['text']) and abs(c['bbox'][0]-x)<=1
                   and br<=c['bbox'][2]+(c['bbox'][2]-c['bbox'][0])/len(c['text'])]
            if len({c['row'] for c in peers})<2:continue
            proposals.append({'source_ids':[a['id'],b['id']], 'text':'-'+b['text'],
                              'bbox':[x,min(y,by),br,max(z,bz)],'peer_ids':[c['id'] for c in peers]})
    return proposals

def merge_rows(rows, boundaries=()):
    cells=[dict(c,id=f'{r}:{i}',row=r,column=i) for r,row in enumerate(rows) for i,c in enumerate(row)]
    changes=propose(cells,boundaries)
    starts={p['source_ids'][0]:p for p in changes}
    removed={p['source_ids'][1] for p in changes}
    by_id={c['id']:c for c in cells}
    output=[]
    for r,row in enumerate(rows):
        merged=[]
        for i,c in enumerate(row):
            key=f'{r}:{i}'
            if key in removed:continue
            value=copy.deepcopy(c)
            if key in starts:
                p=starts[key]
                value.update(text=p['text'],bbox=p['bbox'],source_fragments=[
                    {'text':by_id[k]['text'],'bbox':by_id[k]['bbox']} for k in p['source_ids']])
            merged.append(value)
        output.append(merged)
    return output
