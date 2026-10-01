"""로컬 LLM 후보 층(L4) 시험.

모델은 부르지 않는다 — 모델이 무엇을 내놓든 지켜져야 하는 약속만 시험한다.
  · 원문에 없는 문자열은 버린다
  · 이미 잡힌 자리는 건드리지 않는다
  · 기관 후보는 치환하지 않는다 (명시했을 때만)
  · 이 컴퓨터가 아닌 주소로는 보내지 않는다
  · 게이트는 모델을 부르지 않는다
이름·기관은 전부 허구다.
"""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deident import formats, pipeline  # noqa: E402,F401
from deident.config import default_config  # noqa: E402
from deident.detect import l4_llm  # noqa: E402
from deident.entity.pseudonym import Mapping  # noqa: E402
from deident.errors import ConfigError, DeidentError  # noqa: E402
from deident.model import CAT_ORG, CAT_PERSON, GRADE_LIKELY, GRADE_REVIEW  # noqa: E402

DOC = ("수요기업 확약서는 해든조선해양 1곳뿐이다. 실무는 보리스가 맡는다.\n"
       "문의는 010-2345-6789 로 한다. 교수연구실 배정은 다음 달이다.\n")


@pytest.fixture()
def workspace(tmp_path: Path):
    config = default_config(tmp_path)
    config.workspace.prepare()
    source = tmp_path / "메모.md"
    source.write_text(DOC, encoding="utf-8")
    return config, source


def _fake_model(monkeypatch, items: list[dict]) -> list[str]:
    """모델 응답을 고정한다. 모델에 보낸 글을 기록해 돌려준다."""
    sent: list[str] = []

    def ask(settings, text):
        sent.append(text)
        return items

    monkeypatch.setattr(l4_llm, "_ask", ask)
    return sent


# ── 주소 제한 ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("endpoint", [
    "http://127.0.0.1:11434", "http://localhost:11434", "http://[::1]:11434",
])
def test_local_endpoints_are_accepted(endpoint):
    l4_llm.ensure_local(endpoint)


@pytest.mark.parametrize("endpoint", [
    "http://192.168.0.10:11434",          # 같은 공유기 안의 다른 기기도 '밖'이다
    "https://api.example.com/v1",
    "http://example.com:11434",
    "https://127.0.0.1:11434",            # 로컬이라도 예상 밖 형태는 받지 않는다
    "http://user:pw@127.0.0.1:11434",
    "http://127.0.0.1.example.com:11434",
])
def test_remote_endpoints_are_refused(endpoint):
    with pytest.raises(ConfigError):
        l4_llm.ensure_local(endpoint)


def test_settings_from_config_refuse_remote_endpoint(tmp_path):
    config = default_config(tmp_path)
    config.settings["llm"] = {"endpoint": "http://10.0.0.5:11434"}
    with pytest.raises(ConfigError):
        l4_llm.LLMSettings.from_config(config)


def test_requests_bypass_system_proxy(monkeypatch):
    # 프록시 환경변수가 있어도 원문이 실린 요청은 프록시를 타지 않는다
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example.com:8080")
    monkeypatch.setenv("http_proxy", "http://proxy.example.com:8080")
    handlers = [h for h in l4_llm._opener().handlers
                if isinstance(h, urllib.request.ProxyHandler)]
    assert all(not getattr(h, "proxies", {}) for h in handlers)


def test_preflight_stops_when_model_is_unreachable():
    settings = l4_llm.LLMSettings(endpoint="http://127.0.0.1:9", timeout=2)
    with pytest.raises(DeidentError):
        l4_llm.preflight(settings)


# ── 후보 검증 ────────────────────────────────────────────────────────────────
def test_invented_strings_are_dropped(workspace, monkeypatch):
    config, source = workspace
    _fake_model(monkeypatch, [
        {"text": "보리스", "type": "person"},
        {"text": "문서에 없는 이름", "type": "person"},
    ])
    result = pipeline.scan_file(source, config, Mapping(), llm=l4_llm.LLMSettings())
    from_llm = [f for f in result.findings if "L4" in f.detectors]
    assert [f.text for f in from_llm] == ["보리스"]
    assert any("버렸습니다" in i.message for i in result.issues)


def test_organisations_are_review_only_by_default(workspace, monkeypatch):
    config, source = workspace
    _fake_model(monkeypatch, [{"text": "해든조선해양", "type": "org"}])
    result = pipeline.apply_file(source, config, Mapping(), llm=l4_llm.LLMSettings())
    finding = next(f for f in result.findings if f.text == "해든조선해양")
    assert finding.category == CAT_ORG and finding.grade == GRADE_REVIEW
    assert result.gate is not None and result.gate.passed
    output = result.outputs[0].read_text(encoding="utf-8")
    assert "해든조선해양" in output          # 치환하지 않았다
    assert any(i.code == "llm-review-org" for i in result.issues)


def test_organisations_are_replaced_when_asked(workspace, monkeypatch):
    config, source = workspace
    _fake_model(monkeypatch, [{"text": "해든조선해양", "type": "org"}])
    settings = l4_llm.LLMSettings(replace_orgs=True)
    result = pipeline.apply_file(source, config, Mapping(), llm=settings)
    assert result.gate is not None and result.gate.passed
    assert "해든조선해양" not in result.outputs[0].read_text(encoding="utf-8")


def test_person_candidates_are_replaced_and_pass_the_gate(workspace, monkeypatch):
    config, source = workspace
    _fake_model(monkeypatch, [{"text": "보리스", "type": "person"}])
    result = pipeline.apply_file(source, config, Mapping(), llm=l4_llm.LLMSettings())
    finding = next(f for f in result.findings if f.text == "보리스")
    assert finding.category == CAT_PERSON and finding.grade == GRADE_LIKELY
    assert result.gate is not None and result.gate.passed
    assert "보리스" not in result.outputs[0].read_text(encoding="utf-8")


def test_existing_findings_are_never_weakened(workspace, monkeypatch):
    config, source = workspace
    # 모델이 이미 잡힌 전화번호를 포함해 더 넓게 집어도, 전화번호 탐지는 그대로다
    _fake_model(monkeypatch, [{"text": "문의는 010-2345-6789", "type": "phone"},
                              {"text": "010-2345-6789", "type": "org"}])
    result = pipeline.apply_file(source, config, Mapping(), llm=l4_llm.LLMSettings())
    phone = next(f for f in result.findings if f.text == "010-2345-6789")
    assert phone.grade != GRADE_REVIEW and "L1" in phone.detectors
    assert "010-2345-6789" not in result.outputs[0].read_text(encoding="utf-8")


def test_masked_text_is_what_the_model_sees(workspace, monkeypatch):
    config, source = workspace
    sent = _fake_model(monkeypatch, [])
    pipeline.scan_file(source, config, Mapping(), llm=l4_llm.LLMSettings())
    assert sent and all("010-2345-6789" not in text for text in sent)
    assert any(l4_llm.MASKED in text for text in sent)


def test_two_syllable_value_inside_another_word_is_skipped(workspace, monkeypatch):
    config, source = workspace
    # '수연'을 받으면 '교수연구실' 때문에 게이트가 영원히 막힌다
    _fake_model(monkeypatch, [{"text": "수연", "type": "person"}])
    result = pipeline.apply_file(source, config, Mapping(), llm=l4_llm.LLMSettings())
    assert not [f for f in result.findings if f.text == "수연"]
    assert result.gate is not None and result.gate.passed


def test_shapeless_candidates_are_dropped(workspace, monkeypatch):
    config, source = workspace
    source.write_text("법인카드로 결제했다. 1번 지원자는 보류한다. 실무는 보리스가 맡는다.\n",
                      encoding="utf-8")
    _fake_model(monkeypatch, [
        {"text": "법인카드", "type": "account"},      # 숫자 없는 '계좌'
        {"text": "1번 지원자", "type": "person"},      # 숫자 든 '이름'
        {"text": "보리스", "type": "person"},
    ])
    result = pipeline.scan_file(source, config, Mapping(), llm=l4_llm.LLMSettings())
    assert [f.text for f in result.findings if "L4" in f.detectors] == ["보리스"]


def test_quasi_identifiers_go_to_private_report_only(workspace, monkeypatch):
    config, source = workspace
    phrase = "실무는 보리스가 맡는다"
    _fake_model(monkeypatch, [{"text": phrase, "type": "quasi"}])
    result = pipeline.apply_file(source, config, Mapping(), llm=l4_llm.LLMSettings())
    assert result.llm_quasi == [phrase]
    # 사람과 AI가 함께 읽는 채널에는 건수만 나간다
    assert all(phrase not in i.message for i in result.issues)
    assert phrase in result.outputs[0].read_text(encoding="utf-8")   # 치환하지 않는다


def test_gate_does_not_call_the_model(workspace, monkeypatch):
    config, source = workspace
    sent = _fake_model(monkeypatch, [{"text": "보리스", "type": "person"}])
    pipeline.apply_file(source, config, Mapping(), llm=l4_llm.LLMSettings())
    calls_with_llm = len(sent)
    assert calls_with_llm == 1          # 조각 하나 — 게이트의 재스캔은 부르지 않았다


def test_failed_chunks_are_reported_not_hidden(workspace, monkeypatch):
    config, source = workspace

    def boom(settings, text):
        raise OSError("연결 끊김")

    monkeypatch.setattr(l4_llm, "_ask", boom)
    result = pipeline.scan_file(source, config, Mapping(), llm=l4_llm.LLMSettings())
    assert any(i.code == "llm-partial" for i in result.issues)


def test_without_flag_the_model_is_not_used(workspace, monkeypatch):
    config, source = workspace
    sent = _fake_model(monkeypatch, [{"text": "보리스", "type": "person"}])
    result = pipeline.apply_file(source, config, Mapping())
    assert not sent and not [f for f in result.findings if "L4" in f.detectors]
