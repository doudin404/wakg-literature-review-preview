"""Map explicit source quantities into WAKG material modules without mass guesses.

The exchange template leaves component row layout open. Rows retain the
reported value/unit without converting a dosage concentration to batch mass.
Source parameters remain lossless until the reviewer projection is migrated.
"""
from __future__ import annotations

import copy
import hashlib
import math
import re
import unicodedata
from typing import Any

VERSION = "canonical-material-quantities-v2-context-routes"
ALIASES = {"flyash": "fa", "classfflyash": "fa", "ggbfs": "ggbs",
           "groundgranulatedblastfurnaceslag": "ggbs"}
MASS_UNITS = {"g", "kg", "kg/m3", "wt.%", "wt%", "%", "vol.%"}
# Value-free paper-specific semantic routing, independently reviewed against
# materials/methods. Abbreviations such as SS are deliberately not global.
ROUTES = {
    "1c116a8946fc": {"activators": ["Na2O-2SiO2", "Na2O-SiO2", "Na2CO3", "Na2SO4", "Borax"], "fine_aggregate": ["QS-1", "QS-2"], "platform_ratios": ["fixed:water-to-binder", "fixed:sand-to-binder", "fixed:total alkali", "fixed:precursor mass ratio", "fixed:aggregate blend mass ratio"]},
    "4df39133d239": {"activators": ["SH", "SS"], "fine_aggregate": ["Sand"], "coarse_aggregate": ["CA 10 mm", "CA 20 mm"], "platform_ratios": ["AL/B", "FA/GGBS", "SS/SH"]},
    "55c422590f17": {"platform_ratios": ["h2o_to_x2o", "x2o_to_sio2"]},
    "66c1278f6f7c": {"activators": ["NaOH", "Na2SiO3"], "platform_ratios": ["Ms", "w/b", "Na2SiO3_silicate_share"]},
    "7717e4cb8387": {"platform_ratios": ["W/B", "Na2O", "GGBS replacement ratio"]},
    "7c7c47d33379": {"activators": ["naoh", "sodium_silicate_solution"], "platform_ratios": ["sand_to_precursor_ratio", "silicate_solution_to_naoh_solution_ratio", "na2o_to_precursor_percent", "Ms", "activator_solution_to_precursor_ratio", "water_to_solid_ratio"]},
    "7f610942ec2f": {"activators": ["Commercial SS", "RHA-derived SS filtered", "RHA-derived SS unfiltered", "NaOH 10 M"], "platform_ratios": ["Liquid/binder", "Na2O/binder", "SiO2/Na2O", "w/s"]},
    "993d666cc40b": {},
    "b7acfcbaf503": {"platform_ratios": ["slag_fraction", "fly_ash_fraction", "fgr_to_precursor", "activator_solution_to_precursor", "na2o_to_precursor_percent"]},
    "f15f24af118a": {"activators": ["NaOH", "Sodium silicate solution"], "fine_aggregate": ["Sand"], "platform_ratios": ["Ms", "Na2O content", "w/b"]},
}


def normalized(value: Any) -> str:
    key = re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKC", str(value or "")).casefold())
    return ALIASES.get(key, key)


def project_explicit_sources(records):
    """Project native source roles without ROUTES, name-to-MAT guesses or arithmetic."""
    from source_specimen_variants import digest
    result=copy.deepcopy(records);mats={m['mat_key']:m for m in result['mats']}
    links={e['evidence_key']:e for e in result['evidence_links']}
    report={'mapped':[],'deferred':[],'conflicts':[],'formal_acceptance':False}
    def label(text,unit):
        return ''.join(str(text or '').replace('('+str(unit)+')','').split()).casefold()
    def basis_signature(text):
        # Literal division phrases only; preserve operand order and all words.
        parts=re.split(r'\s*(?:/|\bdivided\s+by\b|\brelative\s+to\b)\s*',str(text).casefold())
        return tuple(' '.join(p.split()) for p in parts)
    def same_cell(a,b):
        return bool(a and b and a.get('asset_key')==b.get('asset_key') and a.get('page')==b.get('page')
            and a.get('bbox') and b.get('bbox') and all(abs(x-y)<.02 for x,y in zip(a['bbox'],b['bbox'])))
    for mix in result['mixes']:
        material=mix['modules']['materials']
        for i,p in enumerate(material.get('extensions',{}).get('reported_parameters',[])):
            source_path=f'/modules/materials/extensions/reported_parameters/{i}/value'
            ev=links.get(p.get('evidence_key'));prov=mix.get('field_provenance',{}).get(source_path,{})
            if not ev or not ev.get('extensions',{}).get('semantic_request_sha256'):continue
            claim={'record_key':mix['mix_key'],'source_field_path':source_path,'source_evidence_key':ev['evidence_key']}
            if ev['record_key']!=mix['mix_key'] or ev['field_path']!=source_path or prov.get('evidence_key')!=ev['evidence_key']:
                report['deferred'].append({**claim,'reason':'parameter evidence needs association'});continue
            role=p.get('semantic_role');unit=p.get('unit');mat=p.get('mat_key');basis=p.get('mass_basis')
            if p.get('original_unit')!=unit or prov.get('original_unit')!=unit:
                report['deferred'].append({**claim,'reason':'source unit conversion needs association'});continue
            if not basis or p.get('value') is None:
                report['deferred'].append({**claim,'reason':'source basis or numeric value unavailable'});continue
            try:direct=type(p['value']) in (int,float) and math.isfinite(p['value']) and float(p['original_value'])==p['value']
            except (TypeError,ValueError):direct=False
            if not direct:
                report['deferred'].append({**claim,'reason':'Original composite or converted value retained in source parameters'});continue
            if mat is not None and (mat not in mats or mats[mat]['paper_key']!=mix['paper_key']):
                report['deferred'].append({**claim,'reason':'material identity needs association'});continue
            if role=='solid_fraction' and mat is not None and unit in ('wt.%','%','g','kg','kg/m3','vol.%'):
                destination='solid_materials';target_mat=mat
            elif role in ('replacement_fraction','reported_ratio','reported_dosage') and unit in ('%','ratio','molar ratio','mass ratio','volume ratio'):
                destination='platform_ratios';target_mat=None
            else:
                report['deferred'].append({**claim,'reason':'source role/unit has no supported destination'});continue
            rows=material.get(destination) or []
            candidates=[]
            for j,old in enumerate(rows):
                if old.get('mat_key')!=target_mat:continue
                old_ev=links.get(old.get('evidence_key'))
                if same_cell(old_ev,ev) or label(old.get('name'),unit)==label(p.get('original_label'),unit):candidates.append(j)
            if len(candidates)>1:
                report['conflicts'].append({**claim,'reason':'multiple existing source-identity matches'});continue
            if not candidates:
                possible=[j for j,r in enumerate(rows) if r.get('mat_key')==target_mat
                    and r.get('value')==p['value'] and r.get('unit')==unit]
                if possible:
                    report['deferred'].append({**claim,'reason':'possible legacy alias; numeric equality is not semantic identity',
                        'candidate_paths':[f'/modules/materials/{destination}/{j}/value' for j in possible]});continue
            index=candidates[0] if candidates else len(rows);old=rows[index] if candidates else None
            if old is not None:
                old_basis=old.get('extensions',{}).get('mass_basis')
                if old['value']!=p['value'] or old.get('unit') not in (None,unit):
                    report['conflicts'].append({**claim,'reason':'existing scientific value/unit conflicts with source',
                        'existing_path':f'/modules/materials/{destination}/{index}/value'});continue
                if old_basis and old_basis!='NOT_APPLICABLE' and basis_signature(old_basis)!=basis_signature(basis):
                    report['deferred'].append({**claim,'reason':'legacy and source basis equivalence not established',
                        'existing_basis':old_basis,'source_basis':basis});continue
            path=f'/modules/materials/{destination}/{index}/value'
            new_key='ev-source-material-'+digest([mix['mix_key'],path,ev['evidence_key']])[:24]
            if old is not None and old.get('evidence_key')==new_key:
                report['mapped'].append({**claim,'field_path':path,'evidence_key':new_key,'operation':'reused'});continue
            if old is not None:
                history=mix.setdefault('extensions',{}).setdefault('canonical_material_revision_history',[])
                history_path=f'/extensions/canonical_material_revision_history/{len(history)}/row/value'
                history.append({'destination':destination,'index':index,'row':copy.deepcopy(old),
                    'source_parameter_evidence_key':ev['evidence_key']})
                if path in mix['field_provenance']:mix['field_provenance'][history_path]=mix['field_provenance'].pop(path)
                old_ev=links.get(old.get('evidence_key'))
                if old_ev and old_ev.get('field_path')==path:old_ev['field_path']=history_path
            mapped={**copy.deepcopy(ev),'evidence_key':new_key,'field_path':path}
            mapped.setdefault('extensions',{})['source_parameter_path']=source_path
            result['evidence_links'].append(mapped);links[new_key]=mapped
            row={'name':p['original_label'],'mat_key':target_mat,'value':p['value'],'unit':unit,
                'transformation':None,'evidence_key':new_key,'extensions':{
                    'source_parameter_key':p['parameter_key'],'source_parameter_path':source_path,
                    'source_material_key':mat,'mass_basis':basis,'semantic_role':role,
                    'mapping_version':'explicit-source-material-projection-v1','status':'pending'}}
            if material.get(destination) is None:material[destination]=rows
            if old is None:rows.append(row)
            else:rows[index]=row
            # For an existing empty list, `or []` created a new local container.
            material[destination]=rows
            mix['field_provenance'][path]={**copy.deepcopy(prov),'evidence_key':new_key,
                'extraction_method':'explicit-source-material-projection-v1','review_status':'pending'}
            if mat is not None and mat not in material.setdefault('mat_refs',[]):material['mat_refs'].append(mat)
            material['review_status']='pending'
            report['mapped'].append({**claim,'field_path':path,'evidence_key':new_key,
                'operation':'added' if old is None else 'rebound'})
    report['input_records_sha256']=digest(records);report['output_records_sha256']=digest(result)
    return result,report


def enrich(records: dict[str, Any]) -> dict[str, int]:
    mat_names: dict[str, list[str]] = {}
    for mat in records.get("mats", []):
        name = normalized(mat.get("custom_material_id"))
        mat_names.setdefault(name, []).append(mat["mat_key"])
    links = {e["evidence_key"]: e for e in records["evidence_links"]}
    non_solid_keys={m['mat_key'] for m in records.get('mats',[]) if
                    m.get('extensions',{}).get('semantic_role') in
                    ('mixing_water','activator','admixture','solution','upstream_reagent')}
    counts = {"mapped_quantities": 0, "mapped_mixes": 0}
    for mix in records.get("mixes", []):
        materials = mix["modules"]["materials"]
        parameters = materials.get("extensions", {}).get("reported_parameters", [])
        short = next((key for key in ROUTES if key in mix.get("paper_key", "")), None)
        routes = {normalized(name): dest for dest, names in ROUTES.get(short, {}).items() for name in names}
        # Existing native standardized rows remain authoritative. Repeated
        # calls add no duplicate row or evidence link.
        destinations = ("solid_materials", "activators", "fine_aggregate", "coarse_aggregate", "platform_ratios")
        existing = {r.get("extensions", {}).get("source_parameter_key"): (dest, r) for dest in destinations for r in materials.get(dest) or []}
        for parameter in parameters:
            parameter_key = parameter.get("parameter_key")
            candidates = mat_names.get(normalized(parameter_key), [])
            destination = routes.get(normalized(parameter_key))
            if destination is None and len(candidates) == 1 and candidates[0] not in non_solid_keys and parameter.get("unit") in MASS_UNITS:
                destination = "solid_materials"
            if parameter_key in existing:
                old_destination, old = existing[parameter_key]
                if old_destination != destination or old.get("value") != parameter.get("value") or old.get("unit") != parameter.get("unit") or old.get("transformation") != parameter.get("transformation"):
                    raise ValueError(f"canonical quantity drift: {mix['mix_key']} {parameter_key}")
                continue
            if destination is None:
                continue
            evidence = links.get(parameter.get("evidence_key"))
            if evidence is None or evidence.get("record_key") != mix["mix_key"]:
                raise ValueError(f"material quantity has no owned evidence: {mix['mix_key']} {parameter_key}")
            value = parameter.get("value")
            if destination != "platform_ratios" and value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
                continue
            row = {"name": parameter_key, "mat_key": candidates[0] if len(candidates) == 1 and destination != "platform_ratios" else None, "value": value, "unit": parameter.get("unit"),
                   "transformation": copy.deepcopy(parameter.get("transformation")),
                   "extensions": {"source_parameter_key": parameter_key, "mapping_version": VERSION,
                                  "original_label": parameter.get("original_label"), "status": parameter.get("status"),
                                  "reason": parameter.get("reason")}}
            if materials.get(destination) is None:
                materials[destination] = []
            index = len(materials[destination])
            path = f"/modules/materials/{destination}/{index}/value"
            evidence_key = "ev-material-map-" + hashlib.sha256(f"{mix['mix_key']}|{path}|{evidence['evidence_key']}".encode()).hexdigest()[:24]
            mapped_link = {**copy.deepcopy(evidence), "evidence_key": evidence_key, "field_path": path}
            records["evidence_links"].append(mapped_link)
            links[evidence_key] = mapped_link
            row["evidence_key"] = evidence_key
            materials[destination].append(row)
            mix.setdefault("field_provenance", {})[path] = {
                "evidence_key": evidence_key, "original_value": parameter.get("original_value"),
                "original_unit": parameter.get("original_unit"), "formula": (parameter.get("transformation") or {}).get("formula"),
                "extraction_method": VERSION, "confidence": parameter.get("confidence"), "review_status": "pending"}
            if parameter.get("source_binding"):
                mix["field_provenance"][path]["source_binding"]=copy.deepcopy(parameter["source_binding"])
            if row["mat_key"] and (value is None or value > 0) and row["mat_key"] not in materials.setdefault("mat_refs", []):
                materials["mat_refs"].append(row["mat_key"])
            existing[parameter_key] = (destination, row)
            counts["mapped_quantities"] += 1
        counts["mapped_mixes"] += any(materials.get(dest) for dest in destinations)
    records.setdefault("extensions", {})["canonical_material_mapping"] = {"version": VERSION}
    return counts
