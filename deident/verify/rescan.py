"""산출물 재스캔 — 탐지기를 산출물에 다시 돌린다.

두 겹으로 본다.

  ① 구조 재스캔: 산출물을 **원본과 똑같은 추출기**로 열어 같은 탐지기를 돌린다.
     조이너가 같으므로, 치환기가 노드 경계 문제로 놓친 값은 반드시 다시 잡힌다.
  ② 원시 훑기: 추출기가 모델링하지 않는 부분(미리보기 캐시·차트·customXml 등)을
     태그 단위로 쪼개 훑는다. 태그를 지우고 이어 붙이면 서로 다른 칸의 숫자가
     붙어 있지도 않은 계좌번호를 만들어 내므로, 반드시 쪼갠 조각별로 본다.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..config import Config
from ..detect import l1_patterns, merge, rules
from ..entity.pseudonym import Mapping
from ..model import KIND_LINE, Finding, Piece, Segment
from .exhaustive import RE_TAG, iter_parts

# 추출기가 이미 제대로 훑는 부분 — 원시 훑기에서 중복으로 볼 필요가 없다
MODELED_HINTS = ("section", "document.xml", "header", "footer", "footnote", "endnote")

MIN_CHUNK = 4


def output_segments(path: Path) -> list[Segment]:
    """산출물을 원본과 같은 추출기로 연다. 실패하면 빈 목록 — 원시 훑기가 받는다."""
    from .. import pipeline   # 순환 참조를 피해 늦게 들여온다

    try:
        return pipeline.handler_for(path).extract(path)
    except Exception:
        return []


def _structured(path: Path, config: Config,
                segments: list[Segment] | None = None) -> list[Finding]:
    from .. import pipeline

    segments = output_segments(path) if segments is None else segments
    if not segments:
        return []
    return pipeline.detect(segments, config, Mapping(), quick=True)


def _raw_chunks(path: Path) -> list[Segment]:
    """부분을 태그·줄 단위 조각으로 쪼갠다. 조각을 넘어선 매치는 만들지 않는다."""
    segments: list[Segment] = []
    order = 0
    for part in iter_parts(path):
        if not part.is_text or not part.text:
            continue
        pieces = RE_TAG.split(part.text) if "<" in part.text else part.text.split("\n")
        for chunk in pieces:
            chunk = chunk.strip()
            if len(chunk) < MIN_CHUNK:
                continue
            for line in chunk.split("\n"):
                line = line.strip()
                if len(line) < MIN_CHUNK:
                    continue
                segments.append(Segment(
                    seg_id=f"{part.name}:{order:06d}", text=line, kind=KIND_LINE,
                    part=part.name, order=order,
                    pieces=[Piece(node=None, slot="line", start=0, length=len(line))],
                ))
                order += 1
    return segments


def rescan(path: Path, config: Config,
           segments: list[Segment] | None = None) -> list[Finding]:
    """산출물에서 여전히 탐지되는 민감정보."""
    findings = list(_structured(path, config, segments))

    raw_segments = _raw_chunks(path)
    for seg in raw_segments:
        findings.extend(l1_patterns.detect_segment(seg))
        findings.extend(rules.detect_segment(seg, config))

    texts = {s.seg_id: s.text for s in raw_segments}
    return merge.merge_findings(findings, config, texts)
