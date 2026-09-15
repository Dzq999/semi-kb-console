#!/usr/bin/env python
"""C 轨常态修剪·同义合并事务执行器（人工确认后落盘的唯一入口）。

把 B1a 已跑通的三件套（merge_synonyms → fix_synonym_prop_refs → 发布门禁）包成
一个**原子事务**：任一步失败即整体回滚，活文件绝不留半成品。

为何要本脚本而非直接串三件套：merge_synonyms 的备份是内存态、成功即丢弃（B1a 教训
——门禁在合并之后才失败时已无法回滚）。本脚本在动任何文件前，先把三件套可能触碰的
**全部活文件**拷进一份磁盘级快照，作为唯一可靠回滚源；门禁通过才保留，否则整体还原。

事务步骤：
  1. 全量快照：generated/current/provenance 三 TTL + mappings/ + knowledge/entries/
     + ontology/rules/ 整目录 → build/reports/_prune_snapshot_<ts>/
  2. merge_synonyms.py --map <map>       （删同义声明 + 重指引用，改 3 个 RDF 图）
  3. fix_synonym_prop_refs.py --apply --map <map>  （改 JSON 映射/条目 + .rq 规则的悬空引用）
  4. --gate（默认开）：kb.py check --no-precheck --defer-derived  （发布门禁重校验）
  5. 全绿 → 写审计 prune-synonyms-<ts>.json(status=committed)、留快照、退 0
     任一步失败 → 从快照整体还原全部文件 → 写审计(status=rolled_back) → 退 1

用法：
  python scripts/prune_synonyms.py --map build/reports/_c1_merge_map.json
  python scripts/prune_synonyms.py --map <map> --no-gate     # 跳门禁（单测/离线用）
  python scripts/prune_synonyms.py --map <map> --dry-run     # 只报将改动，不快照不落盘
退出码：0 成功(committed)；1 失败(已回滚)；2 用法/文件错误。
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
REPORTS = ROOT / "build" / "reports"
SEMI = "urn:pxai:semi:"

# 三件套会触碰的活文件集合（快照/回滚边界）。
TTLS = [
    ROOT / "ontology" / "modules" / "generated.ttl",
    ROOT / "knowledge" / "semantic" / "current.ttl",
    ROOT / "knowledge" / "semantic" / "provenance.ttl",
]
TREES = [
    ROOT / "mappings",
    ROOT / "knowledge" / "entries",
    ROOT / "ontology" / "rules",
]

def _run(script: str, *args: str) -> tuple[int, str]:
    """跑一个 scripts/ 下的脚本，回 (exit_code, 合并输出)。cwd=引擎根。"""
    cmd = [sys.executable, str(SCRIPTS / script), *args]
    proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _load_map(map_path: Path) -> list[dict]:
    doc = json.loads(map_path.read_text(encoding="utf-8"))
    if not isinstance(doc, list) or not doc:
        raise ValueError("合并映射必须是非空的簇列表")
    for c in doc:
        if not c.get("canonical") or not c.get("merge"):
            raise ValueError(f"簇缺 canonical/merge：{c}")
    return doc


def _snapshot(dest: Path) -> list[str]:
    """把全部活文件拷进快照目录，返回已快照的相对路径清单。"""
    dest.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    for p in TTLS:
        if p.is_file():
            rel = p.relative_to(ROOT).as_posix()
            target = dest / rel.replace("/", "__")
            shutil.copy2(p, target)
            saved.append(rel)
    for tree in TREES:
        if not tree.is_dir():
            continue
        tree_dest = dest / tree.relative_to(ROOT).as_posix().replace("/", "__")
        shutil.copytree(tree, tree_dest)
        for f in tree.rglob("*"):
            if f.is_file():
                saved.append(f.relative_to(ROOT).as_posix())
    return saved


def _restore(dest: Path) -> None:
    """从快照整体还原全部活文件（回滚）。"""
    for p in TTLS:
        target = dest / p.relative_to(ROOT).as_posix().replace("/", "__")
        if target.is_file():
            shutil.copy2(target, p)
    for tree in TREES:
        tree_dest = dest / tree.relative_to(ROOT).as_posix().replace("/", "__")
        if tree_dest.is_dir():
            if tree.is_dir():
                shutil.rmtree(tree)
            shutil.copytree(tree_dest, tree)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--map", required=True, help="合并映射 JSON（find_synonyms 输出的子集）")
    ap.add_argument("--gate", dest="gate", action="store_true", default=True)
    ap.add_argument("--no-gate", dest="gate", action="store_false",
                    help="跳发布门禁（单测/离线用；生产务必开门禁）")
    ap.add_argument("--dry-run", action="store_true", help="只报将改动，不快照不落盘")
    args = ap.parse_args()
    for stream in (sys.stdout, sys.stderr):
        try: stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError): pass

    map_path = Path(args.map)
    if not map_path.is_absolute():
        map_path = (ROOT / map_path).resolve()
    if not map_path.is_file():
        print(f"找不到合并映射：{map_path}", file=sys.stderr)
        return 2
    try:
        clusters = _load_map(map_path)
    except ValueError as exc:
        print(f"映射非法：{exc}", file=sys.stderr)
        return 2
    total_terms = sum(len(c["merge"]) for c in clusters)
    print(f"同义修剪：{len(clusters)} 簇 / {total_terms} 个待并术语；门禁={'开' if args.gate else '关'}")

    map_arg = str(map_path)
    if args.dry_run:
        code, out = _run("merge_synonyms.py", "--map", map_arg, "--dry-run")
        print(out.strip())
        code2, out2 = _run("fix_synonym_prop_refs.py", "--map", map_arg)  # 无 --apply = dry
        print(out2.strip())
        print("[dry-run] 未快照、未落盘")
        return 0 if code == 0 and code2 == 0 else 1

    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    snap = REPORTS / f"_prune_snapshot_{ts}"
    log = REPORTS / f"prune-synonyms-{ts}.json"
    audit: dict = {"generated_at": ts, "map": clusters, "clusters": len(clusters),
                   "terms": total_terms, "gate": args.gate, "steps": {}}

    saved = _snapshot(snap)
    audit["snapshot"] = snap.relative_to(ROOT).as_posix()
    audit["snapshot_files"] = len(saved)
    print(f"已快照 {len(saved)} 个活文件 → {audit['snapshot']}")

    def fail(step: str, code: int, out: str) -> int:
        audit["steps"][step] = {"exit_code": code, "output": out[-4000:]}
        print(f"✗ 步骤 {step} 失败（exit {code}），回滚中……", file=sys.stderr)
        _restore(snap)
        audit["status"] = "rolled_back"
        audit["failed_step"] = step
        _write_log(log, audit)
        print(f"已从快照整体还原。审计：{log.relative_to(ROOT).as_posix()}", file=sys.stderr)
        return 1

    # 2. merge
    code, out = _run("merge_synonyms.py", "--map", map_arg)
    audit["steps"]["merge"] = {"exit_code": code, "output": out[-4000:]}
    print(out.strip()[-800:])
    if code != 0:
        return fail("merge", code, out)

    # 3. ref-fix（先 dry 看命中，再 apply）
    code, out = _run("fix_synonym_prop_refs.py", "--apply", "--map", map_arg)
    audit["steps"]["reffix"] = {"exit_code": code, "output": out[-4000:]}
    print(out.strip()[-800:])
    if code != 0:
        return fail("reffix", code, out)

    # 4. gate
    if args.gate:
        print("跑发布门禁（约 13 分钟，semantic_validate 主导）……")
        code, out = _run("kb.py", "check", "--no-precheck", "--defer-derived")
        audit["steps"]["gate"] = {"exit_code": code, "output": out[-8000:]}
        if code != 0:
            return fail("gate", code, out)
        print("✓ 门禁通过")

    audit["status"] = "committed"
    _write_log(log, audit)
    print(f"✓ 同义修剪已提交。审计：{log.relative_to(ROOT).as_posix()}")
    print(f"  快照保留于 {audit['snapshot']}（确认无误后可手工清理）")
    return 0


def _write_log(log: Path, audit: dict) -> None:
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
