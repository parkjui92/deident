"""산출물 전수 탐색 — 매핑에 등재된 원값이 어디에도 남지 않았는지 확인한다.

치환기가 모델링한 영역만 보는 게 아니라 파일 안의 모든 바이트를 본다.
치환기가 다루지 않는 부분(미리보기 캐시·차트·customXml·멤버 파일명)에 원문이
남아도 여기서 반드시 드러난다 — 게이트가 치환기의 슈퍼셋인 이유다.
"""

from __future__ import annotations

import re
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path

from ..model import digits_only

# 텍스트로 볼 수 있는 부분만 추리기 위한 태그 제거
RE_TAG = re.compile(r"<[^>]*>")

# XML 계열은 선언대로 UTF-8 이다. 다른 인코딩으로도 읽어 보면 같은 바이트가
# 뜻 없는 글자 덩어리로 한 번 더 실려, 그 안에서 우연히 계좌·전화 모양이
# 만들어진다(실측: 5MB짜리 헛 조각에서 계좌번호 6건 오탐).
XML_LIKE = frozenset({".xml", ".hpf", ".rels", ".rdf", ".opf", ".xhtml"})


# 글자가 들어 있는 것이 확실한 확장자. 그 밖(그림·글꼴·OLE)은 이진으로 본다.
TEXT_SUFFIXES = frozenset({
    ".xml", ".txt", ".hpf", ".rels", ".json", ".csv", ".htm", ".html", ".md",
    ".xhtml", ".opf", ".plist", ".ini", ".cfg", ".yaml", ".yml",
})
BINARY_SUFFIXES = frozenset({
    ".bmp", ".png", ".jpg", ".jpeg", ".gif", ".tif", ".tiff", ".emf", ".wmf",
    ".ico", ".webp", ".ttf", ".otf", ".woff", ".woff2", ".bin", ".zip", ".ole",
    ".mp3", ".mp4", ".avi", ".wav", ".pdf", ".xls", ".ppt", ".doc",
})


def is_text_member(name: str) -> bool:
    """이 멤버를 '글자'로 다뤄도 되는가.

    그림 파일을 글자로 읽으면 무작위 바이트가 카드번호·전화번호 모양으로 보인다.
    실제로 게이트가 정상 문서를 이런 헛것 때문에 막는 일이 생긴다. 그래서
    패턴 탐지는 텍스트 멤버에만 돌리고, 이진 멤버에는 '아는 원값이 있는가'만 묻는다.
    """
    suffix = Path(name).suffix.lower()
    if suffix in BINARY_SUFFIXES:
        return False
    if suffix in TEXT_SUFFIXES or not suffix:
        return True
    return False


@dataclass
class Part:
    name: str
    text: str
    is_text: bool


def _decode_variants(data: bytes, name: str = "") -> list[str]:
    """같은 바이트열을 여러 인코딩으로 읽어 본다 (hwpx 미리보기는 UTF-16LE).

    XML 계열은 UTF-8 하나만 본다 — 나머지는 헛 글자만 만들어 낸다.
    """
    encodings = ("utf-8",) if Path(name).suffix.lower() in XML_LIKE else (
        "utf-8", "utf-16-le", "cp949")
    out: list[str] = []
    for encoding in encodings:
        try:
            text = data.decode(encoding, errors="ignore")
        except (UnicodeDecodeError, LookupError):
            continue
        if text.strip():
            out.append(text)
    return out


def iter_parts(path: Path) -> list[Part]:
    """산출물을 텍스트로 펼친다. 이진 멤버는 is_text=False 로 표시해 함께 낸다."""
    parts: list[Part] = []

    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                # 멤버 파일명 자체도 검사 대상 (홍길동_증명사진.jpg)
                parts.append(Part(f"{info.filename}#name", info.filename, True))
                if info.is_dir() or info.file_size > 64 * 1024 * 1024:
                    continue
                text_member = is_text_member(info.filename)
                data = zf.read(info)
                # 태그를 여기서 지우면 안 된다. 지운 뒤에는 태그로 쪼갤 수 없어
                # XML 한 편이 통째로 한 조각이 되고, 서로 떨어진 칸의 숫자가
                # 한 덩어리로 묶여 없는 계좌번호가 만들어진다(실측).
                for idx, text in enumerate(_decode_variants(data, info.filename)):
                    parts.append(Part(f"{info.filename}#{idx}", text, text_member))
        return parts

    data = path.read_bytes()
    if path.suffix.lower() == ".pdf":
        try:
            import fitz  # PyMuPDF

            with fitz.open(path) as doc:
                for page in doc:
                    parts.append(Part(f"page{page.number + 1}",
                                      page.get_text("text"), True))
        except Exception:
            pass
        # PDF 원시 바이트는 압축 스트림이라 글자로 읽으면 잡음이 된다
        for idx, text in enumerate(_decode_variants(data)):
            parts.append(Part(f"raw#{idx}", text, False))
        return parts

    text_file = is_text_member(path.name) or path.suffix.lower() in ("", ".md", ".txt")
    for idx, text in enumerate(_decode_variants(data, path.name)):
        parts.append(Part(f"raw#{idx}", text, text_file))
    return parts


def iter_text_parts(path: Path) -> list[tuple[str, str]]:
    """패턴 탐지용 — 글자로 볼 수 있는 부분만 (태그 제거본)."""
    return [(p.name, RE_TAG.sub(" ", p.text) if "<" in p.text else p.text)
            for p in iter_parts(path) if p.is_text]


def chunks_of(part: Part) -> list[str]:
    """XML 부분을 태그 단위로 쪼갠다.

    태그를 지우고 통째로 이어 붙이면, 서로 다른 문단·칸의 글자가 붙어 원문에
    없는 문자열이 만들어진다. 실제로 게이트가 그 헛것 때문에 정상 문서를 막았다.
    """
    if "<" in part.text:
        return [c for c in RE_TAG.split(part.text) if c.strip()]
    return [line for line in part.text.split("\n") if line.strip()]


RE_SEPARATORS = re.compile(r"[\s\-–—.·]")
MIN_SPACED_LENGTH = 3


def _normalized_forms(value: str) -> set[str]:
    """같은 값의 글자 그대로의 표기 (유니코드 정규형 차이만 흡수)."""
    forms = {unicodedata.normalize("NFC", value), unicodedata.normalize("NFD", value)}
    return {f for f in forms if len(f) >= 2}


def _spaced_form(value: str):
    """글자 사이에 공백·가운뎃점이 끼어도 찾는 패턴.

    '한 국 화 학'처럼 벌려 쓴 표기를 잡는다. 다만 구분자를 통째로 지운 뒤
    부분열을 찾는 방식은 쓰지 않는다 — 그러면 옆 낱말이 붙어 원문에 없는
    이름이 만들어지고, 정상 문서가 영원히 게이트를 통과하지 못한다(실측).
    """
    if len(value) < MIN_SPACED_LENGTH or any(ch.isdigit() for ch in value):
        return None
    return re.compile(r"[\s·]*".join(re.escape(ch) for ch in value))


def _prepare(originals: list[str], min_digits: int):
    prepared = []
    for original in originals:
        forms = _normalized_forms(original)
        digits = digits_only(original)
        prepared.append((original, forms, digits if len(digits) >= min_digits else "",
                         _spaced_form(original)))
    return prepared


def _hits(text: str, prepared, use_digits: bool) -> list[str]:
    # 숫자값만 구분자를 지우고 본다. 번호는 자릿수 자체가 특징이라 우연히
    # 만들어지지 않지만, 글자값은 압축하면 이웃 낱말과 뒤섞인다.
    compact = RE_SEPARATORS.sub("", text)
    digit_stream = digits_only(text) if use_digits else ""
    found = []
    for original, forms, digits, spaced in prepared:
        if any(form in text for form in forms):
            found.append(original)
        elif digits and (digits in compact or (digit_stream and digits in digit_stream)):
            found.append(original)
        elif spaced is not None and spaced.search(text):
            found.append(original)
    return found


def find_leaks(path: Path, originals: list[str], min_digits: int = 7,
               segment_texts: list[tuple[str, str]] | None = None,
               ) -> list[tuple[str, str]]:
    """(원값, 발견된 부분 이름) 목록. 비어 있으면 잔여 없음.

    두 층으로 본다.
      ① 원시 조각: 파일 안의 모든 부분을 태그 단위로 쪼개 훑는다. 치환기가
         모델링하지 않는 곳(미리보기·차트·멤버 파일명)까지 닿는다.
      ② 구조 텍스트: 추출기가 이어 붙인 문단 텍스트. 한 값이 여러 노드로
         쪼개져 저장돼도 읽히는 모습 그대로 잡는다.

    조각을 넘어선 이어 붙이기는 하지 않는다. 서로 다른 칸의 글자가 붙어 원문에
    없는 문자열을 만들어 내면 정상 문서가 통과하지 못한다.
    """
    if not originals:
        return []

    prepared = _prepare(originals, min_digits)
    leaks: list[tuple[str, str]] = []

    for part in iter_parts(path):
        if not part.text:
            continue
        for chunk in chunks_of(part):
            for original in _hits(chunk, prepared, part.is_text):
                leaks.append((original, part.name))

    for name, text in (segment_texts or []):
        for original in _hits(text, prepared, True):
            leaks.append((original, name))

    return leaks
