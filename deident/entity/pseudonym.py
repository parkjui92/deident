"""가명 발급과 매핑 저장소.

같은 원값은 언제 어느 문서에서 만나도 같은 가명을 받는다. 그래야 재실행이
멱등하고, 여러 문서를 함께 다뤄도 인물 관계가 보존된다. 매핑 파일은 원값을
담으므로 _private 밖으로 나가면 안 된다.
"""

from __future__ import annotations

import json
import os
import string
from dataclasses import dataclass, field
from pathlib import Path

from ..model import (
    CATEGORY_LABEL, CAT_DEPT, CAT_ORG, CAT_PERSON, digits_only, normalize_key,
)

# 사람이 읽기 좋은 이름표
PERSON_LABEL = "연구자"
ORG_LABEL = "기관"
DEPT_LABEL = "부서"

ALPHA_CATEGORIES = {CAT_PERSON: PERSON_LABEL, CAT_ORG: ORG_LABEL, CAT_DEPT: DEPT_LABEL}


def _alpha_tag(index: int) -> str:
    """0→A, 25→Z, 26→AA … 이니셜을 쓰지 않는다(정보 누설)."""
    letters = string.ascii_uppercase
    tag = ""
    n = index
    while True:
        tag = letters[n % 26] + tag
        n = n // 26 - 1
        if n < 0:
            break
    return tag


@dataclass
class MappingEntry:
    key: str
    category: str
    original: str
    pseudonym: str
    variants: list[str] = field(default_factory=list)
    attributes: dict[str, str] = field(default_factory=dict)
    count: int = 0


class Mapping:
    """원값↔가명 저장소. 키는 (카테고리, 정규화 원값)."""

    VERSION = 2

    def __init__(self, fmt: str = "[{label}{tag}]") -> None:
        self.format = fmt
        self.entries: dict[tuple[str, str], MappingEntry] = {}
        self._counters: dict[str, int] = {}

    # ── 발급 ────────────────────────────────────────────────────────────
    def _make_pseudonym(self, category: str) -> str:
        index = self._counters.get(category, 0)
        self._counters[category] = index + 1
        if category in ALPHA_CATEGORIES:
            label, tag = ALPHA_CATEGORIES[category], _alpha_tag(index)
        else:
            label, tag = CATEGORY_LABEL.get(category, category), str(index + 1)
        return self.format.format(label=label, tag=tag)

    def pseudonym_for(self, original: str, category: str,
                      variants: list[str] | None = None,
                      attributes: dict[str, str] | None = None) -> str:
        key = self.key_for(original, category)
        entry = self.entries.get((category, key))
        if entry is None:
            entry = MappingEntry(key=key, category=category,
                                 original=normalize_key(original),
                                 pseudonym=self._make_pseudonym(category))
            self.entries[(category, key)] = entry
        if variants:
            for v in variants:
                nv = normalize_key(v)
                if nv and nv not in entry.variants:
                    entry.variants.append(nv)
        if attributes:
            entry.attributes.update({k: v for k, v in attributes.items() if v})
        entry.count += 1
        return entry.pseudonym

    @staticmethod
    def key_for(original: str, category: str) -> str:
        """번호류는 숫자만으로 키를 만든다 — 구분자를 바꾼 같은 값을 하나로 본다."""
        norm = normalize_key(original)
        digits = digits_only(norm)
        if digits and len(digits) >= 6 and not any(ch.isalpha() for ch in norm):
            return digits
        return norm.lower()

    # ── 조회 ────────────────────────────────────────────────────────────
    def originals(self) -> list[tuple[str, str]]:
        """(원값, 가명) 목록 — 검증 게이트의 전수 탐색 대상."""
        pairs = []
        for entry in self.entries.values():
            pairs.append((entry.original, entry.pseudonym))
            for v in entry.variants:
                pairs.append((v, entry.pseudonym))
        return pairs

    def reverse_pairs(self) -> list[tuple[str, str]]:
        """복원용 (가명, 원값) — 긴 가명부터 적용."""
        pairs = [(e.pseudonym, e.original) for e in self.entries.values()]
        pairs.sort(key=lambda kv: len(kv[0]), reverse=True)
        return pairs

    # ── 저장·적재 ───────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {
            "version": self.VERSION,
            "format": self.format,
            "counters": self._counters,
            "entries": [
                {
                    "key": e.key, "category": e.category, "original": e.original,
                    "pseudonym": e.pseudonym, "variants": e.variants,
                    "attributes": e.attributes, "count": e.count,
                }
                for e in self.entries.values()
            ],
        }

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
                        encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    @classmethod
    def load(cls, path: Path, fmt: str = "[{label}{tag}]") -> "Mapping":
        mapping = cls(fmt)
        if not path.exists():
            return mapping
        data = json.loads(path.read_text(encoding="utf-8"))
        mapping.format = data.get("format", fmt)
        mapping._counters = {k: int(v) for k, v in (data.get("counters") or {}).items()}
        for raw in data.get("entries", []):
            entry = MappingEntry(
                key=raw["key"], category=raw["category"], original=raw["original"],
                pseudonym=raw["pseudonym"], variants=list(raw.get("variants", [])),
                attributes=dict(raw.get("attributes", {})), count=int(raw.get("count", 0)),
            )
            mapping.entries[(entry.category, entry.key)] = entry
        return mapping
