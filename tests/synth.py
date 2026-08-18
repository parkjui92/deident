"""합성 픽스처 생성기.

저장소에 실제 개인정보를 두지 않기 위해, 형태만 유효한 허구 값을 만든다.
주민등록번호는 검증식만 만족하는 조합이며 실재 인물과 무관하다.
"""

from __future__ import annotations

import random

SURNAMES = ["김", "이", "박", "최", "정", "강", "조", "윤", "장", "임", "남궁", "황보"]
GIVEN = ["가온", "다솜", "라온", "미르", "바다", "새롬", "아름", "여울", "하람", "슬기", "차오름"]


def rrn(seed: int) -> str:
    """검증식을 통과하는 허구 주민등록번호."""
    rng = random.Random(seed)
    while True:
        yy = rng.randint(60, 99)
        mm = rng.randint(1, 12)
        dd = rng.randint(1, 28)
        body = f"{yy:02d}{mm:02d}{dd:02d}"
        gender = rng.choice("12")
        rest = f"{rng.randint(0, 99999):05d}"
        head12 = body + gender + rest   # 앞 12자리 (검증 대상)
        weights = [2, 3, 4, 5, 6, 7, 8, 9, 2, 3, 4, 5]
        total = sum(int(head12[i]) * weights[i] for i in range(12))
        check = (11 - (total % 11)) % 10
        if check < 10:
            return f"{body}-{gender}{rest}{check}"


def name(seed: int) -> str:
    rng = random.Random(seed)
    return rng.choice(SURNAMES) + rng.choice(GIVEN)


def card(seed: int) -> str:
    """Luhn 을 통과하는 허구 카드번호."""
    rng = random.Random(seed)
    digits = [rng.randint(0, 9) for _ in range(15)]
    digits = [5, 4, 3, 2] + digits[4:]
    total, parity = 0, (16) % 2
    for idx, n in enumerate(digits):
        v = n
        if idx % 2 == parity:
            v *= 2
            if v > 9:
                v -= 9
        total += v
    check = (10 - total % 10) % 10
    full = "".join(str(d) for d in digits) + str(check)
    return "-".join(full[i:i + 4] for i in range(0, 16, 4))


PLANTED = {
    "person_pi": name(1),
    "person_co": name(2),
    "person_ext": name(3),
    "rrn_pi": rrn(11),
    "rrn_co": rrn(12),
    "phone_pi": "010-2345-6789",
    "tel_org": "042-860-1234",
    "email_pi": "gaon.kim@krict.re.kr",
    "email_co": "dasom@snu.ac.kr",
    "addr": "대전광역시 유성구 가정로 141 본관 302호",
    "birth": "1979년 3월 14일",
    "account": "110-234-567890",
    "card": card(21),
    "bizno": "220-82-01234",
    "org_a": "한국화학연구원",
    "org_b": "서울대학교",
    "dept": "탄소중립연구본부",
    "project_no": "1711123456",
    "budget": "1,250백만원",
    "secret_kw": "저온 플라즈마 개질 공정",
}


def research_plan_markdown() -> str:
    p = PLANTED
    return f"""# 2026년도 국가연구개발사업 연구개발계획서

## 1. 연구개발과제 개요

- 과제번호: {p['project_no']}
- 총 연구개발비: {p['budget']}
- 주관연구개발기관: {p['org_a']} {p['dept']}
- 사업자등록번호: {p['bizno']}

## 2. 연구책임자 인적사항

| 구분 | 성명 | 소속 | 직위 | 생년월일 | 연락처 | 이메일 |
|------|------|------|------|----------|--------|--------|
| 연구책임자 | {p['person_pi']} | {p['org_a']} | 책임연구원 | {p['birth']} | {p['phone_pi']} | {p['email_pi']} |
| 공동연구원 | {p['person_co']} | {p['org_b']} | 교수 | 1985년 7월 2일 | 010-9876-5432 | {p['email_co']} |

- 주민등록번호(연구책임자): {p['rrn_pi']}
- 주민등록번호(공동연구원): {p['rrn_co']}
- 주소: {p['addr']}
- 기관 대표전화: {p['tel_org']}
- 연구비 입금 계좌: 하나은행 {p['account']} (예금주 {p['person_pi']})
- 법인 카드번호: {p['card']}

## 3. 연구개발의 필요성

본 연구는 {p['secret_kw']} 기술을 확보하기 위한 것으로, {p['org_a']}가 보유한
설비를 활용한다. {p['person_pi']} 책임연구원은 관련 분야에서 15년간 연구를
수행하였으며, {p['person_co']} 교수와 공동으로 예비실험을 진행한 바 있다.

외부 자문은 {p['person_ext']} 박사가 맡는다. 연구기간은 2026.03.01 ~ 2028.02.28이다.

## 4. 추진 체계

{p['dept']}가 총괄하고 {p['org_b']}가 분석을 담당한다. 실무 연락은
{p['email_pi']} 로 한다.
"""


if __name__ == "__main__":  # pragma: no cover
    print(research_plan_markdown())
