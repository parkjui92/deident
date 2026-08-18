"""탐지 결과를 치환 계획으로 바꾼다.

같은 세그먼트 안에서는 반드시 뒤에서 앞으로(start 내림차순) 적용해야 한다.
앞을 먼저 바꾸면 뒤 오프셋이 통째로 어긋난다 — 모든 포맷의 치환기가 이
규칙을 공유한다.
"""

from __future__ import annotations

from ..config import Config
from ..entity.pseudonym import Mapping
from ..model import (
    GRADE_CERTAIN, Finding, Replacement, Segment,
)


def plan_replacements(findings: list[Finding], segments: dict[str, Segment],
                      mapping: Mapping, config: Config,
                      strict_only: bool = False, mask_mode: bool = False) -> list[Replacement]:
    plans: list[Replacement] = []
    for f in findings:
        if strict_only and f.grade != GRADE_CERTAIN:
            continue
        if not strict_only and not config.replace_likely and f.grade != GRADE_CERTAIN:
            continue
        seg = segments.get(f.seg_id)
        if seg is None or not seg.slice_editable(f.start, f.end):
            continue

        if mask_mode:
            mask_char = str(config.settings.get("mask_char", "●"))[:1] or "●"
            replacement = mask_char * max(1, len(f.text.strip()))
        else:
            replacement = mapping.pseudonym_for(f.text, f.category)

        plans.append(Replacement(
            seg_id=f.seg_id, start=f.start, end=f.end,
            original=f.text, replacement=replacement, category=f.category,
        ))
    return plans


def group_by_segment(plans: list[Replacement]) -> dict[str, list[Replacement]]:
    """세그먼트별로 묶고 뒤에서 앞 순서로 정렬한다."""
    grouped: dict[str, list[Replacement]] = {}
    for plan in plans:
        grouped.setdefault(plan.seg_id, []).append(plan)
    for items in grouped.values():
        items.sort(key=lambda p: p.start, reverse=True)
    return grouped


def apply_to_text(text: str, plans: list[Replacement]) -> str:
    """문자열 세그먼트에 치환 적용 (역순 전제)."""
    out = text
    for plan in sorted(plans, key=lambda p: p.start, reverse=True):
        out = out[:plan.start] + plan.replacement + out[plan.end:]
    return out
