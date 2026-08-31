"""黄金问题集回归。自动扩充链路的守门：库劣化了立刻有非零退出码。

    python scripts/regress.py                  # 跑 tests/ 下全部黄金问题集
    python scripts/regress.py --file tests/questions-real.txt
    python scripts/regress.py --json

两类问题必须一起跑，缺一类都能被糊弄过去：
  questions-real.txt       真实工厂问法，绝大多数期望 refuse。防误报。
  questions-type-level.txt 本体本来该答的问法，多数期望 answerable。防误拒。
只跑前者的话，把 ask.py 改成永远返回 3 就能满分。

退出码：0 全过 | 1 有失败
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ask  # noqa: E402
import common as C  # noqa: E402


def load(path: Path) -> list[tuple[str, str]]:
    out = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        if "\t" not in ln:
            print(f"  ! 跳过无 TAB 分隔的行：{ln[:40]}")
            continue
        exp, q = ln.split("\t", 1)
        out.append((exp.strip(), q.strip()))
    return out


def load_context() -> dict:
    """本体只加载一次。每题起一个 Python 进程重读一遍会把几秒的判定拖成
    几分钟：上百次冷启动 + 上百次 YAML 解析。

    只加载 cq 与 entities：regress 从不传 --probe，graph.json 与 kb 实例
    在这条路径上用不到，不必读。
    """
    cq = C.load_competency()
    entities, _ = C.load_entities()
    return {"cq": cq, "et": ask.entity_terms(entities)}


def run_one(question: str, ctx: dict) -> tuple[str, str]:
    """返回 (实际判定, 命中的 CQ ID)。

    判定必须与 ask.py 的退出码语义逐字一致，否则回归就在验一套自己发明的规则：
      实例级命中          -> 退出码 2 -> refuse
      命中 in_scope       -> 退出码 0 -> answerable
      命中 out_of_scope   -> 退出码 2 -> refuse
      分数不足/无候选     -> 退出码 3 -> refuse
    """
    inst = ask.instance_level(question)
    if inst:
        return "refuse", "实例级:" + "/".join(inst)

    cands = ask.rank(question, ctx["cq"], ctx["et"])
    top = cands[0] if cands and cands[0]["score"] >= ask.MIN_SCORE else None
    if not top:
        return "refuse", "-"
    verdict = "answerable" if top["bucket"] == "in_scope" else "refuse"
    return verdict, top["item"]["id"]


def main() -> int:
    C.setup_console()
    as_json = "--json" in sys.argv
    only = None
    if "--file" in sys.argv:
        only = Path(sys.argv[sys.argv.index("--file") + 1])

    files = [only] if only else sorted((C.ROOT / "tests").glob("questions-*.txt"))
    if not files:
        print("tests/ 下没有 questions-*.txt 黄金问题集")
        return 1

    ctx = load_context()
    report, failed = [], 0
    for f in files:
        cases = load(f)
        fails = []
        for exp, q in cases:
            got, tag = run_one(q, ctx)
            if got != exp:
                fails.append({"q": q, "expect": exp, "got": got, "matched": tag})
        failed += len(fails)
        report.append({"file": f.name, "total": len(cases),
                       "failed": len(fails), "cases": fails})

    if as_json:
        print(json.dumps({"failed": failed, "files": report},
                         ensure_ascii=False, indent=2))
        return 1 if failed else 0

    print("=" * 68)
    for r in report:
        ok = r["total"] - r["failed"]
        print(f"{r['file']}  {ok}/{r['total']} 通过")
        for c in r["cases"]:
            print(f"  期望 {c['expect']:10} 实得 {c['got']:10} [{c['matched']}]")
            print(f"       {c['q'][:60]}")
    print("-" * 68)
    if failed:
        print(f"回归失败 {failed} 条。")
        print("误报（期望 refuse 实得 answerable）优先修：那是把答不了的说成答得了。")
        return 1
    print("回归全过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
