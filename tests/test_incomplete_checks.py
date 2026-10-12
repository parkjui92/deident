"""검사하지 못한 문서를 통과시키거나 공유 폴더에 남기지 않는다."""
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deident import formats, pipeline  # noqa: F401,E402
from deident.config import default_config
from deident.detect import l4_llm
from deident.entity.pseudonym import Mapping
from deident.errors import DocumentRejected
from deident.verify.gate import run_gate


def test_unreadable_embedded_document_stops_scan(tmp_path):
    from docx import Document
    source = tmp_path / "broken.docx"
    doc = Document()
    doc.add_paragraph("일반 문서")
    doc.save(source)
    with zipfile.ZipFile(source, "a") as zf:
        zf.writestr("word/embeddings/data.xlsx", b"unreadable workbook")
    with pytest.raises(DocumentRejected):
        pipeline.scan_file(source, default_config(tmp_path), Mapping())


def test_unreadable_output_cannot_pass_verification(tmp_path):
    config = default_config(tmp_path)
    config.workspace.prepare()
    source = config.workspace.out / "broken.docx"
    source.write_bytes(b"unreadable office file")
    result = run_gate(source, Mapping(), config)
    assert not result.passed
    assert result.quarantined and result.quarantined.exists()
    assert any(i.level == "block" for i in result.issues)


@pytest.mark.parametrize("content", ["{}", '{"items":null}', '{"items":{}}', "[]",
                                     '{"items":[{"text":"test"}]}'])
def test_invalid_model_reply_is_not_a_clean_result(tmp_path, monkeypatch, content):
    config = default_config(tmp_path)
    source = tmp_path / "memo.md"
    source.write_text("다음 회의는 수요일입니다.", encoding="utf-8")
    monkeypatch.setattr(l4_llm, "_request", lambda *a: {"message": {"content": content}})
    result = pipeline.apply_file(source, config, Mapping(), llm=l4_llm.LLMSettings())
    assert result.blocked and not result.ok
    assert not result.outputs
    assert not list(config.workspace.out.glob("*.비식별.*"))


def test_interrupted_model_stops_apply(tmp_path, monkeypatch):
    config = default_config(tmp_path)
    source = tmp_path / "memo.md"
    source.write_text("다음 회의는 수요일입니다.", encoding="utf-8")
    def fail(*args):
        raise OSError("synthetic connection failure")
    monkeypatch.setattr(l4_llm, "_ask", fail)
    result = pipeline.apply_file(source, config, Mapping(), llm=l4_llm.LLMSettings())
    assert result.blocked and not result.ok
    assert not result.outputs


def test_apply_failure_does_not_leave_unverified_output(tmp_path, monkeypatch):
    config = default_config(tmp_path)
    source = tmp_path / "memo.md"
    source.write_text("일반 문서", encoding="utf-8")
    handler = pipeline.handler_for(source)
    def fail(path, out_path, *args, **kwargs):
        out_path.write_text("unfinished output", encoding="utf-8")
        raise OSError("synthetic write failure")
    monkeypatch.setattr(handler, "apply", fail)
    with pytest.raises(OSError):
        pipeline.apply_file(source, config, Mapping())
    assert not list(config.workspace.out.glob("*.비식별.*"))


def test_missing_format_output_is_not_partial_success(tmp_path, monkeypatch):
    config = default_config(tmp_path)
    source = tmp_path / "memo.txt"
    source.write_text("일반 문서", encoding="utf-8")
    monkeypatch.setattr(pipeline.handler_for(source), "apply", lambda *a, **kw: [])
    result = pipeline.apply_file(source, config, Mapping())
    assert result.blocked and not result.outputs
    assert not list(config.workspace.out.glob("*.비식별.*"))


def test_scan_exit_code_reports_incomplete_model(tmp_path, monkeypatch, capsys):
    from deident.cli import main, EXIT_GATE_FAILED
    source = tmp_path / "memo.md"
    source.write_text("다음 회의는 수요일입니다.", encoding="utf-8")
    monkeypatch.setattr(l4_llm, "preflight", lambda *a: None)
    monkeypatch.setattr(l4_llm, "_request", lambda *a: {"message": {"content": "{}"}})
    assert main(["--root", str(tmp_path), "scan", str(source), "--llm"]) == EXIT_GATE_FAILED
    assert "검사" in capsys.readouterr().out
