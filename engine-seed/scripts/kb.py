"""库的日常入口：查状态、跑全链。

    python scripts/kb.py check              # precheck -> validate -> build_index -> regress
    python scripts/kb.py check --quick       # 跳过 build_index/scenario_mine
    python scripts/kb.py check --defer-derived  # 发布门禁：跳派生物、保 regress 等判定
    python scripts/kb.py refresh-derived     # 发布后补跑 build_index/scenario_mine/migrate
    python scripts/kb.py status              # 规模、provenance 分布、选题缺口
    python scripts/kb.py status --json

退出码：check 全过 0、有环节失败 1；refresh-derived 全过 0、有失败 1（调用方按
best-effort 处理，不因此回滚已发布内容）；status 恒 0（查询工具不是校验器）。

一个脚本管两面：跑之前想知道该补什么（status），跑之后想知道有没有跑坏
（check）。

## check 为什么要固化顺序

validate 必须在 build_index 之前跑。反了不会报错，只会拿旧索引校验新本体
并给出过期结论——这种错最难发现，因为它一切正常只是答案是错的。
顺序写进代码就不会再有人记错，顺带省掉几次 Python 冷启动。

## status 报什么、不报什么

报：规模、按类型与域的分布、provenance 可信度分布、无 kb 实例的异常
（选题主依据）、孤立实体（建了没接进图）、声称跨域但只连一侧的类比。

不报"仅一条边的实体"。Equipment 只有 belongs_to、Cause 只有 may_cause、
Parameter 只有 controls 都是常态——这类实体的自然关系数本来就是 1，
一条边是建对了，不是没接好。报它只会制造不存在的工作。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import common as C  # noqa: E402
except ModuleNotFoundError as exc:
    print(f"缺少运行依赖 {exc.name}；请安装 requirements.txt。", file=sys.stderr)
    raise SystemExit(2) from exc


# ---------- status ----------

def collect() -> dict:
    ip, gp = C.ROOT / "build" / "index.json", C.ROOT / "build" / "graph.json"
    for p in (ip, gp):
        if not p.is_file():
            print(f"缺 {p.relative_to(C.ROOT)}，先跑 python scripts/kb.py check")
            raise SystemExit(0)
    idx = json.loads(ip.read_text(encoding="utf-8"))
    g = json.loads(gp.read_text(encoding="utf-8"))

    recs = idx.get("records") or []
    ents = {r["id"]: r for r in recs if r.get("kind") == "entity"}
    kbs = [r for r in recs if r.get("kind") == "kb_case"]
    covered = {r.get("anomaly_ref") for r in kbs if r.get("anomaly_ref")}
    oa, ia = g.get("out_adjacency") or {}, g.get("in_adjacency") or {}

    def deg(i: str) -> int:
        return len(oa.get(i, [])) + len(ia.get(i, []))

    anomalies = {i for i, r in ents.items() if r.get("type") == "Anomaly"}

    by_type: dict[str, int] = {}
    by_domain: dict[str, dict[str, int]] = {}
    conf: dict[str, int] = {}
    src: dict[str, int] = {}
    for r in ents.values():
        t = r.get("type") or "?"
        by_type[t] = by_type.get(t, 0) + 1
        by_domain.setdefault(r.get("domain") or "?", {})[t] = \
            by_domain.setdefault(r.get("domain") or "?", {}).get(t, 0) + 1
    # build_index 把 provenance 摊平成顶层字段，直接读顶层
    for r in list(ents.values()) + kbs:
        conf[r.get("confidence") or "?"] = conf.get(r.get("confidence") or "?", 0) + 1
        src[r.get("source_type") or "?"] = src.get(r.get("source_type") or "?", 0) + 1

    # 类比目标只连一个域，且 note 自称跨域共用——名不副实，值得补另一侧。
    # 单域本身是常态（实测全部 ext.* 都是单域），所以只报自称跨域的。
    CROSS = ("两域", "跨域", "共用", "同一套")
    ext_link: dict[str, set[str]] = {}
    ext_claim: set[str] = set()
    for e in g.get("edges") or []:
        to = str(e.get("to") or "")
        if e.get("type") == "analogous_to" and to.startswith("ext."):
            ext_link.setdefault(to, set()).add(str(e.get("from") or "").split(".")[0])
            if any(k in (e.get("note") or "") for k in CROSS):
                ext_claim.add(to)

    return {
        "entities": len(ents), "kb_cases": len(kbs),
        "edges": len(g.get("edges") or []),
        "anomaly_total": len(anomalies),
        "anomaly_covered": len(anomalies & covered),
        "by_type": by_type, "by_domain": by_domain,
        "confidence": conf, "source_type": src,
        "uncovered": [{"id": i, "degree": deg(i), "name_zh": ents[i].get("name_zh"),
                       "severity": ents[i].get("severity"),
                       "domain": ents[i].get("domain")}
                      for i in sorted(anomalies - covered, key=lambda x: -deg(x))],
        "orphans": [{"id": i, "type": ents[i].get("type"),
                     "name_zh": ents[i].get("name_zh")}
                    for i in sorted(ents, key=lambda x: (ents[x].get("type") or "", x))
                    if deg(i) == 0],
        "ext_asymmetric": sorted(k for k, v in ext_link.items()
                                 if len(v) == 1 and k in ext_claim),
    }


def cmd_status(a) -> int:
    s = collect()
    if a.as_json:
        print(json.dumps(s, ensure_ascii=False, indent=2))
        return 0

    print("=" * 68)
    print(f"实体 {s['entities']} | 知识库实例 {s['kb_cases']} | 图边 {s['edges']}")
    print("=" * 68)
    print("按类型：" + "  ".join(f"{k} {v}" for k, v in sorted(s["by_type"].items())))
    for d in ("core", "fab", "ap"):
        if d in s["by_domain"]:
            print(f"  {d:<5} " + "  ".join(
                f"{k} {v}" for k, v in sorted(s["by_domain"][d].items())))
    print("\n可信度：" + "  ".join(f"{k} {v}" for k, v in sorted(s["confidence"].items()))
          + "    来源：" + "  ".join(f"{k} {v}" for k, v in sorted(s["source_type"].items())))

    print("\n" + "-" * 68)
    print(f"Anomaly {s['anomaly_total']} | 有 kb 实例 {s['anomaly_covered']} | "
          f"无实例 {len(s['uncovered'])}")
    print("-" * 68)
    if s["uncovered"]:
        print("未覆盖异常（度数降序；度数高说明它在风险图谱里更关键，空洞更显眼）：")
        for u in s["uncovered"]:
            print(f"  度数 {u['degree']:>2}  {u['id']:<34} {u['name_zh'] or '':<12} "
                  f"severity={u['severity'] or '-':<9} {u['domain']}")
    else:
        print("所有异常都有 kb 实例。")

    if s["orphans"]:
        print(f"\n孤立实体 {len(s['orphans'])} 个（图上零边，建了但没接进去）：")
        for o in s["orphans"][:12]:
            print(f"  {o['type']:<10} {o['id']:<38} {o['name_zh'] or ''}")
    if s["ext_asymmetric"]:
        print(f"\n自称跨域但只连一侧 {len(s['ext_asymmetric'])} 个：")
        for k in s["ext_asymmetric"]:
            print(f"  {k}")
    return 0


# ---------- check ----------

def run_step(name: str, timeout: int = 1800) -> tuple[bool, float, str]:
    t0 = time.monotonic()
    r = subprocess.run([sys.executable, str(C.ROOT / "scripts" / name)],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", cwd=str(C.ROOT), timeout=timeout,
                       env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    return r.returncode == 0, time.monotonic() - t0, (r.stdout or "") + (r.stderr or "")


# build_index / scenario_mine 是"派生物"步骤：只重建检索索引与场景卡，不参与
# 一致性 PASS/FAIL 判定（build_index 唯一的非零退出=重复实体 ID，已被链内更前
# 的 validate.py R001 覆盖；scenario_mine 不校验任何图内容，且没有任何 shape 针
# 对场景卡类）。发布门禁把它们移出关键路径、发布成功后由 refresh-derived 补跑。
DERIVED_STEPS = ["build_index.py", "scenario_mine.py"]


def build_check_chain(has_pending: bool, skip_derived: bool) -> list[str]:
    """构造 check 链。skip_derived=True 时跳过 build_index/scenario_mine（派生物，
    非一致性判定步骤），其余环节——含 semantic_validate/semantic_test/simulate_check/
    regress——一律保留。抽成纯函数便于单测门禁子集不含派生物、仍含 regress。"""
    chain = (["precheck.py"] if has_pending else []) + [
        "source_ingest.py",
        "vfab_ingest.py",
        "align_sources.py",
        "capability_validate.py",
        "validate.py",
    ]
    if not skip_derived:
        chain.extend(DERIVED_STEPS)
    chain.extend(["migrate_semantic.py", "semantic_validate.py", "semantic_test.py"])
    chain.extend(["simulate_check.py", "regress.py"])
    return chain


def cmd_check(a) -> int:
    """--no-precheck 用于只判断"已落库的库好不好"，不看 pending 里的提案。

    daily_refresh 的前置检查需要这个：它要确认基线自己是干净的，此时
    pending 里可能已经存在由当前 Agent 生成的待处理提案。
    若把提案也算进来，一份坏提案会让前置检查失败并退出码 2（环境没就绪、
    未做任何改动），而它本该走到第 3b 段闸门、以退出码 1 结束（提案被拒）。
    两者对调用方的含义完全不同，混在一起会让定时任务误判失败原因。
    """
    pending = ([] if a.no_precheck
               else list((C.ROOT / "changesets" / "pending").glob("*.yaml")))
    skip_derived = bool(a.quick or getattr(a, "defer_derived", False))
    chain = build_check_chain(bool(pending), skip_derived)
    if a.no_precheck:
        print("（--no-precheck：只查已落库内容，不看 pending/）")
    elif not pending:
        print("（pending/ 为空，跳过 precheck）")

    total = 0.0
    for name in chain:
        ok, dt, out = run_step(name)
        total += dt
        print(f"[{'OK  ' if ok else 'FAIL'}] {name:<16} {dt:>5.2f}s")
        if not ok:
            lines = [l for l in out.strip().splitlines() if l.strip()]
            for l in lines[-12:]:
                print("    " + l)
            print(f"\n{name} 失败，链路中止。总耗时 {total:.2f}s")
            return 1
        keep = [l for l in out.strip().splitlines()
                if any(k in l for k in ("ERROR", "通过", "实体 ", "已生成", "检索记录", "经营模型/仿真"))]
        for l in keep[-3:]:
            print("    " + l.strip())

    print(f"\n全链通过，总耗时 {total:.2f}s")
    return 0


def cmd_refresh_derived(a) -> int:
    """发布后 best-effort 刷新"派生物"：build_index → scenario_mine → migrate_semantic。

    这三步不参与发布 PASS/FAIL 判定，已从每轮发布关键路径移出（见 build_check_chain
    的 skip_derived）。发布门禁通过后由本命令补跑，把最新检索索引/场景卡刷出来，并
    经 migrate_semantic 把刷新后的场景卡重新并入 build/semantic/current.trig 供查询层。
    失败只告警、返回非零，调用方（发布路径）按 best-effort 处理，绝不因此回滚已发布内容。
    """
    total = 0.0
    ok_all = True
    for name in [*DERIVED_STEPS, "migrate_semantic.py"]:
        ok, dt, out = run_step(name)
        total += dt
        print(f"[{'OK  ' if ok else 'FAIL'}] {name:<18} {dt:>5.2f}s")
        if not ok:
            ok_all = False
            lines = [l for l in out.strip().splitlines() if l.strip()]
            for l in lines[-12:]:
                print("    " + l)
    print(f"\n派生物刷新{'完成' if ok_all else '有失败（不影响已发布内容）'}，总耗时 {total:.2f}s")
    return 0 if ok_all else 1


def main() -> int:
    C.setup_console()
    ap = argparse.ArgumentParser(description="库的日常入口")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="跑完整检查链")
    c.add_argument("--quick", action="store_true", help="跳过 build_index/scenario_mine")
    c.add_argument("--defer-derived", action="store_true", dest="defer_derived",
                   help="发布门禁用：跳过 build_index/scenario_mine（派生物，发布后由 "
                        "refresh-derived 补跑），regress 等一致性判定环节一律保留")
    c.add_argument("--no-precheck", action="store_true", dest="no_precheck",
                   help="不检查 pending/ 里的提案，只判断已落库内容")
    sub.add_parser("refresh-derived",
                   help="发布后 best-effort 刷新检索索引/场景卡并并回 current.trig")
    st = sub.add_parser("status", help="规模与选题缺口")
    st.add_argument("--json", action="store_true", dest="as_json")
    a = ap.parse_args()
    if a.cmd == "check":
        return cmd_check(a)
    if a.cmd == "refresh-derived":
        return cmd_refresh_derived(a)
    return cmd_status(a)


if __name__ == "__main__":
    sys.exit(main())
