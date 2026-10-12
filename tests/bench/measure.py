"""탐지 실측 — 정답을 아는 가상 문서로 누락·오탐을 잰다.

    python3 tests/bench/measure.py                # 세 묶음 전부, 규칙만
    python3 tests/bench/measure.py dev            # 한 묶음만
    python3 tests/bench/measure.py holdout --llm  # 로컬 LLM까지 (ollama 필요, 느림)
    python3 tests/bench/measure.py dev -v         # 누락·오탐 목록 출력

묶음 셋의 쓰임이 다르다.
  dev      규칙을 고치며 본 문서. 여기 성적은 규칙이 이 문서에 맞춰진 만큼 좋게 나온다.
  check    규칙을 모르는 채 쓰인 문서. 1차 측정 뒤 여기서 드러난 누락도 규칙에 반영했다.
  holdout  규칙을 모르는 채 쓰인 문서. 규칙을 고치는 데 쓰지 않았다 — 믿을 수치는 이것이다.
규칙을 고칠 때 holdout 의 누락 목록을 보고 고치면 그 순간부터 holdout 이 아니다.

판정은 도구의 게이트에 맡기지 않는다. 치환을 끝낸 본문에 정답 문자열이 온전히 남아
있는지를 직접 본다. 문서는 전부 허구다.
"""

from __future__ import annotations

import importlib
import shutil
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1]))

from deident import formats, pipeline  # noqa: E402,F401
from deident.config import default_config  # noqa: E402
from deident.detect import l3_ner  # noqa: E402
from deident.entity.pseudonym import Mapping  # noqa: E402

SETS = {"dev": "corpus_dev", "check": "corpus_check", "holdout": "corpus_holdout"}


def run_document(name: str, text: str, llm=None):
    """문서 하나를 apply 로 돌려 (치환된 본문, 탐지 목록, 게이트 통과 여부)를 낸다."""
    work = Path(tempfile.mkdtemp(prefix="deident-bench-"))
    # 문서마다 형태소 분석기를 새로 시작한다 — 문서 순서가 수치를 바꾸지 않게.
    l3_ner._kiwi, l3_ner._kiwi_words = None, frozenset()
    try:
        config = default_config(work)
        config.workspace.prepare()
        source = work / f"{name}.md"
        source.write_text(text, encoding="utf-8")
        kwargs = {"llm": llm} if llm is not None else {}
        result = pipeline.apply_file(source, config, Mapping(), **kwargs)
        output = text
        for folder in (config.workspace.out, config.workspace.failed):
            path = folder / f"{name}.비식별.md"
            if path.exists():
                output = path.read_text(encoding="utf-8")
                break
        return output, result
    finally:
        shutil.rmtree(work, ignore_errors=True)


def score(module_name: str, llm=None, verbose: bool = False) -> dict:
    corpus = importlib.import_module(module_name)
    total = {"gold": 0, "missed": 0, "false": 0, "docs": 0, "passed": 0, "by_class": {}}
    for name, text in corpus.DOCS.items():
        gold = corpus.GOLD[name]
        gold_texts = [g for _, g in gold]
        output, result = run_document(name, text, llm)
        missed = [(cls, g) for cls, g in gold if g in output]
        # 검토 등급은 치환하지 않으므로 오탐으로 세지 않는다 (문서를 바꾸지 않는다)
        false = sorted({f"{f.text} ({f.category}, {f.note})" for f in result.findings
                        if f.grade != "검토"
                        and not any(f.text.strip() in g or g.strip() in f.text
                                    for g in gold_texts)})
        total["docs"] += 1
        total["passed"] += bool(result.outputs)
        total["gold"] += len(gold)
        total["missed"] += len(missed)
        total["false"] += len(false)
        for cls, _ in gold:
            bucket = total["by_class"].setdefault(cls, [0, 0])
            bucket[0] += 1
        for cls, _ in missed:
            total["by_class"][cls][1] += 1
        if verbose:
            print(f"  ■ {name}: 누락 {len(missed)}/{len(gold)}  오탐 {len(false)}"
                  f"  게이트 {'통과' if result.outputs else '불합격'}")
            if missed:
                print(f"     누락: {missed}")
            if false:
                print(f"     오탐: {false}")
    return total


def main(argv: list[str]) -> int:
    names = [a for a in argv if a in SETS] or list(SETS)
    verbose = "-v" in argv
    llm = None
    if "--llm" in argv:
        from deident.detect import l4_llm
        llm = l4_llm.LLMSettings(replace_orgs="--llm-orgs" in argv)
        if "--llm-model" in argv:
            llm.model = argv[argv.index("--llm-model") + 1]
        l4_llm.preflight(llm)

    for name in names:
        started = time.time()
        try:
            total = score(SETS[name], llm, verbose)
        except ModuleNotFoundError:
            print(f"[{name}] 문서 묶음이 없습니다 — 건너뜁니다.")
            continue
        caught = total["gold"] - total["missed"]
        rate = caught / total["gold"] if total["gold"] else 1.0
        classes = "  ".join(f"{cls} {n - m}/{n}" for cls, (n, m) in sorted(total["by_class"].items()))
        print(f"[{name}] 정답 {total['gold']}건 중 {caught}건 제거 ({rate:.0%}) · "
              f"누락 {total['missed']} · 오탐 {total['false']} · "
              f"게이트 {total['passed']}/{total['docs']} · {time.time() - started:.0f}초")
        print(f"        구분별 제거: {classes}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
