"""Neutral MIX construction: no scientific context inherited from another row."""


def empty_source_mix(paper_key, mix_key, identity, table, row):
    return {
        'schema_version': '1.0', 'mix_key': mix_key, 'paper_key': paper_key, 'mix_family_key': None,
        'modules': {
            'identity_source_specimen': {'custom_test_id': identity,
                'literature_source': {'title': None, 'doi': None, 'year': None},
                'specimen_type': None, 'specimens': [], 'extensions': {}},
            'materials': {'mat_refs': [], 'solid_materials': [], 'activator_total_mass_g': None,
                'activators': [], 'fine_aggregate': None, 'coarse_aggregate': None,
                'reported_mass_basis': table, 'platform_ratios': None, 'conversion': None,
                'review_status': 'pending', 'extensions': {'reported_parameters': []}},
            'mixing_curing': {'mixing': None, 'forming': None, 'demoulding': None,
                'curing_stages': [], 'curing_route': None, 'age_origin': None, 'extensions': {}},
            'performance': [], 'characterizations': []},
        'field_provenance': {}, 'extensions': {'source_table': table, 'source_row': row}}
