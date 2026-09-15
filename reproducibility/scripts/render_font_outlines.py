"""Render a font program's actual contours, independent of PDF text bboxes."""
import io
import fitz
from fontTools.cffLib import CFFFontSet
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.boundsPen import BoundsPen


def glyph_pixmap(font_bytes, gid):
    cff = CFFFontSet()
    cff.decompile(io.BytesIO(font_bytes), None)
    top = cff[cff.fontNames[0]]
    glyph = top.CharStrings[top.charset[gid]]
    bounds = BoundsPen(None)
    glyph.draw(bounds)
    if bounds.bounds is None:
        return None
    x0,y0,x1,y1 = bounds.bounds
    pen = SVGPathPen(None)
    glyph.draw(pen)
    margin = 40
    width,height=max(1,x1-x0)+2*margin,max(1,y1-y0)+2*margin
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">'
           f'<path transform="translate({margin-x0},{margin+y1}) scale(1,-1)" '
           f'd="{pen.getCommands()}"/></svg>').encode()
    with fitz.open(stream=svg, filetype='svg') as source:
        with fitz.open(stream=source.convert_to_pdf(), filetype='pdf') as converted:
            return converted[0].get_pixmap(matrix=fitz.Matrix(100/max(width,height),100/max(width,height)),alpha=False)
