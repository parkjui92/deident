"""마무리 훑기 — 한 번 민감하다고 판정된 값은 문서 전체에서 지운다.

탐지는 문맥에 기댄다. 그래서 같은 이름이 표에서는 잡히고 본문 한구석에서는
놓치는 일이 생긴다(발표자료 실측에서 슬라이드 8에서는 잡히고 슬라이드 1에서는
놓쳤다). 그런데 그 값이 민감하다는 판정은 이미 내려져 있다 — 매핑에 올라 있다면
그 값은 문서 어디에 있든 민감하다.

그래서 치환 계획을 세운 뒤, 매핑에 오른 값들을 다시 한 번 전 세그먼트에서 찾는다.
이 한 겹이 '문맥 없는 자리에 남는 이름'을 없앤다.
"""

from __future__ import annotations

import re

from ..config import Config
from ..entity import boundary
from ..entity.boundary import BOUNDED, LOOSE_BOUNDARY_LENGTH
from ..entity.registry import match_is_whole
from ..entity.pseudonym import Mapping
from ..model import RE_PSEUDONYM, Replacement, Segment

MIN_LENGTH = 2

# 벌려 쓴 표기에서 낱말 경계를 따지지 않을 최소 길이 (붙여쓴 경우보다 한 글자 더 요구)
SPACED_LOOSE_LENGTH = 4


# 글자가 숨어 있는 XML 속성. 도형 이름·대체 텍스트에 본문이 복사돼 들어가는 일이
# 잦은데, 화면에 보이지 않아 사람이 눈으로 찾지 못한다.
TEXT_ATTRS = ("name", "descr", "title", "alt", "tooltip", "author", "creator")


def sweep_attributes(root, mapping: Mapping) -> int:
    """XML 속성에 남은 원값을 가명으로 바꾼다. 바꾼 개수를 돌려준다."""
    pairs = [(e.original, e.pseudonym) for e in mapping.entries.values()
             if e.original and len(e.original) >= MIN_LENGTH]
    if not pairs:
        return 0
    pairs.sort(key=lambda kv: len(kv[0]), reverse=True)

    changed = 0
    for element in root.iter():
        for attr in TEXT_ATTRS:
            value = element.get(attr)
            if not value:
                continue
            new_value = value
            for original, pseudonym in pairs:
                if original in new_value:
                    new_value = new_value.replace(original, pseudonym)
            if new_value != value:
                element.set(attr, new_value)
                changed += 1
    return changed


def sweep_text(text: str, mapping: Mapping, config: Config) -> str:
    """이미 만들어진 텍스트 산출물에 마지막으로 한 번 더 훑는다.

    마크다운으로 다시 그릴 때 서로 다른 칸·문단이 한 줄로 이어지면서, 원본에서는
    쪼개져 있던 이름이 읽히는 형태로 되살아나는 일이 있다. 문서 안에서 지웠어도
    렌더 결과에서 다시 생기면 그것도 유출이다.
    """
    for entry in mapping.entries.values():
        if not config.category_enabled(entry.category):
            continue
        for value in sorted([entry.original, *entry.variants], key=len, reverse=True):
            if len(value) < MIN_LENGTH or RE_PSEUDONYM.search(value):
                continue
            if boundary.needs_word_check(value, entry.category):
                # 두 글자 값은 그 낱말로 쓰인 자리만 — '강산'을 지우다 '강산성'을
                # 망가뜨리지 않는다. 게이트도 같은 기준으로 본다.
                for start in reversed(boundary.occurrences(text, value, entry.category)):
                    text = text[:start] + entry.pseudonym + text[start + len(value):]
                continue
            if value in text:
                text = text.replace(value, entry.pseudonym)
            spaced = _spaced_pattern(value)
            if spaced is not None:
                text = spaced.sub(entry.pseudonym, text)
    return text


def sweep(segments: list[Segment], mapping: Mapping, config: Config,
          planned: list[Replacement]) -> list[Replacement]:
    """이미 세운 계획에 더해, 매핑에 오른 값의 남은 자리를 채운다."""
    targets: list[tuple[str, str, str]] = []   # (원값, 가명, 카테고리)
    for entry in mapping.entries.values():
        if not config.category_enabled(entry.category):
            continue
        for value in [entry.original, *entry.variants]:
            if len(value) >= MIN_LENGTH and not RE_PSEUDONYM.search(value):
                targets.append((value, entry.pseudonym, entry.category))
    if not targets:
        return []

    # 긴 값을 먼저 — 짧은 값이 긴 값의 일부를 갉아먹지 않도록
    targets.sort(key=lambda t: len(t[0]), reverse=True)

    taken: dict[str, list[tuple[int, int]]] = {}
    for plan in planned:
        taken.setdefault(plan.seg_id, []).append((plan.start, plan.end))

    extra: list[Replacement] = []
    for seg in segments:
        text = seg.text
        if not text.strip():
            continue
        spans = taken.setdefault(seg.seg_id, [])
        pseudo_spans = [(m.start(), m.end()) for m in RE_PSEUDONYM.finditer(text)]

        def claim(idx: int, end: int, value: str, pseudonym: str, category: str,
                  strict: bool, cross: bool = False) -> None:
            if any(idx < e and s < end for s, e in spans):
                return
            if any(idx < e and s < end for s, e in pseudo_spans):
                return
            if category in BOUNDED and (strict or len(value) < LOOSE_BOUNDARY_LENGTH):
                # 벌려 쓴 표기는 띄어쓰기만 본다. 붙여 쓴 자리는 낱말 경계까지 본다 —
                # '발표자강산'은 치환하고 '강산성'은 둔다 (게이트와 같은 기준).
                whole = (match_is_whole(text, idx, end, category) if cross
                         else boundary.is_occurrence(text, idx, end, category))
                if not whole:
                    return
            if not cross and not seg.slice_editable(idx, end):
                return
            spans.append((idx, end))
            extra.append(Replacement(
                seg_id=seg.seg_id, start=idx, end=end, original=text[idx:end],
                replacement=pseudonym, category=category, cross_boundary=cross,
            ))

        for value, pseudonym, category in targets:
            start = 0
            while True:
                idx = text.find(value, start)
                if idx < 0:
                    break
                claim(idx, idx + len(value), value, pseudonym, category, strict=False)
                start = idx + 1

            # 자간을 벌려 쓴 표기 ('홍 길 동') — 발표자료 제목·표지에서 흔하다.
            # 눈에는 같은 이름인데 붙여쓴 형태만 찾으면 그대로 남는다.
            # 네 글자 이상이면 앞뒤 붙은 글자를 따지지 않는다. 벌려 쓴 글에서는
            # 낱말 경계가 흐려져, 경계를 엄격히 보면 아는 값이 그대로 남는다.
            spaced = _spaced_pattern(value)
            if spaced is not None:
                strict = len(value) < SPACED_LOOSE_LENGTH
                for m in spaced.finditer(text):
                    claim(m.start(), m.end(), value, pseudonym, category, strict,
                          cross=True)

    return extra


_SPACED_CACHE: dict[str, "re.Pattern | None"] = {}


def _spaced_pattern(value: str):
    """글자 사이에 공백·가운뎃점이 끼어도 찾는 패턴.

    값 자체에 이미 띄어쓰기가 있어도 만든다 ('한국 화학연구원'). 원래 공백을
    글자로 세면 패턴이 만들어지지 않아, 벌려 쓴 기관명이 그대로 남는다.
    """
    if value in _SPACED_CACHE:
        return _SPACED_CACHE[value]
    letters = [ch for ch in value if not ch.isspace()]
    pattern = None
    if (len(letters) >= LOOSE_BOUNDARY_LENGTH
            and all("가" <= ch <= "힣" for ch in letters)):
        pattern = re.compile(r"[\s·]*".join(re.escape(ch) for ch in letters))
    _SPACED_CACHE[value] = pattern
    return pattern
