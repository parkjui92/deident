"""설정·사용자 사전 로드와 작업 디렉토리 규약."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .errors import ConfigError
from .model import ALL_CATEGORIES, CAT_TITLE, normalize_key

# 직위는 기본 off — 본문의 '교수·연구원'까지 치우면 문서가 읽히지 않는다.
# 표의 직위 칸은 인물 엔티티 속성으로 따로 다룬다.
_DEFAULT_CATEGORIES = {cat: cat != CAT_TITLE for cat in sorted(ALL_CATEGORIES)}

DEFAULT_SETTINGS: dict = {
    # 끌 카테고리는 settings.yaml 의 categories 에서 false 로.
    "categories": _DEFAULT_CATEGORIES,
    # 추정 등급도 기본 치환 (fail-closed). --strict-only 로 뒤집는다.
    "replace_likely": True,
    "mask_char": "●",
    # 치환 서식 — {label} 은 카테고리 한글명, {tag} 는 일련 태그
    "pseudonym_format": "[{label}{tag}]",
    "strip_images": False,
    "strip_embedded": False,
}


@dataclass
class Workspace:
    """입출력 디렉토리. _private 는 원값이 머무는 유일한 구역이다."""

    root: Path

    @property
    def out(self) -> Path:
        return self.root / "out"

    @property
    def private(self) -> Path:
        return self.root / "_private"

    @property
    def mapping_path(self) -> Path:
        return self.private / "mapping.json"

    @property
    def logs(self) -> Path:
        return self.private / "logs"

    @property
    def failed(self) -> Path:
        return self.private / "failed"

    @property
    def restored(self) -> Path:
        return self.private / "restored"

    @property
    def config_dir(self) -> Path:
        return self.root / "config"

    def prepare(self) -> None:
        self.out.mkdir(parents=True, exist_ok=True)
        for d in (self.private, self.logs, self.failed, self.restored):
            d.mkdir(parents=True, exist_ok=True)
        # 격리 구역은 권한과 .gitignore 로 이중 방어
        try:
            os.chmod(self.private, 0o700)
        except OSError:
            pass
        gitignore = self.private / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text("*\n", encoding="utf-8")


@dataclass
class Rule:
    """confidential_rules.yaml 의 사용자 정의 탐지 규칙."""

    name: str
    category: str
    pattern: re.Pattern | None = None
    literals: list[str] = field(default_factory=list)
    context: re.Pattern | None = None   # 같은 세그먼트에 이 말이 있어야 인정
    grade: str = "확실"


@dataclass
class Config:
    settings: dict
    allowlist: set[str]
    denylist: list[tuple[str, str]]      # (원값, 카테고리)
    rules: list[Rule]
    workspace: Workspace

    def category_enabled(self, category: str) -> bool:
        return bool(self.settings.get("categories", {}).get(category, True))

    @property
    def replace_likely(self) -> bool:
        return bool(self.settings.get("replace_likely", True))


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path.name} 파싱 실패: {exc.__class__.__name__}") from None
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name} 최상위는 매핑이어야 합니다")
    return data


def _load_list_file(path: Path) -> list[str]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


def load_config(root: Path) -> Config:
    ws = Workspace(root)
    cfg_dir = ws.config_dir

    settings = {**DEFAULT_SETTINGS, "categories": {**_DEFAULT_CATEGORIES}}
    user = _load_yaml(cfg_dir / "settings.yaml")
    for key, value in user.items():
        if key == "categories" and isinstance(value, dict):
            merged = {**settings["categories"]}
            for cat, on in value.items():
                if cat not in ALL_CATEGORIES:
                    raise ConfigError(f"settings.yaml: 알 수 없는 카테고리 '{cat}'")
                merged[cat] = bool(on)
            settings["categories"] = merged
        else:
            settings[key] = value

    allowlist = {normalize_key(v) for v in _load_list_file(cfg_dir / "allowlist.txt")}

    denylist: list[tuple[str, str]] = []
    for line in _load_list_file(cfg_dir / "denylist.txt"):
        parts = re.split(r"[\t,]", line, maxsplit=1)
        value = normalize_key(parts[0])
        category = parts[1].strip() if len(parts) > 1 else "confidential"
        if category not in ALL_CATEGORIES:
            raise ConfigError(f"denylist.txt: 알 수 없는 카테고리 '{category}'")
        if value:
            denylist.append((value, category))
    # 긴 값을 먼저 적용해야 부분 매치가 긴 값을 갉아먹지 않는다
    denylist.sort(key=lambda kv: len(kv[0]), reverse=True)

    rules: list[Rule] = []
    raw_rules = _load_yaml(cfg_dir / "confidential_rules.yaml").get("rules", []) or []
    for entry in raw_rules:
        if not isinstance(entry, dict):
            raise ConfigError("confidential_rules.yaml: rules 항목은 매핑이어야 합니다")
        name = str(entry.get("name", "unnamed"))
        category = str(entry.get("category", "confidential"))
        if category not in ALL_CATEGORIES:
            raise ConfigError(f"confidential_rules.yaml[{name}]: 알 수 없는 카테고리 '{category}'")
        pattern = None
        if entry.get("pattern"):
            try:
                pattern = re.compile(str(entry["pattern"]))
            except re.error as exc:
                raise ConfigError(f"confidential_rules.yaml[{name}]: 정규식 오류 — {exc}") from None
        context = None
        if entry.get("context"):
            try:
                context = re.compile(str(entry["context"]))
            except re.error as exc:
                raise ConfigError(f"confidential_rules.yaml[{name}]: context 정규식 오류 — {exc}") from None
        literals = [normalize_key(str(v)) for v in (entry.get("literals") or [])]
        rules.append(Rule(name=name, category=category, pattern=pattern,
                          literals=[v for v in literals if v], context=context,
                          grade=str(entry.get("grade", "확실"))))

    return Config(settings=settings, allowlist=allowlist, denylist=denylist,
                  rules=rules, workspace=ws)


def default_config(root: Path) -> Config:
    """설정 파일이 없어도 동작하는 기본값 (테스트·최초 실행용)."""
    settings = {**DEFAULT_SETTINGS, "categories": {**_DEFAULT_CATEGORIES}}
    return Config(settings=settings, allowlist=set(), denylist=[],
                  rules=[], workspace=Workspace(root))
