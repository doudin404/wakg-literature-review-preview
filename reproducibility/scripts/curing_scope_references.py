"""Attach the stated antecedent for explicitly repeated preparation procedures."""
import copy
import re


def repair(request,response):
    result=copy.deepcopy(response);spans={s['span_id']:s for s in request['spans']}
    numbers={n['token_id']:n for n in request['tokens']};changes=[]
    for stage in result['stages']:
        if stage['kind']!='PREPARATION':continue
        for prop in stage['properties']:
            token=numbers.get(prop['numeric_token_id'])
            if not token or prop['name']!='duration':continue
            selected=[spans[s] for s in prop['source_span_ids']]
            if any(token['locator'] in s['locators'] or token['block_id']==s['block_id'] for s in selected):continue
            text=' '.join(s['quote'] for s in selected)
            if not re.search(r'\b(?:following|using|according to)\s+(?:the\s+)?same\s+(?:preparation\s+)?procedure\b',text,re.I):continue
            if re.search(r'\b(?:except|instead|different|shorter|longer)\b',text,re.I):continue
            antecedents=[]
            for prior in response['stages']:
                if prior['stage_id']==stage['stage_id'] or prior['kind']!=stage['kind'] or prior['operation']!=stage['operation']:continue
                for p in prior['properties']:
                    if (p['name'],p['numeric_token_id'],p['unit'])!=(prop['name'],prop['numeric_token_id'],prop['unit']):continue
                    antecedents.extend(sid for sid in p['source_span_ids'] if token['locator'] in spans[sid]['locators'])
            choices={spans[sid]['block_id'] for sid in antecedents}
            if len(choices)!=1:continue
            # The repeated procedure refers backwards to the same or previous
            # source page; other recipes elsewhere in the paper do not qualify.
            locations=[w for s in selected for w in s['locators']]
            origin=token['locator']
            if not locations or any(w.get('asset_key')!=origin.get('asset_key') for w in locations):continue
            if origin.get('page') is None:
                start=origin.get('source_locator',{}).get('body_index')
                if start is None or not all(0<=w.get('source_locator',{}).get('body_index',-1)-start<=3 for w in locations):continue
            else:
                if not all(w.get('page') is not None and 0<=w['page']-origin['page']<=1 for w in locations):continue
                if any(w['page']==origin['page'] and w['bbox'][1]<origin['bbox'][1] for w in locations):continue
            support=min(set(antecedents),key=lambda sid:len(spans[sid]['locators']))
            prop['source_span_ids'].append(support)
            changes.append({'stage_id':stage['stage_id'],'property':prop['name'],
                            'numeric_token_id':prop['numeric_token_id'],'antecedent_span_id':support,
                            'method':'EXPLICIT_REPEATED_PROCEDURE'})
    return result,changes
