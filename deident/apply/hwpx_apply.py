"""hwpx 치환·위생·재포장.

치환 자체는 공용 엔진(xml_apply)에 맡기고, 이 모듈은 hwpx 라서 필요한 세 가지를
책임진다.

  ① 조판 캐시 — 글자가 바뀐 문단의 hp:linesegarray(줄 배치 기록)를 지운다.
     남겨 두면 한글이 옛 줄 길이로 그리려다 글자가 겹치거나 잘려 보인다.
  ② 유출 채널 — 본문만 고치면 원문이 세 군데에 그대로 남는다.
     Preview/PrvText.txt(평문 미리보기)·Preview/PrvImage.png(첫 쪽 그림)·
     DocHistory/(과거 판 전체). 여기에 작성자 메타데이터와 BinData 파일명까지
     본문 치환으로는 절대 지워지지 않는 통로다.
  ③ 재포장 — mimetype 은 선두·무압축(OCF 규약). 손대지 않은 멤버는 원본
     바이트를 그대로 옮긴다. 그래야 '치환 0건이면 산출물도 그대로'가 성립하고,
     서명·압축 방식 차이로 생기는 잡음이 사라진다.
"""

from __future__ import annotations

import os
import re
import zipfile
from pathlib import Path

from ..extract import hwpx as hwpx_extract
from ..extract.hwpx import (
    TAG_LINESEG, TAG_P, HwpxDoc, is_body_part, parse_document,
)
from ..model import DocumentIssue, Replacement, Segment
from .planner import apply_to_text
from .sweep import sweep_attributes
from .xml_apply import apply_to_segments

# ── 유출 채널 ───────────────────────────────────────────────────────────────
PREVIEW_TEXT = "Preview/PrvText.txt"
HISTORY_PREFIX = "dochistory/"
PREVIEW_IMAGE_SUFFIX = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".emf", ".wmf")

# 값이 곧 신원인 메타데이터 (opf:meta@name / config-item@name)
META_IDENTITY = frozenset({
    "creator", "author", "lastsaveby", "lastsavedby", "lastprintedby", "subject",
    "description", "keyword", "keywords", "company", "manager", "owner", "user",
    "username", "useraccount",
})
# 어느 요소에 붙어 있어도 신원인 속성 이름
IDENTITY_ATTRS = frozenset({
    "author", "creator", "lastsaveby", "lastsavedby", "company", "writer",
    "username", "useraccount", "owner",
})
# (요소 이름, 속성 이름) 이 맞아야 신원인 것 — 'name' 처럼 흔한 속성은 여기로
IDENTITY_FIELDS = frozenset({
    ("trackchangeauthor", "name"),
    ("memo", "author"),
})

SAFE_BIN_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


# ── ① 조판 캐시 ─────────────────────────────────────────────────────────────
def _drop_line_cache(doc: HwpxDoc, touched: set[int]) -> None:
    """글자가 바뀐 문단(과 그 문단을 품은 바깥 문단)의 줄 배치 기록을 지운다."""
    for seg in doc.segments:
        if seg.order not in touched:
            continue
        member = doc.part_of.get(seg.seg_id)
        if member in doc.parts:
            doc.parts[member].dirty = True
        node = doc.container.get(seg.seg_id)
        if node is None:
            continue
        if node.tag != TAG_P:
            node = hwpx_extract._nearest(node, TAG_P)
        while node is not None:
            for cache in node.findall(TAG_LINESEG):
                node.remove(cache)
            node = hwpx_extract._nearest(node, TAG_P)


# ── ② 유출 채널 ─────────────────────────────────────────────────────────────
def _clear_preview(doc: HwpxDoc, replaced: dict[str, bytes],
                   issues: list[DocumentIssue]) -> None:
    """평문 미리보기를 비운다.

    비식별 본문으로 다시 써 넣어 보기도 했지만 그건 본문이 아직 들고 있는 것을
    한 벌 더 만드는 일이라 안전을 조금도 보태지 못한다. 인코딩(UTF-8/UTF-16LE)도
    문서마다 달라 되쓰기는 실패 여지만 남긴다. 비우는 쪽이 통로를 아예 없앤다 —
    한글이 다음 저장 때 다시 만들어 준다.
    """
    if not doc.package.has(PREVIEW_TEXT) or not doc.package.raw[PREVIEW_TEXT]:
        return
    replaced[PREVIEW_TEXT] = b""
    issues.append(DocumentIssue(
        level="info", code="hwpx.preview_cleared",
        message="평문 미리보기(Preview/PrvText.txt)를 비웠습니다 (원문 캐시 제거).",
        where=PREVIEW_TEXT,
    ))


def _drop_members(doc: HwpxDoc, issues: list[DocumentIssue]) -> set[str]:
    """미리보기 그림·문서 이력처럼 원문이 통째로 남는 멤버를 뺀다."""
    dropped: set[str] = set()
    history = 0
    for name in doc.package.names:
        low = name.lower()
        if low.startswith(HISTORY_PREFIX):
            dropped.add(name)
            history += 1
        elif low.startswith("preview/") and low.endswith(PREVIEW_IMAGE_SUFFIX):
            dropped.add(name)
            issues.append(DocumentIssue(
                level="info", code="hwpx.preview_image_removed",
                message="첫 쪽 미리보기 그림을 제거했습니다 (원문 이미지가 남는 통로).",
                where=name,
            ))
    if history:
        issues.append(DocumentIssue(
            level="info", code="hwpx.history_removed",
            message=f"문서 이력(DocHistory) {history}개 멤버를 제거했습니다 (과거 판 원문).",
            where="DocHistory/",
        ))
    return dropped


def _scrub_metadata(doc: HwpxDoc, issues: list[DocumentIssue]) -> None:
    """작성자·회사 등 신원 메타데이터를 소거한다 (치환이 아니라 삭제)."""
    cleared = 0
    authors = 0
    for member, part in doc.parts.items():
        changed = False
        for el in part.tree.getroot().iter():
            if not isinstance(el.tag, str):
                continue
            tag = _localname(el.tag).lower()

            # opf:meta name="creator" content="..." / config-item name="LastSaveBy"
            if tag in ("meta", "config-item"):
                field = (el.get("name") or "").strip().lower()
                if field in META_IDENTITY:
                    if (el.get("content") or "").strip():
                        el.set("content", "")
                        changed = True
                        cleared += 1
                    if el.text and el.text.strip():
                        el.text = ""
                        changed = True
                        cleared += 1
                    continue

            # dc:creator 등 요소 자체가 신원인 경우
            if tag in ("creator", "lastsaveby", "publisher") and el.text and el.text.strip():
                el.text = ""
                changed = True
                cleared += 1

            for attr in list(el.attrib):
                aname = _localname(attr).lower()
                if aname in IDENTITY_ATTRS or (tag, aname) in IDENTITY_FIELDS:
                    if not (el.get(attr) or "").strip():
                        continue
                    if (tag, aname) in IDENTITY_FIELDS and aname == "name":
                        authors += 1
                        el.set(attr, f"익명{authors}")   # 이름 참조가 있어 비우지 않는다
                    else:
                        el.set(attr, "")
                    changed = True
                    cleared += 1
        if changed:
            part.dirty = True
    if cleared:
        issues.append(DocumentIssue(
            level="info", code="hwpx.meta_cleared",
            message=f"작성자·회사 등 문서 메타데이터 {cleared}건을 소거했습니다.",
            where="Contents/content.hpf 외",
        ))


def _rename_bindata(doc: HwpxDoc, issues: list[DocumentIssue]) -> dict[str, str]:
    """파일명 자체가 개인정보인 첨부(BinData) 를 익명 이름으로 바꾼다.

    참조를 한 곳이라도 못 고치면 그림이 깨진다. 그래서 고칠 수 없는 멤버가
    이름을 참조하면 이름을 그대로 두고 경고로 올린다 (게이트가 최종 판정).
    """
    renamed: dict[str, str] = {}
    editable = set(doc.parts)
    seq = 0

    for name in doc.package.names:
        if not name.lower().startswith("bindata/"):
            continue
        base = name.split("/", 1)[1]
        if SAFE_BIN_NAME.match(base):
            continue
        stem, dot, ext = base.rpartition(".")
        seq += 1
        new_base = f"bin{seq}{dot}{ext}" if dot else f"bin{seq}"
        new_name = f"BinData/{new_base}"

        # 이름을 참조하는 멤버 가운데 우리가 못 고치는 것이 있는지 확인
        blockers = []
        for other in doc.package.names:
            if other == name or other in editable:
                continue
            blob = doc.package.raw[other]
            if any(needle in blob for needle in _name_needles(base)):
                blockers.append(other)
        if blockers:
            issues.append(DocumentIssue(
                level="warn", code="hwpx.bindata_name_kept",
                message=("첨부 파일명이 개인정보로 보이나 참조를 고칠 수 없는 멤버가 있어 "
                         "이름을 유지했습니다. 수동 확인이 필요합니다."),
                where=f"{name} ← {blockers[0]}",
            ))
            continue

        renamed[name] = new_name
        _rewrite_references(doc, base, new_base, name, new_name)
        issues.append(DocumentIssue(
            level="info", code="hwpx.bindata_renamed",
            message="첨부 파일명을 익명 이름으로 바꾸고 참조를 함께 고쳤습니다.",
            where=new_name,
        ))
    return renamed


def _name_needles(base: str) -> list[bytes]:
    out = []
    for encoding in ("utf-8", "utf-16-le", "cp949"):
        try:
            out.append(base.encode(encoding))
        except UnicodeEncodeError:
            continue
    return out


def _rewrite_references(doc: HwpxDoc, old_base: str, new_base: str,
                        old_path: str, new_path: str) -> None:
    """매니페스트·content.hpf·본문의 파일명 참조를 한꺼번에 고친다."""
    stem_old = old_base.rpartition(".")[0] or old_base
    stem_new = new_base.rpartition(".")[0] or new_base
    ids: dict[str, str] = {}

    for part in doc.parts.values():
        changed = False
        for el in part.tree.getroot().iter():
            if not isinstance(el.tag, str):
                continue
            for attr in list(el.attrib):
                value = el.get(attr) or ""
                if not value:
                    continue
                if value in (old_path, old_base):
                    el.set(attr, new_path if value == old_path else new_base)
                    changed = True
                elif old_path in value:
                    el.set(attr, value.replace(old_path, new_path))
                    changed = True
                elif old_base in value and _localname(attr).lower() in ("href", "full-path", "src"):
                    el.set(attr, value.replace(old_base, new_base))
                    changed = True
            # 항목 id 가 파일명에서 온 경우(=id 자체가 개인정보) 함께 바꾼다
            if _localname(el.tag).lower() == "item":
                item_id = el.get("id") or ""
                if item_id and item_id == stem_old:
                    el.set("id", stem_new)
                    ids[item_id] = stem_new
                    changed = True
            if el.text and old_base in el.text:
                el.text = el.text.replace(old_base, new_base)
                changed = True
        if changed:
            part.dirty = True

    if not ids:
        return
    for part in doc.parts.values():
        changed = False
        for el in part.tree.getroot().iter():
            if not isinstance(el.tag, str):
                continue
            for attr in list(el.attrib):
                if _localname(attr).lower() not in ("binaryitemidref", "idref"):
                    continue
                value = el.get(attr) or ""
                if value in ids:
                    el.set(attr, ids[value])
                    changed = True
        if changed:
            part.dirty = True


def _drop_manifest_entries(doc: HwpxDoc, dropped: set[str]) -> None:
    """제거한 멤버를 가리키는 매니페스트 항목도 함께 지운다."""
    if not dropped:
        return
    targets = {name for name in dropped}
    for part in doc.parts.values():
        changed = False
        for el in list(part.tree.getroot().iter()):
            if not isinstance(el.tag, str):
                continue
            values = {(el.get(a) or "") for a in el.attrib}
            hit = values & targets
            if not hit:
                continue
            parent = el.getparent()
            if parent is not None:
                parent.remove(el)
                changed = True
        if changed:
            part.dirty = True


# ── ③ 재포장 ────────────────────────────────────────────────────────────────
def write_package(doc: HwpxDoc, out_path: Path, *, dropped: set[str],
                  renamed: dict[str, str], replaced: dict[str, bytes]) -> None:
    """mimetype 선두·무압축, 나머지는 원본 순서·압축 방식 그대로."""
    names = [n for n in doc.package.names if n not in dropped]
    if "mimetype" in names:
        names.remove("mimetype")
        names.insert(0, "mimetype")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    with zipfile.ZipFile(tmp, "w") as zf:
        for name in names:
            if name in replaced:
                data = replaced[name]
            else:
                part = doc.parts.get(name)
                data = part.serialize() if (part is not None and part.dirty) \
                    else doc.package.raw[name]
            info = doc.package.infos.get(name)
            entry = zipfile.ZipInfo(renamed.get(name, name),
                                    date_time=info.date_time if info else (1980, 1, 1, 0, 0, 0))
            if name == "mimetype":
                entry.compress_type = zipfile.ZIP_STORED
            else:
                entry.compress_type = info.compress_type if info else zipfile.ZIP_DEFLATED
            if info is not None:
                entry.external_attr = info.external_attr
                entry.internal_attr = info.internal_attr
                entry.create_system = info.create_system
            zf.writestr(entry, data)
    os.replace(tmp, out_path)


# ── 마크다운 ────────────────────────────────────────────────────────────────
def _cell(text: str) -> str:
    return " ".join(text.replace("|", "｜").split())


def build_markdown(segments: list[Segment],
                   plans: dict[str, list[Replacement]]) -> str:
    """AI 입력용 본문 — 표는 마크다운 행으로 되돌린다."""
    lines: list[str] = []
    table: dict[int, dict[int, str]] = {}
    table_id: str | None = None

    def flush() -> None:
        nonlocal table_id
        if table:
            width = max((max(cols) + 1) for cols in table.values() if cols) if table else 0
            for rank, row in enumerate(sorted(table)):
                cells = [table[row].get(c, "") for c in range(width)]
                lines.append("| " + " | ".join(cells) + " |")
                if rank == 0:
                    lines.append("|" + "|".join([" --- "] * width) + "|")
            lines.append("")
        table.clear()
        table_id = None

    for seg in segments:
        if not is_body_part(seg.part):
            continue
        text = seg.text
        seg_plans = plans.get(seg.seg_id)
        if seg_plans:
            text = apply_to_text(text, seg_plans)
        ctx = seg.table_ctx
        if ctx is None:
            flush()
            lines.append(text.replace("\t", " "))
            continue
        if ctx.table_id != table_id:
            flush()
            table_id = ctx.table_id
        row = table.setdefault(ctx.row, {})
        prior = row.get(ctx.col)
        row[ctx.col] = f"{prior} {_cell(text)}".strip() if prior else _cell(text)
    flush()
    return "\n".join(lines)


# ── 핸들러 ──────────────────────────────────────────────────────────────────
class HwpxHandler:
    name = "hwpx"
    suffixes = (".hwpx",)
    preserves_layout = True

    def extract(self, path: Path) -> list[Segment]:
        return parse_document(path).segments

    def to_markdown(self, path: Path, segments: list[Segment],
                    plans: dict[str, list[Replacement]]) -> str:
        return build_markdown(segments, plans)

    def apply(self, path: Path, out_path: Path,
              plans: dict[str, list[Replacement]],
              segments: list[Segment], mapping=None) -> list[DocumentIssue]:
        # 넘겨받은 세그먼트는 다른 트리의 노드를 들고 있을 수 있어 그대로 쓰지
        # 않는다. 같은 파일을 다시 파싱하면 seg_id 가 같게 나오므로(결정적)
        # 계획만 옮겨 붙인다.
        doc = parse_document(path)
        issues: list[DocumentIssue] = []

        for member, reason in doc.parse_errors:
            issues.append(DocumentIssue(
                level="block", code="hwpx.parse_failed",
                message=f"XML 멤버를 열지 못해 비식별 여부를 보증할 수 없습니다 ({reason}).",
                where=member,
            ))

        # 세그먼트가 덮지 않은 글자가 하나라도 있으면 그만큼은 탐지조차 되지
        # 않았다는 뜻이다. 산출물을 내보내지 않는다 (fail-closed).
        uncovered = hwpx_extract.uncovered_text_nodes(doc)
        if uncovered:
            spots = ", ".join(sorted({f"{m}:{tag}" for m, tag in uncovered})[:3])
            issues.append(DocumentIssue(
                level="block", code="hwpx.uncovered_text",
                message=(f"세그먼트가 덮지 않은 텍스트 노드 {len(uncovered)}곳이 있습니다 "
                         "— 그 부분은 탐지·치환을 거치지 않았습니다."),
                where=spots,
            ))

        known = {seg.seg_id for seg in doc.segments}
        missing = [sid for sid in plans if sid not in known]
        if missing:
            issues.append(DocumentIssue(
                level="block", code="hwpx.segment_mismatch",
                message=(f"치환 계획 {len(missing)}건의 대상 세그먼트를 다시 찾지 못했습니다 "
                         "(추출이 결정적이지 않음)."),
                where=missing[0],
            ))

        applied, touched = apply_to_segments(doc.segments, plans)
        planned = sum(len(v) for v in plans.values())
        if applied < planned:
            issues.append(DocumentIssue(
                level="warn", code="hwpx.partial_apply",
                message=f"치환 계획 {planned}건 중 {applied}건만 노드에 반영됐습니다.",
                where=str(path.name),
            ))

        # 세그먼트는 텍스트 노드만 덮는다. 도형 이름·설명·연결 주소 같은 XML
        # 속성에도 본문이 복사돼 들어가는데, 화면에 보이지 않아 눈으로 못 찾는다.
        if mapping is not None:
            for member, part in doc.parts.items():
                if sweep_attributes(part.tree.getroot(), mapping):
                    part.dirty = True

        _drop_line_cache(doc, touched)

        dropped = _drop_members(doc, issues)
        replaced: dict[str, bytes] = {}
        _scrub_metadata(doc, issues)
        renamed = _rename_bindata(doc, issues)
        _drop_manifest_entries(doc, dropped)
        _clear_preview(doc, replaced, issues)

        write_package(doc, out_path, dropped=dropped, renamed=renamed, replaced=replaced)
        return issues
