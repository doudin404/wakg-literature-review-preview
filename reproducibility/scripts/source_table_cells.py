"""Shared native table cells and literal units for the planned consumers."""
import copy
import re
import unicodedata
from pathlib import Path

import fitz
from source_specimen_variants import digest
from pdf_symbol_text import repair_symbol_encoding


def source_unit(header, fallback):
    """Read explicit header units; chemical labels and particle sizes are not units."""
    text = unicodedata.normalize('NFKC', header)
    compact = re.sub(r'\s+', '', text)
    # Physical units in parentheses take precedence over solution concentrations
    # embedded in a material name, e.g. NaOH 10 M (kg/m3).
    units = {'kg/m3':'kg/m3', 'g/cm3':'g/cm3', 'g/L':'g/L', 'kg/L':'kg/L',
             'mol/L':'mol/L', 'M':'M', 'g':'g', 'kg':'kg', 'mg':'mg', 'mL':'mL', 'L':'L'}
    explicit = [units[x] for x in re.findall(r'\(([^()]*)\)|\[([^\[\]]*)\]', compact)
                for x in [''.join(x)] if x in units]
    if len(set(explicit)) == 1:
        return explicit[0]
    if re.search(r'(?:vol\.?|volume).*%|%.*(?:vol\.?|volume)', text, re.I):
        return 'vol.%'
    if re.search(r'(?:wt\.?|weight|mass)\s*%', text, re.I):
        return 'wt.%'
    if '%' in text:
        return '%'
    if re.search(r'\((?:vol\.?|volume)\)', text, re.I):
        return 'volume ratio'
    if re.search(r'\((?:wt\.?|weight|mass)\)|mass\s+ratio', text, re.I):
        return 'mass ratio'
    if re.search(r'molar\s+ratio', text, re.I):
        return 'molar ratio'
    if re.search(r'\bratio\b',text,re.I):
        return 'ratio'
    return fallback


def native_matrix(index, source, siblings, assets=()):
    if source.get('coordinate_space') == 'docx_table':
        raw = source['source_object']
        grid = raw.get('layout_grid', raw.get('grid', {}))
        rows = []
        expanded=grid.get('status')=='STRUCTURE_RESOLVED'
        for r, row in enumerate(grid['rows'] if expanded else raw['rows']):
            cells = []
            for c, value in enumerate(row):
                text = value.get('text', '') if isinstance(value, dict) else value or ''
                origin=value.get('origin') if expanded and value else None
                locator={'coordinate_space':'docx_table', 'row':origin['row'] if origin else r,
                         'column':origin['column'] if origin else c, 'table_number':raw['source_label']}
                if origin:
                    locator.update(cell=origin['cell'],display_row=r,display_column=c,
                                   inherited=bool(value['vertical_inheritance']))
                cells.append({'text': text, 'bbox': None,
                    'source_locator':locator})
            rows.append(cells)
        table = {'source_label':raw['source_label'], 'caption':raw.get('caption', ''),
                 'page':source.get('page'), 'bbox':None, 'rows':rows,
                 'coordinate_space':'docx_table', 'document_sha256':source['document_sha256']}
        if grid: table['layout_grid'] = copy.deepcopy(grid)
        table['table_sha256'] = digest(table)
        return table
    from multimodal_extraction_plan import _table_token_matrix
    path = (index['document']['path'] if source['document_sha256'] == index['document']['sha256']
            else next(a['relative_path'] for a in assets if a['sha256'] == source['document_sha256']))
    with fitz.open(Path(path)) as pdf:
        repair_symbol_encoding(pdf)
        return _table_token_matrix(pdf[source['page']-1], source['source_object'],
            [s['source_object'] for s in siblings if s['kind'] == 'table'
             and s['document_sha256'] == source['document_sha256']])


def exclusion_ids(cell_id, cells):
    if cell_id in cells:
        return [cell_id]
    # Expand an explicit same-row range returned by an earlier mapper.
    match = re.fullmatch(r'(.+:\d+:)(\d+)-(\d+)', cell_id)
    if match:
        first, last = map(int, match.group(2, 3))
        if first <= last and last - first < len(cells):
            ids = [match[1] + str(i) for i in range(first, last + 1)]
            if all(i in cells for i in ids): return ids
    raise ValueError('invalid cell exclusion: ' + cell_id)
