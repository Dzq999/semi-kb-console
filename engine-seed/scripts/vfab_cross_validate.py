"""vFab 知识库 × 本体 交叉验证。

历史版本把 vFab 同时跟「内部特征模型/经营指标/场景产物/语义本体」四维做字符串
交集，再取算术平均。实测下来那套口径不成立：

- 内部特征模型：拿 vFab 原始异常日志的列头（equipment/phase/state_machines__* 等
  SECS/GEM 协议列）去撞特征 code，命中的是 load_port/module/wph 这类词面巧合，
  不是「特征在 vFab 里落地」。而且 vFab 目前根本没接入特征，没有特征字段可对。
- 经营指标 / 场景产物：拿 vFab 术语跟一份硬编码词表 / 随产出增长的场景词表求交，
  分母要么污染要么反向惩罚（产出越多、分数越低），不是对齐度。
- 语义本体：related_iris 与本体声明的交集，方向对，但旧口径 2/2=100% 是因为 477 条
  条目全兜底到根类 Equipment，几乎没验什么。

现版本收窄为 vFab 知识库 ↔ 本体这一条真正成立的轴，产出三个口径（不再做平均）：

1. referential_integrity（门禁）：条目 related_iris 是否都在本体中声明。
2. link_specificity（头条）：引用了『具体类』（非通用根类）的条目占比——通用根类由数据
   自动探测（被 >=90% 条目引用者视为兜底根类）。这是能靠补精确 related_iris 提升的真信号。
3. ontology_touch（信息项）：vFab 触达的 curated 本体类占比（排除 generated 自动类噪声）。

输出 build/reports/vfab-cross-validation.json。
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 被 >= 该比例条目引用的类，视为『通用兜底根类』（如 Equipment），不计入 link_specificity。
GENERIC_ROOT_RATIO = 0.90
# 样本过小时不做自动探测，避免偶然全命中被误判为根类。
GENERIC_MIN_ENTRIES = 20
# 自动生成的类模块（噪声），不计入 ontology_touch 的 curated 分母。
GENERATED_MODULE = "generated.ttl"


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


_CLASS_RE = re.compile(r"\bsemi:([A-Za-z][A-Za-z0-9_-]*)\s+a\s+owl:Class")
_TERM_RE = re.compile(
    r"\bsemi:([A-Za-z][A-Za-z0-9_-]*)\s+a\s+(?:owl:Class|owl:ObjectProperty|owl:DatatypeProperty)"
)


def declared_terms() -> tuple[set[str], set[str]]:
    """返回 (全部已声明类/属性 IRI, curated 类 IRI)。

    curated 类 = 排除 generated.ttl 后声明的 owl:Class，用作 ontology_touch 的干净分母，
    避免 639 个自动生成类稀释覆盖率。referential_integrity 仍用全集判定，宁松勿误报。
    """
    all_terms: set[str] = set()
    curated_classes: set[str] = set()
    for ttl_path in sorted((ROOT / "ontology" / "modules").glob("*.ttl")):
        text = ttl_path.read_text(encoding="utf-8")
        all_terms.update("urn:pxai:semi:" + m for m in _TERM_RE.findall(text))
        if ttl_path.name != GENERATED_MODULE:
            curated_classes.update("urn:pxai:semi:" + m for m in _CLASS_RE.findall(text))
    return all_terms, curated_classes


def load_vfab_entry_iris() -> list[set[str]]:
    """每个 vFab 知识条目的 related_iris 集合（保留条目粒度，供逐条判定特异性）。"""
    entries: list[set[str]] = []
    entries_dir = ROOT / "knowledge" / "vfab" / "entries"
    if entries_dir.is_dir():
        for path in sorted(entries_dir.glob("*.json")):
            data = load_json(path)
            iris = {str(i).strip() for i in (data.get("related_iris") or []) if str(i).strip()}
            entries.append(iris)
    return entries


def detect_generic_roots(entry_iris: list[set[str]]) -> set[str]:
    """探测通用兜底根类：被 >= GENERIC_ROOT_RATIO 比例条目引用的类。

    这些类（典型如 Equipment）几乎每条都挂，携带的对齐信息接近零，从 link_specificity
    里剔除后，特异性才反映『条目是否落到了更细的类』。样本过小时返回空集，宁可不剔。
    """
    n = len(entry_iris)
    if n < GENERIC_MIN_ENTRIES:
        return set()
    counter: Counter[str] = Counter()
    for iris in entry_iris:
        counter.update(iris)
    threshold = n * GENERIC_ROOT_RATIO
    return {iri for iri, c in counter.items() if c >= threshold}


def _short(iri: str) -> str:
    return iri.rsplit(":", 1)[-1] if ":" in iri else iri


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    print("加载 vFab 知识条目与本体声明...")
    all_terms, curated_classes = declared_terms()
    entry_iris = load_vfab_entry_iris()
    total_entries = len(entry_iris)

    # 全体引用的去重 IRI（用于门禁与触达统计）
    referenced: set[str] = set()
    for iris in entry_iris:
        referenced |= iris
    undeclared = sorted(referenced - all_terms)

    # 门禁：referential_integrity —— 引用的 IRI 是否都已声明
    ri_matched = len(referenced & all_terms)
    ri_coverage = ri_matched / len(referenced) if referenced else 1.0

    # 头条：link_specificity —— 引用了『具体类』（非通用根类）的条目占比
    generic_roots = detect_generic_roots(entry_iris)
    specific_entries = sum(1 for iris in entry_iris if (iris - generic_roots))
    specificity = specific_entries / total_entries if total_entries else 0.0

    # 信息项：ontology_touch —— vFab 触达的 curated 类占比
    touched_curated = referenced & curated_classes
    touch_coverage = len(touched_curated) / len(curated_classes) if curated_classes else 0.0

    status = "warn" if undeclared else "pass"
    report = {
        "status": status,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "axis": "vfab_knowledge x ontology",
        "summary": {
            "vfab_knowledge_entries": total_entries,
            "vfab_referenced_iris": len(referenced),
            "ontology_declared_terms": len(all_terms),
            "ontology_curated_classes": len(curated_classes),
            "generic_root_classes": sorted(_short(i) for i in generic_roots),
        },
        "headline": "link_specificity",
        "dimensions": {
            "referential_integrity": {
                "role": "gate",
                "description": "vFab 条目 related_iris 是否都在本体中声明（未声明即门禁 warn）",
                "referenced_iris": len(referenced),
                "matched_count": ri_matched,
                "coverage": round(ri_coverage, 4),
                "undeclared_iris": undeclared,
            },
            "link_specificity": {
                "role": "headline",
                "description": "引用了具体类（剔除通用兜底根类）的条目占比——衡量知识库落到本体细粒度的程度",
                "total_entries": total_entries,
                "specific_entries": specific_entries,
                "coverage": round(specificity, 4),
                "generic_root_classes": sorted(_short(i) for i in generic_roots),
            },
            "ontology_touch": {
                "role": "info",
                "description": "vFab 触达的 curated 本体类占比（排除 generated 自动类）",
                "curated_classes": len(curated_classes),
                "touched_count": len(touched_curated),
                "coverage": round(touch_coverage, 4),
                "touched_classes": sorted(_short(i) for i in touched_curated),
            },
        },
        # headline_coverage 供日报直取；不再对不可比维度做算术平均。
        "headline_coverage": round(specificity, 4),
    }

    out = ROOT / "build" / "reports" / "vfab-cross-validation.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print("\nvFab 知识库 × 本体 交叉验证完成")
    print(f"  知识条目：{total_entries} 条，引用去重 IRI：{len(referenced)} 个")
    print(f"  引用完整性（门禁）：{ri_coverage*100:.1f}%（{ri_matched}/{len(referenced)}）")
    print(f"  本体链接特异性（头条）：{specificity*100:.1f}%（{specific_entries}/{total_entries}）")
    print(f"  本体触达（信息）：{touch_coverage*100:.1f}%（{len(touched_curated)}/{len(curated_classes)} curated 类）")
    if generic_roots:
        print(f"  已识别通用兜底根类：{', '.join(sorted(_short(i) for i in generic_roots))}")
    if undeclared:
        print(f"\n⚠️  警告：{len(undeclared)} 个引用 IRI 未在本体中声明，首个：{undeclared[0]}")

    return 1 if undeclared else 0


if __name__ == "__main__":
    raise SystemExit(main())
