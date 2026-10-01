"""L4 — 로컬 LLM이 놓친 후보를 제안한다 (선택 기능, 기본 꺼짐).

규칙(L1~L3)이 원리상 못 잡는 것이 있다 — 접미사 없는 회사 이름, 아무 단서 없이
본문에만 나오는 이름. 그 자리를 이 컴퓨터에서 도는 언어모델에 묻는다.

안전을 위한 제약 네 가지. 이 층의 설계는 이것이 전부다.
  1. 모델은 '가릴 문자열 목록'만 낸다. 문서를 고쳐 쓰지 않는다.
  2. 모델이 낸 문자열이 원문에 글자 그대로 없으면 버린다 (지어낸 후보 차단).
  3. 이미 잡힌 자리는 건드리지 않는다. 이 층은 탐지를 보탤 뿐 줄이지 못한다.
  4. 주소는 이 컴퓨터(127.0.0.1)만 허용한다. 다른 주소면 원문이 밖으로 나가므로
     실행을 거부한다. 시스템 프록시도 타지 않는다.

검증 게이트는 이 층을 쓰지 않는다. 모델을 게이트에 넣으면 같은 문서가 돌릴 때마다
다르게 판정될 수 있다. 이 층이 찾은 값도 매핑에 오르므로, 게이트는 그 값이
산출물에 남지 않았는지를 예전처럼 글자 그대로 확인한다.

실측(가상 문서 6건·정답 71건, gemma3:12b)에서 드러난 한계와 대응:
  - 부처·대기업·역사 인물을 금지해도 집는다 → 기관 후보는 자동 치환하지 않고
    '검토' 등급으로 리포트에만 올린다. 치환하려면 --llm-orgs.
  - 한 번에 길게 넣으면 재현율이 무너진다 → 500자 안팎으로 잘라 묻는다.
  - 1,000자에 10~30초 걸린다 → 열기 전 경보(guard)에는 쓰지 않는다.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from . import l3_ner

from ..config import Config
from ..entity import boundary
from ..entity.registry import match_is_whole
from ..kdata import ORG_STOPWORDS, PLACE_NAMES
from ..errors import ConfigError, DeidentError
from ..model import (
    CAT_ACCOUNT, CAT_ADDRESS, CAT_EMAIL, CAT_HANDLE, CAT_ORG, CAT_PERSON, CAT_PHONE,
    GRADE_LIKELY, GRADE_REVIEW, RE_PSEUDONYM, DocumentIssue, Finding, Segment,
    normalize_key,
)

LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
MASKED = "[가림]"
MIN_LENGTH = 2
MAX_LENGTH = 60

TYPE_CATEGORY = {
    "person": CAT_PERSON, "org": CAT_ORG, "phone": CAT_PHONE, "email": CAT_EMAIL,
    "account": CAT_ACCOUNT, "id": CAT_HANDLE, "address": CAT_ADDRESS,
}
QUASI = "quasi"
NUMBER_LIKE = frozenset({CAT_PHONE, CAT_ACCOUNT})

SYSTEM = """너는 문서 비식별화 도구의 보조 탐지기다. 문서를 고쳐 쓰지 않고, 가려야 할 표현을 찾아 목록으로만 답한다.

찾을 것 — 문서에 등장하는 특정 개인을 알아낼 수 있게 하는 표현:
- person: 사람 이름. 성명 전체, 이름만 부른 것('지훈이'의 '지훈'), 성+직함('김 박사'), 영문 표기, 별칭
- org: 그 사람들이 속한 기관·회사·학교·부서. 약칭도 포함
- phone / email / account / id: 전화번호, 전자우편, 계좌, 메신저 계정·사번·과제번호. 숫자를 한글로 풀어 썼거나 기호를 말로 바꿔 쓴 것도 포함
- address: 집 주소, 거주지, 건물·호실
- quasi: 이름이 없어도 누구인지 좁혀지는 서술(예: '부서에서 유일한 여성 팀장', 가족의 직장·학교)

찾지 말 것:
- 일반명사, 기술 용어, 직함 자체('교수', '책임연구원')
- 정책 주체나 분석 대상으로 언급된 정부 부처·공공기관·대기업
- 법령 이름, 제도 이름
- 인용 문헌의 저자, 역사적 인물, 널리 알려진 공인
- [가림] 이나 [성명A] 처럼 대괄호로 이미 가려진 자리

규칙:
- text 에는 문서에 적힌 글자 그대로를 옮긴다. 고치거나 요약하지 않는다.
- 가능한 한 짧게 자른다. 이름은 이름만, 서술은 식별에 필요한 구절만.
- 같은 표현은 한 번만 적는다. 없으면 빈 목록을 낸다."""

# 본보기 한 벌 — 실측에서 식별 서술 탐지가 4건 중 1건에서 3건으로 늘었다
EXAMPLE_DOC = ("간담회에는 배도윤 팀장과 미래에너지랩 직원 둘이 참석했다. 도윤 씨는 산업부 "
               "고시가 바뀌면 현대자동차 같은 완성차 업체가 먼저 영향을 받는다고 했다. "
               "배 팀장은 사내에서 유일한 외국 국적 임원이며, 아내가 한성초등학교 교사다. "
               "회의에서는 정약용의 실학과 권오름(2022)의 연구가 언급됐다. "
               "문의는 도윤닷배 골뱅이 메일닷컴.")
EXAMPLE_ANSWER = json.dumps({"items": [
    {"text": "배도윤", "type": "person"},
    {"text": "도윤 씨", "type": "person"},
    {"text": "배 팀장", "type": "person"},
    {"text": "미래에너지랩", "type": "org"},
    {"text": "사내에서 유일한 외국 국적 임원", "type": "quasi"},
    {"text": "아내가 한성초등학교 교사", "type": "quasi"},
    {"text": "도윤닷배 골뱅이 메일닷컴", "type": "email"},
]}, ensure_ascii=False)

SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "type": {"type": "string",
                             "enum": [*TYPE_CATEGORY, QUASI]},
                },
                "required": ["text", "type"],
            },
        }
    },
    "required": ["items"],
}


@dataclass
class LLMSettings:
    model: str = "gemma3:12b"
    endpoint: str = "http://127.0.0.1:11434"
    chunk_chars: int = 500
    timeout: int = 180
    replace_orgs: bool = False
    seed: int = 7

    @classmethod
    def from_config(cls, config: Config, *, model: str | None = None,
                    replace_orgs: bool = False) -> "LLMSettings":
        raw = config.settings.get("llm") or {}
        if not isinstance(raw, dict):
            raise ConfigError("settings.yaml: llm 항목은 매핑이어야 합니다")
        settings = cls(
            model=str(raw.get("model", cls.model)),
            endpoint=str(raw.get("endpoint", cls.endpoint)),
            chunk_chars=int(raw.get("chunk_chars", cls.chunk_chars)),
            timeout=int(raw.get("timeout", cls.timeout)),
            replace_orgs=bool(raw.get("replace_orgs", False)) or replace_orgs,
        )
        if model:
            settings.model = model
        ensure_local(settings.endpoint)
        return settings


@dataclass
class LLMOutcome:
    findings: list[Finding] = field(default_factory=list)
    issues: list[DocumentIssue] = field(default_factory=list)
    chunks: int = 0
    failed: int = 0
    dropped: int = 0          # 원문에 없어 버린 후보
    seconds: float = 0.0
    quasi: list[str] | None = None   # 식별 서술 — 비공개 리포트 전용 (원문)


def ensure_local(endpoint: str) -> None:
    """주소가 이 컴퓨터를 가리키는지 확인한다. 아니면 멈춘다.

    이 도구의 약속은 '원문이 이 컴퓨터를 떠나지 않는다'이다. 설정 한 줄로 그
    약속이 깨지면 안 되므로, 편의보다 거부를 택한다.
    """
    parts = urlsplit(endpoint)
    if parts.scheme != "http" or (parts.hostname or "") not in LOCAL_HOSTS:
        raise ConfigError(
            "LLM 주소는 이 컴퓨터(http://127.0.0.1 또는 localhost)만 허용합니다. "
            "다른 주소로는 원문이 밖으로 나가므로 실행하지 않습니다.")
    if parts.username or parts.password:
        raise ConfigError("LLM 주소에 계정 정보를 넣을 수 없습니다.")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)


def _opener() -> urllib.request.OpenerDirector:
    # 프록시 환경변수가 걸려 있으면 로컬 주소로 보낸 요청도 프록시를 거친다.
    # 원문이 실린 요청이므로 프록시와 다른 주소로의 넘김을 모두 끈다.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def _request(settings: LLMSettings, path: str, payload: dict | None) -> dict:
    ensure_local(settings.endpoint)
    url = settings.endpoint.rstrip("/") + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    with _opener().open(req, timeout=settings.timeout) as resp:
        return json.loads(resp.read())


def preflight(settings: LLMSettings) -> None:
    """모델을 쓸 수 있는지 먼저 본다.

    --llm 을 달았는데 모델이 없다고 조용히 건너뛰면, 덜 본 것을 다 본 것처럼
    말하게 된다. 그래서 시작 전에 확인하고, 안 되면 처리 자체를 멈춘다.
    """
    try:
        tags = _request(settings, "/api/tags", None)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise DeidentError(
            f"로컬 LLM에 연결하지 못했습니다 ({exc.__class__.__name__}). "
            "ollama 가 켜져 있는지 확인하거나 --llm 을 빼고 실행하십시오.") from None
    names = {str(m.get("name", "")) for m in tags.get("models", [])}
    wanted = settings.model
    if wanted not in names and f"{wanted}:latest" not in names:
        raise DeidentError(
            f"모델 '{wanted}' 이(가) 이 컴퓨터에 없습니다. "
            f"`ollama pull {wanted}` 로 받거나 --llm-model 로 다른 모델을 지정하십시오.")


def _ask(settings: LLMSettings, text: str) -> list[dict]:
    payload = {
        "model": settings.model,
        "stream": False,
        "format": SCHEMA,
        "options": {"temperature": 0, "seed": settings.seed,
                    "num_ctx": 8192, "num_predict": 3000},
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": "문서:\n\n" + EXAMPLE_DOC},
            {"role": "assistant", "content": EXAMPLE_ANSWER},
            {"role": "user", "content": "문서:\n\n" + text},
        ],
    }
    data = _request(settings, "/api/chat", payload)
    raw = (data.get("message") or {}).get("content", "")
    items = json.loads(raw).get("items", [])
    return items if isinstance(items, list) else []


# ── 조각내기 ─────────────────────────────────────────────────────────────────
def _chunks(segments: list[Segment], limit: int) -> list[list[Segment]]:
    """문서 순서대로 limit 자 안팎의 묶음을 만든다. 세그먼트는 쪼개지 않는다."""
    out: list[list[Segment]] = []
    current: list[Segment] = []
    size = 0
    for seg in segments:
        if seg.table_ctx is not None and seg.table_ctx.is_header:
            continue
        text = seg.text.strip()
        if len(text) < MIN_LENGTH:
            continue
        if current and size + len(text) > limit:
            out.append(current)
            current, size = [], 0
        current.append(seg)
        size += len(text)
    if current:
        out.append(current)
    return out


def _spans_by_segment(findings: list[Finding]) -> dict[str, list[tuple[int, int]]]:
    spans: dict[str, list[tuple[int, int]]] = {}
    for f in findings:
        spans.setdefault(f.seg_id, []).append((f.start, f.end))
    return spans


def _masked(text: str, spans: list[tuple[int, int]]) -> str:
    """이미 잡힌 자리를 가린 글. 모델이 남은 것에만 집중하고, 출력도 짧아진다."""
    pieces: list[str] = []
    cursor = 0
    for start, end in sorted(spans):
        if start < cursor:
            continue
        pieces.append(text[cursor:start])
        pieces.append(MASKED)
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces)


def _free_pieces(start: int, end: int,
                 taken: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """[start, end) 에서 이미 잡힌 구간을 뺀 나머지 조각들."""
    pieces = [(start, end)]
    for s, e in sorted(taken):
        nxt: list[tuple[int, int]] = []
        for a, b in pieces:
            if e <= a or b <= s:
                nxt.append((a, b))
                continue
            if a < s:
                nxt.append((a, s))
            if e < b:
                nxt.append((e, b))
        pieces = nxt
    return pieces


def _short_hangul(value: str) -> bool:
    return len(value) <= 2 and all("가" <= ch <= "힣" for ch in value)


def _explained_everywhere(value: str, category: str, segments: list[Segment],
                          taken: dict[str, list[tuple[int, int]]]) -> bool:
    """두 글자 값이 문서 어디서든 '그 낱말'로만 쓰였는가.

    형태소 분석기가 없으면 게이트는 매핑에 오른 값을 글자 그대로 찾는다. '수연'을
    올렸는데 문서에 '교수연구실'이 있으면 치환기는 그 자리를 건드리지 않고 게이트는
    누출로 본다 — 정상 문서가 통과하지 못하게 된다. 그 환경에서는 그런 값을 올리지
    않는다. (분석기가 있으면 양쪽이 낱말 경계로 판정한다 — entity/boundary.py)
    """
    for seg in segments:
        cursor = seg.text.find(value)
        while cursor >= 0:
            end = cursor + len(value)
            covered = any(s <= cursor and end <= e for s, e in taken.get(seg.seg_id, []))
            if not covered and not match_is_whole(seg.text, cursor, end, category):
                return False
            cursor = seg.text.find(value, cursor + 1)
    return True


def detect(segments: list[Segment], config: Config, existing: list[Finding],
           settings: LLMSettings) -> LLMOutcome:
    """남은 자리에서 후보를 찾는다. existing 은 L1~L3 가 이미 잡은 것."""
    started = time.time()
    taken = _spans_by_segment(existing)
    kiwi = l3_ner._get_kiwi(None)
    # 인용 저자는 규칙 층과 같은 기준으로 건너뛴다 — 모델은 '변사랑(2024)'도 집는다
    citations = {seg.seg_id: l3_ner._citation_spans(seg.text) for seg in segments}
    allow = config.allowlist
    outcome = LLMOutcome()
    quasi: list[str] = []
    review = 0

    for chunk in _chunks(segments, settings.chunk_chars):
        outcome.chunks += 1
        text = "\n".join(_masked(seg.text, taken.get(seg.seg_id, [])) for seg in chunk)
        if not text.replace(MASKED, "").strip():
            continue
        try:
            items = _ask(settings, text)
        except (urllib.error.URLError, OSError, ValueError, KeyError):
            outcome.failed += 1
            continue

        seen: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            value = str(item.get("text", "")).strip()
            kind = str(item.get("type", ""))
            if (not MIN_LENGTH <= len(value) <= MAX_LENGTH or value in seen
                    or MASKED in value or RE_PSEUDONYM.search(value)
                    or normalize_key(value) in allow):
                continue
            seen.add(value)

            if kind == QUASI:
                if any(value in seg.text for seg in chunk):
                    quasi.append(value)
                else:
                    outcome.dropped += 1
                continue
            category = TYPE_CATEGORY.get(kind)
            if category is None or not config.category_enabled(category):
                continue
            # 모델은 '법인카드'를 계좌로, '1번 지원자'를 사람으로 내기도 한다(실측).
            # 전화·계좌에는 숫자가 있어야 하고, 사람 이름에는 숫자가 없어야 한다.
            # 더 조이면('엄지'처럼 낱말과 같은 이름, 숫자 없는 계정) 맞는 후보까지 버린다.
            has_digit = any(ch.isdigit() for ch in value)
            if category in NUMBER_LIKE and not has_digit:
                continue
            if category == CAT_PERSON and has_digit:
                continue
            # 형태소 분석기가 없으면 게이트가 글자 그대로 찾는다 — 그때만 가려 받는다
            if (_short_hangul(value) and not boundary.available()
                    and not _explained_everywhere(value, category, segments, taken)):
                continue
            if category == CAT_ORG and (
                    value in PLACE_NAMES or value in ORG_STOPWORDS
                    or l3_ner._role_noun(value)
                    or l3_ner._looks_common(kiwi, value, strict=False)):
                continue   # '정부', '스타트업', '강화군' — 기관 이름이 아니다

            hit = False
            for seg in chunk:
                spans = taken.setdefault(seg.seg_id, [])
                cursor = seg.text.find(value)
                while cursor >= 0:
                    hit = True
                    end = cursor + len(value)
                    # 두 글자 한글은 다른 낱말 속에도 있다 — 낱말 경계를 따진다
                    whole = (not _short_hangul(value)
                             or match_is_whole(seg.text, cursor, end, category))
                    cited = category == CAT_PERSON and l3_ner._inside(
                        cursor, end, citations.get(seg.seg_id, []))
                    if whole and not cited:
                        for a, b in _free_pieces(cursor, end, spans):
                            piece = seg.text[a:b]
                            lead = len(piece) - len(piece.lstrip())
                            a, b = a + lead, a + lead + len(piece.strip())
                            if b - a < MIN_LENGTH or not seg.slice_editable(a, b):
                                continue
                            grade = GRADE_LIKELY
                            if category == CAT_ORG and not settings.replace_orgs:
                                grade = GRADE_REVIEW
                                review += 1
                            outcome.findings.append(Finding(
                                seg_id=seg.seg_id, start=a, end=b, text=seg.text[a:b],
                                category=category, grade=grade, detectors={"L4"},
                                note=f"LLM 제안 ({settings.model})", part=seg.part,
                            ))
                            spans.append((a, b))
                    cursor = seg.text.find(value, cursor + 1)
            if not hit:
                outcome.dropped += 1   # 원문에 없는 문자열 — 모델이 지어냈다

    outcome.seconds = round(time.time() - started, 1)
    outcome.issues.append(DocumentIssue(
        level="info", code="llm-run",
        message=(f"로컬 LLM({settings.model})으로 {outcome.chunks}개 조각을 "
                 f"{outcome.seconds}초 동안 검사해 {len(outcome.findings)}건을 보탰습니다."
                 + (f" 원문에 없는 후보 {outcome.dropped}건은 버렸습니다."
                    if outcome.dropped else ""))))
    if outcome.failed:
        outcome.issues.append(DocumentIssue(
            level="warn", code="llm-partial",
            message=(f"LLM 검사가 {outcome.chunks}개 조각 중 {outcome.failed}개에서 "
                     "실패했습니다. 그 구간은 규칙으로만 검사했습니다.")))
    if review:
        outcome.issues.append(DocumentIssue(
            level="warn", code="llm-review-org",
            message=(f"LLM이 기관으로 본 {review}건은 치환하지 않고 '검토' 등급으로 "
                     "남겼습니다(공공기관·대기업을 자주 잘못 집습니다). 비공개 리포트에서 "
                     "확인한 뒤 가릴 것은 denylist 에 넣거나 --llm-orgs 로 다시 실행하십시오.")))
    if quasi:
        outcome.issues.append(DocumentIssue(
            level="warn", code="llm-quasi",
            message=(f"이름 없이도 사람을 좁힐 수 있는 서술 {len(quasi)}건을 LLM이 "
                     "짚었습니다. 치환할 수 없는 내용이라 비공개 리포트에만 적었습니다.")))
    outcome.quasi = quasi
    return outcome
