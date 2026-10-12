"""PDF 추출 — 문자 단위 좌표를 함께 들고 온다.

PDF는 글자를 좌표에 찍어 둔 것이라 문장을 다시 흘려 넣을 수 없다. 그래서
가명으로 바꾸는 대신 검은 칸으로 덮는다(레댁션). 덮을 자리를 알려면 탐지
구간에 해당하는 문자들의 사각형이 필요하고, 그것을 Piece.ref 에 담는다.

쪽에 찍힌 글자 말고도 글자가 사는 자리가 있다 — 주석(메모), 양식 필드에 적어 넣은
값, 책갈피 제목, 링크 주소, 첨부 파일. 뷰어에서 그대로 읽히는데 쪽 글자를 뽑는
통로로는 나오지 않는다. 이 자리들은 KIND_HIDDEN 세그먼트로 따로 올린다.
"""

from __future__ import annotations

from pathlib import Path

from ..errors import DocumentRejected, UnsupportedFormat
from ..model import KIND_HIDDEN, KIND_LINE, Piece, Segment

# 숨은 자리 세그먼트의 조각 슬롯 — 치환기가 이 이름으로 갈래를 탄다
SLOT_CHARS = "chars"
SLOT_ANNOT = "annot"            # node = (쪽 번호, xref)
SLOT_FORM = "form"              # node = (쪽 번호, xref)
SLOT_LINK = "link"              # node = (쪽 번호, 그 쪽에서의 순번)
SLOT_BOOKMARK = "bookmark"      # node = 책갈피 순번
SLOT_ATTACHMENT = "attachment"  # node = 첨부 이름
ANNOT_FIELDS = ("content", "title", "subject")


def _fitz():
    try:
        import fitz  # PyMuPDF
    except ImportError as exc:  # pragma: no cover
        raise UnsupportedFormat("PDF 처리에는 PyMuPDF 가 필요합니다") from exc
    return fitz


def extract(path: Path) -> list[Segment]:
    fitz = _fitz()
    segments: list[Segment] = []
    order = 0
    text_pages = 0

    with fitz.open(path) as doc:
        for page in doc:
            page_text = page.get_text("text").strip()
            if page_text:
                text_pages += 1
            data = page.get_text("rawdict")
            for block in data.get("blocks", []):
                for line in block.get("lines", []):
                    buf: list[str] = []
                    rects: list[tuple] = []
                    for span in line.get("spans", []):
                        for ch in span.get("chars", []):
                            buf.append(ch["c"])
                            rects.append(tuple(ch["bbox"]))
                    text = "".join(buf)
                    if not text.strip():
                        continue
                    segments.append(Segment(
                        seg_id=f"p{page.number:04d}s{order:05d}", text=text,
                        kind=KIND_LINE, part=f"page{page.number + 1}", order=order,
                        pieces=[Piece(node=page.number, slot=SLOT_CHARS, start=0,
                                      length=len(text), ref=rects)],
                    ))
                    order += 1

        order = _hidden_segments(doc, segments, order)
        page_count = doc.page_count

    if page_count and text_pages == 0:
        raise DocumentRejected(
            "글자층이 없는 PDF(스캔본·이미지)입니다. 기계가 읽지 못하는 글자는 "
            "비식별을 보장할 수 없습니다. 원본 문서 파일로 작업하십시오.")

    return segments


def _hidden(segments: list[Segment], order: int, seg_id: str, text: str, part: str,
            node, slot: str) -> int:
    if not text or not text.strip():
        return order
    segments.append(Segment(
        seg_id=seg_id, text=text, kind=KIND_HIDDEN, part=part, order=order,
        pieces=[Piece(node=node, slot=slot, start=0, length=len(text))]))
    return order + 1


def _hidden_segments(doc, segments: list[Segment], order: int) -> int:
    """쪽 글자 밖의 자리 — 주석·양식 값·링크 주소·책갈피·첨부 파일 이름."""
    for page in doc:
        where = f"page{page.number + 1}"
        for annot in page.annots() or []:
            info = annot.info or {}
            for name in ANNOT_FIELDS:
                order = _hidden(segments, order, f"p{page.number:04d}a{annot.xref}:{name}",
                                str(info.get(name) or ""), f"{where}/annot",
                                (page.number, annot.xref), f"{SLOT_ANNOT}:{name}")
        for widget in page.widgets() or []:
            for name, value in (("value", widget.field_value), ("label", widget.field_label)):
                if isinstance(value, str):
                    order = _hidden(segments, order,
                                    f"p{page.number:04d}w{widget.xref}:{name}", value,
                                    f"{where}/form", (page.number, widget.xref), SLOT_FORM)
        for index, link in enumerate(page.get_links()):
            order = _hidden(segments, order, f"p{page.number:04d}l{index:04d}",
                            str(link.get("uri") or ""), f"{where}/link",
                            (page.number, index), SLOT_LINK)

    for index, entry in enumerate(doc.get_toc(simple=True)):
        order = _hidden(segments, order, f"toc{index:05d}", str(entry[1]), "bookmark",
                        index, SLOT_BOOKMARK)

    for name in doc.embfile_names():
        info = doc.embfile_info(name)
        label = " ".join(str(info.get(key) or "") for key in ("filename", "desc")
                         if info.get(key))
        order = _hidden(segments, order, f"embfile:{name}", label or name,
                        "attachment", name, SLOT_ATTACHMENT)
    return order
