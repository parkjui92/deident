"""예외 규약과 stdout 위생.

이 시스템에서 stdout 은 사람과 LLM이 함께 읽는 유일한 채널이다.
예외 메시지에 원문 조각이 섞이면 그 자체가 유출이므로, 예외는 원문을
담지 않는다는 규약을 지키고 트레이스백은 파일로만 흘린다.
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path


class DeidentError(Exception):
    """모든 내부 예외의 뿌리. 메시지에 문서 원문을 넣지 않는다."""


class UnsupportedFormat(DeidentError):
    pass


class DocumentRejected(DeidentError):
    """안전을 보장할 수 없어 처리를 거부한 경우 (변경추적·이미지 PDF 등)."""


class GateFailed(DeidentError):
    """검증 게이트 불합격 — 산출물은 격리되고 out/ 에는 아무것도 남지 않는다."""


class ConfigError(DeidentError):
    pass


def install_excepthook(log_dir: Path) -> None:
    """예상 못 한 예외의 전체 트레이스백을 로그 파일로만 남긴다."""

    def _hook(exc_type, exc, tb) -> None:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "traceback.log"
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write("".join(traceback.format_exception(exc_type, exc, tb)))
            fh.write("\n")
        try:
            log_path.chmod(0o600)
        except OSError:
            pass
        frame = tb
        while frame and frame.tb_next:
            frame = frame.tb_next
        where = ""
        if frame:
            where = f"{Path(frame.tb_frame.f_code.co_filename).name}:{frame.tb_lineno}"
        print(
            f"[오류] {exc_type.__name__} at {where} — 상세는 {log_path} (원문 포함 가능, 격리 보관)",
            file=sys.stderr,
        )
        sys.exit(1)

    sys.excepthook = _hook
