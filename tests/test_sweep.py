"""마무리 훑기 시험.

실문서(발표자료·한글 문서)에서 실제로 겪은 누출 경로들을 고정한다.
셋 다 '탐지는 됐는데 다른 자리에 그대로 남는' 같은 종류의 사고였다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deident import formats, pipeline  # noqa: E402,F401
from deident.apply.planner import plan_replacements  # noqa: E402
from deident.apply.sweep import sweep, sweep_attributes  # noqa: E402
from deident.config import default_config  # noqa: E402
from deident.entity.pseudonym import Mapping  # noqa: E402
from deident.extract import md as md_extract  # noqa: E402
from deident.model import CAT_PERSON  # noqa: E402


def _plan(text: str, tmp_path: Path, quick: bool = False):
    config = default_config(tmp_path)
    segments = md_extract.extract_text(text)
    mapping = Mapping()
    findings = pipeline.detect(segments, config, mapping, quick=quick)
    plans = plan_replacements(findings, {s.seg_id: s for s in segments},
                              mapping, config)
    return config, segments, mapping, plans


def test_sweep_removes_value_missed_by_detectors(tmp_path: Path):
    """탐지기가 놓친 자리라도, 이미 민감하다고 판정된 값이면 지운다.

    형태소 계층을 끈 상태로 재현한다 — 실제로는 문맥이 없는 슬라이드 제목이나
    표 밖 한 줄에서 이 일이 일어난다.
    """
    text = (
        "| 성명 | 소속 |\n|---|---|\n| 남궁민수 | 한국화학연구원 |\n\n"
        "발표자남궁민수담당\n"      # 앞뒤에 글자가 붙어 낱말 경계가 없는 자리
    )
    config, segments, mapping, plans = _plan(text, tmp_path, quick=True)
    bare = next(s for s in segments if "발표자남궁민수" in s.text)
    assert not [p for p in plans if p.seg_id == bare.seg_id], "전제 확인: 탐지가 놓쳐야 한다"

    extra = sweep(segments, mapping, config, plans)
    assert any(p.seg_id == bare.seg_id for p in extra), "문맥 없는 이름이 남았다"


def test_sweep_catches_letter_spaced_name(tmp_path: Path):
    """'남 궁 민 수' 처럼 자간을 벌려 쓴 표기도 같은 이름이다."""
    text = (
        "| 성명 | 소속 |\n|---|---|\n| 남궁민수 | 한국화학연구원 |\n\n"
        "발 표 자 : 남 궁 민 수\n"
    )
    config, segments, mapping, plans = _plan(text, tmp_path)
    extra = sweep(segments, mapping, config, plans)

    spaced = next(s for s in segments if "남 궁" in s.text)
    assert any(p.seg_id == spaced.seg_id for p in extra), "띄어 쓴 이름이 남았다"


def test_sweep_does_not_eat_longer_name(tmp_path: Path):
    """짧은 이름이 더 긴 이름 안에서 잘리면 안 된다."""
    text = "| 성명 |\n|---|\n| 김민 |\n| 김민수 |\n\n김민수 연구원이 담당한다.\n"
    config, segments, mapping, plans = _plan(text, tmp_path)
    extra = sweep(segments, mapping, config, plans)

    body = next(s for s in segments if "담당한다" in s.text)
    body_plans = [p for p in (plans + extra) if p.seg_id == body.seg_id]
    assert body_plans, "본문의 이름을 놓쳤다"
    # 긴 이름이 통째로 치환돼야 한다 ('김민' + '수' 로 쪼개지면 안 된다)
    assert any(p.original == "김민수" for p in body_plans)


def test_sweep_skips_already_pseudonymized(tmp_path: Path):
    """이미 가명이 들어간 자리를 다시 건드리지 않는다 (재실행 멱등)."""
    text = "| 성명 |\n|---|\n| 남궁민수 |\n\n[연구자A] 가 발표한다.\n"
    config, segments, mapping, plans = _plan(text, tmp_path)
    extra = sweep(segments, mapping, config, plans)

    target = next(s for s in segments if "[연구자A]" in s.text)
    assert not [p for p in extra if p.seg_id == target.seg_id]


def test_sweep_attributes_cleans_shape_names():
    """도형 이름·대체 텍스트에 복사된 본문은 화면에 안 보여 눈으로 못 찾는다."""
    from lxml import etree

    mapping = Mapping()
    pseudonym = mapping.pseudonym_for("남궁민수", CAT_PERSON)
    root = etree.fromstring(
        '<root><shape name="TextBox 남궁민수" descr="남궁민수 사진"/></root>')

    assert sweep_attributes(root, mapping) == 2   # name·descr 두 곳
    shape = root[0]
    assert "남궁민수" not in shape.get("name")
    assert "남궁민수" not in shape.get("descr")
    assert pseudonym in shape.get("name")
