"""가명 → 원값 복원.

복원본은 곧 재식별본이므로 _private/restored/ 안에서만 만들어진다.
텍스트 산출물(.md)만 대상으로 한다 — 서식 문서 복원은 필요가 없고,
있다면 원본을 쓰면 된다.
"""

from __future__ import annotations

from pathlib import Path

from .config import Config
from .entity.pseudonym import Mapping
from .errors import DeidentError


def restore_text(text: str, mapping: Mapping) -> str:
    out = text
    for pseudonym, original in mapping.reverse_pairs():
        if pseudonym and original:
            out = out.replace(pseudonym, original)
    return out


def restore_file(path: Path, mapping: Mapping, config: Config) -> Path:
    if path.suffix.lower() not in (".md", ".txt", ".markdown"):
        raise DeidentError("복원은 텍스트 산출물(.md/.txt)만 지원합니다. 서식 문서는 원본을 쓰십시오.")
    target_dir = config.workspace.restored
    target_dir.mkdir(parents=True, exist_ok=True)
    out_path = target_dir / path.name.replace(".비식별", ".복원")
    out_path.write_text(restore_text(path.read_text(encoding="utf-8"), mapping),
                        encoding="utf-8")
    try:
        out_path.chmod(0o600)
    except OSError:
        pass
    return out_path
