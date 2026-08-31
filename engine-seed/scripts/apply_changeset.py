"""变更提案的审核与合并。

自动更新链路的落地环节：kb-refresh 产出提案 -> 确定性预检
-> 本脚本按证据策略放行 -> 合并后自动跑完整语义检查 -> 移入 applied/ 或留 pending/。

默认链路没有人工闸门；结构、引用、来源、OWL、SHACL、推理、经营模型、仿真和
黄金问题集共同守门。可选 verdict 仅用于额外治理，不是默认依赖。

用法：
    python scripts/apply_changeset.py --dry-run       # 只输出裁决清单，不改文件
    python scripts/apply_changeset.py                 # 按 config.yaml 策略执行
    python scripts/apply_changeset.py --no-review     # 绕过 verdict 全放行（仅调试用）
    python scripts/apply_changeset.py --force-review  # 全部拦下，用于空跑看清单
    python scripts/apply_changeset.py --file <path>   # 只处理指定提案
退出码：0 正常；1 合并后校验失败（已自动回滚）；2 提案格式错误。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import yaml

import common as C

CONF_ORDER = {"low": 0, "medium": 1, "high": 2}
SECTION_KEY = {"entities": "entities", "relations": "relations", "kb_cases": "cases"}
CHANGE_KIND = {"entities": "entities", "relations": "relations", "kb_cases": "instances"}


def load_verdict(path: Path) -> tuple[dict, str]:
    """读取提案对应的裁决文件。返回 ({条目键: 裁决}, 整体说明)。

    本脚本只消费结论，故意不在这里做任何"像审核"的判断：apply 必须保持
    确定性（同样的提案 + 同样的 verdict 得到同样的结果，可离线复跑、可进 CI），
    一旦内嵌判断就做不到了。

    verdict 由谁写与本脚本无关，只要格式对得上。本项目当前一律由当前 Agent
    自审后写 verdict（不派 SubAgent），故 verifier 一律是 self:<子流程名>。
    本脚本不校验也无法校验它的真实性——要紧的是标得诚实，
    假装独立审过比自审更糟。
    """
    vp = C.ROOT / "changesets" / "verdicts" / f"{path.stem}.verdict.yaml"
    if not vp.exists():
        return {}, f"缺 verdict 文件（期望 {vp.relative_to(C.ROOT)}）"
    try:
        data = yaml.safe_load(vp.read_text(encoding="utf-8")) or {}
    except Exception as exc:                           # noqa: BLE001
        return {}, f"verdict 文件解析失败：{exc!r}"
    if (data.get("changeset") or "") not in ("", path.name):
        return {}, f"verdict 声明的提案是 {data.get('changeset')}，与 {path.name} 不符"
    items = {}
    for row in data.get("items") or []:
        key = (row or {}).get("key")
        if key:
            items[key] = row
    return items, f"verdict by {data.get('verifier') or '?'} @ {data.get('verified_at') or '?'}"


def item_key(section: str, item: dict) -> str:
    """verdict 里定位条目的键。关系没有 id，用三元组。"""
    if section == "relations":
        return f"{item.get('from')}|{item.get('type')}|{item.get('to')}"
    return str(item.get("id") or item.get("title") or "")


def classify(section: str, item: dict, cfg: dict, override: str | None,
             verdicts: dict | None = None) -> tuple[bool, str]:
    """返回 (是否可自动放行, 理由)。"""
    review = cfg.get("review") or {}
    if override == "auto":
        return True, "--no-review 强制自动"
    if override == "manual":
        return False, "--force-review 强制拦下"

    kind = CHANGE_KIND[section]
    if not (review.get("auto_apply") or {}).get(kind, False):
        return False, f"策略 auto_apply.{kind}=false"

    guards = review.get("guards") or {}
    min_conf = guards.get("min_confidence", "medium")
    conf = (item.get("provenance") or {}).get("confidence", "low")
    if CONF_ORDER.get(conf, 0) < CONF_ORDER.get(min_conf, 1):
        return False, f"可信度 {conf} 低于门槛 {min_conf}"
    if guards.get("require_ref_for_web") and (item.get("provenance") or {}).get("source_type") == "web":
        if not (item.get("provenance") or {}).get("ref"):
            return False, "web 来源缺少 ref (URL)"

    if guards.get("require_verdict"):
        row = (verdicts or {}).get(item_key(section, item))
        if not row:
            return False, "verdict 未覆盖该条目"
        if row.get("verdict") != "pass":
            return False, f"裁定 {row.get('verdict')}：{row.get('reason') or '未说明'}"
        return True, f"审核通过：{row.get('reason') or '无附注'}"

    return True, f"符合 auto_apply.{kind} 且可信度 {conf} 达标"


def collect_refs(section: str, item: dict) -> set[str]:
    """提取条目对本体实体的引用，用于检测提案内部依赖。"""
    refs: set[str] = set()
    if section == "relations":
        for k in ("from", "to"):
            if isinstance(item.get(k), str):
                refs.add(item[k])
        return refs
    if section == "kb_cases":
        for k in ("anomaly_ref", "detected_at_ref"):
            if isinstance(item.get(k), str):
                refs.add(item[k])
        for lst, key in (("possible_causes", "cause_ref"), ("actions", "action_ref"),
                         ("detection", "metric_ref")):
            for sub in item.get(lst) or []:
                if isinstance((sub or {}).get(key), str):
                    refs.add(sub[key])
        for p in (item.get("impact") or {}).get("blocked_processes") or []:
            if isinstance(p, str):
                refs.add(p)
        return refs
    for _, target in C.entity_refs(item):
        refs.add(target)
    return refs


def label_of(section: str, item: dict) -> str:
    if section == "relations":
        return f"{item.get('from')} --{item.get('type')}--> {item.get('to')}"
    return f"{item.get('id')} ({item.get('name_zh') or item.get('title') or ''})"


def append_items(target: Path, section: str, items: list[dict]) -> None:
    """文本级追加：保留目标文件原有注释与格式（PyYAML 重写会丢注释）。"""
    key = SECTION_KEY[section]
    text = target.read_text(encoding="utf-8")
    if not text.endswith("\n"):
        text += "\n"
    if f"{key}:" not in text:
        text += f"\n{key}:\n"
    blocks = []
    for item in items:
        dumped = yaml.safe_dump([item], allow_unicode=True, sort_keys=False,
                                default_flow_style=False, width=100)
        blocks.append("\n".join("  " + ln if ln.strip() else ln
                                for ln in dumped.rstrip("\n").split("\n")))
    target.write_text(text + "\n".join(blocks) + "\n", encoding="utf-8")


def process(path: Path, cfg: dict, override: str | None, dry_run: bool) -> dict:
    doc = C.load_yaml(path) or {}
    head = doc.get("changeset") or {}
    additions = doc.get("additions") or {}
    mods = doc.get("modifications") or []
    dels = doc.get("deletions") or []

    verdicts, verdict_note = ({}, "override 跳过 verdict") if override else load_verdict(path)

    print("=" * 68)
    print(f"提案 {path.name}")
    print(f"  标题：{head.get('title')}")
    print(f"  来源：{head.get('author')} | 创建 {head.get('created_at')}")
    print(f"  审核：{verdict_note}" + (f"，覆盖 {len(verdicts)} 条" if verdicts else ""))
    print("=" * 68)

    auto: dict[Path, dict[str, list[dict]]] = {}
    pending_manual: list[str] = []
    n_auto = 0

    # 提案内部依赖：本提案引入的新 ID 集合。
    # 若某条目引用的 ID 只存在于本提案且该 ID 未被自动放行，则本条目也须一并转人工，
    # 否则实例先于本体合并必然产生悬空引用（虽有回滚兜底，但应主动避免）。
    introduced = {
        (e.get("item") or {}).get("id")
        for sec in ("entities", "kb_cases")
        for e in additions.get(sec) or []
        if (e.get("item") or {}).get("id")
    }
    gated: set[str] = set()

    for section in ("entities", "relations", "kb_cases"):
        entries = additions.get(section) or []
        if not entries:
            continue
        print(f"\n[新增 {section}] {len(entries)} 条")
        for entry in entries:
            item = entry.get("item") or {}
            target = entry.get("target_file")
            if not target:
                print(f"  ! 缺少 target_file，跳过：{label_of(section, item)}")
                continue
            ok, reason = classify(section, item, cfg, override, verdicts)
            if ok:
                blocked = sorted((collect_refs(section, item) & introduced) & gated)
                if blocked:
                    ok = False
                    reason = f"依赖本提案中被拦下的条目：{', '.join(blocked)}"
            if not ok and item.get("id"):
                gated.add(item["id"])
            mark = "自动放行" if ok else "拦下"
            print(f"  [{mark}] {label_of(section, item)}")
            print(f"      -> {target}")
            print(f"      理由：{reason}")
            if ok:
                n_auto += 1
                auto.setdefault(C.ROOT / target, {}).setdefault(section, []).append(item)
            else:
                pending_manual.append(f"{section}: {label_of(section, item)} — {reason}")

    if mods or dels:
        # 本脚本只会做文本级追加，改不了也删不了既有条目，所以这两类天然落不了地。
        print(f"\n[修改 {len(mods)} 条 | 删除 {len(dels)} 条] 本脚本不支持，一律拦下")
        for m in mods:
            pending_manual.append(f"modification: {m}")
        for d in dels:
            pending_manual.append(f"deletion: {d}")

    print("\n" + "-" * 68)
    print(f"结论：自动放行 {n_auto} 条 | 拦下 {len(pending_manual)} 条")
    if pending_manual:
        print("\n拦下清单：")
        for line in pending_manual:
            print(f"  · {line}")

    return {"auto": auto, "manual": pending_manual, "n_auto": n_auto, "head": head}


def merge_and_validate(auto: dict[Path, dict[str, list[dict]]]) -> bool:
    """合并后立即校验；失败则回滚，保证仓库始终处于可用状态。"""
    backups: dict[Path, str] = {}
    try:
        for target, sections in auto.items():
            if not target.exists():
                print(f"  ! 目标文件不存在：{target}")
                return False
            backups[target] = target.read_text(encoding="utf-8")
            for section, items in sections.items():
                append_items(target, section, items)
                print(f"  已写入 {len(items)} 条 {section} -> {target.relative_to(C.ROOT)}")

        print("\n合并后自动校验：")
        res = subprocess.run([sys.executable, str(C.ROOT / "scripts" / "kb.py"),
                              "check", "--quick", "--no-precheck"],
                            cwd=C.ROOT, capture_output=True, text=True, encoding="utf-8")
        print(res.stdout.strip())
        if res.returncode != 0:
            print("\n校验失败，回滚全部改动。")
            for target, original in backups.items():
                target.write_text(original, encoding="utf-8")
            return False
        return True
    except Exception as exc:                       # noqa: BLE001
        print(f"\n合并异常 {exc!r}，回滚。")
        for target, original in backups.items():
            target.write_text(original, encoding="utf-8")
        return False


def main() -> int:
    C.setup_console()
    argv = sys.argv[1:]
    dry_run = "--dry-run" in argv
    override = "auto" if "--no-review" in argv else ("manual" if "--force-review" in argv else None)

    cfg = C.load_config()
    pending_dir = C.ROOT / "changesets" / "pending"

    if "--file" in argv:
        files = [Path(argv[argv.index("--file") + 1]).resolve()]
    else:
        files = sorted(pending_dir.glob("*.yaml"))

    if not files:
        print("changesets/pending/ 下没有待处理提案。")
        return 0

    mode = (cfg.get("review") or {}).get("mode")
    print(f"审核策略：mode={mode}"
          + (f"（命令行覆盖为 {override}）" if override else "")
          + ("  [DRY-RUN 不写入]" if dry_run else ""))

    exit_code = 0
    for path in files:
        try:
            result = process(path, cfg, override, dry_run)
        except Exception as exc:                   # noqa: BLE001
            print(f"提案 {path.name} 解析失败：{exc!r}")
            return 2

        if dry_run:
            print("\nDRY-RUN：未写入任何文件，提案保留在 pending/。")
            continue

        if not result["auto"]:
            print("\n无可自动放行内容，提案保留在 pending/（补 verdict 或修正后下轮重试）。")
            continue

        print()
        if not merge_and_validate(result["auto"]):
            print(f"提案 {path.name} 未合并，保留在 pending/。")
            exit_code = 1
            continue

        dest_dir = "applied" if not result["manual"] else "pending"
        if dest_dir == "applied":
            dest = C.ROOT / "changesets" / "applied" / path.name
            shutil.move(str(path), str(dest))
            print(f"\n提案已全部合并，移入 changesets/applied/{path.name}")
        else:
            print(f"\n自动部分已合并，仍有 {len(result['manual'])} 条被拦下，"
                  f"提案保留在 pending/（下轮补齐 verdict 后可继续，或移入 rejected/）")

        print("重建索引：")
        res = subprocess.run([sys.executable, str(C.ROOT / "scripts" / "build_index.py")],
                            cwd=C.ROOT, capture_output=True, text=True, encoding="utf-8")
        print(res.stdout.strip())

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
