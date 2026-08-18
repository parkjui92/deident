"""번호 체계 검증기.

중요 — 체크섬은 신뢰도를 올리는 데만 쓰고, 탈락 근거로 쓰지 않는다.
2020년 10월 이후 발급된 주민등록번호는 뒷자리가 무작위여서 옛 검증식이
성립하지 않는다. 검증식으로 걸러내면 최신 번호를 통째로 놓친다.
"""

from __future__ import annotations

from datetime import date


def _digits(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


def valid_birth_part(six: str) -> bool:
    """앞 6자리가 실재 가능한 날짜인지 (YYMMDD)."""
    if len(six) != 6 or not six.isdigit():
        return False
    month, day = int(six[2:4]), int(six[4:6])
    if not 1 <= month <= 12 or not 1 <= day <= 31:
        return False
    if month in (4, 6, 9, 11) and day > 30:
        return False
    if month == 2 and day > 29:
        return False
    return True


def rrn_checksum_ok(value: str) -> bool:
    """구 주민등록번호 검증식 통과 여부 (2020.10 이전 발급분에만 성립)."""
    d = _digits(value)
    if len(d) != 13:
        return False
    weights = [2, 3, 4, 5, 6, 7, 8, 9, 2, 3, 4, 5]
    total = sum(int(d[i]) * weights[i] for i in range(12))
    return (11 - (total % 11)) % 10 == int(d[12])


def rrn_shape_ok(value: str) -> bool:
    """주민등록번호로 인정할 최소 형태 — 날짜 유효 + 성별코드 1~8."""
    d = _digits(value)
    if len(d) != 13:
        return False
    if not valid_birth_part(d[:6]):
        return False
    return d[6] in "1234567890" and d[6] != "0"


def foreign_id_ok(value: str) -> bool:
    """외국인등록번호 — 뒷자리 첫 숫자가 5~8."""
    d = _digits(value)
    return len(d) == 13 and valid_birth_part(d[:6]) and d[6] in "5678"


def bizno_checksum_ok(value: str) -> bool:
    """사업자등록번호 10자리 검증식."""
    d = _digits(value)
    if len(d) != 10:
        return False
    weights = [1, 3, 7, 1, 3, 7, 1, 3, 5]
    total = sum(int(d[i]) * weights[i] for i in range(9))
    total += (int(d[8]) * 5) // 10
    return (10 - (total % 10)) % 10 == int(d[9])


def corpno_checksum_ok(value: str) -> bool:
    """법인등록번호 13자리 검증식."""
    d = _digits(value)
    if len(d) != 13:
        return False
    weights = [1, 2] * 6
    total = sum(int(d[i]) * weights[i] for i in range(12))
    return (10 - (total % 10)) % 10 == int(d[12])


def luhn_ok(value: str) -> bool:
    """카드번호 Luhn 검증."""
    d = _digits(value)
    if not 13 <= len(d) <= 19:
        return False
    total, parity = 0, len(d) % 2
    for idx, ch in enumerate(d):
        n = int(ch)
        if idx % 2 == parity:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def plausible_birth_date(year: int, month: int, day: int) -> bool:
    """생년월일로 그럴듯한 범위인지 — 연구기간 날짜를 걸러내는 보조 판정."""
    if not 1900 <= year <= date.today().year:
        return False
    try:
        date(year, month, day)
    except ValueError:
        return False
    return True
