"""L2 — 문서 구조에서 값을 수확하고 문서 전체로 전파한다.

이름 탐지의 주력이다. 연구계획서의 실명은 거의 언제나 인적사항 표나
'연구책임자: 홍길동' 같은 라벨 뒤에 처음 등장한다. 거기서 확실하게 건진
이름을 대장에 올리고 변형을 만들어 본문 전체를 훑는 편이, 형태소 분석기로
본문에서 이름을 찾아내려는 것보다 정확하다.
"""

from __future__ import annotations

import re

from ..config import Config
from ..entity.registry import Registry, match_is_whole
from ..kdata import HEADER_CATEGORY, LABEL_CATEGORY, ORG_SUFFIXES
from ..model import (
    CAT_DEPT, CAT_EMAIL, CAT_ORG, CAT_PERSON, CAT_PHONE, CAT_TITLE,
    GRADE_CERTAIN, RE_PSEUDONYM, Finding, Segment, normalize_key,
)

# 표 헤더는 줄바꿈·괄호가 섞인다: '성 명\n(한글)' → '성명'
RE_HEADER_CLEAN = re.compile(r"[\s()（）]+")

RE_LABEL = re.compile(
    r"(?P<label>" + "|".join(sorted(LABEL_CATEGORY, key=len, reverse=True)) + r")"
    r"\s*[:：]\s*(?P<value>[^\s,，/|]{2,20})"
)

MAX_NAME_LEN = 5
MIN_ORG_LEN = 3


def _clean_header(text: str) -> str:
    return RE_HEADER_CLEAN.sub("", text)


def header_category(seg: Segment) -> str | None:
    """이 칸을 지배하는 이름표가 무엇을 가리키는가.

    추출기는 가로·세로 두 축의 이름표를 함께 넘긴다('연구책임자 성명').
    문맥어 탐색에는 그편이 좋지만, 사전 조회는 정확히 일치해야 해서 이어 붙인
    문자열로는 통째로 빗나간다. 그래서 전체 → 오른쪽 조각부터 차례로 본다.
    오른쪽(열 이름표)이 대개 더 구체적이다 — '연구책임자 연락처'는 이름이
    아니라 연락처 칸이다.
    """
    ctx = seg.table_ctx
    if ctx is None or ctx.is_header or not ctx.header_text:
        return None
    whole = HEADER_CATEGORY.get(_clean_header(ctx.header_text))
    if whole:
        return whole
    for part in reversed(ctx.header_text.split()):
        found = HEADER_CATEGORY.get(_clean_header(part))
        if found:
            return found
    return None


def _looks_like_name(value: str) -> bool:
    if not 2 <= len(value) <= MAX_NAME_LEN:
        return False
    return all("가" <= ch <= "힣" for ch in value)


def _looks_like_org(value: str) -> bool:
    if len(value) < MIN_ORG_LEN:
        return False
    return any(value.endswith(suffix) or suffix in value for suffix in ORG_SUFFIXES)


def harvest(segments: list[Segment], config: Config) -> Registry:
    """표와 라벨에서 인물·기관을 건져 대장을 만든다."""
    registry = Registry()

    # 같은 행의 다른 칸들을 인물의 속성으로 붙이기 위한 색인
    rows: dict[tuple[str, str, int], dict[str, str]] = {}
    for seg in segments:
        ctx = seg.table_ctx
        if ctx is None or ctx.is_header or not seg.text.strip():
            continue
        category = header_category(seg)
        if category:
            rows.setdefault((seg.part, ctx.table_id, ctx.row), {})[category] = seg.text.strip()

    for seg in segments:
        category = header_category(seg)
        if not category:
            continue
        value = seg.text.strip()
        if not value or RE_PSEUDONYM.search(value):
            continue   # 이미 치환된 칸에서 가명을 이름으로 다시 건지면 안 된다
        ctx = seg.table_ctx
        attributes = dict(rows.get((seg.part, ctx.table_id, ctx.row), {}))
        attributes.pop(category, None)

        if category == CAT_PERSON:
            # 한 칸에 '홍길동(책임)' 처럼 붙어 오는 경우가 흔하다
            for name in re.findall(r"[가-힣]{2,5}", value):
                if _looks_like_name(name):
                    registry.add(name, CAT_PERSON, attributes)
        elif category == CAT_ORG:
            if _looks_like_org(value) or len(value) >= MIN_ORG_LEN:
                registry.add(value, CAT_ORG, attributes)
        elif category == CAT_DEPT:
            registry.add(value, CAT_DEPT, attributes)

    # 자유문 라벨 — '연구책임자: 홍길동'
    for seg in segments:
        for m in RE_LABEL.finditer(seg.text):
            category = LABEL_CATEGORY[m.group("label")]
            value = m.group("value").strip()
            if RE_PSEUDONYM.search(value):
                continue
            if category == CAT_PERSON and not _looks_like_name(value):
                continue
            if category in (CAT_ORG, CAT_DEPT) and len(value) < MIN_ORG_LEN:
                continue
            registry.add(value, category)

    return registry


def _cell_findings(segments: list[Segment], config: Config) -> list[Finding]:
    """표 헤더가 지목한 칸 자체를 탐지 결과로 만든다.

    본문 전파와 별개로, 표 안의 값은 그 자리에서 확실 등급으로 잡는다.
    (헤더가 '성명'이면 그 칸은 이름이다 — 형태가 어떻든.)
    """
    out: list[Finding] = []
    for seg in segments:
        category = header_category(seg)
        if category is None or category in (CAT_TITLE, CAT_PHONE, CAT_EMAIL):
            continue   # 연락처·이메일은 L1 이 이미 정확히 잡는다
        if not config.category_enabled(category):
            continue
        value = seg.text.strip()
        if not value or len(value) < 2 or RE_PSEUDONYM.search(value):
            continue
        if category == CAT_PERSON and not _looks_like_name(value):
            continue   # '성명' 칸에 이름이 아닌 것이 들어 있으면 L1·L3 소관이다
        start = seg.text.index(value)
        out.append(Finding(
            seg_id=seg.seg_id, start=start, end=start + len(value), text=value,
            category=category, grade=GRADE_CERTAIN, detectors={"L2"},
            note=f"표 머리글 '{seg.table_ctx.header_text.strip()}'", part=seg.part,
        ))
    return out


def propagate(segments: list[Segment], registry: Registry,
              config: Config) -> list[Finding]:
    """대장의 표기 변형을 문서 전체에서 찾는다 (최장일치 우선)."""
    pairs = registry.all_variants()
    if not pairs:
        return []

    out: list[Finding] = []
    for seg in segments:
        text = seg.text
        if not text.strip():
            continue
        if seg.table_ctx is not None and seg.table_ctx.is_header:
            continue   # 머리글 칸은 이름표다 — 값이 아니므로 치환 대상이 아니다
        taken: list[tuple[int, int]] = []
        for variant, record in pairs:
            if not config.category_enabled(record.category):
                continue
            start = 0
            while True:
                idx = text.find(variant, start)
                if idx < 0:
                    break
                end = idx + len(variant)
                start = idx + 1
                if any(idx < e and s < end for s, e in taken):
                    continue
                if not match_is_whole(text, idx, end, record.category):
                    continue
                taken.append((idx, end))
                out.append(Finding(
                    seg_id=seg.seg_id, start=idx, end=end, text=text[idx:end],
                    category=record.category, grade=GRADE_CERTAIN,
                    detectors={"L2"}, entity_id=record.entity_id,
                    note="대장 전파", part=seg.part,
                ))
    return out


def detect(segments: list[Segment], config: Config,
           registry: Registry | None = None) -> tuple[list[Finding], Registry]:
    registry = registry or harvest(segments, config)
    findings = _cell_findings(segments, config)
    findings.extend(propagate(segments, registry, config))
    return findings, registry
