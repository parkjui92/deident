"""XML 문서 공용 조인·역매핑 치환 엔진 (hwpx·docx 공유).

한 문장이 여러 텍스트 노드로 쪼개져 있는 것이 한글·워드 파일의 기본값이다.
('홍길동' 이 <t>홍길</t><t>동</t> 으로 나뉘어 저장되는 일이 흔하다.)
그래서 탐지는 이어 붙인 문자열에서 하고, 치환은 그 오프셋을 다시 노드 위치로
되돌려 넣는다. 되돌릴 때 지켜야 할 것:

  · 노드 문자열은 lxml 의 .text/.tail 로만 만진다. 직렬화된 XML 문자열에
    정규식을 대면 태그·네임스페이스가 깨진다.
  · 한 노드에 여러 편집이 걸리면 뒤에서 앞으로 적용한다.
  · 매치가 여러 노드에 걸치면 첫 노드에 대체 문자열을 넣고 나머지는 지운다.
    빈 노드는 지우지 않는다 — 서식·참조가 붙어 있다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterator

from lxml import etree

from ..model import Piece, Replacement, Segment


@dataclass
class _Edit:
    local_start: int
    local_end: int
    text: str


def iter_text_slots(element: etree._Element,
                    skip: Callable[[etree._Element], bool] | None = None,
                    ) -> Iterator[tuple[etree._Element, str]]:
    """문서 순서대로 (노드, 슬롯) 을 낸다.

    lxml 은 텍스트를 element.text 와 child.tail 두 곳에 나눠 담는다.
    둘 다 훑지 않으면 문장의 절반을 놓친다.
    """
    if skip is not None and skip(element):
        return
    if element.text:
        yield element, "text"
    for child in element:
        yield from iter_text_slots(child, skip)
        if child.tail:
            yield child, "tail"


def get_slot(node: etree._Element, slot: str) -> str:
    return (node.text if slot == "text" else node.tail) or ""


def set_slot(node: etree._Element, slot: str, value: str) -> None:
    if slot == "text":
        node.text = value
    else:
        node.tail = value


def build_segment(seg_id: str, root: etree._Element, *,
                  kind: str, part: str, order: int,
                  skip: Callable[[etree._Element], bool] | None = None,
                  pseudo: Callable[[etree._Element], str | None] | None = None,
                  table_ctx=None) -> Segment:
    """한 문단(또는 셀)의 텍스트 노드를 이어 붙여 세그먼트를 만든다.

    pseudo 는 텍스트가 없는 요소(탭·줄바꿈)를 어떤 문자로 대신할지 정한다.
    그 자리는 editable=False 로 표시돼 치환이 넘어가지 못하는 벽이 된다.
    """
    pieces: list[Piece] = []
    buf: list[str] = []
    cursor = 0

    def walk(element: etree._Element) -> None:
        nonlocal cursor
        if skip is not None and skip(element):
            return
        if pseudo is not None:
            token = pseudo(element)
            if token is not None:
                # 공백만으로 된 자리는 '무른 경계' — 매치가 가로질러도 된다.
                # 탭·줄바꿈은 단단한 경계로 남겨 문단·칸을 넘는 치환을 막는다.
                soft = bool(token) and token.strip() == "" and not {"\t", "\n"} & set(token)
                pieces.append(Piece(node=element, slot="pseudo", start=cursor,
                                    length=len(token), editable=False, soft=soft))
                buf.append(token)
                cursor += len(token)
                return
        if element.text:
            pieces.append(Piece(node=element, slot="text", start=cursor,
                                length=len(element.text)))
            buf.append(element.text)
            cursor += len(element.text)
        for child in element:
            walk(child)
            if child.tail:
                pieces.append(Piece(node=child, slot="tail", start=cursor,
                                    length=len(child.tail)))
                buf.append(child.tail)
                cursor += len(child.tail)

    walk(root)
    return Segment(seg_id=seg_id, text="".join(buf), pieces=pieces, kind=kind,
                   part=part, order=order, table_ctx=table_ctx)


def apply_to_segment(seg: Segment, plans: list[Replacement]) -> int:
    """세그먼트의 치환 계획을 실제 노드에 반영하고 적용 건수를 돌려준다."""
    if not plans:
        return 0

    edits: dict[tuple[int, str], list[_Edit]] = {}
    node_of: dict[tuple[int, str], etree._Element] = {}
    applied = 0

    for plan in plans:
        covering = [p for p in seg.pieces
                    if p.editable and p.start < plan.end and plan.start < p.end]
        if not covering:
            continue
        if not getattr(plan, "cross_boundary", False) and any(
                not p.editable and not p.soft and p.start < plan.end and plan.start < p.end
                for p in seg.pieces):
            continue   # 단단한 경계(탭·줄바꿈)를 넘는 매치는 손대지 않는다
        # 경계를 넘기로 한 계획은 글자만 지우고 경계 요소는 그대로 둔다
        # (탭·줄바꿈 요소는 우리가 지울 수 없고, 남아도 문서가 상하지 않는다)

        covering.sort(key=lambda p: p.start)
        for idx, piece in enumerate(covering):
            local_start = max(0, plan.start - piece.start) + piece.node_offset
            local_end = min(piece.length, plan.end - piece.start) + piece.node_offset
            key = (id(piece.node), piece.slot)
            node_of[key] = piece.node
            # 대체 문자열은 첫 조각에만 넣고, 나머지 조각에서는 걸린 부분을 지운다
            text = plan.replacement if idx == 0 else ""
            edits.setdefault(key, []).append(_Edit(local_start, local_end, text))
        applied += 1

    for key, items in edits.items():
        node = node_of[key]
        slot = key[1]
        value = get_slot(node, slot)
        for edit in sorted(items, key=lambda e: e.local_start, reverse=True):
            value = value[:edit.local_start] + edit.text + value[edit.local_end:]
        set_slot(node, slot, value)

    return applied


def apply_to_segments(segments: list[Segment],
                      plans: dict[str, list[Replacement]]) -> tuple[int, set[int]]:
    """여러 세그먼트에 적용하고 (적용 건수, 손댄 세그먼트 순번) 을 돌려준다."""
    total, touched = 0, set()
    for seg in segments:
        seg_plans = plans.get(seg.seg_id)
        if not seg_plans:
            continue
        count = apply_to_segment(seg, seg_plans)
        if count:
            total += count
            touched.add(seg.order)
    return total, touched


def preserve_space(node: etree._Element, slot: str = "text") -> None:
    """앞뒤 공백이 살아남도록 xml:space 를 지정한다 (docx 에서 필요)."""
    value = get_slot(node, slot)
    if value != value.strip():
        node.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")


@dataclass
class XmlPart:
    """수정 대상 XML 멤버 하나."""

    name: str
    tree: etree._ElementTree
    dirty: bool = False
    segments: list[Segment] = field(default_factory=list)

    def serialize(self) -> bytes:
        """원본 선언을 유지한 채 직렬화한다 (pretty_print 금지 — 공백 오염)."""
        return etree.tostring(
            self.tree,
            xml_declaration=True,
            encoding=self.tree.docinfo.encoding or "UTF-8",
            standalone=self.tree.docinfo.standalone,
        )
