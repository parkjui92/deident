"""hwpx(한글) 추출기 — 패키지를 열고 문단·표·글상자·머리말을 세그먼트로 편다.

hwpx 는 OCF(zip) 안에 HWPML XML 을 담는다. 텍스트가 있는 곳은 넓게 흩어져
있어서(표 셀·글상자·머리말/꼬리말·각주·바탕쪽·그림 설명) 최상위 문단만 훑으면
표 하나를 통째로 놓친다. 놓친 곳은 그대로 유출 통로다.

그래서 컨테이너를 열거하지 않고 반대로 간다.
  ① 트리 전체를 훑어 hp:p 를 만나면 무조건 세그먼트로 만든다.
     종류(셀·머리말·각주…)와 표 좌표는 조상을 보고 사후에 정한다.
     새 컨테이너가 생겨도 문단이 빠지지 않는 구조다.
  ② 그러고도 남은 텍스트 노드(그림 설명 hp:shapeComment·문서 제목 등)를
     잔여 훑기로 전부 주워 담는다. 여기까지 오면 XML 안에 세그먼트가 덮지
     않은 텍스트는 원리적으로 없다.

seg_id 는 (멤버명, 등장 순번) 으로만 만든다 — 같은 파일을 다시 파싱하면 같은
id 가 나오므로 치환기가 자기 트리를 새로 만들어도 계획을 그대로 붙일 수 있다.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree

from ..apply.xml_apply import XmlPart, build_segment
from ..errors import DocumentRejected
from ..model import (
    KIND_CAPTION, KIND_CELL, KIND_FOOTER, KIND_FOOTNOTE, KIND_HEADER, KIND_META,
    KIND_PARA, KIND_TEXTBOX, Piece, Segment, TableCtx,
)

# ── 네임스페이스 ────────────────────────────────────────────────────────────
NS = {
    "ha": "http://www.hancom.co.kr/hwpml/2011/app",
    "hp": "http://www.hancom.co.kr/hwpml/2011/paragraph",
    "hp10": "http://www.hancom.co.kr/hwpml/2016/paragraph",
    "hs": "http://www.hancom.co.kr/hwpml/2011/section",
    "hc": "http://www.hancom.co.kr/hwpml/2011/core",
    "hh": "http://www.hancom.co.kr/hwpml/2011/head",
    "hhs": "http://www.hancom.co.kr/hwpml/2011/history",
    "hm": "http://www.hancom.co.kr/hwpml/2011/master-page",
    "hpf": "http://www.hancom.co.kr/schema/2011/hpf",
    "opf": "http://www.idpf.org/2007/opf/",
    "dc": "http://purl.org/dc/elements/1.1/",
}
HP = "{%s}" % NS["hp"]

# ── 조판·구조 상수 ──────────────────────────────────────────────────────────
TAG_P = HP + "p"
TAG_T = HP + "t"
TAG_TBL = HP + "tbl"
TAG_TC = HP + "tc"
TAG_LINESEG = HP + "linesegarray"

# 텍스트가 없는 요소를 대신할 의사문자. editable=False 라 치환이 넘지 못하는
# 벽이 된다 — 이름이 탭 건너 이어져 보여도 한 덩어리로 묶이지 않는다.
PSEUDO_TOKENS = {
    HP + "tab": "\t",
    HP + "lineBreak": "\n",
    HP + "fwSpace": " ",
    HP + "nbSpace": " ",
    HP + "hypen": "-",
}

# 문단 세그먼트를 만들 때 내려가지 않을 가지 (각자 제 세그먼트를 갖는다)
SKIP_IN_PARA = frozenset({TAG_P, TAG_LINESEG, HP + "shapeComment"})

# 조상 태그 → 세그먼트 종류 (가장 가까운 조상이 이긴다)
KIND_BY_TAG = {
    TAG_TC: KIND_CELL,
    HP + "header": KIND_HEADER,
    HP + "footer": KIND_FOOTER,
    HP + "footNote": KIND_FOOTNOTE,
    HP + "footnote": KIND_FOOTNOTE,
    HP + "endNote": KIND_FOOTNOTE,
    HP + "endnote": KIND_FOOTNOTE,
    HP + "caption": KIND_CAPTION,
    HP + "drawText": KIND_TEXTBOX,
    HP + "memo": KIND_TEXTBOX,
}

PARSE_SUFFIXES = (".xml", ".hpf", ".rdf")
MAX_PARSE_BYTES = 64 * 1024 * 1024

RE_SECTION = re.compile(r"^Contents/section(\d+)\.xml$", re.I)
RE_MASTER = re.compile(r"^Contents/masterpage(\d*)\.xml$", re.I)

BODY_PART_RE = (RE_SECTION, RE_MASTER)


# ── 패키지 ──────────────────────────────────────────────────────────────────
@dataclass
class HwpxPackage:
    """zip 멤버를 원본 순서·압축방식과 함께 메모리에 들고 있는다."""

    path: Path
    names: list[str] = field(default_factory=list)
    raw: dict[str, bytes] = field(default_factory=dict)
    infos: dict[str, zipfile.ZipInfo] = field(default_factory=dict)

    def has(self, name: str) -> bool:
        return name in self.raw


@dataclass
class HwpxDoc:
    """추출 한 판의 결과 — 치환기가 그대로 이어받는다."""

    package: HwpxPackage
    parts: dict[str, XmlPart] = field(default_factory=dict)
    part_order: list[str] = field(default_factory=list)
    segments: list[Segment] = field(default_factory=list)
    container: dict[str, etree._Element] = field(default_factory=dict)  # seg_id → 담은 요소
    part_of: dict[str, str] = field(default_factory=dict)               # seg_id → 멤버명
    parse_errors: list[tuple[str, str]] = field(default_factory=list)   # (멤버, 사유)

    def part_for_segment(self, seg: Segment) -> XmlPart | None:
        return self.parts.get(self.part_of.get(seg.seg_id, ""))


def load_package(path: Path) -> HwpxPackage:
    pkg = HwpxPackage(path=path)
    try:
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                pkg.names.append(info.filename)
                pkg.infos[info.filename] = info
                pkg.raw[info.filename] = zf.read(info)
    except (zipfile.BadZipFile, OSError) as exc:
        # 메시지에 원문 조각이 섞이지 않도록 예외 종류만 옮긴다
        raise DocumentRejected(
            f"hwpx 패키지를 열 수 없습니다 ({exc.__class__.__name__}). "
            "한글에서 다시 저장한 뒤 시도하십시오."
        ) from None
    if not pkg.raw:
        raise DocumentRejected("hwpx 패키지가 비어 있습니다.")
    return pkg


def is_body_part(name: str) -> bool:
    """본문(구역·바탕쪽) 멤버인지 — 마크다운 산출 대상 판별용."""
    return any(rx.match(name) for rx in BODY_PART_RE)


def _order_key(name: str) -> tuple[int, int, str]:
    """본문 → 바탕쪽 → 머리부 → 패키지 메타 순. 문서 순서를 닮게 만든다."""
    m = RE_SECTION.match(name)
    if m:
        return (0, int(m.group(1)), name)
    m = RE_MASTER.match(name)
    if m:
        return (1, int(m.group(1) or 0), name)
    low = name.lower()
    if low == "contents/header.xml":
        return (2, 0, name)
    if low == "contents/content.hpf":
        return (3, 0, name)
    return (4, 0, name)


def _part_key(name: str) -> str:
    """seg_id 앞머리 — 짧고 결정적이면 된다."""
    base = name.split("/")[-1]
    for suffix in PARSE_SUFFIXES:
        if base.lower().endswith(suffix):
            base = base[: -len(suffix)]
            break
    return re.sub(r"[^0-9A-Za-z._-]", "_", base) or "part"


def parse_parts(pkg: HwpxPackage) -> tuple[dict[str, XmlPart], list[str], list[tuple[str, str]]]:
    parts: dict[str, XmlPart] = {}
    errors: list[tuple[str, str]] = []
    for name in pkg.names:
        if not name.lower().endswith(PARSE_SUFFIXES):
            continue
        data = pkg.raw[name]
        if len(data) > MAX_PARSE_BYTES:
            errors.append((name, "크기 초과"))
            continue
        try:
            root = etree.fromstring(data)
        except etree.XMLSyntaxError as exc:
            errors.append((name, exc.__class__.__name__))
            continue
        parts[name] = XmlPart(name=name, tree=root.getroottree())
    order = sorted(parts, key=_order_key)
    return parts, order, errors


# ── 표 문맥 ─────────────────────────────────────────────────────────────────
def _ancestors(el: etree._Element):
    parent = el.getparent()
    while parent is not None:
        yield parent
        parent = parent.getparent()


def _nearest(el: etree._Element, tag: str) -> etree._Element | None:
    for parent in _ancestors(el):
        if parent.tag == tag:
            return parent
    return None


def _cell_addr(tc: etree._Element) -> tuple[int, int, int, int]:
    """(row, col, rowSpan, colSpan) — hwpx 는 좌표를 직접 적어 준다."""
    addr = tc.find(HP + "cellAddr")
    span = tc.find(HP + "cellSpan")

    def num(el, key, default):
        if el is None:
            return default
        try:
            return max(int(el.get(key, default)), 0)
        except (TypeError, ValueError):
            return default

    row = num(addr, "rowAddr", 0)
    col = num(addr, "colAddr", 0)
    row_span = max(num(span, "rowSpan", 1), 1)
    col_span = max(num(span, "colSpan", 1), 1)
    return row, col, row_span, col_span


def _cell_text(tc: etree._Element, limit: int = 160) -> str:
    """셀 텍스트 — 중첩 표 안의 글자는 제 셀의 것이므로 뺀다."""
    chunks: list[str] = []
    total = 0
    for t in tc.iter(TAG_T):
        if _nearest(t, TAG_TC) is not tc:
            continue
        text = "".join(t.itertext())
        if not text:
            continue
        chunks.append(text)
        total += len(text)
        if total >= limit:
            break
    return " ".join(" ".join(chunks).split())[:limit]


@dataclass
class _TableGrid:
    table_id: str
    col_headers: dict[int, str] = field(default_factory=dict)
    row_headers: dict[int, str] = field(default_factory=dict)
    header_rows: frozenset[int] = frozenset({0})   # 이름표를 공급하는 행


def _build_grid(tbl: etree._Element, table_id: str) -> _TableGrid:
    """머리행(가로형)·머리열(세로형) 값을 병합 범위까지 펴서 기록한다.

    표 안의 값 칸에는 '생년월일' 같은 이름표가 없다. 이름표는 머리 칸에 있고
    탐지는 그 문맥이 있어야 날짜를 생년월일로 판정한다.
    """
    grid = _TableGrid(table_id=table_id)
    cells: list[tuple[int, int, int, int, etree._Element]] = []
    for tc in tbl.iter(TAG_TC):
        if _nearest(tc, TAG_TBL) is not tbl:
            continue                       # 중첩 표의 셀
        row, col, row_span, col_span = _cell_addr(tc)
        cells.append((row, col, row_span, col_span, tc))

    if not cells:
        return grid

    # 첫 행이 표 전체를 덮는 제목 칸이면 다음 행도 머리행으로 본다
    row0 = [c for c in cells if c[0] == 0]
    max_col = max(c[1] + c[3] for c in cells)
    title_row = len(row0) == 1 and row0[0][3] >= max_col and max(c[0] for c in cells) >= 2
    header_rows = {0, 1} if title_row else {0}
    grid.header_rows = frozenset(header_rows)

    for row, col, row_span, col_span, tc in cells:
        if row in header_rows:
            text = _cell_text(tc)
            if text:
                for c in range(col, col + col_span):
                    prev = grid.col_headers.get(c, "")
                    grid.col_headers[c] = f"{prev} {text}".strip() if prev else text
        if col == 0:
            text = _cell_text(tc)
            if text:
                for r in range(row, row + row_span):
                    grid.row_headers.setdefault(r, text)
    return grid


def _table_ctx(tc: etree._Element, grid: _TableGrid) -> TableCtx:
    row, col, row_span, col_span = _cell_addr(tc)

    # 이름표를 공급하는 칸은 제 글자를 제 문맥으로 삼지 않는다 ('성명' 칸의
    # 헤더가 '성명'이 되면 이름표 자체를 값으로 오인한다).
    col_header = "" if row in grid.header_rows else grid.col_headers.get(col, "")
    row_header = "" if col == 0 else grid.row_headers.get(row, "")

    # is_header 는 '이 칸은 값이 아니다'라는 판정이라 치환 제외로 이어진다.
    # 그래서 추측하지 않고 문서가 선언한 값(hp:tc@header)을 먼저 믿는다.
    # 첫 행을 무조건 머리로 보면 표를 칸 나누기로 쓴 문서에서 값 칸이 통째로
    # 치환에서 빠진다 — 유출로 직결된다.
    declared = tc.get("header")
    is_header = declared == "1" if declared is not None else row in grid.header_rows

    # 어느 축이 이름표인지 문서마다 달라 둘 다 문맥으로 준다 (문맥어 탐색 전용).
    header_text = " ".join(dict.fromkeys([p for p in (row_header, col_header) if p]))
    axis = ""
    if header_text:
        axis = "col" if col_header else "row"

    return TableCtx(
        table_id=grid.table_id, row=row, col=col,
        row_span=row_span, col_span=col_span,
        header_text=header_text, header_axis=axis, is_header=is_header,
    )


# ── 세그먼트 만들기 ─────────────────────────────────────────────────────────
def _paragraph_kind(para: etree._Element) -> tuple[str, etree._Element | None]:
    for parent in _ancestors(para):
        kind = KIND_BY_TAG.get(parent.tag)
        if kind is not None:
            return kind, parent
    return KIND_PARA, None


def _make_skip(para: etree._Element):
    def skip(el: etree._Element) -> bool:
        return el is not para and el.tag in SKIP_IN_PARA
    return skip


def _pseudo(el: etree._Element) -> str | None:
    return PSEUDO_TOKENS.get(el.tag)


def build_segments(parts: dict[str, XmlPart], part_order: list[str]
                   ) -> tuple[list[Segment], dict[str, etree._Element], dict[str, str]]:
    segments: list[Segment] = []
    container: dict[str, etree._Element] = {}
    part_of: dict[str, str] = {}
    order = 0

    for member in part_order:
        part = parts[member]
        root = part.tree.getroot()
        key = _part_key(member)
        local = 0
        covered: set[tuple[int, str]] = set()
        grids: dict[int, _TableGrid] = {}
        table_seq = 0

        # ① 문단 — 어디에 박혀 있든 hp:p 면 전부 잡는다
        for para in root.iter(TAG_P):
            kind, holder = _paragraph_kind(para)
            ctx = None
            if kind == KIND_CELL and holder is not None:
                tbl = _nearest(holder, TAG_TBL)
                if tbl is not None:
                    if id(tbl) not in grids:
                        table_seq += 1
                        grids[id(tbl)] = _build_grid(tbl, f"{key}T{table_seq}")
                    ctx = _table_ctx(holder, grids[id(tbl)])
                else:
                    kind = KIND_PARA

            seg_id = f"{key}:{local:06d}"
            local += 1
            seg = build_segment(seg_id, para, kind=kind, part=member, order=order,
                                skip=_make_skip(para), pseudo=_pseudo, table_ctx=ctx)
            order += 1
            segments.append(seg)
            container[seg_id] = para
            part_of[seg_id] = member
            for piece in seg.pieces:
                if piece.slot in ("text", "tail"):
                    covered.add((id(piece.node), piece.slot))

        # ② 잔여 훑기 — 문단 밖에 남은 텍스트(그림 설명·문서 제목·설정값)
        for el in root.iter():
            if not isinstance(el.tag, str):
                continue                    # 주석·처리명령
            for slot in ("text", "tail"):
                value = el.text if slot == "text" else el.tail
                if not value or not value.strip():
                    continue
                if (id(el), slot) in covered:
                    continue
                seg_id = f"{key}:{local:06d}"
                local += 1
                segments.append(Segment(
                    seg_id=seg_id, text=value, kind=KIND_META, part=member, order=order,
                    pieces=[Piece(node=el, slot=slot, start=0, length=len(value))],
                ))
                order += 1
                container[seg_id] = el
                part_of[seg_id] = member
                covered.add((id(el), slot))

    return segments, container, part_of


def parse_document(path: Path) -> HwpxDoc:
    """패키지를 열어 세그먼트까지 만든다 (추출·치환이 같은 경로를 쓴다)."""
    pkg = load_package(path)
    parts, part_order, errors = parse_parts(pkg)
    segments, container, part_of = build_segments(parts, part_order)
    return HwpxDoc(package=pkg, parts=parts, part_order=part_order, segments=segments,
                   container=container, part_of=part_of, parse_errors=errors)


def extract(path: Path) -> list[Segment]:
    return parse_document(path).segments


def uncovered_text_nodes(doc: HwpxDoc) -> list[tuple[str, str]]:
    """세그먼트가 덮지 않은 텍스트 노드 — 있으면 그만큼이 유출 통로다.

    (멤버, 요소 이름) 만 돌려준다. 값은 절대 싣지 않는다.
    """
    covered: set[tuple[int, str]] = set()
    for seg in doc.segments:
        for piece in seg.pieces:
            if piece.slot in ("text", "tail"):
                covered.add((id(piece.node), piece.slot))

    out: list[tuple[str, str]] = []
    for member, part in doc.parts.items():
        for el in part.tree.getroot().iter():
            if not isinstance(el.tag, str):
                continue
            for slot in ("text", "tail"):
                value = el.text if slot == "text" else el.tail
                if value and value.strip() and (id(el), slot) not in covered:
                    out.append((member, f"{el.tag.rsplit('}', 1)[-1]}@{slot}"))
    return out
