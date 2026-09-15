#!/usr/bin/env python3
"""Build ignored local review assets from accepted WAKG pipeline runs."""
from __future__ import annotations

import argparse, atexit, hashlib, json, math, os, re, shutil, subprocess, sys, tempfile, time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import fitz

# Keep sibling helper imports stable when this file is executed directly and
# when a test loads it with ``importlib.util.spec_from_file_location``.
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
SKILL_SCRIPT_DIR = SCRIPT_DIR.parents[1] / "skills" / "wakg-literature-pipeline" / "scripts"
if str(SKILL_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SKILL_SCRIPT_DIR))
PROJECT_SCRIPT_DIR = SCRIPT_DIR.parents[1] / "scripts"
if str(PROJECT_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_SCRIPT_DIR))
import review_curve_projection as curve_review
import sparse_review_projection as sparse_review
import supplement_psd_review_projection as supplement_psd_review
import supplement_marker_review_projection as supplement_marker_review
import main_marker_review_projection as main_marker_review
import source_material_identity as material_identity

from curing_display import curing_summary as controlled_curing_summary
from inspect_pdf_corpus import inspect as inspect_pdf
from reported_parameters import (
    SOURCE_EXPLANATION_TEMPLATE_VERSION,
    SOURCE_EXPLANATION_TEMPLATES,
    STATUS_LABELS,
    material_reported_parameters,
    source_explanation,
)
from review_page_renderer import (
    RenderCacheMissing,
    materialize_cached_pages,
    render_pdf_to_cache,
)
from prehuman_review_packets import (
    BUDGET_KIND,
    budget_verdict as compact_budget_verdict,
    digest_value as compact_digest_value,
    load_cached_verified_plan,
    rebind_acceptance,
    seal_verified_plan,
    verify_plan as verify_compact_plan,
    write_review_packets,
    zero_usage_report,
)

USER10V2_RUNS=(
    "user10v2-1c116a8946fc","user10v2-4df39133d239","user10v2-55c422590f17",
    "user10v2-66c1278f6f7c","user10v2-7717e4cb8387","user10v2-7c7c47d33379",
    "user10v2-7f610942ec2f","user10v2-b7acfcbaf503","user10v2-993d666cc40b",
    "user10v2-f15f24af118a",
)
BLOCKED_EVIDENCE={}
PHASE_C_ROOT=Path("runs/user-corpus-10-efficiency-v2/phase-c-full10")
_PAGE_WORD_CACHE:dict[tuple[Any,int],list[tuple[Any,...]]]={}
_PAGE_BLOCK_CACHE:dict[tuple[Any,int],list[tuple[Any,...]]]={}
_PAGE_TOKEN_CACHE:dict[tuple[Any,int],dict[str,list[tuple[Any,...]]]]={}
_PAGE_SPAN_CACHE:dict[tuple[Any,int],list[tuple[float,float,float,float,str]]]={}
_BBOX_TEXT_CACHE:dict[tuple[Any,int,float,float,float,float],str]={}
_IDENTITY_CACHE:dict[tuple[str,str,int|None],tuple[int,list[float],str]|None]={}
_CURING_CACHE:dict[str,tuple[int,list[float],str]|None]={}
_GLOBAL_WORD_INDEX:dict[str,list[tuple[int,fitz.Rect,str]]]|None=None

def page_cache_key(page:fitz.Page)->tuple[Any,int]:
    # Keep the document object itself in the key.  Python may recycle ``id``
    # values after a PDF closes, which can otherwise return words cached for a
    # different paper/page during a long multi-paper process or test suite.
    return page.parent,int(page.number)

def cached_page_words(page:fitz.Page)->list[tuple[Any,...]]:
    key=page_cache_key(page)
    if key not in _PAGE_WORD_CACHE:_PAGE_WORD_CACHE[key]=page.get_text("words")
    return _PAGE_WORD_CACHE[key]

def cached_page_blocks(page:fitz.Page)->list[tuple[Any,...]]:
    key=page_cache_key(page)
    if key not in _PAGE_BLOCK_CACHE:_PAGE_BLOCK_CACHE[key]=page.get_text("blocks")
    return _PAGE_BLOCK_CACHE[key]

def cached_page_tokens(page:fitz.Page)->dict[str,list[tuple[Any,...]]]:
    key=page_cache_key(page)
    if key not in _PAGE_TOKEN_CACHE:
        index:dict[str,list[tuple[Any,...]]]={}
        for word in cached_page_words(page):
            index.setdefault(normalized_token(word[4]).casefold(),[]).append(word)
        _PAGE_TOKEN_CACHE[key]=index
    return _PAGE_TOKEN_CACHE[key]

def cached_page_spans(page:fitz.Page)->list[tuple[float,float,float,float,str]]:
    key=page_cache_key(page)
    if key not in _PAGE_SPAN_CACHE:
        spans=[]
        for block in page.get_text("dict").get("blocks",[]):
            for line in block.get("lines",[]):
                for span in line.get("spans",[]):
                    bbox=span.get("bbox");text=str(span.get("text") or "")
                    if isinstance(bbox,(list,tuple)) and len(bbox)==4 and text:spans.append((*[float(value) for value in bbox],text))
        _PAGE_SPAN_CACHE[key]=spans
    return _PAGE_SPAN_CACHE[key]

def cached_bbox_text(page:fitz.Page,bbox:list[float]|fitz.Rect)->str:
    rect=fitz.Rect(bbox)
    doc_key,page_number=page_cache_key(page)
    key=(doc_key,page_number,round(float(rect.x0),3),round(float(rect.y0),3),round(float(rect.x1),3),round(float(rect.y1),3))
    if key not in _BBOX_TEXT_CACHE:_BBOX_TEXT_CACHE[key]=page.get_textbox(rect)
    return _BBOX_TEXT_CACHE[key]

def global_word_index(doc:fitz.Document)->dict[str,list[tuple[int,fitz.Rect,str]]]:
    global _GLOBAL_WORD_INDEX
    if _GLOBAL_WORD_INDEX is None:
        index:dict[str,list[tuple[int,fitz.Rect,str]]]={}
        for page_index,page in enumerate(doc):
            for word in cached_page_words(page):
                index.setdefault(normalized_token(word[4]).casefold(),[]).append((page_index,fitz.Rect(word[:4]),str(word[4])))
        _GLOBAL_WORD_INDEX=index
    return _GLOBAL_WORD_INDEX

# Reviewer-facing names are a closed display vocabulary.  Canonical WAKG
# ``performance[].name`` values stay unchanged in records; only the review
# projection is translated.  This prevents internal snake_case identifiers
# and context-free specimen codes from leaking into the human decision layer.
PERFORMANCE_DISPLAY_NAMES={
    "tube_volume_increase":"密封管体积增长率",
    "fresh_paste_viscosity":"新拌浆体黏度",
    "bet_plate_reflux":"BET 比表面积（回流法）",
    "bet_plate_shaker":"BET 比表面积（振荡法）",
    "bet_tube_reflux":"BET 比表面积（回流法）",
    "bet_tube_shaker":"BET 比表面积（振荡法）",
    "compressive_strength_105_plate":"抗压强度（105 °C 养护）",
    "compressive_strength_105_tube":"抗压强度（105 °C 养护）",
    "compressive_strength_60_plate":"抗压强度（60 °C 养护）",
    "compressive_strength_60_tube":"抗压强度（60 °C 养护）",
    "compressive_strength":"抗压强度",
    "fracture_toughness":"断裂韧度",
    "flexural_strength":"抗折强度",
    "dynamic_yield_stress":"动态屈服应力",
    "plastic_viscosity":"塑性黏度",
    "v_funnel_discharging_time":"V 形漏斗流出时间",
    "discharging_time":"流出时间",
    "elastic_modulus_cnash_gel":"C-(N)-A-S-H 凝胶弹性模量",
    "hardness_cnash_gel":"C-(N)-A-S-H 凝胶硬度",
    "slump":"坍落度",
    "flow":"流动度",
    "flow_diameter":"流动直径",
    "setting_time":"凝结时间",
}
SPECIMEN_DISPLAY_NAMES={
    "cubes":"立方体试件",
    "prisms":"棱柱试件",
    "cube with size of 100 mm":"100 mm 立方体试件",
    "aafs concrete":"碱激发粉煤灰-矿渣混凝土",
    "fresh paste":"新拌浆体",
    "plate":"平板试件",
    "open plate":"开口平板试件",
    "tube":"管状试件",
    "sealed tube":"密封管试件",
    "reported specimen":"论文所述试件",
}
CHARACTERIZATION_DISPLAY_NAMES={
    "chemically_bound_water":"化学结合水",
    "initially_added_water":"初始加水量",
    "mass_loss":"质量损失",
    "open_porosity":"开口孔隙率",
    "oven_dried_density":"烘干密度",
    "real_density":"真密度",
    "unreacted_water":"未反应水",
    "unreacted_water_volume":"未反应水体积",
    "water_absorption":"吸水率",
}

# Canonical ``parameter_key`` values remain machine-readable in WAKG records,
# while the final human-review projection uses this closed display vocabulary.
# Unknown snake_case keys are a build error: silently exposing an internal key
# makes an otherwise correct scalar impossible to review without code context.
PARAMETER_DISPLAY_NAMES={
    "GGBS_content":"粒化高炉矿渣粉（GGBS）含量",
    "GGBS_to_FA_mass_ratio":"矿渣/粉煤灰质量比",
    "MgO_additive_mass":"MgO 外加质量",
    "MgO_amount":"MgO 用量",
    "MgO_molar_mass":"MgO 摩尔质量",
    "Na2O_to_precursor_mass_ratio":"Na₂O/前驱体质量比",
    "QS1_to_QS2_mass_ratio":"石英砂 QS1/QS2 质量比",
    "activator_modulus":"激发剂模数",
    "aggregate_to_paste_ratio":"骨料/浆体比",
    "alkali_activated_solution_to_precursor_mass_ratio":"碱激发溶液/前驱体质量比",
    "calcined_soil_content":"煅烧土含量",
    "equivalent_Na2O_content":"等效 Na₂O 含量",
    "foaming_agent_content":"发泡剂掺量",
    "initially_added_water":"初始加水量",
    "sand_to_binder_mass_ratio":"砂/胶凝材料质量比",
    "sand_to_precursor_ratio":"砂/前驱体比",
    "silicate_modulus_Ms":"硅酸盐模数 Ms",
    "slag_mass":"矿渣质量",
    "total_MgO_mass":"MgO 总质量",
    "total_alkali_content":"总碱含量",
    "total_precursor":"前驱体总量",
    "uncalcined_soil_content":"未煅烧土含量",
    "water_to_binder_mass_ratio":"水/胶凝材料质量比",
    "water_to_solid_mass_ratio":"水/固体质量比",
    "CA_10mm":"10 mm 粗骨料",
    "CA_20mm":"20 mm 粗骨料",
    "CCR_percent":"碳化混凝土再生料（CCR）占比",
    "CO2_curing_duration":"CO₂ 养护时长",
    "GGBS_percent":"粒化高炉矿渣粉（GGBS）占比",
    "LP_percent":"石灰石粉（LP）占比",
    "MgO_additive":"MgO 外加量",
    "Na2O_percent":"Na₂O 占比",
    "Na2SiO3_percent":"硅酸钠（Na₂SiO₃）占比",
    "NaOH_10M":"10 M NaOH 溶液",
    "RHA_SS_filtered":"过滤后的 RHA–硅酸钠溶液",
    "RHA_SS_unfiltered":"未过滤的 RHA–硅酸钠溶液",
    "SH":"氢氧化钠溶液（SH）",
    "SPs":"减水剂（SPs）",
    "SS":"硅酸钠溶液（SS）",
    "SS_percent":"硅酸钠溶液（SS）占比",
    "activator_solution_to_precursor":"激发剂溶液/前驱体",
    "added_water":"外加水",
    "calcined_soil_percent":"煅烧土占比",
    "commercial_SS":"商用硅酸钠溶液",
    "factor_value":"配比因子值",
    "fgr_to_precursor":"烟气脱硫残渣（FGR）/前驱体",
    "fly_ash":"粉煤灰",
    "fly_ash_fraction":"粉煤灰占胶凝材料比例",
    "ggbs_percent":"粒化高炉矿渣粉（GGBS）占比",
    "h2o2_mass_percent":"过氧化氢（H₂O₂）掺量",
    "h2o_to_x2o":"H₂O/X₂O 摩尔比",
    "na2o_to_precursor_percent":"Na₂O/前驱体",
    "sand":"砂（细骨料）",
    "spc_mass_percent":"过碳酸钠（SPC）掺量",
    "slag_fraction":"矿渣占胶凝材料比例",
    "sodium_silicate_solution":"硅酸钠溶液",
    "uncalcined_or_recycled_soil_percent":"未煅烧或再生土占比",
    "w_s":"水/固体比（w/s）",
    "water_to_binder":"水胶比",
    "water-to-binder":"水胶比（原文 binder 基准）",
    "sand-to-binder":"砂胶比（原文 binder 基准）",
    "total alkali":"总碱量（等效 Na₂O/前驱体）",
    "GGBS-to-FA":"矿渣/粉煤灰质量比",
    "QS#1-to-QS#2":"石英砂 QS#1/QS#2 质量比",
    "x2o_to_sio2":"X₂O/SiO₂ 摩尔比",
}

def load(path:Path)->dict[str,Any]:
    value=json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value,dict): raise ValueError(f"expected object: {path}")
    return value

def sha(path:Path)->str:
    digest=hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b""): digest.update(chunk)
    return digest.hexdigest()

def safe_run(value:str)->str:
    if not value or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in value): raise ValueError(f"unsafe run id: {value!r}")
    return value

def atomic_json(path:Path,value:dict[str,Any])->None:
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,temporary=tempfile.mkstemp(prefix=path.name+".",suffix=".tmp",dir=path.parent)
    try:
        with os.fdopen(fd,"w",encoding="utf-8",newline="\n") as stream:
            json.dump(value,stream,ensure_ascii=False,separators=(",",":"));stream.write("\n")
        os.replace(temporary,path)
    except Exception:
        try:os.unlink(temporary)
        except FileNotFoundError:pass
        raise

def acquire_single_instance_lock(project:Path):
    """Prevent duplicate full-corpus projection runs from wasting CPU."""
    path=project/"internal_assets"/"review-build.lock";path.parent.mkdir(parents=True,exist_ok=True)
    stream=path.open("a+b")
    if stream.tell()==0:stream.write(b"0");stream.flush()
    stream.seek(0)
    try:
        import msvcrt
        msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
    except (ImportError,OSError) as error:
        stream.close();raise RuntimeError("another review-bundle build is already running") from error
    stream.seek(0);stream.write(str(os.getpid()).encode("ascii").ljust(32,b" "));stream.flush();stream.seek(0)
    atexit.register(stream.close)
    return stream

def repair_state_root(project:Path)->Path:
    return project/"internal_assets"/"review-repair"

def write_internal_repair_state(project:Path,status:str,requests:dict[str,Any],defects:dict[str,Any],public_queue_published:bool=False)->None:
    target=repair_state_root(project)
    atomic_json(target/"evidence-repair-requests.json",requests)
    atomic_json(target/"extraction-repair-defects.json",defects)
    atomic_json(target/"status.json",{
        "schemaVersion":1,"status":status,"requestCount":requests["requestCount"],
        "extractionDefectCount":defects["defectCount"],"publicQueuePublished":public_queue_published,
    })

def stable_json(value:Any)->str:
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":"))

def review_subject(queue:dict[str,Any])->dict[str,Any]:
    papers=queue.get("papers")
    if not isinstance(papers,list) or not papers:raise ValueError("agent review subject has no papers")
    return {
        "schemaVersion":1,
        "queueScope":"USER10V2_FINAL_REVIEW",
        "sourceExplanationContract":queue.get("sourceExplanationContract"),
        "papers":papers,
    }

def review_subject_sha256(queue:dict[str,Any])->str:
    return hashlib.sha256(stable_json(review_subject(queue)).encode()).hexdigest()

def git_head(project:Path)->str:
    completed=subprocess.run(["git","rev-parse","HEAD"],cwd=project,check=True,capture_output=True,text=True)
    value=completed.stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}",value):raise ValueError("invalid project Git HEAD")
    return value

def agent_review_root(project:Path)->Path:
    return project/"internal_assets"/"pre-human-agent-review"

def invalidate_agent_review_state(project:Path,blocking_status:str)->None:
    root=agent_review_root(project);root.mkdir(parents=True,exist_ok=True)
    for name in ("candidate-queue.json","agent-review-request.json","accepted-receipt.json"):
        try:(root/name).unlink()
        except FileNotFoundError:pass
    shutil.rmtree(root/"review-packets",ignore_errors=True)
    atomic_json(root/"status.json",{
        "schemaVersion":1,
        "status":blocking_status,
        "subjectSha256":None,
        "reviewedHead":None,
        "paperCount":0,
        "receiptSha256":None,
    })

def agent_review_request(queue:dict[str,Any],head:str)->dict[str,Any]:
    papers=queue["papers"]
    return {
        "schemaVersion":1,"kind":"WAKG_PRE_HUMAN_AGENT_REVIEW_REQUEST",
        "reviewerRole":"PRE_HUMAN_AGENT","reviewedHead":head,
        "subjectSha256":review_subject_sha256(queue),
        "paperCount":len(papers),
        "papers":[{"runId":paper.get("runId"),"paperKey":paper.get("paperKey"),"pdfSha256":paper.get("pdfSha256")} for paper in papers],
        "requiredVerdict":"PASS|REJECT|ESCALATE",
        "policy":"read-only independent PDF-backed review before human publication",
    }

def write_agent_review_state(project:Path,queue:dict[str,Any],head:str,status:str,receipt:dict[str,Any]|None=None)->dict[str,Any]:
    root=agent_review_root(project);request=agent_review_request(queue,head)
    previous=load(root/"accepted-receipt.json") if (root/"accepted-receipt.json").is_file() else None
    atomic_json(root/"candidate-queue.json",queue)
    if status=="CLOSED" and receipt is not None:
        request.update({
            "reviewProtocolVersion":receipt.get("reviewProtocolVersion"),
            "reviewProtocolSha256":receipt.get("reviewProtocolSha256"),
            "wakgContractVersion":receipt.get("wakgContractVersion"),
            "semanticReviewNeeded":False,
        })
        atomic_json(root/"agent-review-request.json",request);atomic_json(root/"accepted-receipt.json",receipt)
        receipt_sha=hashlib.sha256(stable_json(receipt).encode()).hexdigest()
        atomic_json(root/"status.json",{"schemaVersion":1,"status":status,"subjectSha256":request["subjectSha256"],"reviewedHead":head,"paperCount":request["paperCount"],"receiptSha256":receipt_sha})
        return request
    cached=load_cached_verified_plan(project,request,root/"review-packets")
    if cached is None:
        plan=write_review_packets(project,queue,request,root/"review-packets")
        proof=verify_compact_plan(project,plan);atomic_json(root/"review-packets"/"proof.json",proof)
        if proof.get("verdict")!="PASS":raise ValueError("compact pre-human packet verification failed")
        seal_verified_plan(root/"review-packets",plan,proof)
    else:
        plan,proof=cached
    request.update({
        "reviewPacketPlan":str((root/"review-packets"/"plan.json").relative_to(project)).replace("\\","/"),
        "reviewPacketCount":len(plan["shards"]),
        "formalClaimCount":plan["formalClaimCount"],
        "reviewBudget":plan["budget"],
        "reviewProtocolVersion":plan["reviewProtocolVersion"],
        "reviewProtocolSha256":plan["reviewProtocolSha256"],
        "wakgContractVersion":plan["wakgContractVersion"],
        "reviewMethod":"model-free exhaustive locator verification, then one semantic pass per compact shard",
        "proofPath":str((root/"review-packets"/"proof.json").relative_to(project)).replace("\\","/"),
    })
    if receipt is None and status=="AGENT_REVIEW_REQUIRED" and previous is not None:
        rebound=rebind_acceptance(plan,proof,previous)
        if rebound is not None:
            usage=zero_usage_report();budget=compact_budget_verdict(plan,usage,rebound,proof)
            if budget.get("verdict")!="PASS":raise ValueError("reused pre-human acceptance failed budget binding")
            atomic_json(root/"review-packets"/"usage-zero.json",usage)
            atomic_json(root/"review-packets"/"reused-acceptance.json",rebound)
            atomic_json(root/"review-packets"/"budget-verdict.json",budget)
            request.update({
                "semanticReviewNeeded":False,
                "reusedAcceptancePath":str((root/"review-packets"/"reused-acceptance.json").relative_to(project)).replace("\\","/"),
                "budgetVerdictPath":str((root/"review-packets"/"budget-verdict.json").relative_to(project)).replace("\\","/"),
            })
    request.setdefault("semanticReviewNeeded",True)
    atomic_json(root/"agent-review-request.json",request)
    receipt_sha=None
    if receipt is not None:
        atomic_json(root/"accepted-receipt.json",receipt);receipt_sha=hashlib.sha256(stable_json(receipt).encode()).hexdigest()
    atomic_json(root/"status.json",{"schemaVersion":1,"status":status,"subjectSha256":request["subjectSha256"],"reviewedHead":head,"paperCount":request["paperCount"],"receiptSha256":receipt_sha})
    return request

def validate_agent_acceptance(queue:dict[str,Any],receipt:dict[str,Any],head:str)->dict[str,Any]:
    if receipt.get("kind")!="WAKG_PRE_HUMAN_AGENT_ACCEPTANCE" or receipt.get("reviewerRole")!="PRE_HUMAN_AGENT":raise ValueError("invalid pre-human agent acceptance envelope")
    subject=review_subject_sha256(queue)
    if receipt.get("subjectSha256")!=subject:raise ValueError("pre-human agent acceptance subject hash mismatch")
    if receipt.get("reviewedHead")!=head:raise ValueError("pre-human agent acceptance Git HEAD mismatch")
    if receipt.get("overallVerdict")!="PASS":raise ValueError("pre-human agent acceptance is not PASS")
    if not receipt.get("reviewProtocolSha256") or receipt.get("wakgContractVersion")!="WAKG-V1.1.4":raise ValueError("pre-human agent acceptance lacks the bound review protocol")
    results=receipt.get("paperResults")
    if not isinstance(results,list):raise ValueError("pre-human agent acceptance lacks paper results")
    expected={str(paper.get("runId")) for paper in queue["papers"]}
    actual={str(item.get("runId")) for item in results if isinstance(item,dict)}
    if len(results)!=len(expected) or actual!=expected:raise ValueError("pre-human agent acceptance paper coverage mismatch")
    for item in results:
        if item.get("verdict")!="PASS" or item.get("defects") not in ([],None):raise ValueError(f"paper did not pass pre-human agent review: {item.get('runId')}")
        if int(item.get("formalNonNullCount",-1))<0 or int(item.get("verifiedLocatorCount",-1))<0:raise ValueError("agent acceptance lacks verification counts")
        if int(item["verifiedLocatorCount"])!=int(item["formalNonNullCount"]):raise ValueError(f"agent locator coverage is incomplete: {item.get('runId')}")
    reviewers=receipt.get("reviewers")
    if not isinstance(reviewers,list) or not reviewers or any(not str(value).strip() for value in reviewers):raise ValueError("pre-human agent acceptance lacks reviewer identities")
    receipt_sha=hashlib.sha256(stable_json(receipt).encode()).hexdigest()
    return {"verdict":"PASS","reviewerRole":"PRE_HUMAN_AGENT","reviewerCount":len(reviewers),"reviewedHead":head,"subjectSha256":subject,"receiptSha256":receipt_sha,"reviewedAt":receipt.get("reviewedAt")}

def validate_review_budget(queue:dict[str,Any],receipt:dict[str,Any],budget:dict[str,Any],head:str)->dict[str,Any]:
    subject=review_subject_sha256(queue)
    if budget.get("kind")!=BUDGET_KIND or budget.get("verdict")!="PASS":raise ValueError("pre-human review budget gate is not PASS")
    if budget.get("reviewedHead")!=head or budget.get("subjectSha256")!=subject:raise ValueError("pre-human review budget identity mismatch")
    if budget.get("reviewProtocolSha256")!=receipt.get("reviewProtocolSha256") or budget.get("wakgContractVersion")!=receipt.get("wakgContractVersion"):raise ValueError("pre-human review budget protocol mismatch")
    if budget.get("acceptanceSha256")!=compact_digest_value(receipt):raise ValueError("pre-human review budget acceptance hash mismatch")
    if int(budget.get("proxyTokens",-1))>=int(budget.get("proxyTokensMaxExclusive",0)):raise ValueError("pre-human review token budget exceeded")
    if int(budget.get("modelEvents",-1))>int(budget.get("modelEventsMax",0)):raise ValueError("pre-human review model-event budget exceeded")
    return {"verdict":"PASS","proxyTokens":budget["proxyTokens"],"proxyTokensMaxExclusive":budget["proxyTokensMaxExclusive"],"modelEvents":budget["modelEvents"],"modelEventsMax":budget["modelEventsMax"],"budgetSha256":compact_digest_value(budget)}

def field_is_repair_state(item:dict[str,Any])->bool:
    return (
        bool(item.get("evidenceBlocked"))
        or item.get("status") in {"evidence_blocked","not_extracted"}
        or item.get("sourceExplanationCode") in {"SOURCE_EVIDENCE_BLOCKED","SOURCE_LOCATED_NOT_TRANSCRIBED"}
        or any("待修复" in str(item.get(key) or "") for key in ("statusLabel","reason"))
    )

def normalize_resolved_field(item:dict[str,Any])->None:
    if item.get("status")!="reported" or not item.get("evidenceKey") or item.get("evidenceBlocked"):return
    item["statusLabel"]=STATUS_LABELS["reported"]
    if "待修复" in str(item.get("reason") or ""):item["reason"]=None

def stamp_source_explanation(item:dict[str,Any],provenance_value:dict[str,Any]|None=None)->None:
    provenance_value=provenance_value or {"formula":item.get("sourceFormula")}
    code,label=source_explanation(str(item.get("status") or "system_note"),item.get("reason"),provenance_value)
    item["sourceExplanationCode"]=code;item["sourceExplanation"]=label

def validate_source_explanation(item:dict[str,Any])->None:
    code=item.get("sourceExplanationCode");label=item.get("sourceExplanation")
    if code not in SOURCE_EXPLANATION_TEMPLATES:raise ValueError(f"unsupported source explanation code: {code!r}")
    if label!=SOURCE_EXPLANATION_TEMPLATES[code]:raise ValueError(f"source explanation label does not match template: {code}")


def build_recall_correctness_gate(project:Path,papers:list[dict[str,Any]])->dict[str,Any]:
    """Require source recall and contract correctness before Agent review."""
    phase_root=project/PHASE_C_ROOT;summary_path=phase_root/"build-summary.json";contract_path=phase_root/"actual-pipeline-contract-validation.json"
    summary=load(summary_path);contract=load(contract_path);failures=[];rows=[]
    contract_by_run={str(item.get("source_run_id")):item for item in contract.get("results") or []}
    for paper in papers:
        run_id=str(paper.get("runId"));completeness_path=phase_root/run_id/"completeness-report.json";completeness=load(completeness_path)
        page_evidence={str(item.get("evidenceKey")) for page in paper.get("pages") or [] for item in page.get("evidence") or [] if item.get("evidenceKey")}
        reported=[item for group in ("mats","mixes") for card in (paper.get("records") or {}).get(group) or [] for item in card.get("fields") or [] if item.get("status")=="reported" and item.get("value") not in (None,"")]
        missing=[str(item.get("label")) for item in reported if str(item.get("evidenceKey") or "") not in page_evidence]
        supplement_expected=any(asset.get("kind")=="supplement" for asset in load(phase_root/run_id/"generated-records.json").get("assets") or [])
        supplement_visible=any(str(page.get("sourceKind") or "").startswith("supplement") for page in paper.get("pages") or [])
        row_failures=[]
        if completeness.get("verdict")!="PASS":row_failures.append("source_recall_blocked")
        if (contract_by_run.get(run_id) or {}).get("verdict")!="PASS":row_failures.append("contract_rejected")
        if missing:row_failures.append("reported_field_without_review_locator")
        if supplement_expected and not supplement_visible:row_failures.append("supplement_not_visible")
        failures.extend(f"{run_id}:{value}" for value in row_failures)
        rows.append({"runId":run_id,"recallVerdict":completeness.get("verdict"),"correctnessVerdict":(contract_by_run.get(run_id) or {}).get("verdict"),"reportedNonNullCount":len(reported),"reviewLocatorCount":len(reported)-len(missing),"supplementExpected":supplement_expected,"supplementVisible":supplement_visible,"failures":row_failures,"completenessSha256":sha(completeness_path)})
    expected={str(paper.get("runId")) for paper in papers};actual=set(contract_by_run)
    if expected!=actual:failures.append("paper_coverage_mismatch")
    if summary.get("completeness",{}).get("verdict")!="PASS":failures.append("batch_recall_blocked")
    if contract.get("verdict")!="PASS":failures.append("batch_contract_rejected")
    return {"schemaVersion":1,"kind":"WAKG_RECALL_CORRECTNESS_GATE","verdict":"PASS" if not failures else "REJECT","failures":failures,"paperCount":len(papers),"papers":rows,"sourceArtifacts":{"buildSummarySha256":sha(summary_path),"contractValidationSha256":sha(contract_path)},"policy":{"recall":"every inventoried source item is dispositioned; relevant items are extracted or explicitly quarantined","correctness":"formal WAKG contract, asset hashes, field provenance, and every displayed non-null locator pass"}}


def validate_recall_correctness_gate(queue:dict[str,Any],project:Path|None=None)->dict[str,Any]:
    gate=queue.get("recallCorrectnessGate") or {}
    if gate.get("kind")!="WAKG_RECALL_CORRECTNESS_GATE" or gate.get("verdict")!="PASS":raise ValueError("recall/correctness gate is not PASS")
    expected={str(paper.get("runId")) for paper in queue.get("papers") or []};actual={str(row.get("runId")) for row in gate.get("papers") or []}
    if expected!=actual or int(gate.get("paperCount",-1))!=len(expected):raise ValueError("recall/correctness paper coverage mismatch")
    if any(row.get("recallVerdict")!="PASS" or row.get("correctnessVerdict")!="PASS" or row.get("reviewLocatorCount")!=row.get("reportedNonNullCount") or row.get("failures") for row in gate.get("papers") or []):raise ValueError("recall/correctness paper gate contains a failure")
    if project is not None:
        artifacts=gate.get("sourceArtifacts") or {};phase_root=project/PHASE_C_ROOT
        if artifacts.get("buildSummarySha256")!=sha(phase_root/"build-summary.json") or artifacts.get("contractValidationSha256")!=sha(phase_root/"actual-pipeline-contract-validation.json"):raise ValueError("recall/correctness source artifact drift")
    return gate

def prepare_public_candidate(queue:dict[str,Any])->dict[str,Any]:
    repair=queue.get("evidenceRepair")
    if isinstance(repair,dict) and repair.get("status")!="CLOSED":raise ValueError("review repair is not closed; public queue publication is forbidden")
    papers=queue.get("papers")
    if not isinstance(papers,list) or not papers:raise ValueError("review queue has no papers")
    validate_recall_correctness_gate(queue)
    for paper in papers:
        records=paper.get("records") or {}
        for group in ("mats","mixes"):
            for card in records.get(group) or []:
                for item in card.get("fields") or []:
                    normalize_resolved_field(item);item.pop("_identityDerived",None)
                # Internal classifications, QC sums, synthesized row labels,
                # and unbound figure-level context are retained in the WAKG
                # records but are not paper-reported claims.  They must not be
                # shown to the final human as an unexplained "system note".
                card["fields"]=[item for item in card.get("fields") or [] if item.get("status")!="system_note"]
                if group=="mixes":
                    specimen=next((item for item in card["fields"] if item.get("semanticRole")=="source_specimen_id" and item.get("status")=="reported" and item.get("value") not in (None,"")),None)
                    card["label"]=f"试样 {specimen['value']}" if specimen else "配比"
                for item in card.get("fields") or []:validate_source_explanation(item)
                if any(field_is_repair_state(item) for item in card.get("fields") or []):raise ValueError(f"formal repair state remains: {paper.get('runId')} {card.get('key')}")
        records["quarantined"]=[card for card in records.get("quarantined") or [] if not any(field_is_repair_state(item) for item in card.get("fields") or [])]
        paper["records"]=records;paper.pop("evidenceBlocked",None);paper.pop("precisionBlockedFieldCount",None)
        evidence_keys={str(item.get("evidenceKey")) for group in ("mats","mixes","quarantined") for card in records.get(group) or [] for item in card.get("fields") or [] if item.get("evidenceKey")}
        page_keys=set()
        for page in paper.get("pages") or []:
            page["evidence"]=[item for item in page.get("evidence") or [] if str(item.get("evidenceKey")) in evidence_keys]
            page_keys.update(str(item.get("evidenceKey")) for item in page["evidence"] if item.get("evidenceKey"))
        for group in ("mats","mixes","quarantined"):
            for card in records.get(group) or []:
                for item in card.get("fields") or []:
                    validate_source_explanation(item)
                    if field_is_repair_state(item):raise ValueError("repair state survived final queue filter")
                    if group in ("mats","mixes") and item.get("status")=="system_note":raise ValueError("system-processing note survived final review filter")
                    has_value=item.get("value") not in (None,"")
                    if item.get("status")=="reported" and has_value and str(item.get("evidenceKey") or "") not in page_keys:raise ValueError(f"reported value lacks public evidence: {paper.get('runId')} {card.get('key')} {item.get('label')}")
    queue.pop("evidenceRepair",None);queue.pop("preHumanAgentReview",None)
    queue["reviewReadiness"]="PRE_HUMAN_AGENT_REVIEW_REQUIRED";queue["queueScope"]="USER10V2_FINAL_REVIEW"
    queue["sourceExplanationContract"]={
        "version":SOURCE_EXPLANATION_TEMPLATE_VERSION,
        "templates":dict(SOURCE_EXPLANATION_TEMPLATES),
    }
    queue.pop("generatedAt",None)
    return queue

def finalize_public_queue(queue:dict[str,Any],agent_acceptance:dict[str,Any],budget_verdict:dict[str,Any],head:str,project:Path|None=None)->dict[str,Any]:
    queue=prepare_public_candidate(queue)
    validate_recall_correctness_gate(queue,project)
    queue["preHumanAgentReview"]=validate_agent_acceptance(queue,agent_acceptance,head)
    queue["preHumanReviewBudget"]=validate_review_budget(queue,agent_acceptance,budget_verdict,head)
    queue["reviewReadiness"]="FINAL_REVIEW_READY"
    queue["generatedAt"]=datetime.now(timezone.utc).isoformat().replace("+00:00","Z")
    return queue

def remove_public_repair_envelopes(target:Path)->None:
    for name in ("evidence-repair-requests.json","extraction-repair-defects.json"):
        try:(target/name).unlink()
        except FileNotFoundError:pass

def batch_runs(project:Path,value:Path)->list[str]:
    path=(value if value.is_absolute() else project/value).resolve()
    if project not in path.parents or not path.is_file():raise ValueError(f"missing or unsafe batch manifest: {value}")
    batch=load(path)
    if batch.get("stage")!="ACCEPTED_WITH_QUARANTINES":raise ValueError("batch manifest is not accepted with quarantines")
    formal=batch.get("formal_runs");quarantined=batch.get("quarantined_runs")
    if not isinstance(formal,list) or not isinstance(quarantined,list):raise ValueError("batch manifest has invalid run lists")
    runs=[safe_run(str(run)) for run in [*formal,*quarantined]]
    if len(runs)!=len(set(runs)):raise ValueError("batch manifest has duplicate runs")
    for run in formal:
        if (load(project/"runs"/str(run)/"manifest.json").get("stage"))!="ACCEPTED":raise ValueError(f"formal run is not accepted: {run}")
    for run in quarantined:
        if (load(project/"runs"/str(run)/"manifest.json").get("stage"))!="QUARANTINED_MAP_ONLY":raise ValueError(f"quarantined run changed stage: {run}")
    return runs

def preserve_unselected_assets(target:Path,stage:Path,selected:set[str])->None:
    if not target.is_dir():return
    for child in target.iterdir():
        if child.name=="queue.json" or child.name in selected or not child.is_dir():continue
        shutil.copytree(child,stage/child.name)

def publish_staged_assets(public:Path,target:Path,stage:Path)->None:
    backup=public/f"review-assets-old-{os.getpid()}"
    moved_existing=False
    try:
        if target.exists():os.replace(target,backup);moved_existing=True
        os.replace(stage,target)
        if backup.exists():shutil.rmtree(backup)
    except PermissionError:
        # Fail closed if a Windows development server holds the directory.
        # Replacing children in place can leave an old queue pointing at a
        # partially updated asset tree if any later operation fails.
        if moved_existing and not target.exists() and backup.exists():os.replace(backup,target)
        raise

def materialize_review_candidate(project:Path,stage:Path,queue:dict[str,Any],scale:float)->None:
    for paper in queue.get("papers") or []:
        run_id=safe_run(str(paper.get("runId") or ""));run=project/"runs"/run_id
        manifest=load(run/"manifest.json")
        pdf=(project/str((manifest.get("input") or {}).get("pdf_path") or "")).resolve()
        expected=str((manifest.get("input") or {}).get("pdf_sha256") or "")
        if project.resolve() not in pdf.parents or not pdf.is_file():raise ValueError(f"missing or unsafe PDF: {run_id}")
        if expected!=paper.get("pdfSha256") or sha(pdf)!=expected:raise ValueError(f"candidate PDF mismatch: {run_id}")
        run_assets=stage/run_id;pages_dir=run_assets/"pages";run_assets.mkdir(parents=True,exist_ok=True)
        shutil.copy2(pdf,run_assets/"paper.pdf")
        cached=materialize_cached_pages(project,expected,scale,pages_dir)
        pages=paper.get("pages") or []
        if paper.get("pageCount") not in (None,len(pages)):raise ValueError(f"candidate page count mismatch: {run_id}")
        main_pages=[page for page in pages if page.get("sourceKind") in (None,"main")]
        if len(main_pages)!=cached.get("pageCount"):raise ValueError(f"candidate main-page count mismatch: {run_id}")
        materialized_supplements:set[tuple[str,str]]=set()
        for index,page in enumerate(pages,start=1):
            if page.get("number")!=index:raise ValueError(f"candidate page route mismatch: {run_id}:{index}")
            image_url=str(page.get("imageUrl") or "")
            prefix=f"review-assets/{run_id}/"
            if not image_url.startswith(prefix):raise ValueError(f"candidate page route mismatch: {run_id}:{index}")
            relative=Path(image_url[len(prefix):])
            destination=(run_assets/relative).resolve()
            if run_assets.resolve() not in destination.parents:raise ValueError(f"unsafe candidate page route: {run_id}:{index}")
            kind=str(page.get("sourceKind") or "main")
            source_page=page.get("sourcePage",index)
            if not isinstance(source_page,int) or source_page<1:raise ValueError(f"invalid candidate source page: {run_id}:{index}")
            if kind=="main":
                expected_url=f"review-assets/{run_id}/pages/page-{source_page:03d}.png"
                if image_url!=expected_url or page.get("sourceArtifactSha256") not in (None,expected):raise ValueError(f"candidate page route mismatch: {run_id}:{index}")
            elif kind=="supplement":
                source=(project/str(page.get("sourceArtifactPath") or "")).resolve();source_sha=str(page.get("sourceArtifactSha256") or "")
                if project.resolve() not in source.parents or not source.is_file() or source.suffix.casefold()!=".pdf" or sha(source)!=source_sha:raise ValueError(f"candidate supplement mismatch: {run_id}:{index}")
                if destination.name!=f"page-{source_page:03d}.png":raise ValueError(f"candidate supplement route mismatch: {run_id}:{index}")
                key=(source_sha,str(destination.parent))
                if key not in materialized_supplements:
                    manifest=materialize_cached_pages(project,source_sha,scale,destination.parent)
                    materialized_supplements.add(key)
                    if source_page>int(manifest.get("pageCount") or 0):raise ValueError(f"candidate supplement page mismatch: {run_id}:{index}")
            elif kind=="supplement-figure":
                source=(project/str(page.get("sourceArtifactPath") or "")).resolve();source_sha=str(page.get("sourceArtifactSha256") or "")
                if project.resolve() not in source.parents or not source.is_file() or sha(source)!=source_sha:raise ValueError(f"candidate supplement figure mismatch: {run_id}:{index}")
                destination.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,destination)
            else:raise ValueError(f"unsupported candidate source kind: {run_id}:{index}:{kind}")
            if not destination.is_file():raise ValueError(f"candidate page asset missing: {run_id}:{index}")

def merge_incremental_papers(previous:dict[str,Any],replacements:list[dict[str,Any]])->list[dict[str,Any]]:
    if previous.get("reviewReadiness")!="FINAL_REVIEW_READY":raise ValueError("incremental publication requires a final-ready previous queue")
    existing=previous.get("papers")
    if not isinstance(existing,list) or not existing:raise ValueError("incremental publication requires existing papers")
    by_run={str(paper.get("runId")):paper for paper in replacements}
    if not by_run or len(by_run)!=len(replacements):raise ValueError("incremental replacements must have unique run IDs")
    missing=sorted(set(by_run)-{str(paper.get("runId")) for paper in existing})
    if missing:raise ValueError(f"incremental replacement is not in the public queue: {missing}")
    return [by_run.get(str(paper.get("runId")),paper) for paper in existing]

def show(value:Any)->str:
    if value is None:return ""
    if isinstance(value,bool):return "true" if value else "false"
    if isinstance(value,float):return "0.0" if value==0.0 else f"{value:g}"
    if isinstance(value,(dict,list)):return json.dumps(value,ensure_ascii=False,separators=(",",":"))
    return str(value)

def provenance(record:dict[str,Any],path:str)->dict[str,Any]:
    value=(record.get("field_provenance") or {}).get(path)
    return value if isinstance(value,dict) else {}

def fold(value:Any)->str:
    text=re.sub(r"[^a-z0-9]+","",str(value or "").casefold()).replace("derived","")
    return {"na2sio3percent":"na2sio3","watertobinder":"wb","waterbinder":"wb","silicatemodulus":"ms","h2o2masspercent":"h2o2","spcmasspercent":"spc","x2otosio2":"x2osio2","h2otox2o":"h2ox2o"}.get(text,text)

def same_value(left:Any,right:Any)->bool:
    if left is None or right is None:return left is right
    try:return abs(float(left)-float(right))<=1e-9
    except (TypeError,ValueError):return str(left).strip()==str(right).strip()

def item_provenance(item:dict[str,Any])->dict[str,Any]:
    return {
        "evidence_key":item.get("evidence_key"),"original_value":item.get("original_value",item.get("value")),
        "original_unit":item.get("original_unit",item.get("unit")),"formula":item.get("formula") or ((item.get("transformation") or {}).get("formula") if isinstance(item.get("transformation"),dict) else None),
        "extraction_method":item.get("extraction_method") or "deterministic-field-transcription","confidence":item.get("confidence",0.99),"review_status":"confirmed",
        "source_binding":item.get("source_binding"),
    }

def precise_parameter(item:dict[str,Any],precise_items:list[dict[str,Any]])->dict[str,Any]|None:
    label=fold(item.get("original_label") or item.get("parameter_key"));unit=fold(item.get("unit"))
    matches=[candidate for candidate in precise_items if fold(candidate.get("original_label") or candidate.get("parameter_key"))==label]
    if len(matches)==1:return matches[0]
    unit_matches=[candidate for candidate in matches if fold(candidate.get("unit"))==unit]
    if len(unit_matches)==1:return unit_matches[0]
    exact=[candidate for candidate in unit_matches or matches if same_value(candidate.get("value"),item.get("value"))]
    return exact[0] if len(exact)==1 else None

def pair_precise_parameters(canonical_items:list[dict[str,Any]],precise_items:list[dict[str,Any]])->tuple[list[tuple[dict[str,Any],dict[str,Any]|None]],list[dict[str,Any]]]:
    used:set[int]=set();pairs=[]
    for item in canonical_items:
        label=fold(item.get("original_label") or item.get("parameter_key"))
        candidates=[(index,candidate) for index,candidate in enumerate(precise_items) if index not in used and fold(candidate.get("original_label") or candidate.get("parameter_key"))==label]
        value_matches=[(index,candidate) for index,candidate in candidates if same_value(candidate.get("value"),item.get("value"))]
        selected=None
        if len(value_matches)==1:selected=value_matches[0]
        elif len(value_matches)>1:
            unit=fold(item.get("unit"));unit_matches=[value for value in value_matches if fold(value[1].get("unit"))==unit]
            if len(unit_matches)==1:selected=unit_matches[0]
            else:raise ValueError(f"ambiguous precise parameter match: {item.get('original_label') or item.get('parameter_key')}")
        elif candidates and item.get("value") is not None:
            raise ValueError(f"conflicting canonical/precise parameter value: {item.get('original_label') or item.get('parameter_key')}")
        elif len(candidates)==1:selected=candidates[0]
        if selected is not None:used.add(selected[0])
        pairs.append((item,selected[1] if selected is not None else None))
    return pairs,[item for index,item in enumerate(precise_items) if index not in used]

def performance_equivalent(item:dict[str,Any],candidate:dict[str,Any])->bool:
    if same_value(candidate.get("value"),item.get("value")) and fold(candidate.get("unit"))==fold(item.get("unit")):return True
    ext=candidate.get("extensions") or {}
    return same_value(ext.get("original_value"),item.get("value")) and fold(ext.get("original_unit"))==fold(item.get("unit"))

def precise_performance(item:dict[str,Any],precise_items:list[dict[str,Any]])->dict[str,Any]|None:
    matches=[candidate for candidate in precise_items
             if fold(candidate.get("name"))==fold(item.get("name"))
             and performance_equivalent(item,candidate)]
    contextual=[candidate for candidate in matches
                if same_value(candidate.get("age_seconds"),item.get("age_seconds"))
                and fold(candidate.get("specimen"))==fold(item.get("specimen"))]
    if len(contextual)==1:return contextual[0]
    # A narrow source-table row is allowed to omit context only when the
    # scalar identity is itself unique. The merge below preserves canonical
    # age/specimen/method in that bounded case.
    return matches[0] if len(matches)==1 else None

def pair_precise_performance(canonical_items:list[dict[str,Any]],precise_items:list[dict[str,Any]])->tuple[list[tuple[int,dict[str,Any],int|None,dict[str,Any]|None]],list[tuple[int,dict[str,Any]]]]:
    used:set[int]=set();pairs=[]
    for canonical_index,item in enumerate(canonical_items):
        matches=[(index,candidate) for index,candidate in enumerate(precise_items) if index not in used and fold(candidate.get("name"))==fold(item.get("name")) and performance_equivalent(item,candidate)]
        contextual=[value for value in matches if same_value(value[1].get("age_seconds"),item.get("age_seconds")) and fold(value[1].get("specimen"))==fold(item.get("specimen")) and fold(value[1].get("method"))==fold(item.get("method"))]
        selected=None
        if len(contextual)==1:selected=contextual[0]
        elif len(contextual)>1:raise ValueError(f"ambiguous precise performance context: {item.get('name')}")
        elif len(matches)==1:selected=matches[0]
        elif len(matches)>1:raise ValueError(f"ambiguous precise performance scalar: {item.get('name')}")
        if selected is not None:used.add(selected[0])
        pairs.append((canonical_index,item,selected[0] if selected is not None else None,selected[1] if selected is not None else None))
    return pairs,[(index,item) for index,item in enumerate(precise_items) if index not in used]

def merged_performance(canonical:dict[str,Any],precise:dict[str,Any]|None)->dict[str,Any]:
    """Use precise scalar evidence without discarding canonical context.

    Phase-C rows are optimized for exact cell localization and can omit the
    specimen or age carried by the canonical WAKG observation.  Replacing the
    whole observation with that narrower row previously made the review page
    claim that context was missing.  Merge the layers explicitly instead.
    """
    if precise is None:return dict(canonical)
    merged=dict(precise)
    if canonical.get("name") is not None:merged["name"]=canonical["name"]
    if canonical.get("age_seconds") is not None:merged["age_seconds"]=canonical["age_seconds"]
    if canonical.get("specimen") not in (None,"reported specimen"):merged["specimen"]=canonical["specimen"]
    if canonical.get("method") not in (None,"source table"):merged["method"]=canonical["method"]
    merged_extensions=dict(canonical.get("extensions") or {})
    merged_extensions.update(precise.get("extensions") or {})
    if merged_extensions:merged["extensions"]=merged_extensions
    return merged

def field(label:str,value:Any,unit:Any=None,prov:dict[str,Any]|None=None,*,status:str="reported",reason:str|None=None,semantic_role:str|None=None,transformation:dict[str,Any]|None=None,evidence_blocked:bool=False)->dict[str,Any]:
    p=prov or {};has_value=value is not None
    source_dash=not has_value and (reason=='source_dash' or p.get('original_value')=='/')
    if source_dash:status='not_reported';reason='source_dash'
    linked=(has_value or source_dash) and bool(p.get("evidence_key")) and not evidence_blocked
    if transformation is None and isinstance(p.get("transformation"),dict):transformation=p["transformation"]
    if not has_value and status=="reported":
        status="not_extracted";reason=reason or "该字段未在当前正式记录中报告。"
    if has_value and status=="reported" and not p.get("evidence_key"):
        evidence_blocked=True;status="evidence_blocked";reason=reason or "该值尚未绑定可核验的原文位置；必须先由自动证据修复流程处理。"
    if evidence_blocked:
        status="evidence_blocked";reason=reason or "已知 bbox 绑定错误；证据已隔离，待修复。"
    item={"label":label,"value":show(value) if has_value else None,"unit":unit,"evidenceKey":p.get("evidence_key") if linked else None,"confidence":p.get("confidence") if linked else None,"originalValue":p.get("original_value") if linked else None,"originalUnit":p.get("original_unit") if linked else None,"status":status,"statusLabel":STATUS_LABELS.get(status,"系统处理说明"),"reason":reason,"semanticRole":semantic_role,"transformation":transformation,"sourceFormula":p.get("formula"),"sourceBinding":p.get("source_binding"),"evidenceBlocked":evidence_blocked}
    if p.get("extraction_method")=="reviewed-material-identity-v1":
        item["_materialIdentityReceipt"]={"source_parts":p.get("source_parts"),"acceptance_sha256":p.get("acceptance_sha256")}
    stamp_source_explanation(item,p);return item

def material_identity_key(value:Any)->str:
    text=fold(value)
    aliases={"ggbs":"slag","ggbfs":"slag","groundgranulatedblastfurnaceslag":"slag","bfs":"slag","fa":"flyash","classfflyash":"flyash","pc":"portlandcement","rha":"ricehuskash","fgr":"fluegasresidue"}
    return aliases.get(text,text)

def precise_material_match(canonical:dict[str,Any],precise_mats:list[dict[str,Any]],used:set[str])->dict[str,Any]|None:
    key=material_identity_key(canonical.get("custom_material_id") or canonical.get("material_type"))
    exact=[mat for mat in precise_mats if str(mat.get("mat_key")) not in used and material_identity_key(mat.get("custom_material_id") or mat.get("material_type"))==key]
    return exact[0] if len(exact)==1 else None

def has_scientific_material_data(mat:dict[str,Any])->bool:
    psd=mat.get("particle_size_distribution") or {}
    return bool((mat.get("xrf_composition") or {}).get("rows") or mat.get("xrd_qxrd") or mat.get("ftir_spectrum") or mat.get("si29_nmr_spectrum") or mat.get("al27_nmr_spectrum") or any(psd.get(key) is not None for key in ("d10_um","d50_um","d90_um","points_asset_key","source_image_asset_key")))

def xrf_provenance(record:dict[str,Any],component:Any)->dict[str,Any]:
    return provenance(record,f"/xrf_composition/rows/{component}/normalized_value") or provenance(record,f"/xrf_composition/rows/{component}")

def mat_identity_key(value:Any)->str:
    text=fold(value)
    aliases={"ggbs":"slag","ggbfs":"slag","bfs":"slag","groundgranulatedblastfurnaceslag":"slag","fa":"flyash","classfflyash":"flyash","pc":"portlandcement","rha":"ricehuskash","fgr":"fluegasresidue","calcinedkaolinmetakaolin":"calcinedkaolin"}
    return aliases.get(text,text)

def pair_review_mats(canonical:list[dict[str,Any]],precise:list[dict[str,Any]])->list[tuple[dict[str,Any],dict[str,Any]|None]]:
    by_identity:dict[str,list[int]]={}
    for index,item in enumerate(precise):by_identity.setdefault(mat_identity_key(item.get("custom_material_id")),[]).append(index)
    used:set[int]=set();pairs=[]
    for item in canonical:
        candidates=[index for index in by_identity.get(mat_identity_key(item.get("custom_material_id")),[]) if index not in used]
        selected=candidates[0] if len(candidates)==1 else None
        if selected is not None:used.add(selected)
        pairs.append((item,precise[selected] if selected is not None else None))
    for index,item in enumerate(precise):
        if index not in used and has_scientific_material_data(item):pairs.append((item,item));used.add(index)
    return pairs

def mix_identity_key(item:dict[str,Any])->str:
    modules=item.get("modules") or {}
    specimen=modules.get("identity_source_specimen") or {}
    return fold(specimen.get("custom_test_id"))

def mix_parameters(item:dict[str,Any])->list[dict[str,Any]]:
    return ((((item.get("modules") or {}).get("materials") or {}).get("extensions") or {}).get("reported_parameters") or [])

def source_row_anchor(item:dict[str,Any])->tuple[str,int]|None:
    extensions=item.get("extensions") or {};table=fold(extensions.get("source_table"));row=extensions.get("source_row")
    try:row=int(row)
    except (TypeError,ValueError):return None
    return (table,row) if table else None

def coded_identity_composition(item:dict[str,Any])->tuple[tuple[str,str],...]|None:
    """Parse compact source IDs such as C70G30U0 without dropping zeroes."""
    modules=item.get("modules") or {};specimen=modules.get("identity_source_specimen") or {};identity=str(specimen.get("custom_test_id") or "").strip()
    parts=re.findall(r"([A-Za-z]+)([-+]?\d+(?:\.\d+)?)",identity)
    if not parts or "".join(f"{name}{value}" for name,value in parts).casefold()!=re.sub(r"[^A-Za-z0-9.+-]","",identity).casefold():return None
    values={"U" if name.upper() in {"U","R"} else name.upper():show(float(value)) for name,value in parts}
    if "C" not in values or "G" not in values:return None
    values.setdefault("U","0.0")
    return tuple(sorted(values.items()))

def identity_alias_only(item:dict[str,Any])->bool:
    modules=item.get("modules") or {}
    return not (modules.get("performance") or modules.get("characterizations"))

def mix_overlap_score(canonical:dict[str,Any],precise:dict[str,Any])->int:
    score=0
    precise_items=mix_parameters(precise)
    for item in mix_parameters(canonical):
        exact=precise_parameter(item,precise_items)
        if exact is not None and same_value(exact.get("value"),item.get("value")):score+=1
    return score

def pair_review_mixes(canonical:list[dict[str,Any]],precise:list[dict[str,Any]])->list[tuple[dict[str,Any],dict[str,Any]|None]]:
    """Merge the semantic WAKG skeleton with the source-complete projection.

    The source-only pass is deliberately narrow enough to obtain exact cell
    boxes.  It may therefore omit fields already recovered by the semantic
    pass, and it may shorten a two-part row identity.  Pair both layers first;
    never replace the semantic row wholesale with the locator-oriented row.
    """
    used:set[int]=set();selected:dict[int,int]={}
    precise_by_id:dict[str,list[int]]={}
    for index,item in enumerate(precise):precise_by_id.setdefault(mix_identity_key(item),[]).append(index)
    precise_by_anchor:dict[tuple[str,int],list[int]]={}
    for index,item in enumerate(precise):
        anchor=source_row_anchor(item)
        if anchor:precise_by_anchor.setdefault(anchor,[]).append(index)
    # A table/row anchor is stronger than display identity and safely handles
    # repeated labels in factorial tables.
    for canonical_index,item in enumerate(canonical):
        anchor=source_row_anchor(item);candidates=[index for index in precise_by_anchor.get(anchor,[]) if index not in used] if anchor else []
        if len(candidates)==1:selected[canonical_index]=candidates[0];used.add(candidates[0])
        elif len(candidates)>1:raise ValueError(f"ambiguous precise MIX source-row anchor: {anchor}")
    # Reserve unambiguous literal identities next.
    for canonical_index,item in enumerate(canonical):
        if canonical_index in selected:continue
        candidates=[index for index in precise_by_id.get(mix_identity_key(item),[]) if index not in used]
        if len(candidates)==1:selected[canonical_index]=candidates[0];used.add(candidates[0])
    for canonical_index,item in enumerate(canonical):
        if canonical_index in selected:continue
        identity=mix_identity_key(item)
        candidates=[]
        for index,candidate in enumerate(precise):
            if index in used:continue
            candidate_identity=mix_identity_key(candidate)
            if not identity or not candidate_identity:continue
            if not (identity.startswith(candidate_identity) or candidate_identity.startswith(identity)):continue
            score=mix_overlap_score(item,candidate)
            if score:candidates.append((score,index))
        if candidates:
            best=max(score for score,_ in candidates);winners=[index for score,index in candidates if score==best]
            if len(winners)==1:selected[canonical_index]=winners[0];used.add(winners[0])
            else:raise ValueError(f"ambiguous precise MIX identity/parameter match: {identity}")
    matched_compositions={coded_identity_composition(canonical[index]) for index in selected if coded_identity_composition(canonical[index]) is not None}
    pairs=[]
    for index,item in enumerate(canonical):
        precise_index=selected.get(index)
        composition=coded_identity_composition(item)
        if precise_index is None and composition is not None and composition in matched_compositions and identity_alias_only(item):
            # A source-prose alias or typo that encodes the same complete
            # composition and adds no observation remains internal metadata.
            continue
        pairs.append((item,precise[precise_index] if precise_index is not None else None))
    for index,item in enumerate(precise):
        if index not in used:pairs.append((item,item))
    return pairs

def append_chart_fields(fields,record):
    """Consume native chart bridge fields in the existing MAT/MIX cards."""
    by_evidence={f.get('evidenceKey'):i for i,f in enumerate(fields) if f.get('evidenceKey')}
    for chart in record.get('extensions',{}).get('chart_review_fields',[]):
        prov=record.get('field_provenance',{}).get(chart['fieldPath'],{})
        label=fields[by_evidence[chart['evidenceKey']]]['label'] if chart['evidenceKey'] in by_evidence else ' · '.join(str(v) for v in (chart['label'],chart.get('series'),chart.get('category'),f"第{chart['page']}页" if chart.get('page') else None) if v)
        unit={'um':'μm','µm':'μm'}.get(chart.get('unit'),chart.get('unit'))
        item=field(label,chart['value'],unit,prov)
        item.update(_fieldPath=chart['fieldPath'],pointsAssetKey=chart.get('pointsAssetKey'),reviewRegions=chart.get('reviewRegions',[]),approximate=chart.get('approximate',True))
        item['observationCategory']=chart.get('category')
        if chart['fieldPath'].endswith('/age_seconds'):
            item['semanticRole']='performance_age'
        if chart.get('approximate',True) and isinstance(chart['value'],(int,float)):
            item['value']='约 '+format(chart['value'],'.4g')
        if chart['evidenceKey'] in by_evidence:
            index=by_evidence[chart['evidenceKey']]
            fields[index]={**fields[index],**item}
        else:fields.append(item)


def mat_card(mat:dict[str,Any],precise:dict[str,Any]|None=None)->dict[str,Any]:
    source=precise or mat
    canonical_xrf=mat.get("xrf_composition") or {};precise_xrf=(precise or {}).get("xrf_composition") or {}
    canonical_rows=canonical_xrf.get("rows") or [];precise_rows=precise_xrf.get("rows") or []
    precise_by_component={fold(row.get("component")):row for row in precise_rows if isinstance(row,dict) and row.get("component")}
    rows=[];used_components=set()
    for row in canonical_rows:
        component=fold(row.get("component"));replacement=precise_by_component.get(component)
        rows.append(replacement or row);used_components.add(component)
    rows.extend(row for row in precise_rows if fold(row.get("component")) not in used_components)
    xrf=precise_xrf or canonical_xrf
    material_type=source.get("material_type") or mat.get("material_type");material_id=source.get("custom_material_id") or mat.get("custom_material_id")
    material_type_prov=provenance(source,"/material_type") or provenance(mat,"/material_type")
    fields=[
        field("材料类型",material_type,prov=material_type_prov,status="reported" if material_type is not None and material_type_prov else "system_note" if material_type is not None else "not_reported",reason=None if material_type is not None and material_type_prov else "根据论文材料名称归入 WAKG 受控材料类型。" if material_type is not None else "原文未报告独立材料类型。",semantic_role="material_identity",transformation=None if material_type_prov else {"formula":"controlled material-type classification","formal":False}),
        field("文献材料 ID",material_id,prov=provenance(source,"/custom_material_id") or provenance(mat,"/custom_material_id"),status="reported" if material_id is not None else "not_reported",reason=None if material_id is not None else "原文未报告独立材料编号。",semantic_role="material_identity"),
    ]
    solution=(source.get('extensions') or {}).get('solution_specification') or (mat.get('extensions') or {}).get('solution_specification')
    if solution is not None:
        path='/extensions/solution_specification/value'
        if (solution.get('parameter_key')!='Molarity of SH' or solution.get('unit')!='M'
                or solution.get('basis')!='solution_concentration_not_dry_reagent_mass'
                or type(solution.get('value')) not in (int,float)):
            raise ValueError('unsupported solution specification projection')
        fields.append(field('溶液浓度',solution['value'],solution['unit'],
                            provenance(source,path) or provenance(mat,path),semantic_role='solution_concentration'))
    canonical_psd=mat.get("particle_size_distribution") or {};precise_psd=(precise or {}).get("particle_size_distribution") or {}
    psd={**canonical_psd,**{key:value for key,value in precise_psd.items() if value is not None}}
    for key,label in (("d10_um","粒径 D10"),("d50_um","中位粒径 D50"),("d90_um","粒径 D90")):
        if psd.get(key) is not None:fields.append(field(label,psd[key],"µm",provenance(source,f"/particle_size_distribution/{key}") or provenance(mat,f"/particle_size_distribution/{key}"),semantic_role="particle_size"))
    # Export the existing contract's physical properties, not just true density.
    # Select value and provenance from the same layer; a partial locator layer
    # must not hide unrelated canonical measurements or turn null into zero.
    for key,label,unit in (
        ("true_density_kg_m3","真密度","kg/m³"),
        ("apparent_density_kg_m3","表观密度","kg/m³"),
        ("bulk_density_kg_m3","堆积密度","kg/m³"),
        ("specific_surface_m2_kg","比表面积","m²/kg"),
        ("specific_surface_method","比表面积测试方法",None),
        ("total_porosity_percent","总孔隙率","%"),
        ("open_porosity_percent","开口孔隙率","%"),
        ("closed_porosity_percent","闭口孔隙率","%"),
    ):
        owner=precise if ((precise or {}).get("physical_properties") or {}).get(key) is not None else mat
        value=(owner.get("physical_properties") or {}).get(key)
        if value is not None:
            fields.append(field(label,value,unit,provenance(owner,f"/physical_properties/{key}"),semantic_role="physical_property"))
    pxrf=xrf;prows={fold(row.get("component")):row for row in pxrf.get("rows") or [] if isinstance(row,dict)}
    for row in rows:
        if not isinstance(row,dict) or not row.get("component"):continue
        value=row.get("normalized_value") if row.get("normalized_value") is not None else row.get("original_value")
        canonical_provenance=xrf_provenance(source,row["component"]) or xrf_provenance(mat,row["component"])
        repaired=str(canonical_provenance.get("extraction_method") or "").startswith("deterministic-repair")
        prow=None if repaired else prows.get(fold(row["component"]));pp=xrf_provenance(source,row["component"]) if prow else {}
        if prow:
            value=prow.get("normalized_value") if prow.get("normalized_value") is not None else prow.get("original_value")
        reason=(prow or row).get("reason") if value is None else None
        fields.append(field(str(row["component"]),value,(pxrf if prow else xrf).get("unit"),pp or canonical_provenance,status="reported" if value is not None else "not_reported",reason=reason or ("该正式 XRF 行未报告数值。" if value is None else None)))
    original_total=precise_xrf.get("original_total") if precise_xrf.get("original_total") is not None else canonical_xrf.get("original_total")
    if rows and original_total is not None:
        fields.append(field("XRF 原始合计",original_total,"wt.%",status="system_note",reason="由原表各个有数值的组分直接求和；不用于覆盖原始组分。",transformation={"formula":"sum(reported XRF components)","formal":False}))
    qxrd_source=source if source.get("xrd_qxrd") else mat
    qxrd=qxrd_source.get("xrd_qxrd") or {}
    if "rows" in qxrd or "internal_standard" in qxrd:
        raise ValueError("QXRD source uses obsolete fields; regenerate canonical records")
    for index,row in enumerate(qxrd.get("qxrd_phase_table") or []):
        value_key='mass_fraction_percent' if row.get('mass_fraction_percent') is not None else 'value'
        value=row.get(value_key)
        item=field(f"QXRD · {row.get('phase') or '物相'}",value,row.get('unit') or "wt.%",provenance(qxrd_source,f"/xrd_qxrd/qxrd_phase_table/{index}/{value_key}"),status="reported" if value is not None else "not_reported",semantic_role="qxrd_phase_fraction")
        item['sourceBasis']=row.get('basis') or row.get('reported_basis')
        fields.append(item)
    if qxrd.get("amorphous_total_percent") is not None:
        fields.append(field("QXRD 无定形相总含量",qxrd["amorphous_total_percent"],"wt.%",provenance(qxrd_source,"/xrd_qxrd/amorphous_total_percent"),semantic_role="qxrd_amorphous_total"))
    for key,label in (("standard_formula","QXRD 标样化学式"),("standard_method","QXRD 标样方式")):
        if qxrd.get(key) is not None:fields.append(field(label,qxrd[key],None,provenance(qxrd_source,f"/xrd_qxrd/{key}")))
    fraction=qxrd.get("standard_fraction_percent")
    if fraction is not None:
        fraction_provenance=provenance(qxrd_source,"/xrd_qxrd/standard_fraction_percent")
        fields.append(field("QXRD 内标占掺标混合物",fraction,"%",fraction_provenance,semantic_role="qxrd_internal_standard_fraction",transformation=fraction_provenance.get("transformation")))
    fields.extend(curve_review.display_fields(source,field))
    asset_refs=[]
    for key,label in ((psd.get("source_image_asset_key"),"PSD 原始图"),(psd.get("points_asset_key"),"PSD 数据点"),(qxrd.get("points_asset_key"),"XRD/QXRD 数据点")):
        if key:asset_refs.append({"label":label,"assetKey":key})
    # Preserve the canonical graph key when precise evidence is layered onto an
    # existing MAT.  Replacing it with the locator-pass key breaks MIX.matRefs.
    append_chart_fields(fields,mat)
    return {"key":mat.get("mat_key") or source.get("mat_key"),"label":material_id or mat.get("mat_key") or source.get("mat_key"),"kind":"MAT · 材料","status":"extracted_review_pending","fields":fields,"assets":asset_refs}

def identity_provenance(mix:dict[str,Any],specimen:dict[str,Any],materials_provenance:dict[str,Any])->dict[str,Any]:
    source_id=specimen.get("custom_test_id")
    specific=provenance(mix,"/modules/identity_source_specimen/custom_test_id")
    if source_id is not None and specific.get("evidence_key"):return specific
    # A table-row evidence link is not automatically evidence for the exact
    # specimen identifier. Leave it unbound so the PDF localizer must find the
    # literal ID. This avoids falsely classifying a direct source token as a
    # composite value without a composition formula.
    return {}

def display_parameter_label(item:dict[str,Any])->str:
    raw=str(item.get("original_label") or item.get("parameter_key") or "").strip()
    if raw in PARAMETER_DISPLAY_NAMES:raw=PARAMETER_DISPLAY_NAMES[raw]
    if re.fullmatch(r"[A-Za-z0-9]+(?:_[A-Za-z0-9]+)+",raw):
        raise ValueError(f"review display vocabulary missing parameter label: {raw}")
    return raw+(' · '+str(item['_display_basis']) if item.get('_display_basis') else '')

def parameter_field(item:dict[str,Any],materials_provenance:dict[str,Any],blocked:set[str],precise_items:list[dict[str,Any]],field_path:str|None=None)->dict[str,Any]:
    repaired=str(item.get("extraction_method") or "").startswith("deterministic-repair")
    exact=None if repaired else precise_parameter(item,precise_items);source=exact or item;value=source.get("value");evidence=source.get("evidence_key")
    provenance_value=item_provenance(source) if exact or repaired or evidence else (materials_provenance if value is not None and evidence==materials_provenance.get("evidence_key") else {})
    source_label=str(source.get("original_label") or source.get("parameter_key") or "")
    formula=(provenance_value or {}).get("formula") or ((source.get("transformation") or {}).get("formula") if isinstance(source.get("transformation"),dict) else None)
    original_zero_token=(provenance_value or {}).get("original_value",source.get("original_value"))
    explicit_zero=bool(re.search(r"(?<![A-Za-z0-9])0(?:\.0+)?(?![A-Za-z0-9])",str(original_zero_token if original_zero_token is not None else "")))
    if same_value(value,0) and not explicit_zero and not formula:
        # ``No CO2`` or ``100% slag`` is scientifically useful context but is
        # not a verbatim numerical zero. Keep the formal field null rather than
        # manufacturing a scalar that cannot have token-level evidence.
        return field(display_parameter_label(item),None,source.get("unit"),status="not_reported",reason="原文以成分或否定语义说明该项未加入，但未直接报告数值 0；正式值保持为空。",semantic_role=source.get("semantic_role"))
    result=field(display_parameter_label(item),value,source.get("unit"),provenance_value,status=str(source.get("status")),reason=item.get("reason") or source.get("reason"),semantic_role=source.get("semantic_role"),transformation=source.get("transformation"),evidence_blocked=bool(evidence and evidence in blocked))
    result["_sourceLabel"]=source_label
    if field_path:result["_fieldPath"]=field_path
    return result

def apply_identity_encoded_transformation(field_value:dict[str,Any],source_id:Any)->None:
    identity=str(source_id or "").strip();source_label=str(field_value.get("_sourceLabel") or "")
    if not identity or field_value.get("value") in (None,""):return
    formula=None
    prefix={"calcined_soil_percent":"C","ggbs_percent":"G","uncalcined_or_recycled_soil_percent":"UR"}.get(source_label)
    if prefix:
        components={key:raw for key,raw in re.findall(r"([A-Za-z])([-+]?\d+(?:\.\d+)?)",identity)}
        keys=("U","R") if prefix=="UR" else (prefix,)
        matched=next((components[key] for key in keys if key in components and same_value(components[key],field_value.get("value"))),None)
        if matched is not None:formula=f"{source_label} = numeric component following {'U or R' if prefix=='UR' else prefix} in the paper-reported specimen ID"
    if not formula:return
    field_value.update({"originalValue":identity,"sourceFormula":formula,"transformation":{"formula":formula,"formal":True},"_identityDerived":True})
    stamp_source_explanation(field_value,{"formula":formula})

def performance_label(perf:dict[str,Any])->str:
    raw_name=str(perf.get("name") or "性能")
    name=PERFORMANCE_DISPLAY_NAMES.get(raw_name.casefold(),raw_name)
    raw_specimen=str(perf.get("specimen") or "").strip()
    parts=[name]
    if raw_specimen:parts.append(SPECIMEN_DISPLAY_NAMES.get(raw_specimen.casefold(),raw_specimen))
    method=str(perf.get('method') or '').strip()
    if method:parts.append(method)
    age=perf.get('age_seconds')
    if age is not None:
        if isinstance(age,str):
            parts.append(age)
            return ' · '.join(parts)
        seconds=float(age)
        if not math.isfinite(seconds) or seconds<0:raise ValueError('invalid performance age for display')
        value,unit=(seconds/86400,'d') if seconds%86400==0 else (seconds/3600,'h')
        parts.append(f'{show(value)} {unit}')
    return ' · '.join(parts)

def performance_age_value(age:Any)->tuple[str,str]:
    if isinstance(age,str):return age,""
    hours=float(age)/3600
    return (show(hours),"h")

def performance_method_fields(owner:dict[str,Any],index:int,perf:dict[str,Any])->list[dict[str,Any]]:
    ext=perf.get('extensions') or {}
    if not (ext.get('prose_binding_id') or ext.get('sparse_binding_id')):return []
    result=[]
    for suffix,label,value in [('method','试验方法',perf.get('method')),
        ('extensions/calculation_method','计算方法（原文公式引用）',(perf.get('extensions') or {}).get('calculation_method'))]:
        if value is None:continue
        path=f'/modules/performance/{index}/{suffix}';source=provenance(owner,path)
        if not source.get('evidence_key'):raise ValueError('prose performance method lacks source evidence')
        row=field(f'{label} · {performance_label(perf)}',value,None,source,status='reported',semantic_role='performance_method')
        if source.get('source_parts'):
            row['_methodSourceParts']=source['source_parts'];row['methodFieldPath']=path
        row['_fieldPath']=path;result.append(row)
    return result

def review_age_provenance(age:Any,provenance_value:dict[str,Any])->dict[str,Any]:
    if not provenance_value:return {}
    projected=dict(provenance_value or {})
    if isinstance(age,str):
        projected.pop('formula',None)
        projected.pop('transformation',None)
        return projected
    hours=float(age)/3600.0
    upstream=projected.get("transformation") if isinstance(projected.get("transformation"),dict) else None
    inputs=(upstream or {}).get("inputs") if isinstance((upstream or {}).get("inputs"),dict) else None
    if inputs and isinstance(inputs.get("value"),(int,float)) and isinstance(inputs.get("scale"),(int,float)):
        original=float(inputs["value"]);original_unit=str(inputs.get("unit") or "source_unit");scale=float(inputs["scale"])/3600.0
    else:
        original_text=str(projected.get("original_value") or "").strip().casefold()
        declared_unit=str(projected.get("original_unit") or "").strip().casefold()
        word_numbers={"one":1.0,"two":2.0,"three":3.0,"seven":7.0,"fourteen":14.0,"twenty-eight":28.0,"fifty-six":56.0,"ninety":90.0}
        numeric_match=re.search(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)",original_text)
        word_match=next((number for word,number in word_numbers.items() if re.search(rf"\b{re.escape(word)}\b",original_text)),None)
        parsed=float(numeric_match.group()) if numeric_match else word_match
        unit_scales={"d":24.0,"day":24.0,"days":24.0,"h":1.0,"hour":1.0,"hours":1.0,"min":1.0/60.0,"minute":1.0/60.0,"minutes":1.0/60.0,"s":1.0/3600.0,"second":1.0/3600.0,"seconds":1.0/3600.0}
        normalized_unit=declared_unit or next((unit for unit in ("days","day","hours","hour","minutes","minute","seconds","second") if re.search(rf"\b{unit}\b",original_text)),"")
        if parsed is not None and normalized_unit in unit_scales:
            original=float(parsed);original_unit=normalized_unit;scale=unit_scales[normalized_unit]
            if abs(original*scale-hours)>1e-9:raise ValueError("reported age source disagrees with canonical age_seconds")
        else:
            original=float(age);original_unit="s";scale=1.0/3600.0
    formula=f"hours = {original_unit} * {scale:g}"
    projected["formula"]=formula
    projected["transformation"]={
        "kind":"unit_scale","formal":True,"formula":formula,
        "inputs":{"value":original,"unit":original_unit,"scale":scale},
        "output":{"value":hours,"unit":"h"},
        "forward_check":{"computed":original*scale,"recorded":hours,"tolerance":1e-9},
        "reverse_check":{"computed":hours/scale,"recorded":original,"tolerance":1e-9},
    }
    return projected

def canonical_performance_projection(perf:dict[str,Any],prov:dict[str,Any])->tuple[Any,Any,dict[str,Any]]|None:
    """Return a reviewer projection that respects WAKG canonical units.

    Ambiguous source units are not silently repaired in the UI. They remain
    internal/quarantined until the extraction stage can resolve the semantics.
    """
    name=str(perf.get("name") or "").casefold();unit=str(perf.get("unit") or "").strip();value=perf.get("value")
    if "viscosity" in name and unit.casefold()=="pa":return None
    if name.startswith("bet_") and unit.casefold().replace("²","2")=="m2/kg":
        projected=dict(prov);projected["formula"]="m²/kg = m²/g × 1000"
        return value,"m²/kg",projected
    if name.startswith("bet_") and unit.casefold().replace("²","2")=="m2/g" and value is not None:
        converted=float(value)*1000.0
        projected=dict(prov)
        projected.update({
            "original_value":prov.get("original_value",value),"original_unit":prov.get("original_unit") or unit,
            "formula":"m²/kg = m²/g × 1000",
        })
        return converted,"m²/kg",projected
    return value,perf.get("unit"),prov

def curing_summary(curing:dict[str,Any])->str|None:
    return controlled_curing_summary(curing,show)

def mix_card(mix:dict[str,Any],blocked:set[str]|None=None,precise:dict[str,Any]|None=None)->dict[str,Any]:
    blocked=blocked or set()
    precise=precise or {};modules=mix.get("modules") or {};pmodules=precise.get("modules") or {};specimen=modules.get("identity_source_specimen") or {}
    canonical_curing=modules.get("mixing_curing") or {};precise_curing=pmodules.get("mixing_curing") or {}
    precise_curing_status=str((precise_curing.get("extensions") or {}).get("review_status") or "")
    curing=precise_curing if precise_curing.get("curing_stages") or precise_curing_status else canonical_curing
    mp=provenance(mix,"/modules/materials")
    cp=provenance(precise if curing is precise_curing else mix,"/modules/mixing_curing/curing_stages")
    curing_value=curing_summary(curing)
    pparams=((((pmodules.get("materials") or {}).get("extensions") or {}).get("reported_parameters")) or [])
    source_id=specimen.get("custom_test_id");derived_identity=bool(re.match(r"^Table\s+\d+\s+row\s+\d+:",str(source_id or ""),re.I))
    displayed_source_id=None if derived_identity else source_id
    identity_reason="论文没有独立试样编号；系统生成的表格行描述不作为正式字段。" if derived_identity else None
    curing_status="reported" if curing_value is not None else precise_curing_status or "not_extracted"
    curing_reason_code=str((curing.get("extensions") or {}).get("review_reason_code") or "")
    curing_reasons={"fresh_individual_stream_only":"该记录仅对应新拌态个体流测试；论文未为其报告硬化养护路线。"}
    if curing_reason_code and curing_reason_code not in curing_reasons:raise ValueError(f"unsupported curing review reason code: {curing_reason_code}")
    curing_reason=None if curing_value is not None else curing_reasons.get(curing_reason_code,"本轮未单独转录养护标签。")
    parameters=material_reported_parameters(mix)
    parameter_fields=[];parameter_pairs,unmatched_precise_parameters=pair_precise_parameters(parameters,pparams)
    for index,(item,exact) in enumerate(parameter_pairs):
        if sum(p.get('parameter_key')==item.get('parameter_key') for p in parameters)>1:
            item={**item,'_display_basis':item.get('basis') or item.get('unit')}
        # Canonical quantities already carry their own field-bound evidence.
        # A legacy precise overlay must not replace that authority.
        if item.get("_canonical_material"):exact=None
        parameter_fields.append(parameter_field(item,mp,blocked,[exact] if exact is not None else [],item.get("_field_path") or f"/modules/materials/extensions/reported_parameters/{index}/value"))
    # The precise source pass can add table/supplement columns that were not in
    # the semantic skeleton.  Append only unmatched columns; the canonical
    # fields above remain present even when the precise row is narrower.
    for index,item in enumerate(unmatched_precise_parameters):
        parameter_fields.append(parameter_field(item,{},blocked,[],f"/modules/materials/extensions/reported_parameters/{index}/value"))
    fields=[field("试样",displayed_source_id,None,identity_provenance(mix,specimen,mp) if displayed_source_id is not None else {},status="reported" if displayed_source_id is not None else "not_reported",reason=identity_reason,semantic_role="source_specimen_id"),*parameter_fields,field("养护条件",curing_value,None,cp,status=curing_status,reason=curing_reason,semantic_role="curing_condition")]
    for projected_field in fields:apply_identity_encoded_transformation(projected_field,displayed_source_id)
    pperformance=pmodules.get("performance") or []
    canonical_performance=modules.get("performance") or [];performance_pairs,unmatched_precise_performance=pair_precise_performance(canonical_performance,pperformance)
    for index,perf,precise_index,exact in performance_pairs:
        if not isinstance(perf,dict):continue
        path=f"/modules/performance/{index}/value";p=provenance(mix,path)
        if exact:
            ext=exact.get("extensions") or {};p=item_provenance({**exact,**ext})
        source=merged_performance(perf,exact)
        projection=canonical_performance_projection(source,p)
        if projection is None:continue
        projected_value,projected_unit,projected_provenance=projection
        performance_field=field(performance_label(source),projected_value,projected_unit,projected_provenance,status="reported" if projected_value is not None else "not_reported",reason=None if projected_value is not None else "该性能观察未报告标量值。",semantic_role="performance_observation")
        performance_field["_sourceLabel"]=str(source.get("name") or "")
        performance_field["_fieldPath"]=path
        fields.append(performance_field)
        fields.extend(performance_method_fields(mix,index,perf))
        # Phase-C may supply a more precise value locator without carrying the
        # canonical observation age.  Age is a separate formal field and must
        # therefore come from the canonical performance record.
        age=perf.get("age_seconds")
        if age is not None:
            age_value,age_unit=performance_age_value(age)
            age_provenance=review_age_provenance(age,provenance(mix,f"/modules/performance/{index}/age_seconds"))
            age_field=field(f"龄期 · {performance_label(source)}",age_value,age_unit,age_provenance,status="reported",reason=None,semantic_role="performance_age")
            age_field["_fieldPath"]=f"/modules/performance/{index}/age_seconds"
            fields.append(age_field)
    for index,perf in unmatched_precise_performance:
        if not isinstance(perf,dict):continue
        path=f"/modules/performance/{index}/value";p=item_provenance({**perf,**(perf.get("extensions") or {})})
        projection=canonical_performance_projection(perf,p)
        if projection is None:continue
        projected_value,projected_unit,projected_provenance=projection
        performance_field=field(performance_label(perf),projected_value,projected_unit,projected_provenance,status="reported" if projected_value is not None else "not_reported",reason=None if projected_value is not None else "该性能观察未报告标量值。",semantic_role="performance_observation")
        performance_field["_sourceLabel"]=str(perf.get("name") or "");performance_field["_fieldPath"]=path;fields.append(performance_field)
        fields.extend(performance_method_fields(precise,index,perf))
        if perf.get("age_seconds") is not None:
            age_value,age_unit=performance_age_value(perf["age_seconds"]);age_prov=review_age_provenance(perf["age_seconds"],provenance(precise,f"/modules/performance/{index}/age_seconds"))
            if age_prov:
                age_field=field(f"龄期 · {performance_label(perf)}",age_value,age_unit,age_prov,status="reported",semantic_role="performance_age")
                age_field["_fieldPath"]=f"/modules/performance/{index}/age_seconds";fields.append(age_field)
    canonical_characterizations=modules.get("characterizations") or [];precise_characterizations=pmodules.get("characterizations") or []
    characterization_pairs=[];used_precise_characterizations:set[int]=set()
    for canonical_index,item in enumerate(canonical_characterizations):
        matches=[(index,candidate) for index,candidate in enumerate(precise_characterizations) if index not in used_precise_characterizations and fold(candidate.get("name"))==fold(item.get("name")) and same_value(candidate.get("value"),item.get("value")) and fold(candidate.get("unit"))==fold(item.get("unit"))]
        if len(matches)>1:raise ValueError(f"ambiguous precise characterization match: {item.get('name')}")
        precise_index,exact=matches[0] if matches else (None,None)
        if precise_index is not None:used_precise_characterizations.add(precise_index)
        characterization_pairs.append((item,exact,precise if exact is not None else mix,precise_index if exact is not None else canonical_index))
    characterization_pairs.extend((item,item,precise,index) for index,item in enumerate(precise_characterizations) if index not in used_precise_characterizations)
    for canonical_item,precise_item,owner,owner_index in characterization_pairs:
        item=precise_item or canonical_item
        if not isinstance(item,dict):continue
        if (item.get("extensions") or {}).get("spectrum"):
            # An asset-valued spectrum is not a missing scalar observation.
            spectrum_path=f"/modules/characterizations/{owner_index}/extensions/spectrum/points_asset_key"
            native=provenance(owner,spectrum_path)
            if native.get('extraction_method')=='chart_readout' and any(
                f.get('fieldPath')==spectrum_path and f.get('evidenceKey')==native.get('evidence_key')
                for f in owner.get('extensions',{}).get('chart_review_fields',[])):
                continue  # Rendered below as an asset field, with pending provenance.
            if provenance(owner,spectrum_path).get("extraction_method")!="reviewed-vector-contract-v1":
                raise ValueError("spectrum has no verified reviewer projection")
            continue
        raw_name=str(item.get("name") or "characterization")
        label=CHARACTERIZATION_DISPLAY_NAMES.get(raw_name)
        if label is None:
            if re.fullmatch(r"[A-Za-z0-9]+(?:_[A-Za-z0-9]+)+",raw_name):raise ValueError(f"review display vocabulary missing characterization label: {raw_name}")
            label=raw_name
        path=f"/modules/characterizations/{owner_index}/value"
        item_ext=item.get("extensions") or {}
        p=provenance(owner,path) or item_provenance({**item,**item_ext})
        value=item.get("value")
        fields.append(field(label,value,item.get("unit"),p,status="reported" if value is not None else "not_reported",reason=item.get("reason") or ("原文未报告该表征数值。" if value is None else None),semantic_role="material_characterization"))
        age=item.get("age_seconds")
        if age is not None:
            age_value,age_unit=performance_age_value(age)
            age_prov=review_age_provenance(age,provenance(owner,f"/modules/characterizations/{owner_index}/age_seconds"))
            if age_prov:fields.append(field(f"龄期 · {label}",age_value,age_unit,age_prov,status="reported",semantic_role="characterization_age"))
    fields.extend(curve_review.display_fields(precise or mix,field))
    append_chart_fields(fields,mix)
    return {"key":mix.get("mix_key"),"label":f"试样 {displayed_source_id}" if displayed_source_id else "论文未单列试样编号","kind":"MIX · 配比与性能","status":"extracted_review_pending","fields":fields}

def quarantine_card(item:dict[str,Any])->dict[str,Any]:
    return {"key":item.get("candidate_key"),"label":item.get("record_key") or item.get("candidate_key"),"kind":"候选值 · 未转正","status":"quarantined","fields":[field("字段",item.get("field_path"),status="not_applicable" if item.get("field_path") is None else "system_note",reason="Map-only quarantine has no formal field." if item.get("field_path") is None else "系统字段路径，不是论文中的数值。"),field("正式值",item.get("formal_value"),status="not_applicable" if item.get("formal_value") is None else "reported",reason="No formal value was published." if item.get("formal_value") is None else None),field("候选值",item.get("proposed_value"),status="not_applicable" if item.get("proposed_value") is None else "reported",reason="No candidate scalar was promoted." if item.get("proposed_value") is None else None),field("隔离原因",item.get("reason"),status="system_note",reason="系统隔离说明，不是论文原文数值。")]}

def token(value:Any)->str|None:
    if value is None:return None
    text=str(value).strip().replace("−","-").replace("–","-").replace(",","")
    return text if text and not re.search(r"\s",text) and len(text)<=48 else None

def normalized_token(value:Any)->str:
    return re.sub(r"^[\[(]|[\]),;:*]+$","",str(value or "").strip().replace("−","-").replace("–","-").replace(",",""))

def numeric_token(value:Any)->str|None:
    candidate=token(value)
    if candidate is None:return None
    try:float(normalized_token(candidate))
    except ValueError:return None
    return candidate

def single_numeric_token(value:Any)->str|None:
    matches=re.findall(r"(?<![A-Za-z0-9])[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?![A-Za-z0-9])",str(value or "").replace(",",""))
    return matches[0] if len(matches)==1 else None

def same_source_token(actual:Any,expected:Any)->bool:
    left,right=normalized_token(actual),normalized_token(expected)
    if left==right:return True
    try:return float(left)==float(right)
    except ValueError:return False

def exact_word_bbox(page:fitz.Page,bbox:list[float],expected:str,label:str|None=None)->tuple[list[float],str]|None:
    region=fitz.Rect(bbox);words=cached_page_words(page);matches=[]
    expected_key=normalized_token(expected).casefold()
    indexed_words=cached_page_tokens(page).get(expected_key)
    candidate_words=indexed_words
    if candidate_words is None:
        # Preserve numeric-equivalence support for uncommon PDF token forms.
        candidate_words=words
    for word in candidate_words:
        center=fitz.Point((float(word[0])+float(word[2]))/2,(float(word[1])+float(word[3]))/2)
        if region.contains(center) and same_source_token(word[4],expected):matches.append(word)
    if not matches and indexed_words is not None:
        # An exact lexical token may exist elsewhere on the page while the
        # cited cell uses a numerically equivalent spelling (60 vs 60.0).
        # Fall back only for this local region, keeping the common indexed path.
        for word in words:
            center=fitz.Point((float(word[0])+float(word[2]))/2,(float(word[1])+float(word[3]))/2)
            if region.contains(center) and same_source_token(word[4],expected):matches.append(word)
    if len(matches)!=1 and label:
        headers=[word for word in words if normalized_token(word[4]).casefold()==normalized_token(label).casefold() and float(word[3])<=region.y0 and region.y0-float(word[3])<=120]
        if not headers:
            label_terms=set(re.findall(r"[a-z0-9]+",str(label).casefold()))-{"percent","fraction","value"}
            semantic=[]
            for word in words:
                if float(word[3])>region.y0 or region.y0-float(word[3])>120:continue
                compact=normalized_compact(word[4]);score=sum(term in compact for term in label_terms)
                if score:semantic.append((score,word))
            if semantic and ("ratio" in str(label).casefold() or "ccr" in str(label).casefold() or "factor" in str(label).casefold()):
                best=max(score for score,_ in semantic)
                # One matching word in a previous data row is not a column
                # header (for example ``ash`` above another 50/50 row).  A
                # semantic header fallback must encode at least two label terms.
                if best>=1:headers=[word for score,word in semantic if score==best]
        if headers:
            header=max(headers,key=lambda word:(float(word[3]),-float(word[0])));header_x=(float(header[0])+float(header[2]))/2
            column_matches=[word for word in words if float(header[3])<=float(word[1])<=region.y1 and abs((float(word[0])+float(word[2]))/2-header_x)<=max(8.0,float(header[2])-float(header[0])) and same_source_token(word[4],expected)]
            if column_matches:
                row_y=(region.y0+region.y1)/2
                matches=[min(column_matches,key=lambda word:(abs((float(word[0])+float(word[2]))/2-header_x),abs((float(word[1])+float(word[3]))/2-row_y)))]
    if len(matches)>1:
        unique={tuple(round(float(value),3) for value in word[:4])+(str(word[4]),):word for word in matches}
        matches=list(unique.values())
    if len(matches)>1 and label:
        label_terms=set(re.findall(r"[a-z0-9]+",str(label).casefold()))-{"percent","fraction","value","ratio"}
        aliases={"fly":{"fly","f"},"co2":{"co2"},"mgo":{"mgo"}}
        scored=[]
        for match in matches:
            block=int(match[5]) if len(match)>5 else -1
            center=(float(match[0])+float(match[2]))/2;row_y=(float(match[1])+float(match[3]))/2
            nearby=[word for word in words if (len(word)>5 and int(word[5])==block and abs((float(word[1])+float(word[3]))/2-row_y)<=3.5)]
            hit_count=0;distance=0.0
            for term in label_terms:
                accepted=aliases.get(term,{term});candidates=[]
                for word in nearby:
                    compact=normalized_compact(word[4])
                    if compact and any(value in compact or compact in value for value in accepted):
                        candidates.append(abs((float(word[0])+float(word[2]))/2-center))
                if candidates:hit_count+=1;distance+=min(candidates)
                else:distance+=1000.0
            scored.append((hit_count,distance,match))
        best_hits=max(score for score,_,_ in scored);best_distance=min(distance for score,distance,_ in scored if score==best_hits)
        candidates=[match for score,distance,match in scored if score==best_hits and abs(distance-best_distance)<=1e-6]
        if best_hits>0 and len(candidates)==1:matches=candidates
    if len(matches)!=1:return None
    word=matches[0];pad=0.8
    bbox=[round(max(0.0,float(word[0])-pad),3),round(max(0.0,float(word[1])-pad),3),round(min(float(page.rect.width),float(word[2])+pad),3),round(min(float(page.rect.height),float(word[3])+pad),3)]
    return bbox,str(word[4])

def exact_text_bbox(page:fitz.Page,bbox:list[float],expected:Any)->tuple[list[float],str]|None:
    text=str(expected or "").strip()
    if not text:return None
    region=fitz.Rect(bbox);hits=bounded_phrase_rects(page,text,region)
    if len(hits)!=1:return None
    hit=hits[0];pad=0.8
    precise=[round(max(0.0,float(hit.x0)-pad),3),round(max(0.0,float(hit.y0)-pad),3),round(min(float(page.rect.width),float(hit.x1)+pad),3),round(min(float(page.rect.height),float(hit.y1)+pad),3)]
    return precise,text

def reported_age_phrase_bbox(page:fitz.Page,bbox:list[float],original_value:Any,original_unit:Any=None)->tuple[list[float],str]|None:
    try:value=float(original_value)
    except (TypeError,ValueError):return None
    words={1:"one",2:"two",3:"three",7:"seven",14:"fourteen",28:"twenty-eight",56:"fifty-six",90:"ninety"}
    if not math.isfinite(value) or value < 0:return None
    whole=int(value)
    if value!=whole:return None
    unit=str(original_unit or '').casefold()
    suffixes=("day","days","d") if unit in ('day','days','d') else ("hour","hours","h","hr") if unit in ('hour','hours','h','hr') else ("day","days","hour","hours")
    for suffix in suffixes:
        expressions=[f"{whole}-{suffix}",f"{whole} {suffix}"]
        if whole in words:expressions.append(f"{words[whole]} {suffix}")
        for expression in expressions:
            located=exact_text_bbox(page,bbox,expression)
            if located:return located
    return None

def expanded_context_numeric_bbox(page:fitz.Page,bbox:list[float],expected:Any,label:str)->tuple[list[float],str]|None:
    if numeric_token(expected) is None:return None
    region=fitz.Rect([float(value) for value in bbox])
    expanded=[max(0.0,region.x0-2.0),max(0.0,region.y0-3.0),float(page.rect.width),min(float(page.rect.height),region.y1+18.0)]
    return exact_word_bbox(page,expanded,str(expected),label)

def global_context_numeric_bbox(doc:fitz.Document,expected:Any,label:str,record_key:Any)->tuple[int,list[float],str]|None:
    expected_token=numeric_token(expected)
    if expected_token is None:return None
    label_terms=set(re.findall(r"[a-z0-9]+",str(label).casefold()))-{"percent","fraction","value","duration","ratio","additive"}
    record_terms={term for term in re.findall(r"[a-z0-9]+",str(record_key).casefold()) if len(term)>=3 and term not in {"mix","user10","user10v2"} and not re.fullmatch(r"[0-9a-f]{8,}",term)}
    candidates=[]
    for page_index,rect,actual in global_word_index(doc).get(normalized_token(expected_token).casefold(),[]):
        page=doc[page_index];cy=(rect.y0+rect.y1)/2
        nearby=[word for word in cached_page_words(page) if abs((float(word[1])+float(word[3]))/2-cy)<=10.0]
        compact={normalized_compact(word[4]) for word in nearby if normalized_compact(word[4])}
        label_hits=sum(any(term in value or value in term for value in compact) for term in label_terms)
        record_hits=sum(term in compact for term in record_terms)
        score=10*record_hits+3*label_hits
        candidates.append((score,record_hits,label_hits,page_index,float(rect.y0),float(rect.x0),rect,actual))
    if not candidates:return None
    candidates.sort(key=lambda item:(-item[0],-item[1],-item[2],item[3],item[4],item[5]));best=candidates[0]
    if best[2]<1:return None
    if len(candidates)>1 and candidates[1][:3]==best[:3]:return None
    return best[3]+1,projected_bbox(best[6],doc[best[3]]),best[7]

def global_specimen_family_numeric_bbox(doc:fitz.Document,expected:Any,source_id:Any)->tuple[int,list[float],str]|None:
    expected_token=numeric_token(expected);identity=str(source_id or "").strip()
    family_match=re.match(r"([A-Za-z]+)[-_]",identity)
    if expected_token is None or not family_match:return None
    family=family_match.group(1).casefold();candidates=[]
    for page_index,rect,actual in global_word_index(doc).get(normalized_token(expected_token).casefold(),[]):
        page=doc[page_index];cy=(rect.y0+rect.y1)/2
        nearby=[word for word in cached_page_words(page) if abs((float(word[1])+float(word[3]))/2-cy)<=4.0 and abs((float(word[0])+float(word[2]))/2-(rect.x0+rect.x1)/2)<=180]
        family_hits=sum(normalized_compact(word[4])==family for word in nearby)
        candidates.append((family_hits,page_index,float(rect.y0),float(rect.x0),rect,actual))
    if not candidates:return None
    candidates.sort(key=lambda item:(-item[0],item[1],item[2],item[3]));best=candidates[0]
    if best[0]<1 or (len(candidates)>1 and candidates[1][0]==best[0]):return None
    return best[1]+1,projected_bbox(best[4],doc[best[1]]),best[5]

def reclassify_unreported_zero(field_value:dict[str,Any])->None:
    field_value.update({
        "value":None,"evidenceKey":None,"confidence":None,"originalValue":None,"originalUnit":None,
        "status":"not_reported","statusLabel":STATUS_LABELS["not_reported"],"evidenceBlocked":False,
        "reason":"原文以成分或否定语义说明该项未加入，但未直接报告数值 0；正式值保持为空。",
    })
    stamp_source_explanation(field_value)

def global_reported_age_phrase_bbox(doc:fitz.Document,original_value:Any,candidate_page:int|None=None)->tuple[int,list[float],str]|None:
    try:value=float(original_value)
    except (TypeError,ValueError):return None
    words={1:"one",2:"two",3:"three",7:"seven",14:"fourteen",28:"twenty-eight",56:"fifty-six",90:"ninety"}
    whole=int(value)
    if value!=whole or whole not in words:return None
    candidates=[]
    for page_index,page in enumerate(doc):
        for suffix in ("day","days","hour","hours"):
            phrase=f"{words[whole]} {suffix}"
            for hit in page.search_for(phrase):
                context=page.get_textbox(fitz.Rect(max(0,hit.x0-140),max(0,hit.y0-35),min(page.rect.width,hit.x1+180),min(page.rect.height,hit.y1+35)))
                terms=set(re.findall(r"[a-z]+",context.casefold()))
                score=(8 if candidate_page==page_index+1 else 0)+3*len(terms&{"strength","compressive","tested","testing","specimen","sample"})
                candidates.append((score,page_index,float(hit.y0),float(hit.x0),hit,phrase))
    if not candidates:return None
    candidates.sort(key=lambda item:(-item[0],item[1],item[2],item[3]));best=candidates[0]
    if len(candidates)>1 and candidates[1][:4]==best[:4]:return None
    return best[1]+1,projected_bbox(best[4],doc[best[1]]),best[5]

CURING_CONTEXT={"cure","cured","curing","demolded","demolding","demoulded","demoulding","laboratory","standard","conditions","sealed","testing"}
MATERIAL_CONTEXT={"material","materials","binder","precursor","slag","ash","cement","sand","supplied","used","composition"}

def normalized_compact(value:Any)->str:
    return re.sub(r"[^a-z0-9]+","",str(value or "").casefold())

def projected_bbox(hit:fitz.Rect,page:fitz.Page)->list[float]:
    pad=0.8
    return [round(max(0.0,float(hit.x0)-pad),3),round(max(0.0,float(hit.y0)-pad),3),round(min(float(page.rect.width),float(hit.x1)+pad),3),round(min(float(page.rect.height),float(hit.y1)+pad),3)]

def _word_rect(word:tuple[Any,...],page:fitz.Page)->list[float]:
    word_rect=fitz.Rect([float(value) for value in word[:4]]);expected=normalized_token(word[4]).casefold()
    exact=[span for span in cached_page_spans(page) if normalized_token(span[4]).casefold()==expected and word_rect.intersects(fitz.Rect(span[:4]))]
    if exact:
        tight=min(exact,key=lambda span:(fitz.Rect(span[:4]).get_area(),float(span[1]),float(span[0])))
        return projected_bbox(fitz.Rect(tight[:4]),page)
    return projected_bbox(word_rect,page)

def composite_specimen_identity_parts(doc:fitz.Document,source_id:Any)->tuple[list[tuple[int,list[float],str]],str]|None:
    """Locate the paper cells that jointly define a composite specimen ID."""
    identity=str(source_id or "").strip()
    match=re.fullmatch(r"([A-Za-z0-9]+)-([-+]?\d+(?:\.\d+)?)",identity)
    if not match:return None
    family,factor=match.groups();candidates=[]
    for page_index,page in enumerate(doc):
        words=cached_page_words(page)
        families=[word for word in words if normalized_token(word[4]).casefold()==family.casefold()]
        factors=[word for word in words if same_source_token(word[4],factor)]
        if not families or not factors:continue
        table_words=[word for word in words if normalized_token(word[4]).casefold()=="table"]
        for family_word in families:
            family_y=(float(family_word[1])+float(family_word[3]))/2
            table_context=any(0<=family_y-float(word[3])<=90 for word in table_words)
            for factor_word in factors:
                factor_y=(float(factor_word[1])+float(factor_word[3]))/2
                dy=factor_y-family_y;dx=float(factor_word[0])-float(family_word[0])
                if dy < -3 or dy > 36 or dx < 0:continue
                score=(100 if table_context else 0)-4*abs(dy)+min(dx,500)/500
                candidates.append((score,page_index,family_word,factor_word))
    if not candidates:return None
    candidates.sort(key=lambda item:(-item[0],item[1],float(item[2][1]),float(item[3][0])))
    best=candidates[0]
    if len(candidates)>1 and abs(candidates[1][0]-best[0])<1e-9:return None
    page=doc[best[1]]
    parts=[(best[1]+1,_word_rect(best[2],page),str(best[2][4])),(best[1]+1,_word_rect(best[3],page),str(best[3][4]))]
    return parts,"custom_test_id = paper table family label + '-' + paper table factor value"

def direct_formulation_component_bbox(doc:fitz.Document,source_id:Any,source_label:str,value:Any)->tuple[int,list[float],str,str]|None:
    """Prefer an explicit scalar near its material label and specimen ID."""
    expected=numeric_token(value);identity=str(source_id or "").strip()
    aliases={
        "calcined_soil_percent":{"calcined"},
        "ggbs_percent":{"ggbs"},
        "uncalcined_or_recycled_soil_percent":{"uncalcined","recycled"},
    }.get(source_label)
    preferred={
        "calcined_soil_percent":{"corresponding","contents","consisted","consists"},
        "ggbs_percent":{"kept","content","contents","levels","used"},
        "uncalcined_or_recycled_soil_percent":{"levels","content","contents","considered"},
    }.get(source_label,set())
    if expected is None or not identity or not aliases:return None
    candidates=[]
    for page_index,page in enumerate(doc):
        identity_hits=native_phrase_rects(page,identity)
        if not identity_hits:continue
        words=cached_page_words(page)
        semantic=[word for word in words if normalized_token(word[4]).casefold().strip(".,;:()") in aliases]
        numeric=[word for word in words if same_source_token(word[4],expected)]
        if not semantic or not numeric:continue
        for number_word in numeric:
            nx=(float(number_word[0])+float(number_word[2]))/2;ny=(float(number_word[1])+float(number_word[3]))/2
            label_distance=min(abs(ny-(float(word[1])+float(word[3]))/2)*5+abs(nx-(float(word[0])+float(word[2]))/2) for word in semantic)
            identity_distance=min(abs(ny-(hit.y0+hit.y1)/2)*3+abs(nx-(hit.x0+hit.x1)/2)/4 for hit in identity_hits)
            if label_distance>260 or identity_distance>360:continue
            nearby_terms={normalized_token(word[4]).casefold().strip(".,;:()") for word in words if abs(ny-(float(word[1])+float(word[3]))/2)<=16}
            preferred_hits=len(preferred&nearby_terms)
            candidates.append((label_distance+identity_distance-80*preferred_hits,page_index,float(number_word[1]),float(number_word[0]),number_word))
    if not candidates:return None
    candidates.sort(key=lambda item:(item[0],item[1],item[2],item[3]));best=candidates[0]
    if len(candidates)>1 and abs(candidates[1][0]-best[0])<1e-6:return None
    formula=f"{source_label} = explicit paper scalar mapped to specimen {identity} by the paper's adjacent labeling context"
    return best[1]+1,_word_rect(best[4],doc[best[1]]),str(best[4][4]),formula

def cached_context_text(page:fitz.Page,rect:fitz.Rect)->str:
    """Join cached text blocks intersecting a small context rectangle."""
    return " ".join(
        str(block[4] or "")
        for block in cached_page_blocks(page)
        if fitz.Rect(block[:4]).intersects(rect)
    )

def literal_source_phrase(clip:str,expected:str)->str|None:
    """Return the shortest verbatim clip substring matching a source phrase."""
    if expected in clip:
        return expected
    words=[word for word in re.split(r"\s+",expected.strip()) if word]
    if not words:
        return None
    match=re.search(r"[\s\u00a0]+".join(re.escape(word) for word in words),clip,re.IGNORECASE)
    return match.group(0) if match else None

def phrase_hit_is_token_bounded(page:fitz.Page,hit:fitz.Rect,text:str)->bool:
    """Reject MuPDF substring hits such as ``B`` in ``Table``.

    ``search_for`` is glyph based and intentionally finds substrings.  That is
    useful for prose search but unsafe for evidence localization.  A formal
    source phrase must equal one or more complete PDF words after conservative
    punctuation folding; being merely contained inside a larger word is not
    sufficient.
    """
    expected=normalized_compact(text)
    if not expected:return False
    intersecting=[]
    for word in cached_page_words(page):
        rect=fitz.Rect(word[:4])
        if rect.intersects(hit) and min(rect.y1,hit.y1)-max(rect.y0,hit.y0)>0:
            compact=normalized_compact(word[4])
            if compact:intersecting.append((float(word[1]),float(word[0]),compact))
    intersecting.sort()
    tokens=[item[2] for item in intersecting]
    for start in range(len(tokens)):
        joined=""
        for end in range(start,len(tokens)):
            joined+=tokens[end]
            if joined==expected:return True
            if len(joined)>=len(expected):break
    return False

def complete_word_phrase_bbox(page:fitz.Page,region:fitz.Rect,text:str)->tuple[list[float],str]|None:
    """Locate a phrase as a contiguous sequence of complete PDF words."""
    expected=normalized_compact(text)
    if not expected:return None
    words=[word for word in cached_page_words(page) if fitz.Rect(word[:4]).intersects(region)]
    words.sort(key=lambda word:(int(word[5]) if len(word)>5 else 0,int(word[6]) if len(word)>6 else 0,int(word[7]) if len(word)>7 else 0,float(word[1]),float(word[0])))
    for start in range(len(words)):
        joined=""
        for end in range(start,len(words)):
            compact=normalized_compact(words[end][4])
            if not compact:continue
            joined+=compact
            if joined==expected:
                union=fitz.Rect(words[start][:4])
                for word in words[start+1:end+1]:union|=fitz.Rect(word[:4])
                bbox=projected_bbox(union,page);source=cached_bbox_text(page,fitz.Rect(bbox)).strip()
                return bbox,source
            if len(joined)>=len(expected):break
    return None

def bounded_phrase_rects(page:fitz.Page,text:str,clip:fitz.Rect|None=None)->list[fitz.Rect]:
    """Return native phrase hits only when complete source words match."""
    return [fitz.Rect(hit) for hit in page.search_for(text,clip=clip) if phrase_hit_is_token_bounded(page,fitz.Rect(hit),text)]

def native_phrase_rects(page:fitz.Page,text:str)->list[fitz.Rect]:
    """Return native hits plus same-line unions for font-split phrases."""
    raw=[fitz.Rect(hit) for hit in page.search_for(text)]
    hits=list(raw)
    if len(hits)<2 or (" " not in text and not any(char in text for char in "()[]{}")):
        return [hit for hit in hits if phrase_hit_is_token_bounded(page,hit,text)]
    components:list[list[fitz.Rect]]=[]
    for hit in hits:
        center=(hit.y0+hit.y1)/2
        for component in components:
            component_center=sum((item.y0+item.y1)/2 for item in component)/len(component)
            left=min(item.x0 for item in component);right=max(item.x1 for item in component)
            horizontal_gap=max(0.0,left-hit.x1,hit.x0-right)
            if abs(center-component_center)<=4.0 and horizontal_gap<=12.0:
                component.append(hit)
                break
        else:
            components.append([hit])
    unions=[]
    for component in components:
        if len(component)<2:
            continue
        union=fitz.Rect(component[0])
        for hit in component[1:]:
            union|=hit
        unions.append(union)
    return [hit for hit in unions+hits if phrase_hit_is_token_bounded(page,hit,text)]

def global_identity_bbox(doc:fitz.Document,expected:Any,semantic_role:str,candidate_page:int|None=None)->tuple[int,list[float],str]|None:
    text=str(expected or "").strip()
    if not text:return None
    cache_key=(text,semantic_role,candidate_page)
    if cache_key in _IDENTITY_CACHE:return _IDENTITY_CACHE[cache_key]
    matches=[]
    for page_index,page in enumerate(doc):
        # MuPDF's native phrase search is dramatically cheaper than building a
        # Python word index for every page of a paper, and it already handles
        # cross-line phrases.  Context scoring below still resolves repeated
        # abbreviations deterministically.
        for hit in native_phrase_rects(page,text):
            context_rect=fitz.Rect(max(0,hit.x0-120),max(0,hit.y0-45),min(page.rect.width,hit.x1+180),min(page.rect.height,hit.y1+45))
            context=cached_context_text(page,context_rect)
            terms=set(re.findall(r"[a-z]+",context.casefold()))
            if semantic_role=="material_identity":
                score=3*len(terms&MATERIAL_CONTEXT)-(2 if page_index==0 else 0)-(2 if page_index>=max(1,len(doc)-2) else 0)
            else:
                score=(8 if candidate_page==page_index+1 else 0)+3*len(terms&{"mix","mixture","sample","specimen","ratio","table"})
            matches.append((score,page_index,float(hit.y0),float(hit.x0),hit))
    if not matches:
        _IDENTITY_CACHE[cache_key]=None;return None
    matches.sort(key=lambda item:(-item[0],item[1],item[2],item[3]))
    best=matches[0]
    if semantic_role=="material_identity" and best[0]<=0:
        _IDENTITY_CACHE[cache_key]=None;return None
    # ``page.get_textbox`` may return fragments from adjacent columns even for
    # a glyph-tight search rectangle.  The search hit itself is the direct PDF
    # token; publishing the whole clipped extraction makes an otherwise exact
    # identity look like a prose inference and produces noisy reviewer text.
    # Keep the bbox for independent visual/raw-substring verification, but
    # publish only the exact source phrase that PyMuPDF matched.
    # Native search is case-insensitive and may return substring matches (for
    # example ``SS`` inside ``pressure``).  Walk the ranked hits only until the
    # first literal PDF match is found; this preserves exactness without
    # repeating textbox extraction for every hit in the document.
    for ranked in matches:
        if semantic_role=="material_identity" and ranked[0]<=0:
            break
        page=doc[ranked[1]];bbox=projected_bbox(ranked[4],page)
        clip=cached_bbox_text(page,bbox)
        source_phrase=literal_source_phrase(clip,text)
        if source_phrase is None:
            continue
        result=(ranked[1]+1,bbox,source_phrase)
        _IDENTITY_CACHE[cache_key]=result;return result
    _IDENTITY_CACHE[cache_key]=None
    return None

def curing_context_bbox(doc:fitz.Document,field_value:dict[str,Any])->tuple[int,list[float],str]|None:
    value_text=str(field_value.get("originalValue") or field_value.get("value") or "")
    if value_text in _CURING_CACHE:return _CURING_CACHE[value_text]
    expected_numbers=set(re.findall(r"(?<![A-Za-z])\d+(?:\.\d+)?",value_text.replace(",","")))
    expected_terms=set(re.findall(r"[a-z]+",value_text.casefold()))&(CURING_CONTEXT|{"chamber","wrapped","oven","room","temperature","humidity","co2","film"})
    candidates=[]
    for page_index,page in enumerate(doc):
        for block in cached_page_blocks(page):
            text=str(block[4] or "").strip()
            if not text:continue
            terms=set(re.findall(r"[a-z]+",text.casefold()))
            numbers=set(re.findall(r"(?<![A-Za-z])\d+(?:\.\d+)?",text.replace(",","")))
            context_hits=len(terms&(CURING_CONTEXT|{"chamber","wrapped","oven","temperature","humidity","co2","film"}))
            number_hits=len(expected_numbers&numbers)
            semantic_hits=len(expected_terms&terms)
            if context_hits<1 or number_hits<1:continue
            score=8*context_hits+4*number_hits+semantic_hits-(2 if page_index==0 else 0)
            candidates.append((score,page_index,float(block[1]),float(block[0]),fitz.Rect(block[:4]),text))
    if not candidates:
        _CURING_CACHE[value_text]=None;return None
    candidates.sort(key=lambda item:(-item[0],item[1],item[2],item[3]));best=candidates[0]
    if len(candidates)>1 and candidates[1][0]==best[0] and candidates[1][1:4]!=best[1:4]:
        _CURING_CACHE[value_text]=None;return None
    page=doc[best[1]];bbox=projected_bbox(best[4],page)
    # Publish the exact text returned by the final rectangle, not the parser's
    # earlier block string; PDF extraction can differ at ligatures/line ends.
    source_token=cached_bbox_text(page,bbox).strip()
    result=(best[1]+1,bbox,source_token);_CURING_CACHE[value_text]=result;return result

def verified_curing_source_bbox(page:fitz.Page,bbox:list[float],field_value:dict[str,Any])->tuple[list[float],str]|None:
    rect=fitz.Rect([float(value) for value in bbox])
    if rect.is_empty or rect.x0<0 or rect.y0<0 or rect.x1>page.rect.width or rect.y1>page.rect.height:return None
    source_text=cached_bbox_text(page,rect).strip()
    terms=set(re.findall(r"[a-z]+",source_text.casefold()))
    if len(terms&(CURING_CONTEXT|{"room","chamber","oven","temperature","humidity"}))<2:return None
    expected_numbers=set(re.findall(r"(?<![A-Za-z])\d+(?:\.\d+)?",str(field_value.get("originalValue") or field_value.get("value") or "").replace(",","")))
    source_numbers=set(re.findall(r"(?<![A-Za-z])\d+(?:\.\d+)?",source_text.replace(",","")))
    if expected_numbers and not expected_numbers<=source_numbers:return None
    return [round(rect.x0,3),round(rect.y0,3),round(rect.x1,3),round(rect.y1,3)],source_text

def locator_contains_token(page:fitz.Page,bbox:list[float],source_token:Any)->bool:
    rect=fitz.Rect([float(value) for value in bbox])
    if rect.is_empty or rect.x0<0 or rect.y0<0 or rect.x1>page.rect.width or rect.y1>page.rect.height:return False
    source=str(source_token or "")
    if not source.strip():return False
    # Scalar and identifier locators point to one PDF word. Checking the
    # already-indexed word list avoids rebuilding and scanning the full text
    # page once per field. Multi-word semantic evidence keeps the exact-text
    # path so source snippets remain byte-for-byte stable.
    single=token(source)
    if single is not None:
        indexed=cached_page_tokens(page).get(normalized_token(single).casefold())
        candidates=indexed if indexed is not None else cached_page_words(page)
        for word in candidates:
            center=fitz.Point((float(word[0])+float(word[2]))/2,(float(word[1])+float(word[3]))/2)
            if rect.contains(center) and same_source_token(word[4],single):return True
        # Some PDFs split a visual identifier across adjacent internal words,
        # even though get_textbox returns the original combined token.
        return source in cached_bbox_text(page,rect)
    return source in cached_bbox_text(page,rect)

def verified_source_identity_bbox(page:fitz.Page,bbox:list[float],expected:Any)->tuple[list[float],str]|None:
    rect=fitz.Rect([float(value) for value in bbox])
    if rect.is_empty or rect.x0<0 or rect.y0<0 or rect.x1>page.rect.width or rect.y1>page.rect.height:return None
    expected_text=str(expected or "").strip()
    if not expected_text:return None
    return complete_word_phrase_bbox(page,rect,expected_text)


def wrapped_identity_parts(page,bbox,expected):
    """Locate consecutive source words, retaining a separate box per PDF line."""
    import unicodedata
    norm=lambda s:' '.join(unicodedata.normalize('NFKC',str(s)).split())
    region=fitz.Rect(bbox)
    words=sorted([w for w in cached_page_words(page) if region.contains(fitz.Point((w[0]+w[2])/2,(w[1]+w[3])/2))],key=lambda w:(w[5],w[6],w[7]))
    target=norm(expected).split();tokens=[norm(w[4]) for w in words]
    starts=[i for i in range(len(words)-len(target)+1) if tokens[i:i+len(target)]==target]
    if not target or len(starts)!=1:return None
    selected=words[starts[0]:starts[0]+len(target)];groups=[]
    for word in selected:
        if not groups or groups[-1][0][5:7]!=word[5:7]:groups.append([])
        groups[-1].append(word)
    if len(groups)<2:return None
    parts=[]
    for group in groups:
        rect=fitz.Rect(group[0][:4])
        for word in group[1:]:rect|=fitz.Rect(word[:4])
        expected_part=norm(' '.join(w[4] for w in group))
        for inset in (.3,.2,.1,0):
            narrow=fitz.Rect(rect.x0,rect.y0+rect.height*inset,rect.x1,rect.y1-rect.height*inset)
            raw=cached_bbox_text(page,narrow).strip()
            if norm(raw)==expected_part:break
        else:return None
        parts.append((page.number+1,list(narrow),raw))
    return parts if norm(' '.join(p[2] for p in parts))==norm(expected) else None

def publish_deterministic_locator(page:fitz.Page,pages:dict[int,list[dict[str,Any]]],card:dict[str,Any],field_value:dict[str,Any],number:int,bbox:list[float],source_token:str,mode:str,source_key:str|None=None,field_path:str|None=None)->None:
    if not locator_contains_token(page,bbox,source_token):
        raise ValueError(f"locator token is not inside bbox: {card.get('key')} {field_value.get('label')}")
    projected_key="ui-auto-ev-"+hashlib.sha256(f"{card['key']}|{field_value['label']}|{number}|{bbox}|{source_token}".encode()).hexdigest()[:24]
    field_value.update({"evidenceKey":projected_key,"confidence":0.99,"originalValue":source_token,"evidenceBlocked":False,"status":"reported","statusLabel":STATUS_LABELS["reported"],"reason":None})
    stamp_source_explanation(field_value)
    pages.setdefault(number,[]).append({"evidenceKey":projected_key,"sourceEvidenceKey":source_key,"recordKey":card["key"],"fieldPath":field_path,"page":number,"bbox":bbox,"snippet":source_token,"locator":f"Page {number} · 自动核验定位","evidenceMode":mode,"sourceToken":source_token})

def publish_contextual_component_locator(doc:fitz.Document,pages:dict[int,list[dict[str,Any]]],card:dict[str,Any],field_value:dict[str,Any],located:tuple[int,list[float],str,str],source_key:str|None=None,field_path:str|None=None)->None:
    number,bbox,source_token,mapping_rule=located
    publish_deterministic_locator(doc[number-1],pages,card,field_value,number,bbox,source_token,"deterministic-contextual-component",source_key,field_path)
    field_value.update({"sourceFormula":None,"transformation":None,"evidenceMapping":mapping_rule,"_identityDerived":False})
    stamp_source_explanation(field_value)

def publish_composite_specimen_locator(doc:fitz.Document,pages:dict[int,list[dict[str,Any]]],card:dict[str,Any],field_value:dict[str,Any],located:tuple[list[tuple[int,list[float],str]],str],source_key:str|None=None,field_path:str|None=None,kind:str="specimen")->None:
    parts,formula=located
    projected_key="ui-composite-ev-"+hashlib.sha256(f"{card['key']}|{field_value['label']}|{parts}".encode()).hexdigest()[:24]
    field_value.update({"evidenceKey":projected_key,"confidence":0.99,"originalValue":" + ".join(part[2] for part in parts),"evidenceBlocked":False,"status":"reported","statusLabel":STATUS_LABELS["reported"],"reason":None,"sourceFormula":formula,"transformation":{"formula":formula,"formal":True}})
    stamp_source_explanation(field_value,{"formula":formula,"extraction_method":"identity-from-cited-formulation-row"})
    for part_index,(number,bbox,source_token) in enumerate(parts,1):
        if not locator_contains_token(doc[number-1],bbox,source_token):raise ValueError(f"composite locator token is not inside bbox: {card.get('key')} part {part_index}")
        description="跨行材料名称" if kind=="wrapped-material" else "材料名称与缩写定义" if kind=="material" else "复合试样编号来源"
        pages.setdefault(number,[]).append({"evidenceKey":projected_key,"sourceEvidenceKey":source_key,"recordKey":card["key"],"fieldPath":field_path,"page":number,"bbox":bbox,"snippet":source_token,"locator":f"Page {number} · {description} {part_index}/{len(parts)}","evidenceMode":f"deterministic-composite-{kind}","sourceToken":source_token,"partIndex":part_index,"partCount":len(parts)})


def material_identity_locator_parts(receipt,card,field_value,pdf_sha256,sources):
    path=PROJECT_SCRIPT_DIR.parent/"fixtures/material_identity_acceptance_v1.json"
    if sha(path)!=material_identity.ACCEPTANCE_SHA256 or receipt.get("acceptance_sha256")!=material_identity.ACCEPTANCE_SHA256:
        raise ValueError("material review acceptance mismatch")
    matches=[r for r in load(path)["specifications"] if r["record_key"]==card["key"] and r["pdf_sha256"]==pdf_sha256]
    if len(matches)!=1 or matches[0]["material_type"]!=field_value.get("value") or field_value.get("label")!="材料类型":
        raise ValueError("material review value or owner mismatch")
    expected=matches[0]["source_parts"]+matches[0]["definition_parts"]+matches[0]["local_identity_parts"]
    parts=receipt.get("source_parts") or []
    if [{k:p[k] for k in ("page","bbox","snippet")} for p in parts]!=expected:
        raise ValueError("material review source parts mismatch")
    for part in parts:
        source=sources.get(part["evidence_key"],{})
        if source.get("record_key")!=card["key"] or source.get("field_path")!="/material_type" or any(source.get(k)!=part[k] for k in ("page","bbox","snippet")):
            raise ValueError("material review canonical evidence mismatch")
    return [(p["page"],p["bbox"],p["snippet"]) for p in parts]

def quarantine_derived_identity(field_value:dict[str,Any])->None:
    field_value.update({"evidenceKey":None,"confidence":None,"evidenceBlocked":False,"status":"system_note","statusLabel":"系统内部标识","reason":"该标识由多个表格单元组合，仅保留用于记录导航，不作为论文报告的 custom_test_id。"})
    stamp_source_explanation(field_value)

def contextual_curing_bbox(page:fitz.Page,expected:str,context:Any,label:str|None=None)->tuple[list[float],str]|None:
    if label!="养护条件":return None
    words=cached_page_words(page)
    context_terms=set(re.findall(r"[a-z]+",str(context or "").casefold()))&CURING_CONTEXT
    if len(context_terms)<2:return None
    candidates=[]
    for index,word in enumerate(words):
        if not same_source_token(word[4],expected):continue
        block=int(word[5]) if len(word)>5 else -1
        nearby=words[max(0,index-32):index+33]
        local_terms={normalized_token(other[4]).casefold().strip(".") for other in nearby if len(other)>5 and int(other[5])==block}
        score=len(context_terms&local_terms)
        candidates.append((score,index,word))
    if not candidates:return None
    best_score=max(item[0] for item in candidates);best=[item for item in candidates if item[0]==best_score]
    if best_score<2 or len(best)!=1:return None
    word=best[0][2];pad=0.8
    bbox=[round(max(0.0,float(word[0])-pad),3),round(max(0.0,float(word[1])-pad),3),round(min(float(page.rect.width),float(word[2])+pad),3),round(min(float(page.rect.height),float(word[3])+pad),3)]
    return bbox,str(word[4])

def table_row_context_bbox(page:fitz.Page,bbox:list[float],expected:Any)->tuple[list[float],str]|None:
    """Validate and return one complete, possibly wrapped, table-cell row.

    A composite text cell is a direct source value even when PDF word order
    differs by thousandths of a point or the final words wrap to a second
    baseline.  Match the declared cell rectangle against word geometry and a
    token multiset instead of asking an Agent to repair ordinary layout noise.
    """
    source_rect=fitz.Rect([float(value) for value in bbox]);words=[]
    for word in cached_page_words(page):
        center_x=(float(word[0])+float(word[2]))/2
        if not source_rect.x0-0.2<=center_x<=source_rect.x1+0.2:continue
        if float(word[1])<source_rect.y0-0.2 or float(word[3])>source_rect.y1+0.2:continue
        words.append(word)
    if not words:return None
    def terms(value:Any)->list[str]:
        return re.findall(r"[a-z0-9]+|%",normalized_source_text(value))
    from collections import Counter
    expected_terms=Counter(terms(expected));observed_terms=Counter(term for word in words for term in terms(word[4]))
    if not expected_terms or any(observed_terms[term]<count for term,count in expected_terms.items()):return None
    lines:dict[tuple[int,int],list[tuple[Any,...]]]={}
    for word in words:
        lines.setdefault((int(word[5]),int(word[6])),[]).append(word)
    ordered_lines=sorted(lines.values(),key=lambda row:(min(float(word[1]) for word in row),min(float(word[0]) for word in row)))
    source_token="\n".join(" ".join(str(word[4]) for word in sorted(row,key=lambda item:float(item[0]))) for row in ordered_lines)
    tight=[
        round(min(float(word[0]) for word in words),3),round(min(float(word[1]) for word in words),3),
        round(max(float(word[2]) for word in words),3),round(max(float(word[3]) for word in words),3),
    ]
    if source_token not in page.get_textbox(fitz.Rect(tight)):return None
    return tight,source_token

def block_field(field_value:dict[str,Any],source:dict[str,Any]|None=None)->None:
    source=source or {}
    field_value.update({"evidenceKey":None,"confidence":None,"evidenceBlocked":True,"status":"evidence_blocked","statusLabel":STATUS_LABELS.get("evidence_blocked","证据已隔离，待修复"),"reason":"旧证据无法可靠缩小到当前字段的单个原文值；已隔离，待字段级定位修复。","repairSourceEvidenceKey":source.get("evidence_key"),"repairSourceLocator":{"page":source.get("page"),"bbox":source.get("bbox"),"table":source.get("table_number"),"figure":source.get("figure_number"),"section":source.get("section"),"snippet":source.get("snippet"),"fieldPath":source.get("field_path")}})
    stamp_source_explanation(field_value)

def repair_request_id(run_id:str,record_key:Any,field_label:Any,value:Any)->str:
    identity=f"{run_id}|{record_key}|{field_label}|{value}"
    return "repair-"+hashlib.sha256(identity.encode()).hexdigest()[:24]

def normalized_source_text(value:Any)->str:
    normalized=str(value or "").replace("−","-").replace("–","-").strip().casefold()
    normalized=re.sub(r"(?<=\w)-\s+(?=\w)","-",normalized)
    return re.sub(r"\s+"," ",normalized)

def apply_agent_locator(doc:fitz.Document,run_id:str,pdf_sha256:str,card:dict[str,Any],field_value:dict[str,Any],responses:dict[str,dict[str,Any]],pages:dict[int,list[dict[str,Any]]])->bool:
    request_id=repair_request_id(run_id,card["key"],field_value["label"],field_value.get("value"));response=responses.get(request_id)
    if response is None:return False
    if response.get("requestId")!=request_id or response.get("runId")!=run_id or response.get("recordKey")!=card["key"] or response.get("fieldLabel")!=field_value["label"] or response.get("pdfSha256")!=pdf_sha256:raise ValueError(f"agent evidence identity mismatch: {request_id}")
    verdict=response.get("verdict")
    if verdict=="REJECT":
        field_value.update({"evidenceKey":None,"confidence":None,"evidenceBlocked":True,"status":"evidence_blocked","reason":f"自动语义证据 Agent 拒绝该来源声明：{response.get('reason') or 'UNSPECIFIED'}","agentVerdict":"REJECT","agentReason":response.get("reason") or "UNSPECIFIED"})
        stamp_source_explanation(field_value)
        return True
    if verdict!="LOCATED":raise ValueError(f"invalid agent evidence verdict: {request_id}")
    page_number=response.get("page");bbox=response.get("bbox");source_token=str(response.get("sourceToken") or "")
    if not isinstance(page_number,int) or not 1<=page_number<=len(doc) or not isinstance(bbox,list) or len(bbox)!=4 or not source_token:raise ValueError(f"incomplete agent locator: {request_id}")
    values=[float(v) for v in bbox];rect=fitz.Rect(values)
    if rect.is_empty or rect.x0<0 or rect.y0<0 or rect.x1>doc[page_number-1].rect.width or rect.y1>doc[page_number-1].rect.height:raise ValueError(f"out-of-bounds agent locator: {request_id}")
    clipped=doc[page_number-1].get_textbox(rect)
    if source_token not in clipped:raise ValueError(f"agent token not inside bbox: {request_id}")
    projected_key="ui-agent-ev-"+hashlib.sha256(f"{request_id}|{page_number}|{values}|{source_token}".encode()).hexdigest()[:24]
    field_value.update({"evidenceKey":projected_key,"confidence":response.get("confidence",0.95),"originalValue":source_token,"evidenceBlocked":False,"status":"reported","statusLabel":STATUS_LABELS["reported"],"reason":None})
    stamp_source_explanation(field_value)
    pages.setdefault(page_number,[]).append({"evidenceKey":projected_key,"sourceEvidenceKey":field_value.get("repairSourceEvidenceKey"),"recordKey":card["key"],"fieldPath":response.get("fieldPath"),"page":page_number,"bbox":values,"snippet":source_token,"locator":response.get("locator") or f"Page {page_number} · Agent 已验证定位","evidenceMode":"agent-located","sourceToken":source_token})
    return True

def disambiguate_quantity_labels(cards:list[dict[str,Any]])->None:
    """Keep distinct reported quantities visible without inventing a new value."""
    conflicts=set()
    for card in cards:
        grouped={}
        for item in card['fields']:
            if item.get('semanticRole')=='reported_formulation':grouped.setdefault(item['label'],[]).append(item)
        for label,items in grouped.items():
            units=[str(item.get('unit') or '') for item in items]
            if len(items)>1 and all(units) and len(set(units))==len(items):conflicts.add(label)
    for card in cards:
        for item in card['fields']:
            if item.get('semanticRole')=='reported_formulation' and item['label'] in conflicts:
                item.setdefault('_sourceLabel',item['label'])
                item['label']=f"{item['label']}（{item['unit']}）"


def project_field_evidence(doc:fitz.Document,run_id:str,pdf_sha256:str,cards:dict[str,list[dict[str,Any]]],sources:dict[str,dict[str,Any]],agent_responses:dict[str,dict[str,Any]],external_locators:dict[str,dict[str,Any]]|None=None,curve_index:dict|None=None,sparse_index:dict|None=None,supplement_psd_index:dict|None=None,supplement_marker_index:dict|None=None,main_marker_index:dict|None=None)->dict[int,list[dict[str,Any]]]:
    global _GLOBAL_WORD_INDEX
    _PAGE_WORD_CACHE.clear();_PAGE_BLOCK_CACHE.clear();_PAGE_TOKEN_CACHE.clear();_PAGE_SPAN_CACHE.clear();_BBOX_TEXT_CACHE.clear();_IDENTITY_CACHE.clear();_CURING_CACHE.clear();_GLOBAL_WORD_INDEX=None
    pages:dict[int,list[dict[str,Any]]]={};external_locators=external_locators or {}
    disambiguate_quantity_labels(cards['mixes'])
    for group in ("mats","mixes"):
        for card in cards[group]:
            for field_value in card["fields"]:
                source_label=str(field_value.pop("_sourceLabel",field_value.get("label") or ""))
                canonical_field_path=field_value.pop("_fieldPath",None)
                key=field_value.get("evidenceKey")
                material_receipt=field_value.pop("_materialIdentityReceipt",None)
                if material_receipt is not None:
                    parts=material_identity_locator_parts(material_receipt,card,field_value,pdf_sha256,sources)
                    formula="拼接原文跨行名称，并结合本篇材料声明与缩写定义确定材料类型。"
                    publish_composite_specimen_locator(doc,pages,card,field_value,(parts,formula),str(key),"/material_type",kind="material")
                    for entries in pages.values():
                        for entry in entries:
                            if entry.get("evidenceKey")==field_value["evidenceKey"]:
                                entry["sourceEvidenceKey"]=material_receipt["source_parts"][entry["partIndex"]-1]["evidence_key"]
                    continue
                if not key:
                    # Display-only classifications and computed summaries are
                    # not source claims.  Do not turn them into reported fields
                    # merely because a generic word (for example "binder")
                    # occurs somewhere in the PDF.
                    if field_value.get("status")=="system_note":
                        continue
                    role=str(field_value.get("semanticRole") or "")
                    if role in {"material_identity","source_specimen_id"} and field_value.get("value") not in (None,""):
                        located=global_identity_bbox(doc,field_value.get("value"),role)
                        if located:
                            number,bbox,source_token=located;publish_deterministic_locator(doc[number-1],pages,card,field_value,number,bbox,source_token,"deterministic-global-identity")
                            continue
                        if role=="source_specimen_id":
                            composite=composite_specimen_identity_parts(doc,field_value.get("value"))
                            if composite:
                                publish_composite_specimen_locator(doc,pages,card,field_value,composite);continue
                            quarantine_derived_identity(field_value);continue
                    apply_agent_locator(doc,run_id,pdf_sha256,card,field_value,agent_responses,pages)
                    continue
                source=sources.get(str(key))
                if source is not None and canonical_field_path:source={**source,"field_path":canonical_field_path}
                if (source or {}).get('extraction_method')=='chart_readout':
                    number=source.get('page');box=source.get('bbox')
                    if not number or not box or source.get('record_key')!=card['key']:
                        raise ValueError('native chart source lacks matching owner/page/bbox')
                    if not fitz.Rect(doc[number-1].rect).contains(fitz.Rect(box)):
                        raise ValueError('native chart source exceeds PDF page')
                    pages.setdefault(number,[]).append(dict(evidenceKey=key,recordKey=card['key'],
                        fieldPath=source['field_path'],page=number,bbox=box,coordinateSpace='pdf_points',
                        snippet=source.get('snippet',''),sourceToken=source.get('snippet',''),
                        locator=str(source.get('figure_number') or '图中数据'),evidenceMode='native-chart-readout'))
                    continue
                if (source or {}).get('extensions',{}).get('evidence_kind')=='main_figure_digitization':
                    entry=(main_marker_index or {}).get(str(key))
                    if entry is None:raise ValueError('main marker locator requires independently reopened source proof')
                    field_value['displayValue']=entry['displayValue']
                    locator={'evidenceKey':key,'sourceEvidenceKey':key,'recordKey':card['key'],
                        'fieldPath':source['field_path'],'page':source['page'],'bbox':source['bbox'],
                        'coordinateSpace':'pdf_points','sourceToken':entry['formula'],'snippet':entry['formula'],
                        'locator':f"{source['figure_number']} · 图读数据点",'evidenceMode':main_marker_review.MODE,
                        'mainMarkerSubject':entry['subject']}
                    main_marker_review.verify_locator(main_marker_index,locator,field_value,card['key'],pdf_sha256)
                    pages.setdefault(source['page'],[]).append(locator)
                    continue
                if (source or {}).get('extensions',{}).get('evidence_kind')=='reviewed_sparse_geometry':
                    entry=(sparse_index or {}).get(str(key))
                    if entry is None:raise ValueError('sparse locator requires independently reopened source proof')
                    field_value['value']=str(entry['value'])
                    field_value.update(sparse_review.display_metadata(entry))
                    locator={'evidenceKey':key,'sourceEvidenceKey':key,'recordKey':card['key'],
                        'fieldPath':source['field_path'],'page':source['page'],'bbox':source['bbox'],
                        'snippet':source['snippet'],'sourceToken':source['snippet'],
                        'locator':f"{source['figure_number']} · 图读数据点",'evidenceMode':'reviewed-sparse-geometry',
                        'sparseSubject':entry['subject']}
                    sparse_review.verify_locator(sparse_index,locator,field_value,card['key'],pdf_sha256)
                    pages.setdefault(source['page'],[]).append(locator)
                    continue
                if (source or {}).get("extensions",{}).get("evidence_kind") in {"reviewed_curve_geometry","reviewed_raster_curve_geometry"}:
                    entry=(curve_index or {}).get(str(key))
                    if entry is None:raise ValueError("curve locator requires independently reopened source proof")
                    locator={"evidenceKey":key,"sourceEvidenceKey":key,"canonicalEvidenceKey":key,"recordKey":card["key"],
                             "fieldPath":source["field_path"],"page":source["page"],"bbox":source["bbox"],
                             "snippet":source["snippet"],"sourceToken":source["snippet"],"locator":f"{source['figure_number']} · 曲线坐标来源",
                             "evidenceMode":"reviewed-curve-geometry","curveSubject":entry["subject"]}
                    curve_review.verify_locator(curve_index,locator,field_value,card["key"],pdf_sha256)
                    pages.setdefault(source["page"],[]).append(locator)
                    continue
                external=external_locators.get(str(key))
                if (source or {}).get('extraction_method') == 'source-component-marker-v2':
                    entry=(supplement_marker_index or {}).get(str(key))
                    if entry is None or external is None:
                        raise ValueError('supplement marker requires independent source proof and source image page')
                    field_value['displayValue']=entry['displayValue']
                    locator={k:v for k,v in external.items() if k!='_sourcePageMeta'}
                    locator.update({'evidenceKey':key,'sourceEvidenceKey':key,'recordKey':card['key'],
                        'fieldPath':source['field_path'],'sourcePage':1,'coordinateSpace':'image_pixels',
                        'sourceToken':entry['formula'],'snippet':entry['formula'],
                        'evidenceMode':supplement_marker_review.MODE,'supplementMarkerSubject':entry['subject']})
                    supplement_marker_review.verify_locator(supplement_marker_index,locator,field_value,
                        card['key'],pdf_sha256,external.get('_sourcePageMeta') or {})
                    pages.setdefault(int(locator['page']),[]).append(locator)
                    continue
                if (source or {}).get('extraction_method') == 'masked-source-pixel-intersections-v2':
                    entry=(supplement_psd_index or {}).get(str(key))
                    if entry is None or external is None:
                        raise ValueError('supplement PSD requires independent source proof and source image page')
                    field_value['displayValue']=f"≈ {entry['value']:.3f}"
                    if entry['label']=='D50':field_value['curveKey']=entry['curve']['pointsAssetKey']
                    locator={k:v for k,v in external.items() if k!='_sourcePageMeta'}
                    locator.update({'evidenceKey':key,'sourceEvidenceKey':key,'recordKey':card['key'],
                        'fieldPath':source['field_path'],'sourcePage':1,'coordinateSpace':'image_pixels',
                        'evidenceMode':supplement_psd_review.MODE,'supplementPsdSubject':entry['subject']})
                    supplement_psd_review.verify_locator(supplement_psd_index,locator,field_value,
                        card['key'],pdf_sha256,external.get('_sourcePageMeta') or {})
                    pages.setdefault(int(locator['page']),[]).append(locator)
                    continue
                if external is not None:
                    projected_key="ui-supp-ev-"+hashlib.sha256(f"{card['key']}|{field_value['label']}|{key}".encode()).hexdigest()[:24]
                    field_value.update({"evidenceKey":projected_key,"evidenceBlocked":False,"status":"reported","statusLabel":STATUS_LABELS["reported"],"originalValue":external["sourceToken"]})
                    stamp_source_explanation(field_value)
                    pages.setdefault(int(external["page"]),[]).append({"evidenceKey":projected_key,"sourceEvidenceKey":key,"recordKey":card["key"],"fieldPath":(source or {}).get("field_path"),**{k:v for k,v in external.items() if k!='_sourcePageMeta'}})
                    continue
                number=int((source or {}).get("page") or 0);bbox=(source or {}).get("bbox")
                if not source or number<1 or number>len(doc) or not isinstance(bbox,list) or len(bbox)!=4:
                    block_field(field_value,source);continue
                if field_value.get('semanticRole')=='performance_method':
                    method_parts=field_value.pop('_methodSourceParts',None)
                    if method_parts:
                        normalized_method=lambda value:re.sub(r'\s+',' ',str(value or '')).strip()
                        expected=str(field_value.get('value') or '')
                        if normalized_method(' '.join(part['snippet'] for part in method_parts))!=normalized_method(expected):
                            raise ValueError('wrapped performance method does not reconstruct source value')
                        staged=[]
                        for index,part in enumerate(method_parts,1):
                            canonical=sources.get(str(part.get('evidence_key')))
                            if (not canonical or canonical.get('field_path')!=canonical_field_path
                                    or canonical.get('record_key')!=card['key']
                                    or canonical.get('paper_key')!=source.get('paper_key')
                                    or canonical.get('asset_key')!=source.get('asset_key')
                                    or canonical.get('evidence_key')!=part.get('evidence_key')
                                    or any(canonical.get(k)!=part.get(k) for k in ('page','bbox','snippet'))):
                                raise ValueError('wrapped performance method source identity mismatch')
                            pn=part['page'];box=part['bbox'];method_token=part['snippet']
                            if (not isinstance(pn,int) or not 1<=pn<=len(doc)
                                    or not locator_contains_token(doc[pn-1],box,method_token)):
                                raise ValueError('wrapped performance method token outside source box')
                            staged.append((pn,{'evidenceKey':key,'sourceEvidenceKey':part['evidence_key'],
                                'recordKey':card['key'],'fieldPath':canonical_field_path,'page':pn,'bbox':box,
                                'snippet':method_token,'sourceToken':method_token,'partIndex':index,'partCount':len(method_parts),
                                'locator':f'Page {pn} · 试验方法 {index}/{len(method_parts)}',
                                'evidenceMode':'deterministic-wrapped-method'}))
                        for pn,entry in staged:pages.setdefault(pn,[]).append(entry)
                        field_value['originalValue']=expected
                        field_value['evidenceBlocked']=False
                        continue
                    # Standard identifiers are text, not numeric measurements.
                    # Keep exact PDF whitespace so wrapped citations also pass
                    # the same source-token verifier as every other field.
                    expected=str(field_value.get('originalValue') or field_value.get('value') or '')
                    readback=doc[number-1].get_textbox(fitz.Rect(bbox)).strip()
                    if re.sub(r'\s+',' ',expected).strip() not in re.sub(r'\s+',' ',readback).strip():
                        raise ValueError('performance method source text mismatch')
                    publish_deterministic_locator(doc[number-1],pages,card,field_value,number,bbox,readback,
                        'deterministic-method-text',str(key),source.get('field_path'))
                    field_value['originalValue']=expected
                    continue
                if field_value.get('semanticRole')=='material_identity':
                    parts=wrapped_identity_parts(doc[number-1],bbox,field_value.get('originalValue') or field_value.get('value'))
                    if parts:
                        publish_composite_specimen_locator(doc,pages,card,field_value,
                            (parts,'按原文阅读顺序拼接跨行名称；仅展开字体连字，不改变材料含义。'),
                            str(key),source.get('field_path'),kind='wrapped-material')
                        continue
                if field_value.get("semanticRole")=="source_specimen_id":
                    verified_identity=verified_source_identity_bbox(doc[number-1],bbox,field_value.get("originalValue") or field_value.get("value"))
                    if verified_identity:
                        identity_bbox,source_token=verified_identity;publish_deterministic_locator(doc[number-1],pages,card,field_value,number,identity_bbox,source_token,"deterministic-source-identity",str(key),source.get("field_path"));continue
                if field_value.get("semanticRole")=="curing_condition":
                    contextual=verified_curing_source_bbox(doc[number-1],bbox,field_value)
                    if contextual:
                        context_bbox,source_token=contextual;publish_deterministic_locator(doc[number-1],pages,card,field_value,number,context_bbox,source_token,"deterministic-curing-context",str(key),source.get("field_path"));continue
                    located=curing_context_bbox(doc,field_value)
                    if located:
                        located_page,located_bbox,source_token=located;publish_deterministic_locator(doc[located_page-1],pages,card,field_value,located_page,located_bbox,source_token,"deterministic-curing-context",str(key),source.get("field_path"));continue
                    block_field(field_value,source);continue
                if field_value.get("_identityDerived") and field_value.get("semanticRole")=="reported_formulation":
                    specimen_field=next((item for item in card.get("fields") or [] if item.get("semanticRole")=="source_specimen_id"),{})
                    direct_component=direct_formulation_component_bbox(doc,specimen_field.get("value"),source_label,field_value.get("value"))
                    if direct_component:
                        publish_contextual_component_locator(doc,pages,card,field_value,direct_component,str(key),source.get("field_path"));continue
                if source_label=="Mixture Type" and field_value.get("semanticRole")=="reported_formulation":
                    table_row=table_row_context_bbox(doc[number-1],[float(v) for v in bbox],field_value.get("originalValue") or field_value.get("value"))
                    if table_row:
                        row_bbox,source_token=table_row
                        publish_deterministic_locator(doc[number-1],pages,card,field_value,number,row_bbox,source_token,"deterministic-table-row-context",str(key),source.get("field_path"));continue
                mode="computed" if field_value.get("transformation") or field_value.get("sourceFormula") else "direct"
                # A computed canonical value is not a verbatim PDF token. Its
                # locator must anchor the reported original value and retain a
                # formula; direct fields continue to prefer the displayed
                # scalar and then the recorded original token.
                expected=(token(field_value.get("originalValue")) or numeric_token(field_value.get("originalValue")) or numeric_token(field_value.get("value"))) if mode=="computed" else (numeric_token(field_value.get("value")) or token(field_value.get("originalValue")) or token(field_value.get("value")))
                precise=exact_word_bbox(doc[number-1],[float(v) for v in bbox],expected,source_label) if expected else None
                if precise is None:
                    search_value=expected or field_value.get("originalValue") or field_value.get("value")
                    if numeric_token(search_value) is None:
                        precise=exact_text_bbox(doc[number-1],[float(v) for v in bbox],search_value)
                if precise is None and expected and not same_value(field_value.get("value"),0):
                    precise=expanded_context_numeric_bbox(doc[number-1],[float(v) for v in bbox],expected,source_label)
                if precise is None and expected and not same_value(field_value.get("value"),0):
                    located_numeric=global_context_numeric_bbox(doc,expected,source_label,card.get("key"))
                    if located_numeric:
                        numeric_page,numeric_bbox,numeric_source=located_numeric;publish_deterministic_locator(doc[numeric_page-1],pages,card,field_value,numeric_page,numeric_bbox,numeric_source,"deterministic-record-context",str(key),source.get("field_path"));continue
                if precise is None and source_label=="factor_value":
                    specimen_field=next((item for item in card.get("fields") or [] if item.get("semanticRole")=="source_specimen_id"),{})
                    located_family=global_specimen_family_numeric_bbox(doc,expected,specimen_field.get("value"))
                    if located_family:
                        family_page,family_bbox,family_source=located_family;publish_deterministic_locator(doc[family_page-1],pages,card,field_value,family_page,family_bbox,family_source,"deterministic-specimen-family-context",str(key),source.get("field_path"));continue
                if precise is None and mode=="computed" and token(field_value.get("originalValue")) and numeric_token(field_value.get("originalValue")) is None:
                    located_composite=global_identity_bbox(doc,field_value.get("originalValue"),"source_specimen_id",number)
                    if located_composite:
                        composite_page,composite_bbox,composite_source=located_composite;publish_deterministic_locator(doc[composite_page-1],pages,card,field_value,composite_page,composite_bbox,composite_source,"deterministic-composite-identity",str(key),source.get("field_path"));continue
                if precise is None and field_value.get("semanticRole")=="performance_age":
                    precise=reported_age_phrase_bbox(doc[number-1],[float(v) for v in bbox],field_value.get("originalValue"),field_value.get("originalUnit"))
                    if precise is None:
                        located_age=global_reported_age_phrase_bbox(doc,field_value.get("originalValue"),number)
                        if located_age:
                            age_page,age_bbox,age_token=located_age;publish_deterministic_locator(doc[age_page-1],pages,card,field_value,age_page,age_bbox,age_token,"deterministic-age-context",str(key),source.get("field_path"));continue
                if precise is None and field_value.get("semanticRole")=="source_specimen_id":
                    search_value=field_value.get("originalValue") or field_value.get("value")
                    precise=exact_text_bbox(doc[number-1],[0.0,0.0,float(doc[number-1].rect.width),float(doc[number-1].rect.height)],search_value)
                    if precise is None:
                        located=global_identity_bbox(doc,field_value.get("value"),"source_specimen_id",number)
                        if located:
                            located_page,located_bbox,source_token=located;publish_deterministic_locator(doc[located_page-1],pages,card,field_value,located_page,located_bbox,source_token,"deterministic-global-identity",str(key),source.get("field_path"));continue
                relocalized=False
                if precise is None:
                    if same_value(field_value.get("value"),0) and field_value.get("semanticRole")=="reported_formulation":
                        reclassify_unreported_zero(field_value);continue
                    if field_value.get("semanticRole")=="source_specimen_id":
                        composite=composite_specimen_identity_parts(doc,field_value.get("value"))
                        if composite:
                            publish_composite_specimen_locator(doc,pages,card,field_value,composite,str(key),source.get("field_path"));continue
                        quarantine_derived_identity(field_value);continue
                    if apply_agent_locator(doc,run_id,pdf_sha256,card,field_value,agent_responses,pages):continue
                    block_field(field_value,source);continue
                precise_bbox,source_token=precise
                if mode=="direct":field_value["originalValue"]=source_token
                projected_key="ui-ev-"+hashlib.sha256(f"{card['key']}|{field_value['label']}|{key}".encode()).hexdigest()[:24]
                field_value["evidenceKey"]=projected_key
                projected={
                    "evidenceKey":projected_key,"sourceEvidenceKey":key,"recordKey":card["key"],"fieldPath":source.get("field_path"),
                    "page":number,"bbox":precise_bbox,"snippet":source.get("snippet") or str(field_value.get("originalValue") or ""),
                    "locator":f"Page {number} · 养护条件" if relocalized else source.get("table_number") or source.get("figure_number") or source.get("section") or f"Page {number}",
                    "evidenceMode":mode,"sourceToken":source_token,
                }
                if not locator_contains_token(doc[number-1],precise_bbox,source_token):
                    raise ValueError(f"projected locator token is not inside bbox: {run_id} {card.get('key')} {field_value.get('label')}")
                pages.setdefault(number,[]).append(projected)
    return pages


def _supplement_table_locators(document:fitz.Document,sources:list[dict[str,Any]],page_offset:int)->dict[str,dict[str,Any]]:
    """Bind DOCX table cells to exact tokens in the hash-bound Word PDF."""
    result:dict[str,dict[str,Any]]={};groups:dict[tuple[str,int],list[dict[str,Any]]]={}
    table_pages:dict[str,int]={};table_regions:dict[str,fitz.Rect]={};labels:set[str]=set()
    for source in sources:
        label=str(source.get("table_number") or "")
        locator=source.get("source_locator") or {}
        if locator.get("coordinate_space")!="docx_table" or not label:continue
        labels.add(label)
        row=int(locator.get("row") or 0);groups.setdefault((label,row),[]).append(source)
    # A rendered supplement can contain several tables on one page and repeat
    # the same scalar in each table.  Bind every label to the vertical section
    # beginning at its caption and ending at the next known table caption.
    by_page:dict[int,list[tuple[str,fitz.Rect]]]={}
    for label in sorted(labels):
        matches=[]
        for index,page in enumerate(document):
            matches.extend((index,hit) for hit in bounded_phrase_rects(page,label))
        if len(matches)==1:
            index,hit=matches[0];table_pages[label]=index;by_page.setdefault(index,[]).append((label,hit))
    for index,items in by_page.items():
        page=document[index];items.sort(key=lambda item:(item[1].y0,item[1].x0,item[0]))
        for ordinal,(label,hit) in enumerate(items):
            bottom=items[ordinal+1][1].y0-0.5 if ordinal+1<len(items) else float(page.rect.height)
            table_regions[label]=fitz.Rect(0.0,max(0.0,hit.y1-0.5),float(page.rect.width),max(hit.y1,bottom))
    for (label,row),items in groups.items():
        page_index=table_pages.get(label)
        if page_index is None:continue
        page=document[page_index];table_region=table_regions[label];ordered=sorted(items,key=lambda item:(999 if (item.get("source_locator") or {}).get("column") is None else int((item.get("source_locator") or {}).get("column")),str(item.get("evidence_key"))))
        anchor=next((item for item in ordered if (item.get("source_locator") or {}).get("column")==0),None)
        anchor_hits=bounded_phrase_rects(page,str((anchor or {}).get("snippet") or ""),table_region) if anchor else []
        anchor_y=(anchor_hits[0].y0+anchor_hits[0].y1)/2 if anchor_hits else None
        used:dict[str,int]={};resolved=[]
        for source in ordered:
            snippet=str(source.get("snippet") or "").strip();column=(source.get("source_locator") or {}).get("column")
            if not snippet or column is None:continue
            row_region=table_region
            if anchor_y is not None:
                row_region=fitz.Rect(table_region.x0,max(table_region.y0,anchor_y-8.0),table_region.x1,min(table_region.y1,anchor_y+8.0))
            hits=bounded_phrase_rects(page,snippet,row_region)
            if anchor_y is not None:
                nearby=[hit for hit in hits if abs((hit.y0+hit.y1)/2-anchor_y)<=16.0]
                if nearby:hits=nearby
            hits=sorted(hits,key=lambda hit:(hit.x0,hit.y0))
            occurrence=used.get(snippet,0);used[snippet]=occurrence+1
            if not hits:continue
            hit=hits[min(occurrence,len(hits)-1)];bbox=projected_bbox(hit,page);resolved.append(bbox)
            result[str(source["evidence_key"])]={"page":page_offset+page_index+1,"bbox":bbox,"snippet":snippet,"locator":f"补充材料 {label} · 第 {row} 行第 {int(column)+1} 列","evidenceMode":"supplement-table-cell","sourceToken":snippet,"sourceKind":"supplement"}
        row_union=None
        if resolved:
            row_union=[min(box[0] for box in resolved),min(box[1] for box in resolved),max(box[2] for box in resolved),max(box[3] for box in resolved)]
        for source in ordered:
            if (source.get("source_locator") or {}).get("column") is None and row_union:
                key=str(source["evidence_key"]);result[key]={"page":page_offset+page_index+1,"bbox":row_union,"snippet":str(source.get("snippet") or ""),"locator":f"补充材料 {label} · 第 {row} 行","evidenceMode":"supplement-table-row","sourceToken":str(source.get("snippet") or ""),"sourceKind":"supplement"}
    return result


def _supplement_caption_locators(document,sources,page_offset):
    locators={}
    normalized=lambda text:re.sub(r'\s+','',str(text)).casefold()
    for source in sources:
        caption=(source.get('extensions') or {}).get('caption_text')
        token=source.get('snippet')
        if not caption or not token or token not in caption:
            raise ValueError('supplement caption lacks original token')
        candidates=[]
        for page in document:
            blocks=[block for block in page.get_text('blocks') if normalized(block[4])]
            wanted=normalized(caption)
            for start in range(len(blocks)):
                joined='';selected=[]
                for block in blocks[start:]:
                    joined+=normalized(block[4]);selected.append(block)
                    if joined==wanted:
                        hits=[hit for hit in page.search_for(token) if any(fitz.Rect(part[:4]).contains(hit) for part in selected)]
                        candidates.extend((page.number+1,list(hit)) for hit in hits)
                        break
                    if not wanted.startswith(joined):break
        if len(candidates)!=1:
            raise ValueError('supplement caption token is missing or ambiguous in rendered source')
        number,bbox=candidates[0]
        locators[source['evidence_key']]={'page':page_offset+number,'bbox':bbox,
            'snippet':token,'sourceToken':token,'sourceKind':'supplement',
            'evidenceMode':'supplement-caption-token','locator':f"补充材料 {source['figure_number']} · 图注"}
    return locators


def materialize_supplement_sources(project:Path,run_id:str,run_assets:Path,precise:dict[str,Any],page_offset:int,scale:float,render_pages:bool)->tuple[list[dict[str,Any]],dict[str,dict[str,Any]]]:
    """Append reviewable supplement pages and bind evidence to real source pixels."""
    assets={str(item.get("asset_key")):item for item in precise.get("assets") or [] if item.get("asset_key")}
    docx_assets=[item for item in assets.values() if item.get("kind")=="supplement"]
    if not docx_assets:return [],{}
    if len(docx_assets)!=1:raise ValueError(f"expected one supplement DOCX: {run_id}")
    docx=docx_assets[0];docx_sha=str(docx.get("sha256") or "")
    render_meta_path=project/"internal_assets"/"supplement-render-cache"/docx_sha/"render.json"
    render_meta=json.loads(render_meta_path.read_text(encoding="utf-8-sig"));supp_pdf=(project/str(render_meta.get("pdf_relative_path") or "")).resolve()
    if project.resolve() not in supp_pdf.parents or not supp_pdf.is_file() or sha(supp_pdf)!=render_meta.get("pdf_sha256") or render_meta.get("source_docx_sha256")!=docx_sha:raise ValueError(f"invalid supplement render cache: {run_id}")
    pages:list[dict[str,Any]]=[];locators:dict[str,dict[str,Any]]={};supp_pages_dir=run_assets/"supplement-pages"
    with fitz.open(supp_pdf) as document:
        caption_sources=[source for source in precise.get('evidence_links') or [] if (source.get('source_locator') or {}).get('coordinate_space')=='docx_caption']
        locators.update(_supplement_caption_locators(document,caption_sources,page_offset))
        table_sources=[source for source in precise.get("evidence_links") or [] if ((source.get("source_locator") or {}).get("coordinate_space")=="docx_table")]
        locators.update(_supplement_table_locators(document,table_sources,page_offset))
        for index,page in enumerate(document):
            number=page_offset+index+1;name=f"page-{index+1:03d}.png"
            pages.append({"number":number,"imageUrl":f"review-assets/{run_id}/supplement-pages/{name}","width":float(page.rect.width),"height":float(page.rect.height),"evidence":[],"sourceKind":"supplement","sourceLabel":"补充材料","sourceArtifactPath":supp_pdf.relative_to(project).as_posix(),"sourceArtifactSha256":str(render_meta["pdf_sha256"]),"sourcePage":index+1})
        chart_sources=[source for source in precise.get("evidence_links") or [] if ((source.get("source_locator") or {}).get("coordinate_space")=="ooxml_chart_cache")]
        for source in chart_sources:
            label=str(source.get("figure_number") or "Fig. S1");matches=[(index,page.search_for(label)) for index,page in enumerate(document) if page.search_for(label)]
            if len(matches)!=1:continue
            index,hits=matches[0];page=document[index];caption=min(hits,key=lambda hit:hit.y0)
            bbox=[24.0,24.0,float(page.rect.width)-24.0,max(30.0,float(caption.y0)-3.0)]
            locators[str(source["evidence_key"])]={"page":page_offset+index+1,"bbox":bbox,"snippet":str(source.get("snippet") or ""),"locator":f"补充材料 {label} · 原生图表数据缓存对应图区","evidenceMode":"supplement-native-chart","sourceToken":str(source.get("snippet") or ""),"sourceKind":"supplement"}
    if render_pages:
        render_pdf_to_cache(project,supp_pdf,str(render_meta["pdf_sha256"]),scale);materialize_cached_pages(project,str(render_meta["pdf_sha256"]),scale,supp_pages_dir)
    next_page=page_offset+len(pages)
    image_spaces={"image_pixels","companion_figure_image_pixels"}
    referenced_figure_keys={str(source.get("asset_key")) for source in precise.get("evidence_links") or [] if ((source.get("source_locator") or {}).get("coordinate_space") in image_spaces)}
    for asset_key in sorted(referenced_figure_keys):
        asset=assets.get(asset_key)
        if not asset or asset.get("kind")!="supplement_figure":continue
        relative=Path(str(asset.get("relative_path") or ""));path=relative if relative.is_absolute() else project/PHASE_C_ROOT/run_id/relative
        if not path.is_file() or sha(path)!=asset.get("sha256"):raise ValueError(f"invalid supplement figure asset: {run_id} {asset_key}")
        pixmap=fitz.Pixmap(str(path));next_page+=1;name=f"figure-{asset_key}.{path.suffix.lstrip('.')}";destination=run_assets/"supplement-figures"/name
        if render_pages:destination.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,destination)
        pages.append({"number":next_page,"imageUrl":f"review-assets/{run_id}/supplement-figures/{name}","width":float(pixmap.width),"height":float(pixmap.height),"evidence":[],"sourceKind":"supplement-figure","sourceLabel":str((asset.get("extensions") or {}).get("figure_number") or "补充材料图"),"sourceArtifactPath":path.resolve().relative_to(project).as_posix(),"sourceArtifactSha256":str(asset.get("sha256")),"sourcePage":1})
        for source in precise.get("evidence_links") or []:
            if str(source.get("asset_key"))!=asset_key:continue
            bbox=source.get("bbox")
            if not isinstance(bbox,list) or len(bbox)!=4:continue
            values=[float(value) for value in bbox]
            if values[0]<0 or values[1]<0 or values[2]>pixmap.width or values[3]>pixmap.height:raise ValueError(f"supplement figure bbox out of bounds: {source.get('evidence_key')}")
            locators[str(source["evidence_key"])]={"page":next_page,"bbox":values,"snippet":str(source.get("snippet") or ""),"locator":f"补充材料 {(asset.get('extensions') or {}).get('figure_number') or ''} · 图中数据区域","evidenceMode":"supplement-figure-region","sourceToken":str(source.get("snippet") or ""),"sourceKind":"supplement"}
            locators[str(source['evidence_key'])]['_sourcePageMeta']=pages[-1]
    return pages,locators

def evidence_repair_requests(run_id:str,pdf_sha256:str,cards:dict[str,list[dict[str,Any]]])->list[dict[str,Any]]:
    requests=[]
    for group in ("mats","mixes"):
        for card in cards[group]:
            for item in card["fields"]:
                if not item.get("evidenceBlocked") or item.get("value") in (None,"") or item.get("agentVerdict")=="REJECT":continue
                locator=item.get("repairSourceLocator") or {}
                requests.append({
                    "requestId":repair_request_id(run_id,card["key"],item["label"],item.get("value")),
                    "runId":run_id,"pdfSha256":pdf_sha256,"recordGroup":group,"recordKey":card["key"],
                    "fieldLabel":item["label"],"claimedValue":item.get("value"),"unit":item.get("unit"),
                    "failureKind":"precise_bbox_not_found" if item.get("repairSourceEvidenceKey") else "source_evidence_missing",
                    "sourceEvidenceKey":item.get("repairSourceEvidenceKey"),"candidateLocator":locator,
                    "allowedOutcomes":["LOCATED","REJECT"],
                    "requiredEvidence":{"page":"1-based integer","bbox":"exact [x0,y0,x1,y1]","sourceToken":"verbatim text inside bbox"},
                })
    return requests

def extraction_repair_defects(run_id:str,pdf_sha256:str,cards:dict[str,list[dict[str,Any]]])->list[dict[str,Any]]:
    defects=[]
    for group in ("mats","mixes"):
        for card in cards[group]:
            for item in card["fields"]:
                if item.get("agentVerdict")=="REJECT":
                    defects.append({"runId":run_id,"pdfSha256":pdf_sha256,"recordGroup":group,"recordKey":card["key"],"fieldLabel":item["label"],"claimedValue":item.get("value"),"failureKind":"agent_rejected_source","reason":item.get("agentReason"),"requiredAction":"repair canonical extraction or quarantine; then rebuild review evidence"})
                elif item.get("status")=="not_extracted":
                    defects.append({"runId":run_id,"pdfSha256":pdf_sha256,"recordGroup":group,"recordKey":card["key"],"fieldLabel":item["label"],"claimedValue":item.get("value"),"failureKind":"field_not_transcribed","reason":item.get("reason"),"requiredAction":"automatic extraction agent must transcribe the located value or reclassify it as source-not-reported; do not route to final human review"})
    return defects

def build_paper(project:Path,assets:Path,run_id:str,scale:float,include_extracted:bool=False,agent_responses:dict[str,dict[str,Any]]|None=None,render_pages:bool=True)->dict[str,Any]:
    run=project/"runs"/safe_run(run_id); manifest=load(run/"manifest.json"); records=load(run/"records.json")
    if manifest.get("stage")!="ACCEPTED" and not (include_extracted and manifest.get("stage")=="EXTRACTED"):raise ValueError(f"run is not reviewable: {run_id}")
    paper=(records.get("papers") or [None])[0]
    if not isinstance(paper,dict):raise ValueError(f"run has no paper: {run_id}")
    pdf=(project/str((manifest.get("input") or {}).get("pdf_path") or "")).resolve()
    if project.resolve() not in pdf.parents or not pdf.is_file():raise ValueError(f"missing or unsafe PDF: {run_id}")
    actual=sha(pdf); expected=str((manifest.get("input") or {}).get("pdf_sha256") or "")
    if actual!=expected:raise ValueError(f"PDF hash mismatch: {run_id}")
    run_assets=assets/run_id; pages_dir=run_assets/"pages"; pages_dir.mkdir(parents=True); shutil.copy2(pdf,run_assets/"paper.pdf")
    precise_path=project/PHASE_C_ROOT/run_id/"generated-records.json";precise=load(precise_path) if precise_path.is_file() else {"mats":[],"mixes":[],"evidence_links":[]}
    # One scientific authority: never graft current source-only evidence onto
    # historical MAT/MIX identities or silently recover values from old runs.
    source_only=precise_path.is_file()
    canonical_source={"recordsPath":precise_path.resolve().relative_to(project.resolve()).as_posix(),"recordsSha256":sha(precise_path)} if source_only else None
    if source_only:
        records=precise
        source_papers=records.get("papers") or []
        if len(source_papers)!=1:raise ValueError("source-only review requires one canonical paper")
        paper=source_papers[0]
    curve_index=curve_review.verified_index(project,curve_review.subject_for(project,precise_path)) if any(precise.get("extensions",{}).get(kind) for kind in ("vector_curve_projection","raster_curve_projection")) else {}
    curve_assets=curve_review.materialize(curve_index,run_assets) if curve_index else {}
    sparse_index=sparse_review.verified_index(project,sparse_review.subject_for(project,precise_path)) if precise.get('extensions',{}).get('sparse_performance_promotion') else {}
    main_marker_index=main_marker_review.verified_index(project,main_marker_review.subject_for(project,precise_path)) if precise.get('extensions',{}).get('main_figure_merge') else {}
    supplement_subject_path=precise_path.parent/'supplement-psd-review-subject.json'
    supplement_psd_index=supplement_psd_review.verified_index(project,load(supplement_subject_path)) if supplement_subject_path.is_file() else {}
    marker_subject_path=precise_path.parent/'supplement-marker-review-subject.json'
    supplement_marker_index=supplement_marker_review.verified_index(project,load(marker_subject_path)) if marker_subject_path.is_file() else {}
    if any(entry['subject']['files']['records']['sha256']!=sha(precise_path) for entry in supplement_marker_index.values()):
        raise ValueError('supplement marker subject belongs to different canonical records')
    if supplement_psd_index and any(entry['subject']['files']['records']['sha256']!=sha(precise_path) for entry in supplement_psd_index.values()):
        raise ValueError('supplement PSD subject belongs to different canonical records')
    if supplement_psd_index:
        psd_assets=supplement_psd_review.materialize(supplement_psd_index,run_assets)
        if set(curve_assets)&set(psd_assets):raise ValueError('duplicate reviewed curve identity')
        curve_assets.update(psd_assets)
    source_links:dict[str,dict[str,Any]]={}
    blocked=BLOCKED_EVIDENCE.get(run_id,set())
    for item in records.get("evidence_links") or []:
        if item.get("evidence_key") in blocked:continue
        if item.get("evidence_key"):source_links[str(item["evidence_key"])]=item
    pmats=precise.get("mats") or [];pmixes=precise.get("mixes") or []
    canonical_mats=records.get("mats") or []
    # Some early accepted runs collapsed several explicitly named source
    # materials into one anonymous MAT shell.  When the source-only rebuild has
    # resolved, non-null MAT identities, review those source-derived MATs
    # directly instead of preserving an extraction-stage null placeholder.
    promote_precise_mats=bool(
        canonical_mats and pmats
        and all(item.get("material_type") is None and item.get("custom_material_id") is None for item in canonical_mats)
        and all(item.get("material_type") is not None and item.get("custom_material_id") is not None for item in pmats)
    )
    material_pairs=[(value,None) for value in canonical_mats] if source_only else ([(value,value) for value in pmats] if promote_precise_mats else pair_review_mats(canonical_mats,pmats))
    mix_pairs=[(value,None) for value in records.get("mixes") or []] if source_only else (pair_review_mixes(records.get("mixes") or [],pmixes) if pmixes else [(value,None) for value in records.get("mixes") or []])
    cards={"mats":[mat_card(canonical,source) for canonical,source in material_pairs],"mixes":[mix_card(canonical,blocked,source) for canonical,source in mix_pairs],"quarantined":[quarantine_card(x) for x in records.get("candidates") or [] if x.get("status")=="quarantined"]}
    if any(r.get('extensions',{}).get('chart_review_fields') for r in records.get('mats',[])+records.get('mixes',[])):
        from native_chart_review import materialize
        curve_assets.update(materialize(project,records,cards,run_assets/'native-curves',f'review-assets/{run_id}/native-curves'))
    pages=[]
    with fitz.open(pdf) as doc:
        supplement_pages,supplement_locators=materialize_supplement_sources(project,run_id,run_assets,precise,len(doc),scale,render_pages)
        links=project_field_evidence(doc,run_id,actual,cards,source_links,agent_responses or {},supplement_locators,curve_index,sparse_index,supplement_psd_index,supplement_marker_index,main_marker_index)
        for index,p in enumerate(doc):
            number=index+1; image=pages_dir/f"page-{number:03d}.png"
            pages.append({"number":number,"imageUrl":f"review-assets/{run_id}/pages/{image.name}","width":float(p.rect.width),"height":float(p.rect.height),"evidence":links.get(number,[]),"sourceKind":"main","sourceArtifactPath":pdf.relative_to(project).as_posix(),"sourceArtifactSha256":actual,"sourcePage":number})
        for item in supplement_pages:
            item["evidence"]=links.get(int(item["number"]),[]);pages.append(item)
    if render_pages:
        render_pdf_to_cache(project,pdf,actual,scale)
        materialize_cached_pages(project,actual,scale,pages_dir)
    newly_blocked=sum(1 for group in ("mats","mixes") for card in cards[group] for item in card["fields"] if item.get("evidenceBlocked"))
    return {"mainMarkerSubject":next(iter(main_marker_index.values()))['subject'] if main_marker_index else None,"canonicalSource":canonical_source,"runId":run_id,"paperKey":paper.get("paper_key"),"title":paper.get("title"),"citation":paper.get("citation"),"doi":paper.get("doi"),"pdfUrl":f"review-assets/{run_id}/paper.pdf","pdfSha256":actual,"pageCount":len(pages),"pages":pages,"extractionStatus":manifest.get("stage"),"independentReviewStatus":"待独立验收","evidenceBlocked":sorted(blocked),"precisionBlockedFieldCount":newly_blocked,"_evidenceRepairRequests":evidence_repair_requests(run_id,actual,cards),"_extractionRepairDefects":extraction_repair_defects(run_id,actual,cards),"records":cards,"curves":curve_assets}

def build_quarantined_paper(project:Path,assets:Path,run_id:str,scale:float)->dict[str,Any]:
    run=project/"runs"/safe_run(run_id); manifest=load(run/"manifest.json")
    if manifest.get("stage")!="QUARANTINED_MAP_ONLY":raise ValueError(f"run is not map-only quarantined: {run_id}")
    pdf=(project/str((manifest.get("input") or {}).get("pdf_path") or "")).resolve()
    if project.resolve() not in pdf.parents or not pdf.is_file():raise ValueError(f"missing or unsafe PDF: {run_id}")
    actual=sha(pdf); expected=str((manifest.get("input") or {}).get("pdf_sha256") or "")
    if actual!=expected:raise ValueError(f"PDF hash mismatch: {run_id}")
    mapping=load(run/"mapping-manifest.json"); inventory=load(run/"entity-inventory.json"); paper_map=load(run/"paper-map.json")
    if mapping.get("input_pdf_sha256")!=actual or inventory.get("pdf_sha256")!=actual or paper_map.get("pdf_sha256")!=actual:raise ValueError(f"map/inventory input mismatch: {run_id}")
    run_assets=assets/run_id;pages_dir=run_assets/"pages";pages_dir.mkdir(parents=True);shutil.copy2(pdf,run_assets/"paper.pdf");pages=[]
    with fitz.open(pdf) as doc:
        for index,p in enumerate(doc):
            number=index+1;image=pages_dir/f"page-{number:03d}.png";pages.append({"number":number,"imageUrl":f"review-assets/{run_id}/pages/{image.name}","width":float(p.rect.width),"height":float(p.rect.height),"evidence":[]})
    render_pdf_to_cache(project,pdf,actual,scale);materialize_cached_pages(project,actual,scale,pages_dir)
    paper=paper_map.get("paper") or {};terminal=manifest.get("terminal_exception") or {};candidate={"candidate_key":f"quarantine-{run_id}","record_key":None,"field_path":None,"formal_value":None,"proposed_value":None,"reason":terminal.get("reason"),"status":"quarantined"}
    return {"runId":run_id,"paperKey":f"paper-{actual[:24]}","title":paper.get("title") or pdf.stem,"citation":None,"doi":paper.get("doi") or "unavailable","pdfUrl":f"review-assets/{run_id}/paper.pdf","pdfSha256":actual,"pageCount":len(pages),"pages":pages,"extractionStatus":"QUARANTINED_MAP_ONLY","records":{"mats":[],"mixes":[],"quarantined":[quarantine_card(candidate)]}}

def build_raw_paper(project:Path,assets:Path,pdf:Path,scale:float)->dict[str,Any]:
    summary=inspect_pdf(pdf);digest=summary["sha256"];run_id=f"test-{digest[:12]}";run_assets=assets/run_id;pages_dir=run_assets/"pages";pages_dir.mkdir(parents=True);shutil.copy2(pdf,run_assets/"paper.pdf");pages=[]
    with fitz.open(pdf) as doc:
        for index,p in enumerate(doc):
            number=index+1;image=pages_dir/f"page-{number:03d}.png";pages.append({"number":number,"imageUrl":f"review-assets/{run_id}/pages/{image.name}","width":float(p.rect.width),"height":float(p.rect.height),"evidence":[]})
    render_pdf_to_cache(project,pdf,digest,scale);materialize_cached_pages(project,digest,scale,pages_dir)
    return {"runId":run_id,"paperKey":f"paper-{digest[:24]}","title":summary.get("title") or pdf.stem,"citation":f"User-supplied test corpus · {pdf.name}","doi":summary.get("doi") or "unavailable","pdfUrl":f"review-assets/{run_id}/paper.pdf","pdfSha256":digest,"pageCount":len(pages),"pages":pages,"extractionStatus":"NOT_EXTRACTED","records":{"mats":[],"mixes":[],"quarantined":[]}}

def main()->int:
    global PHASE_C_ROOT
    started=time.perf_counter()
    parser=argparse.ArgumentParser();parser.add_argument("--project-root",type=Path,default=Path(__file__).resolve().parents[2]);parser.add_argument("--phase-c-root",type=Path,default=None,help="project-relative validated Phase-C artifact root; required when publishing a candidate");parser.add_argument("--run",action="append",default=[]);parser.add_argument("--batch-manifest",type=Path);parser.add_argument("--pdf-dir",action="append",type=Path,default=[]);parser.add_argument("--candidate",type=Path,help="publish the exact agent-reviewed no-render candidate from the page cache");parser.add_argument("--candidate-out",type=Path,help="persist a no-render candidate directly to a project artifact path");parser.add_argument("--include-extracted",action="store_true");parser.add_argument("--preserve-unselected-assets",action="store_true");parser.add_argument("--replace-in-existing",action="store_true");parser.add_argument("--agent-evidence-response",type=Path);parser.add_argument("--agent-acceptance",type=Path);parser.add_argument("--budget-verdict",type=Path);parser.add_argument("--finalize-existing",action="store_true");parser.add_argument("--no-render",action="store_true",help="validate structured review evidence without generating page PNGs");parser.add_argument("--scale",type=float,default=1.45);args=parser.parse_args()
    default_queue=not args.run and not args.pdf_dir and not args.batch_manifest
    if args.batch_manifest and (args.run or args.pdf_dir):raise ValueError("--batch-manifest cannot be combined with --run or --pdf-dir")
    if args.replace_in_existing and (not args.run or args.batch_manifest or args.pdf_dir):raise ValueError("--replace-in-existing requires one or more --run values only")
    if args.no_render and (args.agent_acceptance or args.finalize_existing):raise ValueError("--no-render is validation-only and cannot publish a final queue")
    if args.candidate_out and not args.no_render:raise ValueError("--candidate-out requires --no-render")
    if args.candidate and (args.run or args.pdf_dir or args.batch_manifest or args.replace_in_existing or args.agent_evidence_response or args.finalize_existing or args.no_render):raise ValueError("--candidate is an isolated fast-publication input")
    if args.candidate and not args.agent_acceptance:raise ValueError("--candidate requires --agent-acceptance")
    if args.candidate and args.phase_c_root is None:raise ValueError("--candidate requires the validated --phase-c-root")
    if not .75<=args.scale<=2.5:raise ValueError("scale must be between 0.75 and 2.5")
    project=args.project_root.resolve();phase_arg=args.phase_c_root or PHASE_C_ROOT;phase_root=(phase_arg if phase_arg.is_absolute() else project/phase_arg).resolve()
    if project not in phase_root.parents or not phase_root.is_dir():raise ValueError("missing or unsafe Phase-C artifact root")
    PHASE_C_ROOT=phase_root.relative_to(project)
    build_lock=acquire_single_instance_lock(project);public=project/"review-ui"/"public";target=public/"review-assets";public.mkdir(parents=True,exist_ok=True);head=git_head(project)
    agent_acceptance=None;review_budget=None
    if args.agent_acceptance:
        acceptance_path=(args.agent_acceptance if args.agent_acceptance.is_absolute() else project/args.agent_acceptance).resolve()
        if project not in acceptance_path.parents or not acceptance_path.is_file():raise ValueError("missing or unsafe pre-human agent acceptance receipt")
        agent_acceptance=load(acceptance_path)
        if not args.budget_verdict:raise ValueError("--agent-acceptance requires --budget-verdict")
        budget_path=(args.budget_verdict if args.budget_verdict.is_absolute() else project/args.budget_verdict).resolve()
        if project not in budget_path.parents or not budget_path.is_file():raise ValueError("missing or unsafe pre-human review budget verdict")
        review_budget=load(budget_path)
    elif args.budget_verdict:raise ValueError("--budget-verdict requires --agent-acceptance")
    if args.candidate:
        candidate_path=(args.candidate if args.candidate.is_absolute() else project/args.candidate).resolve()
        if project not in candidate_path.parents or not candidate_path.is_file():raise ValueError("missing or unsafe review candidate")
        queue=prepare_public_candidate(load(candidate_path));queue=finalize_public_queue(queue,agent_acceptance,review_budget,head,project)
        stage=Path(tempfile.mkdtemp(prefix="review-assets-",dir=public))
        try:
            materialize_review_candidate(project,stage,queue,args.scale)
            (stage/"queue.json").write_text(json.dumps(queue,ensure_ascii=False,separators=(",",":"))+"\n",encoding="utf-8")
            publish_staged_assets(public,target,stage)
            empty_requests={"schemaVersion":1,"kind":"WAKG_EVIDENCE_REPAIR_REQUESTS","policy":"deterministic-first; semantic agent only for unresolved context; no human routing before final review","requestCount":0,"requests":[]}
            empty_defects={"schemaVersion":1,"kind":"WAKG_EXTRACTION_REPAIR_DEFECTS","defectCount":0,"defects":[]}
            write_internal_repair_state(project,"CLOSED",empty_requests,empty_defects,public_queue_published=True)
            write_agent_review_state(project,prepare_public_candidate(queue),head,"CLOSED",agent_acceptance)
        except RenderCacheMissing as error:
            shutil.rmtree(stage,ignore_errors=True)
            print(json.dumps({"status":"render_required","publicQueuePublished":False,"candidate":str(candidate_path.relative_to(project)),"error":str(error),"runtimeSeconds":round(time.perf_counter()-started,3)},sort_keys=True));return 4
        except Exception:
            shutil.rmtree(stage,ignore_errors=True);raise
        print(json.dumps({"status":"published_from_candidate","papers":len(queue["papers"]),"target":"review-ui/public/review-assets","runtimeSeconds":round(time.perf_counter()-started,3)},sort_keys=True));return 0
    if args.finalize_existing:
        if args.run or args.pdf_dir or args.batch_manifest or args.agent_evidence_response:raise ValueError("--finalize-existing cannot be combined with build inputs")
        queue=prepare_public_candidate(load(target/"queue.json"))
        if agent_acceptance is None:
            request=write_agent_review_state(project,queue,head,"AGENT_REVIEW_REQUIRED")
            if request.get("semanticReviewNeeded") is False:
                agent_acceptance=load(project/request["reusedAcceptancePath"]);review_budget=load(project/request["budgetVerdictPath"])
            else:
                print(json.dumps({"status":"agent_review_required","publicQueuePublished":False,"requestPath":"internal_assets/pre-human-agent-review/agent-review-request.json","subjectSha256":request["subjectSha256"],"reviewedHead":head,"runtimeSeconds":round(time.perf_counter()-started,3)},sort_keys=True));return 3
        queue=finalize_public_queue(queue,agent_acceptance,review_budget,head,project);atomic_json(target/"queue.json",queue);remove_public_repair_envelopes(target);write_agent_review_state(project,prepare_public_candidate(queue),head,"CLOSED",agent_acceptance)
        print(json.dumps({"status":"published","reviewReadiness":queue["reviewReadiness"],"papers":len(queue["papers"]),"target":"review-ui/public/review-assets"},sort_keys=True));return 0
    agent_responses:dict[str,dict[str,Any]]={}
    if args.agent_evidence_response:
        response_path=(args.agent_evidence_response if args.agent_evidence_response.is_absolute() else project/args.agent_evidence_response).resolve();payload=load(response_path)
        if payload.get("kind")!="WAKG_EVIDENCE_REPAIR_RESPONSES" or not isinstance(payload.get("responses"),list):raise ValueError("invalid agent evidence response envelope")
        for item in payload["responses"]:
            if not isinstance(item,dict) or not item.get("requestId") or item["requestId"] in agent_responses:raise ValueError("invalid or duplicate agent evidence response")
            agent_responses[str(item["requestId"])]=item
    previous_queue=load(target/"queue.json") if (target/"queue.json").is_file() else None
    previous_anomaly=previous_queue.get("anomalyReview") if previous_queue else None
    stage=Path(tempfile.mkdtemp(prefix="review-assets-",dir=public))
    try:
        papers=[]
        runs=batch_runs(project,args.batch_manifest) if args.batch_manifest else (args.run or list(USER10V2_RUNS))
        if args.preserve_unselected_assets or args.replace_in_existing:preserve_unselected_assets(target,stage,{safe_run(value) for value in runs})
        for value in runs:
            run_id=safe_run(value);manifest=load(project/"runs"/run_id/"manifest.json")
            papers.append(build_quarantined_paper(project,stage,run_id,args.scale) if manifest.get("stage")=="QUARANTINED_MAP_ONLY" else build_paper(project,stage,run_id,args.scale,args.include_extracted or default_queue,agent_responses,not args.no_render))
        seen={paper["pdfSha256"] for paper in papers}
        for directory in args.pdf_dir:
            corpus=directory.resolve();pdfs=sorted(corpus.glob("*.pdf"),key=lambda p:p.name.lower())
            if not pdfs:raise ValueError(f"no PDFs found in {corpus}")
            for pdf in pdfs:
                if sha(pdf) not in seen:
                    papers.append(build_raw_paper(project,stage,pdf,args.scale));seen.add(papers[-1]["pdfSha256"])
        repair_requests=[];extraction_defects=[]
        for paper in papers:
            repair_requests.extend(paper.pop("_evidenceRepairRequests",[]));extraction_defects.extend(paper.pop("_extractionRepairDefects",[]))
        repair_payload={"schemaVersion":1,"kind":"WAKG_EVIDENCE_REPAIR_REQUESTS","policy":"deterministic-first; semantic agent only for unresolved context; no human routing before final review","requestCount":len(repair_requests),"requests":repair_requests}
        defect_payload={"schemaVersion":1,"kind":"WAKG_EXTRACTION_REPAIR_DEFECTS","defectCount":len(extraction_defects),"defects":extraction_defects}
        repair_status="AGENT_REQUIRED" if repair_requests else ("EXTRACTION_REPAIR_REQUIRED" if extraction_defects else "CLOSED")
        write_internal_repair_state(project,repair_status,repair_payload,defect_payload)
        if repair_status!="CLOSED":
            invalidate_agent_review_state(project,"BLOCKED_BY_"+repair_status)
            shutil.rmtree(stage,ignore_errors=True)
            print(json.dumps({"status":"repair_required","repairStatus":repair_status,"requestCount":len(repair_requests),"extractionDefectCount":len(extraction_defects),"publicQueuePublished":False,"repairRoot":"internal_assets/review-repair","runtimeSeconds":round(time.perf_counter()-started,3)},sort_keys=True));return 2
        if args.replace_in_existing:
            if previous_queue is None:raise ValueError("incremental publication requires an existing public queue")
            papers=merge_incremental_papers(previous_queue,papers)
        queue={"schemaVersion":1,"generatedAt":datetime.now(timezone.utc).isoformat().replace("+00:00","Z"),"queueScope":"USER10V2_EXTRACTED_REVIEW_PENDING","evidenceRepair":{"status":repair_status,"requestCount":len(repair_requests),"requestFile":"review-assets/evidence-repair-requests.json","extractionDefectCount":len(extraction_defects),"extractionDefectFile":"review-assets/extraction-repair-defects.json"},"papers":papers}
        queue["recallCorrectnessGate"]=build_recall_correctness_gate(project,papers)
        atomic_json(project/PHASE_C_ROOT/"recall-correctness-gate.json",queue["recallCorrectnessGate"])
        if previous_anomaly is not None:queue["anomalyReview"]=previous_anomaly
        queue=prepare_public_candidate(queue)
        if agent_acceptance is None:
            request=write_agent_review_state(project,queue,head,"AGENT_REVIEW_REQUIRED")
            candidate_output=None
            if args.candidate_out:
                candidate_output=(args.candidate_out if args.candidate_out.is_absolute() else project/args.candidate_out).resolve()
                if project not in candidate_output.parents:raise ValueError("unsafe candidate output path")
                atomic_json(candidate_output,queue)
            if request.get("semanticReviewNeeded") is False and not args.no_render:
                agent_acceptance=load(project/request["reusedAcceptancePath"]);review_budget=load(project/request["budgetVerdictPath"])
            else:
                shutil.rmtree(stage,ignore_errors=True)
                status="agent_review_reused" if request.get("semanticReviewNeeded") is False else "agent_review_required"
                print(json.dumps({"status":status,"publicQueuePublished":False,"requestPath":"internal_assets/pre-human-agent-review/agent-review-request.json","candidatePath":str(candidate_output.relative_to(project)) if candidate_output else None,"reusedAcceptancePath":request.get("reusedAcceptancePath"),"budgetVerdictPath":request.get("budgetVerdictPath"),"subjectSha256":request["subjectSha256"],"reviewedHead":head,"papers":len(papers),"runtimeSeconds":round(time.perf_counter()-started,3)},sort_keys=True));return 0 if status=="agent_review_reused" else 3
        queue=finalize_public_queue(queue,agent_acceptance,review_budget,head,project)
        (stage/"queue.json").write_text(json.dumps(queue,ensure_ascii=False,separators=(",",":"))+"\n",encoding="utf-8")
        publish_staged_assets(public,target,stage)
        write_internal_repair_state(project,"CLOSED",repair_payload,defect_payload,public_queue_published=True);write_agent_review_state(project,prepare_public_candidate(queue),head,"CLOSED",agent_acceptance)
    except Exception:
        shutil.rmtree(stage,ignore_errors=True);raise
    print(json.dumps({"status":"prepared","papers":len(papers),"target":"review-ui/public/review-assets"},sort_keys=True));return 0

if __name__=="__main__":raise SystemExit(main())
