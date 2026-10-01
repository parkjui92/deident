"""엑셀(.xlsx) 추출 — 셀에 보이는 글자와, 셀 밖에 숨은 글자를 함께 올린다.

인적사항 명단이 엑셀로 오는 일이 많아 이 통로를 비워 둘 수 없다.

xlsx 는 OOXML zip 이고, 셀에 보이는 글자는 대개 `xl/sharedStrings.xml` 에
한 벌만 저장된 뒤 여러 셀이 그 번호를 가리킨다. 그래서 **문자열 하나 = 세그먼트
하나**로 잡는다. 같은 노드를 여러 세그먼트에 담으면 한쪽을 고친 뒤 다른 쪽의
오프셋이 어긋나 조용히 실패한다.

공유 문자열을 고치면 그것을 가리키는 모든 셀이 함께 바뀌는데, 같은 값은 어차피
같은 가명을 받으므로 결과가 달라지지 않는다.

문자열 말고도 값이 사는 자리가 있다.

  · 숫자 셀 — 주민등록번호·전화번호를 숫자로 넣으면 공유 문자열에 오르지 않고
    시트 XML 의 `<v>` 에 그대로 적힌다 (전화번호는 앞자리 0이 떨어진 채로).
  · 수식 — 결과가 글자면 `<v>` 에 저장돼 있고, 수식 안에 이름이 글자로 박히기도 한다.
  · 셀 영역 밖 — 인쇄 머리글, 목록 상자, 하이퍼링크, 시트 이름, 정의된 이름, 메모,
    도형, 문서 속성. 이쪽은 hidden 모듈이 맡는다.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

from lxml import etree

from ..model import KIND_CELL, KIND_HIDDEN, Piece, Segment, TableCtx
from . import embedded, hidden

try:   # 숫자 셀을 볼지 정할 때 쓰는 머리글 사전
    from ..kdata import HEADER_CATEGORY as KNOWN_HEADERS
except ImportError:  # pragma: no cover
    KNOWN_HEADERS: dict[str, str] = {}

NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS = {"m": NS_MAIN}
RE_CELL_REF = re.compile(r"^([A-Z]+)(\d+)$")
RE_HEADER_CLEAN = re.compile(r"[\s()（）]+")

SHARED_STRINGS = "xl/sharedStrings.xml"
TAG_SHEET_DATA = f"{{{NS_MAIN}}}sheetData"
TAG_V = f"{{{NS_MAIN}}}v"
TAG_F = f"{{{NS_MAIN}}}f"
TAG_IS = f"{{{NS_MAIN}}}is"
TAG_T = f"{{{NS_MAIN}}}t"

# 숫자 셀 세그먼트의 seg_id 머리표 — 치환 뒤 셀 형식을 글자로 바꿀 때 찾는다
NUMERIC_PREFIX = "N:"
# 이 자릿수부터는 머리글이 없어도 본다 (전화 9~11, 사업자 10, 주민 13, 카드 16)
MIN_NUMERIC_DIGITS = 9


def column_index(letters: str) -> int:
    value = 0
    for ch in letters:
        value = value * 26 + (ord(ch) - ord("A") + 1)
    return value - 1


def _cell_text(node: etree._Element) -> str:
    """`<t>` 조각들을 잇는다 (서식이 섞이면 여러 조각으로 쪼개진다)."""
    return "".join(t.text or "" for t in node.iter(TAG_T))


def _sheet_members(zf: zipfile.ZipFile) -> list[str]:
    return sorted(n for n in zf.namelist()
                  if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"))


def _known_header(text: str) -> bool:
    return RE_HEADER_CLEAN.sub("", text) in KNOWN_HEADERS


def _numeric_worth(text: str, header: str) -> bool:
    """이 숫자 셀을 검사 대상에 올릴 것인가.

    모든 숫자를 올리면 금액·수량까지 번호로 오인한다. 자릿수가 번호만큼 길거나,
    머리글이 그 칸을 민감 항목(주민등록번호·생년월일·연락처 …)으로 지목할 때만 본다.
    """
    value = text.strip()
    if not value:
        return False
    if value.isdigit() and len(value) >= MIN_NUMERIC_DIGITS:
        return True
    return bool(header) and _known_header(header)


def _skip_sheet_data(element: etree._Element) -> bool:
    return element.tag == TAG_SHEET_DATA


def parse(path: Path) -> tuple[dict[str, etree._ElementTree], list[Segment], set[str]]:
    """(수정 대상 XML 트리, 세그먼트, 늘 다시 쓰는 멤버) — apply 도 같은 함수를 쓴다."""
    trees: dict[str, etree._ElementTree] = {}
    segments: list[Segment] = []

    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())

        shared_nodes: list[etree._Element] = []
        if SHARED_STRINGS in names:
            tree = etree.fromstring(zf.read(SHARED_STRINGS)).getroottree()
            trees[SHARED_STRINGS] = tree
            shared_nodes = list(tree.getroot().findall(f"{{{NS_MAIN}}}si"))

        # 공유 문자열이 어느 열 머리글 아래에서 쓰였는지 모아 둔다
        shared_context: dict[int, tuple[str, str, int, int]] = {}
        # (seg_id, 멤버, 글자가 든 노드, 표 좌표, 조각을 `<t>` 에서 찾을지)
        cells: list[tuple[str, str, etree._Element, TableCtx | None, bool]] = []
        sheet_names: list[str] = []

        for member in _sheet_members(zf):
            tree = etree.fromstring(zf.read(member)).getroottree()
            trees[member] = tree
            sheet_names.append(member)
            sheet_id = Path(member).stem
            headers: dict[int, str] = {}

            for row in tree.getroot().iter(f"{{{NS_MAIN}}}row"):
                row_no = int(row.get("r", "0"))
                for cell in row.findall(f"{{{NS_MAIN}}}c"):
                    cell_ref = cell.get("r", "")
                    ref = RE_CELL_REF.match(cell_ref)
                    col = column_index(ref.group(1)) if ref else 0
                    kind = cell.get("t")
                    value = cell.find(TAG_V)
                    header = headers.get(col, "") if row_no > 1 else ""
                    ctx = TableCtx(table_id=sheet_id, row=row_no, col=col,
                                   header_text=header, header_axis="col")

                    # 첫 행은 아래 행들의 머리글로 쓰되, 검사 대상에서 빼지 않는다.
                    # 시트마다 첫 행이 머리글이라는 보장이 없어서다 —
                    # '작성자 | 홍길동' 처럼 첫 행에 값이 오는 시트가 흔하다.
                    if kind == "s":
                        if value is not None and (value.text or "").isdigit():
                            index = int(value.text)
                            if index < len(shared_nodes):
                                if row_no == 1:
                                    headers[col] = _cell_text(shared_nodes[index])
                                shared_context.setdefault(
                                    index, (sheet_id, header, row_no, col))
                    elif kind == "inlineStr":
                        node = cell.find(TAG_IS)
                        if node is not None:
                            if row_no == 1:
                                headers[col] = _cell_text(node)
                            cells.append((f"I:{sheet_id}!{cell_ref}", member, node, ctx, True))
                    elif kind == "str":
                        # 수식이 만든 글자 — 수식을 지워도 저장된 결과는 남는다
                        if value is not None and (value.text or "").strip():
                            if row_no == 1:
                                headers[col] = value.text
                            cells.append((f"V:{sheet_id}!{cell_ref}", member, value, ctx, False))
                    elif kind in (None, "n"):
                        if value is not None and _numeric_worth(value.text or "", header):
                            cells.append((f"{NUMERIC_PREFIX}{sheet_id}!{cell_ref}",
                                          member, value, ctx, False))

                    formula = cell.find(TAG_F)
                    if formula is not None and hidden.has_content(formula.text or ""):
                        cells.append((f"F:{sheet_id}!{cell_ref}", member, formula, None, False))

        order = 0
        for index, node in enumerate(shared_nodes):
            text = _cell_text(node)
            if not text.strip():
                continue
            sheet_id, header, row_no, col = shared_context.get(index, ("", "", 0, 0))
            segments.append(_segment_for(
                f"S{index:06d}", node, text, SHARED_STRINGS, order,
                TableCtx(table_id=sheet_id or "shared", row=row_no, col=col,
                         header_text=header, header_axis="col")))
            order += 1

        for seg_id, member, node, ctx, rich in cells:
            if rich:
                text = _cell_text(node)
                if not text.strip():
                    continue
                segments.append(_segment_for(seg_id, node, text, member, order, ctx))
            else:
                text = node.text or ""
                segments.append(Segment(
                    seg_id=seg_id, text=text, part=member, order=order, table_ctx=ctx,
                    kind=KIND_CELL if ctx is not None else KIND_HIDDEN,
                    pieces=[Piece(node=node, slot="text", start=0, length=len(text))]))
            order += 1

        always = set(trees)
        side_trees, side_segments = hidden.collect(
            zf, trees, order_start=order,
            partial={name: _skip_sheet_data for name in sheet_names})
        trees.update(side_trees)
        segments.extend(side_segments)
        segments.extend(embedded.extract(zf, order_start=order + len(side_segments)))

    return trees, segments, always


def _segment_for(seg_id: str, node: etree._Element, text: str, part: str,
                 order: int, ctx: TableCtx) -> Segment:
    """`<t>` 조각들을 이어 붙이고 각 조각의 위치를 기억한다."""
    pieces: list[Piece] = []
    cursor = 0
    for t in node.iter(TAG_T):
        value = t.text or ""
        if not value:
            continue
        pieces.append(Piece(node=t, slot="text", start=cursor, length=len(value)))
        cursor += len(value)
    return Segment(seg_id=seg_id, text=text, pieces=pieces, kind=KIND_CELL,
                   part=part, order=order, table_ctx=ctx)


RE_NUMBER = re.compile(r"^[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?$")


def settle_numeric_cells(segments: list[Segment]) -> None:
    """가명이 들어간 숫자 셀을 글자 셀로 바꾼다.

    숫자 셀의 `<v>` 에 글자가 들어 있으면 엑셀이 파일을 손상된 것으로 본다.
    """
    for seg in segments:
        if not seg.seg_id.startswith(NUMERIC_PREFIX) or not seg.pieces:
            continue
        value = seg.pieces[0].node
        text = value.text or ""
        if RE_NUMBER.match(text.strip()):
            continue
        cell = value.getparent()
        if cell is None:
            continue
        if cell.find(TAG_F) is not None:
            cell.set("t", "str")          # 수식이 있으면 '수식 결과가 글자'로 둔다
            continue
        cell.remove(value)
        holder = etree.SubElement(cell, TAG_IS)
        etree.SubElement(holder, TAG_T).text = text
        cell.set("t", "inlineStr")


def extract(path: Path) -> list[Segment]:
    return parse(path)[1]
