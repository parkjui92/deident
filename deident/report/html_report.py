"""검토 리포트 2종.

  out/report.html        마스킹판 — 건수·위치·등급만. 공유·LLM 열람 안전.
  _private/report_full.html  원문 하이라이트판 — 오탐 검토는 이것으로만 가능하다.
                         (홍*동만 보고서는 홍길동인지 홍갑동인지 가릴 수 없다)

두 파일 모두 외부 자원을 참조하지 않는 자기완결 HTML이다.
"""

from __future__ import annotations

import html
import os
from collections import Counter
from datetime import datetime
from pathlib import Path

from ..config import Config
from ..entity.pseudonym import Mapping
from ..model import CATEGORY_LABEL, GRADE_CERTAIN, Finding, Segment
from .mask import mask_value

_CSS = """
:root { color-scheme: light dark; --bg:#fff; --fg:#1a1a1a; --muted:#666;
  --line:#e0e0e0; --certain:#c62828; --likely:#ef6c00; --chip:#f4f4f5; }
@media (prefers-color-scheme: dark) { :root { --bg:#16181c; --fg:#e8e8e8;
  --muted:#9aa0a6; --line:#33363b; --chip:#23262b; } }
* { box-sizing:border-box; }
body { margin:0; padding:2rem 1.5rem 4rem; background:var(--bg); color:var(--fg);
  font:15px/1.7 -apple-system,'Apple SD Gothic Neo','Noto Sans KR',sans-serif; }
.wrap { max-width:1000px; margin:0 auto; }
h1 { font-size:1.5rem; margin:0 0 .3rem; }
h2 { font-size:1.1rem; margin:2.2rem 0 .8rem; padding-bottom:.3rem;
  border-bottom:1px solid var(--line); }
.meta { color:var(--muted); font-size:.85rem; margin-bottom:1.5rem; }
.cards { display:flex; flex-wrap:wrap; gap:.6rem; margin:1rem 0; }
.card { background:var(--chip); border-radius:8px; padding:.6rem .9rem; min-width:120px; }
.card b { display:block; font-size:1.3rem; }
.card span { color:var(--muted); font-size:.8rem; }
table { border-collapse:collapse; width:100%; font-size:.9rem; }
th,td { text-align:left; padding:.45rem .6rem; border-bottom:1px solid var(--line);
  vertical-align:top; }
th { color:var(--muted); font-weight:600; font-size:.8rem; }
.tablewrap { overflow-x:auto; }
code { background:var(--chip); padding:.1rem .35rem; border-radius:4px;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:.85em; }
mark { background:rgba(198,40,40,.18); color:inherit; border-bottom:2px solid var(--certain);
  padding:0 .1em; border-radius:2px; }
mark.likely { background:rgba(239,108,0,.16); border-bottom-color:var(--likely); }
.g-확실 { color:var(--certain); font-weight:600; }
.g-추정 { color:var(--likely); font-weight:600; }
.note { background:var(--chip); border-left:3px solid var(--muted); padding:.8rem 1rem;
  border-radius:0 6px 6px 0; margin:1rem 0; font-size:.9rem; }
.fail { border-left-color:var(--certain); }
.seg { padding:.5rem .7rem; border-bottom:1px solid var(--line); }
.seg .sid { color:var(--muted); font-size:.75rem; font-family:ui-monospace,monospace; }
"""


def _grade_class(finding: Finding) -> str:
    return "" if finding.grade == GRADE_CERTAIN else "likely"


def _counts_table(findings: list[Finding]) -> str:
    counter: Counter[tuple[str, str]] = Counter((f.category, f.grade) for f in findings)
    categories = sorted({cat for cat, _ in counter})
    rows = []
    for cat in categories:
        certain = counter.get((cat, GRADE_CERTAIN), 0)
        likely = sum(v for (c, g), v in counter.items() if c == cat and g != GRADE_CERTAIN)
        rows.append(
            f"<tr><td>{html.escape(CATEGORY_LABEL.get(cat, cat))}</td>"
            f"<td class='g-확실'>{certain}</td><td class='g-추정'>{likely}</td>"
            f"<td>{certain + likely}</td></tr>"
        )
    if not rows:
        return "<p>탐지된 항목이 없습니다.</p>"
    return ("<div class='tablewrap'><table><tr><th>구분</th><th>확실</th><th>추정</th>"
            "<th>합계</th></tr>" + "".join(rows) + "</table></div>")


def _gate_block(result) -> str:
    gate = getattr(result, "gate", None)
    if gate is None:
        return ""
    if gate.passed:
        return ("<div class='note'>검증 게이트 <b>통과</b> — 탐지된 값의 제거와 "
                "매핑 원값의 부재(바이트 수준)를 확인했습니다.</div>")
    reasons = "".join(f"<li>{html.escape(r)}</li>" for r in gate.summary_lines()[:30])
    return (f"<div class='note fail'>검증 게이트 <b>불합격</b> — 산출물은 "
            f"_private/failed/ 로 격리했고 out/ 에는 아무것도 남기지 않았습니다."
            f"<ul>{reasons}</ul></div>")


def _issue_block(result) -> str:
    issues = getattr(result, "issues", [])
    if not issues:
        return ""
    rows = "".join(
        f"<tr><td>{html.escape(i.level)}</td><td>{html.escape(i.code)}</td>"
        f"<td>{html.escape(i.message)}</td><td>{html.escape(i.where)}</td></tr>"
        for i in issues
    )
    return ("<h2>사람이 판단할 사항</h2><div class='tablewrap'><table>"
            "<tr><th>수준</th><th>코드</th><th>내용</th><th>위치</th></tr>"
            + rows + "</table></div>")


def _shell(title: str, source: str, body: str, warning: str = "") -> str:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    banner = f"<div class='note fail'>{warning}</div>" if warning else ""
    return (
        "<!doctype html><html lang='ko'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title><style>{_CSS}</style></head><body><div class='wrap'>"
        f"<h1>{html.escape(title)}</h1>"
        f"<div class='meta'>{html.escape(source)} · {stamp} · 전 과정 로컬 실행</div>"
        f"{banner}{body}</div></body></html>"
    )


def _masked_findings_table(findings: list[Finding]) -> str:
    rows = []
    for f in findings[:1000]:
        rows.append(
            f"<tr><td>{html.escape(CATEGORY_LABEL.get(f.category, f.category))}</td>"
            f"<td><code>{html.escape(mask_value(f.text, f.category))}</code></td>"
            f"<td class='g-{f.grade}'>{f.grade}</td>"
            f"<td>{html.escape(','.join(sorted(f.detectors)))}</td>"
            f"<td><code>{html.escape(f.part or '')}:{html.escape(f.seg_id)}</code></td></tr>"
        )
    if not rows:
        return ""
    more = ("<p class='meta'>1000건까지만 표시했습니다.</p>"
            if len(findings) > 1000 else "")
    return ("<h2>탐지 항목 (마스킹)</h2><div class='tablewrap'><table>"
            "<tr><th>구분</th><th>값(마스킹)</th><th>등급</th><th>탐지기</th><th>위치</th></tr>"
            + "".join(rows) + "</table></div>" + more)


def _highlight_segment(seg: Segment, findings: list[Finding]) -> str:
    """세그먼트 원문에 탐지 구간을 표시한다 (사람 전용 리포트에서만 호출)."""
    pieces, cursor = [], 0
    for f in sorted(findings, key=lambda x: x.start):
        if f.start < cursor:
            continue
        pieces.append(html.escape(seg.text[cursor:f.start]))
        cls = _grade_class(f)
        label = html.escape(CATEGORY_LABEL.get(f.category, f.category))
        pieces.append(
            f"<mark class='{cls}' title='{label} / {f.grade} / "
            f"{html.escape(','.join(sorted(f.detectors)))}'>"
            f"{html.escape(seg.text[f.start:f.end])}</mark>"
        )
        cursor = f.end
    pieces.append(html.escape(seg.text[cursor:]))
    return "".join(pieces)


def write_reports(result, config: Config, mapping: Mapping) -> tuple[Path, Path]:
    ws = config.workspace
    ws.prepare()
    findings = result.findings
    source = result.source.name

    total = len(findings)
    certain = sum(1 for f in findings if f.grade == GRADE_CERTAIN)
    cards = (
        "<div class='cards'>"
        f"<div class='card'><b>{total}</b><span>탐지 합계</span></div>"
        f"<div class='card'><b>{certain}</b><span>확실</span></div>"
        f"<div class='card'><b>{total - certain}</b><span>추정</span></div>"
        f"<div class='card'><b>{len(mapping.entries)}</b><span>가명 발급</span></div>"
        "</div>"
    )

    guide = (
        "<div class='note'>오탐(치우면 안 될 것을 치운 경우)은 "
        "<code>config/allowlist.txt</code> 에, 누락(더 치워야 할 것)은 "
        "<code>config/denylist.txt</code> 에 한 줄씩 적고 다시 실행하십시오. "
        "값 확인은 <code>_private/report_full.html</code> 에서만 할 수 있습니다.</div>"
    )

    masked_body = (cards + _gate_block(result) + "<h2>구분별 집계</h2>"
                   + _counts_table(findings) + _masked_findings_table(findings)
                   + _issue_block(result) + guide)
    public_path = ws.out / f"{result.source.stem}.report.html"
    public_path.write_text(
        _shell(f"비식별 검토 리포트 — {result.source.stem}", source, masked_body),
        encoding="utf-8")

    # ── 사람 전용 (원문 포함) ────────────────────────────────────────────
    by_segment: dict[str, list[Finding]] = {}
    for f in findings:
        by_segment.setdefault(f.seg_id, []).append(f)
    seg_index = {s.seg_id: s for s in result.segments}

    blocks = []
    for seg_id, group in list(by_segment.items())[:600]:
        seg = seg_index.get(seg_id)
        if seg is None:
            continue
        blocks.append(f"<div class='seg'><span class='sid'>{html.escape(seg.part)}"
                      f":{html.escape(seg_id)}</span><br>"
                      f"{_highlight_segment(seg, group)}</div>")

    full_body = (cards + "<h2>원문 대조 (탐지 구간 표시)</h2>"
                 + ("".join(blocks) or "<p>탐지된 항목이 없습니다.</p>")
                 + _issue_block(result) + guide)
    private_path = ws.private / f"{result.source.stem}.report_full.html"
    private_path.write_text(
        _shell(f"원문 대조 리포트 — {result.source.stem}", source, full_body,
               warning="이 파일은 원문(민감정보)을 포함합니다. _private 밖으로 옮기지 마십시오."),
        encoding="utf-8")
    try:
        os.chmod(private_path, 0o600)
    except OSError:
        pass

    return public_path, private_path
