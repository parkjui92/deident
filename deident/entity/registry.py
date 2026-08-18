"""엔티티 대장 — 수확한 인물·기관과 그 표기 변형을 관리한다.

이름 탐지의 신뢰도는 여기서 갈린다. 표에서 확실하게 건진 이름을 대장에
올리고, 그 이름의 변형들을 만들어 문서 전체에 전파하는 것이 형태소 분석기
단독 탐지보다 훨씬 정확하다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..kdata import AMBIGUOUS_SURNAMES, COMPOUND_SURNAMES, JOSA, TITLE_ALTERNATION, TITLES
from ..model import CAT_ORG, CAT_PERSON, normalize_key

# 이름 뒤에 바로 붙을 수 있는 것들 — 여기서 끝나야 온전한 이름 매치로 본다
_TAIL = "|".join(sorted(set(JOSA) | set(TITLES), key=len, reverse=True))
RE_NAME_TAIL = re.compile(rf"^(?:{_TAIL})")


@dataclass
class EntityRecord:
    entity_id: str
    category: str
    canonical: str
    variants: set[str] = field(default_factory=set)
    attributes: dict[str, str] = field(default_factory=dict)
    source: str = "L2"

    @property
    def surname(self) -> str:
        for compound in COMPOUND_SURNAMES:
            if self.canonical.startswith(compound):
                return compound
        return self.canonical[:1]


class Registry:
    def __init__(self) -> None:
        self.records: dict[str, EntityRecord] = {}
        self._by_value: dict[tuple[str, str], str] = {}
        self._seq = 0

    def add(self, value: str, category: str, attributes: dict[str, str] | None = None,
            source: str = "L2") -> EntityRecord | None:
        value = normalize_key(value)
        if not value:
            return None
        key = (category, value)
        if key in self._by_value:
            record = self.records[self._by_value[key]]
            if attributes:
                record.attributes.update({k: v for k, v in attributes.items() if v})
            return record

        self._seq += 1
        record = EntityRecord(entity_id=f"E{self._seq:03d}", category=category,
                              canonical=value, attributes=dict(attributes or {}),
                              source=source)
        record.variants = generate_variants(value, category)
        self.records[record.entity_id] = record
        for variant in record.variants | {value}:
            self._by_value.setdefault((category, variant), record.entity_id)
        return record

    def by_category(self, category: str) -> list[EntityRecord]:
        return [r for r in self.records.values() if r.category == category]

    def surname_index(self) -> dict[str, list[EntityRecord]]:
        """성씨 → 인물 목록. '김 박사' 같은 성+직함 해소에 쓴다."""
        index: dict[str, list[EntityRecord]] = {}
        for record in self.by_category(CAT_PERSON):
            index.setdefault(record.surname, []).append(record)
        return index

    def all_variants(self) -> list[tuple[str, EntityRecord]]:
        """(표기, 엔티티) 목록 — 긴 표기부터. 최장일치 전파를 위해."""
        pairs: list[tuple[str, EntityRecord]] = []
        for record in self.records.values():
            for variant in record.variants:
                pairs.append((variant, record))
        pairs.sort(key=lambda kv: len(kv[0]), reverse=True)
        return pairs


def generate_variants(value: str, category: str) -> set[str]:
    """한 대상의 표기 변형들.

    본문은 표와 똑같이 쓰지 않는다. '박슬기'가 '박슬기 교수', '박 교수',
    '박슬기박사님'으로 나타난다. 전파는 이 변형들을 다 훑어야 한다.
    """
    variants = {value}
    if category != CAT_PERSON:
        if category == CAT_ORG:
            # 괄호 표기·약칭 앞부분 (한국화학연구원(KRICT) → 한국화학연구원)
            head = re.split(r"[（(]", value)[0].strip()
            if len(head) >= 2:
                variants.add(head)
        return {v for v in variants if len(v) >= 2}

    # 직함이 붙은 표기는 변형으로 만들지 않는다. 맨이름 매치가 이미 그 자리를
    # 찾아내고(뒤따르는 직함은 match_is_whole 이 경계로 인정한다), 변형으로
    # 등록하면 '박슬기 책임연구원' 전체가 통째로 치환돼 문장이 무너진다.
    # 마스킹 표기 (홍*동, 홍○동)
    if len(value) >= 3:
        for filler in ("*", "○", "O", "●"):
            variants.add(value[0] + filler * (len(value) - 2) + value[-1])
    return {v for v in variants if len(v) >= 2}


def match_is_whole(text: str, start: int, end: int, category: str) -> bool:
    """매치가 더 긴 낱말의 일부가 아닌지 본다.

    '유성'이 '유성구' 안에서 잡히는 것을 막는 장치. 뒤에 한글이 이어지면
    조사·직함일 때만 인정한다.
    """
    before = text[start - 1] if start > 0 else ""
    if _is_hangul(before):
        return False
    after = text[end:]
    if after and _is_hangul(after[0]):
        if category == CAT_ORG:
            return bool(RE_NAME_TAIL.match(after))
        return bool(RE_NAME_TAIL.match(after))
    return True


def _is_hangul(ch: str) -> bool:
    return bool(ch) and "가" <= ch <= "힣"


def resolve_surname_title(surname: str, registry: Registry) -> EntityRecord | None:
    """'김 박사'의 '김'을 대장의 인물로 연결한다.

    같은 성이 둘 이상이면 누구인지 특정할 수 없으므로 연결하지 않는다
    (치환은 하되 등급을 낮춰 사람이 확인하게 한다).
    """
    candidates = registry.surname_index().get(surname, [])
    if len(candidates) == 1:
        return candidates[0]
    return None


def surname_is_ambiguous(surname: str) -> bool:
    return surname in AMBIGUOUS_SURNAMES
