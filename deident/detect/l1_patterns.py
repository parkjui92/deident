"""L1 — 정규식과 검증기로 잡는 구조적 개인정보.

설계 규칙 두 가지:
  1. 공백 문자 클래스에 \\t\\n 을 넣지 않는다. 추출기가 탭·줄바꿈을 편집 불가
     의사문자로 삽입하므로, 매치가 그 경계를 넘지 못하면 노드 경계를 침범하는
     치환이 구조적으로 발생하지 않는다.
  2. 체크섬은 신뢰도 표시일 뿐 탈락 근거가 아니다 (validators 참조).
"""

from __future__ import annotations

import re

from ..model import (
    CAT_ACCOUNT, CAT_ADDRESS, CAT_BIRTH, CAT_BIZNO, CAT_CARD, CAT_CORPNO,
    CAT_DRIVER, CAT_EMAIL, CAT_FOREIGN_ID, CAT_HANDLE, CAT_PASSPORT, CAT_PHONE,
    CAT_PROJECT_NO, CAT_RRN, CAT_TEL, CAT_VEHICLE, GRADE_CERTAIN, GRADE_LIKELY,
    Finding, Segment,
)
from . import validators as V

# 탭·줄바꿈을 제외한 '같은 줄 안의 공백'
S = r"[  　]"
SEP = rf"(?:{S}*[-–—.·]{S}*|{S}+)"


def context_text(seg: Segment) -> str:
    """문맥어를 찾을 범위.

    표 셀은 값만 들어 있고 '생년월일' 같은 이름표는 헤더 칸에 따로 있다.
    헤더를 문맥에 포함하지 않으면 표 안의 값은 영원히 문맥 없는 숫자로 남는다.
    """
    if seg.table_ctx and seg.table_ctx.header_text:
        return f"{seg.table_ctx.header_text} {seg.text}"
    return seg.text


def _f(seg: Segment, m: re.Match, category: str, grade: str = GRADE_CERTAIN,
       note: str = "", group: int = 0) -> Finding:
    return Finding(
        seg_id=seg.seg_id, start=m.start(group), end=m.end(group),
        text=m.group(group), category=category, grade=grade,
        detectors={"L1"}, note=note, part=seg.part,
    )


# ── 개별 탐지기 ─────────────────────────────────────────────────────────────
RE_RRN = re.compile(rf"(?<![0-9])(\d{{6}}){S}*[-–—]{S}*(\d{{7}})(?![0-9])")
RE_RRN_NOSEP = re.compile(r"(?<![0-9])(\d{6})(\d{7})(?![0-9])")


def detect_rrn(seg: Segment) -> list[Finding]:
    out: list[Finding] = []
    seen: set[tuple[int, int]] = set()
    for pattern in (RE_RRN, RE_RRN_NOSEP):
        for m in pattern.finditer(seg.text):
            if (m.start(), m.end()) in seen:
                continue
            body = m.group(1) + m.group(2)
            if not V.valid_birth_part(body[:6]):
                continue
            if V.foreign_id_ok(body):
                cat, note = CAT_FOREIGN_ID, "외국인등록번호 형태"
            elif V.rrn_shape_ok(body):
                cat = CAT_RRN
                note = "체크섬 통과" if V.rrn_checksum_ok(body) else "체크섬 불일치(2020.10 이후 발급분 가능)"
            else:
                continue
            # 구분자 없는 13자리는 다른 번호일 여지가 있어 등급을 낮춘다
            grade = GRADE_CERTAIN
            if pattern is RE_RRN_NOSEP and not V.rrn_checksum_ok(body):
                grade = GRADE_LIKELY
            seen.add((m.start(), m.end()))
            out.append(_f(seg, m, cat, grade, note))
    return out


# 지역번호를 괄호로 닫아 쓰는 표기('044)123-4567', '(02) 123-4567')도 받는다.
# 여는 괄호는 닫는 괄호가 짝으로 있을 때만 값에 넣는다 — 한쪽만 삼키면 문장에
# 짝 잃은 괄호가 남는다.
_MOBILE = r"0?1[016789]"
_AREA = r"0(?:2|[3-6][1-5]|70|50\d)"


_DASH = rf"{S}*[-–—.·]{S}*"


def _number(code: str) -> str:
    """괄호 없는 표기는 예전 그대로, 괄호로 닫은 표기는 국번 뒤에 줄표를 요구한다.

    '02) 2023 2024년'처럼 목록 번호 뒤에 연도가 이어지는 글이 전화번호가 되는 것을 막는다.
    """
    plain = rf"{code}{SEP}?\d{{3,4}}{SEP}?\d{{4}}"
    closed = rf"(?:\({code}\)|{code}\)){S}*\d{{3,4}}{_DASH}\d{{4}}"
    return rf"(?:{closed}|{plain})"


RE_PHONE = re.compile(rf"(?<![0-9])(?:\+?82{SEP}?)?{_number(_MOBILE)}(?![0-9])")
RE_TEL = re.compile(rf"(?<![0-9])(?:\+?82{SEP}?)?{_number(_AREA)}(?![0-9])")
# 국가번호가 붙은 해외 번호 ('+47 55 50 01 42'). 82는 위 두 패턴이 받는다.
RE_TEL_INTL = re.compile(
    r"(?<![0-9+])\+(?!82)\d{1,3}(?:[ \-]\(?\d{1,4}\)?){2,6}(?![0-9])")
# 내선 번호 — 번호만으로는 뜻이 없어 이름표를 요구한다
RE_EXTENSION = re.compile(
    rf"(?:내선|구내|교환|[Ee][Xx][Tt]\.?){S}*(?:번호)?{S}*[:：]?{S}*(\d{{3,5}})(?![0-9])"
)
# 숫자를 한글로 풀어 쓴 휴대전화 ('공일공-이삼사오-육칠팔구')
_KDIGIT = "공영일이삼사오육칠팔구"
RE_PHONE_SPOKEN = re.compile(
    rf"(?<![가-힣])공일[공일육칠팔구]{SEP}?[{_KDIGIT}]{{3,4}}{SEP}?[{_KDIGIT}]{{4}}(?![{_KDIGIT}])"
)


def detect_phone(seg: Segment) -> list[Finding]:
    out = [_f(seg, m, CAT_PHONE) for m in RE_PHONE.finditer(seg.text)]
    out.extend(_f(seg, m, CAT_PHONE, note="한글로 풀어 쓴 번호")
               for m in RE_PHONE_SPOKEN.finditer(seg.text))
    taken = {(f.start, f.end) for f in out}
    for m in RE_TEL.finditer(seg.text):
        if any(m.start() < e and s < m.end() for s, e in taken):
            continue
        out.append(_f(seg, m, CAT_TEL))
        taken.add((m.start(), m.end()))
    for m in RE_TEL_INTL.finditer(seg.text):
        digits = sum(ch.isdigit() for ch in m.group())
        if not 8 <= digits <= 15:
            continue
        if any(m.start() < e and s < m.end() for s, e in taken):
            continue
        out.append(_f(seg, m, CAT_TEL, note="해외 번호"))
        taken.add((m.start(), m.end()))
    for m in RE_EXTENSION.finditer(seg.text):
        if any(m.start(1) < e and s < m.end(1) for s, e in taken):
            continue
        out.append(_f(seg, m, CAT_TEL, note="내선", group=1))
    return out


RE_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


# 수집을 피하려고 기호를 말로 바꿔 쓴 전자우편 ('hong dot gd 골뱅이 daum 쩜 net').
# 맨 ' at '만으로는 영어 문장('data at www.site.com')이 걸리므로, 그 경우에는
# 점도 말로 풀어 쓴 형태만 인정한다.
_EW = r"[A-Za-z0-9_%+\-]+"
_EDOT_WORD = rf"(?:{S}*(?:\(dot\)|\[dot\]|\(점\)){S}*|{S}+(?:dot|DOT|닷|점|쩜){S}+|{S}*(?:닷|쩜){S}*)"
_EAT_STRONG = rf"(?:{S}*(?:\(at\)|\[at\]|\(골뱅이\)|\[골뱅이\]|골뱅이|앳){S}*)"
_EAT_PLAIN = rf"(?:{S}+(?:at|AT){S}+)"
RE_EMAIL_SPOKEN = re.compile(
    rf"(?<![A-Za-z0-9]){_EW}(?:(?:\.|{_EDOT_WORD}){_EW})*{_EAT_STRONG}{_EW}(?:(?:\.|{_EDOT_WORD}){_EW})+"
)
RE_EMAIL_SPOKEN_PLAIN = re.compile(
    rf"(?<![A-Za-z0-9]){_EW}(?:(?:\.|{_EDOT_WORD}){_EW})*{_EAT_PLAIN}{_EW}(?:{_EDOT_WORD}{_EW})+"
)


def detect_email(seg: Segment) -> list[Finding]:
    out = [_f(seg, m, CAT_EMAIL) for m in RE_EMAIL.finditer(seg.text)]
    spans = [(f.start, f.end) for f in out]
    for pattern in (RE_EMAIL_SPOKEN, RE_EMAIL_SPOKEN_PLAIN):
        for m in pattern.finditer(seg.text):
            if any(m.start() < e and s < m.end() for s, e in spans):
                continue
            spans.append((m.start(), m.end()))
            out.append(_f(seg, m, CAT_EMAIL, note="말로 풀어 쓴 전자우편"))
    return out


# 메신저·서비스 계정 — 서비스 이름이 앞에 있을 때만. 글자만으로 된 낱말은 계정인지
# 알 수 없어, 숫자·밑줄·점이 섞인 것만 받는다.
_SERVICES = (r"카카오톡|카톡|텔레그램|라인|위챗|인스타그램|인스타|트위터|페이스북|깃허브|"
             r"디스코드|슬랙|스카이프|[Kk]akao[Tt]alk|[Tt]elegram|[Ii]nstagram|"
             r"[Gg]it[Hh]ub|[Ss]lack|[Ss]kype|[Dd]iscord")
_HANDLE = r"@?([A-Za-z][A-Za-z0-9_.\-]{3,29})(?![A-Za-z0-9@])"
RE_HANDLE_SERVICE = re.compile(
    rf"(?:{_SERVICES}){S}*(?:ID|아이디|계정)?{S}*[:：]?{S}*{_HANDLE}")
RE_HANDLE_LABEL = re.compile(rf"(?<![A-Za-z가-힣])(?:ID|아이디|계정){S}*[:：]{S}*{_HANDLE}")
RE_HANDLE_SHAPE = re.compile(r"[0-9_.]")


def detect_handle(seg: Segment) -> list[Finding]:
    out: list[Finding] = []
    spans: list[tuple[int, int]] = []
    for pattern, grade in ((RE_HANDLE_SERVICE, GRADE_CERTAIN), (RE_HANDLE_LABEL, GRADE_LIKELY)):
        for m in pattern.finditer(seg.text):
            value = m.group(1).rstrip(".-")
            if not RE_HANDLE_SHAPE.search(value):
                continue
            start, end = m.start(1), m.start(1) + len(value)
            if any(start < e and s < end for s, e in spans):
                continue
            spans.append((start, end))
            out.append(Finding(
                seg_id=seg.seg_id, start=start, end=end, text=value,
                category=CAT_HANDLE, grade=grade, detectors={"L1"},
                note="서비스 이름 동반" if grade == GRADE_CERTAIN else "계정 이름표",
                part=seg.part,
            ))
    return out


# 과제·협약 번호 — 이름표가 붙은 식별자. 기관마다 체계가 달라 모양으로는 못 잡는다.
RE_PROJECT_NO = re.compile(
    rf"(?:과제|협약|접수){S}*(?:고유)?{S}*번호{S}*[:：]?{S}*"
    r"([A-Za-z0-9][A-Za-z0-9\-_]{5,24})(?![A-Za-z0-9])"
)


# 체계가 공개돼 있어 모양만으로 알 수 있는 과제번호
#   한국연구재단 2022R1A2C4007716 · RS-2023-00212345 / 정보통신기획평가원 2020-0-01234
RE_PROJECT_NO_SHAPE = re.compile(
    r"(?<![A-Za-z0-9])((?:NRF-)?20\d{2}[A-Z]\d[A-Z]\d[A-Z]\d{7,8}"
    r"|RS-20\d{2}-\d{8}|20\d{2}-0-\d{5})(?![A-Za-z0-9])")
# 사람에게 붙는 번호 — 이름표가 있을 때만
RE_ID_NUMBER = re.compile(
    rf"(?:사번|학번|교번|직번|사원번호|직원번호|수험번호|회원번호){S}*[:：(（]?{S}*"
    r"([A-Za-z]{0,3}\d{5,12})(?![A-Za-z0-9])")


def detect_project_no(seg: Segment) -> list[Finding]:
    out = []
    spans: list[tuple[int, int]] = []
    for m in RE_PROJECT_NO.finditer(seg.text):
        if any(ch.isdigit() for ch in m.group(1)):
            out.append(_f(seg, m, CAT_PROJECT_NO, GRADE_CERTAIN, "번호 이름표 동반", group=1))
            spans.append((m.start(1), m.end(1)))
    for m in RE_PROJECT_NO_SHAPE.finditer(seg.text):
        if not any(m.start(1) < e and s < m.end(1) for s, e in spans):
            out.append(_f(seg, m, CAT_PROJECT_NO, GRADE_CERTAIN, "과제번호 체계", group=1))
    for m in RE_ID_NUMBER.finditer(seg.text):
        out.append(_f(seg, m, CAT_HANDLE, GRADE_CERTAIN, "사번·학번 이름표", group=1))
    return out


RE_CARD = re.compile(rf"(?<![0-9])\d{{4}}{SEP}?\d{{4}}{SEP}?\d{{4}}{SEP}?\d{{4}}(?![0-9])")


def detect_card(seg: Segment) -> list[Finding]:
    out = []
    for m in RE_CARD.finditer(seg.text):
        if V.luhn_ok(m.group()):
            out.append(_f(seg, m, CAT_CARD, GRADE_CERTAIN, "Luhn 통과"))
    return out


# 계좌번호는 범용 체크섬이 없어 문맥어를 요구한다 — 없으면 과탐이 폭발한다
RE_ACCOUNT_CTX = re.compile(r"계좌|예금주|입금|송금|은행|출금|account")
RE_ACCOUNT = re.compile(rf"(?<![0-9])\d{{2,6}}{SEP}\d{{2,6}}{SEP}\d{{2,7}}(?:{SEP}\d{{1,6}})?(?![0-9])")


def detect_account(seg: Segment) -> list[Finding]:
    if not RE_ACCOUNT_CTX.search(context_text(seg)):
        return []
    out = []
    for m in RE_ACCOUNT.finditer(seg.text):
        digits = "".join(ch for ch in m.group() if ch.isdigit())
        if not 9 <= len(digits) <= 16:
            continue
        # 주민번호·카드는 각자의 탐지기가 맡는다. 다만 '자릿수가 우연히 13자리'인
        # 계좌(301-0274-5518-63)까지 넘기면 아무도 잡지 않는다(실측) — 주민번호
        # 모양(6자리-7자리)으로 적힌 것만 넘긴다.
        if RE_RRN.fullmatch(m.group()) or RE_RRN_NOSEP.fullmatch(m.group()):
            continue
        if len(digits) == 16 and V.luhn_ok(m.group()):
            continue
        out.append(_f(seg, m, CAT_ACCOUNT, GRADE_CERTAIN, "계좌 문맥어 동반"))
    return out


RE_BIZNO = re.compile(rf"(?<![0-9])\d{{3}}{SEP}\d{{2}}{SEP}\d{{5}}(?![0-9])")
RE_CORPNO = re.compile(rf"(?<![0-9])\d{{6}}{SEP}\d{{7}}(?![0-9])")


def detect_bizno(seg: Segment) -> list[Finding]:
    out = []
    for m in RE_BIZNO.finditer(seg.text):
        ok = V.bizno_checksum_ok(m.group())
        out.append(_f(seg, m, CAT_BIZNO, GRADE_CERTAIN if ok else GRADE_LIKELY,
                      "체크섬 통과" if ok else "체크섬 불일치"))
    for m in RE_CORPNO.finditer(seg.text):
        if V.corpno_checksum_ok(m.group()) and not V.rrn_shape_ok(m.group()):
            out.append(_f(seg, m, CAT_CORPNO, GRADE_CERTAIN, "체크섬 통과"))
    return out


RE_PASSPORT = re.compile(r"(?<![A-Z0-9])[MSRODmsrod]\d{8}(?![A-Z0-9])")
RE_PASSPORT_CTX = re.compile(r"여권|passport", re.IGNORECASE)


def detect_passport(seg: Segment) -> list[Finding]:
    grade = GRADE_CERTAIN if RE_PASSPORT_CTX.search(context_text(seg)) else GRADE_LIKELY
    return [_f(seg, m, CAT_PASSPORT, grade) for m in RE_PASSPORT.finditer(seg.text)]


RE_DRIVER = re.compile(rf"(?<![0-9])(?:\d{{2}}|[가-힣]{{2}}){SEP}?\d{{2}}{SEP}?\d{{6}}{SEP}?\d{{2}}(?![0-9])")
RE_DRIVER_CTX = re.compile(r"운전면허|면허번호")


def detect_driver(seg: Segment) -> list[Finding]:
    if not RE_DRIVER_CTX.search(context_text(seg)):
        return []
    return [_f(seg, m, CAT_DRIVER) for m in RE_DRIVER.finditer(seg.text)]


RE_VEHICLE = re.compile(r"(?<![0-9])\d{2,3}[가-힣]{1}\s?\d{4}(?![0-9])")
RE_VEHICLE_CTX = re.compile(r"차량|차번호|자동차|번호판")


def detect_vehicle(seg: Segment) -> list[Finding]:
    if not RE_VEHICLE_CTX.search(context_text(seg)):
        return []
    return [_f(seg, m, CAT_VEHICLE) for m in RE_VEHICLE.finditer(seg.text)]


# 주소 — 도로명 앵커만으로는 안 된다. 한국어에서 '~로'는 조사·부사형으로
# 훨씬 자주 쓰여서 '중심으로 3개', '단계적으로 2027년'이 전부 도로명이 된다.
# 그래서 행정구역(시·도·군·구)이 앞에 오거나, 세그먼트에 주소 문맥어가 있을 때만 인정한다.
_ROAD = rf"[가-힣A-Za-z0-9]{{1,12}}(?:로|길)\s?\d+(?:-\d+)?(?:번길\s?\d+)?"
_BUILDING = (rf"(?:{S}*[,(]?{S}*[가-힣A-Za-z0-9]{{1,12}}"
             rf"(?:동|호|층|관|빌딩|타워|아파트|마을|빌라|오피스텔|맨션|단지)[,)]?)*")
_ADMIN = rf"[가-힣]{{2,10}}(?:특별시|광역시|특별자치시|특별자치도|도|시|군|구)"

RE_ADDRESS_ADMIN = re.compile(rf"(?:{_ADMIN}{S}*){{1,3}}{_ROAD}{_BUILDING}")
RE_ADDRESS_ROAD = re.compile(rf"{_ROAD}{_BUILDING}")
RE_ADDRESS_JIBUN = re.compile(
    rf"{_ADMIN}{S}+[가-힣]{{1,10}}(?:읍|면|동|리){S}+\d+(?:-\d+)?(?:번지)?"
)
RE_ADDRESS_CTX = re.compile(r"주소|소재지|주소지|우편|address", re.IGNORECASE)


# 도로명 없이 나오는 거처·호실. 행정구역 주소보다 짧지만 사람을 좁히는 힘은 같다.
# '임대아파트' 같은 보통명사를 피하려고, 공동주택 이름은 동·호수가 붙었거나
# 같은 문단에 거주를 뜻하는 말이 있을 때만 받는다.
RE_RESIDENCE = re.compile(
    rf"(?<![가-힣A-Za-z0-9])([가-힣A-Za-z0-9]{{2,10}})(?:아파트|빌라|오피스텔|맨션)"
    rf"(?P<unit>(?:{S}*\d{{1,4}}동)?(?:{S}*\d{{1,4}}호)?)(?![0-9])"
)
RE_RESIDENCE_CTX = re.compile(r"거주|사는|살[며고던았아]|산다|자택|이사|입주|주소|댁|집")
RESIDENCE_GENERIC = frozenset({
    "임대", "공공", "민간", "신축", "노후", "고층", "저층", "분양", "재건축", "재개발",
    "국민", "영구", "행복", "소형", "중형", "대형", "일반", "해당", "인근", "주변",
    "서민", "고급", "신규", "기존", "공동", "단지형", "도시형",
})
RE_ROOM = re.compile(
    rf"(?<![가-힣A-Za-z0-9])(?:\d{{1,4}}동|[가-힣A-Za-z0-9]{{1,10}}"
    rf"(?:관|빌딩|센터|타워|홀|연구동|실험동|기숙사)){S}?[A-Za-z]?\d{{2,4}}호(?![0-9])"
)


def detect_address(seg: Segment) -> list[Finding]:
    out = [_f(seg, m, CAT_ADDRESS) for m in RE_ADDRESS_ADMIN.finditer(seg.text)]
    spans = [(f.start, f.end) for f in out]

    def add(m: re.Match) -> None:
        if any(m.start() < e and s < m.end() for s, e in spans):
            return
        spans.append((m.start(), m.end()))
        out.append(_f(seg, m, CAT_ADDRESS))

    for m in RE_ADDRESS_JIBUN.finditer(seg.text):
        add(m)
    # 행정구역 없이 도로명만 있는 형태는 '주소' 라벨이 있을 때만
    if RE_ADDRESS_CTX.search(context_text(seg)):
        for m in RE_ADDRESS_ROAD.finditer(seg.text):
            add(m)

    # 온전한 주소를 먼저 잡고, 남은 자리에서만 거처·호실 조각을 본다.
    # 순서가 바뀌면 '본관 302호'가 먼저 자리를 차지해 도로명이 통째로 남는다.
    def add_likely(m: re.Match, note: str) -> None:
        if any(m.start() < e and s < m.end() for s, e in spans):
            return
        spans.append((m.start(), m.end()))
        out.append(_f(seg, m, CAT_ADDRESS, GRADE_LIKELY, note))

    has_residence_ctx = bool(RE_RESIDENCE_CTX.search(context_text(seg)))
    for m in RE_RESIDENCE.finditer(seg.text):
        if m.group(1) in RESIDENCE_GENERIC:
            continue
        if m.group("unit").strip() or has_residence_ctx:
            add_likely(m, "공동주택 이름")
    for m in RE_ROOM.finditer(seg.text):
        add_likely(m, "건물·호실")
    return out


# 생년월일은 문맥어를 요구한다 — 요구하지 않으면 연구기간·회의일자를 전부 지운다
RE_BIRTH_CTX = re.compile(r"생년월일|생일|출생|birth|D\.?O\.?B", re.IGNORECASE)
RE_DATE = re.compile(
    rf"(?<![0-9])(\d{{4}}){S}*[.\-/년]{S}*(\d{{1,2}}){S}*[.\-/월]{S}*(\d{{1,2}}){S}*일?(?![0-9])"
)


def detect_birth(seg: Segment) -> list[Finding]:
    if not RE_BIRTH_CTX.search(context_text(seg)):
        return []
    out = []
    for m in RE_DATE.finditer(seg.text):
        year, month, day = (int(m.group(i)) for i in (1, 2, 3))
        if V.plausible_birth_date(year, month, day):
            out.append(_f(seg, m, CAT_BIRTH, GRADE_CERTAIN, "생년월일 문맥어 동반"))
    return out


DETECTORS = (
    detect_rrn, detect_phone, detect_email, detect_card, detect_account,
    detect_bizno, detect_passport, detect_driver, detect_vehicle,
    detect_address, detect_birth, detect_handle, detect_project_no,
)


def detect_segment(seg: Segment) -> list[Finding]:
    out: list[Finding] = []
    for fn in DETECTORS:
        out.extend(fn(seg))
    # 편집 불가 경계(탭·줄바꿈)를 넘은 매치는 채택하지 않는다
    return [f for f in out if seg.slice_editable(f.start, f.end)]
