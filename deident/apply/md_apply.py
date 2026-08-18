"""마크다운·평문 처리기.

세그먼트가 표 셀로 쪼개져 있어도 산출은 원래 줄로 되돌린다. 각 조각이
자기 줄과 줄 안 위치를 들고 있으므로, 줄 단위로 모아 뒤에서 앞으로 적용하면
원본 줄 모양이 그대로 보존된다.
"""

from __future__ import annotations

from pathlib import Path

from ..extract import md as md_extract
from ..model import DocumentIssue, Replacement, Segment


def reassemble_lines(source_text: str, segments: list[Segment],
                     plans: dict[str, list[Replacement]]) -> str:
    """세그먼트 오프셋을 줄 오프셋으로 되돌려 원문 줄에 적용한다."""
    lines = source_text.split("\n")
    per_line: dict[int, list[tuple[int, int, str]]] = {}

    for seg in segments:
        for plan in plans.get(seg.seg_id, []):
            for piece in seg.pieces:
                if not isinstance(piece.node, int):
                    continue
                if plan.end <= piece.start or plan.start >= piece.end:
                    continue
                start = piece.node_offset + (plan.start - piece.start)
                end = piece.node_offset + (plan.end - piece.start)
                per_line.setdefault(piece.node, []).append((start, end, plan.replacement))
                break

    for line_no, edits in per_line.items():
        if line_no >= len(lines):
            continue
        text = lines[line_no]
        for start, end, replacement in sorted(edits, key=lambda e: e[0], reverse=True):
            text = text[:start] + replacement + text[end:]
        lines[line_no] = text

    return "\n".join(lines)


class MarkdownHandler:
    name = "text"
    # 글자만 든 파일은 전부 여기서 받는다. 경보(guard)가 '검사 불가'로 넘기는
    # 파일이 많을수록 사람이 확인해야 할 몫이 늘어나므로, 읽을 수 있는 형식은
    # 최대한 읽는다.
    suffixes = (".md", ".txt", ".markdown", ".html", ".htm", ".json", ".jsonl",
                ".yaml", ".yml", ".log", ".tex", ".rst", ".srt", ".vtt")
    preserves_layout = True    # 줄 모양을 그대로 지켜 원래 확장자로 되돌려 준다

    def extract(self, path: Path) -> list[Segment]:
        return md_extract.extract(path)

    def to_markdown(self, path: Path, segments: list[Segment],
                    plans: dict[str, list[Replacement]]) -> str:
        return reassemble_lines(path.read_text(encoding="utf-8"), segments, plans)

    def apply(self, path: Path, out_path: Path,
              plans: dict[str, list[Replacement]],
              segments: list[Segment]) -> list[DocumentIssue]:
        out_path.write_text(self.to_markdown(path, segments, plans), encoding="utf-8")
        return []
