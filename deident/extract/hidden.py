"""숨은 자리 추출 — 화면에 보이지 않는 곳의 글자를 세그먼트로 올린다.

본문·표만 읽으면 문서의 절반만 본 것이다. 오피스 파일에는 글자가 사는 곳이 더 있다.

  · XML 속성 — 시트 이름, 정의된 이름, 책갈피, 도형 이름·대체 텍스트, 도구 설명,
    문서 변수, 외부 링크 주소(mailto:…)
  · 곁가지 멤버 — 메모, 차트(제목·범례·저장된 값), 스마트아트, 문서 속성, customXml

이 자리들을 세그먼트로 올려야 탐지·치환·게이트가 같은 것을 본다. 치환기만 따로
훑게 하면 탐지 리포트에는 '없음'으로 나오는데 파일에는 남는, 가장 나쁜 조합이 된다.

세그먼트는 KIND_HIDDEN 으로 표시한다. AI 입력용 마크다운에는 싣지 않는다.
"""

from __future__ import annotations

import io
import re
import zipfile
from typing import Callable, Iterable

from lxml import etree

from ..apply.xml_apply import ATTR_SLOT, build_segment
from ..model import KIND_HIDDEN, Piece, Segment

NS_DRAWING = "http://schemas.openxmlformats.org/drawingml/2006/main"
NS_SHEET = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
A_BR = f"{{{NS_DRAWING}}}br"

# 여러 조각(run)으로 쪼개진 글자를 한 문장으로 이어 읽어야 하는 그릇
RICH_CONTAINERS = frozenset({
    f"{{{NS_DRAWING}}}p",                 # 도형·차트·스마트아트의 문단
    f"{{{NS_SHEET}}}text",                # 엑셀 메모 본문
    f"{{{NS_SHEET}}}si", f"{{{NS_SHEET}}}is",
})

# 글자가 들어가는 속성 이름 (네임스페이스를 뗀 이름)
TEXT_ATTRS = frozenset({
    "name", "descr", "title", "alt", "tooltip", "author", "creator", "instr",
    "display", "location", "anchor", "prompt", "promptTitle", "error", "errorTitle",
    "displayName", "userId", "lastModifiedBy",
})
# val 은 온갖 설정값이 쓰는 이름이라, 글자가 들어가는 요소에서만 본다
VAL_PARENTS = frozenset({"docVar", "alias", "tag"})
# 한글이 들어 있어도 내용이 아닌 속성 (글꼴·언어·서식 이름)
SKIP_ATTRS = frozenset({
    "typeface", "eastAsia", "ascii", "hAnsi", "cs", "lang", "altLang", "bidi",
    "panose", "style", "type", "uri", "id", "Id", "Type", "TargetMode", "ContentType",
    "PartName", "Extension", "formatCode", "numFmtId", "fontId", "ref", "sqref",
    "spans", "r", "s", "t", "codeName", "space", "schemaLocation", "Ignorable",
})

# 내용이 없는 멤버 — 글꼴·테마·서식 정의. 여기까지 훑으면 서식 이름이 이름으로 잡힌다.
RE_TECHNICAL_MEMBER = re.compile(
    r"(?:^|/)(?:theme|printerSettings|media|fonts|activeX|ctrlProps)/"
    r"|(?:^|/)(?:styles|stylesWithEffects|fontTable|webSettings|numbering|tableStyles"
    r"|calcChain|viewProps|sharedStrings)\.xml$"
    r"|\[Content_Types\]\.xml$"
)
XML_SUFFIXES = (".xml", ".rels", ".vml")
MAX_MEMBER_BYTES = 64 * 1024 * 1024

RE_HANGUL = re.compile(r"[가-힣]")
RE_LETTERS = re.compile(r"[A-Za-z가-힣]{2}")
RE_GROUPED_NUMBER = re.compile(r"\d{2,}[-–]\d{3,}")       # 010-1234-…, 800101-1…
RE_LONG_DIGITS = re.compile(r"(?<!\d)\d{9,}(?!\d)")
# GUID·시각 표기는 번호처럼 생겼지만 내용이 아니다. GUID 는 다른 글자 사이에 끼어
# 있어도 걷어낸다 — 0으로만 된 GUID 는 16자리 숫자로 읽혀 카드번호 검증식까지 통과한다.
RE_GUID = re.compile(
    r"\{?[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\}?")
RE_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")


def _without_machine_values(value: str) -> str:
    v = value.strip()
    if RE_TIMESTAMP.match(v):
        return ""
    return RE_GUID.sub(" ", v).strip()

_PARSER = etree.XMLParser(remove_blank_text=False, resolve_entities=False,
                          no_network=True, huge_tree=True)


def local_name(name) -> str:
    return name.rsplit("}", 1)[-1] if isinstance(name, str) else ""


def has_content(value: str, long_digits: bool = True) -> bool:
    """이 문자열에 사람·기관·번호가 들어 있을 여지가 있는가.

    long_digits=False 는 속성값용이다. 속성에 든 긴 숫자는 거의 다 식별자여서
    (`creationId val="1840726560"`), 번호로 보면 정상 문서가 게이트에 걸린다(실측).
    """
    v = _without_machine_values(value)
    if len(v) < 2:
        return False
    if RE_HANGUL.search(v) or "@" in v or RE_GROUPED_NUMBER.search(v):
        return True
    return long_digits and bool(RE_LONG_DIGITS.search(v))


def has_words(value: str) -> bool:
    """글자가 들어가는 자리로 정해진 곳은 영문만 있어도 본다 (영문 이름·기관)."""
    v = _without_machine_values(value)
    if len(v) < 2:
        return False
    return bool(RE_LETTERS.search(v)) or has_content(v)


def attr_wanted(element: etree._Element, key: str, value: str, in_rels: bool) -> bool:
    name = local_name(key)
    if in_rels:
        # 관계 파일에서 내용이 들어가는 곳은 바깥을 가리키는 주소뿐이다
        return name == "Target" and element.get("TargetMode") == "External" and has_words(value)
    if name == "val":
        return local_name(element.tag) in VAL_PARENTS and has_words(value)
    if name in SKIP_ATTRS or key.startswith("{http://www.w3.org/"):
        return False
    if name in TEXT_ATTRS:
        return has_words(value)
    return has_content(value, long_digits=False)


def is_technical_member(name: str) -> bool:
    return bool(RE_TECHNICAL_MEMBER.search(name))


def parse_bytes(data: bytes) -> etree._ElementTree | None:
    try:
        return etree.parse(io.BytesIO(data), _PARSER)
    except etree.XMLSyntaxError:
        return None


def _pseudo(element: etree._Element) -> str | None:
    return "\n" if element.tag == A_BR else None


class Collector:
    """트리를 훑어 숨은 자리 세그먼트를 모은다. seg_id 는 훑는 순서로 정해져 결정적이다."""

    def __init__(self, order_start: int = 0) -> None:
        self.segments: list[Segment] = []
        self.order = order_start

    def _add(self, seg_id: str, text: str, pieces: list[Piece], part: str) -> None:
        self.segments.append(Segment(seg_id=seg_id, text=text, pieces=pieces,
                                     kind=KIND_HIDDEN, part=part, order=self.order))
        self.order += 1

    def attributes(self, root: etree._Element, part: str) -> None:
        in_rels = part.endswith(".rels")
        seq = 0
        for element in root.iter():
            if not isinstance(element.tag, str):
                continue
            for key, value in element.attrib.items():
                if not value or not attr_wanted(element, key, value, in_rels):
                    continue
                self._add(f"{part}@a{seq:05d}", value,
                          [Piece(node=element, slot=ATTR_SLOT + key, start=0,
                                 length=len(value))], part)
                seq += 1

    def texts(self, root: etree._Element, part: str,
              skip: Callable[[etree._Element], bool] | None = None) -> None:
        seq = 0

        def leaf(node: etree._Element, slot: str, value: str) -> None:
            nonlocal seq
            if not has_words(value):
                return
            self._add(f"{part}@t{seq:05d}", value,
                      [Piece(node=node, slot=slot, start=0, length=len(value))], part)
            seq += 1

        def walk(element: etree._Element) -> None:
            nonlocal seq
            if not isinstance(element.tag, str):
                return
            if skip is not None and skip(element):
                return
            if element.tag in RICH_CONTAINERS:
                seg = build_segment(f"{part}@t{seq:05d}", element, kind=KIND_HIDDEN,
                                    part=part, order=self.order, pseudo=_pseudo)
                if has_words(seg.text):
                    self.segments.append(seg)
                    self.order += 1
                    seq += 1
                return
            if element.text:
                leaf(element, "text", element.text)
            for child in element:
                walk(child)
                if child.tail and isinstance(child.tag, str):
                    leaf(child, "tail", child.tail)

        walk(root)


def side_members(names: Iterable[str], modeled: Iterable[str],
                 exclude: Iterable[str] = ()) -> list[str]:
    """추출기가 직접 다루지 않는 XML 멤버 가운데 내용이 있을 수 있는 것 (이름순)."""
    skip = set(modeled) | set(exclude)
    return sorted(n for n in names
                  if n not in skip and n.lower().endswith(XML_SUFFIXES)
                  and not is_technical_member(n))


def collect(zf: zipfile.ZipFile, modeled: dict[str, etree._ElementTree], *,
            order_start: int = 0, exclude: Iterable[str] = (),
            partial: dict[str, Callable[[etree._Element], bool]] | None = None,
            ) -> tuple[dict[str, etree._ElementTree], list[Segment]]:
    """(곁가지 멤버 트리, 숨은 자리 세그먼트).

    modeled 는 포맷 추출기가 이미 본문을 세그먼트로 만든 트리다. 거기서는 속성만
    본다 (글자는 이미 덮여 있다). partial 에 건너뛸 범위를 준 트리는 그 범위 밖의
    글자도 함께 본다 — 엑셀 시트에서 셀 영역(sheetData) 밖의 머리글·목록 상자처럼.
    """
    partial = partial or {}
    collector = Collector(order_start)

    for name in sorted(modeled):
        root = modeled[name].getroot()
        if name in partial:
            collector.texts(root, name, skip=partial[name])
        collector.attributes(root, name)

    trees: dict[str, etree._ElementTree] = {}
    for name in side_members(zf.namelist(), modeled, exclude):
        info = zf.getinfo(name)
        if info.file_size > MAX_MEMBER_BYTES:
            continue
        tree = parse_bytes(zf.read(name))
        if tree is None:
            continue
        before = len(collector.segments)
        if not name.endswith(".rels"):
            collector.texts(tree.getroot(), name)
        collector.attributes(tree.getroot(), name)
        if len(collector.segments) > before:
            trees[name] = tree

    return trees, collector.segments


def touched_parts(segments: list[Segment], touched_orders: set[int]) -> set[str]:
    return {seg.part for seg in segments if seg.order in touched_orders}
