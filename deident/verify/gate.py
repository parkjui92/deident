"""검증 게이트 — out/ 불변식의 집행자.

보증 범위를 정직하게 적는다.
  보증하는 것 ① 탐지된 값은 산출물에서 제거되었다
             ② 매핑에 등재된 원값은 산출물 어디에도(바이트 수준) 없다
  보증하지 못하는 것: 애초에 탐지가 놓친 값. 결정적 탐지기의 재실행은 같은
  것을 다시 놓친다. 그래서 계층 다양성·사람 검토·카나리 테스트가 함께 간다.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Config
from ..entity.pseudonym import Mapping
from ..model import CATEGORY_LABEL, GRADE_CERTAIN, DocumentIssue, Finding
from .exhaustive import find_leaks
from .rescan import output_segments, rescan


@dataclass
class GateResult:
    passed: bool
    rescan_findings: list[Finding] = field(default_factory=list)
    leaks: list[tuple[str, str]] = field(default_factory=list)
    issues: list[DocumentIssue] = field(default_factory=list)
    quarantined: Path | None = None

    def summary_lines(self) -> list[str]:
        """원값을 담지 않는 실패 사유 — stdout 으로 나가도 안전하다."""
        lines = []
        for f in self.rescan_findings:
            label = CATEGORY_LABEL.get(f.category, f.category)
            lines.append(f"재탐지: {label} @ {f.part or f.seg_id}")
        seen_parts: dict[str, int] = {}
        for _, part in self.leaks:
            seen_parts[part] = seen_parts.get(part, 0) + 1
        for part, count in seen_parts.items():
            lines.append(f"원값 잔존: {part} ({count}건)")
        return lines


def run_gate(output_path: Path, mapping: Mapping, config: Config,
             issues: list[DocumentIssue] | None = None,
             strict_only: bool = False) -> GateResult:
    """산출물을 검사하고, 불합격이면 _private/failed/ 로 격리한다.

    판정 기준은 '적용한 정책'이다. 사용자가 --strict-only 로 '추정 등급은
    남긴다'를 골랐다면, 그 값이 산출물에 남아 있는 것은 실패가 아니라 선택의
    결과다. 그걸 불합격으로 처리하면 옵션 자체가 덫이 된다.
    """
    issues = list(issues or [])

    # 산출물을 한 번만 열어 재스캔과 원값 대조가 같은 텍스트를 보게 한다
    segments = output_segments(output_path)
    findings = rescan(output_path, config, segments)
    if strict_only:
        kept = [f for f in findings if f.grade != GRADE_CERTAIN]
        findings = [f for f in findings if f.grade == GRADE_CERTAIN]
        if kept:
            issues.append(DocumentIssue(
                level="warn", code="strict-only-kept",
                message=(f"'추정' 등급 {len(kept)}건은 --strict-only 정책에 따라 "
                         "그대로 두었습니다. 사람이 확인하십시오."),
                where=output_path.name))

    originals = []
    for original, pseudonym in mapping.originals():
        if original and original != pseudonym:
            originals.append(original)
    leaks = find_leaks(output_path, originals,
                       segment_texts=[(s.part or s.seg_id, s.text) for s in segments])

    blocking = [i for i in issues if i.level == "block"]
    passed = not findings and not leaks and not blocking

    result = GateResult(passed=passed, rescan_findings=findings, leaks=leaks, issues=issues)

    if not passed:
        failed_dir = config.workspace.failed
        failed_dir.mkdir(parents=True, exist_ok=True)
        target = failed_dir / output_path.name
        shutil.move(str(output_path), str(target))
        result.quarantined = target

    return result
