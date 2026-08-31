"""提案落库前的形状检查。只查 validate.py 看不到的内容。

    python scripts/precheck.py                              # 查 pending/ 下全部
    python scripts/precheck.py changesets/pending/x.yaml     # 查单份
    python scripts/precheck.py --json

退出码：0 无 error｜1 有 error（不应落库）｜2 用法/文件错误

检查范围：
- changeset/additions/target_file 等提案外形，避免 apply 静默跳过；
- 提案内部依赖，提示新增实体导致的审核顺序影响；
- provenance、因果措辞与 KB 图边的轻量 lint，只报 warn。

validate.py 负责合并后的全库字段、引用、关系和一致性校验；本脚本不重复实现。
本脚本只读，可离线复跑。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402

SECTIONS = ("entities", "relations", "kb_cases")

# 三类 lint 都只报 warn：是"值得看一眼"的线索，不是铁定违规。

# 时长类措辞配 may_cause：影响 MTTR，不决定异常是否发生。
MTTR_WORDS = ("持续多久", "持续时长", "修复时长", "停机时长",
              "恢复时间", "维修时长", "MTTR")

# 标 model_prior 却写标准号或量化阈值，是"看起来有据可查、实际是编的"，
# 比老实标 model_prior 更糟——它伪装成了可核查的证据。
CITE_PAT = re.compile(
    r"(SEMI\s*[A-Z]\d+|JEDEC|JESD\d+|J-STD-\d+|IPC[\s/-]|ISO\s*\d+|"
    r"\d+\s*(nm|µm|um|μm|ppm|kPa|MPa|°C|℃)\b)", re.IGNORECASE)


class Finding:
    __slots__ = ("level", "section", "key", "msg")

    def __init__(self, level: str, section: str, key: str, msg: str) -> None:
        self.level, self.section, self.key, self.msg = level, section, key, msg

    def as_dict(self) -> dict:
        return {"level": self.level, "section": self.section,
                "key": self.key, "msg": self.msg}


def item_key(section: str, item: dict) -> str:
    if section == "relations":
        return f"{item.get('from')}|{item.get('type')}|{item.get('to')}"
    return str(item.get("id") or "?")


def iter_items(doc: dict):
    """遍历提案条目，产出 (section, entry, item)。"""
    add = doc.get("additions")
    if not isinstance(add, dict):
        return
    for section in SECTIONS:
        for entry in add.get(section) or []:
            if isinstance(entry, dict) and isinstance(entry.get("item"), dict):
                yield section, entry, entry["item"]


def check_envelope(doc: dict, path: Path, out: list[Finding]) -> bool:
    """顶层信封。返回 False 表示结构错到没法继续查。

    这是本脚本存在的首要理由：键名写错不会报错，apply_changeset 会静默
    跳过并以退出码 0 报成功。
    """
    if not isinstance(doc, dict):
        out.append(Finding("error", "-", path.name, "根节点不是映射"))
        return False
    if "changeset" not in doc:
        wrong = [k for k in ("meta", "changset", "change_set", "info") if k in doc]
        hint = f"（发现 {wrong[0]!r}，应为 changeset）" if wrong else ""
        out.append(Finding("error", "-", path.name,
                           f"缺顶层键 changeset:{hint}。apply_changeset 会静默"
                           "跳过并以退出码 0 报成功——这是唯一能骗过定时任务的失败模式"))
        return False
    if not isinstance(doc.get("additions"), dict):
        out.append(Finding("error", "-", path.name,
                           "缺顶层键 additions: 或它不是映射，本提案不会合并任何内容"))
        return False
    if not any(True for _ in iter_items(doc)):
        out.append(Finding("error", "-", path.name,
                           "additions 下没有任何可识别条目——检查 entities/relations/"
                           "kb_cases 的拼写，以及每条是否都有 item:"))
        return False
    return True


def check_shape(doc: dict, out: list[Finding]) -> None:
    """每个条目的 target_file 与 item 是否就位。"""
    add = doc.get("additions") or {}
    for unknown in set(add) - set(SECTIONS):
        out.append(Finding("error", "-", str(unknown),
                           f"未知段名 {unknown!r}，本段全部条目会被忽略。"
                           f"合法段名：{', '.join(SECTIONS)}"))
    for section in SECTIONS:
        for i, entry in enumerate(add.get(section) or []):
            if not isinstance(entry, dict):
                out.append(Finding("error", section, f"#{i}", "条目不是映射"))
                continue
            if not isinstance(entry.get("item"), dict):
                out.append(Finding("error", section, f"#{i}",
                                   "缺 item: 或它不是映射，该条会被跳过"))
                continue
            key = item_key(section, entry["item"])
            if not entry.get("target_file"):
                wrong = next((k for k in ("target", "file", "path") if k in entry), None)
                hint = f"（发现 {wrong!r}）" if wrong else ""
                out.append(Finding("error", section, key,
                                   f"缺 target_file{hint}——apply 会打印'跳过'后继续，"
                                   "退出码仍是 0"))
            elif not (C.ROOT / str(entry["target_file"])).parent.is_dir():
                out.append(Finding("error", section, key,
                                   f"target_file 的目录不存在：{entry['target_file']}"))


def check_internal_deps(doc: dict, out: list[Finding]) -> None:
    """提案内部的落库顺序依赖。validate 只看最终状态，看不到顺序。"""
    new_ents = {i.get("id") for s, _e, i in iter_items(doc) if s == "entities"}
    for section, _entry, item in iter_items(doc):
        if section != "kb_cases":
            continue
        refs = [item.get("anomaly_ref"), item.get("detected_at_ref")]
        refs += [(c or {}).get("cause_ref") for c in item.get("possible_causes") or []]
        refs += [(a or {}).get("action_ref") for a in item.get("actions") or []]
        dep = sorted({r for r in refs if r and r in new_ents})
        if dep:
            out.append(Finding("warn", "kb_cases", item_key(section, item),
                               f"引用了本提案新增的 {len(dep)} 个实体"
                               f"（{', '.join(dep[:3])}）。apply 会把它转人工，"
                               "这是预期行为不是 bug"))


def check_lints(doc: dict, existing: dict, out: list[Finding]) -> None:
    """三类判断线索。全部 warn。"""
    new_ents = {i["id"]: i for s, _e, i in iter_items(doc)
                if s == "entities" and i.get("id")}

    for section, _entry, item in iter_items(doc):
        key = item_key(section, item)

        # 1. MTTR 措辞 + may_cause
        if section == "relations" and item.get("type") == "may_cause":
            src = item.get("from")
            ent = new_ents.get(src) or existing.get(src) or {}
            text = f"{ent.get('description') or ''} {item.get('note') or ''} " \
                   f"{(ent.get('economic_hooks') or {}).get('note') or ''}"
            hit = [w for w in MTTR_WORDS if w in text]
            if hit:
                out.append(Finding("warn", section, key,
                                   f"描述含 {hit[:2]} 等时长类措辞却用 may_cause。"
                                   "may_cause 只表示导致异常发生，不表示决定持续多久"))

        # 2. model_prior 却写了需引用的具体断言
        if (item.get("provenance") or {}).get("source_type") == "model_prior":
            fields = [item.get("description"), item.get("note"),
                      item.get("notes"), (item.get("impact") or {}).get("economic")]
            for txt in fields:
                m = CITE_PAT.search(str(txt or ""))
                if m:
                    out.append(Finding("warn", section, key,
                                       f"标 model_prior 却出现 {m.group(0)!r}——"
                                       "标准号与量化阈值属需文献支撑的外部事实"))
                    break

        # 3. kb_case 列了 cause/action 但图上没有对应边
        if section == "kb_cases" and item.get("anomaly_ref"):
            an = item["anomaly_ref"]
            new_rels = {f"{i.get('from')}|{i.get('type')}|{i.get('to')}"
                        for s, _e, i in iter_items(doc) if s == "relations"}
            try:
                live = {f"{r.get('from')}|{r.get('type')}|{r.get('to')}"
                        for r in C.load_relations()}
            except Exception:                                  # noqa: BLE001
                live = set()
            both = new_rels | live
            miss = [c for c in
                    ((x or {}).get("cause_ref") for x in item.get("possible_causes") or [])
                    if c and f"{c}|may_cause|{an}" not in both]
            miss += [a for a in
                     ((x or {}).get("action_ref") for x in item.get("actions") or [])
                     if a and f"{an}|mitigated_by|{a}" not in both]
            if miss:
                out.append(Finding("warn", section, key,
                                   f"{len(miss)} 个 ref 在图上没有对应边"
                                   f"（{', '.join(miss[:3])}）。库内约定允许，"
                                   "但补齐后风险图谱回溯才完整"))


def check_one(path: Path) -> list[Finding]:
    out: list[Finding] = []
    try:
        doc = C.load_yaml(path)
    except Exception as exc:                                   # noqa: BLE001
        return [Finding("error", "-", path.name, f"YAML 解析失败：{exc!r}")]
    if not check_envelope(doc, path, out):
        return out
    check_shape(doc, out)
    check_internal_deps(doc, out)
    try:
        existing, _dup = C.load_entities()
    except Exception:                                          # noqa: BLE001
        existing = {}
    check_lints(doc, existing, out)
    return out


def main() -> int:
    C.setup_console()
    ap = argparse.ArgumentParser(description="提案落库前的形状检查")
    ap.add_argument("paths", nargs="*", help="提案路径；省略则查 pending/ 全部")
    ap.add_argument("--json", action="store_true", dest="as_json")
    a = ap.parse_args()

    if a.paths:
        paths = [Path(p) if Path(p).is_absolute() else C.ROOT / p for p in a.paths]
        for p in paths:
            if not p.is_file():
                print(f"文件不存在：{p}")
                return 2
    else:
        paths = sorted((C.ROOT / "changesets" / "pending").glob("*.yaml"))
        if not paths:
            print("changesets/pending/ 下没有提案。")
            return 0

    results = {p: check_one(p) for p in paths}
    n_err = sum(1 for fs in results.values() for f in fs if f.level == "error")
    n_warn = sum(1 for fs in results.values() for f in fs if f.level == "warn")

    if a.as_json:
        print(json.dumps({str(p.relative_to(C.ROOT)): [f.as_dict() for f in fs]
                          for p, fs in results.items()},
                         ensure_ascii=False, indent=2))
        return 1 if n_err else 0

    for p, fs in results.items():
        e = sum(1 for f in fs if f.level == "error")
        w = len(fs) - e
        print("=" * 72)
        print(f"提案 {p.name}：ERROR {e} | WARN {w}")
        print("=" * 72)
        if not fs:
            print("  形状检查全过。")
        for f in sorted(fs, key=lambda x: 0 if x.level == "error" else 1):
            print(f"  [{f.level:<5}] [{f.section}] {f.key}")
            print(f"          {f.msg}")

    print("-" * 72)
    if n_err:
        print(f"共 ERROR {n_err} | WARN {n_warn}。有 error 不应落库。")
        return 1
    print(f"共 ERROR {n_err} | WARN {n_warn}。形状检查通过。")
    print("字段级规则由 apply 合并后的 validate 兜（失败自动回滚）。"
          "剩下需要人判断的是：provenance 标得该不该、概念是否重复、建模层次是否恰当。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
