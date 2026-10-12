"""발표자료(.pptx) 처리기.

발표자료에는 발표자 이름·소속·조직도·연락처가 표지와 슬라이드 노트에 흔히 남는다.
구조는 xlsx 와 같은 OOXML zip 이고, 글자는 `<a:p>` 문단 안의 `<a:t>` 조각에 들어
있다. 한 문장이 서식 때문에 여러 조각으로 쪼개지는 것도 워드·한글과 같아서,
공용 조인·역매핑 엔진을 그대로 쓴다.

발표자 노트(`ppt/notesSlides/`)를 빼먹으면 안 된다 — 화면에 안 보인다고 파일에
없는 게 아니다.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from lxml import etree

from ..extract import embedded, hidden
from ..model import (
    KIND_CAPTION, KIND_HIDDEN, KIND_PARA, DocumentIssue, Replacement, Segment,
)
from . import ooxml
from .planner import apply_to_text
from .sweep import sweep_attributes
from .xml_apply import apply_to_segments, build_segment

NS_DRAWING = "http://schemas.openxmlformats.org/drawingml/2006/main"
P_TAG = f"{{{NS_DRAWING}}}p"
BREAK_TAGS = {f"{{{NS_DRAWING}}}br"}
FIELD_TAG = f"{{{NS_DRAWING}}}fld"


def _members(zf: zipfile.ZipFile) -> list[str]:
    """슬라이드 → 노트 → 마스터·레이아웃 순. 순서를 고정해야 seg_id 가 안정된다."""
    names = zf.namelist()

    def pick(prefix: str) -> list[str]:
        return sorted(n for n in names
                      if n.startswith(prefix) and n.endswith(".xml"))

    return (pick("ppt/slides/slide") + pick("ppt/notesSlides/notesSlide")
            + pick("ppt/slideMasters/slideMaster") + pick("ppt/slideLayouts/slideLayout")
            + [n for n in ("ppt/presProps.xml",) if n in names])


def _pseudo(element: etree._Element) -> str | None:
    """줄바꿈 요소는 편집 불가 경계로 바꿔 둔다."""
    return "\n" if element.tag in BREAK_TAGS else None


def parse(path: Path) -> tuple[dict[str, etree._ElementTree], list[Segment], set[str]]:
    """(수정 대상 XML 트리, 세그먼트, 늘 다시 쓰는 멤버)."""
    trees: dict[str, etree._ElementTree] = {}
    segments: list[Segment] = []
    order = 0

    with zipfile.ZipFile(path) as zf:
        for member in _members(zf):
            try:
                tree = etree.fromstring(zf.read(member)).getroottree()
            except etree.XMLSyntaxError:
                continue
            trees[member] = tree
            kind = KIND_CAPTION if "notesSlide" in member else KIND_PARA
            for index, paragraph in enumerate(tree.getroot().iter(P_TAG)):
                seg = build_segment(
                    f"{Path(member).stem}:{index:04d}", paragraph,
                    kind=kind, part=member, order=order, pseudo=_pseudo,
                )
                if seg.text.strip():
                    segments.append(seg)
                    order += 1

        # 슬라이드 밖에 사는 글자 — 차트·스마트아트·메모·문서 속성·도형 이름·링크 주소
        always = set(trees)
        side_trees, side_segments = hidden.collect(zf, trees, order_start=order)
        trees.update(side_trees)
        segments.extend(side_segments)
        segments.extend(embedded.extract(zf, order_start=order + len(side_segments)))

    return trees, segments, always


def extract(path: Path) -> list[Segment]:
    return parse(path)[1]


class PptxHandler:
    name = "pptx"
    suffixes = (".pptx", ".pptm")
    preserves_layout = True

    def extract(self, path: Path) -> list[Segment]:
        return extract(path)

    def to_markdown(self, path: Path, segments: list[Segment],
                    plans: dict[str, list[Replacement]]) -> str:
        lines: list[str] = []
        current = ""
        for seg in segments:
            if seg.kind == KIND_HIDDEN:
                continue
            if seg.part != current:
                lines.append(f"\n## {Path(seg.part).stem}\n")
                current = seg.part
            text = seg.text
            seg_plans = plans.get(seg.seg_id)
            if seg_plans:
                text = apply_to_text(text, seg_plans)
            lines.append(text)
        return "\n".join(lines)

    def apply(self, path: Path, out_path: Path,
              plans: dict[str, list[Replacement]],
              segments: list[Segment], mapping=None) -> list[DocumentIssue]:
        trees, fresh, always = parse(path)
        _, touched = apply_to_segments(fresh, plans)

        # 도형 이름·대체 텍스트에 본문이 복사돼 들어가는 일이 있다 (화면에 안 보인다)
        swept: set[str] = set()
        if mapping is not None:
            for name, tree in trees.items():
                if sweep_attributes(tree.getroot(), mapping):
                    swept.add(name)

        with zipfile.ZipFile(path) as zf:
            dirty = always | swept | hidden.touched_parts(fresh, touched)
            dirty |= ooxml.clear_identity(zf, trees)
            replaced = {name: ooxml.serialize(tree) for name, tree in trees.items()
                        if name in dirty}
            inner, issues = embedded.apply(zf, plans, mapping)
            replaced.update(inner)

        ooxml.rewrite_zip(path, out_path, replaced)

        issues.append(DocumentIssue(
            level="warn", code="pptx-images",
            message="발표자료의 그림·캡처(조직도·명함 사진 등) 속 글자는 읽지 못합니다. "
                    "슬라이드를 눈으로 확인하십시오.",
            where=path.name))
        return issues
