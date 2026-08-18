"""공용 데이터 규약.

이 모듈의 자료구조는 추출·탐지·치환·검증 전 단계가 공유한다.
핵심은 조인-역매핑(join & reverse-map): 추출기는 물리 노드에 흩어진
텍스트 조각(Piece)을 하나의 논리 텍스트(Segment.text)로 이어 붙이고,
탐지는 이어 붙인 텍스트만 본다. 치환은 Segment 내 오프셋을 다시
Piece 위치로 되돌려 노드에 적용한다.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

# 이미 치환된 가명의 모양. 재실행이 자기 산출물을 다시 갉아먹지 않도록
# 탐지·수확 양쪽에서 이 구간을 건너뛴다 (멱등성의 근거).
RE_PSEUDONYM = re.compile(r"\[[가-힣A-Za-z]+[A-Z]*\d*(?:-[가-힣A-Za-z]+\d*)?\]")

# ── 카테고리 ────────────────────────────────────────────────────────────────
# 개인 식별정보
CAT_PERSON = "person"
CAT_RRN = "rrn"                  # 주민등록번호
CAT_FOREIGN_ID = "foreign_id"    # 외국인등록번호
CAT_PASSPORT = "passport"
CAT_DRIVER = "driver"
CAT_PHONE = "phone"              # 휴대전화
CAT_TEL = "tel"                  # 유선전화·팩스
CAT_EMAIL = "email"
CAT_CARD = "card"
CAT_ACCOUNT = "account"
CAT_ADDRESS = "address"
CAT_BIRTH = "birth"
CAT_VEHICLE = "vehicle"
# 소속·기관
CAT_ORG = "org"
CAT_DEPT = "dept"
CAT_TITLE = "title"              # 직위·직급
CAT_BIZNO = "bizno"              # 사업자등록번호
CAT_CORPNO = "corpno"            # 법인등록번호
# 사업·기술 기밀
CAT_PROJECT_NO = "project_no"
CAT_BUDGET = "budget"
CAT_CONFIDENTIAL = "confidential"

PERSONAL_CATEGORIES = frozenset({
    CAT_PERSON, CAT_RRN, CAT_FOREIGN_ID, CAT_PASSPORT, CAT_DRIVER,
    CAT_PHONE, CAT_TEL, CAT_EMAIL, CAT_CARD, CAT_ACCOUNT,
    CAT_ADDRESS, CAT_BIRTH, CAT_VEHICLE,
})
ORG_CATEGORIES = frozenset({CAT_ORG, CAT_DEPT, CAT_TITLE, CAT_BIZNO, CAT_CORPNO})
CONFIDENTIAL_CATEGORIES = frozenset({CAT_PROJECT_NO, CAT_BUDGET, CAT_CONFIDENTIAL})
ALL_CATEGORIES = PERSONAL_CATEGORIES | ORG_CATEGORIES | CONFIDENTIAL_CATEGORIES

CATEGORY_LABEL = {
    CAT_PERSON: "성명", CAT_RRN: "주민등록번호", CAT_FOREIGN_ID: "외국인등록번호",
    CAT_PASSPORT: "여권번호", CAT_DRIVER: "운전면허번호", CAT_PHONE: "휴대전화",
    CAT_TEL: "유선전화", CAT_EMAIL: "이메일", CAT_CARD: "카드번호",
    CAT_ACCOUNT: "계좌번호", CAT_ADDRESS: "주소", CAT_BIRTH: "생년월일",
    CAT_VEHICLE: "차량번호", CAT_ORG: "기관", CAT_DEPT: "부서",
    CAT_TITLE: "직위", CAT_BIZNO: "사업자등록번호", CAT_CORPNO: "법인등록번호",
    CAT_PROJECT_NO: "과제번호", CAT_BUDGET: "예산", CAT_CONFIDENTIAL: "기밀",
}

# ── 신뢰 등급 ───────────────────────────────────────────────────────────────
GRADE_CERTAIN = "확실"
GRADE_LIKELY = "추정"

# ── 세그먼트 종류 ───────────────────────────────────────────────────────────
KIND_PARA = "para"
KIND_CELL = "cell"
KIND_HEADER = "header"
KIND_FOOTER = "footer"
KIND_FOOTNOTE = "footnote"
KIND_TEXTBOX = "textbox"
KIND_CAPTION = "caption"
KIND_META = "meta"          # 메타데이터(작성자 등) — 치환 아닌 소거 대상
KIND_LINE = "line"          # 평문·마크다운 한 줄


@dataclass
class Piece:
    """Segment.text 의 한 구간이 유래한 물리 위치.

    editable=False 인 조각은 요소 자체(탭·줄바꿈 등)에서 만들어낸 의사문자라
    치환 대상이 될 수 없다. 탐지 정규식이 이 경계를 넘지 못하게 하는 것이
    노드 경계 오염을 막는 1차 방어선이다.
    """

    node: Any                 # lxml Element | 줄 번호 | None
    slot: str                 # "text" | "tail" | "chars" | "line"
    start: int                # Segment.text 내 시작 오프셋
    length: int
    editable: bool = True
    # 공백 성격의 경계(전각 공백·묶음 공백 요소)는 '무른 경계'다. 고칠 수는 없지만
    # 매치가 가로질러도 문서 구조가 상하지 않는다. 자간을 벌려 쓴 이름
    # ('과 학 기 술')이 여기에 걸려 통째로 남는 사고가 실제로 있었다.
    soft: bool = False
    node_offset: int = 0      # 물리 단위(노드 문자열·줄) 안에서의 시작 위치
    ref: Any = None           # 포맷별 부가 정보 (PDF 문자 rect 목록 등)

    @property
    def end(self) -> int:
        return self.start + self.length


@dataclass
class TableCtx:
    """세그먼트가 표 셀일 때의 좌표와 이웃 정보."""

    table_id: str
    row: int
    col: int
    row_span: int = 1
    col_span: int = 1
    header_text: str = ""          # 이 셀을 지배하는 헤더(가로형은 같은 열, 세로형은 같은 행)
    header_axis: str = ""          # "col" | "row" | ""
    is_header: bool = False


@dataclass
class Segment:
    """탐지가 바라보는 논리 단위 (문단 하나, 표 셀 하나, 한 줄)."""

    seg_id: str
    text: str
    pieces: list[Piece] = field(default_factory=list)
    kind: str = KIND_PARA
    table_ctx: TableCtx | None = None
    part: str = ""            # 원본 내 위치 (zip 멤버명·페이지 번호 등)
    order: int = 0            # 문서 내 등장 순서

    def slice_editable(self, start: int, end: int) -> bool:
        """[start, end) 구간을 고쳐도 되는지.

        탭·줄바꿈 같은 단단한 경계를 넘는 매치는 인정하지 않는다. 공백 성격의
        무른 경계는 넘어도 된다 — 그 자리는 손대지 않고 남겨 둔다.
        """
        for p in self.pieces:
            if p.end <= start or p.start >= end:
                continue
            if not p.editable and not p.soft:
                return False
        return True


@dataclass
class Finding:
    """탐지 결과 한 건."""

    seg_id: str
    start: int
    end: int
    text: str                       # 탐지된 원문 (리포트·매핑용, _private 밖으로 나가면 안 됨)
    category: str
    grade: str = GRADE_CERTAIN
    detectors: set[str] = field(default_factory=set)   # {"L1","L2","L3","rule"}
    entity_id: str | None = None
    note: str = ""                  # 체크섬 통과 여부 등 부가 판정
    part: str = ""

    @property
    def length(self) -> int:
        return self.end - self.start


@dataclass
class Replacement:
    """치환 계획 한 건 (Finding + 확정된 대체 문자열)."""

    seg_id: str
    start: int
    end: int
    original: str
    replacement: str
    category: str
    # 이미 민감하다고 판정된 값이라, 줄바꿈·탭이 중간에 끼어 있어도 지운다.
    # (탐지 단계에서는 허용하지 않는다 — 경계를 넘는 매치는 대개 헛것이다.)
    cross_boundary: bool = False

    @property
    def delta(self) -> int:
        return len(self.replacement) - (self.end - self.start)


@dataclass
class Entity:
    """동일 대상(인물·기관)의 여러 표기를 묶는 대장 항목."""

    entity_id: str
    category: str
    canonical: str
    pseudonym: str
    variants: set[str] = field(default_factory=set)
    attributes: dict[str, str] = field(default_factory=dict)
    linked_values: list[str] = field(default_factory=list)


@dataclass
class DocumentIssue:
    """치환으로 해결되지 않는 위험 (사람 판단 필요)."""

    level: str        # "block" | "warn" | "info"
    code: str
    message: str
    where: str = ""


def normalize_key(text: str) -> str:
    """매핑 키 정규화 — 유니코드 정규형·공백·구분자 차이를 흡수한다.

    macOS 파일 경유 텍스트는 NFD로 들어오는 일이 잦아 NFC 고정이 필수다.
    """
    t = unicodedata.normalize("NFC", text)
    t = t.replace("​", "").replace("﻿", "")
    t = " ".join(t.split())
    return t.strip()


def digits_only(text: str) -> str:
    """숫자만 남긴다 — 구분자를 바꿔 재포맷한 번호를 같은 값으로 보기 위함."""
    return "".join(ch for ch in unicodedata.normalize("NFKC", text) if ch.isdigit())
