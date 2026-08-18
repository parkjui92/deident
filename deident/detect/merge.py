"""탐지 결과 병합 — 중복·중첩 해소와 allowlist 필터.

우선순위: rule(사용자 지정) > L1(구조적) > L2(표 수확·전파) > L3(형태소 추정).
같은 구간을 여러 탐지기가 잡으면 detectors 를 합산해 리포트에 신뢰도로 보여준다.
"""

from __future__ import annotations

from ..config import Config
from ..model import RE_PSEUDONYM, Finding, GRADE_CERTAIN, normalize_key

DETECTOR_RANK = {"rule": 0, "L1": 1, "L2": 2, "L3": 3}


def _rank(finding: Finding) -> int:
    return min((DETECTOR_RANK.get(d, 9) for d in finding.detectors), default=9)


def _pseudonym_spans(text: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in RE_PSEUDONYM.finditer(text)]


def merge_findings(findings: list[Finding], config: Config,
                   segment_texts: dict[str, str] | None = None) -> list[Finding]:
    allow = config.allowlist
    texts = segment_texts or {}
    spans_cache: dict[str, list[tuple[int, int]]] = {}

    kept: list[Finding] = []
    for f in findings:
        if not config.category_enabled(f.category):
            continue
        if normalize_key(f.text) in allow:
            continue
        if RE_PSEUDONYM.fullmatch(f.text.strip()):
            continue
        # 가명 안쪽을 다시 잡는 것도 막는다 ('[연구자A]' 속의 '연구자')
        text = texts.get(f.seg_id)
        if text is not None:
            if f.seg_id not in spans_cache:
                spans_cache[f.seg_id] = _pseudonym_spans(text)
            if any(f.start < end and start < f.end for start, end in spans_cache[f.seg_id]):
                continue
        kept.append(f)

    # 세그먼트별로 정렬 후 겹침 해소
    by_segment: dict[str, list[Finding]] = {}
    for f in kept:
        by_segment.setdefault(f.seg_id, []).append(f)

    merged: list[Finding] = []
    for seg_id, group in by_segment.items():
        # 시작 오름차순, 같은 시작이면 긴 것 먼저, 그다음 우선순위 높은 것
        group.sort(key=lambda f: (f.start, -f.length, _rank(f)))
        chosen: list[Finding] = []
        for f in group:
            overlap = None
            for existing in chosen:
                if f.start < existing.end and existing.start < f.end:
                    overlap = existing
                    break
            if overlap is None:
                chosen.append(f)
                continue

            same_span = (f.start, f.end) == (overlap.start, overlap.end)
            if same_span:
                # 동일 구간 — 탐지기 합산, 카테고리는 우선순위 높은 쪽 유지
                overlap.detectors |= f.detectors
                if _rank(f) < _rank(overlap):
                    overlap.category = f.category
                    overlap.note = f.note
                if f.grade == GRADE_CERTAIN:
                    overlap.grade = GRADE_CERTAIN
                continue

            contains = f.start >= overlap.start and f.end <= overlap.end
            if contains:
                overlap.detectors |= f.detectors
                continue

            if overlap.start >= f.start and overlap.end <= f.end:
                # 새 탐지가 기존을 품는다 — 넓은 쪽으로 교체
                f.detectors |= overlap.detectors
                chosen[chosen.index(overlap)] = f
                continue

            # 부분 중첩: 합집합 구간으로 넓히고 표시를 남긴다
            text = (segment_texts or {}).get(seg_id)
            start, end = min(overlap.start, f.start), max(overlap.end, f.end)
            overlap.start, overlap.end = start, end
            overlap.detectors |= f.detectors
            overlap.note = (overlap.note + " / 중첩 병합").strip(" /")
            if text is not None:
                overlap.text = text[start:end]
            if _rank(f) < _rank(overlap):
                overlap.category = f.category

        merged.extend(chosen)

    merged.sort(key=lambda f: (f.part, f.seg_id, f.start))
    return merged
