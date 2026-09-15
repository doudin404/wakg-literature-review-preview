"""One paper, one semantic draft, deterministic assembly; optional debug review."""
import argparse
import copy
import json
import re
import time
import os
import tempfile
from pathlib import Path

import fitz

from paper_source_index import build as index_paper
from pdf_symbol_text import repair_symbol_encoding
from source_table_cells import native_matrix
from source_quantity_producer import atomic_json, atomic_text, invoke, obj
from source_anchor_match import resolved_mapping, resolve_anchor, positioning_anchors
from draft_error_retry import fact_error, repair as repair_draft, repair_assembly


def load(path):
    return json.loads(Path(path).read_bytes())


def prepare(pdf, supplements=(), *, annotate_fonts=True):
    index = index_paper(pdf, list(supplements))
    # Font templates are resolved locally by the source reader. Semantic cleanup
    # belongs to the same whole-paper extraction call, not a glyph Agent call.
    packet = {'document': index['document'], 'font_annotation':index.get('font_annotation', {}), 'sources': [], 'tables': [], 'figures': [],
              'supplements': index['supplements'], 'supplement_references': index['supplement_references']}
    assets = [{'sha256': s['sha256'], 'relative_path': s['relative_path']}
              for s in index['supplements'] if s.get('relative_path')]
    for source in index['sources']:
        if source['kind'] == 'text':
            packet['sources'].append({**source, 'id': 'B' + str(len(packet['sources']) + 1)})
        elif source['kind'] == 'table':
            table = native_matrix(index, source, index['sources'], assets)
            tid = 'T' + str(len(packet['tables']) + 1)
            cells = [{**cell, 'id': f'{tid}:{r}:{c}', 'row': r, 'column': c}
                     for r, row in enumerate(table['rows']) for c, cell in enumerate(row)]
            packet['tables'].append({'id': tid, 'source': source, 'table': table, 'cells': cells})
        elif source['kind'] == 'figure':
            packet['figures'].append(copy.deepcopy(source))
    return packet


def visible(packet):
    # Select prose by major sections; tables remain independent floating objects.
    main=[s for s in packet['sources'] if s.get('document_role')!='supplement']
    midpoints={p:(min(s['bbox'][0] for s in main if s.get('page')==p and s.get('bbox'))+
                   max(s['bbox'][2] for s in main if s.get('page')==p and s.get('bbox')))/2
               for p in {s.get('page') for s in main if s.get('bbox')}}
    ordered=sorted(main,key=lambda s:(s.get('page') or 0, (s.get('bbox') or [0])[0]>=midpoints.get(s.get('page'),float('inf')),
                                     (s.get('bbox') or [0,0])[1]))
    chosen=set();active=False;found=False
    for s in ordered:
        heading=s.get('text','').splitlines()[0].strip()
        if re.match(r'^\d+\.\s+(?:Experimental|Materials|Methods)',heading,re.I):
            active=True;found=True
        inside_table=any(t['source'].get('document_sha256')==s.get('document_sha256')
            and t['table']['page']==s.get('page') and t['table'].get('bbox')
            and fitz.Rect(t['table']['bbox']).contains(fitz.Rect(s['bbox']))
            for t in packet['tables'] if s.get('bbox'))
        if not inside_table and (re.match(r'^\d+\.\s+(?:Conclusions?|Summary and conclusions)',heading,re.I) or heading.lower() in ('references','acknowledgements','acknowledgments')):
            active=False
        if active:chosen.add(s['id'])
    tables = []
    regions = []
    for t in packet['tables']:
        cells = t['cells']
        xs = sorted({round(c['bbox'][0], 1) for c in cells if c.get('bbox')})
        lanes = []
        for x in xs:
            if not lanes or x - lanes[-1] > 4:
                lanes.append(x)
        rows = {}
        for c in cells:
            lane = min(range(len(lanes)), key=lambda n: abs(lanes[n]-c['bbox'][0])) if lanes and c.get('bbox') else c['column']
            rows.setdefault(c['row'], []).append([c['column'], c['text'], lane])
        tables.append({'id':t['id'], 'page':t['table']['page'],
                       'caption':t['table'].get('caption'),
                       'rows':[[r, v] for r,v in sorted(rows.items())]})
        boxes = [c['bbox'] for c in cells if c.get('bbox')]
        if boxes:
            regions.append((t['source'].get('document_sha256'), t['table']['page'],
                            [min(b[0] for b in boxes),min(b[1] for b in boxes),
                             max(b[2] for b in boxes),max(b[3] for b in boxes)]))
    def table_body(s):
        b=s.get('bbox')
        return b and any(s.get('document_sha256')==sha and s.get('page')==page and
                         b[0]>=r[0]-2 and b[1]>=r[1]-2 and b[2]<=r[2]+2 and b[3]<=r[3]+2
                         for sha,page,r in regions)
    return {'font_reading': packet.get('font_annotation', {}),
            'text': [{k: s.get(k) for k in ('id', 'page', 'document_role', 'text')}
                     for s in packet['sources'] if not table_body(s) and
                     (not found or s['id'] in chosen or s.get('document_role')=='supplement' or
                      re.match(r'^(?:Note:|Notes:|Table\s+\d|Fig\.?\s*\d)',s.get('text','')))],
            'table_format': 'rows = [row_number, cells]; each cell = [column_number, original_text, alignment_lane]. Cell ID is table_id:row_number:column_number. Lanes indicate horizontal alignment, not PDF coordinates. Header rows, units and missing symbols are retained verbatim. Table notes outside the body remain in text with source IDs.',
            'tables': tables,
            'figures':[{k:f.get(k) for k in ('source_id','page','text','document_role')} for f in packet.get('figures',[])],
            'supplements': packet['supplements'],
            'supplement_references': packet['supplement_references']}


def draft_schema():
    text = {'type': 'string'}
    nullable = {'type': ['string', 'null']}
    refs = {'type': 'array', 'items': text}
    ref = obj({'id': text, 'quote': text, 'token': text})
    anchor = obj({'ids': refs, 'text': text})
    quantity = obj({'text': text, 'unit': nullable, 'ref': ref})
    field = obj({'kind': text, 'name': text, 'unit': nullable, 'component_id': nullable,
                 'destination': nullable, 'basis': nullable, 'specimen': nullable,
                 'method': nullable, 'age': {'anyOf': [quantity, {'type': 'null'}]}})
    return obj({
        'materials': {'type': 'array', 'items': obj({'id': text, 'label': text,
            'material_type': nullable, 'aliases': refs, 'refs': {'type': 'array', 'items': ref}})},
        'mixes': {'type': 'array', 'items': obj({'id': text, 'label': text,
            'specimen_type': nullable, 'refs': {'type': 'array', 'items': ref}})},
        'tables': {'type': 'array', 'items': obj({'id': text, 'orientation': text,
            'owners': {'type': 'array', 'items': obj({'ids': refs, 'anchor': anchor})},
            'fields': {'type': 'array', 'items': obj({'anchors': {'type':'array','items':anchor}, 'field': field})},
            'cells': {'type': 'array', 'items': obj({'ref': anchor, 'owners': refs,
                                                   'field_index': {'type': 'integer'}})}})},
        'facts': {'type': 'array', 'items': obj({'owners': refs, 'field': field,
            'text': text, 'ref': ref, 'context': refs})},
        'notes': {'type': 'array', 'items': obj({'source_ids': refs, 'text': text})},
        'figure_tasks': {'type':'array','items':obj({'source_id':text,'kind':text,'owners':refs,'context':text,'specimen_type':nullable})}
    })


PROMPT = '''Read this paper as one study, including its supplied supplement text and tables.
Build a single draft of materials, formulations, specimens, shared preparation/curing
conditions and measured properties. Use materials/methods to interpret results anywhere
in the paper. Return the JSON draft. Scripts will read cell values, convert units and
locate each cited value in the original document.
For each supplied figure caption, return one figure_tasks entry: source_id, kind
(psd/ftir/xrd/nmr/bar/scatter/pie/other), owners using your material/mix IDs,
specimen_type (null for mixed/unspecified types), and context explaining the property,
age, test-specimen relationship, axis meaning or NMR isotope when stated.
Use kind skip with a reason for non-data illustrations. The image stage reads values.

materials lists source-defined raw materials, aggregates and additives with local IDs,
labels, types, aliases and source refs. mixes lists every experimental formulation,
with the original label and specimen type. Source text is the evidence for this task.

For a single-object table use orientation table_record: owners identifies the whole
table's material, and its anchor cites the caption or prose establishing ownership.
Field anchors still identify the table headers; scripts read the values below them.
Each other table mapping has orientation row_records (owners on rows) or column_records
(owners on columns). owners supplies the identity cell anchor for each record; fields
supplies the leaf header/row-label cell anchors. All rows are processed. Give the
printed unit, material component and meaning once per field. cells is an optional
list for merged cells or irregular layouts, linking a source cell to owners and a
zero-based field_index. Reuse shared values across the source-defined applicable rows.
Table input uses compact rows as described by table_format. Reconstruct cell IDs
from table ID, row and column numbers. Each anchor is {ids: [cell IDs], text: original
label}; spanning labels use multiple IDs. Return units and associations once.
Put table measurements in table mappings, and prose measurements in facts.

Field kinds:
  xrf: name is the oxide, unit wt.% and owner is MAT.
  qxrd: name is the phase, unit wt.%, basis states the reported denominator.
  spectral_assignment: text is the reported wavenumber or range, unit cm^-1;
    name is the assigned bond/vibration and method is FTIR. basis distinguishes
    measured assignments from literature/reference assignments. Use the actual
    material/specimen owners, or PAPER for a general interpretation table. Cite
    the wavenumber in ref and the assignment cells in context; one band per fact.
  recipe: name is the printed column meaning; destination is solid_materials,
    activators, fine_aggregate, coarse_aggregate, platform_ratios or reported_parameters.
    component_id links a material where applicable; basis states the mass denominator.
  performance: name is the property (e.g. compressive_strength, slump, flow_spread,
    initial_setting_time); age is the observation's own reported age, or null for
    fresh properties/unreported age. specimen and method describe the actual test.
    Each fact is one scalar result at one age for the listed owners. Separate the
    values for different owners in a sentence such as 'A and B were 10 and 20'.
  test_method: name is the tested property; text describes its method, loading rate,
    specimen geometry and scheduled ages. These are stored separately from measurements.
  field: name is an existing record JSON pointer, e.g.
    /physical_properties/specific_surface_m2_kg, /physical_properties/specific_surface_method,
    /physical_properties/true_density_kg_m3, /particle_size_distribution/d50_um,
    /modules/mixing_curing/mixing, /modules/mixing_curing/forming,
    /modules/mixing_curing/age_origin,
    /modules/mixing_curing/demoulding/time_after_mixing_seconds,
    /modules/mixing_curing/curing_stages/0/method,
    /modules/mixing_curing/curing_stages/0/temperature_C,
    /modules/mixing_curing/curing_stages/0/humidity_percent,
    /modules/mixing_curing/curing_stages/0/duration_seconds,
    /modules/mixing_curing/curing_stages/1/extensions/duration_label.
    Record temperature/humidity tolerances in stage extensions. Use until_testing
    for an open-ended stage. Source-specific material properties can use
    /extensions/reported_properties/<descriptive_name> when no standard slot applies.
    Mean particle size and median particle size (D50) are distinct properties.
    age_origin describes only the time origin, e.g. mixing_end or curing_start.
    Describe paste/concrete formulation relations in /extensions/formulation_relation.

facts handles all prose values and common conditions. Each fact gives its owners,
field, original numeric text (or concise Chinese text for categorical descriptions),
ref {id, quote, token}, and contextual block IDs. Use an exact short source quote;
token is the specific printed value to highlight. A table cell can itself be a ref.
A numeric text such as 24 with unit h is converted by scripts for a seconds field.
Preserve density type, concentration basis, precursor/aggregate distinction, specific
surface method, age and test specimen. Refer each performance observation directly
to its formulation and source; curing information is a separate association.
Keep concrete and corresponding aggregate-free paste as distinct MIX objects, linked
by the source relation. A paste setting-time or flow result belongs to its paste MIX.
For stages continued under the same conditions, expand those conditions onto each
stage and cite their original definition. Use Chinese for method descriptions.

Extract the numerical results stated in text even when the complete series appears
in figures. For this native-text/table pass, note the remaining figure-only series.
Use notes for source omissions, unavailable supplements or unresolved interpretations.
Keep distinct experimental objects with coincident labels distinct in your local IDs.
Shared prose facts may name multiple owners. Missing symbols remain missing.
Keep the response compact by using table mappings and shared facts rather than copying
every table value. The remaining optional field properties can be null.
'''


FONT_READING_PROMPT = '''
The PDF reader uses normal ToUnicode mappings and cached local OCR for missing or
suspicious mappings. Read the whole paper in context and correct residual
near-shape substitutions as part of extraction (e.g. I/l in words, v/nu in words,
decimal dots, quotation marks and mathematical minus signs). Preserve meaningful
Greek variables, signs, exponents, units and sample identities. Interpret the
source, not merely its character codes. Keep every ref.quote and ref.token verbatim
from the supplied text/cell, even when the interpreted value uses corrected text;
this retains the original coordinate link. Corrected semantic text belongs in the
draft value, not in the source reference. Describe uncertain meanings in notes.
Draft quantity units describe the ORIGINAL source unit, not the destination field
unit. A value 24 with source unit h belongs in a _seconds field as 24 h; the script
converts it to 86400 s. Review the assembled value before proposing unit changes.
'''


def extract(packet, directory):
    directory = Path(directory)
    response = directory / 'response.json'
    if response.exists():
        return load(response)
    return invoke(packet, directory, model='gpt-5.6-sol', schema=draft_schema(), visible=visible(packet),
                  prompt_text=PROMPT + FONT_READING_PROMPT, timeout=900)


def scalar(text):
    text = str(text).strip().replace('−', '-').strip(' ,;()%*†‡')
    if text in ('', '/', '-', '–', '—'):
        return None
    try:
        return float(text.replace(',', ''))
    except ValueError:
        return text


def measurement_number(value):
    """Separate a numerical observation from its printed qualifier/range."""
    if not isinstance(value, str):
        return value, None
    text = value.strip().replace('−','-')
    number = r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?'
    match = re.fullmatch(r'(?:~|≈|∼|about\s+|around\s+|approximately\s+|约\s*)('+number+r')',text,re.I)
    if match:
        return float(match[1]), {'relation':'approximate','reported_text':value}
    match = re.fullmatch('('+number+r')\s*(?:–|—|-|to)\s*('+number+')',text,re.I)
    if match:
        return None, {'relation':'range','lower':float(match[1]),'upper':float(match[2]),'reported_text':value}
    match = re.fullmatch(r'(<=|>=|≤|≥|<|>)\s*('+number+')',text)
    if match:
        return None, {'relation':match[1],'limit':float(match[2]),'reported_text':value}
    match = re.fullmatch('('+number+r')\s*±\s*('+number+')',text)
    if match:
        return float(match[1]), {'relation':'plus_minus','tolerance':float(match[2]),'reported_text':value}
    return None, {'relation':'above_instrument_range' if text.lower()=='max' else 'textual_quantity',
                  'reported_text':value}


def pointer_set(record, path, value):
    parts = path.strip('/').split('/')
    node = record
    for i, part in enumerate(parts[:-1]):
        empty = [] if parts[i + 1].isdigit() else {}
        if isinstance(node, list):
            ordinal = int(part)
            while len(node) <= ordinal:
                node.append(None)
            if node[ordinal] is None:
                node[ordinal] = empty
            node = node[ordinal]
        else:
            if node.get(part) is None:
                node[part] = empty
            node = node[part]
    if isinstance(node, list):
        ordinal = int(parts[-1])
        while len(node) <= ordinal:
            node.append(None)
        node[ordinal] = value
    else:
        node[parts[-1]] = value


def converted(value, unit, field):
    from planned_material_text import FIELDS
    from recipe_performance_consumer import UNITS
    unit = (unit or '').replace('²', '2').replace('³', '3') or None
    name = field['name'].split('/')[-1]
    target, factor = unit, 1
    if isinstance(value, str) and 'ratio' in field['name'].lower():
        # The Agent supplies ratio semantics; the reader may render the slash as |.
        if re.fullmatch(r'\d+(?:\.\d+)?\s*\|\s*\d+(?:\.\d+)?', value):
            value = re.sub(r'\s*\|\s*', '/', value)
    if isinstance(value, (float, int)):
        if field['kind'] == 'field' and name in FIELDS and unit in FIELDS[name][1]:
            target, scales = FIELDS[name]
            factor = scales[unit]
        elif name.endswith('_seconds') or name == 'age_seconds':
            target = 's'
            factor = {'s': 1, 'min': 60, 'h': 3600, 'd': 86400,
                      'days': 86400, 'hours': 3600}.get(unit, 1)
        elif field['kind'] == 'performance' and unit in UNITS.get(name, {}):
            target, factor = UNITS[name][unit]
        elif name in ('d10_um', 'd50_um', 'd90_um'):
            target, factor = 'um', {'um': 1, 'µm': 1, 'μm': 1, 'mm': 1000, 'nm': .001}.get(unit, 1)
        return value * factor, target, f'value = original * {factor}' if factor != 1 else None
    if isinstance(value, str) and unit and 'ratio' in unit and re.fullmatch(r'\d+(?:\.\d+)?/\d+(?:\.\d+)?', value):
        a, b = map(float, value.split('/'))
        return a / b, unit, f'value = {a} / {b}'
    return value, unit, None


class Evidence:
    def __init__(self, packet, records):
        self.records = records
        self.sources = {s['id']: s for s in packet['sources']}
        self.tables = {t['id']: t for t in packet['tables']}
        self.cells = {c['id']: (t, c) for t in packet['tables'] for c in t['cells']}
        self.assets = {a['sha256']: a for a in records['assets']}
        self.documents = {sha: fitz.open(a['relative_path']) for sha, a in self.assets.items()
                          if Path(a['relative_path']).suffix.lower() == '.pdf'}
        for doc in self.documents.values():
            repair_symbol_encoding(doc)

    def close(self):
        for doc in self.documents.values():
            doc.close()

    def locate(self, ref):
        table_id=ref['id'].split(':')[0]
        if table_id in self.tables and ref['id'] not in self.cells:
            table=self.tables[table_id]
            ids=resolve_anchor(table,{'ids':[ref['id']],'text':ref['token'] or ref['quote']})
            location=self.locate({**ref,'id':ids[0]})
            boxes=[self.cells[cid][1]['bbox'] for cid in ids if self.cells[cid][1].get('bbox')]
            if boxes:
                location['bbox']=[min(b[0] for b in boxes),min(b[1] for b in boxes),
                                  max(b[2] for b in boxes),max(b[3] for b in boxes)]
                location['bboxes']=boxes
            location.update(snippet=ref['token'] or ref['quote'],source_cell_ids=ids)
            return location
        if ref['id'] in self.cells:
            table, cell = self.cells[ref['id']]
            source = table['source']
            location = {'asset_key': self.assets[source['document_sha256']]['asset_key'],
                    'page': source['page'], 'bbox': cell.get('bbox'),
                    'table_number': table['table']['source_label'], 'snippet': cell['text'],
                    'source_locator': cell.get('source_locator'), 'source_cell_id': cell['id']}
            token=ref.get('token','')
            if re.fullmatch(r'\s*\d+(?:\.\d+)?(?:\s*,\s*\d+(?:\.\d+)?)+\s*',token):
                row=sorted([c for c in table['cells'] if c['row']==cell['row']
                            and c['column']>=cell['column']],key=lambda c:c['column'])
                selected=[]
                for candidate in row:
                    selected.append(candidate)
                    joined=re.sub(r'\s','', ''.join(c['text'] for c in selected))
                    target=re.sub(r'\s','',token)
                    if joined==target:
                        boxes=[c['bbox'] for c in selected if c.get('bbox')]
                        location.update(snippet=token,source_cell_ids=[c['id'] for c in selected])
                        if boxes:location['bboxes']=boxes
                        break
                    if not target.startswith(joined):break
            return location
        source = self.sources.get(ref['id'])
        if source:
            location = self.locate_block(source, ref)
            if location.get('bbox') or location.get('source_locator'):
                return location
        # Search the original quote throughout the document, including block breaks.
        def compact(text):
            return re.sub(r'\s+|\u00ad|-\s*\n\s*', '', text).casefold()
        quote = compact(ref.get('quote',''))
        token = compact(ref.get('token',''))
        if not quote:
            return self.locate_token(ref, source)
        groups = {}
        for item in self.sources.values():
            if source and item['document_sha256'] != source['document_sha256']:
                continue
            groups.setdefault(item['document_sha256'], []).append(item)
        candidates = []
        for items in groups.values():
            pages = {}
            for item in items:
                pages.setdefault(item.get('page') or 0, []).append(item)
            ordered = []
            for page_items in pages.values():
                boxes = [s['bbox'] for s in page_items if s.get('bbox')]
                middle = (min(b[0] for b in boxes)+max(b[2] for b in boxes))/2 if boxes else float('inf')
                ordered.extend(sorted(page_items, key=lambda s:((s.get('bbox') or [0])[0]>=middle,
                                                               (s.get('bbox') or [0,0])[1])))
            ordered.sort(key=lambda s:s.get('page') or 0)
            joined=''; offsets=[]
            for item in ordered:
                text=compact(item['text'])
                offsets.append((len(joined),len(joined)+len(text),item))
                joined+=text
            start=joined.find(quote)
            while start>=0:
                token_at=quote.find(token) if token else -1
                begin=start+token_at if token_at>=0 else start
                end=begin+len(token) if token_at>=0 else start+len(quote)
                for lo,hi,item in offsets:
                    if hi>begin and lo<end:
                        location=self.locate_block(item,ref)
                        if location.get('bbox') or location.get('source_locator'):
                            candidates.append((item,location))
                start=joined.find(quote,start+1)
            # A sentence can cross pages with running headers or figure labels
            # between its pieces. Match exact consecutive quote pieces, skipping
            # unrelated layout blocks rather than limiting the search to neighbours.
            for i,item in enumerate(ordered):
                text=compact(item['text'])
                for offset in [m.start() for m in re.finditer(re.escape(quote[:12]),text)]:
                    consumed=0; spans=[]
                    for part in ordered[i:]:
                        piece=compact(part['text'])
                        if part is item:piece=piece[offset:]
                        remaining=quote[consumed:]
                        n=0
                        while n<min(len(piece),len(remaining)) and piece[n]==remaining[n]:n+=1
                        if n and (n==len(piece) or n==len(remaining)):
                            spans.append((consumed,consumed+n,part)); consumed+=n
                        elif part is item:
                            break
                        if consumed==len(quote):
                            at=quote.find(token) if token else -1
                            for lo,hi,part in spans:
                                if at<0 or (hi>at and lo<at+len(token)):
                                    loc=self.locate_block(part,ref)
                                    if loc.get('bbox') or loc.get('source_locator'):
                                        candidates.append((part,loc))
                            break
        if not candidates:
            return self.locate_token(ref, source)
        origin_page=(source or {}).get('page') or 0
        origin_box=(source or {}).get('bbox') or [0,0,0,0]
        def distance(candidate):
            item,loc=candidate
            box=loc.get('bbox') or item.get('bbox') or [0,0,0,0]
            return (abs((item.get('page') or 0)-origin_page),
                    abs(box[0]-origin_box[0])+abs(box[1]-origin_box[1]))
        chosen,location=min(candidates,key=distance)
        location['resolved_block_id']=chosen['id']
        return location

    def locate_token(self, ref, source=None):
        """Recover a failed quote using literal word spans nearest its origin."""
        def norm(text):
            return re.sub(r'[\s\u00ad]', '', text).strip('.,;:()[]').casefold()
        target=norm(ref.get('token') or ref.get('quote') or '')
        if not target:
            raise ValueError('No source text supplied')
        origin_page=(source or {}).get('page') or 1
        origin_box=(source or {}).get('bbox') or [0,0,0,0]
        candidates=[]
        for sha,doc in self.documents.items():
            if source and sha!=source['document_sha256']:continue
            pages=[]
            for page in doc:
                blocks={}
                for word in page.get_text('words'):
                    blocks.setdefault(word[5],[]).append((page.number+1,word))
                pages.append(list(blocks.values()))
            for pi,blocks in enumerate(pages):
                for block in blocks:
                    for start in range(len(block)):
                        joined='';span=[]
                        for entry in block[start:]:
                            joined+=norm(entry[1][4]);span.append(entry)
                            if not target.startswith(joined):break
                            if joined==target:
                                candidates.append((sha,list(span)));break
                        else:
                            # A literal phrase may continue after a page break.
                            if joined and target.startswith(joined) and pi+1<len(pages):
                                for continuation in pages[pi+1]:
                                    value=joined;combined=list(span)
                                    for entry in continuation:
                                        value+=norm(entry[1][4]);combined.append(entry)
                                        if not target.startswith(value):break
                                        if value==target:
                                            candidates.append((sha,combined));break
        if not candidates:
            raise ValueError('Original quote/value not located in document')
        def distance(candidate):
            page,word=candidate[1][0]
            return (abs(page-origin_page),abs(word[0]-origin_box[0])+abs(word[1]-origin_box[1]))
        sha,span=min(candidates,key=distance)
        locations=[{'page':p,'bbox':list(w[:4]),'snippet':w[4]} for p,w in span]
        return {'asset_key':self.assets[sha]['asset_key'],'page':locations[0]['page'],
                'bbox':locations[0]['bbox'],'bboxes':[x['bbox'] for x in locations if x['page']==locations[0]['page']],
                'locations':locations,'snippet':ref.get('token') or ref.get('quote'),
                'resolution':'literal_token_nearest_origin'}

    def locate_block(self, source, ref):
        location = {'asset_key': self.assets[source['document_sha256']]['asset_key'],
                    'page': source['page'], 'bbox': None, 'table_number': None,
                    'snippet': ref['token'] or ref['quote'], 'source_id': source['source_id']}
        doc = self.documents.get(source['document_sha256'])
        if doc:
            page = doc[source['page'] - 1]
            clip = fitz.Rect(source['bbox'])
            tokens = page.search_for(ref['token'], clip=clip) if ref['token'] else page.search_for(ref['quote'], clip=clip)
            if len(tokens)>1 and ref['quote']:
                quotes=page.search_for(ref['quote'],clip=clip)
                if quotes:
                    quote_box=fitz.Rect(quotes[0])
                    for rect in quotes[1:]:quote_box|=rect
                    chosen=min(tokens,key=lambda rect:abs(rect.x0-quote_box.x0)+abs(rect.y0-quote_box.y0))
                    inside=[rect for rect in tokens if quote_box.intersects(rect)]
                    tokens=inside or [chosen]
            if not tokens and ref['token']:
                # Preserve the boxes of a word split by PDF discretionary hyphens.
                words = page.get_text('words', clip=fitz.Rect(source['bbox']))
                for i, word in enumerate(words):
                    joined = word[4]
                    for j in range(i + 1, min(i + 4, len(words))):
                        if not joined.endswith(('\u00ad', '-')):
                            break
                        joined = joined[:-1] + words[j][4]
                        if joined.casefold() == ref['token'].casefold():
                            tokens = [fitz.Rect(w[:4]) for w in words[i:j + 1]]
                            location['bboxes'] = [list(r) for r in tokens]
                            break
                    if tokens:
                        break
            if tokens:
                location['bbox'] = list(tokens[0])
                if not ref['token']:
                    location['bboxes'] = [list(r) for r in tokens]
        else:
            location['source_locator'] = {'coordinate_space': source.get('coordinate_space'),
                                           **source.get('source_object', {})}
        return location

    def attach(self, record, path, value, unit, ref, *, formula=None, context=()):
        try:
            location = self.locate(ref)
        except (KeyError, ValueError, IndexError) as error:
            location = {'bbox':None,'page':None,'snippet':ref.get('token') or ref.get('quote'),
                        'source_error':str(error)}
        if not location.get('bbox') and not location.get('source_locator'):
            self.records['extensions'].setdefault('source_issues', []).append(
                {'record_key':record.get('mat_key') or record.get('mix_key'),
                 'field_path':path,'source':copy.deepcopy(ref),
                 'reason':location.get('source_error','Text location not found')})
        key = record.get('mat_key') or record.get('mix_key') or record['paper_key']
        evkey = 'ev-paper-' + str(len(self.records['evidence_links']) + 1)
        self.records['evidence_links'].append({'evidence_key': evkey, 'record_key': key,
            'record_type': 'mat' if 'mat_key' in record else 'mix' if 'mix_key' in record else 'paper', 'field_path': path,
            **location, 'extraction_method': 'paper-native-flow',
            'extensions': {'context_source_ids': list(context), 'source_ref': ref}})
        record.setdefault('field_provenance', {})[path] = {'evidence_key': evkey, 'original_value': value,
            'original_unit': unit, 'formula': formula, 'extraction_method': 'paper-native-flow',
            'review_status': 'pending', 'source_type': 'DERIVED' if formula else 'DIRECT_REPORTED'}
        return evkey


def table_facts(packet, draft, issues=None):
    """Expand all mapped rows/columns mechanically, including explicit shared cells."""
    tables = {t['id']: t for t in packet['tables']}
    output = []
    issues = issues if issues is not None else []
    for mapping in draft['tables']:
        table = tables.get(mapping['id'])
        if table is None:
            # Mapping names may have a semantic suffix; native cell addresses
            # still identify the one source table without another model call.
            referenced = set(re.findall(r'\b(T\d+):\d+:\d+', json.dumps(mapping))) & set(tables)
            if len(referenced) == 1:
                table = tables[referenced.pop()]
                mapping = {**mapping, 'id':table['id']}
        if table is None:
            issues.append({'table':mapping['id'],'reason':'Source table not found'})
            continue
        mapping = resolved_mapping(table, mapping, issues)
        cells = {c['id']: c for c in table['cells']}
        xpos = lambda c: c['bbox'][0] if c.get('bbox') else c['column'] * 100
        selected = {}
        field_positions = positioning_anchors(mapping['fields'])
        anchors = [cells[o['anchor']] for o in mapping['owners']
                   if isinstance(o['anchor'], str) and o['anchor'] in cells]
        orientation = mapping['orientation']
        if len({a['row'] for a in anchors}) > 1 and len({round(xpos(a) / 5) for a in anchors}) == 1:
            orientation = 'row_records'
        elif len({a['row'] for a in anchors}) == 1 and len({round(xpos(a) / 5) for a in anchors}) > 1:
            orientation = 'column_records'
        if orientation == 'table_record':
            positions = {i:min(xpos(cells[a]) for a in anchors)
                         for i,anchors in enumerate(field_positions) if anchors}
            header_end = max((cells[a]['row'] for f in mapping['fields']
                              for a in f['anchors']), default=-1)
            for i, position in positions.items():
                candidates = [c for c in cells.values() if c['row'] > header_end
                              and min(positions, key=lambda k: abs(xpos(c)-positions[k])) == i]
                if candidates:
                    chosen = min(candidates, key=lambda c: (c['row'], abs(xpos(c)-position)))
                    for owner in mapping['owners']:
                        for owner_id in owner['ids']:
                            selected[owner_id, i] = chosen['id']
        elif orientation == 'row_records':
            positions = {i:min(xpos(cells[a]) for a in anchors)
                         for i,anchors in enumerate(field_positions) if anchors}
            for owner in mapping['owners']:
                anchor = cells[owner['anchor']]
                # A ratio/dosage cell can identify a row and also be a value.
                # Only textual identity labels are excluded from value columns.
                labels=[cells[cid] for cid in owner.get('context_anchors',[owner['anchor']])
                        if re.search(r'[A-Za-z\u4e00-\u9fff]',cells[cid]['text'])]
                column_starts = list(positions.items()) + [(None,xpos(c)) for c in labels]
                body = [c for c in cells.values() if c['row'] == anchor['row']]
                for i, field in enumerate(mapping['fields']):
                    if i not in positions:continue
                    candidates = [c for c in body if min(column_starts,
                        key=lambda entry: abs(xpos(c) - entry[1]))[0] == i]
                    if candidates:
                        chosen = min(candidates, key=lambda c: abs(xpos(c) - positions[i]))
                        for owner_id in owner['ids']:
                            selected[owner_id, i] = chosen['id']
        elif orientation == 'column_records':
            positions = [xpos(cells[o['anchor']]) for o in mapping['owners']]
            for i, field in enumerate(mapping['fields']):
                anchors = [cells[a] for a in field_positions[i]]
                if not anchors:continue
                label_rows = {c['row'] for c in anchors}
                body = [c for c in cells.values() if c['row'] in label_rows]
                column_starts = positions + [min(xpos(c) for c in anchors)]
                for j, owner in enumerate(mapping['owners']):
                    candidates = [c for c in body if min(range(len(column_starts)),
                        key=lambda k: abs(xpos(c) - column_starts[k])) == j]
                    if candidates:
                        chosen = min(candidates, key=lambda c: abs(xpos(c) - positions[j]))
                        for owner_id in owner['ids']:
                            selected[owner_id, i] = chosen['id']
        for explicit in mapping.get('cells', []):
            for owner_id in explicit['owners']:
                selected[owner_id, explicit['field_index']] = explicit['ref']
        produced_fields = {i for owner_id, i in selected}
        for i, field in enumerate(mapping['fields']):
            if i not in produced_fields:
                issues.append({'table':mapping['id'], 'part':'field', 'field':field['field']['name'],
                               'source':field['anchors'], 'reason':'MAPPED_FIELD_NO_VALUES: mapped field produced no cells'})
        for (owner_id, i), cid in selected.items():
            cell = cells[cid]
            label=' '.join(cells[a]['text'] for a in mapping['fields'][i]['anchors'] if a in cells)
            output.append({'owners': [owner_id], 'field': dict(mapping['fields'][i]['field'],original_label=label),
                           'text': cell['text'], 'ref': {'id': cid, 'quote': '', 'token': cell['text']},
                           'context': mapping['fields'][i]['anchors']})
    return output


def assemble(packet, draft):
    from source_mix_record import empty_source_mix
    from build_phase_c_full10 import mat_record
    from source_bibliography import enrich as bibliography
    started = time.monotonic()
    doc = packet['document']
    paper_key = 'paper-' + doc['sha256'][:12]
    records = {'schema_version': '1.0', 'papers': [{'paper_key': paper_key, 'title': None,
        'doi': None, 'year': None, 'citation': None, 'extensions': {}}],
        'mats': [], 'mixes': [], 'assets': [], 'evidence_links': [], 'quarantined': [],
        'extensions': {'workflow': 'one-paper-native-draft', 'source_notes': copy.deepcopy(draft['notes'])}}
    documents = [{'sha256': doc['sha256'], 'relative_path': doc['path']}, *packet['supplements']]
    for document in documents:
        if document.get('relative_path') and Path(document['relative_path']).is_file():
            records['assets'].append({'asset_key': 'asset-' + document['sha256'][:12],
                'paper_key': paper_key, 'sha256': document['sha256'], 'kind': 'pdf' if str(document['relative_path']).endswith('.pdf') else 'supplement',
                'relative_path': document['relative_path'], 'extensions': {}})
    owners = {'PAPER': records['papers'][0]}
    for ordinal, material in enumerate(draft['materials'], 1):
        mat = mat_record('user10v2-' + doc['sha256'][:12], paper_key,
                         {'key': str(ordinal), 'label': material['label'], 'role': material['material_type']})
        mat.update(mat_key=f'mat-{doc["sha256"][:12]}-{ordinal}', material_type=material['material_type'],
                   extensions={'source_aliases': material['aliases']})
        owners[material['id']] = mat
        records['mats'].append(mat)
    for ordinal, item in enumerate(draft['mixes'], 1):
        mix = empty_source_mix(paper_key, f'mix-{doc["sha256"][:12]}-{ordinal}', item['label'], None, None)
        mix['modules']['identity_source_specimen']['specimen_type'] = item['specimen_type']
        owners[item['id']] = mix
        records['mixes'].append(mix)
    evidence = Evidence(packet, records)
    for item in draft['materials'] + draft['mixes']:
        record = owners[item['id']]
        path = '/custom_material_id' if 'mat_key' in record else '/modules/identity_source_specimen/custom_test_id'
        for ref in item['refs']:
            evidence.attach(record, path, item['label'], None, ref)
    problems = []
    from draft_error_retry import fact_address
    overrides = {fact_address(f):f for f in draft.get('table_fact_overrides',[])}
    facts = [overrides.get(fact_address(f),f) for f in table_facts(packet, draft, problems)] + draft['facts']
    for fact in facts:
        error = fact_error(fact)
        if error:
            problems.append({'fact':fact, 'reason':error})
            continue
        for owner_id in fact['owners']:
            try:
                write_fact(records, owners[owner_id], fact, owners, evidence)
            except (KeyError, ValueError, TypeError, IndexError) as error:
                problems.append({'owner': owner_id, 'fact': fact, 'reason': str(error)})
    evidence.close()
    bibliography(records, doc['path'], 'asset-' + doc['sha256'][:12])
    records['extensions']['assembly_notes'] = problems
    from paper_record_normalization import normalize as normalize_records
    normalize_records(records)
    report = {'mats': len(records['mats']), 'mixes': len(records['mixes']),
        'table_values': len(facts) - len(draft['facts']), 'prose_facts': len(draft['facts']),
        'xrf_values': sum(len(m['xrf_composition']['rows']) for m in records['mats']),
        'recipe_values': sum(len(m['modules']['materials']['extensions']['reported_parameters']) for m in records['mixes']),
        'performance_values': sum(len(m['modules']['performance']) for m in records['mixes']),
        'curing_stages': sum(len(m['modules']['mixing_curing']['curing_stages']) for m in records['mixes']),
        'assembly_notes': len(problems),
        'unlocated_fields': sum(not e.get('bbox') and not e.get('source_locator') for e in records['evidence_links']),
        'seconds': round(time.monotonic() - started, 3)}
    return records, report


def source_time_unit(value, unit, ref):
    """Read an explicit source duration such as '24 h' before unit conversion."""
    token = ref.get('token', '').strip()
    match = re.fullmatch(r'([+-]?\d+(?:\.\d+)?)\s*(s|min|h|d|hours|days)', token)
    if match and isinstance(value, (float, int)) and float(match[1]) == value:
        return match[2]
    return unit


def write_fact(records, record, fact, owners, evidence):
    field = dict(fact['field'])
    raw = scalar(fact['text'])
    if field['kind']=='field' and field['name'].endswith('_seconds'):
        field['unit'] = source_time_unit(raw, field['unit'], fact['ref'])
    numeric, qualifier = measurement_number(raw) if field['kind'] in ('performance','qxrd','xrf') else (raw, None)
    value, unit, formula = converted(numeric, field['unit'], field)
    if qualifier and numeric is None:
        _, unit, bound_formula = converted(1, field['unit'], field)
        formula = 'range/limit: '+bound_formula if bound_formula else None
    kind, name = field['kind'], field['name']
    if kind == 'spectral_assignment':
        ref=dict(fact['ref'])
        if ref['id'] in getattr(evidence,'cells',{}):
            _,cell=evidence.cells[ref['id']]
            printed=cell['text'].strip()
            if printed.startswith(('~','≈','∼')) and scalar(printed[1:].strip())==raw:
                raw=printed
            if re.fullmatch(r'\s*\d+(?:\.\d+)?(?:\s*,\s*\d+(?:\.\d+)?)+\s*',str(raw)):
                ref['token']=str(raw)
        rows = record.setdefault('extensions', {}).setdefault('spectral_assignments', [])
        path = f'/extensions/spectral_assignments/{len(rows)}/wavenumber'
        rows.append({'wavenumber': raw, 'unit': field['unit'], 'assignment': name,
                     'method': field['method'], 'reported_basis': field['basis']})
        evidence.attach(record, path, raw, field['unit'], ref, context=fact['context'])
        return
    if 'mix_key' not in record and kind in ('recipe', 'performance'):
        # The original MAT contract has no recipe/performance module. Preserve
        # these quantities in its existing extension area, with the same source.
        rows = record.setdefault('extensions', {}).setdefault('reported_observations', [])
        item = copy.deepcopy(fact)
        item.update(schema_version='1.0', routing='MAT_EXTENSION' if 'mat_key' in record else 'PAPER_EXTENSION',
                    owners=[key for key, owner in owners.items() if owner is record],
                    value=raw, unit=field['unit'], original_value=raw, original_unit=field['unit'])
        if field.get('component_id'):
            item['component_mat_key'] = owners[field['component_id']]['mat_key']
        index = len(rows)
        rows.append(item)
        item['evidence_key'] = evidence.attach(record,
            f'/extensions/reported_observations/{index}/value', raw, field['unit'],
            fact['ref'], context=fact['context'])
        return
    if kind == 'field':
        if name.startswith('/'):
            path = name
        elif name == 'specimen_label' and 'mix_key' in record:
            path = '/modules/identity_source_specimen/custom_test_id'
        else:
            # Descriptive properties stay in the existing extension container;
            # they are not new top-level contract fields or guessed densities.
            path = '/extensions/reported_properties/' + name + '/value'
            record.setdefault('extensions',{}).setdefault('reported_properties',{})[name] = {
                'value':None,'unit':unit,'basis':field.get('basis'),
                'specimen':field.get('specimen'),'method':field.get('method')}
        pointer_set(record, path, value)
    elif kind == 'xrf':
        rows = record['xrf_composition']['rows']
        path = f'/xrf_composition/rows/{len(rows)}/normalized_value'
        rows.append({'component': name, 'normalized_value': value, 'original_value': raw,
                     'status': 'pending' if value is not None else 'not_reported', 'reason': None,
                     'extensions': {'reported_basis': field['basis']}})
    elif kind == 'qxrd':
        if record.get('xrd_qxrd') is None:
            record['xrd_qxrd'] = {'schema_version': '1.0', 'qxrd_phase_table': [], 'extensions': {}}
        rows = record['xrd_qxrd']['qxrd_phase_table']
        path = f'/xrd_qxrd/qxrd_phase_table/{len(rows)}/value'
        rows.append({'phase': name, 'value': value, 'unit': unit, 'extensions': {'reported_basis': field['basis']}})
    elif kind == 'test_method':
        rows = record['extensions'].setdefault('test_protocols', [])
        path = f'/extensions/test_protocols/{len(rows)}/description'
        rows.append({'name': name, 'description': fact['text'], 'specimen': field['specimen'],
                     'method': field['method'], 'reported_unit': field['unit']})
    elif kind == 'recipe':
        module = record['modules']['materials']
        matkey = owners[field['component_id']]['mat_key'] if field['component_id'] else None
        if matkey and matkey not in module['mat_refs']:
            module['mat_refs'].append(matkey)
        parameters = module['extensions']['reported_parameters']
        index = len(parameters)
        path = f'/modules/materials/extensions/reported_parameters/{index}/value'
        item = {'parameter_key': name, 'original_label': field.get('original_label') or name.replace('_',' '), 'value': value,
                'unit': unit, 'original_value': raw, 'original_unit': field['unit'],
                'mat_key': matkey, 'mass_basis': field['basis'], 'basis': field['basis'],
                'status': 'reported' if value is not None else 'not_reported',
                'reason': 'source_dash' if str(raw).strip() in ('/','–','—','-') else None,
                'semantic_role': field['destination']}
        parameters.append(item)
        destination = {'binder':'solid_materials', 'activator':'activators',
                       'aggregate.fine':'fine_aggregate','aggregate.coarse':'coarse_aggregate',
                       'ratio.binder_components':'platform_ratios',
                       'ratio.activator_to_binder':'platform_ratios',
                       'ratio.activator_components':'platform_ratios',
                       'activator.solution_concentration':'reported_parameters',
                       'admixture':'reported_parameters'}.get(field['destination'],field['destination']) or 'reported_parameters'
        if destination != 'reported_parameters':
            if module.get(destination) is None:
                module[destination] = []
            rows = module[destination]
            target = f'/modules/materials/{destination}/{len(rows)}/value'
            rows.append({'name': name, 'mat_key': matkey, 'value': value, 'unit': unit,
                         'extensions': {'mass_basis': field['basis']}})
            evidence.attach(record, target, raw, field['unit'], fact['ref'], formula=formula, context=fact['context'])
    elif kind == 'performance':
        rows = record['modules']['performance']
        index = len(rows)
        path = f'/modules/performance/{index}/value'
        age = field['age']
        age_seconds = converted(scalar(age['text']), age['unit'], {'kind': 'field', 'name': 'age_seconds'})[0] if age else None
        rows.append({'name': name, 'value': value, 'unit': unit, 'age_seconds': age_seconds,
            'specimen': field['specimen'], 'method': field['method'],
            'extensions': {'original_value': raw, 'original_unit': field['unit'],
                'source_type': 'DIRECT_TABLE_CELL' if fact['ref']['id'].startswith('T') else 'DIRECT_PROSE',
                'age_basis': 'REPORTED' if age else 'NOT_REPORTED_OR_FRESH', 'reported_basis': field['basis']}})
        if age:
            evidence.attach(record, f'/modules/performance/{index}/age_seconds', scalar(age['text']),
                            age['unit'], age['ref'], formula='seconds = original * unit scale')
    else:
        record.setdefault('extensions', {}).setdefault('reported_observations', []).append(copy.deepcopy(fact))
        return
    if qualifier:
        qualifier = dict(qualifier, original_unit=field['unit'], unit=unit)
        for bound in ('lower','upper','limit','tolerance'):
            if bound in qualifier:
                qualifier[bound] = converted(qualifier[bound],field['unit'],field)[0]
        rows[-1]['extensions']['quantity_qualifier'] = qualifier
    evkey = evidence.attach(record, path, raw, field['unit'], fact['ref'], formula=formula, context=fact['context'])
    if kind == 'recipe':
        item['evidence_key'] = evkey


def apply_draft_edits(draft, edits):
    result = copy.deepcopy(draft)
    for edit in edits:
        parts = edit['path'].strip('/').split('/')
        node = result
        for part in parts[:-1]:
            node = node[int(part)] if isinstance(node, list) else node[part]
        part = parts[-1]
        if edit['op'] == 'remove':
            if isinstance(node, list):
                node.pop(int(part))
            else:
                node.pop(part)
        elif isinstance(node, list):
            if part == '-':
                node.append(copy.deepcopy(edit['value']))
            elif edit['op'] == 'add':
                node.insert(int(part), copy.deepcopy(edit['value']))
            else:
                node[int(part)] = copy.deepcopy(edit['value'])
        else:
            node[part] = copy.deepcopy(edit['value'])
    return result


def review(packet, records, draft, directory):
    prompt = '''Review one complete paper draft against the supplied full text and native
tables. Check which requested material, formulation, condition and performance facts
are present, omitted, numerically wrong, or assigned to the wrong object. Inspect
the actual evidence tokens and contexts. Return one consolidated list of concrete
corrections, including exact source IDs/tokens and intended owners. Note figure-only
data separately. Give a short verdict about this native text/table result. A record
count by itself is not a completeness measure.
This is a debug report only. Report the affected data, problem and original evidence.
Relative percentage comparisons are not required independent strength measurements.
The draft data convention is below.
''' + PROMPT + FONT_READING_PROMPT
    text = {'type': 'string'}
    schema = obj({'verdict': text, 'summary': text, 'defects': {'type': 'array', 'items': obj({
        'owner': text, 'field': text, 'problem': text, 'source_ids': {'type': 'array', 'items': text},
        'correction': text})}, 'figure_only': {'type': 'array', 'items': text}})
    # Group identical projected values across owners so shared conditions are read once.
    projected = {}
    for record in records['mats'] + records['mixes']:
        label = record.get('custom_material_id') or record['modules']['identity_source_specimen']['custom_test_id']
        for path, provenance in record['field_provenance'].items():
            node = record
            for part in path.strip('/').split('/'):
                node = node[int(part)] if isinstance(node, list) else node[part]
            ev = next(e for e in records['evidence_links'] if e['evidence_key'] == provenance['evidence_key'])
            key = json.dumps([path, node, ev.get('snippet'), ev.get('source_cell_id'), ev.get('source_id')], ensure_ascii=False)
            projected.setdefault(key, {'owners': [], 'path': path, 'value': node,
                'source_token': ev.get('snippet'), 'page': ev.get('page'),
                'source_ref': ev.get('extensions', {}).get('source_ref')})['owners'].append(label)
    indexed_draft = copy.deepcopy(draft)
    for section, items in draft.items():
        indexed_draft[section] = {str(i): item for i, item in enumerate(items)}
    prompt += '\nDraft sections are keyed by original indices for identifying problems.\n'
    request = {'source': visible(packet), 'draft': indexed_draft, 'actual_values': list(projected.values())}
    return invoke(request, directory, model='gpt-5.6-luna', reasoning_effort='medium',
                  schema=schema, visible=request, prompt_text=prompt, timeout=900)


def apply_review(draft, response):
    if 'corrections' not in response:
        return apply_draft_edits(draft, json.loads(response['draft_edits_json']))
    result = copy.deepcopy(draft)
    for section, changes in response['corrections'].items():
        for replacement in changes['replace']:
            result[section][replacement['index']] = replacement['item']
        for index in sorted(changes['remove'], reverse=True):
            del result[section][index]
        result[section].extend(changes['add'])
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['prepare', 'extract', 'assemble', 'run', 'full', 'debug-review', 'reproduce'])
    parser.add_argument('--index', help='Existing document/supplement paths; source bytes are parsed afresh')
    parser.add_argument('--pdf')
    parser.add_argument('--output', required=True)
    parser.add_argument('--draft')
    args = parser.parse_args()
    root = Path(args.output)
    if args.command in ('prepare', 'run', 'full'):
        manifest = load(args.index) if args.index else {}
        started = time.monotonic()
        packet = prepare(args.pdf or manifest['document']['path'], manifest.get('supplements', []))
        atomic_json(root / 'source-packet.json', packet)
        print(json.dumps({'prepared_seconds': round(time.monotonic() - started, 3),
                          'text_blocks': len(packet['sources']), 'tables': len(packet['tables'])}), flush=True)
    else:
        packet = load(root / 'source-packet.json')
    if args.command in ('extract', 'run', 'full'):
        draft = extract(packet, root / 'extraction')
        print(json.dumps({'materials': len(draft['materials']), 'mixes': len(draft['mixes']),
                          'table_mappings': len(draft['tables']), 'prose_facts': len(draft['facts'])}), flush=True)
    if args.command in ('assemble', 'run', 'full'):
        draft = load(args.draft or root / 'extraction/response.json')
        if args.command in ('run', 'full'):
            draft = repair_draft(packet, draft, root / 'format-retry')
            draft, records, report = repair_assembly(packet, draft, root / 'assembly-retry')
        else:
            records, report = assemble(packet, draft)
        if args.command == 'full':
            from paper_chart_bridge import integrate
            report['figures']=integrate(packet,draft,records,root/'figures')
        atomic_json(root / 'generated-records.json', records)
        atomic_json(root / 'assembly-report.json', report)
        print(json.dumps(report, ensure_ascii=False), flush=True)
    if args.command == 'debug-review':
        draft = load(args.draft or root / 'extraction/response.json')
        review(packet, load(root / 'generated-records.json'), draft, root / 'debug-review')
    if args.command == 'reproduce':
        draft = load(args.draft or root / 'extraction/response.json')
        outputs, timings = [], []
        for suffix in ('a', 'b'):
            started = time.monotonic()
            directory = root / ('rebuild-' + suffix)
            directory.mkdir(parents=True, exist_ok=True)
            previous_cache = os.environ.get('WAKG_FONT_CACHE_DIR')
            os.environ['WAKG_FONT_CACHE_DIR'] = tempfile.mkdtemp(prefix='font-cache-',dir=directory)
            try:
                fresh = prepare(packet['document']['path'], packet['supplements'])
                rebuilt, report = assemble(fresh, draft)
                if (root/'figures/integration.json').exists():
                    from paper_chart_bridge import integrate
                    def cached_chart_plan(image,pdf,page,caption,directory,context=None):
                        source_id=Path(directory).parent.name
                        return load(root/'figures'/source_id/'planner/binding.json')
                    report['figures']=integrate(fresh,draft,rebuilt,directory/'figures',planner=cached_chart_plan)
            finally:
                if previous_cache is None:
                    os.environ.pop('WAKG_FONT_CACHE_DIR', None)
                else:
                    os.environ['WAKG_FONT_CACHE_DIR'] = previous_cache
            atomic_json(directory / 'source-packet.json', fresh)
            atomic_json(directory / 'generated-records.json', rebuilt)
            comparable=copy.deepcopy(rebuilt)
            for asset in comparable.get('assets',[]):
                asset['relative_path']='sha256:'+asset['sha256']
            outputs.append(comparable)
            timings.append(round(time.monotonic() - started, 3))
        result = {'equal_scientific_records_and_evidence': outputs[0] == outputs[1],
                  'seconds': timings, 'inputs': 'source PDF/supplements plus extraction draft and chart bindings; asset locations compared by content SHA256',
                  'model_calls': 0}
        atomic_json(root / 'reproduction-report.json', result)
        print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
