"""Strict source-cell parsing shared by supplement XRF producers."""
from decimal import Decimal, InvalidOperation
import re

OXIDES = {'SiO2', 'Al2O3', 'CaO', 'Fe2O3', 'K2O', 'Na2O', 'MgO',
          'TiO2', 'SO3', 'P2O5', 'MnO', 'BaO', 'SrO', 'ZrO2', 'Cr2O3'}


def component_headers(raw_headers):
    headers = [re.sub(r'\s+', '', h).translate(str.maketrans('₀₁₂₃₄₅₆₇₈₉', '0123456789'))
               for h in raw_headers]
    headers = ['LOI' if re.fullmatch(r'LOI[a-z*†‡]',h) else h for h in headers]
    if len(headers) != len(set(headers)):
        raise ValueError('duplicate normalized XRF component')
    if any(h not in OXIDES | {'LOI','Other','Others'} and
           not ('+' in h and all(x in OXIDES for x in h.split('+'))) for h in headers):
        raise ValueError('XRF column semantics require review; not every numeric column is a component')
    return headers


def select_composition_tables(inventory):
    """Select a material-row oxide table by content, not its table number.

    Return all explicit composition tables; ownership is checked by the producer.
    """
    matches = []
    for table in inventory.get('tables', []):
        rows = table.get('rows', [])
        if len(rows) < 2 or len(rows[0]) < 3:
            continue
        headers = [re.sub(r'\s+', '', h).translate(str.maketrans('₀₁₂₃₄₅₆₇₈₉', '0123456789'))
                   for h in rows[0][1:]]
        caption = table.get('caption', '').lower()
        if sum(h in OXIDES for h in headers) < 2:
            continue
        if not re.search(r'xrf|chemical|oxide|composition', caption):
            continue
        if any(len(row) != len(rows[0]) or not row[0].strip() for row in rows[1:]):
            raise ValueError('composition table requires material-row alignment review')
        matches.append(table)
    if not matches:
        raise ValueError(f'composition table identity unresolved: {len(matches)} candidates')
    for table in matches:
        component_headers(table['rows'][0][1:])
        if not re.search(r'(?:wt\.?\s*%|weight\s*%|mass\s*%|mass\s+percent)',
                         table.get('caption', ''), re.I):
            raise ValueError('composition mass-percent basis requires source review')
    return matches


def select_composition_table(inventory):
    matches = select_composition_tables(inventory)
    if len(matches) != 1:
        raise ValueError(f'composition table identity unresolved: {len(matches)} candidates')
    return matches[0]


def parse_row(headers, cells):
    if len(headers)!=len(cells) or len(set(headers))!=len(headers):
        raise ValueError('XRF header/cell alignment mismatch')
    values=[]
    for raw in cells:
        if raw.strip() in {'','/','-','–','—'}:
            values.append(None);continue
        try: value=Decimal(raw)
        except InvalidOperation: raise ValueError('invalid XRF cell')
        if not value.is_finite() or value<0: raise ValueError('invalid XRF cell')
        values.append(value)
    return [None if v is None else float(v) for v in values],float(sum((v for v in values if v is not None),Decimal(0)))


def existing_composition_matches(existing, headers, values):
    """Require explicit reconciliation rather than overwrite another source."""
    if not existing or not existing.get('rows'):
        return False
    rows = existing['rows']
    old = {r['component']: r.get('original_value') for r in rows}
    expected = dict(zip(headers, values))
    if (existing.get('unit') != 'wt.%' or len(old) != len(rows) or old != expected
            or any(r.get('normalized_value') != r.get('original_value') for r in rows)
            or existing.get('normalisation_candidate') is not None):
        raise ValueError('existing XRF composition requires source reconciliation')
    return True
