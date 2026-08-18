"""구형 .hwp 처리기 — 읽기 전용, 마크다운 산출만.

바이너리 .hwp 는 서식을 유지한 채 안전하게 고치기 어렵다. 레코드 길이·
문자 수 캐시가 곳곳에 흩어져 있어 한 군데만 어긋나도 파일이 열리지 않는다.
그래서 여기서는 텍스트만 뽑아 비식별 마크다운을 만들고, 서식 사본이
필요하면 한글에서 .hwpx 로 저장해 오도록 안내한다.
"""

from __future__ import annotations

from pathlib import Path

from ..extract import hwp_bin
from ..model import DocumentIssue, Replacement, Segment


class HwpHandler:
    name = "hwp"
    suffixes = (".hwp",)
    preserves_layout = False   # 마크다운 산출만 — 서식 보존 치환은 하지 않는다

    def extract(self, path: Path) -> list[Segment]:
        return hwp_bin.extract(path)

    def inspect(self, path: Path, segments: list[Segment]) -> list[DocumentIssue]:
        return [DocumentIssue(
            level="info", code="hwp-readonly",
            message="구형 .hwp 는 서식 보존 치환을 지원하지 않아 비식별 마크다운만 "
                    "만듭니다. 서식 사본이 필요하면 한글에서 .hwpx 로 저장해 "
                    "다시 넣어 주십시오.",
            where=path.name)]

    def apply(self, path: Path, out_path: Path,
              plans: dict[str, list[Replacement]],
              segments: list[Segment]) -> list[DocumentIssue]:
        return []
