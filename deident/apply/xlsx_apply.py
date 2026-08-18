"""엑셀(.xlsx)·CSV 처리기.

xlsx 는 문자열이 담긴 XML 만 고치고 나머지 멤버는 원본 바이트를 그대로 옮긴다.
그렇게 해야 서식·수식·차트가 살아남는다 (스프레드시트 라이브러리로 열었다
저장하면 조용히 잃는 것들이 있다).
"""

from __future__ import annotations

import csv
import io
import zipfile
from pathlib import Path

from lxml import etree

from ..extract import xlsx_ as xlsx_extract
from ..model import KIND_CELL, DocumentIssue, Piece, Replacement, Segment, TableCtx
from .planner import apply_to_text
from .xml_apply import apply_to_segments

# 작성자·회사가 남는 메타데이터
META_MEMBERS = ("docProps/core.xml", "docProps/app.xml", "docProps/custom.xml")


def _rewrite_zip(source: Path, target: Path, replaced: dict[str, bytes],
                 drop: set[str] = frozenset()) -> None:
    with zipfile.ZipFile(source) as src, zipfile.ZipFile(
            target, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            if info.filename in drop:
                continue
            data = replaced.get(info.filename)
            if data is None:
                data = src.read(info)
            new_info = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            new_info.compress_type = info.compress_type
            new_info.external_attr = info.external_attr
            dst.writestr(new_info, data)


def _clean_metadata(source: Path) -> dict[str, bytes]:
    """작성자·회사 정보를 비운다. 문서 정보 창에 이름이 남는 통로다."""
    cleaned: dict[str, bytes] = {}
    strip_tags = {"creator", "lastModifiedBy", "Manager", "Company", "cp:lastModifiedBy"}
    with zipfile.ZipFile(source) as zf:
        for member in META_MEMBERS:
            if member not in zf.namelist():
                continue
            root = etree.fromstring(zf.read(member))
            changed = False
            for node in root.iter():
                tag = etree.QName(node).localname
                if tag in strip_tags and (node.text or "").strip():
                    node.text = ""
                    changed = True
            if changed:
                cleaned[member] = etree.tostring(root, xml_declaration=True,
                                                 encoding="UTF-8", standalone=True)
    return cleaned


class XlsxHandler:
    name = "xlsx"
    suffixes = (".xlsx", ".xlsm")
    preserves_layout = True

    def extract(self, path: Path) -> list[Segment]:
        return xlsx_extract.extract(path)

    def to_markdown(self, path: Path, segments: list[Segment],
                    plans: dict[str, list[Replacement]]) -> str:
        rows: dict[tuple[str, int], dict[int, str]] = {}
        for seg in segments:
            ctx = seg.table_ctx
            if ctx is None:
                continue
            text = seg.text
            seg_plans = plans.get(seg.seg_id)
            if seg_plans:
                text = apply_to_text(text, seg_plans)
            rows.setdefault((ctx.table_id, ctx.row), {})[ctx.col] = text.replace("\n", " ")

        lines: list[str] = []
        current = None
        for (sheet, row_no) in sorted(rows, key=lambda k: (k[0], k[1])):
            if sheet != current:
                lines.append(f"\n## {sheet}\n")
                current = sheet
            cells = rows[(sheet, row_no)]
            ordered = [cells.get(c, "") for c in range(max(cells) + 1)] if cells else []
            lines.append("| " + " | ".join(ordered) + " |")
        return "\n".join(lines)

    def apply(self, path: Path, out_path: Path,
              plans: dict[str, list[Replacement]],
              segments: list[Segment]) -> list[DocumentIssue]:
        # 세그먼트가 든 노드는 다른 트리일 수 있으니 여기서 다시 파싱한다
        trees, fresh = xlsx_extract.parse(path)
        apply_to_segments(fresh, plans)

        replaced = {name: etree.tostring(tree, xml_declaration=True,
                                         encoding=tree.docinfo.encoding or "UTF-8",
                                         standalone=tree.docinfo.standalone)
                    for name, tree in trees.items()}
        replaced.update(_clean_metadata(path))
        _rewrite_zip(path, out_path, replaced)
        return [DocumentIssue(
            level="info", code="xlsx-shared-strings",
            message="같은 글자를 쓰는 셀은 함께 바뀝니다(엑셀이 문자열을 한 벌만 "
                    "저장하기 때문). 값이 같으면 가명도 같으므로 결과는 동일합니다.",
            where=path.name)]


class CsvHandler:
    name = "csv"
    suffixes = (".csv", ".tsv")
    preserves_layout = True

    @staticmethod
    def _dialect(path: Path):
        return "excel-tab" if path.suffix.lower() == ".tsv" else "excel"

    def _read(self, path: Path) -> list[list[str]]:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        return list(csv.reader(io.StringIO(text), dialect=self._dialect(path)))

    def extract(self, path: Path) -> list[Segment]:
        rows = self._read(path)
        headers = rows[0] if rows else []
        segments: list[Segment] = []
        for row_no, row in enumerate(rows):
            for col, value in enumerate(row):
                if not value.strip():
                    continue
                segments.append(Segment(
                    seg_id=f"R{row_no:05d}C{col:03d}", text=value, kind=KIND_CELL,
                    part=path.name, order=len(segments),
                    table_ctx=TableCtx(
                        table_id="csv", row=row_no, col=col,
                        header_text=(headers[col] if row_no > 0 and col < len(headers)
                                     else ""),
                        header_axis="col"),
                    pieces=[Piece(node=(row_no, col), slot="line", start=0,
                                  length=len(value))],
                ))
        return segments

    def _rendered_rows(self, path: Path, segments: list[Segment],
                       plans: dict[str, list[Replacement]]) -> list[list[str]]:
        rows = self._read(path)
        for seg in segments:
            seg_plans = plans.get(seg.seg_id)
            if not seg_plans:
                continue
            row_no, col = seg.pieces[0].node
            if row_no < len(rows) and col < len(rows[row_no]):
                rows[row_no][col] = apply_to_text(seg.text, seg_plans)
        return rows

    def to_markdown(self, path: Path, segments: list[Segment],
                    plans: dict[str, list[Replacement]]) -> str:
        return "\n".join("| " + " | ".join(row) + " |"
                         for row in self._rendered_rows(path, segments, plans))

    def apply(self, path: Path, out_path: Path,
              plans: dict[str, list[Replacement]],
              segments: list[Segment]) -> list[DocumentIssue]:
        rows = self._rendered_rows(path, segments, plans)
        with out_path.open("w", encoding="utf-8", newline="") as fh:
            csv.writer(fh, dialect=self._dialect(path)).writerows(rows)
        return []
