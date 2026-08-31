"""共用加载层：本体/知识库/元模型的读取与规范化。

validate.py / build_index.py / apply_changeset.py 共用此模块，
确保三者对"什么是一条实体、一条关系"的理解完全一致。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent


def setup_console() -> None:
    """Windows 控制台默认 GBK，中文输出需显式切 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def load_yaml(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_config() -> dict:
    return load_yaml(ROOT / "config.yaml") or {}


def load_meta() -> dict:
    return load_yaml(ROOT / "ontology" / "meta-schema.yaml") or {}


def load_competency() -> dict:
    """能力问题清单：库的边界声明。缺失时返回 {}，由 R016 报错。"""
    return load_yaml(ROOT / "ontology" / "competency-questions.yaml") or {}


def _rel_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(path)


def _merge_provenance(item: dict, default: dict | None) -> dict:
    """条目级 provenance 覆盖文件级 default_provenance，逐字段合并。"""
    merged = dict(default or {})
    merged.update(item.get("provenance") or {})
    return merged


def iter_ontology_files(kind: str) -> list[Path]:
    """kind: 'entities' | 'relations'"""
    out: list[Path] = []
    for domain in ("core", "fab", "ap"):
        d = ROOT / "ontology" / domain / kind
        if d.is_dir():
            out.extend(sorted(d.glob("*.yaml")))
    return out


def load_entities() -> tuple[dict[str, dict], list[str]]:
    """返回 (id -> entity, 重复ID告警列表)。entity 内注入 _file 与合并后的 provenance。"""
    entities: dict[str, dict] = {}
    dup: list[str] = []
    for path in iter_ontology_files("entities"):
        doc = load_yaml(path) or {}
        default_prov = doc.get("default_provenance")
        for item in doc.get("entities") or []:
            if not isinstance(item, dict):
                continue
            item = dict(item)
            item["_file"] = _rel_path(path)
            item["provenance"] = _merge_provenance(item, default_prov)
            eid = item.get("id")
            if eid in entities:
                dup.append(f"{eid} (重复出现于 {entities[eid]['_file']} 与 {item['_file']})")
                continue
            if eid:
                entities[eid] = item
    return entities, dup


def load_relations() -> list[dict]:
    rels: list[dict] = []
    for path in iter_ontology_files("relations"):
        doc = load_yaml(path) or {}
        default_prov = doc.get("default_provenance")
        for item in doc.get("relations") or []:
            if not isinstance(item, dict):
                continue
            item = dict(item)
            item["_file"] = _rel_path(path)
            item["provenance"] = _merge_provenance(item, default_prov)
            rels.append(item)
    return rels


def load_kb() -> list[dict]:
    """知识库实例：kb/<domain>/*.yaml，每文件含 cases 列表。"""
    cases: list[dict] = []
    for domain in ("fab", "ap"):
        d = ROOT / "kb" / domain
        if not d.is_dir():
            continue
        for path in sorted(d.glob("*.yaml")):
            doc = load_yaml(path) or {}
            default_prov = doc.get("default_provenance")
            for item in doc.get("cases") or []:
                if not isinstance(item, dict):
                    continue
                item = dict(item)
                item["_file"] = _rel_path(path)
                item["provenance"] = _merge_provenance(item, default_prov)
                cases.append(item)
    return cases


def derived_relations(entities: dict[str, dict], meta: dict) -> list[dict]:
    """按 meta-schema.derived_relations 从实体字段展开隐式关系，避免同一事实两处维护。"""
    out: list[dict] = []
    for spec in meta.get("derived_relations") or []:
        field = spec["from_field"]
        rtype = spec["relation"]
        reverse = spec.get("direction", "entity -> value").strip().startswith("value")
        for eid, ent in entities.items():
            raw = ent.get(field)
            if raw is None:
                continue
            values = raw if isinstance(raw, list) else [raw]
            for val in values:
                a, b = (val, eid) if reverse else (eid, val)
                out.append({
                    "from": a, "type": rtype, "to": b,
                    "_derived": True, "_field": field, "_file": ent["_file"],
                })
    return out


def entity_refs(ent: dict) -> list[tuple[str, str]]:
    """返回实体内所有对其他实体的引用 [(字段名, 目标ID)]，供悬空引用检查。"""
    ref_fields = ("parameters", "metrics", "equipment", "materials", "stage", "steps")
    out: list[tuple[str, str]] = []
    for field in ref_fields:
        raw = ent.get(field)
        if raw is None:
            continue
        values = raw if isinstance(raw, list) else [raw]
        for val in values:
            if isinstance(val, str):
                out.append((field, val))
    return out


def is_external(eid: str) -> bool:
    """ext.* 为外部域（其他行业本体）引用，豁免本地解析。"""
    return isinstance(eid, str) and eid.startswith("ext.")
