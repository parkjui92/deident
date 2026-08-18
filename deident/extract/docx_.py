"""docx 추출기 — 본문·표·머리말/꼬리말·각주·글상자를 세그먼트로 편다.

워드는 한 문장을 여러 <w:t> 로 쪼개 저장한다. 그 조인·역매핑은 xml_apply 가
이미 책임지므로, 이 모듈이 결정하는 것은 오직 '어디를 읽을 것인가' 다.
빠뜨린 자리는 곧 유출이므로 다음을 모두 훑는다.

  · word/document.xml 본문 문단과 표의 모든 셀
  · header*.xml·footer*.xml·footnotes.xml·endnotes.xml
    — 성명·기관·연락처가 상투적으로 박히는 자리다
  · 글상자: mc:Choice(wps:txbx) 와 mc:Fallback(v:textbox) 에 같은 텍스트가
    두 벌 들어간다. 두 벌 다 세그먼트로 잡는다. 한쪽만 고치면 다른 쪽에 원문이
    남고, 워드가 어느 쪽을 그릴지는 환경에 따라 다르다.
  · 숨김 서식(w:vanish) 런 — 화면에 안 보일 뿐 파일에는 그대로 있다.
    별도 분기를 두지 않는 것이 곧 포함이다.
  · 필드(w:fldSimple / w:instrText) 의 지시문과 표시 결과 런
    — AUTHOR 필드의 캐시된 작성자명이 여기 남는다.

seg_id 는 '부분 이름 + 그 부분 안에서의 발행 순번' 이라 같은 파일을 다시
파싱하면 같은 값이 나온다. 치환기가 다른 트리로 재파싱해도 seg_id 로 다시
붙일 수 있는 근거가 이것이다.
"""

from __future__ import annotations

import io
import posixpath
import re
import zipfile
from pathlib import Path

from lxml import etree

from ..apply.xml_apply import XmlPart, build_segment
from ..errors import DocumentRejected
from ..model import (
    KIND_CELL, KIND_FOOTER, KIND_FOOTNOTE, KIND_HEADER, KIND_PARA, KIND_TEXTBOX,
    Segment, TableCtx,
)

try:   # 표 방향 판정에만 쓰는 참고 사전 — 없으면 모양 휴리스틱으로 내려간다
    from ..kdata import HEADER_CATEGORY as KNOWN_HEADERS
except ImportError:  # pragma: no cover
    KNOWN_HEADERS: dict[str, str] = {}

RE_HEADER_CLEAN = re.compile(r"[\s()（）]+")


def _clean_header(text: str) -> str:
    """'성 명(한글)' → '성명' — 사전 조회 형태로 맞춘다 (L2 와 같은 규칙)."""
    return RE_HEADER_CLEAN.sub("", text)

NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "pkg": "http://schemas.openxmlformats.org/package/2006/relationships",
    "ct": "http://schemas.openxmlformats.org/package/2006/content-types",
}


def qn(name: str) -> str:
    """'w:p' → '{네임스페이스}p'."""
    prefix, local = name.split(":", 1)
    return f"{{{NS[prefix]}}}{local}"


W_P = qn("w:p")
W_TBL = qn("w:tbl")
W_TR = qn("w:tr")
W_TC = qn("w:tc")
W_TCPR = qn("w:tcPr")
W_GRIDSPAN = qn("w:gridSpan")
W_VMERGE = qn("w:vMerge")
W_VAL = qn("w:val")
W_T = qn("w:t")
W_TAB = qn("w:tab")
W_BR = qn("w:br")
W_CR = qn("w:cr")
W_NOBREAKHYPHEN = qn("w:noBreakHyphen")
W_TXBXCONTENT = qn("w:txbxContent")
W_SDT = qn("w:sdt")
W_SDTCONTENT = qn("w:sdtContent")
W_R = qn("w:r")
W_RPR = qn("w:rPr")
MC_FALLBACK = qn("mc:Fallback")

# 변경 추적 흔적 — 하나라도 있으면 치환을 거부한다 (w:delText 에 원문이 산다)
REVISION_TAGS = (qn("w:ins"), qn("w:del"), qn("w:moveFrom"), qn("w:moveTo"))

# 글상자 두 벌 중 Fallback 쪽 세그먼트임을 part 이름에 남긴다.
# 치환은 양쪽 다 하되, 사람·AI 가 읽는 마크다운에는 한 벌만 싣기 위한 표시다.
FALLBACK_MARK = "#fallback"

_SKIP_TAGS = frozenset({W_TXBXCONTENT})
# 텍스트가 없는 요소를 대신할 의사문자. editable=False 경계가 되어
# 탐지 정규식이 노드 경계를 넘어가는 것을 구조적으로 막는다.
_PSEUDO = {W_TAB: "\t", W_BR: "\n", W_CR: "\n", W_NOBREAKHYPHEN: "-"}

MAIN_PART = "word/document.xml"
RE_HEADER = re.compile(r"^word/header\d*\.xml$")
RE_FOOTER = re.compile(r"^word/footer\d*\.xml$")
RE_NOTES = re.compile(r"^word/(?:footnotes|endnotes)\.xml$")
RE_GLOSSARY = re.compile(r"^word/glossary/(?:document|header\d*|footer\d*)\.xml$")

_PARSER = etree.XMLParser(remove_blank_text=False, resolve_entities=False,
                          no_network=True, huge_tree=True)


# ── 부분(멤버) 선정 ─────────────────────────────────────────────────────────
def main_document_name(zf: zipfile.ZipFile) -> str:
    """패키지 관계에서 본문 파트를 찾는다 (이름이 document.xml 이 아닐 수 있다)."""
    try:
        root = etree.fromstring(zf.read("_rels/.rels"), _PARSER)
    except (KeyError, etree.XMLSyntaxError):
        return MAIN_PART
    for rel in root:
        if str(rel.get("Type", "")).endswith("/officeDocument"):
            target = str(rel.get("Target", ""))
            return posixpath.normpath(target.lstrip("/"))
    return MAIN_PART


def part_names(zf: zipfile.ZipFile) -> list[str]:
    """세그먼트를 뽑을 XML 멤버 — 본문 먼저, 나머지는 이름순(결정적)."""
    names = set(zf.namelist())
    main = main_document_name(zf)
    ordered = [main] if main in names else []
    extra = [
        n for n in names
        if n != main and (RE_HEADER.match(n) or RE_FOOTER.match(n)
                          or RE_NOTES.match(n) or RE_GLOSSARY.match(n))
    ]
    ordered.extend(sorted(extra))
    return ordered


def kind_for_part(name: str) -> str:
    if RE_HEADER.match(name):
        return KIND_HEADER
    if RE_FOOTER.match(name):
        return KIND_FOOTER
    if RE_NOTES.match(name):
        return KIND_FOOTNOTE
    return KIND_PARA


def parse_member(zf: zipfile.ZipFile, name: str) -> etree._ElementTree | None:
    try:
        data = zf.read(name)
    except KeyError:
        return None
    try:
        return etree.parse(io.BytesIO(data), _PARSER)
    except etree.XMLSyntaxError:
        return None


# ── 세그먼트 조립 콜백 ──────────────────────────────────────────────────────
def _skip(element: etree._Element) -> bool:
    # 글상자 안쪽 문단은 바깥 문단 텍스트에 섞지 않는다 (따로 세그먼트가 된다)
    return element.tag in _SKIP_TAGS


def _pseudo(element: etree._Element) -> str | None:
    return _PSEUDO.get(element.tag)


# ── 표 격자 ─────────────────────────────────────────────────────────────────
def _children_of(parent: etree._Element, tag: str) -> list[etree._Element]:
    """직계 자식 + w:sdt 로 감싼 자식을 문서 순서대로 편다."""
    out: list[etree._Element] = []
    for child in parent:
        if child.tag == tag:
            out.append(child)
        elif child.tag == W_SDT:
            for content in child:
                if content.tag == W_SDTCONTENT:
                    out.extend(c for c in content if c.tag == tag)
    return out


def _grid_span(tc: etree._Element) -> int:
    for pr in tc:
        if pr.tag != W_TCPR:
            continue
        for item in pr:
            if item.tag == W_GRIDSPAN:
                try:
                    return max(1, int(item.get(W_VAL, "1")))
                except ValueError:
                    return 1
    return 1


def _vmerge(tc: etree._Element) -> str:
    """"" | "restart" | "continue" — 세로 병합 상태."""
    for pr in tc:
        if pr.tag != W_TCPR:
            continue
        for item in pr:
            if item.tag == W_VMERGE:
                return "restart" if item.get(W_VAL) == "restart" else "continue"
    return ""


def _cell_text(tc: etree._Element, limit: int = 40) -> str:
    """헤더 문맥용 간단 텍스트 — 표시가 아니라 라벨 판정에만 쓴다."""
    parts = [t.text for t in tc.iter(W_T) if t.text]
    text = " ".join("".join(parts).split())
    return text[:limit]


def _is_label(text: str) -> bool:
    """'성명·생년월일' 처럼 값이 아니라 이름표로 보이는가."""
    t = text.strip()
    return bool(t) and len(t) <= 12 and not any(ch.isdigit() for ch in t)


def _known_header(text: str) -> bool:
    """사전에 등재된 표 머리글인가 — 표 방향 판정의 가장 정확한 단서."""
    return _clean_header(text) in KNOWN_HEADERS


def _label_ratio(values: list[str]) -> float:
    filled = [v for v in values if v.strip()]
    if not filled:
        return 0.0
    return sum(1 for v in filled if _is_label(v)) / len(filled)


def _orientation(grid: list[list[tuple]], texts: list[list[str]]) -> str:
    """'col' = 가로형(첫 행이 머리글) · 'row' = 세로형(첫 열이 이름표) · '' = 머리글 없음.

    모양만으로는 갈리지 않는다. '성명|김가온' 두 칸짜리 행은 세로형 서식이고
    '성명|연락처' 머리글 행도 두 칸이며, 이름과 이름표는 둘 다 짧은 한글이다.
    그래서 어느 축에 아는 머리글이 더 많은지를 1차 근거로 삼고, 사전에 없는
    표에서만 이름표 모양 비율로 판정한다.

    한 칸짜리 표(본문을 감싼 상자)와 머리글 행이 없는 표에는 ''를 준다.
    없는 머리글을 만들어 내면 첫 행이 '이름표'로 취급돼 그 안의 이름이 치환
    대상에서 빠진다 — 실제로 이 함정을 산출물 검증에서 한 번 밟았다.
    """
    if not grid or len(grid) < 2:
        return ""
    width = max((row[-1][1] + row[-1][2] for row in grid if row), default=0)
    if width < 2:
        return ""

    row0 = [texts[0][j] for j in range(1, len(grid[0]))]
    col0 = [texts[r][0] for r in range(1, len(grid)) if grid[r] and grid[r][0][1] == 0]
    known_row = sum(1 for t in row0 if _known_header(t))
    known_col = sum(1 for t in col0 if _known_header(t))
    if known_row or known_col:
        return "col" if known_row >= known_col else "row"

    body = [texts[r][j] for r in range(1, len(grid)) for j in range(1, len(grid[r]))]
    base = _label_ratio(body)
    if _label_ratio(col0) - base > 0.3 and _label_ratio(col0) > _label_ratio(row0):
        return "row"
    return "col" if _label_ratio(texts[0]) >= 0.5 else ""


def _row_span(grid: list[list[tuple]], row: int, col: int, vmerge: str) -> int:
    if vmerge != "restart":
        return 1
    span = 1
    for r in range(row + 1, len(grid)):
        if any(c0 == col and vm == "continue" for _, c0, _, vm in grid[r]):
            span += 1
        else:
            break
    return span


def _row_label(grid: list[list[tuple]], texts: list[list[str]], row: int) -> str:
    """세로형 표에서 그 행의 이름표 — 세로 병합된 이름표는 위로 거슬러 찾는다."""
    for r in range(row, -1, -1):
        if not grid[r] or grid[r][0][1] != 0:
            return ""
        text = texts[r][0]
        if text.strip():
            return text
        if grid[r][0][3] != "continue":
            return ""
    return ""


def _header_context(grid: list[list[tuple]], texts: list[list[str]], axis: str,
                    row: int, index: int, col: int) -> tuple[str, str, bool]:
    """(헤더 텍스트, 축, 머리글 칸 여부).

    헤더는 반드시 한 칸의 텍스트 하나여야 한다. 소비자(L2 표 수확)가
    '성명·생년월일' 같은 이름표를 사전에서 정확히 찾아 쓰기 때문에, 두 축을
    이어 붙이면 그 조회가 통째로 빗나간다.

    머리글 칸 자체에는 헤더를 주지 않는다. 이름표에 이름표를 씌우면 '생년월일'
    이라는 글자가 '성명' 칸의 값으로 수확돼 사람 이름처럼 치환된다.
    """
    if axis == "":
        return "", "", False

    if axis == "row":
        if col == 0:
            return "", "", True
        header = _row_label(grid, texts, row)
        return (header, "row", False) if header else ("", "", False)

    if row == 0:
        return "", "", True
    for j, (_, start, span, _) in enumerate(grid[0]):
        if start <= col < start + span:
            header = texts[0][j]
            return (header, "col", False) if header else ("", "", False)
    return "", "", False


# ── 부분 하나를 세그먼트로 ──────────────────────────────────────────────────
class _PartBuilder:
    def __init__(self, part_name: str, order_start: int) -> None:
        self.part = part_name
        self.kind_default = kind_for_part(part_name)
        self.segments: list[Segment] = []
        self.seq = 0            # seg_id 용 (부분 안에서 결정적)
        self.order = order_start
        self.table_seq = 0

    def _emit(self, node: etree._Element, kind: str,
              ctx: TableCtx | None, fallback: bool) -> None:
        seg = build_segment(
            f"{self.part}:{self.seq:05d}", node,
            kind=kind, part=self.part + (FALLBACK_MARK if fallback else ""),
            order=self.order, skip=_skip, pseudo=_pseudo, table_ctx=ctx,
        )
        self.seq += 1
        self.order += 1
        self.segments.append(seg)

    def walk(self, element: etree._Element, *, ctx: TableCtx | None = None,
             fallback: bool = False, textbox: bool = False) -> None:
        for child in element:
            tag = child.tag
            if tag == W_P:
                kind = KIND_CELL if ctx else (KIND_TEXTBOX if textbox else self.kind_default)
                self._emit(child, kind, ctx, fallback)
                # 문단 안에 글상자가 있으면 그 안의 문단을 따로 발행한다
                self.walk(child, ctx=ctx, fallback=fallback, textbox=textbox)
            elif tag == W_TBL:
                self.table(child, fallback=fallback, textbox=textbox)
            elif tag == W_TXBXCONTENT:
                self.walk(child, ctx=None, fallback=fallback, textbox=True)
            elif tag == MC_FALLBACK:
                self.walk(child, ctx=ctx, fallback=True, textbox=textbox)
            else:
                self.walk(child, ctx=ctx, fallback=fallback, textbox=textbox)

    def table(self, tbl: etree._Element, *, fallback: bool, textbox: bool) -> None:
        self.table_seq += 1
        table_id = f"{self.part.rsplit('/', 1)[-1].split('.')[0]}T{self.table_seq}"

        grid: list[list[tuple]] = []
        for tr in _children_of(tbl, W_TR):
            cursor = 0
            row_info: list[tuple] = []
            for tc in _children_of(tr, W_TC):
                span = _grid_span(tc)
                row_info.append((tc, cursor, span, _vmerge(tc)))
                cursor += span      # 세로 병합 칸도 격자 열은 차지한다
            grid.append(row_info)
        texts = [[_cell_text(tc) for tc, _, _, _ in row] for row in grid]
        axis = _orientation(grid, texts)

        for r, row_info in enumerate(grid):
            for index, (tc, col, span, vmerge) in enumerate(row_info):
                header_text, header_axis, is_header = _header_context(
                    grid, texts, axis, r, index, col)
                ctx = TableCtx(
                    table_id=table_id, row=r, col=col,
                    row_span=_row_span(grid, r, col, vmerge), col_span=span,
                    header_text=header_text, header_axis=header_axis,
                    is_header=is_header,
                )
                self.walk(tc, ctx=ctx, fallback=fallback, textbox=textbox)


# ── 공개 API ────────────────────────────────────────────────────────────────
def load_parts(zf: zipfile.ZipFile) -> list[XmlPart]:
    """수정 대상 XML 부분과 그 세그먼트 — 추출과 치환이 같은 함수를 쓴다."""
    parts: list[XmlPart] = []
    order = 0
    for name in part_names(zf):
        tree = parse_member(zf, name)
        if tree is None:
            continue
        builder = _PartBuilder(name, order)
        builder.walk(tree.getroot())
        order = builder.order
        parts.append(XmlPart(name=name, tree=tree, segments=builder.segments))
    return parts


def open_package(path: Path) -> zipfile.ZipFile:
    """열 수 없는 파일은 조용히 빈 결과로 넘기지 않고 거부한다."""
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError):
        raise DocumentRejected(
            f"{path.name} 을(를) 열 수 없습니다 (손상되었거나 docx 가 아닙니다)") from None
    if main_document_name(zf) not in set(zf.namelist()):
        zf.close()
        raise DocumentRejected(f"{path.name} 에 워드 본문(word/document.xml)이 없습니다")
    return zf


def extract(path: Path) -> list[Segment]:
    with open_package(path) as zf:
        parts = load_parts(zf)
    return [seg for part in parts for seg in part.segments]


def revision_parts(parts: list[XmlPart]) -> list[str]:
    """변경 추적 흔적이 있는 부분 이름 — 비어 있지 않으면 처리를 거부한다."""
    hit: list[str] = []
    for part in parts:
        root = part.tree.getroot()
        if any(next(root.iter(tag), None) is not None for tag in REVISION_TAGS):
            hit.append(part.name)
    return hit


def revision_parts_of(path: Path) -> list[str]:
    """파일을 열어 변경 추적만 훑는다 (치환을 시작하기 전에 판정하기 위함)."""
    try:
        with zipfile.ZipFile(path) as zf:
            parts = []
            for name in part_names(zf):
                tree = parse_member(zf, name)
                if tree is not None:
                    parts.append(XmlPart(name=name, tree=tree))
            return revision_parts(parts)
    except (zipfile.BadZipFile, OSError):
        return []
