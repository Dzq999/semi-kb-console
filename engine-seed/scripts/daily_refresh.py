"""语义提案的自动合并、验证、回滚与提交入口。

调用它的当前 Agent 或定时任务先按 prompts/kb-refresh.md 生成语义 JSON；脚本
不再硬编码任何模型供应商 CLI。任一段失败就回滚到开跑前的提交：

  1. 前置检查   工作区干净、脚本齐、基线自己就通过（kb.py check --quick）
  2. 快照      记下开跑前的 commit，这是回滚点
  3. 生成      调度本脚本的当前 Agent 按 kb-refresh 产出语义 JSON
  3b. 闸门     JSON Schema、来源与语义预检
  4. 落库      apply_semantic_changeset.py 原子合并 OWL/ABox
  5. 护栏      检查无意删除、来源与语义完整性；新增数量不设全局上限
  6. 验证      kb.py check 全链
  7. 提交      全过则 git commit；任一段失败则 git reset --hard 回到第 2 段的点

第 1 段与第 6 段都委托给 kb.py check，手动与无人值守使用同一条链。

回滚的已知边界：reset --hard 收不回未跟踪文件。新建在 ontology/ 下的文件
回滚后仍在库里，靠下一轮前置检查的 require_git_clean 拦住，不会静默累积
但需要人处置。详见 rollback() 的说明。

退出码：0 成功（含"无事可做"）| 1 失败已回滚 | 2 前置检查未过（未做任何改动）
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import yaml

import common as C

LOG_DIR = C.ROOT / "logs"
GIT = ["git", "-C", str(C.ROOT)]


def log(msg: str = "") -> None:
    line = f"[{datetime.now():%H:%M:%S}] {msg}" if msg else ""
    print(line, flush=True)
    LOG_DIR.mkdir(exist_ok=True)
    with (LOG_DIR / f"daily-{datetime.now():%Y%m%d}.log").open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def run(cmd: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=C.ROOT, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout,
                          env={**os.environ, "PYTHONIOENCODING": "utf-8"})


def py(script: str, *args: str, timeout: int = 600) -> subprocess.CompletedProcess:
    return run([sys.executable, str(C.ROOT / "scripts" / script), *args], timeout=timeout)


def counts() -> dict[str, int]:
    """当前规模。用 common 直接读，不依赖 build/ 是否最新。"""
    ents, _ = C.load_entities()
    return {"entities": len(ents), "relations": len(C.load_relations()),
            "kb": len(C.load_kb())}


# ---------- 1. 前置检查 ----------

def preflight(guards: dict) -> str | None:
    """开跑前的环境与基线检查。返回 None 表示通过，否则返回失败原因。

    名字刻意不叫 precheck：scripts/precheck.py 查的是提案形状，
    本函数查的是"这台机器现在能不能开跑"。两件事同名会让人以为是同一层。
    """
    r = run([*GIT, "rev-parse", "--is-inside-work-tree"])
    if r.returncode != 0:
        return ("semi-kb 不是 git 仓库。无人值守必须有回滚线——"
                "跑 `git init && git add -A && git commit` 先立基线。")

    if guards.get("require_git_clean", True):
        r = run([*GIT, "status", "--porcelain"])
        # 提案与日志不算脏：先生成提案再调用本脚本是正常顺序。
        dirty = [l for l in r.stdout.strip().splitlines()
                 if l.strip() and not any(
                     p in l for p in ("changesets/", "semantic_changesets/", "logs/", "build/"))]
        if dirty:
            return ("工作区有未提交的改动，没有干净的回滚点：\n    " +
                    "\n    ".join(dirty[:5]) +
                    "\n  先提交或撤销。自动化不该在别人没存盘的工作上叠改动。")

    for s in ("kb.py", "validate.py", "build_index.py", "regress.py",
              "apply_changeset.py", "apply_semantic_changeset.py", "ask.py", "precheck.py"):
        if not (C.ROOT / "scripts" / s).is_file():
            return f"缺少 scripts/{s}"
    if not list((C.ROOT / "tests").glob("questions-*.txt")):
        return "tests/ 下没有黄金问题集，回归守门形同虚设"

    # 基线必须自己就是干净的。在坏库上做自动扩充，事后分不清问题是本来就有
    # 还是这次加进去的——而分不清就意味着不敢回滚也不敢保留。
    # 用 --quick 跳过 build_index：此刻只需知道基线好不好，不需要重建索引，
    # 重建留给合并之后那次。
    r = py("kb.py", "check", "--quick", "--no-precheck", timeout=1800)
    if r.returncode != 0:
        return ("基线自己就没过，先修库再谈自动扩充：\n"
                + "\n".join("    " + l for l in
                            r.stdout.strip().splitlines()[-10:]))

    if not (C.ROOT / "prompts" / "kb-refresh.md").is_file():
        return "缺少 prompts/kb-refresh.md，生成步骤没有可执行的流程"
    return None


# ---------- 5. 护栏 ----------

def check_growth(before: dict, after: dict, guards: dict) -> str | None:
    d_ent = after["entities"] - before["entities"]
    log(f"净增：实体 {d_ent:+d}｜关系 {after['relations'] - before['relations']:+d}"
        f"｜kb {after['kb'] - before['kb']:+d}")
    if d_ent < 0 or after["relations"] < before["relations"]:
        return f"规模下降了（实体 {d_ent:+d}），自动扩充不该删东西"
    return None


# ---------- 5b. 提案确定性闸门 ----------

def gate_pending() -> str | None:
    """落库前对 pending 提案跑 precheck.py。约 2 秒，挡的是整轮白跑。

    位置在 apply_changeset.py 之前、任何内容合并之前，所以失败可以直接返回
    而不必回滚。这一层专治两类无人值守下最贵的失败：

      顶层键写错 —— apply_changeset 会静默跳过并以退出码 0 报成功，
                    日志只显示"自动放行 0 条"。定时任务看到 0 就以为成功了，
                    实际每天空跑。precheck 把它变成显式 ERROR。
      格式违规 —— 进了 apply 才发现就要 rollback 整轮，丢掉的是
                  上游几十分钟的生成时间。

    只在 ERROR 时阻断。WARN 是"值得看一眼"而非"不合规"，
    无人值守时因为 warn 停掉整轮太激进，记进日志由人事后看。
    """
    legacy = list((C.ROOT / "changesets" / "pending").glob("*.yaml"))
    if legacy:
        r = py("precheck.py")
        out = r.stdout.strip()
        log(out[-1200:] if out else "(precheck 无输出)")
        if r.returncode != 0:
            return "兼容 YAML 提案有 ERROR 级问题，不应落库"
    semantic = list((C.ROOT / "semantic_changesets" / "pending").glob("*.json"))
    if semantic:
        r = py("apply_semantic_changeset.py", "--check")
        out = (r.stdout + r.stderr).strip()
        log(out[-1200:] if out else "(语义预检无输出)")
        if r.returncode != 0:
            return "OWL/ABox 提案预检失败，不应落库"
    return None


# ---------- 6. 验证 ----------

def verify() -> str | None:
    """合并后的整体验证。委托给 kb.py check，不自己编排。

    顺序只在一处定义：validate 必须在 build_index 之前，否则是拿旧索引校验
    新本体，不报错但结论过期。委托出去也保证手动跑与无人值守跑同一条链。

    apply_changeset.py 内部已经跑过 validate + build_index（合并是原子的：
    追加后立即校验、失败即回滚）。这里再跑一遍判定范围不同：apply 保证
    "合并没把库弄坏"，这里确认全库最终状态——build/ 与本体一致、黄金问题集全通。
    """
    # 同样带 --no-precheck：此刻要判断的是合并后的库，而 pending 里可能还留着
    # 被拦下的条目（apply 部分放行时就是这样）。那些条目的问题不该让
    # 已成功合并的内容被回滚。
    r = py("kb.py", "check", "--no-precheck", timeout=1800)
    log(r.stdout.strip()[-900:])
    if r.returncode != 0:
        return "合并后全链验证失败（validate / build_index / regress 之一）"
    return None


# ---------- 2 / 7. 快照与回滚 ----------

def snapshot() -> str:
    return run([*GIT, "rev-parse", "HEAD"]).stdout.strip()


def rollback(point: str, why: str) -> None:
    """把已跟踪文件恢复到 point。只在真的合并过内容之后调。

    刻意不碰 changesets/：提案是数据，可能是人手工放进去的，合并失败时更
    需要留着看为什么失败。也刻意不 clean 未跟踪文件。

    这个选择的代价不是无害的：**reset --hard 收不回未跟踪文件**。新建在
    ontology/ 下的文件回滚后仍在，仍会被 validate 与 build_index 读到，
    所以"回滚完成"不等于"库回到了开跑前的状态"。

    兜底靠下一次前置检查：未跟踪的 ontology/ 文件会让 require_git_clean
    拦住整轮。污染不会静默累积，但会挡住下一次自动运行，需要人处置。
    取舍是留证据 + 挡下一轮，好过悄悄删掉一份可能有价值的产出。
    """
    log(f"!! {why}")
    log(f"回滚已跟踪文件到 {point[:8]}")
    log("注意：未跟踪文件与 changesets/ 保留，reset --hard 收不回它们——"
        "若下面列出了 ontology/ 或 kb/ 下的文件，库并未真正回到开跑前状态，"
        "需人工处置，否则下一轮前置检查会被拦住。")
    run([*GIT, "reset", "--hard", point])
    r = run([*GIT, "status", "--porcelain"])
    left = [l for l in r.stdout.strip().splitlines() if l.startswith("??")]
    if left:
        log("回滚后仍有未跟踪文件，需人工处置：")
        for l in left[:8]:
            log(f"    {l}")


def commit(before: dict, after: dict, note: str) -> bool:
    run([*GIT, "add", "-A"])
    msg = (f"auto: 每日扩充 {datetime.now():%Y-%m-%d}\n\n"
           f"实体 {before['entities']} -> {after['entities']}｜"
           f"关系 {before['relations']} -> {after['relations']}｜"
           f"kb {before['kb']} -> {after['kb']}\n\n{note[:400]}\n\n"
           f"来源、语义、经营仿真与黄金问题集均通过。")
    r = run([*GIT, "-c", "user.name=semi-kb-bot",
             "-c", "user.email=bot@local", "commit", "-q", "-m", msg])
    if r.returncode == 0:
        log(f"已提交 {snapshot()[:8]}")
        return True
    else:
        log(f"提交失败（可能无改动）：{(r.stdout + r.stderr).strip()[:200]}")
        return False


def archive_semantic(files: list[Path]) -> list[tuple[Path, Path]]:
    """全链通过后再归档，避免失败提案被误标为已应用。"""
    applied = C.ROOT / "semantic_changesets" / "applied"
    applied.mkdir(parents=True, exist_ok=True)
    moves: list[tuple[Path, Path]] = []
    for source in files:
        destination = applied / source.name
        shutil.move(str(source), str(destination))
        moves.append((source, destination))
    return moves


def restore_archived(moves: list[tuple[Path, Path]]) -> None:
    for source, destination in reversed(moves):
        if destination.exists() and not source.exists():
            shutil.move(str(destination), str(source))


def main() -> int:
    C.setup_console()
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只预检已有提案，不落库")
    a = ap.parse_args()

    cfg = yaml.safe_load((C.ROOT / "config.yaml").read_text(encoding="utf-8")) or {}
    guards = ((cfg.get("review") or {}).get("guards") or {})

    log("=" * 60)
    log(f"每日自动扩充开跑  dry_run={a.dry_run}")

    why = preflight(guards)
    if why:
        log(f"!! 前置检查未过：{why}")
        log("未做任何改动。")
        return 2
    log("前置检查通过（工作区干净、基线校验与回归均过）")

    point = snapshot()
    before = counts()
    log(f"回滚点 {point[:8]}｜当前 实体 {before['entities']}"
        f"｜关系 {before['relations']}｜kb {before['kb']}")

    note = "由当前 Agent/自动化任务按 prompts/kb-refresh.md 生成提案"

    legacy_pending = sorted((C.ROOT / "changesets" / "pending").glob("*.yaml"))
    semantic_pending = sorted((C.ROOT / "semantic_changesets" / "pending").glob("*.json"))
    pending = legacy_pending + semantic_pending
    log(f"pending 提案 {len(pending)} 个：{[p.name for p in pending]}")
    if pending:
        gate = gate_pending()
        if gate:
            # 刻意不 rollback：此刻还没合并任何内容，没有要撤的东西。
            # reset --hard 在这里只会破坏——提案与诊断信息都还有价值。
            log(f"!! 提案确定性检查未过：{gate}")
            log("未落库、未改动已跟踪文件。提案留在 pending/ 待修。")
            return 1
    if not pending:
        # 不回滚。没合并过任何内容，没有要撤的东西；而 reset --hard 在这里
        # 只会破坏——比如删掉生成步骤中途写坏但还有诊断价值的文件。
        log("无事可做（没有待落库的提案）。不算失败，也不动任何文件。")
        r = run([*GIT, "status", "--porcelain"])
        if r.stdout.strip():
            log("注意：工作区有改动但没有提案，生成步骤可能中途失败了：")
            for l in r.stdout.strip().splitlines()[:8]:
                log(f"    {l}")
        return 0

    if a.dry_run:
        log("--dry-run：到此为止。提案留在 pending/，不落库、不改文件、不回滚。")
        return 0

    for script, files in (("apply_changeset.py", legacy_pending),
                          ("apply_semantic_changeset.py", semantic_pending)):
        if not files:
            continue
        args = ("--defer-archive", "--semantic-only") if script == "apply_semantic_changeset.py" else ()
        r = py(script, *args)
        log((r.stdout + r.stderr).strip()[-1200:])
        if r.returncode != 0:
            rollback(point, f"落库失败（{script} 退出码 {r.returncode}）")
            return 1

    after = counts()
    if (why := check_growth(before, after, guards)):
        rollback(point, why)
        return 1

    if (why := verify()):
        rollback(point, why)
        return 1

    archived: list[tuple[Path, Path]] = []
    try:
        archived = archive_semantic(semantic_pending)
    except OSError as exc:
        restore_archived(archived)
        rollback(point, f"语义提案归档失败：{exc}")
        return 1
    if not commit(before, after, note):
        restore_archived(archived)
        rollback(point, "自动提交失败")
        return 1
    log("本次扩充完成。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("!! 被中断")
        sys.exit(1)
