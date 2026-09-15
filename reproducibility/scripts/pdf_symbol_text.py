"""Restore known symbol outlines to Unicode in the in-memory reading document.

Some publisher fonts call a mu outline ``m`` and a greater-or-equal outline
``equal``, declare WinAnsiEncoding, and omit ToUnicode. Identify the actual
outline, not the paper, font name, nearby number, or decoded text. Original
PDF files and glyph drawings are unchanged.
"""
import hashlib
import io
import json

from fontTools.cffLib import CFFFontSet
from fontTools.pens.recordingPen import RecordingPen


# Independently checked against the displayed glyphs in the pilot PDF, pp. 2/7.
# Hashes identify glyph drawing commands, not PDF files or font subset names.
# The double-bar greater-or-equal glyph is normalized to the semantic >= sign.
KNOWN_OUTLINES = {
    'm': ('bd2aa016a2a7e454433b3c07042f727d34196257cc85f4ab3a28b9affb62ea7e',
          0x6D, '\u03bc'),
    'equal': ('30f3a0a7df01e9169c33b0283956c5f3b9bf6aa71cba97256e2b9aa6ec63c9c4',
              0x3D, '\u2265'),
}


def outline_unicode(font_bytes):
    """Return code -> Unicode for the recognized CFF glyphs only."""
    cff = CFFFontSet()
    cff.decompile(io.BytesIO(font_bytes), None)
    top = cff[cff.fontNames[0]]
    result = {}
    for name, (expected_outline, code, unicode_char) in KNOWN_OUTLINES.items():
        if name not in top.CharStrings.charStrings:
            continue
        pen = RecordingPen()
        top.CharStrings[name].draw(pen)
        drawing = json.dumps(pen.value, separators=(',', ':')).encode()
        if hashlib.sha256(drawing).hexdigest() == expected_outline:
            result[code] = unicode_char
    return result


def unicode_cmap(mapping):
    pairs = [f'<{code:02X}> <{char.encode("utf-16-be").hex().upper()}>'
             for code, char in sorted(mapping.items())]
    return ('/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n'
            '/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n'
            '/CMapName /WAKGSymbolUnicode def\n/CMapType 2 def\n'
            '1 begincodespacerange\n<00> <FF>\nendcodespacerange\n' +
            '\n'.join(f'{len(pairs[i:i+100])} beginbfchar\n' +
                      '\n'.join(pairs[i:i+100]) + '\nendbfchar'
                      for i in range(0, len(pairs), 100)) +
            '\nendcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n').encode()


def _repair_known_outlines(doc):
    """Add missing text mappings before text extraction/search; never save doc.

The same live document can then use get_text(), words, and search_for() with
consistent characters and the original page coordinates. Existing ToUnicode
maps and unrecognized glyphs retain their existing behavior.
    """
    fonts = {f[0]: f for page in doc for f in page.get_fonts(full=True)}
    repairs = []
    for xref, font in fonts.items():
        if font[1:3] != ('cff', 'Type1') or font[5] != 'WinAnsiEncoding':
            continue
        if doc.xref_get_key(xref, 'ToUnicode')[0] != 'null':
            continue
        corrections = outline_unicode(doc.extract_font(xref)[3])
        if not corrections:
            continue
        first = int(doc.xref_get_key(xref, 'FirstChar')[1])
        last = int(doc.xref_get_key(xref, 'LastChar')[1])
        # Preserve the other WinAnsi characters of a multi-character font.
        mapping = {}
        for code in range(first, last + 1):
            char = bytes([code]).decode('cp1252', errors='ignore')
            if char:
                mapping[code] = char
        mapping.update(corrections)
        cmap_xref = doc.get_new_xref()
        doc.update_object(cmap_xref, '<<>>')
        doc.update_stream(cmap_xref, unicode_cmap(mapping))
        doc.xref_set_key(xref, 'ToUnicode', f'{cmap_xref} 0 R')
        repairs.append({'font': font[3], 'font_xref': xref,
                        'mapping': {f'{code:02X}': char for code, char in corrections.items()}})
    return repairs


def repair_symbol_encoding(doc, cache_path=None, annotation_dir=None):
    """Repair missing or suspicious Unicode mappings using cached short-line OCR.

    Original drawing programs and PDF bytes remain untouched. Context-dependent
    residuals are interpreted during the whole-paper semantic extraction call.
    """
    from font_annotation import inventory, cached_corrections, native_baseline
    from local_ocr_font_mapping import labels_for_fonts, cache_root, VERSION
    from pathlib import Path
    if getattr(doc, '_wakg_font_read_complete', False):
        return []
    items = inventory(doc)
    labels, unsupported = labels_for_fonts(doc, items, cache_path)
    repairs = []
    for item in items:
        mapping = cached_corrections(doc, item, labels)
        if not mapping:
            continue
        xref = item['xref']
        previous_type, previous = doc.xref_get_key(xref, 'ToUnicode')
        if previous_type == 'null':
            baseline = native_baseline(doc, item)
            baseline.update(mapping)
        else:
            baseline = mapping
        target = doc.get_new_xref()
        doc.update_object(target, '<<>>')
        doc.update_stream(target, unicode_cmap(baseline))
        if previous_type == 'xref':
            doc.xref_set_key(target, 'UseCMap', previous)
        doc.xref_set_key(xref, 'ToUnicode', f'{target} 0 R')
        repairs.append({'font': item['font'], 'font_xref': xref,
                        'mapping': {f'{c:02X}': t for c, t in mapping.items()}})
    # A document-scoped path keeps the same request/sheets reusable on restart.
    document_key = hashlib.sha256(''.join(i['font_sha256'] for i in items).encode()).hexdigest()[:20]
    directory = Path(annotation_dir) if annotation_dir else cache_root() / 'documents' / document_key
    unresolved=sum(bool(labels.get(i['font_sha256']+':'+str(gid),{}).get('context_sensitive'))
                   for i in items for gid in {o['gid'] for o in i['occurrences']})
    doc.font_annotation = {'directory': str(directory), 'pending_glyphs': unresolved,
                           'method': VERSION, 'unsupported_fonts': unsupported,
                           'semantic_cleanup': 'Interpret near-shape character substitutions in context during paper extraction.',
                           'fonts': [{'font': i['font'], 'reason': i['reason']} for i in items]}
    doc._wakg_font_read_complete = True
    return repairs
