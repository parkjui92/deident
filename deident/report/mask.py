"""표시용 부분 마스킹.

out/ 로 나가는 리포트·요약은 이 함수를 통과한 문자열만 담는다. 여기에 버그가
있으면 그것이 곧 유출이므로 단위 테스트로 고정한다.
"""

from __future__ import annotations

from ..model import (
    CAT_ACCOUNT, CAT_ADDRESS, CAT_CARD, CAT_EMAIL, CAT_FOREIGN_ID, CAT_PERSON,
    CAT_PHONE, CAT_RRN, CAT_TEL,
)

MASK = "*"


def _keep_edges(value: str, head: int, tail: int) -> str:
    core = value.strip()
    if len(core) <= head + tail:
        return MASK * max(1, len(core))
    return core[:head] + MASK * (len(core) - head - tail) + (core[-tail:] if tail else "")


def mask_value(value: str, category: str) -> str:
    """원값을 사람이 위치만 가늠할 수 있는 형태로 줄인다."""
    value = value.strip()
    if not value:
        return ""

    if category == CAT_PERSON:
        # 홍길동 → 홍*동 / 남궁민수 → 남**수 / 김구 → 김*
        if len(value) <= 2:
            return value[0] + MASK
        return value[0] + MASK * (len(value) - 2) + value[-1]

    if category in (CAT_RRN, CAT_FOREIGN_ID):
        digits = [ch for ch in value if ch.isdigit()]
        return f"{''.join(digits[:2])}****-*******" if len(digits) >= 2 else MASK * 6

    if category in (CAT_PHONE, CAT_TEL):
        digits = "".join(ch for ch in value if ch.isdigit())
        if len(digits) >= 4:
            return MASK * (len(digits) - 4) + digits[-4:]
        return MASK * len(digits)

    if category == CAT_EMAIL and "@" in value:
        local, _, domain = value.partition("@")
        parts = domain.rsplit(".", 1)
        dom = _keep_edges(parts[0], 1, 0) + ("." + parts[1] if len(parts) > 1 else "")
        return f"{_keep_edges(local, 1, 0)}@{dom}"

    if category == CAT_CARD:
        digits = "".join(ch for ch in value if ch.isdigit())
        return MASK * max(0, len(digits) - 4) + digits[-4:]

    if category == CAT_ACCOUNT:
        digits = "".join(ch for ch in value if ch.isdigit())
        return MASK * max(0, len(digits) - 3) + digits[-3:]

    if category == CAT_ADDRESS:
        head = value.split()[0] if value.split() else value[:2]
        return f"{head} " + MASK * 6

    return _keep_edges(value, 1, 0)


def mask_context(text: str, start: int, end: int, category: str, width: int = 12) -> str:
    """탐지 위치 주변을 보여주되 값 자체는 마스킹한다.

    주변 문맥에도 다른 민감정보가 있을 수 있어 out/ 리포트에서는 쓰지 않는다.
    사람 전용 리포트(_private)에서만 사용한다.
    """
    left = text[max(0, start - width):start]
    right = text[end:end + width]
    return f"…{left}⟦{mask_value(text[start:end], category)}⟧{right}…"
