"""Select a recipe source by caption semantics and declared specimen identities."""
import re


def recipe_columns(table, specimen_ids, normalize):
    """Resolve source columns and header hierarchy without a material-name list."""
    grid = table.get('layout_grid') or {}
    if grid.get('status') != 'STRUCTURE_RESOLVED':
        raise ValueError('recipe grid unresolved')
    known = {normalize(value) for value in specimen_ids if value}
    start = next((i for i, row in enumerate(table['rows'])
                  if row and normalize(row[0]) in known), None)
    if start is None or start < 1:
        raise ValueError('recipe data boundary requires specimen context')
    header = grid['rows'][start - 1]
    columns = []
    for index, cell in enumerate(header[1:], 1):
        if not cell or not cell.get('text', '').strip():
            raise ValueError('recipe column identity unresolved')
        text = cell['text'].strip()
        if re.search(r'cost|emission|kgCO2|USD', text, re.I):
            continue
        hierarchy = []
        for row in grid['rows'][:start]:
            parent = row[index] if index < len(row) else None
            value = parent.get('text', '').strip() if parent else ''
            if value and (not hierarchy or hierarchy[-1] != value):
                hierarchy.append(value)
        context = ' '.join(hierarchy)
        incompatible = re.search(r'ratio|\btotal\b|\bsum\b|%|°|\bmpa\b|\bmol\b|\bw\s*/\s*b\b', context, re.I)
        source_unit = re.search(r'kg\s*/\s*m(?:3|³)', context, re.I)
        if not source_unit:
            source_unit = re.search(r'kg\s*/\s*m(?:3|³)', table.get('caption', ''), re.I)
        resolved = bool(source_unit) and not incompatible
        columns.append({'column': index, 'label': text, 'unit': 'kg/m3' if resolved else None,
                        'original_unit': source_unit.group() if resolved else None,
                        'semantic_status': 'MASS_QUANTITY' if resolved else 'COLUMN_SEMANTICS_UNRESOLVED',
                        'header_hierarchy': hierarchy, 'header_row': start - 1})
    if not columns:
        raise ValueError('no ingredient columns')
    if len({normalize(c['label']) for c in columns}) != len(columns):
        raise ValueError('ambiguous recipe column identities')
    return start, columns


def select_recipe_table(inventory, specimen_ids, normalize):
    known = {normalize(value) for value in specimen_ids if value}
    candidates = []
    for table in inventory.get('tables', []):
        if not re.search(r'(?:mix(?:ture)?\s+proportions?|mix\s+design|formulation|recipe)',
                         table.get('caption', ''), re.I):
            continue
        grid = table.get('layout_grid') or {}
        if grid.get('status') != 'STRUCTURE_RESOLVED':
            continue
        row_ids = {normalize(row[0]) for row in table.get('rows', []) if row and row[0]}
        if known & row_ids:
            candidates.append(table)
    if len(candidates) != 1:
        raise ValueError(f'recipe source ownership unresolved: {len(candidates)} matching tables')
    return candidates[0]
