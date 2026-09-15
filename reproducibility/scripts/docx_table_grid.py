"""OOXML layout grid with explicit merge origins, never inferred blank filling."""
W='http://schemas.openxmlformats.org/wordprocessingml/2006/main'
NS={'w':W}


def layout(table, text_of):
    rows, issues, active = [], [], {}
    for ri, row in enumerate(table.findall('./w:tr',NS)):
        before=row.find('./w:trPr/w:gridBefore',NS)
        column=int(before.get('{'+W+'}val','0')) if before is not None else 0
        cells=[None]*column
        next_active={}
        for ci, cell in enumerate(row.findall('./w:tc',NS)):
            span_node=cell.find('./w:tcPr/w:gridSpan',NS)
            span=int(span_node.get('{'+W+'}val','1')) if span_node is not None else 1
            if span<1: raise ValueError('invalid DOCX gridSpan')
            merge=cell.find('./w:tcPr/w:vMerge',NS)
            mode=merge.get('{'+W+'}val','continue') if merge is not None else None
            raw=text_of(cell)
            origin={'row':ri,'cell':ci,'column':column,'span':span,'text':raw}
            inherited=False
            if mode=='continue':
                prior=[active.get(c) for c in range(column,column+span)]
                valid=all(p is not None and p==prior[0] for p in prior)
                valid=valid and prior[0]['column']==column and prior[0]['span']==span
                if not valid or (raw.strip() and raw!=prior[0]['text']):
                    issues.append({'row':ri,'cell':ci,'code':'INVALID_VERTICAL_MERGE'})
                    origin=None
                else:
                    origin=prior[0]; inherited=True
            elif mode not in (None,'restart'):
                issues.append({'row':ri,'cell':ci,'code':'UNKNOWN_VERTICAL_MERGE_MODE'})
                origin=None
            for c in range(column,column+span):
                cells.append({'column':c,'raw_cell':ci,'raw_text':raw,
                    'text':origin['text'] if origin else None,
                    'origin':dict(origin) if origin else None,
                    'horizontal_span':span,'vertical_inheritance':inherited})
                if merge is not None and origin is not None: next_active[c]=origin
            column+=span
        after=row.find('./w:trPr/w:gridAfter',NS)
        if after is not None: cells.extend([None]*int(after.get('{'+W+'}val','0')))
        rows.append(cells); active=next_active
    declared=len(table.findall('./w:tblGrid/w:gridCol',NS))
    width=declared or max((len(r) for r in rows),default=0)
    for ri,row in enumerate(rows):
        if len(row)!=width:
            issues.append({'row':ri,'code':'GRID_WIDTH_MISMATCH','actual':len(row),'expected':width})
    return {'schema_version':'docx-layout-grid-v1','rows':rows,'columns':width,
            'issues':issues,'status':'STRUCTURE_RESOLVED' if not issues else 'STRUCTURE_QUARANTINED',
            'scientific_semantics_verified':False}
