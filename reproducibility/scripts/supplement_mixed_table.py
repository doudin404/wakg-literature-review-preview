"""Column-level candidates for mixed formulation/economic tables."""
import re
import hashlib
import json
import copy
from pathlib import Path
import fitz


def bind_review(candidate, review, pdf_path, docx_sha256, table_caption):
    if not isinstance(review.get('reviewer'), str) or not review['reviewer'].strip():
        raise ValueError('reference scope reviewer missing')
    if (review.get('rows_sha256') != candidate['source_rows_sha256']
            or review.get('table_label') != candidate['table_label']
            or review.get('docx_sha256') != docx_sha256
            or hashlib.sha256(pdf_path.read_bytes()).hexdigest() != review.get('pdf_sha256')):
        raise ValueError('reference scope review source mismatch')
    if review.get('decision') != 'ANALYTICAL_REFERENCE_NOT_EXPERIMENTAL_MIX' or review.get('existing_mat_links_permitted') is not False:
        raise ValueError('unsupported reference scope review')
    support=review.get('source_support',[])
    if {s.get('source') for s in support}!={'pdf','docx'}:
        raise ValueError('reference scope requires paragraph and caption')
    with fitz.open(pdf_path) as doc:
        for source in support:
            if not isinstance(source.get('text'), str) or not source['text'].strip():
                raise ValueError('reference scope source quotation empty')
            if source['source'] == 'pdf' and (type(source.get('page')) is not int or not 1 <= source['page'] <= len(doc)):
                raise ValueError('reference scope source page invalid')
            text=table_caption if source['source']=='docx' else doc[source['page']-1].get_text()
            if source['text'] not in re.sub(r'\s+',' ',text):
                raise ValueError('reference scope source quotation mismatch')
    return {**candidate,'semantic_scope':'ANALYTICAL_REFERENCE_NOT_EXPERIMENTAL_MIX',
            'source_docx_sha256':docx_sha256,
            'source_scope_review':copy.deepcopy(review),
            'scope_review_sha256':hashlib.sha256(json.dumps(review,sort_keys=True).encode()).hexdigest(),
            'existing_mat_links_permitted':False,
            'status':'SOURCE_VERIFIED_ANALYTICAL_REFERENCE_PRESERVED'}


def verify_preserved_reference(candidate, assets):
    """Reopen frozen sources and recompute the retained analytical reference."""
    from supplement_docx import inspect_docx
    review = candidate.get('source_scope_review') or {}
    sources = {}
    for kind, key in (('pdf', 'pdf_sha256'), ('supplement', 'docx_sha256')):
        matches = [a for a in assets if a.get('kind') == kind and a.get('sha256') == review.get(key)]
        if len(matches) != 1:
            raise ValueError('reference preservation source missing or ambiguous')
        path = Path(matches[0]['relative_path'])
        if hashlib.sha256(path.read_bytes()).hexdigest() != review[key]:
            raise ValueError('reference preservation source hash mismatch')
        sources[kind] = path
    inventory = inspect_docx(sources['supplement'])
    matches = [t for t in inventory['tables'] if t['rows_sha256'] == candidate.get('source_rows_sha256')]
    if len(matches) != 1:
        raise ValueError('reference preservation table content missing or ambiguous')
    table = matches[0]
    rebuilt = bind_review(candidates(table), review, sources['pdf'], review['docx_sha256'], table['caption'])
    if rebuilt != candidate:
        raise ValueError('reference preservation differs from original source')
    return True


def candidates(table):
    rows=table['rows']
    if not rows: raise ValueError('empty table')
    columns=[]
    for index, header in enumerate(rows[0]):
        normalized=' '.join(header.split())
        mass=re.fullmatch(r'(.+?)\s*\(kg\s*/\s*m(?:3|³)\)',normalized,re.I)
        economic=bool(re.search(r'cost|emission|kgCO2|USD',normalized,re.I))
        columns.append({'column':index,'header':header,
                        'role':'ECONOMIC' if economic else 'INGREDIENT_MASS' if mass else 'UNRESOLVED',
                        'original_unit':re.search(r'kg\s*/\s*m(?:3|³)',header,re.I).group() if mass and not economic else None,
                        'material_label':mass.group(1).strip() if mass and not economic else None})
    values=[]
    for row_index,row in enumerate(rows[1:],1):
        if len(row)!=len(columns): raise ValueError('table column alignment mismatch')
        for col in columns:
            raw=row[col['column']]
            if col['role']!='INGREDIENT_MASS': continue
            missing=raw.strip() in {'','/','-','–','—'}
            try: value=None if missing else float(raw)
            except ValueError: raise ValueError('non-numeric ingredient mass requires semantic interpretation')
            if value is not None and (not __import__('math').isfinite(value) or value<0):
                raise ValueError('invalid ingredient mass')
            values.append({'material_label':col['material_label'],'original_value':raw,
                           'value':value,'unit':'kg/m3','row':row_index,'column':col['column'],
                           'original_unit':col['original_unit'], 'source_header':col['header'],
                           'missing_reason':'SOURCE_MISSING_SYMBOL' if missing else None})
    return {'table_label':table['label'],'source_rows_sha256':table['rows_sha256'],
            'columns':columns,'values':values,
            'source_rows':copy.deepcopy(rows), 'source_caption':table.get('caption'),
            'status':'CANDIDATE_REQUIRES_MATERIAL_AND_REFERENCE_SCOPE_REVIEW',
            'formal_publication_authorized':False}
