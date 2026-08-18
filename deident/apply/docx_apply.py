"""docx 치환·위생·재포장.

치환 자체는 공용 엔진(xml_apply)이 한다. 이 모듈이 책임지는 것은 그 앞뒤다.

  앞 ─ 안전하게 고칠 수 있는 문서인지 판정한다. 변경 추적이 켜져 있으면
       거부한다. w:delText 에 '지운 것으로 보이는 원문' 이 그대로 살아 있어
       치환으로는 안전을 보장할 수 없기 때문이다.
  뒤 ─ 본문을 고쳐도 남는 자리를 소거한다. 메모(작성자 실명!)·문서 속성·
       설정·하이퍼링크 대상은 본문 텍스트가 아니라서 치환 경로가 닿지 않는다.
       여기서 지우지 않으면 게이트의 전수 탐색이 잡아내 산출물이 통째로
       격리된다 — 게이트가 치환기의 슈퍼셋인 이유이기도 하다.

재포장은 '고친 멤버만 새로 쓰고 나머지는 원본 바이트 그대로' 다.
"""

from __future__ import annotations

import posixpath
import zipfile
from pathlib import Path

from lxml import etree

from ..entity.pseudonym import Mapping
from ..extract import docx_ as docx_extract
from ..extract.docx_ import FALLBACK_MARK, W_RPR, W_T, qn
from ..model import (
    KIND_FOOTER, KIND_FOOTNOTE, KIND_HEADER, DocumentIssue, Replacement, Segment,
)
from .planner import apply_to_text
from .sweep import sweep_attributes
from .xml_apply import XmlPart, apply_to_segments, preserve_space

W_COMMENT_RANGE_START = qn("w:commentRangeStart")
W_COMMENT_RANGE_END = qn("w:commentRangeEnd")
W_COMMENT_REFERENCE = qn("w:commentReference")
W_R = qn("w:r")
W_ALTCHUNK = qn("w:altChunk")
W_RSIDS = qn("w:rsids")
W_DOC_PROTECTION = qn("w:documentProtection")
W_WRITE_PROTECTION = qn("w:writeProtection")
W_ATTACHED_TEMPLATE = qn("w:attachedTemplate")

DC_NS = "http://purl.org/dc/elements/1.1/"
CP_NS = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"

# 메모 계열 — people.xml 에는 메모 작성자 실명이 들어 있다
COMMENT_MEMBERS = (
    "word/comments.xml", "word/commentsExtended.xml", "word/commentsIds.xml",
    "word/commentsExtensible.xml", "word/people.xml",
)
CORE_CLEAR = ((DC_NS, "creator"), (CP_NS, "lastModifiedBy"))
CORE_DROP = ((CP_NS, "lastPrinted"),)
CORE_SUB = ((DC_NS, "title"), (DC_NS, "subject"), (DC_NS, "description"),
            (CP_NS, "keywords"), (CP_NS, "category"), (CP_NS, "contentStatus"))
APP_CLEAR = ("Company", "Manager", "Template")

PART_LABEL = {KIND_HEADER: "머리말", KIND_FOOTER: "꼬리말", KIND_FOOTNOTE: "각주·미주"}


# ── 트리 조작 ───────────────────────────────────────────────────────────────
def _remove(element: etree._Element) -> None:
    """요소를 지우되 꼬리 텍스트는 살린다 (lxml 의 remove 는 tail 을 함께 버린다)."""
    parent = element.getparent()
    if parent is None:
        return
    tail = element.tail
    if tail:
        prev = element.getprevious()
        if prev is not None:
            prev.tail = (prev.tail or "") + tail
        else:
            parent.text = (parent.text or "") + tail
    parent.remove(element)


def _sub_values(text: str, pairs: list[tuple[str, str]]) -> str:
    """치환 계획에 등재된 원값을 문자열에서 걷어낸다 (URL·문서속성용)."""
    out = text
    for original, replacement in pairs:
        if original and original in out:
            out = out.replace(original, replacement)
    return out


def _value_pairs(plans: dict[str, list[Replacement]],
                 mapping: Mapping | None = None) -> list[tuple[str, str]]:
    """(원값, 대체값) — 긴 값부터 적용해야 부분 매치가 긴 값을 갉지 않는다.

    매핑이 있으면 그쪽을 함께 쓴다. 게이트가 검사하는 범위가 곧 매핑이므로
    범위를 맞춰 두는 편이 안전하다.
    """
    seen: dict[str, str] = {}
    if mapping is not None:
        for entry in mapping.entries.values():
            for value in [entry.original, *entry.variants]:
                if len(value) >= 2 and value != entry.pseudonym:
                    seen.setdefault(value, entry.pseudonym)
    for items in plans.values():
        for plan in items:
            if len(plan.original) >= 2 and plan.original != plan.replacement:
                seen.setdefault(plan.original, plan.replacement)
    return sorted(seen.items(), key=lambda kv: len(kv[0]), reverse=True)


def _serialize(tree: etree._ElementTree) -> bytes:
    return XmlPart(name="", tree=tree).serialize()


def _revision_issue(parts: list[str]) -> DocumentIssue:
    return DocumentIssue(
        level="block", code="revisions",
        message="변경 추적이 켜져 있습니다. 워드에서 모든 변경을 적용/취소한 뒤 "
                "다시 실행하십시오.",
        where=", ".join(parts),
    )


# ── 본문 위생 ───────────────────────────────────────────────────────────────
def _mark_preserve(part: XmlPart, touched: set[int]) -> None:
    """치환으로 앞뒤 공백이 남은 w:t 에 xml:space="preserve" 를 건다."""
    for seg in part.segments:
        if seg.order not in touched:
            continue
        for piece in seg.pieces:
            node = piece.node
            if piece.slot == "text" and getattr(node, "tag", None) == W_T:
                preserve_space(node, "text")


def _strip_comment_marks(part: XmlPart) -> bool:
    """본문에 남은 메모 참조를 걷어낸다 (멤버 삭제만으로는 깨진 참조가 남는다)."""
    root = part.tree.getroot()
    changed = False
    runs: list[etree._Element] = []
    for tag in (W_COMMENT_RANGE_START, W_COMMENT_RANGE_END, W_COMMENT_REFERENCE):
        for element in list(root.iter(tag)):
            parent = element.getparent()
            if tag == W_COMMENT_REFERENCE and parent is not None and parent.tag == W_R:
                runs.append(parent)
            _remove(element)
            changed = True
    for run in runs:
        if run.getparent() is not None and all(c.tag == W_RPR for c in run):
            _remove(run)   # 내용이 사라진 빈 런은 남길 이유가 없다
    return changed


# ── 곁가지 멤버 위생 ────────────────────────────────────────────────────────
def _drop_targets(names: set[str]) -> tuple[set[str], list[str]]:
    """제거할 멤버와 그 사유 표시."""
    dropped: set[str] = set()
    reasons: list[str] = []

    comments = [n for n in COMMENT_MEMBERS if n in names]
    if comments:
        dropped.update(comments)
        reasons.append("comments")
    if "docProps/custom.xml" in names:
        dropped.add("docProps/custom.xml")
        reasons.append("custom-props")
    thumbs = [n for n in names if n.startswith("docProps/thumbnail.")]
    if thumbs:
        dropped.update(thumbs)
        reasons.append("thumbnail")

    for name in list(dropped):
        rels = posixpath.join(posixpath.dirname(name), "_rels",
                              posixpath.basename(name) + ".rels")
        if rels in names:
            dropped.add(rels)
    return dropped, reasons


def _rewrite_content_types(zf: zipfile.ZipFile, dropped: set[str]) -> bytes | None:
    name = "[Content_Types].xml"
    tree = docx_extract.parse_member(zf, name)
    if tree is None:
        return None
    root = tree.getroot()
    changed = False
    for override in list(root):
        if etree.QName(override).localname != "Override":
            continue
        part = str(override.get("PartName", "")).lstrip("/")
        if part in dropped:
            _remove(override)
            changed = True
    return _serialize(tree) if changed else None


def _rewrite_rels(zf: zipfile.ZipFile, name: str, dropped: set[str],
                  pairs: list[tuple[str, str]]) -> bytes | None:
    tree = docx_extract.parse_member(zf, name)
    if tree is None:
        return None
    root = tree.getroot()
    base = posixpath.dirname(posixpath.dirname(name))
    changed = False
    for rel in list(root):
        target = str(rel.get("Target", ""))
        rel_type = str(rel.get("Type", ""))
        if rel_type.endswith("/attachedTemplate"):
            # 첨부 서식 경로에는 작성자 계정명이 그대로 들어간다
            _remove(rel)
            changed = True
            continue
        if rel.get("TargetMode") == "External":
            new = _sub_values(target, pairs)
            if new != target:
                rel.set("Target", new)
                changed = True
            continue
        resolved = posixpath.normpath(posixpath.join(base, target.lstrip("/")))
        if resolved in dropped:
            _remove(rel)
            changed = True
    return _serialize(tree) if changed else None


def _scrub_core(zf: zipfile.ZipFile, pairs: list[tuple[str, str]]) -> bytes | None:
    tree = docx_extract.parse_member(zf, "docProps/core.xml")
    if tree is None:
        return None
    root = tree.getroot()
    changed = False
    for element in list(root):
        qname = etree.QName(element)
        key = (qname.namespace, qname.localname)
        if key in CORE_CLEAR:
            if element.text:
                element.text = ""
                changed = True
        elif key in CORE_DROP:
            _remove(element)
            changed = True
        elif key == (CP_NS, "revision"):
            if element.text != "1":
                element.text = "1"
                changed = True
        elif key in CORE_SUB and element.text:
            new = _sub_values(element.text, pairs)
            if new != element.text:
                element.text = new
                changed = True
    return _serialize(tree) if changed else None


def _scrub_app(zf: zipfile.ZipFile, pairs: list[tuple[str, str]]) -> bytes | None:
    tree = docx_extract.parse_member(zf, "docProps/app.xml")
    if tree is None:
        return None
    root = tree.getroot()
    changed = False
    for element in root.iter():
        if not isinstance(element.tag, str):
            continue
        local = etree.QName(element).localname
        if local in APP_CLEAR:
            if element.text:
                element.text = ""
                changed = True
        elif local in ("lpstr", "lpwstr") and element.text:
            # TitlesOfParts 에 목차 제목(이름이 섞이기도 한다)이 캐시된다
            new = _sub_values(element.text, pairs)
            if new != element.text:
                element.text = new
                changed = True
    return _serialize(tree) if changed else None


def _scrub_settings(zf: zipfile.ZipFile) -> bytes | None:
    tree = docx_extract.parse_member(zf, "word/settings.xml")
    if tree is None:
        return None
    root = tree.getroot()
    changed = False
    for tag in (W_RSIDS, W_DOC_PROTECTION, W_WRITE_PROTECTION, W_ATTACHED_TEMPLATE):
        for element in list(root.iter(tag)):
            _remove(element)
            changed = True
    return _serialize(tree) if changed else None


# ── 재포장 ──────────────────────────────────────────────────────────────────
def _repack(zf: zipfile.ZipFile, out_path: Path,
            modified: dict[str, bytes], dropped: set[str]) -> None:
    """고친 멤버만 새로 쓰고 나머지는 원본 바이트 그대로 옮긴다."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.name + ".tmp")
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as dst:
            for info in zf.infolist():
                if info.filename in dropped:
                    continue
                data = modified.get(info.filename)
                if data is None:
                    data = zf.read(info)
                entry = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                entry.compress_type = info.compress_type
                entry.external_attr = info.external_attr
                entry.internal_attr = info.internal_attr
                entry.create_system = info.create_system
                dst.writestr(entry, data)
        tmp.replace(out_path)
    finally:
        # 실패한 임시 파일을 out/ 에 남기지 않는다 (반쪽 치환본은 그 자체로 위험)
        if tmp.exists():
            tmp.unlink()


# ── 핸들러 ──────────────────────────────────────────────────────────────────
class DocxHandler:
    name = "docx"
    suffixes = (".docx",)
    preserves_layout = True

    def extract(self, path: Path) -> list[Segment]:
        return docx_extract.extract(path)

    def inspect(self, path: Path, segments: list[Segment]) -> list[DocumentIssue]:
        """치환을 시작하기 전에 이 문서가 안전하게 고쳐질 수 있는지 판정한다."""
        revised = docx_extract.revision_parts_of(path)
        if revised:
            return [_revision_issue(revised)]
        return []

    # ── AI 입력용 마크다운 ──────────────────────────────────────────────
    def to_markdown(self, path: Path, segments: list[Segment],
                    plans: dict[str, list[Replacement]]) -> str:
        lines: list[str] = []
        row_key: tuple[str, int] | None = None
        row_col: int | None = None
        row_cells: list[str] = []
        current_part: str | None = None

        def flush() -> None:
            nonlocal row_cells, row_key, row_col
            if row_cells:
                lines.append("| " + " | ".join(row_cells) + " |")
            row_cells, row_key, row_col = [], None, None

        for seg in segments:
            if seg.part.endswith(FALLBACK_MARK):
                continue      # 글상자는 두 벌이지만 읽는 사람에게는 한 벌이면 된다
            if seg.part != current_part:
                flush()
                if current_part is not None:
                    lines.append("")
                label = PART_LABEL.get(seg.kind, "")
                lines.append(f"<!-- {seg.part}{f' ({label})' if label else ''} -->")
                current_part = seg.part

            text = seg.text
            seg_plans = plans.get(seg.seg_id)
            if seg_plans:
                text = apply_to_text(text, seg_plans)

            ctx = seg.table_ctx
            if ctx is None:
                flush()
                lines.append(text)
                continue

            key = (ctx.table_id, ctx.row)
            if key != row_key:
                flush()
                row_key = key
            cell = text.replace("\n", " ").replace("|", r"\|").strip()
            if ctx.col == row_col and row_cells:
                row_cells[-1] = f"{row_cells[-1]} {cell}".strip()   # 한 셀의 둘째 문단
            else:
                row_cells.append(cell)
                row_col = ctx.col
        flush()
        return "\n".join(lines)

    # ── 치환 ────────────────────────────────────────────────────────────
    def apply(self, path: Path, out_path: Path,
              plans: dict[str, list[Replacement]],
              segments: list[Segment],
              mapping: Mapping | None = None) -> list[DocumentIssue]:
        issues: list[DocumentIssue] = []

        with docx_extract.open_package(path) as zf:
            names = set(zf.namelist())
            # 세그먼트를 같은 순서로 다시 만들어 seg_id 로 계획을 붙인다
            # (추출 때의 노드는 다른 트리에 속해 있어 그대로 쓸 수 없다)
            parts = docx_extract.load_parts(zf)

            # inspect 가 먼저 걸러 주지만, 이 함수만 따로 불릴 수도 있으므로 다시 본다
            revised = docx_extract.revision_parts(parts)
            if revised:
                return [_revision_issue(revised)]

            for part in parts:
                count, touched = apply_to_segments(part.segments, plans)
                if count:
                    _mark_preserve(part, touched)
                    part.dirty = True
                if _strip_comment_marks(part):
                    part.dirty = True
                # 도형 이름·대체 텍스트 같은 속성에도 본문이 복사돼 들어간다
                if mapping is not None and sweep_attributes(part.tree.getroot(), mapping):
                    part.dirty = True
                if next(part.tree.getroot().iter(W_ALTCHUNK), None) is not None:
                    issues.append(DocumentIssue(
                        level="warn", code="altchunk",
                        message="포함 문서(altChunk)는 치환 경로가 닿지 않습니다. "
                                "워드에서 본문으로 변환한 뒤 다시 실행하십시오.",
                        where=part.name,
                    ))

            pairs = _value_pairs(plans, mapping)
            dropped, reasons = _drop_targets(names)
            modified: dict[str, bytes] = {
                part.name: part.serialize() for part in parts if part.dirty
            }

            for member, data in (
                ("[Content_Types].xml", _rewrite_content_types(zf, dropped)),
                ("docProps/core.xml", _scrub_core(zf, pairs)),
                ("docProps/app.xml", _scrub_app(zf, pairs)),
                ("word/settings.xml", _scrub_settings(zf)),
            ):
                if data is not None and member not in dropped:
                    modified[member] = data

            for member in sorted(n for n in names if n.endswith(".rels")):
                if member in dropped:
                    continue
                data = _rewrite_rels(zf, member, dropped, pairs)
                if data is not None:
                    modified[member] = data

            if reasons:
                issues.append(DocumentIssue(
                    level="info", code="stripped",
                    message="제거한 멤버: " + ", ".join(sorted(dropped)),
                    where=", ".join(reasons),
                ))
            embedded = sorted(n for n in names
                              if n.startswith(("word/embeddings/", "word/charts/")))
            if embedded:
                issues.append(DocumentIssue(
                    level="warn", code="embedded",
                    message="포함 개체·차트 내부는 검사하지 못했습니다. 원본에서 "
                            "직접 확인하십시오.",
                    where=f"{len(embedded)}개",
                ))
            media = sorted(n for n in names if n.startswith("word/media/"))
            if media:
                issues.append(DocumentIssue(
                    level="warn", code="media",
                    message="이미지 안의 글자(스캔·캡처)는 검사 대상이 아닙니다.",
                    where=f"{len(media)}개",
                ))

            _repack(zf, out_path, modified, dropped)

        return issues
