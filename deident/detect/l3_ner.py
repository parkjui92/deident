"""L3 — 표에 없는 이름·기관을 형태소 분석과 문맥 단서로 찾는다.

역할은 보조다. 실측해 보면 kiwipiepy 는 사전에 없는 이름을 통째로 쪼갠다
('박서준' → 박/NNP + 서/NNG + 준/NNG). 그래서 L2 가 표에서 건진 이름을
사용자 사전에 등록한 뒤 분석하고, 그래도 남는 이름만 여기서 줍는다.
반대로 '이 교수'의 '이'를 고유명사로 잘못 붙이기도 하므로 성씨 게이트와
중의성 규칙으로 걸러 낸다.

형태소 분석기가 놓치는 자리는 문맥 단서로 메운다 — 본문 이름표('참석: …'),
이름 뒤 직함('육동현 단장'), 이름 앞 신분('석사과정 황보람'), 이름만 부른 표기
('지훈이', '세린 씨'). 단서 규칙은 과탐이 쉬워서('교수 연구실'의 '연구실'),
후보가 사전에 있는 보통 낱말이면 버린다. 그 판정에 형태소 분석기를 쓴다.
"""

from __future__ import annotations

import re

from ..config import Config
from ..entity import boundary
from ..entity.registry import (
    Registry, match_is_whole, resolve_surname_title, surname_is_ambiguous,
)
from ..kdata import (
    COMPANY_CUES, COMPOUND_SURNAMES, GIVEN_SYLLABLES, HONORIFICS, INLINE_PERSON_LABELS,
    ORG_ABBREVIATIONS, ORG_ABBREVIATIONS_LATIN, ORG_SHORT_MIN_PREFIX, ORG_STOPWORDS,
    ORG_SUFFIXES, ORG_SUFFIXES_SHORT, PARTICLES, PLACE_NAMES, REFERENCE_MARKERS,
    ROLE_PREFIXES, ROLE_WORDS, SURNAMES, TITLE_ALTERNATION, TITLES, UNIT_CHARS,
)
from ..model import (
    CAT_DEPT, CAT_ORG, CAT_PERSON, GRADE_CERTAIN, GRADE_LIKELY, Finding, Segment,
)

_kiwi = None
_kiwi_failed = False
_kiwi_words: frozenset[str] = frozenset()   # 지금 분석기 사용자 사전에 올라 있는 이름

# 같은 줄 안의 공백 (탭·줄바꿈은 노드 경계다 — l1_patterns 와 같은 규칙)
_S = "[  　]"

_COMPOUND = sorted((s for s in COMPOUND_SURNAMES if len(s) == 2), reverse=True)
_SUR = "(?:" + "|".join(_COMPOUND) + "|[가-힣])"
_PARTICLE = "(?:" + "|".join(sorted(PARTICLES, key=len, reverse=True)) + ")"
# 직함 뒤 꼬리: '교수님께서', '단장이', '박사가' — 그 뒤에 한글이 더 오면 다른 낱말이다
_TAIL = rf"(?:님)?{_PARTICLE}?(?![가-힣])"

# 사람 말고도 흔히 가리키는 직함·호칭. '반도체 시장', '내부 감사'처럼 앞말이 사람이
# 아닌 경우가 훨씬 많아서, 이름을 새로 찾아내는 단서로는 쓰지 않는다.
WEAK_TITLES = frozenset({
    "시장", "지사", "의원", "감사", "이사", "대리", "주임", "고문", "간사", "강사",
    "기자", "책임", "선임", "수석", "군수", "석사", "님", "군", "양",
})
_STRONG = sorted((t for t in TITLES if t not in WEAK_TITLES), key=len, reverse=True)
_STRONG_ALT = "|".join(_STRONG)
_ROLE_ALT = "|".join(sorted((r for r in ROLE_WORDS if r not in WEAK_TITLES),
                            key=len, reverse=True))
_HONORIFIC_ALT = "|".join(sorted(HONORIFICS, key=len, reverse=True))
_ROLE_SET = frozenset(ROLE_WORDS) | frozenset(TITLES)

# 숫자 뒤 낱자('10명 위원', '3기 위원')는 성씨가 아니다
RE_SURNAME_TITLE = re.compile(
    rf"(?<![가-힣0-9])({_SUR}){_S}+(?:{TITLE_ALTERNATION}){_TAIL}")
RE_NAME_TITLE = re.compile(
    rf"(?<![가-힣])([가-힣]{{3,4}}?){_S}?(?:{_STRONG_ALT}){_TAIL}")
RE_ROLE_NAME = re.compile(
    rf"(?<![가-힣])(?:{_ROLE_ALT}){_S}+([가-힣]{{3,4}}?){_PARTICLE}?(?![가-힣])")
# '실장: 방극현'처럼 직함·신분어 뒤에 쌍점이 오는 것도 이름표다
_LABELS = sorted(set(INLINE_PERSON_LABELS) | set(_STRONG)
                 | {r for r in ROLE_WORDS if r not in WEAK_TITLES}, key=len, reverse=True)
RE_INLINE_LABEL = re.compile(
    r"(?<![가-힣A-Za-z])(" + "|".join(re.escape(label) for label in _LABELS)
    + rf"){_S}*[:：]{_S}*([^\t\n/|;；]+)")
RE_PAYEE = re.compile(rf"예금주{_S}*[:：]?{_S}*([가-힣]{{2,4}})(?![가-힣])")
RE_HANGUL_TOKEN = re.compile(r"(?<![가-힣])[가-힣]{2,6}(?![가-힣])")
RE_ENCLOSED = re.compile(r"\([^()]*\)|<[^<>]*>|\[[^\[\]]*\]")

# 영문 이름 — 호칭이 앞에 있거나, 한글 직함이 뒤따르거나, 전자우편 머리글에 있을 때만
_EN_UP, _EN_LO = "A-ZÀ-ÖØ-ÞĀ-Ž", "a-zß-öø-ÿā-ž'"
_EN_NAME = rf"[{_EN_UP}][{_EN_LO}]+(?:[- ][{_EN_UP}][{_EN_LO}]*\.?){{1,3}}"
RE_EN_HONORIFIC = re.compile(
    rf"(?<![A-Za-z])(?:Prof\.?|Professor|Dr\.?|Mr\.?|Ms\.?|Mrs\.?){_S}+({_EN_NAME})")
RE_EN_TITLE = re.compile(rf"(?<![A-Za-z])({_EN_NAME})\)?{_S}*(?:{_STRONG_ALT}){_TAIL}")
RE_EN_HEADER = re.compile(
    rf"(?<![A-Za-z가-힣])(?:From|To|Cc|보낸{_S}?사람|받는{_S}?사람){_S}*[:：]{_S}*\"?({_EN_NAME})")
RE_EN_ROLE = re.compile(rf"(?<![가-힣])(?:{_ROLE_ALT}){_S}+({_EN_NAME})")
RE_TRANSLITERATED = re.compile(rf"([가-힣]{{2,6}}(?:{_S}[가-힣]{{1,6}})?){_S}?\($")
# 한글로 옮겨 적은 외국인 이름 ('솔바켄 박사', '후지사와 켄 박사') — 사전에 없는 말이
# 사람에게만 붙는 직함 앞에 올 때
RE_FOREIGN_TITLE = re.compile(
    rf"(?<![가-힣])([가-힣]{{2,6}}(?:{_S}[가-힣]{{1,3}})?){_S}"
    rf"(?:박사님|교수님|박사|교수|연구원|학생|선생님){_TAIL}")
EN_ORG_WORDS = frozenset({
    "University", "Institute", "College", "Laboratory", "Lab", "Labs", "Center",
    "Centre", "Council", "Agency", "Ministry", "Department", "School", "Corp",
    "Inc", "Company", "Foundation", "Association", "System", "Program", "Project",
    "National", "International", "Research", "Technology", "Science", "Sciences",
    "Policy", "Innovation", "Advanced", "Global", "Korea", "Korean",
})

# 인용 표기 — 공개된 저자명은 건드리지 않는다
RE_CITATION = re.compile(r"\([^()]{0,60}?\d{4}[a-z]?(?:\s*[:,]\s*(?:pp?\.\s*)?[\d\-–]+)?\)")
RE_CITATION_NARRATIVE = re.compile(
    r"(?:[가-힣]{2,4}\s?[·,와과]\s?)*[가-힣]{2,4}(?:\s*(?:외|등))?(?:\s*\d+\s*인)?\s*\(\d{4}[a-z]?\)")

RE_ORG = re.compile(
    r"(?<![가-힣A-Za-z])([가-힣A-Za-z0-9]{2,15}(?:"
    + "|".join(re.escape(s) for s in ORG_SUFFIXES) + r"))"
)
# 한 글자 접미사(부·처·청)는 앞말이 길 때만 — '연락처'가 기관이 되는 것을 막는다
RE_ORG_SHORT = re.compile(
    rf"(?<![가-힣A-Za-z])([가-힣]{{{ORG_SHORT_MIN_PREFIX},15}}(?:"
    + "|".join(re.escape(s) for s in ORG_SUFFIXES_SHORT) + r"))(?![가-힣])"
)
RE_ORG_PREFIXED = re.compile(rf"(?:㈜|\(주\)|주식회사){_S}?([가-힣A-Za-z0-9]{{2,15}})")
RE_ORG_QUOTED = re.compile(
    rf"(?:{'|'.join(COMPANY_CUES)}){_S}*['‘\"“]([^'’\"”\t\n]{{2,15}})['’\"”]")
RE_ORG_ABBR = re.compile(
    r"(?<![가-힣])(" + "|".join(sorted(ORG_ABBREVIATIONS, key=len, reverse=True)) + r")")
RE_ORG_ABBR_LATIN = re.compile(
    r"(?<![A-Za-z0-9])(" + "|".join(sorted(ORG_ABBREVIATIONS_LATIN, key=len, reverse=True))
    + r")(?![A-Za-z0-9])")
_EN_WORD = r"[A-Z][A-Za-z]+"
RE_ORG_EN = re.compile(
    rf"(?<![A-Za-z])(University of {_EN_WORD}(?: {_EN_WORD})?(?:,? {_EN_WORD})?"
    rf"|(?:{_EN_WORD} ){{1,3}}(?:University|Institute|College|Laboratory|Laboratories)"
    rf"(?: (?:of|for) {_EN_WORD}(?: (?:and|&) {_EN_WORD})?(?: {_EN_WORD})?)?"
    rf"|UC {_EN_WORD}(?: {_EN_WORD})?)(?![A-Za-z])")
RE_UNIV = re.compile(r"(?<![가-힣])([가-힣]{2,8})대학교")
# '영주시 보건소' — 지역 이름과 띄어 쓴 관공서
RE_ORG_LOCAL = re.compile(
    rf"(?<![가-힣])([가-힣]{{2,5}}(?:시|군|구|도){_S}?(?:보건소|시청|군청|구청|도청|교육청"
    rf"|교육지원청|경찰서|소방서|세무서|우체국|주민센터|행정복지센터|의회))")
RE_DEPT_AFTER_ORG = re.compile(
    rf"{_S}+([가-힣A-Za-z0-9]{{1,12}}(?:학과|학부|전공|대학원|대학|연구소|연구실|연구단"
    rf"|센터|본부|팀|실|단|처|국|부|과))(?![가-힣])")


def _get_kiwi(registry: Registry | None):
    """kiwipiepy 를 준비한다. 없으면 조용히 비활성 — 나머지 계층은 계속 돈다.

    사용자 사전에는 **이 문서의** 이름만 올라 있어야 한다. 분석기는 프로세스에
    하나뿐이고 사용자 사전은 덜어낼 수 없어서, 그냥 쌓으면 앞 문서에서 배운 이름이
    뒤 문서의 탐지를 바꾼다 — 같은 파일의 결과가 함께 넘긴 파일과 순서에 따라
    달라진다(실측). 그래서 이 문서에 없는 이름이 올라 있으면 분석기를 새로 만든다.
    """
    global _kiwi, _kiwi_failed, _kiwi_words
    if _kiwi_failed:
        return None
    try:
        from kiwipiepy import Kiwi
    except ImportError:
        _kiwi_failed = True
        return None

    if registry is None:          # 쓸 수 있는지만 묻는 호출 — 사전은 건드리지 않는다
        if _kiwi is None:
            _kiwi, _kiwi_words = Kiwi(), frozenset()
        return _kiwi

    wanted = frozenset(r.canonical for r in registry.by_category(CAT_PERSON))
    if _kiwi is None or not _kiwi_words <= wanted:
        _kiwi, _kiwi_words = Kiwi(), frozenset()
    for name in sorted(wanted - _kiwi_words):
        try:
            _kiwi.add_user_word(name, "NNP")
        except Exception:
            pass
    _kiwi_words = wanted
    return _kiwi


def is_header_cell(seg: Segment) -> bool:
    """표 머리글 칸은 이름표지 값이 아니다. 여기를 치우면 표가 읽히지 않는다."""
    return seg.table_ctx is not None and seg.table_ctx.is_header


# ── 이름 모양 판정 ───────────────────────────────────────────────────────────
def _parses(value: str) -> list[tuple[str, str]]:
    """(성, 이름) 으로 읽을 수 있는 방법들. '황보람'은 황보+람, 황+보람 둘 다다."""
    out: list[tuple[str, str]] = []
    for compound in _COMPOUND:
        if value.startswith(compound) and 1 <= len(value) - 2 <= 2:
            out.append((compound, value[2:]))
    if value[:1] in SURNAMES and 1 <= len(value) - 1 <= 2:
        out.append((value[:1], value[1:]))
    return out


def _surname_of(value: str) -> str:
    for compound in _COMPOUND:
        if value.startswith(compound):
            return compound
    return value[:1]


def _is_name_shape(value: str) -> bool:
    if not all("가" <= ch <= "힣" for ch in value):
        return False
    return bool(_parses(value))


def _given_of(name: str) -> str | None:
    """이름만 부를 때 쓰일 두 글자. 없으면 None."""
    for _, given in _parses(name):
        if len(given) == 2:
            return given
    return None


_COMMON_CACHE: dict[tuple[str, bool], bool] = {}


def _dictionary_word(kiwi, value: str) -> bool:
    """사전의 말로 다 풀리는가 ('주기' → 주+기, '하늘') — 이름으로만 쓰이는 말이 아니다."""
    try:
        tokens = kiwi.tokenize(value)
    except Exception:
        return False
    if not tokens or any(t.oov for t in tokens):
        return False
    return not (len(tokens) == 1 and tokens[0].tag == "NNP")


def _looks_common(kiwi, value: str, strict: bool = True) -> bool:
    """이름 후보가 실은 보통 낱말인가.

    '연구실'(한 낱말), '정원을'(낱말+조사), '임용률'(낱말+접미)을 걸러 낸다.
    이름일 수 있는 꼴은 남긴다 — 성 뒤에 낱말이 오는 '봉+지수'·'주+하늘', 앞 두
    글자가 우연히 낱말인 '구슬+기'·'박수+진'. 여기서 막으면 그런 이름을 가진
    사람은 영영 못 잡는다.

    strict=False 는 '사전에 통째로 오른 낱말인가'만 본다. 형태소 분석기가 문맥에서
    고유명사로 본 토큰에 쓴다 — 문맥 판단을 낱말 분해로 뒤집지 않기 위해서다.
    """
    if kiwi is None:
        return False
    key = (value, strict)
    if key in _COMMON_CACHE:
        return _COMMON_CACHE[key]
    verdict = False
    try:
        tokens = kiwi.tokenize(value)
    except Exception:
        tokens = []
    if tokens:
        first = tokens[0]
        if len(tokens) == 1:
            verdict = not first.oov and first.tag != "NNP"
        elif not strict:
            verdict = False
        elif first.tag == "XPN":
            verdict = True                      # '신+소재' — 접두사로 시작한다
        elif first.len >= 2 and not first.oov and first.tag != "NNP":
            last = tokens[-1]
            # '정원+을'(조사), '반영+하+ㄴ'(어미), '임용+률'·'정책+실'(단위 글자).
            # 성 한 글자로 시작하는 꼴('김+은+채')은 여기 오지 않는다.
            verdict = last.tag[:1] in ("J", "E") or (
                len(tokens) == 2 and last.form in UNIT_CHARS)
    _COMMON_CACHE[key] = verdict
    return verdict


def _unknown_word(kiwi, value: str) -> bool:
    """사전에 없는 말인가 — 옮겨 적은 외국인 이름이 대개 그렇다 ('솔바켄', '민코아')."""
    try:
        tokens = kiwi.tokenize(value.replace(" ", ""))
    except Exception:
        return False
    if not tokens:
        return False
    if any(t.oov for t in tokens):
        return True
    # '민코아' → 민+코+아. 낱자 명사로만 풀리는 세 글자 이상도 사전에 없는 말이다.
    # ('말고' → 말+고 는 어미가 섞여 있어 해당하지 않는다)
    return (len(value) >= 3 and all(t.len == 1 and t.tag in ("NNG", "NNP", "NR")
                                    for t in tokens))


def _name_like_token(kiwi, value: str) -> bool:
    """이름의 한 마디로 볼 수 있는가 — 사전에 없거나 고유명사로 읽히는 말."""
    try:
        tokens = kiwi.tokenize(value)
    except Exception:
        return False
    return len(tokens) == 1 and (tokens[0].oov or tokens[0].tag == "NNP")


def _plausible_given(value: str) -> bool:
    """성을 뗀 나머지가 한국어 이름에 쓰이는 음절로만 되어 있는가."""
    return any(all(ch in GIVEN_SYLLABLES for ch in given) for _, given in _parses(value))


def _acceptable_name(value: str, kiwi) -> bool:
    return (_is_name_shape(value) and value not in PLACE_NAMES
            and value not in _ROLE_SET and not _looks_common(kiwi, value))


# ── 건너뛸 구간 ──────────────────────────────────────────────────────────────
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


def _citation_spans(text: str) -> list[tuple[int, int]]:
    """본문 인용 표기 구간 — '(홍길동 외, 2020)', '홍길동(2020)', '홍길동·김철수(2021)'."""
    spans = [(m.start(), m.end()) for m in RE_CITATION.finditer(text)]
    spans.extend((m.start(), m.end()) for m in RE_CITATION_NARRATIVE.finditer(text))
    return spans


def _inside(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    return any(s <= start and end <= e for s, e in spans)


# ── 후보 생성 ────────────────────────────────────────────────────────────────
def _candidates_from_kiwi(kiwi, text: str) -> list[tuple[int, int, str, bool]]:
    """NNP 토큰과, 쪼개진 이름을 다시 붙인 후보. 마지막 값은 이어 붙였는지."""
    out: list[tuple[int, int, str, bool]] = []
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
            out.append((start, end, form, True))
        out.append((token.start, token.start + token.len, token.form, False))
    return out


def _label_candidates(text: str, kiwi) -> list[tuple[int, int, str]]:
    """본문 이름표 뒤의 이름들 — '참석: 민지훈, 오세린(온라인), 강산'."""
    out: list[tuple[int, int, str]] = []
    for m in RE_INLINE_LABEL.finditer(text):
        base = m.start(2)
        # 괄호·꺾쇠 안은 소속·주소다. 길이를 지켜 지워야 위치가 어긋나지 않는다.
        value = RE_ENCLOSED.sub(lambda e: " " * len(e.group()), m.group(2))
        items: list[tuple[int, str]] = []
        cursor = 0
        for piece in re.split(r"([,，、])", value):
            if piece not in (",", "，", "、"):
                items.append((cursor, piece))
            cursor += len(piece)

        sure: list[tuple[int, int, str]] = []
        short: list[tuple[int, int, str]] = []
        for offset, item in items:
            tokens = [t for t in RE_HANGUL_TOKEN.finditer(item)
                      if t.group() not in _ROLE_SET]
            for token in tokens:
                form = token.group()
                span = (base + offset + token.start(), base + offset + token.end(), form)
                if len(form) >= 3 and _acceptable_name(form, kiwi):
                    sure.append(span)
                elif (len(form) == 2 and _is_name_shape(form) and len(tokens) == 1
                      and form not in PLACE_NAMES):
                    short.append(span)   # 항목이 그 두 글자뿐일 때만 후보
        out.extend(sure)
        # 두 글자 이름은 낱말과 구별이 안 된다('강산'). 같은 목록에 이름이 둘 이상
        # 확인됐을 때만 — 명단이라는 사실이 근거가 된다.
        if len(sure) >= 2:
            out.extend(short)

    for m in RE_PAYEE.finditer(text):
        form = m.group(1)
        if _is_name_shape(form) and form not in PLACE_NAMES and not _looks_common(kiwi, form):
            out.append((m.start(1), m.end(1), form))
    return out


def _cue_candidates(text: str, kiwi) -> list[tuple[int, int, str]]:
    """직함·신분 단서로 찾는 이름. 형태소 분석기가 없으면 과탐을 걸러 낼 수 없어 쓰지 않는다."""
    if kiwi is None:
        return []
    out: list[tuple[int, int, str]] = []
    for pattern in (RE_NAME_TITLE, RE_ROLE_NAME):
        for m in pattern.finditer(text):
            form = m.group(1)
            if len(form) == 4 and not any(form.startswith(c) for c in _COMPOUND):
                continue   # 네 글자는 복성일 때만 이름이다
            if form[:2] in _ROLE_SET:
                continue   # '박사후 연구원'의 '박사후' — 직함으로 시작하는 말
            if _acceptable_name(form, kiwi) and _plausible_given(form):
                out.append((m.start(1), m.end(1), form))
    return out


def _foreign_candidates(text: str, kiwi) -> list[tuple[int, int, str]]:
    """한글로 옮겨 적은 외국인 이름. 사전에 없는 말이어야 한다 ('지도 교수'는 아니다)."""
    if kiwi is None:
        return []
    out: list[tuple[int, int, str]] = []
    for m in RE_FOREIGN_TITLE.finditer(text):
        form = m.group(1)
        parts = form.split()
        head = parts[0]
        if head in _ROLE_SET or head in PLACE_NAMES or _is_name_shape(form):
            continue   # 한국식 이름은 다른 규칙이 맡는다
        if not _unknown_word(kiwi, head):
            continue
        if len(parts) == 2 and not _name_like_token(kiwi, parts[1]):
            # '후지사와 켄 박사'는 두 마디가 이름이지만 '○○ 및 학생'은 아니다
            start = m.start(1)
            out.append((start, start + len(head), head))
            continue
        out.append((m.start(1), m.end(1), form))
    return out


def _english_candidates(text: str) -> list[tuple[int, int, str]]:
    out: list[tuple[int, int, str]] = []
    for pattern in (RE_EN_HONORIFIC, RE_EN_TITLE, RE_EN_HEADER, RE_EN_ROLE):
        for m in pattern.finditer(text):
            form = m.group(1).rstrip(". ")
            if any(word in EN_ORG_WORDS for word in re.split(r"[- ]", form)):
                continue
            start = m.start(1)
            out.append((start, start + len(form), form))
            # '마이클 첸(Michael Chen) 박사' — 괄호 앞 한글 표기도 같은 사람이다
            before = RE_TRANSLITERATED.search(text[:start])
            if before and pattern is RE_EN_TITLE:
                out.append((before.start(1), before.end(1), before.group(1)))
    return out


def _person(seg: Segment, start: int, end: int, text: str, note: str,
            grade: str = GRADE_LIKELY, entity_id: str | None = None) -> Finding:
    return Finding(seg_id=seg.seg_id, start=start, end=end, text=text,
                   category=CAT_PERSON, grade=grade, detectors={"L3"},
                   entity_id=entity_id, note=note, part=seg.part)


def detect_people(segments: list[Segment], config: Config,
                  registry: Registry) -> list[Finding]:
    if not config.category_enabled(CAT_PERSON):
        return []

    kiwi = _get_kiwi(registry)
    reference_segments = _in_reference_section(segments)
    known = {r.canonical for r in registry.by_category(CAT_PERSON)}
    out: list[Finding] = []
    # 이 층이 단서(이름표·직함·신분)와 함께 찾은 성명. 성+직함·이름만 부른 표기를
    # 이 사람들에게 잇는다. 형태소 분석기만 근거인 후보는 넣지 않는다 — 공개 보고서
    # 실측에서 '장주기·전주기'가 이름으로 잡힌 뒤 '주기'까지 번졌다.
    found_names: set[str] = set()
    claimed: dict[str, list[tuple[int, int]]] = {}

    def claim(seg: Segment, start: int, end: int) -> bool:
        spans = claimed.setdefault(seg.seg_id, [])
        if any(start < e and s < end for s, e in spans):
            return False
        spans.append((start, end))
        return True

    for seg in segments:
        text = seg.text
        if not text.strip() or is_header_cell(seg) or seg.seg_id in reference_segments:
            continue
        citations = _citation_spans(text)

        # (1) 본문 이름표 — 가장 강한 단서
        for start, end, form in _label_candidates(text, kiwi):
            if form in known or not claim(seg, start, end):
                continue
            out.append(_person(seg, start, end, form, "본문 이름표"))
            if len(form) >= 3:
                found_names.add(form)

        # (2) 형태소 후보
        if kiwi is not None:
            for start, end, form, joined in _candidates_from_kiwi(kiwi, text):
                if form in known:
                    continue          # 대장 전파(L2)가 이미 확실 등급으로 잡는다
                if not _is_name_shape(form) or form in PLACE_NAMES:
                    continue
                # '나스닥 시장'의 '시장'은 직함이 아니다 — 사람에게만 붙는 직함만 근거로 삼는다
                titled = bool(re.match(rf"{_S}*(?:{_STRONG_ALT}|씨)", text[end:end + 8]))
                # 문맥에서 고유명사로 읽혔고 직함까지 뒤따르면 낱말 분해로 뒤집지 않는다.
                # 그 밖에는 '기술+성', '정보+화' 같은 낱말 꼴을 버린다.
                if form.endswith(("읍", "면")) or _looks_common(
                        kiwi, form, strict=joined or not titled):
                    continue          # '부여읍', '정보화' — 땅 이름·보통 낱말
                if not match_is_whole(text, start, end, CAT_PERSON):
                    continue
                if len(form) == 2 and not titled:
                    continue          # 성+외자 2글자는 직함이 뒤따를 때만 인정한다
                if not titled and not _plausible_given(form):
                    continue          # '사노피', '유전체' — 이름에 안 쓰는 음절
                if _inside(start, end, citations) or not claim(seg, start, end):
                    continue
                out.append(_person(seg, start, end, form, "형태소 후보"))
                if len(form) >= 3 and titled:
                    found_names.add(form)   # 단서 없는 후보는 다른 표기로 넓히지 않는다

        # (3) 직함·신분 단서 — 형태소 분석기가 낱자로 쪼갠 이름을 되살린다
        for start, end, form in _cue_candidates(text, kiwi):
            if form in known or _inside(start, end, citations):
                continue
            if not claim(seg, start, end):
                continue
            out.append(_person(seg, start, end, form, "직함·신분 단서"))
            found_names.add(form)

        # (4) 영문 이름, 한글로 옮겨 적은 외국인 이름
        for start, end, form in _english_candidates(text):
            if _inside(start, end, citations) or not claim(seg, start, end):
                continue
            out.append(_person(seg, start, end, form, "영문 이름"))
        for start, end, form in _foreign_candidates(text, kiwi):
            if _inside(start, end, citations) or not claim(seg, start, end):
                continue
            out.append(_person(seg, start, end, form, "외국인 이름 (직함 단서)"))

    # (5) 성 + 직함 ('김 박사') — 대장에 없으면 이 층이 찾은 성명으로라도 잇는다
    by_surname: dict[str, set[str]] = {}
    for name in found_names:
        by_surname.setdefault(_surname_of(name), set()).add(name)

    for seg in segments:
        text = seg.text
        if not text.strip() or is_header_cell(seg) or seg.seg_id in reference_segments:
            continue
        for m in RE_SURNAME_TITLE.finditer(text):
            surname = m.group(1)
            if len(surname) == 1 and surname not in SURNAMES:
                continue
            record = resolve_surname_title(surname, registry)
            local = by_surname.get(surname, set())
            if record is None and len(local) != 1 and surname_is_ambiguous(surname):
                continue   # '이 교수'의 '이'는 지시관형사일 수 있다
            if not claim(seg, m.start(1), m.end(1)):
                continue
            if record is not None:
                grade, note = GRADE_CERTAIN, "성+직함 (대장 연결)"
            elif len(local) == 1:
                grade, note = GRADE_LIKELY, "성+직함 (본문 성명 연결)"
            else:
                grade, note = GRADE_LIKELY, "성+직함 (미연결)"
            out.append(_person(seg, m.start(1), m.end(1), surname, note, grade,
                               record.entity_id if record else None))

    # (6) 이름만 부른 표기 ('지훈이', '세린 씨')
    out.extend(_given_name_findings(segments, known | found_names, kiwi, claim,
                                    reference_segments))
    return out


def _given_name_findings(segments: list[Segment], names: set[str], kiwi, claim,
                         skip_segments: set[str]) -> list[Finding]:
    """성명이 확인된 사람을 이름만으로 부른 자리.

    두 글자 이름은 다른 낱말 속에도 들어 있다('수연' ⊂ '교수연구실'). 치환기와
    게이트는 그런 자리를 낱말 경계로 가려낸다(entity/boundary.py). 다만 그 판정에는
    형태소 분석기가 필요해서, 분석기가 없을 때는 게이트가 글자 그대로 찾는다 —
    그 환경에서는 문서 안의 모든 출현이 설명될 때만 등록한다. 그러지 않으면 정상
    문서가 게이트를 통과하지 못한다.
    """
    must_explain = not boundary.available()
    out: list[Finding] = []
    for name in sorted(names):
        given = _given_of(name)
        if given is None:
            continue
        surname = name[:-2]
        common = kiwi is not None and _dictionary_word(kiwi, given)
        # 호칭·직함이 붙거나('세린 씨', '동건 대리님'), 이름 뒤 '이'에 조사가 이어지는
        # 꼴('보람이는')은 보통 낱말로는 쓰이지 않는다 — 낱말과 같은 이름에도 허용한다.
        strong = re.compile(
            rf"(?<![가-힣]){re.escape(given)}(?:{_S}?(?:{_HONORIFIC_ALT}|{_STRONG_ALT}"
            rf"|(?:{TITLE_ALTERNATION})님){_PARTICLE}?|이(?:는|가|도|를|의|랑|한테|에게|와|만))"
            rf"(?![가-힣])")
        loose = re.compile(rf"(?<![가-힣]){re.escape(given)}(?:이)?{_PARTICLE}?(?![가-힣])")

        hits: list[tuple[Segment, int]] = []
        explained = True
        for seg in segments:
            text = seg.text
            cursor = text.find(given)
            while cursor >= 0:
                if text[max(0, cursor - len(surname)):cursor + 2] == name:
                    pass    # 성명 전체의 일부 — 성명째로 치환된다
                elif strong.match(text, cursor) or (not common and loose.match(text, cursor)):
                    if seg.seg_id not in skip_segments and not is_header_cell(seg):
                        hits.append((seg, cursor))
                elif must_explain:
                    explained = False
                    break
                cursor = text.find(given, cursor + 1)
            if not explained:
                break
        if not explained:
            continue
        for seg, start in hits:
            if claim(seg, start, start + 2):
                out.append(_person(seg, start, start + 2, given, "이름만 부른 표기"))
    return out


# ── 기관 ─────────────────────────────────────────────────────────────────────
def _role_noun(value: str) -> bool:
    """'박사후연구원', '외부연구원' — 기관이 아니라 신분이다."""
    if not value.endswith("연구원"):
        return False
    return value[:-3].endswith(ROLE_PREFIXES)


# 기관 바로 뒤에 오지만 부서가 아닌 말
DEPT_STOPWORDS = frozenset({
    "연구팀", "연구실", "연구단", "연구부", "사업단", "본부", "센터", "소속", "출신",
    "내부", "외부", "전체", "일부", "전부", "간부", "직원", "관계자", "담당자", "대학",
})
_DEPT_SHORT = ("팀", "실", "단", "처", "국", "부", "과")


def _is_department(kiwi, value: str) -> bool:
    """기관 뒤에 온 말이 부서 이름인가.

    '학과·센터·본부'처럼 긴 접미사는 그대로 믿는다. 한 글자 접미사는 조사와 겹친다 —
    '재단 사업과 연계'의 '사업과'는 부서가 아니다. 그래서 형태소 분석으로 마지막
    조각이 접미사 그 글자인지 확인하고, 분석기가 없으면 받지 않는다.
    """
    if value in DEPT_STOPWORDS or value in _ROLE_SET:
        return False
    if not value.endswith(_DEPT_SHORT):
        return True
    if value.endswith(("학과", "학부", "연구단")):
        return True
    if kiwi is None or len(value) < 3:
        return False
    try:
        tokens = kiwi.tokenize(value)
    except Exception:
        return False
    if not tokens:
        return False
    last = tokens[-1]
    if len(tokens) == 1:
        return True                    # '총무과' — 통째로 한 낱말
    return last.form == value[-1] and last.tag[:1] not in ("J", "E")


def detect_orgs(segments: list[Segment], config: Config,
                registry: Registry) -> list[Finding]:
    """접미사로 기관명을 찾는다. 형태소 분석보다 이쪽이 훨씬 정확하다."""
    if not config.category_enabled(CAT_ORG):
        return []

    kiwi = _get_kiwi(None)
    known = {r.canonical for r in registry.by_category(CAT_ORG)}
    universities = {m.group(1) for name in known for m in [RE_UNIV.fullmatch(name)] if m}
    want_dept = config.category_enabled(CAT_DEPT)
    out: list[Finding] = []

    def org(seg: Segment, start: int, end: int, note: str,
            category: str = CAT_ORG) -> None:
        out.append(Finding(
            seg_id=seg.seg_id, start=start, end=end, text=seg.text[start:end],
            category=category, grade=GRADE_LIKELY, detectors={"L3"},
            note=note, part=seg.part,
        ))

    def departments(seg: Segment, position: int) -> None:
        """기관 바로 뒤의 부서 — '서림대학교 산학협력단 연구비관리팀' 처럼 둘까지 이어진다."""
        if not want_dept:
            return
        for _ in range(2):
            m = RE_DEPT_AFTER_ORG.match(seg.text, position)
            if not m or not _is_department(kiwi, m.group(1)):
                return
            org(seg, m.start(1), m.end(1), "기관 뒤 학과·부서", CAT_DEPT)
            position = m.end(1)

    for seg in segments:
        if is_header_cell(seg):
            continue
        text = seg.text
        # 대장에 오른 기관은 L2 가 치환한다. 여기서는 그 뒤의 부서만 본다.
        for name in known:
            cursor = text.find(name)
            while cursor >= 0:
                departments(seg, cursor + len(name))
                cursor = text.find(name, cursor + 1)

        for pattern in (RE_ORG, RE_ORG_SHORT):
            for m in pattern.finditer(text):
                value = m.group(1)
                if value in ORG_STOPWORDS or _role_noun(value) or value.endswith("정부"):
                    continue          # '참여연구원' 같은 역할어, '중앙정부'
                univ = RE_UNIV.fullmatch(value)
                if univ:
                    universities.add(univ.group(1))
                if value in known:
                    continue
                if not match_is_whole(text, m.start(1), m.end(1), CAT_ORG):
                    continue
                org(seg, m.start(1), m.end(1), "기관 접미사")
                departments(seg, m.end(1))

        for m in RE_ORG_PREFIXED.finditer(text):
            org(seg, m.start(1), m.end(1), "회사 표지 뒤")
            departments(seg, m.end(1))
        for m in RE_ORG_LOCAL.finditer(text):
            org(seg, m.start(1), m.end(1), "지역 관공서")
        for m in RE_ORG_QUOTED.finditer(text):
            org(seg, m.start(1), m.end(1), "회사 단서 뒤 따옴표")
        for m in RE_ORG_ABBR.finditer(text):
            if match_is_whole(text, m.start(1), m.end(1), CAT_ORG):
                org(seg, m.start(1), m.end(1), "통용 약칭")
                departments(seg, m.end(1))
        for m in RE_ORG_ABBR_LATIN.finditer(text):
            org(seg, m.start(1), m.end(1), "통용 약칭")
            departments(seg, m.end(1))
        for m in RE_ORG_EN.finditer(text):
            org(seg, m.start(1), m.end(1), "영문 기관명")

    # '누리대학교'가 나온 문서의 '누리대' — 줄임 표기도 같은 기관이다
    for stem in sorted(universities, key=len, reverse=True):
        short = re.compile(rf"(?<![가-힣]){re.escape(stem)}대(?!학)")
        for seg in segments:
            if is_header_cell(seg):
                continue
            for m in short.finditer(seg.text):
                if match_is_whole(seg.text, m.start(), m.end(), CAT_ORG):
                    org(seg, m.start(), m.end(), "대학 줄임 표기")
                    departments(seg, m.end())
    return out


def detect(segments: list[Segment], config: Config,
           registry: Registry) -> list[Finding]:
    return detect_people(segments, config, registry) + detect_orgs(segments, config, registry)


def available() -> bool:
    return _get_kiwi(None) is not None
