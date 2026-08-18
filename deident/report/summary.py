"""stdout·summary.json — Claude와 사람이 함께 읽는 유일한 채널.

원값은 절대 담지 않는다. 값은 카테고리별 건수로만, 예시가 필요하면
mask.mask_value 를 거친 것만 넣는다.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..model import CATEGORY_LABEL, Finding
from ..verify.gate import GateResult
from .mask import mask_value


def build_summary(source: Path, findings: list[Finding], outputs: list[Path],
                  gate: GateResult | None, mode: str) -> dict:
    counts: dict[str, dict[str, int]] = {}
    for f in findings:
        bucket = counts.setdefault(f.category, {"확실": 0, "추정": 0})
        bucket[f.grade] = bucket.get(f.grade, 0) + 1

    samples: dict[str, list[str]] = {}
    for f in findings:
        bucket = samples.setdefault(f.category, [])
        if len(bucket) < 3:
            masked = mask_value(f.text, f.category)
            if masked not in bucket:
                bucket.append(masked)

    data = {
        "mode": mode,
        "source": source.name,
        "총_탐지": len(findings),
        "카테고리별": {
            CATEGORY_LABEL.get(cat, cat): {
                "확실": v.get("확실", 0), "추정": v.get("추정", 0),
                "예시(마스킹)": samples.get(cat, []),
            }
            for cat, v in sorted(counts.items())
        },
        "산출물": [p.name for p in outputs],
    }

    if gate is not None:
        data["게이트"] = {
            "판정": "통과" if gate.passed else "불합격",
            "재탐지": len(gate.rescan_findings),
            "원값_잔존": len(gate.leaks),
            "사유": gate.summary_lines()[:20],
            "격리": gate.quarantined.name if gate.quarantined else None,
        }
        data["주의"] = [f"[{i.level}] {i.code}: {i.message}" for i in gate.issues]

    return data


def write_summary(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def render_text(data: dict) -> str:
    lines = [f"■ {data['source']} — {data['mode']}"]
    lines.append(f"  탐지 {data['총_탐지']}건")
    for label, info in data.get("카테고리별", {}).items():
        example = ", ".join(info.get("예시(마스킹)", []))
        detail = f"확실 {info['확실']} / 추정 {info['추정']}"
        lines.append(f"   - {label}: {detail}" + (f"  예: {example}" if example else ""))
    gate = data.get("게이트")
    if gate:
        lines.append(f"  게이트: {gate['판정']}"
                     f" (재탐지 {gate['재탐지']}, 원값 잔존 {gate['원값_잔존']})")
        for reason in gate.get("사유", [])[:10]:
            lines.append(f"    · {reason}")
    for note in data.get("주의", []):
        lines.append(f"  ! {note}")
    if data.get("산출물"):
        lines.append("  산출: " + ", ".join(data["산출물"]))
    return "\n".join(lines)
