"""산출물 전수 탐색 — 매핑에 등재된 원값이 어디에도 남지 않았는지 확인한다.

치환기가 모델링한 영역만 보는 게 아니라 파일 안의 모든 바이트를 본다.
치환기가 다루지 않는 부분(미리보기 캐시·차트·customXml·멤버 파일명)에 원문이
남아도 여기서 반드시 드러난다 — 게이트가 치환기의 슈퍼셋인 이유다.

'모든 바이트'에는 글자로 바로 읽히지 않는 것이 섞여 있다. 그래서 세 가지를 따로 편다.
  · XML 속성값 — 태그를 버리면 시트 이름·책갈피·링크 주소가 함께 사라진다
  · 압축 안의 압축 — 차트에 딸린 엑셀은 바깥 zip 에서 압축된 덩어리일 뿐이다
  · PDF 의 쪽 밖 글자 — 주석·양식·책갈피·링크·첨부, 16진으로 적힌 객체 문자열
"""

from __future__ import annotations

import html
import io
import re
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path

from ..model import digits_only

# 텍스트로 볼 수 있는 부분만 추리기 위한 태그 제거
RE_TAG = re.compile(r"<[^>]*>")
# 태그 안의 속성값. 시트 이름·책갈피·대체 텍스트·링크 주소는 글자가 아니라 속성에
# 산다. 태그를 통째로 버리면 그 자리는 영영 검사되지 않는다(실측: 시트 이름에 남은
# 이름을 게이트가 통과시켰다).
RE_ATTR_VALUE = re.compile(r"""=\s*(?:"([^"]*)"|'([^']*)')""")
# 이보다 긴 속성값은 좌표·경로 데이터다. 숫자를 이어 붙여 보지 않는다
# (수천 개의 좌표를 이으면 없는 번호가 만들어진다).
MAX_ATTR_FOR_DIGITS = 200
# 압축 안의 압축을 몇 겹까지 풀어 볼 것인가 (차트에 딸린 엑셀은 한 겹이다)
MAX_NEST = 3

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
    # 태그로 쪼개지 않고 줄 단위로 본다 (XML 이 아닌 글자 — PDF 주석·책갈피·객체 사전).
    # PDF 객체 사전에는 `<<…>>` 가 있어 태그로 오인하면 내용이 통째로 사라진다.
    plain: bool = False


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


def _zip_parts(zf: zipfile.ZipFile, prefix: str = "", depth: int = 0) -> list[Part]:
    parts: list[Part] = []
    for info in zf.infolist():
        name = prefix + info.filename
        # 멤버 파일명 자체도 검사 대상 (홍길동_증명사진.jpg)
        parts.append(Part(f"{name}#name", info.filename, True, plain=True))
        if info.is_dir() or info.file_size > 64 * 1024 * 1024:
            continue
        data = zf.read(info)
        # 문서 안의 문서 — 차트에 딸린 엑셀, 포함된 워드. 압축돼 있어 바깥에서는
        # 글자로 보이지 않는다. 풀어서 같은 방식으로 본다.
        if depth < MAX_NEST and data[:2] == b"PK":
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as inner:
                    parts.extend(_zip_parts(inner, name + ">", depth + 1))
                continue
            except zipfile.BadZipFile:
                pass
        if data[:5] == b"%PDF-":
            parts.extend(_pdf_parts(data, name + ">"))
            continue
        text_member = is_text_member(info.filename)
        # 태그를 여기서 지우면 안 된다. 지운 뒤에는 태그로 쪼갤 수 없어
        # XML 한 편이 통째로 한 조각이 되고, 서로 떨어진 칸의 숫자가
        # 한 덩어리로 묶여 없는 계좌번호가 만들어진다(실측).
        for idx, text in enumerate(_decode_variants(data, info.filename)):
            parts.append(Part(f"{name}#{idx}", text, text_member))
    return parts


RE_PDF_HEX = re.compile(r"<((?:FEFF|feff)[0-9A-Fa-f\s]+)>")


def _pdf_object_text(source: str) -> str:
    """PDF 객체 사전을 글자로 — 16진으로 적힌 문자열을 풀어 덧붙인다.

    한글 책갈피·주석은 `<FEFFD64D…>` 꼴(UTF-16BE)로 저장된다. 바이트를 그대로
    읽어서는 어떤 인코딩으로도 글자가 되지 않는다.
    """
    decoded = []
    for m in RE_PDF_HEX.finditer(source):
        try:
            raw = bytes.fromhex(re.sub(r"\s", "", m.group(1)))
            decoded.append(raw.decode("utf-16", errors="ignore"))
        except ValueError:
            continue
    return source + "\n" + "\n".join(decoded) if decoded else source


def _pdf_parts(data: bytes, prefix: str = "") -> list[Part]:
    """PDF 에서 글자가 사는 모든 자리.

    쪽에 찍힌 글자만 보면 주석·양식 값·책갈피·링크 주소·첨부 파일을 놓친다.
    전부 PDF 뷰어에서 그대로 읽히는 자리다.
    """
    parts: list[Part] = []
    try:
        import fitz  # PyMuPDF

        with fitz.open(stream=data, filetype="pdf") as doc:
            meta = " \n".join(str(v) for v in (doc.metadata or {}).values() if v)
            parts.append(Part(f"{prefix}metadata", meta, True, plain=True))
            try:
                parts.append(Part(f"{prefix}xmp", doc.get_xml_metadata() or "", True))
            except Exception:
                pass
            for entry in doc.get_toc(simple=True):
                parts.append(Part(f"{prefix}bookmark", str(entry[1]), True, plain=True))
            for name in doc.embfile_names():
                info = doc.embfile_info(name)
                label = " \n".join(str(info.get(k, "")) for k in
                                    ("name", "filename", "ufilename", "desc", "description"))
                parts.append(Part(f"{prefix}attachment:{name}#name", label, True, plain=True))
                parts.extend(_bytes_parts(doc.embfile_get(name),
                                          f"{prefix}attachment:{name}"))
            for page in doc:
                where = f"{prefix}page{page.number + 1}"
                parts.append(Part(where, page.get_text("text"), True, plain=True))
                for annot in page.annots() or []:
                    text = " \n".join(str(v) for v in annot.info.values() if v)
                    parts.append(Part(f"{where}/annot", text, True, plain=True))
                for widget in page.widgets() or []:
                    text = " \n".join(str(v) for v in (
                        widget.field_name, widget.field_value, widget.field_label) if v)
                    parts.append(Part(f"{where}/form", text, True, plain=True))
                for link in page.get_links():
                    if link.get("uri"):
                        parts.append(Part(f"{where}/link", str(link["uri"]), True, plain=True))
            # 객체 사전 — 위의 통로로 드러나지 않는 문자열까지. 좌표·객체 번호가
            # 섞여 있어 번호 모양 탐지는 돌리지 않고 아는 원값만 찾는다.
            for xref in range(1, doc.xref_length()):
                try:
                    source = doc.xref_object(xref, compressed=False)
                except Exception:
                    continue
                if source:
                    parts.append(Part(f"{prefix}object{xref}", _pdf_object_text(source),
                                      False, plain=True))
    except Exception:
        pass
    # PDF 원시 바이트는 압축 스트림이라 글자로 읽으면 잡음이 된다
    for idx, text in enumerate(_decode_variants(data)):
        parts.append(Part(f"{prefix}raw#{idx}", text, False))
    return parts


def _bytes_parts(data: bytes, name: str) -> list[Part]:
    """이름만 아는 바이트 덩어리를 펼친다 (PDF 첨부 파일 등)."""
    if data[:2] == b"PK":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as inner:
                return _zip_parts(inner, name + ">", 1)
        except zipfile.BadZipFile:
            pass
    if data[:5] == b"%PDF-":
        return _pdf_parts(data, name + ">")
    text_member = is_text_member(name)
    return [Part(f"{name}#{idx}", text, text_member)
            for idx, text in enumerate(_decode_variants(data, name))]


def iter_parts(path: Path) -> list[Part]:
    """산출물을 텍스트로 펼친다. 이진 멤버는 is_text=False 로 표시해 함께 낸다."""
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            return _zip_parts(zf)

    data = path.read_bytes()
    if path.suffix.lower() == ".pdf" or data[:5] == b"%PDF-":
        return _pdf_parts(data)

    text_file = is_text_member(path.name) or path.suffix.lower() in ("", ".md", ".txt")
    return [Part(f"raw#{idx}", text, text_file)
            for idx, text in enumerate(_decode_variants(data, path.name))]


def iter_text_parts(path: Path) -> list[tuple[str, str]]:
    """패턴 탐지용 — 글자로 볼 수 있는 부분만 (태그 제거본)."""
    return [(p.name, RE_TAG.sub(" ", p.text) if "<" in p.text else p.text)
            for p in iter_parts(path) if p.is_text]


def attribute_values(text: str) -> list[str]:
    """XML 태그 안의 속성값 (엔티티를 푼 형태)."""
    values: list[str] = []
    for tag in RE_TAG.findall(text):
        if "=" not in tag:
            continue
        for m in RE_ATTR_VALUE.finditer(tag):
            value = m.group(1) if m.group(1) is not None else m.group(2)
            if value and len(value) >= 2:
                values.append(html.unescape(value))
    return values


def chunks_of(part: Part) -> list[str]:
    """부분을 조각으로 쪼갠다 — XML 은 글자 노드와 속성값, 그 밖은 줄 단위.

    태그를 지우고 통째로 이어 붙이면, 서로 다른 문단·칸의 글자가 붙어 원문에
    없는 문자열이 만들어진다. 실제로 게이트가 그 헛것 때문에 정상 문서를 막았다.
    """
    if part.plain or "<" not in part.text:
        return [line for line in part.text.split("\n") if line.strip()]
    chunks = [html.unescape(c) for c in RE_TAG.split(part.text) if c.strip()]
    chunks.extend(attribute_values(part.text))
    return chunks


RE_SEPARATORS = re.compile(r"[\s\-–—.·]")
RE_HANGUL = re.compile(r"[가-힣]")
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
    if len(text) > MAX_ATTR_FOR_DIGITS and " " in text and not RE_HANGUL.search(text):
        compact, use_digits = "", False     # 좌표·경로 데이터 — 글자 그대로만 대조한다
    else:
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
