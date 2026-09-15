"""Resolve caption-only figure locators using explicit native layout evidence."""
import re
import copy
import fitz


def layout(page):
    return {'width':page.rect.width,'height':page.rect.height,
        'images':[list(i['bbox']) for i in page.get_image_info()],
        'blocks':[{'bbox':list(b[:4]),'text':b[4]} for b in page.get_text('blocks') if len(b)>6 and b[6]==0]}


def group_before(page,caption_box,caption_text):
    area=page['width']*page['height'];cap=fitz.Rect(caption_box)
    blockers=[fitz.Rect(b['bbox']).y1 for b in page['blocks']
        if fitz.Rect(b['bbox']).y1<cap.y0 and len(b['text'].strip())>4
        and not re.fullmatch(r'\s*\([a-z]\)\s*',b['text'])]
    floor=max(blockers,default=0)
    candidates=[fitz.Rect(b) for b in page['images'] if fitz.Rect(b).get_area()>area*0.01
                and b[1]>=floor and b[3]<=cap.y0]
    if not candidates:raise ValueError('no source graphics above caption')
    panels=set(re.findall(r'\(([a-z])\)',caption_text))
    if len(candidates)>1 and len(panels)<len(candidates):raise ValueError('multiple graphics without panel grouping evidence')
    union=fitz.Rect(candidates[0])
    for b in candidates[1:]:union|=b
    if cap.y0-union.y1>page['height']*0.10:raise ValueError('caption too far from graphic group')
    return list(union)


def resolve_layout(current,previous,source):
    item=source['source_object'];cap=item.get('bbox') or source['bbox']
    if item.get('region_bbox'):
        return {'page':source['page'],'bbox':item['region_bbox'],'caption_page':source['page'],
                'caption_bbox':cap,'method':'EXPLICIT_REGION'}
    # Cross-page association needs the source's own continuation marker.
    markers=[] if previous is None else [b for b in previous['blocks']
        if re.search(r'caption\s+on\s+(?:the\s+)?next\s+page',b['text'],re.I)]
    if markers and cap[1]<current['height']*0.2:
        if len(markers)!=1:raise ValueError('ambiguous cross-page caption markers')
        box=group_before(previous,markers[0]['bbox'],source['text'])
        return {'page':source['page']-1,'bbox':box,'caption_page':source['page'],
            'caption_bbox':cap,'method':'EXPLICIT_CAPTION_ON_NEXT_PAGE','marker':markers[0]}
    box=group_before(current,cap,source['text'])
    return {'page':source['page'],'bbox':box,'caption_page':source['page'],
            'caption_bbox':cap,'method':'NATIVE_GRAPHICS_BEFORE_CAPTION'}


def resolve(pdf,source):
    # A compiled source may already point to the figure page while retaining
    # its caption's native coordinates on the following page.
    caption_page=source['source_object'].get('caption_page') or source['source_object'].get('page') or source['page']
    scoped=copy.deepcopy(source);scoped['page']=caption_page
    result=resolve_layout(layout(pdf[caption_page-1]),layout(pdf[caption_page-2]) if caption_page>1 else None,scoped)
    if caption_page!=source['page']:
        if result['page']!=source['page']:raise ValueError('resolved source page contradicts compiled figure page')
        expected=fitz.Rect(source['bbox']);actual=fitz.Rect(result['bbox'])
        if (expected&actual).get_area()<0.98*max(expected.get_area(),actual.get_area()):raise ValueError('resolved source region contradicts compiled figure region')
    return result
