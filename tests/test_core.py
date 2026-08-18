"""핵심 불변식 시험.

여기서 깨지면 안전 보증이 무너지는 것들만 담는다.
실제 개인정보는 쓰지 않는다 — 전부 tests/synth.py 의 합성값이다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deident import formats, pipeline  # noqa: E402  (핸들러 등록 부수효과)
from deident.config import default_config  # noqa: E402
from deident.detect import l1_patterns, validators  # noqa: E402
from deident.entity.pseudonym import Mapping  # noqa: E402
from deident.extract import md as md_extract  # noqa: E402
from deident.model import (  # noqa: E402
    CAT_EMAIL, CAT_PERSON, CAT_PHONE, CAT_RRN, GRADE_CERTAIN, Piece, Segment,
)
from deident.report import mask  # noqa: E402
from deident.report.html_report import write_reports  # noqa: E402
from deident.restore import restore_text  # noqa: E402
from deident.verify.gate import run_gate  # noqa: E402
from synth import PLANTED, research_plan_markdown  # noqa: E402


@pytest.fixture()
def workspace(tmp_path: Path):
    config = default_config(tmp_path)
    config.workspace.prepare()
    source = tmp_path / "계획서.md"
    source.write_text(research_plan_markdown(), encoding="utf-8")
    return config, source


# ── 표시용 마스킹 ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("value,category", [
    (PLANTED["person_pi"], CAT_PERSON),
    (PLANTED["rrn_pi"], CAT_RRN),
    (PLANTED["phone_pi"], CAT_PHONE),
    (PLANTED["email_pi"], CAT_EMAIL),
])
def test_mask_never_returns_original(value, category):
    masked = mask.mask_value(value, category)
    assert masked != value
    assert value not in masked


def test_mask_person_keeps_only_edges():
    assert mask.mask_value("홍길동", CAT_PERSON) == "홍*동"
    assert mask.mask_value("남궁민수", CAT_PERSON) == "남**수"
    assert mask.mask_value("김구", CAT_PERSON) == "김*"


def test_mask_rrn_hides_back_digits():
    masked = mask.mask_value("900101-1234567", CAT_RRN)
    assert "1234567" not in masked
    assert masked.startswith("90")


# ── 검증기 ──────────────────────────────────────────────────────────────────
def test_rrn_checksum_and_shape():
    assert validators.rrn_checksum_ok(PLANTED["rrn_pi"])
    assert validators.rrn_shape_ok(PLANTED["rrn_pi"])
    # 2020.10 이후 발급분은 검증식을 통과하지 않는다 — 그래도 주민번호로 인정해야 한다
    broken = PLANTED["rrn_pi"][:-1] + str((int(PLANTED["rrn_pi"][-1]) + 1) % 10)
    assert not validators.rrn_checksum_ok(broken)
    assert validators.rrn_shape_ok(broken)


def test_luhn_and_bizno():
    assert validators.luhn_ok(PLANTED["card"])
    assert not validators.luhn_ok("1234-5678-9012-3456")
    assert validators.bizno_checksum_ok("123-45-67890") in (True, False)  # 형태만 확인


def test_rrn_detected_even_when_checksum_fails():
    broken = "900101-1000009"
    seg = Segment(seg_id="s", text=f"주민등록번호: {broken}",
                  pieces=[Piece(node=None, slot="line", start=0, length=30)])
    found = l1_patterns.detect_rrn(seg)
    assert [f.category for f in found] == [CAT_RRN]


# ── 편집 불가 경계 ──────────────────────────────────────────────────────────
def test_match_does_not_cross_uneditable_boundary():
    """탭·줄바꿈 의사문자를 넘는 매치는 채택되지 않는다."""
    text = "010-1234\t-5678"
    seg = Segment(
        seg_id="s", text=text,
        pieces=[
            Piece(node=None, slot="text", start=0, length=8),
            Piece(node=None, slot="pseudo", start=8, length=1, editable=False),
            Piece(node=None, slot="text", start=9, length=5),
        ],
    )
    assert not seg.slice_editable(0, 14)
    assert all(seg.slice_editable(f.start, f.end) for f in l1_patterns.detect_segment(seg))


# ── 종단 동작 ───────────────────────────────────────────────────────────────
def test_apply_removes_planted_values_and_passes_gate(workspace):
    config, source = workspace
    mapping = Mapping()
    result = pipeline.apply_file(source, config, mapping)

    assert result.gate is not None and result.gate.passed
    assert result.outputs, "게이트를 통과했으면 산출물이 있어야 한다"

    text = result.outputs[0].read_text(encoding="utf-8")
    for key in ("rrn_pi", "rrn_co", "phone_pi", "email_pi", "card",
                "person_pi", "person_co", "org_a", "org_b"):
        assert PLANTED[key] not in text, f"{key} 가 산출물에 남았다"


def test_titles_survive_name_replacement(workspace):
    """'박슬기 책임연구원' 에서 이름만 바뀌고 직함은 남아야 한다."""
    config, source = workspace
    result = pipeline.apply_file(source, config, Mapping())
    text = result.outputs[0].read_text(encoding="utf-8")
    assert "책임연구원" in text
    assert "교수" in text


def test_table_headers_are_preserved(workspace):
    config, source = workspace
    result = pipeline.apply_file(source, config, Mapping())
    text = result.outputs[0].read_text(encoding="utf-8")
    for header in ("성명", "소속", "생년월일", "연락처", "이메일"):
        assert header in text, f"표 머리글 '{header}' 가 사라졌다"


def test_apply_is_idempotent(workspace):
    config, source = workspace
    mapping = Mapping()
    first = pipeline.apply_file(source, config, mapping)
    output = first.outputs[0]
    second_source = config.workspace.root / "재실행.md"
    second_source.write_text(output.read_text(encoding="utf-8"), encoding="utf-8")

    second = pipeline.apply_file(second_source, config, mapping)
    assert second.findings == [] or all(f.category == "" for f in second.findings)
    assert second.outputs[0].read_text(encoding="utf-8") == output.read_text(encoding="utf-8")


def test_restore_round_trip(workspace):
    config, source = workspace
    mapping = Mapping()
    result = pipeline.apply_file(source, config, mapping)
    restored = restore_text(result.outputs[0].read_text(encoding="utf-8"), mapping)
    assert restored == source.read_text(encoding="utf-8")


def test_same_value_gets_same_pseudonym(workspace):
    """같은 이메일이 두 곳에 나오면 같은 가명을 받아야 한다."""
    config, source = workspace
    mapping = Mapping()
    pipeline.apply_file(source, config, mapping)
    pseudonyms = {e.original: e.pseudonym for e in mapping.entries.values()}
    assert len(set(pseudonyms.values())) == len(pseudonyms), "가명이 겹쳤다"


# ── 게이트 ──────────────────────────────────────────────────────────────────
def test_gate_fails_and_quarantines_on_leftover(tmp_path: Path):
    config = default_config(tmp_path)
    config.workspace.prepare()
    mapping = Mapping()
    mapping.pseudonym_for(PLANTED["person_pi"], CAT_PERSON)

    leaked = config.workspace.out / "새는산출물.md"
    leaked.write_text(f"연구책임자는 {PLANTED['person_pi']} 입니다.", encoding="utf-8")

    gate = run_gate(leaked, mapping, config)
    assert not gate.passed
    assert not leaked.exists(), "불합격 산출물이 out/ 에 남아 있으면 안 된다"
    assert gate.quarantined and gate.quarantined.exists()
    assert PLANTED["person_pi"] not in "\n".join(gate.summary_lines())


def test_gate_catches_reformatted_number(tmp_path: Path):
    """구분자를 바꿔 넣은 번호도 숫자 흐름으로 잡아낸다."""
    config = default_config(tmp_path)
    config.workspace.prepare()
    mapping = Mapping()
    mapping.pseudonym_for(PLANTED["rrn_pi"], CAT_RRN)

    digits = PLANTED["rrn_pi"].replace("-", "")
    leaked = config.workspace.out / "재포맷.md"
    leaked.write_text(f"주민번호 {digits[:6]}.{digits[6:]}", encoding="utf-8")
    assert not run_gate(leaked, mapping, config).passed


def test_pipeline_stages_all_outputs_on_partial_failure(workspace, monkeypatch):
    """산출물 하나라도 불합격이면 전부 격리한다 — 부분 통과는 없다."""
    config, source = workspace
    from deident.verify import gate as gate_module

    real_rescan = gate_module.rescan
    calls = {"n": 0}

    def flaky(path, cfg, segments=None):
        calls["n"] += 1
        found = real_rescan(path, cfg, segments)
        if calls["n"] == 1:
            from deident.model import Finding
            return found + [Finding(seg_id="x", start=0, end=1, text="?",
                                    category=CAT_RRN, grade=GRADE_CERTAIN,
                                    part="테스트")]
        return found

    monkeypatch.setattr(gate_module, "rescan", flaky)
    result = pipeline.apply_file(source, config, Mapping())
    assert not result.ok
    assert result.outputs == []
    assert not list(config.workspace.out.glob("*.비식별.*"))


# ── 리포트 ──────────────────────────────────────────────────────────────────
def test_public_report_has_no_original_values(workspace):
    config, source = workspace
    mapping = Mapping()
    result = pipeline.apply_file(source, config, mapping)
    public, private = write_reports(result, config, mapping)

    public_text = public.read_text(encoding="utf-8")
    for key in ("person_pi", "rrn_pi", "email_pi", "phone_pi", "card", "account"):
        assert PLANTED[key] not in public_text, f"공개 리포트에 {key} 원값이 있다"

    private_text = private.read_text(encoding="utf-8")
    assert PLANTED["person_pi"] in private_text, "사람 검토용 리포트는 원문을 보여줘야 한다"


def test_summary_carries_no_original_values(workspace):
    from deident.report.summary import build_summary, render_text

    config, source = workspace
    mapping = Mapping()
    result = pipeline.apply_file(source, config, mapping)
    text = render_text(build_summary(source, result.findings, result.outputs,
                                     result.gate, "apply"))
    for key in ("person_pi", "rrn_pi", "email_pi"):
        assert PLANTED[key] not in text


# ── 과탐 회귀 ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("line", [
    "이를 중심으로 3개 과제를 추진한다.",
    "단계적으로 2027년까지 확대한다.",
    "별도로 5개 지표를 설정하였다.",
    "새로 100억 규모 사업을 기획한다.",
    "우선적으로 2개 분야에 집중한다.",
])
def test_common_korean_adverbs_are_not_addresses(line):
    """'~으로 N' 은 도로명이 아니다. 행정구역이나 주소 라벨이 있어야 주소로 본다."""
    seg = md_extract.extract_text(line)[0]
    assert l1_patterns.detect_address(seg) == []


@pytest.mark.parametrize("line", [
    "주소: 대전광역시 유성구 가정로 141 본관 302호",
    "소재지는 서울특별시 관악구 관악로 1이다.",
    "세종특별자치시 도움5로 20",
])
def test_real_addresses_still_detected(line):
    seg = md_extract.extract_text(line)[0]
    assert l1_patterns.detect_address(seg), f"주소를 놓쳤다: {line}"


def test_role_words_are_not_organizations():
    """'참여연구원'은 역할어지 기관이 아니고, '연락처'는 정부 부처가 아니다."""
    from deident.detect import l3_ner
    from deident.entity.registry import Registry

    config = default_config(Path("/tmp"))
    segments = md_extract.extract_text(
        "참여연구원 3명이 담당한다. 연락처는 아래와 같다. 과학기술정보통신부 소관이다.")
    found = {f.text for f in l3_ner.detect_orgs(segments, config, Registry())}
    assert "참여연구원" not in found
    assert "연락처" not in found
    assert "과학기술정보통신부" in found


# ── 경보 (guard) ────────────────────────────────────────────────────────────
def test_guard_flags_sensitive_file(workspace):
    from deident import guard

    config, source = workspace
    results = guard.check_paths([source], config)
    assert results[0].level == guard.LEVEL_STOP
    assert guard.needs_attention(results)


def test_guard_never_calls_unreadable_file_clean(tmp_path: Path):
    """검사하지 못한 파일을 '없음'으로 보고하면 안 된다 — 가장 위험한 거짓말이다."""
    from deident import guard

    config = default_config(tmp_path)
    unsupported = tmp_path / "설계도.dwg"
    unsupported.write_bytes(b"binary")

    results = guard.check_paths([unsupported], config)
    assert not results[0].checked
    assert guard.needs_attention(results), "검사 불가를 안전으로 판정했다"
    text = guard.render_text(results)
    assert "확인 필요" in text
    assert "그대로 열어도 됩니다" not in text


def test_guard_output_has_no_original_values(workspace):
    from deident import guard

    config, source = workspace
    text = guard.render_text(guard.check_paths([source], config))
    for key in ("person_pi", "rrn_pi", "email_pi", "phone_pi"):
        assert PLANTED[key] not in text


# ── 추출 ────────────────────────────────────────────────────────────────────
def test_markdown_table_cells_carry_header_context():
    segments = md_extract.extract_text(
        "| 성명 | 생년월일 |\n|---|---|\n| 홍길동 | 1979년 3월 14일 |")
    cells = [s for s in segments if s.table_ctx and not s.table_ctx.is_header]
    assert any(c.table_ctx.header_text.strip() == "생년월일" for c in cells)


def test_markdown_reassembly_preserves_line_shape():
    from deident.apply.md_apply import reassemble_lines
    from deident.model import Replacement

    source = "| 성명 | 연락처 |\n|---|---|\n| 홍길동 | 010-1111-2222 |"
    segments = md_extract.extract_text(source)
    target = next(s for s in segments if s.text.strip() == "홍길동")
    plans = {target.seg_id: [Replacement(target.seg_id, target.text.index("홍"),
                                         target.text.index("홍") + 3, "홍길동",
                                         "[연구자A]", CAT_PERSON)]}
    out = reassemble_lines(source, segments, plans)
    assert "| [연구자A] | 010-1111-2222 |" in out
    assert out.count("\n") == source.count("\n")


def test_strict_only_does_not_fail_its_own_policy(workspace):
    """'추정은 남긴다'를 고른 결과가 게이트 불합격이 되면 그 옵션은 덫이다."""
    config, source = workspace
    result = pipeline.apply_file(source, config, Mapping(), strict_only=True)

    assert result.gate is not None and result.gate.passed
    assert result.outputs, "정책대로 실행했는데 산출물이 없다"
    codes = {i.code for i in result.gate.issues}
    assert "strict-only-kept" in codes, "남겨 둔 항목을 알리지 않았다"


def test_default_policy_still_fails_on_leftovers(workspace):
    """기본(추정도 치환)에서는 남은 탐지가 곧 불합격이어야 한다."""
    config, source = workspace
    from deident.verify.gate import run_gate

    mapping = Mapping()
    leaked = config.workspace.out / "새는산출물.md"
    leaked.parent.mkdir(parents=True, exist_ok=True)
    leaked.write_text(f"연락처 {PLANTED['phone_pi']}", encoding="utf-8")
    assert not run_gate(leaked, mapping, config).passed
