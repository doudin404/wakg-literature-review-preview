"""Route declared source kinds to inventory objects without predefined figure IDs."""


def select_bound_figure(inventory, bindings, source_kind):
    declared = [(label, value) for label, value in bindings.items()
                if isinstance(value, dict) and value.get('source') == source_kind]
    if len(declared) != 1:
        raise ValueError('source-kind binding missing or ambiguous: ' + source_kind)
    label, binding = declared[0]
    matches = [f for f in inventory.get('figures', []) if f.get('label') == label]
    if len(matches) != 1:
        raise ValueError('bound figure missing or ambiguous')
    figure = matches[0]
    expected_hash = binding.get('image_sha256') or binding.get('source_sha256')
    if source_kind in {'psd_curve_image', 'xrd_curve_image'} and not expected_hash:
        raise ValueError('curve source binding requires image hash')
    if expected_hash and expected_hash != figure.get('sha256'):
        raise ValueError('bound figure content changed')
    if source_kind == 'native_chart_cache' and figure.get('content_kind') != 'chart':
        raise ValueError('native chart binding points to non-chart content')
    return figure, binding


def select_main_scope(assets, scope):
    from marker_formulations import caption_scope
    matches = []
    for asset in assets:
        if asset.get('kind') != 'main_figure':
            continue
        extensions = asset.get('extensions', {})
        try:
            inferred = caption_scope(extensions.get('caption', ''))
        except ValueError:
            continue
        if inferred == scope:
            matches.append(extensions)
    if len(matches) != 1:
        raise ValueError('main figure specimen scope missing or ambiguous')
    return {'label': matches[0]['figure_number'], 'caption': matches[0]['caption']}
