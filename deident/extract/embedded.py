"""포함 문서 — 문서 안에 통째로 들어 있는 오피스 파일.

차트를 넣으면 그 데이터가 엑셀 파일째 문서 안에 실린다(`…/embeddings/*.xlsx`).
화면에는 그래프만 보이지만 '데이터 편집'을 누르면 원본 표가 그대로 열린다.
압축 안의 압축이라 바깥 파일의 글자만 훑어서는 보이지 않는다.

포함 문서의 세그먼트를 바깥 문서의 세그먼트 목록에 함께 올린다. 그래야 대장이
하나로 묶여, 본문의 이름과 차트 데이터의 이름이 같은 가명을 받는다.
"""

from __future__ import annotations

import tempfile
import zipfile
from pathlib import Path
from typing import Iterable

from ..model import KIND_HIDDEN, DocumentIssue, Replacement, Segment
from ..errors import DocumentRejected

EMBED_SUFFIXES = (".xlsx", ".xlsm", ".docx", ".pptx", ".pptm")
SEP = ">"


def embedded_members(names: Iterable[str]) -> list[str]:
    return sorted(n for n in names if n.lower().endswith(EMBED_SUFFIXES))


def _handler(member: str):
    from .. import pipeline   # 순환 참조를 피해 늦게 들여온다

    return pipeline.handler_for(Path(member))


def extract(zf: zipfile.ZipFile, order_start: int = 0) -> list[Segment]:
    segments: list[Segment] = []
    order = order_start
    for member in embedded_members(zf.namelist()):
        try:
            handler = _handler(member)
            with tempfile.TemporaryDirectory(prefix="deident-embed-") as tmp:
                inner = Path(tmp) / ("embedded" + Path(member).suffix.lower())
                inner.write_bytes(zf.read(member))
                found = handler.extract(inner)
        except Exception as exc:
            # 원값을 찾지도 못했으므로 매핑 대조로 대신 검증할 수 없다.
            raise DocumentRejected(
                "포함 문서를 읽지 못해 검사를 중단했습니다 "
                f"({type(exc).__name__}). 첨부 파일을 따로 확인하십시오.") from None
        prefix = member + SEP
        for seg in found:
            seg.seg_id = prefix + seg.seg_id
            seg.part = prefix + seg.part
            seg.kind = KIND_HIDDEN
            seg.order = order
            if seg.table_ctx is not None:
                seg.table_ctx.table_id = prefix + seg.table_ctx.table_id
            order += 1
            segments.append(seg)
    return segments


def apply(zf: zipfile.ZipFile, plans: dict[str, list[Replacement]],
          mapping=None) -> tuple[dict[str, bytes], list[DocumentIssue]]:
    """포함 문서를 같은 계획으로 고쳐, 바뀐 멤버의 새 바이트를 돌려준다."""
    replaced: dict[str, bytes] = {}
    issues: list[DocumentIssue] = []
    for member in embedded_members(zf.namelist()):
        prefix = member + SEP
        inner_plans = {seg_id[len(prefix):]: items for seg_id, items in plans.items()
                       if seg_id.startswith(prefix)}
        try:
            handler = _handler(member)
            with tempfile.TemporaryDirectory(prefix="deident-embed-") as tmp:
                suffix = Path(member).suffix.lower()
                source = Path(tmp) / ("embedded" + suffix)
                target = Path(tmp) / ("embedded.out" + suffix)
                source.write_bytes(zf.read(member))
                # 계획이 없어도 돌린다 — 작성자 같은 문서 속성은 계획과 무관하게 지운다
                from ..pipeline import _call_apply
                inner = _call_apply(handler, source, target, inner_plans, [], mapping)
                # 포함 문서가 거부되면(변경 추적 등) 바깥 문서도 내보내지 않는다
                issues.extend(DocumentIssue(level=i.level, code=i.code, message=i.message,
                                            where=f"{member} › {i.where}")
                              for i in inner or [] if i.level == "block")
                if target.exists():
                    replaced[member] = target.read_bytes()
                elif not any(i.level == "block" for i in inner or []):
                    raise DocumentRejected("포함 문서의 치환 결과가 없습니다.")
        except Exception as exc:   # noqa: BLE001
            issues.append(DocumentIssue(
                level="block", code="embedded-failed",
                message="포함 문서를 고치지 못해 산출물 생성을 중단했습니다 "
                        f"({type(exc).__name__}).",
                where=member))
    return replaced, issues
