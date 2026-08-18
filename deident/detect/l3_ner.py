"""L3 — 표에 없는 이름·기관을 형태소 분석과 접미사로 찾는다.

역할은 보조다. 실측해 보면 kiwipiepy 는 사전에 없는 이름을 통째로 쪼갠다
('박서준' → 박/NNP + 서/NNG + 준/NNG). 그래서 L2 가 표에서 건진 이름을
사용자 사전에 등록한 뒤 분석하고, 그래도 남는 이름만 여기서 줍는다.
반대로 '이 교수'의 '이'를 고유명사로 잘못 붙이기도 하므로 성씨 게이트와
중의성 규칙으로 걸러 낸다.
"""

from __future__ import annotations

import re

from ..config import Config
from ..entity.registry import (
    Registry, match_is_whole, resolve_surname_title, surname_is_ambiguous,
)
from ..kdata import (
    COMPOUND_SURNAMES, ORG_SHORT_MIN_PREFIX, ORG_STOPWORDS, ORG_SUFFIXES,
    ORG_SUFFIXES_SHORT, REFERENCE_MARKERS, SURNAMES, TITLE_ALTERNATION,
)
from ..model import (
    CAT_ORG, CAT_PERSON, GRADE_CERTAIN, GRADE_LIKELY, Finding, Segment,
)

_kiwi = None
_kiwi_failed = False

RE_SURNAME_TITLE = re.compile(rf"(?<![가-힣])([가-힣])\s+({TITLE_ALTERNATION})(?![가-힣])")
RE_ORG = re.compile(
    r"(?<![가-힣A-Za-z])([가-힣A-Za-z0-9]{2,15}(?:"
    + "|".join(re.escape(s) for s in ORG_SUFFIXES) + r"))"
)
# 한 글자 접미사(부·처·청)는 앞말이 길 때만 — '연락처'가 기관이 되는 것을 막는다
RE_ORG_SHORT = re.compile(
    rf"(?<![가-힣A-Za-z])([가-힣]{{{ORG_SHORT_MIN_PREFIX},15}}(?:"
    + "|".join(re.escape(s) for s in ORG_SUFFIXES_SHORT) + r"))(?![가-힣])"
)
RE_CITATION = re.compile(r"\([가-힣]{2,4}(?:\s*(?:외|등))?,\s*\d{4}\)")


def _get_kiwi(registry: Registry | None):
    """kiwipiepy 를 준비한다. 없으면 조용히 비활성 — 나머지 계층은 계속 돈다."""
    global _kiwi, _kiwi_failed
    if _kiwi_failed:
        return None
    if _kiwi is None:
        try:
            from kiwipiepy import Kiwi
        except ImportError:
            _kiwi_failed = True
            return None
        _kiwi = Kiwi()
    if registry:
        for record in registry.by_category(CAT_PERSON):
            try:
                _kiwi.add_user_word(record.canonical, "NNP")
            except Exception:
                pass
    return _kiwi


def is_header_cell(seg: Segment) -> bool:
    """표 머리글 칸은 이름표지 값이 아니다. 여기를 치우면 표가 읽히지 않는다."""
    return seg.table_ctx is not None and seg.table_ctx.is_header


def _surname_of(value: str) -> str:
    for compound in COMPOUND_SURNAMES:
        if value.startswith(compound):
            return compound
    return value[:1]


def _is_name_shape(value: str) -> bool:
    if not all("가" <= ch <= "힣" for ch in value):
        return False
    surname = _surname_of(value)
    if surname not in SURNAMES and surname not in COMPOUND_SURNAMES:
        return False
    given = value[len(surname):]
    return 1 <= len(given) <= 2


def _in_reference_section(segments: list[Segment]) -> set[str]:
    """참고문헌 이후의 세그먼트 — 공개된 저자명은 기본적으로 건드리지 않는다."""
    marked: set[str] = set()
    hit = False
    for seg in segments:
        text = seg.text.strip()
        if not hit and any(text.startswith(m) or text == m for m in
                           (f"#{'#' * i} {mk}" for i in range(4) for mk in REFERENCE_MARKERS)):
            hit = True
        if not hit and text in REFERENCE_MARKERS:
            hit = True
        if hit:
            marked.add(seg.seg_id)
    return marked


def _candidates_from_kiwi(kiwi, text: str) -> list[tuple[int, int, str]]:
    """NNP 토큰과, 쪼개진 이름을 다시 붙인 후보."""
    out: list[tuple[int, int, str]] = []
    try:
        tokens = kiwi.tokenize(text)
    except Exception:
        return out

    for idx, token in enumerate(tokens):
        if token.tag != "NNP":
            continue
        start, end, form = token.start, token.start + token.len, token.form
        # '박/NNP + 서/NNG + 준/NNG' 처럼 쪼개진 이름을 다시 잇는다
        cursor = idx + 1
        while cursor < len(tokens) and len(form) < 4:
            nxt = tokens[cursor]
            if nxt.tag not in ("NNG", "NNP") or nxt.len != 1:
                break
            if nxt.start != end:
                break   # 공백이 끼면 다른 낱말
            form += nxt.form
            end = nxt.start + nxt.len
            cursor += 1
            out.append((start, end, form))
        out.append((token.start, token.start + token.len, token.form))
    return out


def detect_people(segments: list[Segment], config: Config,
                  registry: Registry) -> list[Finding]:
    if not config.category_enabled(CAT_PERSON):
        return []

    kiwi = _get_kiwi(registry)
    reference_segments = _in_reference_section(segments)
    known = {r.canonical for r in registry.by_category(CAT_PERSON)}
    out: list[Finding] = []

    for seg in segments:
        text = seg.text
        if not text.strip() or is_header_cell(seg):
            continue
        in_reference = seg.seg_id in reference_segments

        # (1) 형태소 후보
        if kiwi is not None:
            for start, end, form in _candidates_from_kiwi(kiwi, text):
                if form in known:
                    continue          # 대장 전파(L2)가 이미 확실 등급으로 잡는다
                if not _is_name_shape(form):
                    continue
                if not match_is_whole(text, start, end, CAT_PERSON):
                    continue
                surname = _surname_of(form)
                if len(form) - len(surname) < 2:
                    # 성+외자 2글자는 직함이 뒤따를 때만 인정한다
                    tail = text[end:end + 4]
                    if not re.match(rf"\s*(?:{TITLE_ALTERNATION})", tail):
                        continue
                if in_reference:
                    continue
                out.append(Finding(
                    seg_id=seg.seg_id, start=start, end=end, text=form,
                    category=CAT_PERSON, grade=GRADE_LIKELY, detectors={"L3"},
                    note="형태소 후보", part=seg.part,
                ))

        # (2) 성 + 직함 ('김 박사')
        for m in RE_SURNAME_TITLE.finditer(text):
            surname = m.group(1)
            if surname not in SURNAMES:
                continue
            record = resolve_surname_title(surname, registry)
            if record is None and surname_is_ambiguous(surname):
                continue   # '이 교수'의 '이'는 지시관형사일 수 있다
            grade = GRADE_CERTAIN if record is not None else GRADE_LIKELY
            note = "성+직함 (대장 연결)" if record is not None else "성+직함 (미연결)"
            if in_reference:
                continue
            out.append(Finding(
                seg_id=seg.seg_id, start=m.start(1), end=m.end(1), text=surname,
                category=CAT_PERSON, grade=grade, detectors={"L3"},
                entity_id=record.entity_id if record else None,
                note=note, part=seg.part,
            ))

    return out


def detect_orgs(segments: list[Segment], config: Config,
                registry: Registry) -> list[Finding]:
    """접미사로 기관명을 찾는다. 형태소 분석보다 이쪽이 훨씬 정확하다."""
    if not config.category_enabled(CAT_ORG):
        return []

    known = {r.canonical for r in registry.by_category(CAT_ORG)}
    out: list[Finding] = []
    for seg in segments:
        if is_header_cell(seg):
            continue
        for pattern in (RE_ORG, RE_ORG_SHORT):
            for m in pattern.finditer(seg.text):
                value = m.group(1)
                if value in known:
                    continue          # L2 전파 소관
                if value in ORG_STOPWORDS:
                    continue          # '참여연구원' 같은 역할어
                if not match_is_whole(seg.text, m.start(1), m.end(1), CAT_ORG):
                    continue
                out.append(Finding(
                    seg_id=seg.seg_id, start=m.start(1), end=m.end(1), text=value,
                    category=CAT_ORG, grade=GRADE_LIKELY, detectors={"L3"},
                    note="기관 접미사", part=seg.part,
                ))
    return out


def detect(segments: list[Segment], config: Config,
           registry: Registry) -> list[Finding]:
    return detect_people(segments, config, registry) + detect_orgs(segments, config, registry)


def available() -> bool:
    return _get_kiwi(None) is not None
