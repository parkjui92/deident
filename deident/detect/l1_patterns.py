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
    CAT_DRIVER, CAT_EMAIL, CAT_FOREIGN_ID, CAT_PASSPORT, CAT_PHONE, CAT_RRN,
    CAT_TEL, CAT_VEHICLE, GRADE_CERTAIN, GRADE_LIKELY, Finding, Segment,
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


RE_PHONE = re.compile(
    rf"(?<![0-9])(?:\+?82{SEP}?)?0?1[016789]{SEP}?\d{{3,4}}{SEP}?\d{{4}}(?![0-9])"
)
RE_TEL = re.compile(
    rf"(?<![0-9])(?:\+?82{SEP}?)?0(?:2|[3-6][1-5]|70|50\d){SEP}?\d{{3,4}}{SEP}?\d{{4}}(?![0-9])"
)


def detect_phone(seg: Segment) -> list[Finding]:
    out = [_f(seg, m, CAT_PHONE) for m in RE_PHONE.finditer(seg.text)]
    taken = {(f.start, f.end) for f in out}
    for m in RE_TEL.finditer(seg.text):
        if any(m.start() < e and s < m.end() for s, e in taken):
            continue
        out.append(_f(seg, m, CAT_TEL))
    return out


RE_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def detect_email(seg: Segment) -> list[Finding]:
    return [_f(seg, m, CAT_EMAIL) for m in RE_EMAIL.finditer(seg.text)]


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
        if V.rrn_shape_ok(digits) or V.luhn_ok(m.group()):
            continue   # 주민·카드 탐지기 소관
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
_BUILDING = rf"(?:{S}*[,(]?{S}*[가-힣A-Za-z0-9]{{1,12}}(?:동|호|층|관|빌딩|타워)[,)]?)*"
_ADMIN = rf"[가-힣]{{2,10}}(?:특별시|광역시|특별자치시|특별자치도|도|시|군|구)"

RE_ADDRESS_ADMIN = re.compile(rf"(?:{_ADMIN}{S}*){{1,3}}{_ROAD}{_BUILDING}")
RE_ADDRESS_ROAD = re.compile(rf"{_ROAD}{_BUILDING}")
RE_ADDRESS_JIBUN = re.compile(
    rf"{_ADMIN}{S}+[가-힣]{{1,10}}(?:읍|면|동|리){S}+\d+(?:-\d+)?(?:번지)?"
)
RE_ADDRESS_CTX = re.compile(r"주소|소재지|주소지|우편|address", re.IGNORECASE)


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
    detect_address, detect_birth,
)


def detect_segment(seg: Segment) -> list[Finding]:
    out: list[Finding] = []
    for fn in DETECTORS:
        out.extend(fn(seg))
    # 편집 불가 경계(탭·줄바꿈)를 넘은 매치는 채택하지 않는다
    return [f for f in out if seg.slice_editable(f.start, f.end)]
