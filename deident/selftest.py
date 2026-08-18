"""자체검사 — 심어 둔 민감정보를 얼마나 잡아내는지 매번 같은 방식으로 잰다.

탐지기를 고치면 다른 쪽이 조용히 나빠질 수 있다. 그걸 알아채려면 기준선이
필요하다. 합성 문서에 심은 값 목록과 산출물을 대조해 구분별 재현율을 낸다.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

from .config import default_config
from .entity.pseudonym import Mapping
from .model import (
    CAT_ACCOUNT, CAT_ADDRESS, CAT_BIRTH, CAT_BIZNO, CAT_CARD, CAT_EMAIL,
    CAT_ORG, CAT_PERSON, CAT_PHONE, CAT_PROJECT_NO, CAT_RRN, CATEGORY_LABEL,
)

# 합성 문서에 심은 값 → 어느 구분으로 잡혀야 하는가
EXPECTED = {
    "person_pi": CAT_PERSON, "person_co": CAT_PERSON, "person_ext": CAT_PERSON,
    "rrn_pi": CAT_RRN, "rrn_co": CAT_RRN,
    "phone_pi": CAT_PHONE, "email_pi": CAT_EMAIL, "email_co": CAT_EMAIL,
    "addr": CAT_ADDRESS, "birth": CAT_BIRTH, "account": CAT_ACCOUNT,
    "card": CAT_CARD, "bizno": CAT_BIZNO, "org_a": CAT_ORG, "org_b": CAT_ORG,
    "project_no": CAT_PROJECT_NO,
}


def _load_synth():
    tests_dir = Path(__file__).resolve().parent.parent / "tests"
    sys.path.insert(0, str(tests_dir))
    try:
        from synth import PLANTED, research_plan_markdown  # type: ignore
    except ImportError:
        return None, None
    return PLANTED, research_plan_markdown


def run(verbose: bool = True) -> int:
    planted, make_doc = _load_synth()
    if planted is None:
        print("[자체검사] tests/synth.py 를 찾지 못해 건너뜁니다.")
        return 1

    from . import formats  # noqa: F401
    from . import pipeline

    workdir = Path(tempfile.mkdtemp(prefix="deident-selftest-"))
    try:
        config = default_config(workdir)
        config.workspace.prepare()
        source = workdir / "합성_연구계획서.md"
        source.write_text(make_doc(), encoding="utf-8")

        result = pipeline.apply_file(source, config, Mapping())
        if not result.outputs:
            print("[자체검사] 실패 — 게이트를 통과하지 못해 산출물이 없습니다.")
            if result.gate:
                for line in result.gate.summary_lines()[:10]:
                    print(f"   · {line}")
            return 2

        output = result.outputs[0].read_text(encoding="utf-8")
        missed: list[str] = []
        for key, category in EXPECTED.items():
            if planted[key] in output:
                missed.append(f"{CATEGORY_LABEL.get(category, category)}({key})")

        total = len(EXPECTED)
        caught = total - len(missed)
        status = "통과" if not missed else "실패"
        print(f"[자체검사] {status} — 심은 값 {total}개 중 {caught}개 제거"
              f" (재현율 {caught / total:.0%})")
        if missed:
            print("  놓친 항목: " + ", ".join(missed))
        if verbose:
            print(f"  게이트: {'통과' if result.gate and result.gate.passed else '불합격'}")
            print(f"  탐지 {len(result.findings)}건 · 가명 {len(result.outputs)}개 산출")
        return 0 if not missed else 2
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
