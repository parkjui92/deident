"""탐지 규칙 시험 — 실측에서 놓쳤던 자리와, 고치다 생길 수 있는 과탐.

규칙마다 '잡아야 하는 것'과 '잡으면 안 되는 것'을 짝으로 둔다. 한쪽만 시험하면
재현율을 올리다 문서를 망가뜨리거나, 과탐을 줄이다 이름을 놓치는 일을 알아채지 못한다.
이름·기관·번호는 전부 허구다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent / "bench"))

from deident import formats, pipeline  # noqa: E402,F401
from deident.config import default_config  # noqa: E402
from deident.detect import l1_patterns, l3_ner  # noqa: E402
from deident.entity.pseudonym import Mapping  # noqa: E402
from deident.model import (  # noqa: E402
    CAT_ADDRESS, CAT_DEPT, CAT_EMAIL, CAT_HANDLE, CAT_ORG, CAT_PERSON, CAT_PHONE,
    CAT_PROJECT_NO, CAT_TEL, Piece, Segment,
)
from deident.report import mask  # noqa: E402

needs_kiwi = pytest.mark.skipif(not l3_ner.available(), reason="kiwipiepy 없음")


def _segment(text: str) -> Segment:
    return Segment("s", text, [Piece(None, "line", 0, len(text))])


def _l1(text: str) -> set[tuple[str, str]]:
    return {(f.text, f.category) for f in l1_patterns.detect_segment(_segment(text))}


def _detect(tmp_path: Path, text: str) -> set[tuple[str, str]]:
    config = default_config(tmp_path)
    path = tmp_path / "문서.md"
    path.write_text(text, encoding="utf-8")
    segments = pipeline.handler_for(path).extract(path)
    return {(f.text, f.category) for f in pipeline.detect(segments, config, Mapping())}


def _texts(found: set[tuple[str, str]], category: str) -> set[str]:
    return {text for text, cat in found if cat == category}


# ── 연락처·번호 (L1) ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("line,value,category", [
    ("사무실 044)123-4567 로 부탁드립니다", "044)123-4567", CAT_TEL),
    ("대표번호 (02) 123-4567", "(02) 123-4567", CAT_TEL),
    ("문의는 내선 4417 로", "4417", CAT_TEL),
    ("현지 연락처 +47 55 50 01 42", "+47 55 50 01 42", CAT_TEL),
    ("급한 연락은 공일공-이삼사오-육칠팔구", "공일공-이삼사오-육칠팔구", CAT_PHONE),
    ("메일은 hong dot gd 골뱅이 daum 쩜 net 으로", "hong dot gd 골뱅이 daum 쩜 net", CAT_EMAIL),
    ("카카오톡 gildong_92 로 보내 주세요", "gildong_92", CAT_HANDLE),
    ("개인 사번(E20190452)으로 로그인", "E20190452", CAT_HANDLE),
    ("같은 과제(과제번호 20193010)에 참여", "20193010", CAT_PROJECT_NO),
    ("선행 과제(2022R1A2C4007716)에서는", "2022R1A2C4007716", CAT_PROJECT_NO),
])
def test_contact_shapes_are_detected(line, value, category):
    assert (value, category) in _l1(line)


def test_account_with_thirteen_digits_is_not_dropped():
    # 자릿수가 우연히 13자리라 주민번호 탐지기로 넘겨지고, 거기서도 안 잡혀 통째로 남았다
    found = _l1("반납 계좌: 농협 301-0274-5518-63 (예금주: 산학협력단)")
    assert any(text == "301-0274-5518-63" for text, _ in found)


@pytest.mark.parametrize("line", [
    "02) 2023 2024년 추진 계획",                 # 목록 번호 뒤 연도
    "look at this.example and data at www.site.com",  # 영어 문장의 at
    "ID: admin",                                   # 숫자·밑줄 없는 낱말
    "연구기간은 2026.03.01 ~ 2028.02.28이다",
    "법률 제1234호에 따라",
])
def test_ordinary_text_is_not_a_contact(line):
    assert not _l1(line)


def test_parenthesised_mobile_keeps_brackets_balanced():
    found = _texts(_l1("담당자 (010-1234-5678)"), CAT_PHONE)
    assert found == {"010-1234-5678"}   # 여는 괄호만 삼키면 닫는 괄호가 홀로 남는다


# ── 주소 조각 (L1) ───────────────────────────────────────────────────────────
def test_residence_needs_context_or_unit():
    assert ("한솔아파트", CAT_ADDRESS) in _l1("지금은 읍내 한솔아파트에 살며 출근한다")
    assert ("새빛빌라 3동 201호", CAT_ADDRESS) in _l1("새빛빌라 3동 201호")
    assert not _l1("임대아파트 공급을 늘린다")
    assert not _l1("노후 아파트 단지 정비 계획")


def test_room_number_with_particle():
    assert ("에너지공학관 412호", CAT_ADDRESS) in _l1("연구실은 에너지공학관 412호에 있다")
    assert ("사회과학관 B106호", CAT_ADDRESS) in _l1("오후 4시, 사회과학관 B106호")


def test_full_address_wins_over_room_fragment():
    # 호실 조각이 먼저 자리를 잡으면 도로명이 통째로 남는다
    found = _texts(_l1("주소: 가정로 141 본관 302호"), CAT_ADDRESS)
    assert found == {"가정로 141 본관 302호"}


def test_address_absorbs_complex_name():
    found = _texts(_l1("댁은 세종시 한누리대로 2130 가온마을 7단지 맞으시죠"), CAT_ADDRESS)
    assert found == {"세종시 한누리대로 2130 가온마을 7단지"}


@pytest.mark.parametrize("value,category", [
    ("한솔아파트", CAT_ADDRESS),
    ("공학관 512호", CAT_ADDRESS),
    ("공일공-이삼사오-육칠팔구", CAT_PHONE),
    ("4417", CAT_TEL),
])
def test_mask_hides_short_values(value, category):
    masked = mask.mask_value(value, category)
    assert value not in masked and masked != value


# ── 이름 (L3) ────────────────────────────────────────────────────────────────
@needs_kiwi
def test_attendee_list_including_two_syllable_name(tmp_path):
    found = _texts(_detect(tmp_path, "참석: 민지훈, 오세린, 탁현우, 제갈윤(온라인), 강산\n"),
                   CAT_PERSON)
    assert {"민지훈", "오세린", "탁현우", "제갈윤", "강산"} <= found


@needs_kiwi
def test_label_value_that_is_a_department_is_not_a_name(tmp_path):
    assert not _texts(_detect(tmp_path, "참조: 기획실\n담당: 정책 기획 총괄\n"), CAT_PERSON)


@needs_kiwi
@pytest.mark.parametrize("line,name", [
    ("지난 8월 육동현 단장이 설명회에서 밝힌 방향", "육동현"),
    ("기획위원인 어수연 박사가 이미 두 차례 지적", "어수연"),
    ("실험은 박사후연구원 남궁설이 전담한다", "남궁설"),
    ("석사과정 황보람과 함께 보조한다", "황보람"),
    ("10월 1일부로 신입 장한솔 사원으로 교체합니다", "장한솔"),
    ("지원한 구슬기 박사를 추천합니다", "구슬기"),        # 앞 두 글자가 우연히 낱말
    ("실장: 방극현 / 협조자: 대사체연구단 엄기성", "방극현"),
    ("위원 수당은 국민은행 (예금주 표승아)로 입금", "표승아"),
])
def test_names_with_title_or_role_cue(tmp_path, line, name):
    assert name in _texts(_detect(tmp_path, line + "\n"), CAT_PERSON)


@needs_kiwi
@pytest.mark.parametrize("line", [
    "교수 연구실에서 진행한다",
    "위원 구성원을 확정한다",
    "신임 교수 정원을 늘린다",
    "박사후 연구원 채용 공고",
    "박사후연구원 인건비를 편성한다",
    "반도체 시장 규모는 커지고 있다",
    "10명 위원을 위촉하였다",
    "전 장관은 인터뷰에서 밝혔다",
    "각 국 대표가 참석하였다",
    "제 책임은 여기까지입니다",
    "지도 교수는 아직 정해지지 않았다",
    "석 달 남짓한 학생 연수",
    "강화군이나 고성군 같은 인구감소지역",
    "정보화 사업과 표준화 작업을 병행한다",
])
def test_common_phrases_are_not_names(tmp_path, line):
    assert not _texts(_detect(tmp_path, line + "\n"), CAT_PERSON)


@needs_kiwi
def test_surname_and_title_with_particle(tmp_path):
    text = ("참석: 탁현우, 남궁설, 서나래\n"
            "탁 책임은 일정 조정이 필요하다고 했다. 인건비는 남궁 박사와 학생에게 배정한다. "
            "문의는 서 선임에게 한다.\n")
    found = _texts(_detect(tmp_path, text), CAT_PERSON)
    assert {"탁", "남궁", "서"} <= found


@needs_kiwi
def test_given_name_alone_follows_a_confirmed_full_name(tmp_path):
    text = ("참석: 민지훈, 오세린, 탁현우\n"
            "지훈이 먼저 결과를 공유했다. 세린 씨가 지적했던 문제는 해결했다.\n")
    found = _texts(_detect(tmp_path, text), CAT_PERSON)
    assert {"지훈", "세린"} <= found


@needs_kiwi
def test_given_name_that_is_a_common_noun_needs_an_address_form(tmp_path):
    base = "| 구분 | 성명 |\n|---|---|\n| 책임자 | 주하늘 |\n\n"
    assert "하늘" not in _texts(_detect(tmp_path, base + "오늘 하늘은 맑았다.\n"), CAT_PERSON)
    assert "하늘" in _texts(_detect(tmp_path, base + "하늘 씨가 발표했다.\n"), CAT_PERSON)
    assert "하늘" in _texts(_detect(tmp_path, base + "하늘이는 오늘 쉰다.\n"), CAT_PERSON)


@needs_kiwi
def test_given_name_embedded_in_another_word_does_not_trap_the_gate(tmp_path):
    # '수연'을 매핑에 올리면 '교수연구실'이 게이트에서 누출로 잡혀 정상 문서가 막힌다
    config = default_config(tmp_path)
    config.workspace.prepare()
    source = tmp_path / "메모.md"
    source.write_text("기획위원인 어수연 박사가 지적했다. 수연이 다시 확인했다.\n"
                      "교수연구실 배정은 다음 달에 한다.\n", encoding="utf-8")
    result = pipeline.apply_file(source, config, Mapping())
    assert result.gate is not None and result.gate.passed
    output = result.outputs[0].read_text(encoding="utf-8")
    assert "어수연" not in output and "교수연구실" in output


@needs_kiwi
@pytest.mark.parametrize("line", [
    "수요 전망은 변사랑(2024)의 추정치를 인용했다",
    "국내에서는 금나래 외(2019)와 봉지수(2021)가 검토했다",
    "이는 선행연구와 일치한다(권수경, 김주연, 2019)",
])
def test_cited_authors_are_left_alone(tmp_path, line):
    assert not _texts(_detect(tmp_path, line + "\n"), CAT_PERSON)


@needs_kiwi
@pytest.mark.parametrize("line,name", [
    ("해외 자문은 Prof. Serin Oh가 맡는다", "Serin Oh"),
    ("스위스의 Prof. Céline Rochat 연구실에 파견", "Céline Rochat"),
    ("마이클 첸(Michael Chen) 박사가 맡기로 했다", "Michael Chen"),
    ("From: Seung-A Pyo", "Seung-A Pyo"),
    ("실무 창구로 박사후연구원 Tomasz Wieczorek을 지정", "Tomasz Wieczorek"),
    ("솔바켄 박사는 운용 자료를 공유하기로 했다", "솔바켄"),
])
def test_foreign_names(tmp_path, line, name):
    assert name in _texts(_detect(tmp_path, line + "\n"), CAT_PERSON)


def test_latin_name_in_name_column(tmp_path):
    text = "| 구분 | 성명 | 소속 |\n|---|---|---|\n| 참여연구원 | Nguyen Thi Lan | 가람대학교 |\n\n본문.\n"
    assert "Nguyen Thi Lan" in _texts(_detect(tmp_path, text), CAT_PERSON)


def test_english_institution_is_not_a_person(tmp_path):
    found = _detect(tmp_path, "Seoul National University 교수진이 참여했다.\n")
    assert not _texts(found, CAT_PERSON)


# ── 기관·부서 (L3) ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("line", [
    "면접자: 연구보조원 한 명",
    "박사후연구원 두 명을 채용한다",
    "평가위원 풀을 공유한다",
    "인적자원 개발과 생태공원 조성",
    "특허출원 건수가 늘었다",
    "중앙정부와 지방정부의 역할",
])
def test_role_and_common_nouns_are_not_organisations(tmp_path, line):
    assert not _texts(_detect(tmp_path, line + "\n"), CAT_ORG)


@pytest.mark.parametrize("line,value", [
    ("큰딸은 읍내 가람초등학교 3학년이다", "가람초등학교"),
    ("2024년 지방자치인재개발원 장기교육", "지방자치인재개발원"),
    ("탁 책임은 화학연 쪽 장비 예약이 밀렸다고 했다", "화학연"),
    ("강산은 포스텍 분석 위탁비가 과소하다고 했다", "포스텍"),
    ("지금 스타트업 '루멘웍스'를 운영하신다", "루멘웍스"),
    ("나경태 과장 | ㈜온새미센서 연구기획팀", "온새미센서"),
    ("해외 자문은 UC Davis 소속 연구자", "UC Davis"),
    ("영주시 보건소에서 근무한다", "영주시 보건소"),
])
def test_organisations_without_the_usual_suffix(tmp_path, line, value):
    assert value in _texts(_detect(tmp_path, line + "\n"), CAT_ORG)


def test_university_short_form_follows_the_full_name(tmp_path):
    text = ("| 성명 | 소속 |\n|---|---|\n| 주하늘 | 누리대학교 |\n\n"
            "계산은 누리대 공학관의 클러스터를 쓴다.\n")
    assert "누리대" in _texts(_detect(tmp_path, text), CAT_ORG)


@needs_kiwi
def test_department_after_organisation(tmp_path):
    found = _detect(tmp_path, "검토자: 표승아 (서해대학교 신소재공학부)\n"
                              "작성: 서림대학교 산학협력단 연구비관리팀 채수빈\n")
    assert {"신소재공학부", "연구비관리팀"} <= _texts(found, CAT_DEPT)


@needs_kiwi
def test_particle_after_organisation_is_not_a_department(tmp_path):
    found = _detect(tmp_path, "한국연구재단 사업과 연계하여 추진한다. 한국연구재단 연구결과 공개.\n")
    assert not _texts(found, CAT_DEPT)


# ── 가명 모양 ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("token,expected", [
    ("[연구자A]", True), ("[기관AB]", True), ("[휴대전화12]", True), ("[주소1]", True),
    ("[예지]", False), ("[참고]", False), ("[가림]", False),
])
def test_only_tagged_brackets_count_as_pseudonyms(token, expected):
    from deident.model import RE_PSEUDONYM
    assert bool(RE_PSEUDONYM.fullmatch(token)) is expected


@needs_kiwi
def test_messenger_nicknames_in_brackets_are_replaced(tmp_path):
    # 대화명은 '[이름]' 꼴이라 가명과 모양이 같다. 가명으로 오인하면 치환도 못 하고
    # 게이트도 통과하지 못한다.
    config = default_config(tmp_path)
    config.workspace.prepare()
    source = tmp_path / "대화.md"
    source.write_text("참석: 민지훈, 오세린, 탁현우\n\n"
                      "[지훈] [오전 9:14] 자료 올렸습니다\n"
                      "[세린] [오전 9:15] 확인했어요\n", encoding="utf-8")
    result = pipeline.apply_file(source, config, Mapping())
    assert result.gate is not None and result.gate.passed
    output = result.outputs[0].read_text(encoding="utf-8")
    assert "지훈" not in output and "세린" not in output


# ── 묶음 실측이 뒤로 가지 않게 ───────────────────────────────────────────────
@needs_kiwi
def test_dev_corpus_does_not_regress():
    import measure as bench

    total = bench.score("corpus_dev")
    assert total["passed"] == total["docs"], "게이트를 통과하지 못한 문서가 있다"
    # 남는 것은 접미사 없는 회사 이름·식별 서술 등 규칙으로 못 잡는 것들이다
    assert total["missed"] <= 12, f"누락이 늘었다: {total['missed']}/{total['gold']}"
    assert total["false"] <= 6, f"오탐이 늘었다: {total['false']}"
