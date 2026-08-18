"""마크다운·평문 추출기.

표는 셀 단위 세그먼트로 쪼갠다. 그래야 헤더('생년월일')와 값이 다른 줄에
있어도 문맥을 연결할 수 있다 — 표 문맥은 L2 탐지의 재료다.
Piece.node 는 줄 번호, Piece.node_offset 은 그 줄 안에서의 시작 위치라
치환은 언제나 원래 줄의 정확한 자리로 되돌아간다.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..model import KIND_CELL, KIND_LINE, Piece, Segment, TableCtx

RE_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
RE_TABLE_SEP = re.compile(r"^\s*\|(?:\s*:?-{2,}:?\s*\|)+\s*$")


def _split_row(line: str) -> list[tuple[int, str]]:
    """표 행을 (줄 안 시작 위치, 셀 텍스트) 목록으로 — 위치를 잃지 않는다."""
    cells: list[tuple[int, str]] = []
    bars = [i for i, ch in enumerate(line) if ch == "|"]
    for left, right in zip(bars, bars[1:]):
        cells.append((left + 1, line[left + 1:right]))
    return cells


def extract_text(text: str, part: str = "") -> list[Segment]:
    lines = text.split("\n")
    segments: list[Segment] = []

    # 표 블록을 먼저 찾아 헤더 행을 확보한다
    header_of: dict[int, list[str]] = {}   # 줄 번호 → 열별 헤더
    table_of: dict[int, str] = {}          # 줄 번호 → 표 id
    row_no: dict[int, int] = {}
    table_seq = 0
    idx = 0
    while idx < len(lines):
        if RE_TABLE_ROW.match(lines[idx]) and idx + 1 < len(lines) and RE_TABLE_SEP.match(lines[idx + 1]):
            table_seq += 1
            table_id = f"T{table_seq}"
            headers = [c.strip() for _, c in _split_row(lines[idx])]
            body = idx + 2
            rank = 0
            header_of[idx] = headers
            table_of[idx] = table_id
            row_no[idx] = 0
            while body < len(lines) and RE_TABLE_ROW.match(lines[body]):
                rank += 1
                header_of[body] = headers
                table_of[body] = table_id
                row_no[body] = rank
                body += 1
            idx = body
            continue
        idx += 1

    for line_no, line in enumerate(lines):
        if line_no in table_of:
            headers = header_of[line_no]
            for col, (offset, cell) in enumerate(_split_row(line)):
                ctx = TableCtx(
                    table_id=table_of[line_no], row=row_no[line_no], col=col,
                    header_text=headers[col] if col < len(headers) else "",
                    header_axis="col", is_header=row_no[line_no] == 0,
                )
                segments.append(Segment(
                    seg_id=f"L{line_no:05d}C{col:02d}", text=cell, kind=KIND_CELL,
                    part=part, order=len(segments), table_ctx=ctx,
                    pieces=[Piece(node=line_no, slot="line", start=0,
                                  length=len(cell), node_offset=offset)],
                ))
            continue

        segments.append(Segment(
            seg_id=f"L{line_no:05d}", text=line, kind=KIND_LINE, part=part,
            order=len(segments),
            pieces=[Piece(node=line_no, slot="line", start=0, length=len(line))],
        ))

    return segments


def extract(path: Path) -> list[Segment]:
    return extract_text(path.read_text(encoding="utf-8"), part=path.name)


def line_count(segments: list[Segment]) -> int:
    return max((p.node for s in segments for p in s.pieces if isinstance(p.node, int)),
               default=-1) + 1
