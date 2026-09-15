"""Bind marker semantics to the actual PDF caption, not cached asset metadata.

This conservative parser supports leading lettered panels. Unsupported captions
remain candidates; they must not inherit an adjacent panel's age or specimen.
"""
import re


def normalize(text):
    return re.sub(r'\s+', ' ', text.replace('\u00ad', '')).strip()


def bind_caption(page, caption, chart):
    words = page.get_text('words')
    parts = [normalize(w[4]) for w in words]
    text = ' '.join(parts)
    caption = normalize(caption)
    starts = [m.start() for m in re.finditer(re.escape(caption), text)]
    if len(starts) != 1:
        raise ValueError('main marker caption missing or ambiguous in source PDF')
    offset = starts[0]
    offsets = []; cursor = 0
    for part in parts:
        offsets.append((cursor, cursor + len(part))); cursor += len(part) + 1
    boundaries = list(re.finditer(r'\(([a-z])\)\s*', caption))
    labels = [m[1] for m in boundaries]
    if not labels or len(labels) != len(set(labels)):
        raise ValueError('main marker caption panel labels missing or duplicate')
    fragments = {}
    for index, match in enumerate(boundaries):
        end = boundaries[index+1].start() if index+1 < len(boundaries) else len(caption)
        fragments[match[1]] = (match.end(), end, caption[match.end():end])
    contexts = []
    for panel in chart['panels']:
        if panel['label'] not in fragments:
            raise ValueError('main marker panel absent from source caption')
        start, end, fragment = fragments[panel['label']]
        specimens = set(re.findall(r'\b(mortar|paste|concrete)\b', fragment, re.I))
        ages = re.findall(r'\b(\d+(?:\.\d+)?)-day\b', fragment, re.I)
        if len(specimens) != 1 or len(ages) > 1:
            raise ValueError('main marker panel specimen or age is ambiguous')
        specimen = next(iter(specimens)).lower()
        age = float(ages[0])*86400 if ages else None
        for series in panel['series']:
            if series['age_seconds'] != age:
                raise ValueError('main marker age differs from panel-local source caption')
            name = series['name']
            if name in ('compressive_strength', 'flexural_strength'):
                supported = bool(re.search(r'\bstrength\b', fragment, re.I))
            else:
                supported = bool(re.search(r'\b'+re.escape(name.replace('_', ' '))+r'\b', fragment, re.I))
            if not supported:
                raise ValueError('main marker property differs from panel-local source caption')
        locators = [{'bbox':list(w[:4]), 'token':p} for w,p,(a,b) in zip(words,parts,offsets)
                    if a < offset+end and b > offset+start]
        contexts.append({'panel':panel['label'], 'specimen':specimen,
                         'age_seconds':age, 'caption_fragment':fragment.strip(),
                         'caption_locators':locators,
                         'scope':'panel_context_only_not_mix_ownership'})
    return contexts
