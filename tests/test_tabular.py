"""엑셀·CSV 명단 처리 시험.

인적사항 명단은 엑셀로 오는 일이 많아 이 통로가 막히면 시스템의 의미가 반감된다.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deident import formats, pipeline  # noqa: E402,F401
from deident.config import default_config  # noqa: E402
from deident.entity.pseudonym import Mapping  # noqa: E402
from synth import PLANTED  # noqa: E402

openpyxl = pytest.importorskip("openpyxl")

ROSTER_HEADERS = ["연번", "성명", "소속", "생년월일", "연락처", "이메일", "주민등록번호"]


def _make_xlsx(path: Path) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "참여연구원"
    ws.append(ROSTER_HEADERS)
    ws.append([1, PLANTED["person_pi"], PLANTED["org_a"], PLANTED["birth"],
               PLANTED["phone_pi"], PLANTED["email_pi"], PLANTED["rrn_pi"]])
    ws.append([2, PLANTED["person_co"], PLANTED["org_b"], "1985년 7월 2일",
               "010-9876-5432", PLANTED["email_co"], PLANTED["rrn_co"]])
    # 첫 행이 머리글이 아닌 시트 — 실무에서 흔한 모양이다
    note = wb.create_sheet("비고")
    note.append(["작성자", PLANTED["person_pi"]])
    wb.save(path)


def _make_csv(path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["성명", "소속", "연락처", "이메일"])
        writer.writerow([PLANTED["person_pi"], PLANTED["org_a"],
                         PLANTED["phone_pi"], PLANTED["email_pi"]])
        writer.writerow([PLANTED["person_ext"], PLANTED["org_b"],
                         "010-9876-5432", PLANTED["email_co"]])


@pytest.fixture()
def roster(tmp_path: Path):
    config = default_config(tmp_path)
    config.workspace.prepare()
    xlsx_path = tmp_path / "명단.xlsx"
    csv_path = tmp_path / "명단.csv"
    _make_xlsx(xlsx_path)
    _make_csv(csv_path)
    return config, xlsx_path, csv_path


def test_xlsx_roster_is_deidentified(roster):
    config, xlsx_path, _ = roster
    result = pipeline.apply_file(xlsx_path, config, Mapping())

    assert result.gate is not None and result.gate.passed
    out = next(p for p in result.outputs if p.suffix == ".xlsx")

    wb = openpyxl.load_workbook(out)
    values = [str(v) for ws in wb for row in ws.iter_rows(values_only=True)
              for v in row if v is not None]
    joined = " ".join(values)
    for key in ("person_pi", "person_co", "rrn_pi", "rrn_co", "phone_pi",
                "email_pi", "org_a", "org_b"):
        assert PLANTED[key] not in joined, f"{key} 가 엑셀 산출물에 남았다"

    # 열 머리글은 남아야 표를 읽을 수 있다
    for header in ROSTER_HEADERS:
        assert header in joined


def test_xlsx_first_row_values_are_not_treated_as_headers(roster):
    """시트마다 첫 행이 머리글이라는 보장은 없다 — '작성자 | 홍길동' 이 흔하다."""
    config, xlsx_path, _ = roster
    result = pipeline.apply_file(xlsx_path, config, Mapping())
    out = next(p for p in result.outputs if p.suffix == ".xlsx")

    wb = openpyxl.load_workbook(out)
    note = [str(v) for row in wb["비고"].iter_rows(values_only=True)
            for v in row if v is not None]
    assert PLANTED["person_pi"] not in note
    assert "작성자" in note


def test_same_person_gets_same_pseudonym_across_sheets(roster):
    config, xlsx_path, _ = roster
    result = pipeline.apply_file(xlsx_path, config, Mapping())
    out = next(p for p in result.outputs if p.suffix == ".xlsx")

    wb = openpyxl.load_workbook(out)
    roster_name = wb["참여연구원"].cell(row=2, column=2).value
    note_name = wb["비고"].cell(row=1, column=2).value
    assert roster_name == note_name


def test_csv_roster_keeps_shape(roster):
    config, _, csv_path = roster
    result = pipeline.apply_file(csv_path, config, Mapping())

    assert result.gate is not None and result.gate.passed
    out = next(p for p in result.outputs if p.suffix == ".csv")
    rows = list(csv.reader(out.read_text(encoding="utf-8").splitlines()))

    assert rows[0] == ["성명", "소속", "연락처", "이메일"]
    assert len(rows) == 3
    assert all(len(r) == 4 for r in rows)
    joined = " ".join(v for r in rows for v in r)
    for key in ("person_pi", "person_ext", "phone_pi", "email_pi"):
        assert PLANTED[key] not in joined


def test_guard_flags_roster_before_opening(roster):
    from deident import guard

    config, xlsx_path, _ = roster
    results = guard.check_paths([xlsx_path], config)
    assert results[0].checked
    assert results[0].level == guard.LEVEL_STOP   # 주민등록번호가 있다
