"""Read paper-owned source inventories, retaining explicit legacy read support."""


def source_entries(records, name):
    if name not in {'scientific_assets', 'scientific_tables'}:
        raise ValueError('unknown source inventory')
    current = [entry for paper in records.get('papers', [])
               for entry in paper.get('extensions', {}).get(name, [])]
    legacy = records.get('extensions', {}).get(name, [])
    if current and legacy:
        raise ValueError('mixed paper-owned and legacy source inventories require fresh reconstruction')
    return current if current else legacy
