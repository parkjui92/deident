"""PDF 추출 — 문자 단위 좌표를 함께 들고 온다.

PDF는 글자를 좌표에 찍어 둔 것이라 문장을 다시 흘려 넣을 수 없다. 그래서
가명으로 바꾸는 대신 검은 칸으로 덮는다(레댁션). 덮을 자리를 알려면 탐지
구간에 해당하는 문자들의 사각형이 필요하고, 그것을 Piece.ref 에 담는다.
"""

from __future__ import annotations

from pathlib import Path

from ..errors import DocumentRejected, UnsupportedFormat
from ..model import KIND_LINE, Piece, Segment


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
                        pieces=[Piece(node=page.number, slot="chars", start=0,
                                      length=len(text), ref=rects)],
                    ))
                    order += 1

        page_count = doc.page_count

    if page_count and text_pages == 0:
        raise DocumentRejected(
            "글자층이 없는 PDF(스캔본·이미지)입니다. 기계가 읽지 못하는 글자는 "
            "비식별을 보장할 수 없습니다. 원본 문서 파일로 작업하십시오.")

    return segments
