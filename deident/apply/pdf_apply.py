"""PDF 처리기 — 레댁션 전용.

PDF는 가명으로 바꾸지 않는다. 글자를 지우고 다시 흘려 넣으면 줄이 밀려
문서가 망가지기 때문이다. 대신 해당 자리를 덮어 지운다.

저장할 때 증분 저장(incremental)을 쓰면 안 된다. 옛 객체가 파일 뒤에 그대로
남아 원문이 살아 있는 고전적 유출이 된다 — garbage 수집으로 다시 쓴다.

쪽 글자 밖의 자리는 자리마다 다르게 다룬다.

  · 주석 — 작성자 이름은 늘 비운다. 내용에 민감정보가 있으면 주석을 지운다.
  · 양식 필드 — 값에 민감정보가 있으면 필드를 지운다.
  · 링크 — 주소에 민감정보(mailto: 등)가 있으면 링크를 지운다.
  · 책갈피 — 제목은 평범한 글자라 가명으로 바꿔 쓴다.
  · 첨부 파일 — 전부 뗀다. 비식별본에 원본 파일을 실어 보낼 이유가 없다.
"""

from __future__ import annotations

from pathlib import Path

from ..extract import pdf_ as pdf_extract
from ..extract.pdf_ import (
    SLOT_ANNOT, SLOT_BOOKMARK, SLOT_CHARS, SLOT_FORM, SLOT_LINK,
)
from ..model import DocumentIssue, Replacement, Segment
from .planner import apply_to_text


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
        annots: set[tuple[int, int]] = set()      # 지울 주석 (쪽, xref)
        forms: set[tuple[int, int]] = set()       # 지울 양식 필드
        links: dict[int, set[int]] = {}           # 지울 링크 (쪽 → 순번)
        titles: dict[int, str] = {}               # 고쳐 쓸 책갈피 제목

        if not segments:
            segments = pdf_extract.extract(path)

        for seg in segments:
            seg_plans = plans.get(seg.seg_id)
            if not seg_plans or not seg.pieces:
                continue
            piece = seg.pieces[0]
            slot = piece.slot.split(":", 1)[0]
            if slot == SLOT_CHARS:
                rects = piece.ref or []
                for plan in seg_plans:
                    covered = rects[plan.start:plan.end]
                    if covered:
                        by_page.setdefault(int(piece.node), []).extend(covered)
            elif slot == SLOT_ANNOT:
                # 작성자 칸은 아래에서 모든 주석에 대해 비운다 — 그것만으로 주석을 지우지 않는다
                if not piece.slot.endswith(":title"):
                    annots.add(tuple(piece.node))
            elif slot == SLOT_FORM:
                forms.add(tuple(piece.node))
            elif slot == SLOT_LINK:
                page_no, index = piece.node
                links.setdefault(page_no, set()).add(index)
            elif slot == SLOT_BOOKMARK:
                titles[int(piece.node)] = apply_to_text(seg.text, seg_plans)

        doc = fitz.open(path)
        try:
            removed = {"주석": 0, "양식 필드": 0, "링크": 0}
            for page in doc:
                # 링크는 순번으로 기억해 두었으므로 다른 것을 지우기 전에 먼저 고른다
                doomed = [link for index, link in enumerate(page.get_links())
                          if index in links.get(page.number, ())]
                for link in doomed:
                    page.delete_link(link)
                    removed["링크"] += 1

                for widget in list(page.widgets() or []):
                    if (page.number, widget.xref) in forms:
                        page.delete_widget(widget)
                        removed["양식 필드"] += 1

                for annot in list(page.annots() or []):
                    attached = annot.type[0] == fitz.PDF_ANNOT_FILE_ATTACHMENT
                    if attached or (page.number, annot.xref) in annots:
                        page.delete_annot(annot)
                        removed["주석"] += 1
                    elif (annot.info or {}).get("title"):
                        # 주석 작성자 — 워드 메모의 작성자와 같은 자리다
                        annot.set_info(title="")
                        annot.update()

                rects = by_page.get(page.number)
                if rects:
                    for rect in rects:
                        page.add_redact_annot(fitz.Rect(rect), fill=(0, 0, 0))
                    page.apply_redactions()

            if titles:
                toc = doc.get_toc(simple=False)
                for index, title in titles.items():
                    if index < len(toc):
                        toc[index][1] = title
                doc.set_toc(toc)

            attachments = list(doc.embfile_names())
            for name in attachments:
                doc.embfile_del(name)

            gone = [f"{label} {count}개" for label, count in removed.items() if count]
            if attachments:
                gone.append(f"첨부 파일 {len(attachments)}개")
            if gone:
                issues.append(DocumentIssue(
                    level="warn", code="pdf-removed",
                    message="민감정보가 든 자리를 지웠습니다: " + ", ".join(gone) + ". "
                            "첨부 파일은 내용과 무관하게 모두 뗍니다.",
                    where=path.name))

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
