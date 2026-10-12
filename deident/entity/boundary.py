"""짧은 값이 '그 낱말'로 쓰인 자리인가 — 치환기와 게이트가 함께 쓰는 한 가지 기준.

두 글자 이름은 다른 낱말 속에도 들어 있다('강산' ⊂ '강산성', '수연' ⊂ '교수연구실').
치환기는 그런 자리를 건드리지 않는데 게이트가 글자 그대로 찾으면, 정상 문서가 영원히
게이트를 통과하지 못한다. 반대로 게이트까지 띄어쓰기만 보게 하면 '발표자강산'처럼
붙여 쓴 진짜 이름이 조용히 남는다.

그래서 양쪽이 같은 물음을 던진다 — 그 자리가 낱말의 경계와 맞아떨어지는가.
  · 앞뒤가 한글이 아니거나 조사·직함이 이어지면 그 낱말이다 (match_is_whole)
  · 붙여 쓴 자리는 형태소 분석으로 쪼개 본다. '발표자|강산'은 경계가 맞고,
    '강산성'·'교수|연구실'은 맞지 않는다

형태소 분석기가 없으면 이 판정을 할 수 없다. 그때는 예전처럼 치환기는 띄어쓰기만,
게이트는 글자 그대로 본다 — 막히는 문서가 생기더라도 조용히 새는 쪽보다 낫다.
"""

from __future__ import annotations

from ..model import CAT_DEPT, CAT_ORG, CAT_PERSON
from .registry import match_is_whole

# 낱말 경계를 따져야 하는 구분 — 짧은 한글 값이 다른 낱말에 박히는 것을 막는다
BOUNDED = frozenset({CAT_PERSON, CAT_ORG, CAT_DEPT})

# 이 길이 이상이면 앞뒤에 한글이 붙어 있어도 그 값으로 인정한다.
# 슬라이드·표에서는 '발표자홍길동'처럼 띄어쓰기 없이 붙는 일이 흔해서, 경계를
# 엄격히 따지면 이미 민감하다고 판정한 값이 그대로 남는다(실측에서 확인).
# 더 긴 값을 먼저 처리하므로 '김민'이 '김민수' 안에서 잘리는 일은 생기지 않는다.
LOOSE_BOUNDARY_LENGTH = 3
MIN_WORD_LENGTH = 2

_analyzer = None
_analyzer_failed = False
_cache: dict[str, frozenset[int]] = {}
_CACHE_LIMIT = 4096


def _is_hangul(ch: str) -> bool:
    return "가" <= ch <= "힣"


def _get_analyzer():
    """사용자 사전을 쓰지 않는 분석기.

    탐지용 분석기에는 그 문서의 이름이 등록돼 있다. 그것으로 쪼개면 '강산'을 아는
    탓에 '강산성'도 '강산|성'으로 갈라, 물음 자체가 성립하지 않는다.
    """
    global _analyzer, _analyzer_failed
    if _analyzer_failed:
        return None
    if _analyzer is None:
        try:
            from kiwipiepy import Kiwi
        except ImportError:
            _analyzer_failed = True
            return None
        _analyzer = Kiwi()
    return _analyzer


def available() -> bool:
    return _get_analyzer() is not None


def needs_word_check(value: str, category: str) -> bool:
    """낱말 경계를 따져야 하는 값인가 (두 글자 한글 인명·기관·부서).

    한 글자 값('주 교수'의 성 '주')은 대상이 아니다. 치환기도 게이트도 한 글자는
    문서 전체에서 찾지 않는다 — 찾으면 '다음 주까지'가 전부 걸린다.
    """
    return (category in BOUNDED and MIN_WORD_LENGTH <= len(value) < LOOSE_BOUNDARY_LENGTH
            and all(_is_hangul(ch) for ch in value))


def _token_boundaries(run: str) -> frozenset[int] | None:
    if run in _cache:
        return _cache[run]
    analyzer = _get_analyzer()
    if analyzer is None:
        return None
    try:
        tokens = analyzer.tokenize(run)
    except Exception:
        return None
    marks = {0, len(run)}
    for token in tokens:
        marks.add(token.start)
        marks.add(token.start + token.len)
    result = frozenset(marks)
    if len(_cache) < _CACHE_LIMIT:
        _cache[run] = result
    return result


def is_occurrence(text: str, start: int, end: int, category: str) -> bool:
    """text[start:end] 가 그 값으로 쓰인 자리인가 (다른 낱말의 일부가 아닌가)."""
    if match_is_whole(text, start, end, category):
        return True
    # 붙여 쓴 자리 — 한글이 이어진 구간을 통째로 쪼개 경계가 맞는지 본다
    left = start
    while left > 0 and _is_hangul(text[left - 1]):
        left -= 1
    right = end
    while right < len(text) and _is_hangul(text[right]):
        right += 1
    marks = _token_boundaries(text[left:right])
    if marks is None:
        return False
    return (start - left) in marks and (end - left) in marks


def occurrences(text: str, value: str, category: str) -> list[int]:
    """value 가 그 낱말로 쓰인 시작 위치들."""
    out: list[int] = []
    cursor = text.find(value)
    while cursor >= 0:
        if is_occurrence(text, cursor, cursor + len(value), category):
            out.append(cursor)
        cursor = text.find(value, cursor + 1)
    return out
