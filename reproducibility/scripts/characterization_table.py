"""Identify a transposed property table by measurements and specimen columns."""
LABELS = {
    'Initially added water (kg/m3)': ('initially_added_water', 'kg/m3'),
    'Over-dried density (kg/m3)': ('oven_dried_density', 'kg/m3'),
    'Mass loss (%)': ('mass_loss', '%'),
    'Chemically bound water* (kg/m3)': ('chemically_bound_water', 'kg/m3'),
    'Unreacted water** (kg/m3)': ('unreacted_water', 'kg/m3'),
    'Unreacted water volume (%)': ('unreacted_water_volume', '%'),
    'Open porosity (%)': ('open_porosity', '%'),
}


def select_table(inventory, specimen_ids, normalize):
    known = {normalize(value) for value in specimen_ids if value}
    matches = []
    for table in inventory.get('tables', []):
        rows = table.get('rows', [])
        if len(rows) < 2 or len(rows[0]) < 2:
            continue
        identities = {normalize(value) for value in rows[0][1:] if value}
        properties = {row[0] for row in rows[1:] if row}
        if identities & known and properties & LABELS.keys():
            matches.append(table)
    if len(matches) != 1:
        raise ValueError('characterization table missing or ambiguous')
    return matches[0]
