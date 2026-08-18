"""포맷 핸들러 등록 — import 만으로 파이프라인에 배선된다.

포맷별 의존 라이브러리가 없으면 그 포맷만 조용히 빠지고 나머지는 계속 돈다.
"""

from __future__ import annotations

from . import pipeline
from .apply.md_apply import MarkdownHandler

pipeline.register(MarkdownHandler())

try:
    from .apply.hwpx_apply import HwpxHandler
except ImportError:  # pragma: no cover - lxml 미설치 환경
    HwpxHandler = None
else:
    pipeline.register(HwpxHandler())

try:
    from .apply.docx_apply import DocxHandler
except ImportError:  # pragma: no cover
    DocxHandler = None
else:
    pipeline.register(DocxHandler())

try:
    from .apply.xlsx_apply import CsvHandler, XlsxHandler
except ImportError:  # pragma: no cover
    XlsxHandler = CsvHandler = None
else:
    pipeline.register(XlsxHandler())
    pipeline.register(CsvHandler())

try:
    from .apply.pptx_apply import PptxHandler
except ImportError:  # pragma: no cover
    PptxHandler = None
else:
    pipeline.register(PptxHandler())

try:
    from .apply.pdf_apply import PdfHandler
except ImportError:  # pragma: no cover - PyMuPDF 미설치
    PdfHandler = None
else:
    pipeline.register(PdfHandler())

try:
    from .apply.hwp_apply import HwpHandler
except ImportError:  # pragma: no cover - olefile 미설치
    HwpHandler = None
else:
    pipeline.register(HwpHandler())
