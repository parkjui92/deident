"""사용자 사전 기반 탐지 — denylist(강제 탐지)와 기밀 규칙."""

from __future__ import annotations

import re

from ..config import Config
from ..model import Finding, Segment, normalize_key


def _literal_findings(seg: Segment, value: str, category: str, grade: str,
                      detector: str, note: str) -> list[Finding]:
    """리터럴 값을 세그먼트에서 전부 찾는다 (정규화 차이는 사전에 흡수)."""
    out: list[Finding] = []
    if not value:
        return out
    for m in re.finditer(re.escape(value), seg.text):
        out.append(Finding(
            seg_id=seg.seg_id, start=m.start(), end=m.end(), text=m.group(),
            category=category, grade=grade, detectors={detector},
            note=note, part=seg.part,
        ))
    return out


def detect_segment(seg: Segment, config: Config) -> list[Finding]:
    out: list[Finding] = []

    for value, category in config.denylist:
        out.extend(_literal_findings(seg, value, category, "확실", "rule", "denylist 지정"))

    for rule in config.rules:
        if rule.context and not rule.context.search(seg.text):
            continue
        for literal in rule.literals:
            out.extend(_literal_findings(seg, literal, rule.category, rule.grade,
                                         "rule", f"규칙 {rule.name}"))
        if rule.pattern:
            for m in rule.pattern.finditer(seg.text):
                # 캡처 그룹 1이 있으면 그 부분만 값으로 본다 (문맥어는 남긴다)
                group = 1 if m.lastindex else 0
                out.append(Finding(
                    seg_id=seg.seg_id, start=m.start(group), end=m.end(group),
                    text=m.group(group), category=rule.category, grade=rule.grade,
                    detectors={"rule"}, note=f"규칙 {rule.name}", part=seg.part,
                ))

    return [f for f in out if f.text.strip() and seg.slice_editable(f.start, f.end)]


def normalized_denylist(config: Config) -> set[str]:
    return {normalize_key(v) for v, _ in config.denylist}
