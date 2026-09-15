"""Locate explicit property/specimen intersections in row- or column-oriented tables."""
import re
import unicodedata


def property_identity(value):
    text=unicodedata.normalize('NFKC',str(value)).casefold()
    text=re.sub(r'\s+',' ',text).strip()
    # Do not collapse total/closed/apparent porosity into open porosity.
    if re.fullmatch(r'(?:open porosity|开口孔隙率|开放孔隙率)\s*\(\s*%\s*\)',text):
        return ('open_porosity','%')
    return None


def locate_cells(table, specimen, property_name, unit):
    rows=table.get('rows',[])
    hits=[]
    for r,row in enumerate(rows):
        for c,label in enumerate(row):
            if property_identity(label)!=(property_name,unit):continue
            # Property in the left header column; specimen headings above it.
            if c==0:
                for header_row in rows[:r]:
                    for column,name in enumerate(header_row[1:],1):
                        if name==specimen and column<len(row):
                            hits.append({'row':r,'column':column,'raw':row[column]})
            # Property in a header row; specimen identifiers in the left column.
            if c>0:
                for data_row in range(r+1,len(rows)):
                    values=rows[data_row]
                    if values and values[0]==specimen and c<len(values):
                        hits.append({'row':data_row,'column':c,'raw':values[c]})
    return hits


def locate_cell(table,specimen,property_name,unit):
    hits=locate_cells(table,specimen,property_name,unit)
    if len(hits)!=1:raise ValueError('property source cell missing or ambiguous')
    return hits[0]
