"""문맥 재식별 경고.

이름을 다 지워도 "국내 유일의 ○○ 장비를 보유한 대학"이면 누구인지 특정된다.
이런 서술은 자동으로 치환할 수 없다 — 지우면 문서의 논지가 사라지기 때문이다.
그래서 치환하지 않고 사람에게 표시만 한다. 게이트가 보증하지 못하는 영역을
조용히 넘기지 않기 위한 장치다.
"""

from __future__ import annotations

import re

from ..model import DocumentIssue, Segment

RE_UNIQUENESS = re.compile(
    r"(국내\s*(?:유일|최초|최대)|세계\s*(?:최초|유일|최대)|유일(?:한|하게)?\s*(?:기관|대학|기업|보유)"
    r"|단독\s*(?:보유|수행|운영)|국내에서\s*(?:유일|처음))"
)
RE_SMALL_COHORT = re.compile(r"(?<![0-9])([1-9])\s*명(?:의)?\s*(?:연구원|참여|전담|박사|교수)")

MAX_ISSUES = 20


def scan(segments: list[Segment]) -> list[DocumentIssue]:
    issues: list[DocumentIssue] = []
    for seg in segments:
        for pattern, code, message in (
            (RE_UNIQUENESS, "context-unique",
             "'유일·최초' 서술은 이름 없이도 기관을 특정할 수 있습니다. 표현을 완화할지 검토하십시오."),
            (RE_SMALL_COHORT, "context-small-cohort",
             "인원이 극소수인 서술은 개인을 좁힐 수 있습니다."),
        ):
            if pattern.search(seg.text):
                issues.append(DocumentIssue(level="warn", code=code, message=message,
                                            where=f"{seg.part}:{seg.seg_id}"))
                break
        if len(issues) >= MAX_ISSUES:
            issues.append(DocumentIssue(
                level="info", code="context-truncated",
                message=f"문맥 재식별 경고가 {MAX_ISSUES}건을 넘어 이후는 생략했습니다.",
                where=""))
            break
    return issues
