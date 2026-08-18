"""PDF 레댁션 시험.

PDF는 가명으로 바꾸지 않고 덮는다. 확인할 것은 두 가지 — 글자층에서 사라졌는가,
그리고 파일 바이트 어디에도 남지 않았는가. 증분 저장을 하면 옛 객체에 원문이
그대로 남아 앞의 검사만으로는 통과해 버린다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deident import formats, pipeline  # noqa: E402,F401
from deident.config import default_config  # noqa: E402
from deident.entity.pseudonym import Mapping  # noqa: E402
from deident.errors import DocumentRejected  # noqa: E402
from synth import PLANTED  # noqa: E402

fitz = pytest.importorskip("fitz")

KOREAN_FONTS = [
    "/System/Library/Fonts/Supplemental/AppleGothic.ttf",
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",
]


def _korean_font() -> str:
    for candidate in KOREAN_FONTS:
        if Path(candidate).exists():
            return candidate
    pytest.skip("한글 글꼴이 없어 PDF 시험을 건너뜁니다")


def _make_pdf(path: Path) -> None:
    font = _korean_font()
    doc = fitz.open()
    page = doc.new_page()
    page.insert_font(fontname="KR", fontfile=font)
    lines = [
        "연구개발계획서 (합성 시험본)",
        f"연구책임자: {PLANTED['person_pi']}",
        f"연락처: {PLANTED['phone_pi']}",
        f"이메일: {PLANTED['email_pi']}",
        f"주민등록번호: {PLANTED['rrn_pi']}",
    ]
    y = 80
    for line in lines:
        page.insert_text((60, y), line, fontname="KR", fontsize=12)
        y += 26
    doc.set_metadata({"author": PLANTED["person_pi"], "title": "연구계획서"})
    doc.save(path)
    doc.close()


def test_pdf_redaction_removes_text_and_bytes(tmp_path: Path):
    config = default_config(tmp_path)
    config.workspace.prepare()
    source = tmp_path / "계획서.pdf"
    _make_pdf(source)

    result = pipeline.apply_file(source, config, Mapping())
    assert result.gate is not None and result.gate.passed

    out = next(p for p in result.outputs if p.suffix == ".pdf")
    doc = fitz.open(out)
    text = "".join(page.get_text() for page in doc)
    assert doc.metadata.get("author") in ("", None), "작성자 메타데이터가 남았다"
    doc.close()

    raw = out.read_bytes()
    for key in ("person_pi", "phone_pi", "email_pi", "rrn_pi"):
        assert PLANTED[key] not in text, f"{key} 가 글자층에 남았다"
        assert PLANTED[key].encode("utf-8") not in raw, f"{key} 가 파일 바이트에 남았다"


def test_scanned_pdf_is_rejected(tmp_path: Path):
    """글자층이 없는 스캔본은 보장할 수 없으므로 거부한다."""
    config = default_config(tmp_path)
    config.workspace.prepare()
    source = tmp_path / "스캔본.pdf"
    doc = fitz.open()
    doc.new_page()          # 빈 쪽 = 글자층 없음
    doc.save(source)
    doc.close()

    with pytest.raises(DocumentRejected):
        pipeline.scan_file(source, config, Mapping())
