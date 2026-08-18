#!/usr/bin/env python3
"""deident 실행기 — 어디서 부르든 같은 도구를 쓰게 한다.

작업 폴더(원문·산출물·설정이 머무는 곳)를 정하는 순서:
  ① --root 로 준 경로
  ② 환경변수 DEIDENT_ROOT
  ③ 현재 폴더
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _package_root() -> Path:
    """deident 패키지가 있는 폴더. 설치 위치를 옮겨도 자기 옆을 먼저 본다."""
    candidates = [HERE, HERE.parent]
    for base in candidates:
        if (base / "deident" / "__main__.py").exists():
            return base
    sys.exit("deident 패키지를 찾지 못했습니다 (run.py 옆에 deident/ 가 있어야 합니다).")


def main() -> int:
    sys.path.insert(0, str(_package_root()))
    from deident.cli import main as cli_main

    argv = sys.argv[1:]
    if "--root" not in argv:
        root = os.environ.get("DEIDENT_ROOT")
        if root:
            argv = ["--root", root, *argv]
    return cli_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
