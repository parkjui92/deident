"""파이프라인 — 추출·탐지·치환·검증을 잇는다.

포맷별 처리기는 registry 에 등록된 핸들러가 담당하고, 이 모듈은 순서와
불변식(게이트 통과분만 out/) 만 책임진다.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from .apply.planner import apply_to_text, group_by_segment, plan_replacements
from .apply.sweep import sweep, sweep_text
from .config import Config
from .detect import l1_patterns, l2_structure, l3_ner, merge, rules
from .entity.pseudonym import Mapping
from .errors import UnsupportedFormat
from .model import DocumentIssue, Finding, Replacement, Segment
from .verify import context_risk
from .verify.gate import GateResult, run_gate


class FormatHandler(Protocol):
    """포맷 하나를 다루는 최소 규약."""

    name: str
    suffixes: tuple[str, ...]
    preserves_layout: bool

    def extract(self, path: Path) -> list[Segment]: ...

    def apply(self, path: Path, out_path: Path,
              plans: dict[str, list[Replacement]],
              segments: list[Segment]) -> list[DocumentIssue]: ...


_HANDLERS: list[FormatHandler] = []


def register(handler: FormatHandler) -> None:
    _HANDLERS.append(handler)


def handler_for(path: Path) -> FormatHandler:
    suffix = path.suffix.lower()
    for handler in _HANDLERS:
        if suffix in handler.suffixes:
            return handler
    raise UnsupportedFormat(f"지원하지 않는 형식입니다: {suffix or '(확장자 없음)'}")


# ── 탐지 ────────────────────────────────────────────────────────────────────
DetectHook = Callable[[list[Segment], Config, Mapping], list[Finding]]
_EXTRA_DETECTORS: list[DetectHook] = []


def register_detector(hook: DetectHook) -> None:
    """L2·L3 처럼 문서 전체 문맥이 필요한 탐지기를 끼워 넣는다."""
    _EXTRA_DETECTORS.append(hook)


def detect(segments: list[Segment], config: Config, mapping: Mapping,
           quick: bool = False) -> list[Finding]:
    """L1(정규식) → L2(표 수확·전파) → L3(형태소·접미사) 순으로 훑는다.

    quick 은 사전 경보용 — 무거운 형태소 분석을 건너뛴다. 대신 표 수확은
    남긴다. 이름은 대개 거기서 나오기 때문이다.
    """
    findings: list[Finding] = []
    for seg in segments:
        findings.extend(l1_patterns.detect_segment(seg))
        findings.extend(rules.detect_segment(seg, config))

    l2_findings, registry = l2_structure.detect(segments, config)
    findings.extend(l2_findings)

    if not quick:
        findings.extend(l3_ner.detect(segments, config, registry))

    for hook in _EXTRA_DETECTORS:
        findings.extend(hook(segments, config, mapping))

    texts = {s.seg_id: s.text for s in segments}
    return merge.merge_findings(findings, config, texts)


# ── 실행 결과 ───────────────────────────────────────────────────────────────
@dataclass
class RunResult:
    source: Path
    segments: list[Segment]
    findings: list[Finding]
    outputs: list[Path] = field(default_factory=list)
    gate: GateResult | None = None
    issues: list[DocumentIssue] = field(default_factory=list)

    blocked: bool = False

    @property
    def ok(self) -> bool:
        if self.blocked:
            return False
        return self.gate is None or self.gate.passed


def scan_file(path: Path, config: Config, mapping: Mapping) -> RunResult:
    handler = handler_for(path)
    segments = handler.extract(path)
    findings = detect(segments, config, mapping)
    return RunResult(source=path, segments=segments, findings=findings)


def _markdown_output(handler: FormatHandler, path: Path, segments: list[Segment],
                     plans: dict[str, list[Replacement]]) -> str:
    """AI 입력용 비식별 텍스트.

    핸들러가 자기 방식의 마크다운 복원을 제공하면 그것을 쓰고(줄·표 모양 보존),
    없으면 세그먼트를 문서 순서대로 잇되 표는 행 단위로 다시 묶는다.
    """
    to_markdown = getattr(handler, "to_markdown", None)
    if callable(to_markdown):
        return to_markdown(path, segments, plans)

    lines: list[str] = []
    row_key: tuple[str, int] | None = None
    row_cells: list[str] = []

    def flush() -> None:
        nonlocal row_cells, row_key
        if row_cells:
            lines.append("| " + " | ".join(row_cells) + " |")
            row_cells = []
        row_key = None

    for seg in segments:
        text = seg.text
        seg_plans = plans.get(seg.seg_id)
        if seg_plans:
            text = apply_to_text(text, seg_plans)
        ctx = seg.table_ctx
        if ctx is None:
            flush()
            lines.append(text)
            continue
        key = (ctx.table_id, ctx.row)
        if key != row_key:
            flush()
            row_key = key
        row_cells.append(text.replace("\n", " ").strip())
    flush()
    return "\n".join(lines)


def _call_apply(handler: FormatHandler, path: Path, out_path: Path,
                grouped: dict[str, list[Replacement]], segments: list[Segment],
                mapping: Mapping) -> list[DocumentIssue]:
    """매핑을 받는 핸들러에는 매핑도 넘긴다.

    XML 속성(도형 이름·대체 텍스트)에 숨은 원값을 지우려면 핸들러가 매핑을 알아야
    한다. 예전 서명을 쓰는 핸들러도 그대로 돌아가도록 서명을 확인해 넘긴다.
    """
    try:
        takes_mapping = "mapping" in inspect.signature(handler.apply).parameters
    except (TypeError, ValueError):
        takes_mapping = False
    if takes_mapping:
        return handler.apply(path, out_path, grouped, segments, mapping=mapping)
    return handler.apply(path, out_path, grouped, segments)


def apply_file(path: Path, config: Config, mapping: Mapping, *,
               strict_only: bool = False, mask_mode: bool = False,
               emit_markdown: bool = True) -> RunResult:
    """탐지 → 치환 → 위생 → 게이트. 게이트 통과분만 out/ 에 남는다."""
    ws = config.workspace
    ws.prepare()

    handler = handler_for(path)
    segments = handler.extract(path)
    findings = detect(segments, config, mapping)

    seg_index = {s.seg_id: s for s in segments}
    plans = plan_replacements(findings, seg_index, mapping, config,
                              strict_only=strict_only, mask_mode=mask_mode)
    if not mask_mode:
        # 한 번 민감하다고 판정된 값은 문맥이 없는 자리에서도 지운다
        plans.extend(sweep(segments, mapping, config, plans))
    grouped = group_by_segment(plans)

    result = RunResult(source=path, segments=segments, findings=findings)
    staged: list[Path] = []

    # 안전을 보장할 수 없는 문서(변경 추적이 켜진 워드 등)는 여기서 멈춘다.
    # 반쪽짜리 산출물을 내놓는 것보다 만들지 않는 편이 낫다.
    inspect_hook = getattr(handler, "inspect", None)
    if callable(inspect_hook):
        result.issues.extend(inspect_hook(path, segments))
    # 치환으로 없앨 수 없는 위험은 표시만 한다 (게이트의 사각지대를 드러낸다)
    result.issues.extend(context_risk.scan(segments))
    if any(i.level == "block" for i in result.issues):
        result.blocked = True
        return result

    if handler.preserves_layout:
        out_path = ws.out / f"{path.stem}.비식별{path.suffix}"
        result.issues.extend(
            _call_apply(handler, path, out_path, grouped, segments, mapping))
        staged.append(out_path)

    md_path = ws.out / f"{path.stem}.비식별.md"
    # 원본이 이미 텍스트면 서식 보존본이 곧 마크다운본이다 — 같은 파일을 두 번 쓰지 않는다
    if emit_markdown and md_path not in staged:
        rendered = _markdown_output(handler, path, segments, grouped)
        if not mask_mode:
            # 다시 그리면서 이어 붙은 자리에 이름이 되살아나는 일이 있다
            rendered = sweep_text(rendered, mapping, config)
        md_path.write_text(rendered, encoding="utf-8")
        staged.append(md_path)

    if any(i.level == "block" for i in result.issues):
        # 치환 도중 발견된 차단 사유 — 이미 만든 산출물을 지운다
        for p in staged:
            if p.exists():
                p.unlink()
        result.blocked = True
        return result

    # 게이트: 하나라도 불합격이면 전부 격리한다 (부분 통과는 사고의 씨앗)
    gates = [run_gate(p, mapping, config, result.issues, strict_only=strict_only)
             for p in staged if p.exists()]
    failed = [g for g in gates if not g.passed]
    if failed:
        merged = GateResult(
            passed=False,
            rescan_findings=[f for g in failed for f in g.rescan_findings],
            leaks=[l for g in failed for l in g.leaks],
            issues=result.issues,
            quarantined=next((g.quarantined for g in failed if g.quarantined), None),
        )
        for p in staged:
            if p.exists():
                target = ws.failed / p.name
                p.replace(target)
        result.gate = merged
        result.outputs = []
    else:
        result.gate = gates[0] if gates else None
        result.outputs = [p for p in staged if p.exists()]

    return result
