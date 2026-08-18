"""PDF 처리기 — 레댁션 전용.

PDF는 가명으로 바꾸지 않는다. 글자를 지우고 다시 흘려 넣으면 줄이 밀려
문서가 망가지기 때문이다. 대신 해당 자리를 덮어 지운다.

저장할 때 증분 저장(incremental)을 쓰면 안 된다. 옛 객체가 파일 뒤에 그대로
남아 원문이 살아 있는 고전적 유출이 된다 — garbage 수집으로 다시 쓴다.
"""

from __future__ import annotations

from pathlib import Path

from ..extract import pdf_ as pdf_extract
from ..model import DocumentIssue, Replacement, Segment


class PdfHandler:
    name = "pdf"
    suffixes = (".pdf",)
    preserves_layout = True

    def extract(self, path: Path) -> list[Segment]:
        return pdf_extract.extract(path)

    def apply(self, path: Path, out_path: Path,
              plans: dict[str, list[Replacement]],
              segments: list[Segment]) -> list[DocumentIssue]:
        import fitz  # PyMuPDF

        issues: list[DocumentIssue] = []
        by_page: dict[int, list[tuple]] = {}

        for seg in segments:
            seg_plans = plans.get(seg.seg_id)
            if not seg_plans:
                continue
            piece = seg.pieces[0]
            rects = piece.ref or []
            for plan in seg_plans:
                covered = rects[plan.start:plan.end]
                if not covered:
                    continue
                by_page.setdefault(int(piece.node), []).extend(covered)

        doc = fitz.open(path)
        try:
            for page_no, rects in by_page.items():
                page = doc[page_no]
                for rect in rects:
                    page.add_redact_annot(fitz.Rect(rect), fill=(0, 0, 0))
                page.apply_redactions()

            # 메타데이터 소거 — 작성자·회사가 문서 정보에 그대로 남는다
            doc.set_metadata({})
            try:
                doc.del_xml_metadata()
            except Exception:
                issues.append(DocumentIssue(
                    level="warn", code="pdf-xmp",
                    message="XMP 메타데이터를 지우지 못했습니다. 파일 정보를 확인하십시오.",
                    where=path.name))

            # garbage=4: 참조 끊긴 옛 객체까지 회수한다. 증분 저장 금지.
            doc.save(str(out_path), garbage=4, deflate=True, clean=True,
                     incremental=False)
        finally:
            doc.close()

        issues.append(DocumentIssue(
            level="info", code="pdf-redaction",
            message="PDF는 가명 치환이 불가해 검은 칸으로 덮었습니다. "
                    "가명본이 필요하면 함께 만든 .md 를 쓰십시오.",
            where=path.name))
        return issues
