"""명령줄 진입점.

stdout 으로는 마스킹된 요약만 내보낸다 — 이 채널은 사람과 LLM이 함께 읽는다.
원값이 필요한 검토는 _private/report_full.html 로만 한다.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import formats  # noqa: F401  (핸들러 등록 부수효과)
from . import guard, pipeline, selftest
from .config import Config, load_config
from .entity.pseudonym import Mapping
from .errors import DeidentError, install_excepthook
from .report import html_report
from .report.summary import build_summary, render_text, write_summary
from .restore import restore_file

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_GATE_FAILED = 2
EXIT_SENSITIVE = 3     # guard 전용 — 민감정보가 있다는 신호(오류가 아니다)


def _collect_inputs(patterns: list[str], config: Config) -> list[Path]:
    paths: list[Path] = []
    for pattern in patterns:
        p = Path(pattern).expanduser()
        if p.is_dir():
            paths.extend(sorted(q for q in p.iterdir() if q.is_file()))
        elif p.exists():
            paths.append(p)
        else:
            matched = sorted(Path().glob(pattern))
            if not matched:
                raise DeidentError(f"입력을 찾을 수 없습니다: {pattern}")
            paths.extend(matched)
    if not paths:
        default_dir = config.workspace.root / "in"
        if default_dir.is_dir():
            paths = sorted(q for q in default_dir.iterdir() if q.is_file())
    if not paths:
        raise DeidentError("처리할 파일이 없습니다 (in/ 이 비어 있습니다)")
    return [p for p in paths if not p.name.startswith(".")]


def _load_mapping(config: Config) -> Mapping:
    return Mapping.load(config.workspace.mapping_path,
                        fmt=str(config.settings.get("pseudonym_format", "[{label}{tag}]")))


def cmd_scan(args: argparse.Namespace, config: Config) -> int:
    mapping = _load_mapping(config)
    config.workspace.prepare()
    exit_code = EXIT_OK
    for path in _collect_inputs(args.inputs, config):
        result = pipeline.scan_file(path, config, mapping)
        data = build_summary(path, result.findings, [], None, "scan")
        print(render_text(data))
        write_summary(config.workspace.out / f"{path.stem}.summary.json", data)
        html_report.write_reports(result, config, mapping)
    return exit_code


def cmd_apply(args: argparse.Namespace, config: Config) -> int:
    mapping = _load_mapping(config)
    exit_code = EXIT_OK
    for path in _collect_inputs(args.inputs, config):
        result = pipeline.apply_file(
            path, config, mapping,
            strict_only=args.strict_only, mask_mode=args.mask,
            emit_markdown=not args.no_markdown,
        )
        data = build_summary(path, result.findings, result.outputs, result.gate, "apply")
        print(render_text(data))
        write_summary(config.workspace.out / f"{path.stem}.summary.json", data)
        html_report.write_reports(result, config, mapping)
        if not result.ok:
            exit_code = EXIT_GATE_FAILED
    mapping.save(config.workspace.mapping_path)
    return exit_code


def cmd_verify(args: argparse.Namespace, config: Config) -> int:
    from .verify.gate import run_gate

    mapping = _load_mapping(config)
    exit_code = EXIT_OK
    for path in _collect_inputs(args.inputs, config):
        gate = run_gate(path, mapping, config)
        data = build_summary(path, [], [path], gate, "verify")
        print(render_text(data))
        if not gate.passed:
            exit_code = EXIT_GATE_FAILED
    return exit_code


def cmd_guard(args: argparse.Namespace, config: Config) -> int:
    """열기 전 사전 점검. 파일을 바꾸지 않고 판정만 낸다."""
    results = guard.check_paths(_collect_inputs(args.inputs, config), config,
                               deep=not args.quick)
    print(guard.render_json(results) if args.json else guard.render_text(results))
    # 검사하지 못한 파일도 '주의'로 신호한다 — 못 읽은 것을 안전하다고 하지 않는다
    return EXIT_SENSITIVE if guard.needs_attention(results) else EXIT_OK


def cmd_restore(args: argparse.Namespace, config: Config) -> int:
    mapping = _load_mapping(config)
    for path in _collect_inputs(args.inputs, config):
        out_path = restore_file(path, mapping, config)
        print(f"■ 복원: {path.name} → {out_path}")
        print("  ! 복원본은 재식별된 원문입니다. _private 밖으로 옮기지 마십시오.")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m deident",
        description="연구계획서·기획서 민감정보 비식별화 (전 과정 로컬 실행)",
    )
    parser.add_argument("--root", default=".", help="작업 폴더 (기본: 현재 폴더)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_guard = sub.add_parser(
        "guard", help="열기 전 사전 점검 — 민감정보 유무·위험도만 빠르게 판정")
    p_guard.add_argument("inputs", nargs="*", default=[])
    p_guard.add_argument("--quick", action="store_true",
                         help="형태소 분석 생략 (빠르지만 본문 속 이름을 놓친다)")
    p_guard.add_argument("--json", action="store_true", help="기계 판독용 JSON")
    p_guard.set_defaults(func=cmd_guard)

    p_scan = sub.add_parser("scan", help="탐지만 수행 (파일 변경 없음)")
    p_scan.add_argument("inputs", nargs="*", default=[])
    p_scan.set_defaults(func=cmd_scan)

    p_apply = sub.add_parser("apply", help="탐지→치환→검증 게이트→리포트")
    p_apply.add_argument("inputs", nargs="*", default=[])
    p_apply.add_argument("--strict-only", action="store_true",
                         help="'확실' 등급만 치환 (기본은 '추정'도 치환)")
    p_apply.add_argument("--mask", action="store_true",
                         help="가명 대신 완전 마스킹 (복원 불가)")
    p_apply.add_argument("--no-markdown", action="store_true",
                         help="AI용 .md 산출 생략")
    p_apply.set_defaults(func=cmd_apply)

    p_verify = sub.add_parser("verify", help="산출물을 단독으로 재검증")
    p_verify.add_argument("inputs", nargs="*", default=[])
    p_verify.set_defaults(func=cmd_verify)

    p_restore = sub.add_parser("restore", help="매핑으로 복원 (_private 로만 출력)")
    p_restore.add_argument("inputs", nargs="*", default=[])
    p_restore.set_defaults(func=cmd_restore)

    p_self = sub.add_parser("selftest", help="합성 문서로 탐지 재현율 확인")
    p_self.set_defaults(func=lambda a, c: selftest.run())

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    root = Path(args.root).expanduser().resolve()
    config = load_config(root)
    install_excepthook(config.workspace.logs)
    try:
        return int(args.func(args, config))
    except DeidentError as exc:
        print(f"[중단] {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
