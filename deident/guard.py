"""사전 경보 — 문서를 열기 전에 먼저 훑는다.

쓰임새가 비식별과 다르다. 비식별은 "안전한 사본을 만든다"이고, 경보는
"이 파일을 열어도 되는가"를 먼저 답한다. 그래서 파일을 바꾸지 않고,
빠르게 돌고, 원값을 절대 내보내지 않는다.

AI 도구에 자료를 넘기기 전에 이 검사가 먼저 서면, 원문이 외부로 나가기 전에
사람이 판단할 기회가 생긴다. 순서가 곧 안전이다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .entity.pseudonym import Mapping
from .errors import DeidentError, UnsupportedFormat
from .model import (
    CATEGORY_LABEL, CAT_ACCOUNT, CAT_ADDRESS, CAT_BIRTH, CAT_BIZNO, CAT_CARD,
    CAT_CORPNO, CAT_DEPT, CAT_DRIVER, CAT_EMAIL, CAT_FOREIGN_ID, CAT_ORG,
    CAT_PASSPORT, CAT_PERSON, CAT_PHONE, CAT_RRN, CAT_TEL, CAT_VEHICLE, Finding,
)

# 위험도 — 유출됐을 때 되돌릴 수 없는 정도로 나눈다
CRITICAL = {CAT_RRN, CAT_FOREIGN_ID, CAT_CARD, CAT_ACCOUNT, CAT_PASSPORT, CAT_DRIVER}
HIGH = {CAT_PERSON, CAT_PHONE, CAT_TEL, CAT_EMAIL, CAT_ADDRESS, CAT_BIRTH, CAT_VEHICLE}
MEDIUM = {CAT_ORG, CAT_DEPT, CAT_BIZNO, CAT_CORPNO}

LEVEL_NONE = "없음"
LEVEL_NOTE = "참고"
LEVEL_WARN = "주의"
LEVEL_STOP = "위험"

LEVEL_RANK = {LEVEL_NONE: 0, LEVEL_NOTE: 1, LEVEL_WARN: 2, LEVEL_STOP: 3}

ADVICE = {
    LEVEL_NONE: "그대로 열어도 됩니다.",
    LEVEL_NOTE: "기관·사업 정보가 있습니다. 외부 공유 전 확인하십시오.",
    LEVEL_WARN: "개인 식별정보가 있습니다. 비식별본으로 작업하기를 권합니다.",
    LEVEL_STOP: "주민번호·계좌 등 민감정보가 있습니다. 원문을 외부로 넘기지 마십시오.",
}


@dataclass
class GuardResult:
    path: Path
    level: str = LEVEL_NONE
    counts: dict[str, int] = field(default_factory=dict)
    total: int = 0
    error: str = ""
    checked: bool = True

    @property
    def advice(self) -> str:
        if not self.checked:
            return "검사하지 못했습니다 — 사람이 직접 확인하십시오."
        return ADVICE[self.level]

    def to_dict(self) -> dict:
        return {
            "파일": self.path.name,
            "판정": self.level if self.checked else "검사불가",
            "탐지": self.total,
            "구분별": {CATEGORY_LABEL.get(c, c): n for c, n in sorted(self.counts.items())},
            "권고": self.advice,
            **({"사유": self.error} if self.error else {}),
        }


def _level_for(findings: list[Finding]) -> str:
    categories = {f.category for f in findings}
    if categories & CRITICAL:
        return LEVEL_STOP
    if categories & HIGH:
        return LEVEL_WARN
    if categories:
        return LEVEL_NOTE
    return LEVEL_NONE


def check_file(path: Path, config: Config, deep: bool = True) -> GuardResult:
    """파일 하나를 훑는다. 원값은 결과에 담지 않는다 — 건수와 구분만.

    기본이 정밀 검사(deep)인 이유: 빠른 검사는 표 밖 본문에 있는 이름을 통째로
    놓친다. 실측에서 회의록·검토의견 문서가 빠른 검사로는 '탐지 없음'이었지만
    정밀 검사로는 수십 건이 나왔다. 검사하지 못한 것을 '없음'이라 말하지 않듯,
    덜 본 것도 '없음'이라 말하면 안 된다. 실측 비용은 문서당 0.1~1초다.
    """
    from . import formats  # noqa: F401  (핸들러 등록)
    from . import pipeline

    result = GuardResult(path=path)
    try:
        handler = pipeline.handler_for(path)
        segments = handler.extract(path)
    except UnsupportedFormat as exc:
        result.checked = False
        result.error = str(exc)
        return result
    except DeidentError as exc:
        result.checked = False
        result.error = f"{exc.__class__.__name__}"
        return result
    except Exception as exc:   # 열지 못하는 파일이 경보 전체를 멈추면 안 된다
        result.checked = False
        result.error = f"읽기 실패 ({exc.__class__.__name__})"
        return result

    findings = pipeline.detect(segments, config, Mapping(), quick=not deep)
    for f in findings:
        result.counts[f.category] = result.counts.get(f.category, 0) + 1
    result.total = len(findings)
    result.level = _level_for(findings)
    return result


def check_paths(paths: list[Path], config: Config, deep: bool = True) -> list[GuardResult]:
    return [check_file(p, config, deep) for p in paths]


def overall_level(results: list[GuardResult]) -> str:
    level = LEVEL_NONE
    for r in results:
        if r.checked and LEVEL_RANK[r.level] > LEVEL_RANK[level]:
            level = r.level
    return level


def unchecked(results: list[GuardResult]) -> list[GuardResult]:
    return [r for r in results if not r.checked]


def needs_attention(results: list[GuardResult]) -> bool:
    """사람이 봐야 하는 상태인가.

    검사하지 못한 파일은 '깨끗함'이 아니다. 못 읽은 것을 없다고 말하는 것이
    이런 도구가 저지를 수 있는 가장 나쁜 거짓말이라, 안전한 쪽으로 판정한다.
    """
    return (LEVEL_RANK[overall_level(results)] >= LEVEL_RANK[LEVEL_WARN]
            or bool(unchecked(results)))


MARK = {LEVEL_NONE: "○", LEVEL_NOTE: "·", LEVEL_WARN: "▲", LEVEL_STOP: "■"}


def verdict_line(results: list[GuardResult]) -> str:
    level = overall_level(results)
    skipped = unchecked(results)
    if skipped:
        checked_note = ("검사한 파일에서는 " + ADVICE[level].rstrip(".")
                        if len(results) > len(skipped) else "")
        return (f"판정: 확인 필요 — {len(skipped)}개 파일을 검사하지 못했습니다. "
                f"이 파일들은 민감정보 유무를 알 수 없으니 직접 확인하십시오."
                + (f" ({checked_note})" if checked_note else ""))
    return f"판정: {level} — {ADVICE[level]}"


def render_text(results: list[GuardResult]) -> str:
    lines = ["민감정보 사전 점검 (전 과정 로컬 실행 · 원문은 외부로 나가지 않았습니다)"]
    for r in results:
        if not r.checked:
            lines.append(f"  ? {r.path.name} — 검사 불가: {r.error}")
            continue
        detail = ", ".join(
            f"{CATEGORY_LABEL.get(c, c)} {n}" for c, n in sorted(
                r.counts.items(), key=lambda kv: -kv[1])[:6]
        )
        lines.append(f"  {MARK[r.level]} {r.path.name} — {r.level}"
                     + (f" ({detail})" if detail else " (탐지 없음)"))
    lines.append("")
    lines.append(verdict_line(results))
    if LEVEL_RANK[overall_level(results)] >= LEVEL_RANK[LEVEL_WARN]:
        lines.append("비식별본 만들기:  python3 run.py apply <파일>")
    return "\n".join(lines)


def render_json(results: list[GuardResult]) -> str:
    return json.dumps({
        "판정": "확인필요" if unchecked(results) else overall_level(results),
        "검사불가": len(unchecked(results)),
        "파일": [r.to_dict() for r in results],
    }, ensure_ascii=False, indent=2)
