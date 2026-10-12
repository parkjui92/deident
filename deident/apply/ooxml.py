"""OOXML(워드·엑셀·발표자료) 공용 손질 — 문서 속성 소거와 재포장."""

from __future__ import annotations

import zipfile
from pathlib import Path

from lxml import etree

from ..extract import hidden

META_MEMBERS = ("docProps/core.xml", "docProps/app.xml", "docProps/custom.xml")
# 값이 곧 사람·조직인 속성 — 내용을 볼 것 없이 비운다
IDENTITY_TAGS = frozenset({"creator", "lastModifiedBy", "Manager", "Company", "Template"})


def serialize(tree: etree._ElementTree) -> bytes:
    """원본 선언을 유지한 채 직렬화한다 (pretty_print 금지 — 공백 오염)."""
    return etree.tostring(tree, xml_declaration=True,
                          encoding=tree.docinfo.encoding or "UTF-8",
                          standalone=tree.docinfo.standalone)


def clear_identity(zf: zipfile.ZipFile, trees: dict[str, etree._ElementTree]) -> set[str]:
    """작성자·회사를 비운다. 고친 멤버 이름을 돌려준다.

    trees 에 이미 올라온 멤버는 그 트리를 고친다. 원본에서 다시 읽어 덮어쓰면
    방금 치환한 제목·주제가 원래대로 돌아간다.
    """
    names = set(zf.namelist())
    dirty: set[str] = set()
    for member in META_MEMBERS:
        if member not in names:
            continue
        tree = trees.get(member)
        if tree is None:
            tree = hidden.parse_bytes(zf.read(member))
            if tree is None:
                continue
        changed = False
        for node in tree.getroot().iter():
            if not isinstance(node.tag, str):
                continue
            if hidden.local_name(node.tag) in IDENTITY_TAGS and (node.text or "").strip():
                node.text = ""
                changed = True
        if changed:
            trees[member] = tree
            dirty.add(member)
    return dirty


def rewrite_zip(source: Path, target: Path, replaced: dict[str, bytes],
                drop: frozenset[str] | set[str] = frozenset()) -> None:
    """고친 멤버만 새로 쓰고 나머지는 원본 바이트 그대로 옮긴다."""
    with zipfile.ZipFile(source) as src, zipfile.ZipFile(
            target, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            if info.filename in drop:
                continue
            data = replaced.get(info.filename)
            if data is None:
                data = src.read(info)
            new_info = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            new_info.compress_type = info.compress_type
            new_info.external_attr = info.external_attr
            dst.writestr(new_info, data)
