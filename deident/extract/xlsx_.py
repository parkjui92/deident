"""엑셀(.xlsx) 추출 — 공유 문자열과 인라인 문자열을 노드째 다룬다.

인적사항 명단이 엑셀로 오는 일이 많아 이 통로를 비워 둘 수 없다.

xlsx 는 OOXML zip 이고, 셀에 보이는 글자는 대개 `xl/sharedStrings.xml` 에
한 벌만 저장된 뒤 여러 셀이 그 번호를 가리킨다. 그래서 **문자열 하나 = 세그먼트
하나**로 잡는다. 같은 노드를 여러 세그먼트에 담으면 한쪽을 고친 뒤 다른 쪽의
오프셋이 어긋나 조용히 실패한다.

공유 문자열을 고치면 그것을 가리키는 모든 셀이 함께 바뀌는데, 같은 값은 어차피
같은 가명을 받으므로 결과가 달라지지 않는다.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

from lxml import etree

from ..model import KIND_CELL, Piece, Segment, TableCtx

NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS = {"m": NS_MAIN}
RE_CELL_REF = re.compile(r"^([A-Z]+)(\d+)$")

SHARED_STRINGS = "xl/sharedStrings.xml"


def column_index(letters: str) -> int:
    value = 0
    for ch in letters:
        value = value * 26 + (ord(ch) - ord("A") + 1)
    return value - 1


def _cell_text(node: etree._Element) -> str:
    """`<t>` 조각들을 잇는다 (서식이 섞이면 여러 조각으로 쪼개진다)."""
    return "".join(t.text or "" for t in node.iter(f"{{{NS_MAIN}}}t"))


def _sheet_members(zf: zipfile.ZipFile) -> list[str]:
    return sorted(n for n in zf.namelist()
                  if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"))


def parse(path: Path) -> tuple[dict[str, etree._ElementTree], list[Segment]]:
    """(수정 대상 XML 트리, 세그먼트) — apply 도 같은 함수를 써서 순서를 맞춘다."""
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
        inline_cells: list[tuple[str, etree._Element, TableCtx]] = []

        for member in _sheet_members(zf):
            tree = etree.fromstring(zf.read(member)).getroottree()
            trees[member] = tree
            sheet_id = Path(member).stem
            headers: dict[int, str] = {}

            for row in tree.getroot().iter(f"{{{NS_MAIN}}}row"):
                row_no = int(row.get("r", "0"))
                for cell in row.findall(f"{{{NS_MAIN}}}c"):
                    ref = RE_CELL_REF.match(cell.get("r", ""))
                    col = column_index(ref.group(1)) if ref else 0
                    kind = cell.get("t")

                    # 첫 행은 아래 행들의 머리글로 쓰되, 검사 대상에서 빼지 않는다.
                    # 시트마다 첫 행이 머리글이라는 보장이 없어서다 —
                    # '작성자 | 홍길동' 처럼 첫 행에 값이 오는 시트가 흔하다.
                    if kind == "s":
                        value = cell.find(f"{{{NS_MAIN}}}v")
                        if value is None or not (value.text or "").isdigit():
                            continue
                        index = int(value.text)
                        if index >= len(shared_nodes):
                            continue
                        if row_no == 1:
                            headers[col] = _cell_text(shared_nodes[index])
                        shared_context.setdefault(
                            index, (sheet_id, headers.get(col, "") if row_no > 1 else "",
                                    row_no, col))
                    elif kind == "inlineStr":
                        node = cell.find(f"{{{NS_MAIN}}}is")
                        if node is None:
                            continue
                        text = _cell_text(node)
                        if row_no == 1:
                            headers[col] = text
                        inline_cells.append((member, node, TableCtx(
                            table_id=sheet_id, row=row_no, col=col,
                            header_text=headers.get(col, "") if row_no > 1 else "",
                            header_axis="col")))

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

    for member, node, ctx in inline_cells:
        text = _cell_text(node)
        if not text.strip():
            continue
        segments.append(_segment_for(
            f"I{order:06d}", node, text, member, order, ctx))
        order += 1

    return trees, segments


def _segment_for(seg_id: str, node: etree._Element, text: str, part: str,
                 order: int, ctx: TableCtx) -> Segment:
    """`<t>` 조각들을 이어 붙이고 각 조각의 위치를 기억한다."""
    pieces: list[Piece] = []
    cursor = 0
    for t in node.iter(f"{{{NS_MAIN}}}t"):
        value = t.text or ""
        if not value:
            continue
        pieces.append(Piece(node=t, slot="text", start=cursor, length=len(value)))
        cursor += len(value)
    return Segment(seg_id=seg_id, text=text, pieces=pieces, kind=KIND_CELL,
                   part=part, order=order, table_ctx=ctx)


def extract(path: Path) -> list[Segment]:
    return parse(path)[1]
