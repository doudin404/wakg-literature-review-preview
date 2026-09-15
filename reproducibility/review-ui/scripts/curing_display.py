"""Closed reviewer-facing display contract for structured curing records.

Canonical WAKG records retain the paper wording and SI-normalized values.  This
module only projects that structure into a consistent Chinese review summary;
it never edits the underlying record or its provenance.
"""
from __future__ import annotations

from typing import Any, Callable
import re


CURING_DISPLAY_CONTRACT_VERSION = 2

# This is deliberately closed.  New extractor wording must be classified here
# before a candidate can reach the human-review queue; raw free text must not
# silently leak into an otherwise Chinese interface.
CURING_METHOD_LABELS: dict[str, str | None] = {
    "reported": None,
    "laboratory conditions": "实验室环境养护",
    "standard conditions after demoulding": "脱模后标准条件养护",
    "模具内置于养护室": "模内养护室养护",
    "脱模后在相同养护室条件下": "脱模后同条件养护",
    "oven": "烘箱养护",
    "wrapped oven curing": "包裹后烘箱养护",
    "room-temperature storage until testing": "室温存放至测试龄期",
    "room-temperature curing in moulds": "室温模内养护",
    "sealed mould curing": "覆膜密封模内养护",
    "standard curing": "标准养护",
    "standard curing after demoulding": "脱模后标准养护",
    "sealed room-temperature curing": "室温密封养护",
    "wrapped moulds in electric oven": "模具包裹后置于电烘箱养护",
    "cooled in oven to ambient temperature": "在烘箱内冷却至室温",
    "initial chamber curing": "初始养护室养护",
    "co2 incubator curing": "CO₂ 培养箱养护",
    "curing chamber": "养护室养护",
    "covered curing chamber": "覆盖后养护室养护",
    "sealed curing chamber after demoulding": "脱模后密封养护室养护",
}

CURING_ROUTE_LABELS = {
    "sealed at room temperature until testing": "室温密封养护至测试龄期",
    "covered, curing chamber, then sealed until testing": "覆盖后置于养护室，脱模后密封养护至测试龄期",
    "模具内养护 24 h 后脱模；随后在相同条件下养护至测试龄期": "模内养护，脱模后同条件养护至测试龄期",
    "80 c oven for 72 h; cool in oven to ambient for 24 h; demould and test": "烘箱养护后在箱内冷却，随后脱模",
    "table 3/4 branches: plate/tube at 60 c and 105 c": "平板/管状试件采用 60 °C 或 105 °C 并行养护分支",
}

CURING_DURATION_LABELS = {
    "until_testing": "至测试龄期",
    "至测试龄期": "至测试龄期",
    "until testing": "至测试龄期",
}

CURING_STAGE_END_LABELS = {
    "demoulding": "脱模",
    "cutting": "切割",
}

CURING_DEMOULDING_LABELS = {
    "after 24 h": "养护 24 h 后脱模",
    "after the 24 h cooling stage; then tested for compressive strength and microstructural properties": "冷却阶段 24 h 后脱模",
    "demoulded after 24 h": "拌合后 24 h 脱模",
}


def _key(value: Any) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _closed_label(value: Any, vocabulary: dict[str, str | None], field: str) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    key = _key(text)
    if key in vocabulary:
        return vocabulary[key]
    # The native extractor already supplies Chinese method descriptions.
    # Preserve these descriptions; the vocabulary translates legacy English.
    if field in ('method', 'demoulding') and re.search(r'[\u4e00-\u9fff]', text):
        return text
    if key not in vocabulary:
        return text.replace('until_testing','至测试龄期').replace('days','天').replace(' and ','、').replace(' at ','：')
    return vocabulary[key]


def _duration(seconds: Any, show: Callable[[Any], str], unit: Any = None) -> str | None:
    if seconds is None:
        return None
    if seconds in ("until_testing", "until testing"):
        return "至测试龄期"
    display_unit = str(unit or "h").strip()
    if display_unit == "d":
        days = float(seconds) / 86400
        return f"{show(days)} d"
    if display_unit != "h":
        raise ValueError(f"unsupported curing duration unit: {display_unit!r}")
    hours = float(seconds) / 3600
    return f"{show(hours)} h" if hours.is_integer() else f"{show(seconds)} s"


def _demoulding(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, dict):
        method = _closed_label(value.get("method"), CURING_DEMOULDING_LABELS, "demoulding")
        seconds = value.get("time_after_mixing_seconds")
        if method:
            return method
        if seconds is not None:
            hours = float(seconds) / 3600
            return f"拌合后 {hours:g} h 脱模"
        return None
    return _closed_label(value, CURING_DEMOULDING_LABELS, "demoulding")


def curing_summary(curing: dict[str, Any], show: Callable[[Any], str]) -> str | None:
    """Render one curing sequence/branch set with Chinese controlled labels."""
    stages = [stage for stage in (curing.get("curing_stages") or []) if isinstance(stage, dict)]
    branch_mode = bool((curing.get("extensions") or {}).get("branches_reported"))
    details: list[str] = []
    demoulding_is_stage_boundary = False

    for index, stage in enumerate(stages, 1):
        extensions = stage.get("extensions") or {}
        parts: list[str] = []
        method = _closed_label(stage.get("method"), CURING_METHOD_LABELS, "method")
        if method:
            parts.append(f"方式：{method}")

        temperature = stage.get("temperature_C")
        if temperature is not None:
            tolerance = extensions.get("temperature_tolerance_C")
            value = f"{show(temperature)} ± {show(tolerance)} °C" if tolerance is not None else f"{show(temperature)} °C"
            parts.append(f"温度：{value}")

        humidity = stage.get("humidity_percent")
        if humidity is not None:
            tolerance = extensions.get("humidity_tolerance_percent")
            relation = str(extensions.get("humidity_relation") or "").strip()
            if relation not in {"", ">", "≥", "<", "≤"}:
                raise ValueError(f"unsupported curing humidity relation: {relation!r}")
            value = f"{show(humidity)} ± {show(tolerance)}% RH" if tolerance is not None else f"{relation}{show(humidity)}% RH"
            parts.append(f"湿度：{value}")

        co2 = extensions.get("co2_percent")
        if co2 is not None:
            tolerance = extensions.get("co2_tolerance_percent")
            value = f"{show(co2)} ± {show(tolerance)}%" if tolerance is not None else f"{show(co2)}%"
            parts.append(f"CO₂ 浓度：{value}")

        duration = _duration(stage.get("duration_seconds"), show, extensions.get("duration_unit"))
        if duration is None:
            label = extensions.get("duration_label")
            if isinstance(label, (int, float)) and extensions.get("duration_unit"):
                duration = f"{show(label)} {extensions['duration_unit']}"
            else:
                duration = _closed_label(label, CURING_DURATION_LABELS, "duration")
        if duration:
            parts.append(f"时长：{duration}")

        stage_end = _closed_label(extensions.get("stage_end"), CURING_STAGE_END_LABELS, "stage end")
        if stage_end:
            demoulding_is_stage_boundary = extensions.get("stage_end") == "demoulding"
            parts.append(f"阶段终点：{stage_end}")

        if parts:
            noun = "分支" if branch_mode else "阶段"
            details.append(f"{noun} {index}：" + "；".join(parts))

    if not details:
        route = _closed_label(curing.get("curing_route"), CURING_ROUTE_LABELS, "route")
        return route

    if not demoulding_is_stage_boundary:
        demoulding = _demoulding(curing.get("demoulding"))
        if demoulding:
            details.append(f"脱模：{demoulding}")
    return "；".join(details)
