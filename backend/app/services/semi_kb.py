from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from rdflib import Graph, RDF, RDFS, URIRef
from rdflib.namespace import OWL
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Run, RunRound


# 反向缺口排序时用来识别"技术噪声"特征码（代理键 / 时间戳 / 纯 id 列）。
# 仅用于把业务相关缺口排到前面供 Agent 优先反思，不是硬门禁。
_NOISE_FEATURE_CODES = {"<missing_feature_code>", "_biz_ts_", "_code_"}
_NOISE_FEATURE_RE = re.compile(
    r"(?:^|_)(?:id|ids|ts|dt|seq|idx|no|num|rowkey|guid|uuid|key)$"
    r"|record_id|unique_record|_biz_ts|_time$|_ts$|^_|_$",
    re.I,
)


def _evenly_sample(items: list, k: int) -> list:
    """从已排序列表里等距抽 k 个（含首尾），k<=0 返回空，k>=len 原样返回。

    用于本体样本跨字母段均匀覆盖：stride=(n-1)/(k-1)，round 后严格递增不重复。
    """
    n = len(items)
    if k <= 0:
        return []
    if k >= n:
        return list(items)
    if k == 1:
        return [items[0]]
    step = (n - 1) / (k - 1)
    return [items[round(i * step)] for i in range(k)]


def _balanced_quota(sizes: dict[str, int], limit: int, floor: int = 1) -> dict[str, int]:
    """按各桶占比分配 limit 名额，非空桶保底 floor，余数补给最大桶，且不超过桶自身容量。"""
    total = sum(sizes.values())
    if total <= 0 or limit <= 0:
        return {key: 0 for key in sizes}
    if limit >= total:  # 名额够全取，无需采样
        return dict(sizes)
    quota = {key: (floor if size > 0 else 0) for key, size in sizes.items()}
    remaining = limit - sum(quota.values())
    if remaining > 0:  # 剩余名额按占比分（扣掉已给的 floor，避免小桶被高估）
        weight_total = sum(max(0, size - quota[key]) for key, size in sizes.items())
        if weight_total > 0:
            for key, size in sizes.items():
                head = max(0, size - quota[key])
                quota[key] += int(remaining * head / weight_total)
    # 收尾：夹到容量上限，再把仍剩的名额按桶大小降序补齐。
    for key in quota:
        quota[key] = min(quota[key], sizes[key])
    leftover = limit - sum(quota.values())
    for key in sorted(sizes, key=lambda k: sizes[k], reverse=True):
        if leftover <= 0:
            break
        room = sizes[key] - quota[key]
        take = min(room, leftover)
        quota[key] += take
        leftover -= take
    return quota


class SemiKbError(RuntimeError):
    pass


class SemiKbAdapter:
    _candidate_lock = asyncio.Lock()
    # metrics 累计量(totals/status)缓存窗口。取 20s 是安全的：累计量只随发布变化、
    # 发布会主动 invalidate_cache()，故不会读到陈旧累计；而"今日新增"(today_added)不在
    # 此缓存内、每次都重查 DB，拉长窗口不影响其刷新灵敏度。配合前端 metrics 15s 轮询，
    # 稳态下几乎每次命中缓存(0.007s)。
    _BASE_METRICS_TTL_SECONDS = 20
    def __init__(self, root: Path | None = None):
        self.root = root or settings.engine_root
        self._base_metrics_cache: tuple[float, dict] | None = None
        # 按文件 mtime 失效的图缓存：解析 1.4MB current.ttl 约 1s、modules 约 0.3s，
        # 是指标热路径最贵的一步。缓存解析后的 Graph，semantic_counts / domain_coverage
        # 共用同一份，文件没变就零解析（读路径优化，绝不参与写入/门禁）。
        self._schema_cache: tuple | None = None  # (sig, merged_schema, class_to_module, module_stats)
        self._data_cache: tuple | None = None    # (sig, data_graph)

    def invalidate_cache(self) -> None:
        self._base_metrics_cache = None
        # 发布会重写 current.ttl，其 mtime 变化本就会自动刷新；此处一并清空，确保发布后立即读到新图。
        self._schema_cache = None
        self._data_cache = None

    @staticmethod
    def _graph_sig(paths: list[Path]) -> tuple:
        return tuple((p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in paths)

    def _load_schema_graph(self) -> tuple[Graph, dict, dict]:
        """解析 ontology/modules/*.ttl 一次并按 mtime 缓存。

        返回 (合并后的 schema 图, class→模块 stem 映射, 每模块 class/property 计数)。
        返回的图是**共享只读**对象，调用方绝不可就地修改（下次刷新会整体重建并换引用，
        老读者仍持旧图，天然线程安全）。"""
        paths = sorted((self.root / "ontology" / "modules").glob("*.ttl"))
        sig = self._graph_sig(paths)
        if self._schema_cache and self._schema_cache[0] == sig:
            return self._schema_cache[1], self._schema_cache[2], self._schema_cache[3]
        schema = Graph()
        declared_module: dict[URIRef, str] = {}
        prop_counts: dict[str, int] = {}
        for path in paths:
            module = Graph()
            module.parse(path, format="turtle")
            schema += module
            classes = set(module.subjects(RDF.type, OWL.Class))
            props = set(module.subjects(RDF.type, OWL.ObjectProperty)) | set(module.subjects(RDF.type, OWL.DatatypeProperty))
            prop_counts[path.stem] = len(props)
            for cls in classes:
                declared_module[cls] = path.stem
        # 领域归属增强：generated/common 里声明、但沿 rdfs:subClassOf 上溯能抵达某策展领域模块根类的
        # 类，重归属到该领域模块（读侧归属，不改本体文件、不碰门禁；自维持——新自动生成的领域子类会
        # 自动计入其领域）。声明在领域模块的类保持不动。
        class_to_module = self._resolve_domain_modules(schema, declared_module)
        # module_stats 的 class 计数依"解析后归属"重算（properties 仍按声明文件计），使 domain_coverage
        # 的 类/实例 两栏口径一致：领域模块吸收其派生类，generated/common 相应收缩。
        class_counts: dict[str, int] = {}
        for stem in class_to_module.values():
            class_counts[stem] = class_counts.get(stem, 0) + 1
        module_stats: dict[str, dict[str, int]] = {
            stem: {"classes": class_counts.get(stem, 0), "properties": prop_counts.get(stem, 0)}
            for stem in prop_counts
        }
        self._schema_cache = (sig, schema, class_to_module, module_stats)
        return schema, class_to_module, module_stats

    def _resolve_domain_modules(self, schema: Graph, declared_module: dict) -> dict:
        """把非策展模块（generated/common）里声明、但经 rdfs:subClassOf 可上溯到某策展领域模块根类
        的类，重归属到该领域模块；其余保持声明模块。返回新映射，不改入参。纯结构推导、无副作用。"""
        labels = self._DOMAIN_MODULE_LABELS
        resolved = dict(declared_module)
        for cls, stem in declared_module.items():
            if stem in labels:
                continue
            target = self._nearest_domain_module(cls, schema, declared_module)
            if target:
                resolved[cls] = target
        return resolved

    def _nearest_domain_module(self, cls, schema: Graph, declared_module: dict):
        """沿 rdfs:subClassOf 逐层 BFS 上溯，返回最近祖先所在的策展领域模块 stem；无则 None。
        父类归属以"声明文件"为准（领域根声明在领域模块）；同层命中多个领域根取字典序最小以保确定性；
        seen 去环。"""
        labels = self._DOMAIN_MODULE_LABELS
        seen = {cls}
        frontier = [cls]
        while frontier:
            hits, nxt = [], []
            for node in frontier:
                for parent in schema.objects(node, RDFS.subClassOf):
                    if not isinstance(parent, URIRef) or parent in seen:
                        continue
                    seen.add(parent)
                    if declared_module.get(parent) in labels:
                        hits.append(declared_module[parent])
                    nxt.append(parent)
            if hits:
                return sorted(hits)[0]
            frontier = nxt
        return None

    def _load_data_graph(self) -> Graph:
        """解析 knowledge/semantic 下的 current.ttl（纯实例）+ provenance.ttl（溯源）并按 mtime 缓存。

        两文件合并加载：溯源已从 current.ttl 物理分离，但来源拆分（sourceType 经
        prov:Entity--specializationOf-->个体 回填）等指标仍需读到溯源节点，故此处再并回，
        使各项指标口径与分离前逐条一致。共享只读，调用方绝不可就地修改。"""
        base = self.root / "knowledge" / "semantic"
        paths = [p for p in (base / "current.ttl", base / "provenance.ttl") if p.is_file()]
        if not paths:
            return Graph()
        sig = self._graph_sig(paths)
        if self._data_cache and self._data_cache[0] == sig:
            return self._data_cache[1]
        data = Graph()
        for path in paths:
            data.parse(path, format="turtle")
        self._data_cache = (sig, data)
        return data

    def _ensure_root(self) -> None:
        if not (self.root / "scripts" / "kb.py").is_file():
            raise SemiKbError(f"semi-kb 路径无效：{self.root}")

    async def command(self, *args: str, timeout: int = 1800) -> dict:
        self._ensure_root()
        started = time.monotonic()
        command = [sys.executable, str(self.root / "scripts" / args[0]), *args[1:]]
        child_env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        if os.name == "nt":
            holder: dict[str, subprocess.Popen] = {}

            def run_windows() -> tuple[int, bytes, bytes]:
                process = subprocess.Popen(
                    command, cwd=str(self.root), env=child_env,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                holder["process"] = process
                try:
                    stdout, stderr = process.communicate(timeout=timeout)
                except subprocess.TimeoutExpired:
                    process.kill(); stdout, stderr = process.communicate()
                    raise SemiKbError(f"命令超时：{' '.join(args)}") from None
                return process.returncode, stdout, stderr

            task = asyncio.create_task(asyncio.to_thread(run_windows))
            try:
                exit_code, stdout, stderr = await task
            except asyncio.CancelledError:
                process = holder.get("process")
                if process and process.poll() is None:
                    process.kill()
                try: await task
                except (Exception, asyncio.CancelledError): pass
                raise
            output = (stdout + stderr).decode("utf-8", "replace")
            return {"exit_code": exit_code, "duration_seconds": round(time.monotonic() - started, 3), "output": output}
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(self.root),
            env=child_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            raise SemiKbError(f"命令超时：{' '.join(args)}") from None
        except asyncio.CancelledError:
            process.kill()
            await process.communicate()
            raise
        output = (stdout + stderr).decode("utf-8", "replace")
        return {"exit_code": process.returncode, "duration_seconds": round(time.monotonic() - started, 3), "output": output}

    async def status(self) -> dict:
        result = await self.command("kb.py", "status", "--json", timeout=120)
        if result["exit_code"]:
            raise SemiKbError(result["output"].strip() or "无法读取 semi-kb 状态")
        try:
            return json.loads(result["output"])
        except json.JSONDecodeError as exc:
            raise SemiKbError("semi-kb 状态不是合法 JSON") from exc

    async def validate(self, full: bool = True, defer_derived: bool = False) -> dict:
        # full=False → --quick（基线/非发布快检，本就跳派生物）。
        # full=True + defer_derived=True → --defer-derived：发布门禁子集，跳 build_index/
        # scenario_mine（派生物），regress 等一致性判定环节全保留；派生物发布后另行补跑。
        if not full:
            args = ["kb.py", "check", "--quick"]
        elif defer_derived:
            args = ["kb.py", "check", "--defer-derived"]
        else:
            args = ["kb.py", "check"]
        result = await self.command(*args)
        result["passed"] = result["exit_code"] == 0
        return result

    async def refresh_derived(self) -> dict:
        """发布成功后 best-effort 刷新派生物（检索索引/场景卡）并并回 current.trig。

        这三步（build_index/scenario_mine/migrate_semantic）不参与发布 PASS/FAIL，已从
        每轮门禁关键路径移出。此处失败只记录、返回 passed=False，绝不回滚已发布内容、
        也绝不把成功的发布翻成失败。受 settings.refresh_derived_after_publish 控制。
        """
        try:
            result = await self.command("kb.py", "refresh-derived", timeout=1800)
        except SemiKbError as exc:
            return {"passed": False, "exit_code": None, "output": str(exc), "duration_seconds": None}
        result["passed"] = result["exit_code"] == 0
        return result

    async def _refresh_derived_after_publish(self, result: dict) -> None:
        """发布成功后按 settings.refresh_derived_after_publish best-effort 补跑派生物刷新，
        把结果记进 result["checks"]["refresh_derived"]；绝不抛异常、绝不影响已发布内容。"""
        if not settings.refresh_derived_after_publish:
            result["checks"]["refresh_derived"] = {"passed": True, "skipped": True, "output": "refresh_derived_after_publish=off"}
            return
        refreshed = await self.refresh_derived()
        result["checks"]["refresh_derived"] = {
            "passed": refreshed["passed"],
            "duration_seconds": refreshed.get("duration_seconds"),
            "output": (refreshed.get("output") or "")[-4000:],
        }

    def ontology_context(self, limit: int = 500, *, balanced: bool = False) -> dict:
        """本体术语上下文。

        balanced=False（默认，供导入门禁/编排的 target_class 白名单）：按 (kind, iri)
        排序取前 limit，class 优先，保持原有语义不变。
        balanced=True（供问答接地）：按 kind 占比分配名额、每类内跨字母段等距采样，
        避免样本偏科到「只有 A–D 段的 class、property 一条不入」。
        两种模式都附全量 by_kind 计数与 sampled/strategy 标注，便于消费方（尤其 LLM）
        据实说明覆盖口径，不必从样本反推分布。
        """
        schema = Graph()
        for path in sorted((self.root / "ontology" / "modules").glob("*.ttl")):
            schema.parse(path, format="turtle")
        buckets: dict[str, list[dict]] = {"class": [], "object_property": [], "datatype_property": []}
        for rdf_type, kind in ((OWL.Class, "class"), (OWL.ObjectProperty, "object_property"), (OWL.DatatypeProperty, "datatype_property")):
            for subject in schema.subjects(RDF.type, rdf_type):
                label = next(schema.objects(subject, RDFS.label), None)
                buckets[kind].append({"iri": str(subject), "label": str(label) if label else str(subject), "kind": kind})
        by_kind = {kind: len(items) for kind, items in buckets.items()}
        total = sum(by_kind.values())
        for items in buckets.values():
            items.sort(key=lambda item: item["iri"])

        if not balanced:
            terms = sorted(
                (item for items in buckets.values() for item in items),
                key=lambda item: (item["kind"], item["iri"]),
            )[:limit]
        else:
            quota = _balanced_quota(by_kind, limit)
            picked = [t for kind, items in buckets.items() for t in _evenly_sample(items, quota[kind])]
            terms = sorted(picked, key=lambda item: (item["kind"], item["iri"]))

        return {
            "terms": terms,
            "total": total,
            "by_kind": by_kind,
            "sampled": len(terms) < total,
            "strategy": "balanced" if balanced else "kind_iri_order",
        }

    def business_context(self) -> dict:
        models = []
        for path in sorted((self.root / "business" / "models").glob("*.yaml")):
            try:
                document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except (OSError, yaml.YAMLError):
                continue
            models.append({"path": path.relative_to(self.root).as_posix(), "document": document})
        scenarios = []
        for path in sorted((self.root / "simulation" / "scenarios").glob("*.yaml")):
            try:
                document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except (OSError, yaml.YAMLError):
                continue
            scenarios.append({"path": path.relative_to(self.root).as_posix(), "document": document})
        return {"models": models, "existing_scenario_examples": scenarios[:6]}

    def existing_candidate_ids(self, limit: int = 1500) -> dict:
        """已入库的知识条目 ID 与自动规则 ID 清单，供 Agent 去重规避。

        知识条目 ID 是内容派生的语义 slug（非 run 作用域），自动规则 ID 亦为内容派生，
        两者都会与历史轮次的库存相撞——Agent 若无既有清单便无从查重，只能反复重提同名
        概念，触发服务端隔离。这里把清单注入 prompt，让 Agent 主动改用更具体命名或不提出。
        与 ontology_context 同构：排序后按 limit 截断并附全量计数/截断标记，便于据实说明。
        """
        knowledge_dir = self.root / "knowledge" / "entries"
        knowledge_ids = sorted(p.stem for p in knowledge_dir.glob("*.json")) if knowledge_dir.is_dir() else []
        rules_path = self.root / "ontology" / "rules" / "registry.json"
        rule_ids: list[str] = []
        if rules_path.is_file():
            try:
                registry = json.loads(rules_path.read_text(encoding="utf-8"))
                rules = registry.get("rules", []) if isinstance(registry, dict) else registry
                rule_ids = sorted(str(item.get("rule_id")) for item in (rules or []) if isinstance(item, dict) and item.get("rule_id"))
            except (OSError, json.JSONDecodeError):
                rule_ids = []
        # 名额在两类间按占比分配，避免一类挤占另一类；各自内部等距采样保持覆盖面。
        k_total, r_total = len(knowledge_ids), len(rule_ids)
        grand = k_total + r_total
        if grand <= limit:
            picked_k, picked_r = knowledge_ids, rule_ids
        else:
            k_quota = max(1, round(limit * k_total / grand)) if k_total else 0
            r_quota = max(0, limit - k_quota)
            picked_k = _evenly_sample([{"id": i} for i in knowledge_ids], k_quota)
            picked_r = _evenly_sample([{"id": i} for i in rule_ids], r_quota)
            picked_k = [d["id"] for d in picked_k]
            picked_r = [d["id"] for d in picked_r]
        return {
            "knowledge_ids": picked_k,
            "rule_ids": picked_r,
            "knowledge_total": k_total,
            "rule_total": r_total,
            "truncated": grand > limit,
        }

    def vfab_knowledge_context(self, samples_per_family: int = 12) -> dict:
        """把 knowledge/vfab/entries 的散文知识整理成可引用的检索上下文。

        供研究 Agent 在提出变更/知识条目时能真正引用 SEMI 标准与设备手册，而不是
        编造来源。落实用户「vFab 只进检索层」的决定：这里只把知识作为**证据线索**
        提供给 LLM，不生成本体断言、不进推理层。

        NDA 安全：classification=restricted 的条目（设备手册，含 NDA 正文）只贡献
        结构性元数据（title / source_ref / terms），**绝不外泄 content 或 summary**。
        非受限条目（SEMI 标准）可带一句 summary 摘要。

        按可引用的来源族（如 "SEMI E5" / 设备手册型号）聚合，返回计数 + 若干引用
        句柄样本，控制 prompt 体积；477 条不逐条铺开。
        """
        entries_dir = self.root / "knowledge" / "vfab" / "entries"
        if not entries_dir.is_dir():
            return {"families": [], "total": 0, "note": "尚无 vFab 知识条目"}

        families: dict[str, dict] = {}
        total = 0
        for path in sorted(entries_dir.glob("*.json")):
            try:
                entry = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            total += 1
            prov = entry.get("provenance") or {}
            restricted = prov.get("classification") == "restricted"
            # 引用族：优先用 provenance.source_ref 的前缀（"SEMI E5 S6F11" -> "SEMI E5"），
            # 手册用其型号名；退化到 source_type。
            src_ref = str(prov.get("source_ref") or "").strip()
            if src_ref.upper().startswith("SEMI "):
                family = " ".join(src_ref.split()[:2])  # "SEMI E5"
            elif restricted:
                family = src_ref or "设备手册"
            else:
                family = src_ref or str(prov.get("source_type") or "vfab")
            fam = families.setdefault(family, {
                "family": family, "kind": "equipment_manual" if restricted else "standard_spec",
                "classification": "restricted" if restricted else (prov.get("classification") or "internal"),
                "count": 0, "citations": [],
            })
            fam["count"] += 1
            if restricted:
                fam["classification"] = "restricted"
            # 引用句柄：受限条目只给标题（结构性、非机密），非受限再附一句摘要。
            if len(fam["citations"]) < samples_per_family:
                handle = {"ref": src_ref or entry.get("id"), "title": entry.get("title")}
                if not restricted:
                    summary = str(entry.get("summary") or "")[:80]
                    if summary:
                        handle["gist"] = summary
                fam["citations"].append(handle)
        ordered = sorted(families.values(), key=lambda f: (-f["count"], f["family"]))
        return {
            "families": ordered,
            "total": total,
            "usage": "可在 knowledge_entries.source_refs 引用这些来源族作为证据；restricted 来源正文仅本地留存，勿在输出中复制其正文。",
        }

    async def cross_validate(self) -> dict:
        checks = {}
        for name, script, args in (
            ("source_alignment", "align_sources.py", ("--check",)),
            ("capability", "capability_validate.py", ()),
        ):
            result = await self.command(script, *args, timeout=300)
            checks[name] = {"passed": result["exit_code"] == 0, "duration_seconds": result["duration_seconds"], "output": result["output"][-4000:]}
        checks["passed"] = all(item.get("passed") for item in checks.values() if isinstance(item, dict))
        return checks

    def candidate_alignment(self, semantic_sources: list[Path]) -> dict:
        feature_catalog_path = self.root / "build" / "source" / "feature-model-catalog.json"
        entity_map_path = self.root / "mappings" / "feature-model" / "entity-map.json"
        property_map_path = self.root / "mappings" / "feature-model" / "property-map.json"
        vfab_path = self.root / "build" / "source" / "vfab-catalog.json"
        feature_catalog = json.loads(feature_catalog_path.read_text(encoding="utf-8")) if feature_catalog_path.is_file() else {}
        entity_map = json.loads(entity_map_path.read_text(encoding="utf-8")) if entity_map_path.is_file() else {}
        property_map = json.loads(property_map_path.read_text(encoding="utf-8")) if property_map_path.is_file() else {}
        vfab = json.loads(vfab_path.read_text(encoding="utf-8")) if vfab_path.is_file() else {"status": "awaiting_source", "datasets": []}
        internal_targets = {str(item.get("target_class")) for item in entity_map.get("mappings") or [] if item.get("target_class")}
        internal_targets.update(str(item.get("target_property")) for item in property_map.get("mappings") or [] if item.get("target_property"))
        source_terms = {str(item.get("name") or "").casefold() for item in feature_catalog.get("entities") or []}
        source_terms.update(str(item.get("code") or "").casefold() for item in feature_catalog.get("entities") or [])
        vfab_targets = {str(item.get("target_class")) for item in vfab.get("datasets") or [] if item.get("target_class")}
        rows = []
        for path in semantic_sources:
            document = json.loads(path.read_text(encoding="utf-8"))
            additions = document.get("additions") or {}
            for section in ("classes", "object_properties", "datatype_properties"):
                for item in additions.get(section) or []:
                    iri, label = str(item.get("iri") or ""), str(item.get("label_zh") or "")
                    token = iri.rsplit(":", 1)[-1].casefold()
                    internal_state = "mapped" if iri in internal_targets else ("lexical_match" if token in source_terms or label.casefold() in source_terms else "not_found")
                    rows.append({"iri": iri, "label": label, "kind": section, "internal_feature_state": internal_state, "vfab_state": "matched" if iri in vfab_targets else vfab.get("status", "awaiting_source")})
        return {
            "passed": True,
            "internal_feature_source": feature_catalog.get("source_id", "missing"),
            "vfab_state": vfab.get("status", "awaiting_source"),
            "terms_checked": len(rows),
            "internal_supported": sum(row["internal_feature_state"] != "not_found" for row in rows),
            "rows": rows,
            "note": "not_found 表示内部特征未覆盖，不等于外部证据无效；vFab 未提供时始终保持 awaiting_source。",
        }

    @staticmethod
    def _safe_stem(value: str) -> str:
        return re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-.")[:120] or "candidate"

    @staticmethod
    def _json_from_output(output: str) -> dict:
        """从子进程输出（command() 已合并 stdout+stderr）里取出脚本打印的 JSON 对象。

        优先整体解析；失败则从后往前找第一段完整的 {...} 行，兼容脚本前有零星 stderr 噪声。
        """
        text = (output or "").strip()
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
        for line in reversed(text.splitlines()):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    parsed = json.loads(line)
                    if isinstance(parsed, dict):
                        return parsed
                except json.JSONDecodeError:
                    continue
        raise SemiKbError("草案校验脚本未返回合法 JSON：" + text[-500:])

    async def validate_business_draft(self, model_path: Path) -> dict:
        """对单份草案 model 运行 validate_business_draft.py，返回 {passed, errors, outputs}。

        model_path 可为绝对（须在引擎根内）或相对引擎根的路径；ref 相对引擎根解析，
        故 business/drafts/<id>/model.yaml 的自指 template_ref/dataset_ref 可就地校验。
        """
        self._ensure_root()
        rel = model_path.relative_to(self.root) if model_path.is_absolute() else Path(model_path)
        result = await self.command("validate_business_draft.py", rel.as_posix(), timeout=300)
        payload = self._json_from_output(result["output"])
        return {
            "passed": bool(payload.get("passed")),
            "errors": [str(item) for item in (payload.get("errors") or [])],
            "outputs": payload.get("outputs") or {},
            "duration_seconds": result["duration_seconds"],
        }

    async def promote_business_draft(self, draft_dir: Path) -> dict:
        """把一份已校验的草案三件套晋升为线上经营基线（人点『采纳为基线』后调用）。

        持 _candidate_lock 与 process_candidates 串行，避免两个写者同时改动全库门禁看到的
        business/models/。改写 model 的 template_ref/dataset_ref 指向线上文件（template.extends
        已指向线上基座、保持不变）；写入后跑 simulate_check.py 全库门禁，任何失败都只回滚
        本次写入的三个文件，绝不留下半成品。成功即失效缓存，新 pair 随即进入闭环 approved_pairs。
        """
        self._ensure_root()
        draft_dir = draft_dir if draft_dir.is_absolute() else (self.root / draft_dir)
        template_doc = yaml.safe_load((draft_dir / "template.yaml").read_text(encoding="utf-8")) or {}
        dataset_doc = yaml.safe_load((draft_dir / "dataset.yaml").read_text(encoding="utf-8")) or {}
        model_doc = yaml.safe_load((draft_dir / "model.yaml").read_text(encoding="utf-8")) or {}
        template = template_doc.get("template") or {}
        dataset = dataset_doc.get("dataset") or {}
        model = model_doc.get("model") or {}
        template_id = str(template.get("id") or "")
        dataset_id = str(dataset.get("id") or "")
        model_id = str(model.get("id") or "")
        if not (template_id and dataset_id and model_id):
            raise SemiKbError("草案缺少 template/dataset/model 的 id，无法晋升")
        async with self._candidate_lock:
            templates_dir = self.root / "business" / "templates"
            datasets_dir = self.root / "business" / "datasets"
            models_dir = self.root / "business" / "models"
            for directory in (templates_dir, datasets_dir, models_dir):
                directory.mkdir(parents=True, exist_ok=True)
            template_path = templates_dir / f"{self._safe_stem(template_id)}.yaml"
            dataset_path = datasets_dir / f"{self._safe_stem(dataset_id)}.yaml"
            model_path = models_dir / f"{self._safe_stem(model_id)}.yaml"
            for target in (template_path, dataset_path, model_path):
                if target.exists():
                    raise SemiKbError(f"线上已存在同名基线文件：{target.relative_to(self.root).as_posix()}")
            # 草案里 model 的 ref 指向 drafts/ 自身；晋升时改写为线上文件（extends 不动）。
            model["template_ref"] = template_path.relative_to(self.root).as_posix()
            model["dataset_ref"] = dataset_path.relative_to(self.root).as_posix()
            written: list[Path] = []
            try:
                template_path.write_text(yaml.safe_dump(template_doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
                written.append(template_path)
                dataset_path.write_text(yaml.safe_dump(dataset_doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
                written.append(dataset_path)
                model_path.write_text(yaml.safe_dump(model_doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
                written.append(model_path)
                gate = await self.command("simulate_check.py", timeout=600)
                if gate["exit_code"]:
                    raise SemiKbError("晋升后全库经营模型门禁失败：" + gate["output"][-3000:])
            except BaseException:
                for path in written:
                    path.unlink(missing_ok=True)
                raise
            self.invalidate_cache()
            return {
                "template_path": template_path.relative_to(self.root).as_posix(),
                "dataset_path": dataset_path.relative_to(self.root).as_posix(),
                "model_path": model_path.relative_to(self.root).as_posix(),
                "gate_output": gate["output"][-2000:],
            }

    async def semantic_precheck_sources(self, sources: list[Path]) -> dict:
        """Run one isolated semantic changeset precheck for a source group.

        Used by partial publication to locate incompatible candidates without
        invoking the full OWL/SHACL/business/simulation pipeline for each file.
        The temporary files are always removed under the candidate lock.
        """
        sources = [path for path in sources if path.is_file()]
        if not sources:
            return {"passed": True, "duration_seconds": 0.0, "output": ""}
        self._ensure_root()
        async with self._candidate_lock:
            pending = self.root / "semantic_changesets" / "pending"
            pending.mkdir(parents=True, exist_ok=True)
            staged: list[Path] = []
            try:
                for index, source in enumerate(sources, 1):
                    target = pending / f"console-probe-{self._safe_stem(source.parent.name)}-{self._safe_stem(source.stem)}-{index}.json"
                    await asyncio.to_thread(shutil.copy2, source, target)
                    staged.append(target)
                return await self.command("apply_semantic_changeset.py", "--check", timeout=600)
            finally:
                for path in staged:
                    path.unlink(missing_ok=True)

    async def publish_auxiliary_candidates(self, candidates: dict[str, list[Path]]) -> dict:
        """Validate and publish knowledge/rule/article artifacts without rerunning
        the heavyweight semantic and simulation gates.

        These artifacts have independent local contracts and provenance checks;
        coupling them to a full OWL merge made partial publication needlessly
        slow and caused valid knowledge entries to wait behind one bad semantic
        candidate.
        """
        self._ensure_root()
        knowledge_sources = [path for path in candidates.get("knowledge", []) if path.is_file()]
        rule_sources = [path for path in candidates.get("rules", []) if path.is_file()]
        article_sources = [path for path in candidates.get("articles", []) if path.is_file()]
        result = {"published": False, "published_knowledge": [], "published_rules": [], "published_articles": [], "quarantined_candidates": [], "checks": {"auxiliary_contract": {"passed": True, "duration_seconds": 0.0}}}
        async with self._candidate_lock:
            knowledge_dir = self.root / "knowledge" / "entries"; knowledge_dir.mkdir(parents=True, exist_ok=True)
            rules_path = self.root / "ontology" / "rules" / "registry.json"
            rule_dir = self.root / "ontology" / "rules" / "generated"; rule_dir.mkdir(parents=True, exist_ok=True)
            registry_backup = rules_path.read_bytes() if rules_path.is_file() else None
            registry = {"rules": []}
            try:
                if rules_path.is_file():
                    loaded = json.loads(rules_path.read_text(encoding="utf-8")); registry = loaded if isinstance(loaded, dict) else {"rules": loaded}
                existing = {str(item.get("rule_id")) for item in registry.get("rules") or [] if isinstance(item, dict)}
                staged_knowledge: list[tuple[Path, Path]] = []
                staged_rules: list[tuple[dict, Path]] = []
                for source in knowledge_sources:
                    try:
                        entry = json.loads(source.read_text(encoding="utf-8")); entry_id = str(entry.get("id") or "")
                        if not re.match(r"^urn:pxai:semi:knowledge:[A-Za-z0-9._:%-]+$", entry_id) or len(str(entry.get("content") or "")) < 40:
                            raise ValueError("知识条目缺少合法 id 或 content 少于 40 字")
                        target = knowledge_dir / f"{self._safe_stem(entry_id)}.json"
                        if target.exists(): raise ValueError(f"知识条目 ID 已存在：{entry_id}")
                        staged_knowledge.append((source, target))
                    except Exception as exc:
                        result["quarantined_candidates"].append({"path": str(source), "category": "knowledge", "reason": str(exc)})
                for source in rule_sources:
                    try:
                        rule = json.loads(source.read_text(encoding="utf-8")); rule_id = str(rule.get("rule_id") or ""); query = str(rule.get("query") or "")
                        if not re.match(r"^R-AUTO-[A-Za-z0-9._-]+$", rule_id) or rule_id in existing or not rule.get("name"):
                            raise ValueError("规则 ID 重复或缺少名称")
                        if rule.get("implementation") != "sparql" or not re.search(r"(?is)\bconstruct\b", query):
                            raise ValueError("规则必须是只读 SPARQL CONSTRUCT")
                        if re.search(r"\b(?:INSERT|DELETE|LOAD|CLEAR|DROP|CREATE|MOVE|COPY|ADD)\b", query, re.I):
                            raise ValueError("SPARQL 规则包含危险更新语句")
                        Graph().query(query)
                        target = rule_dir / f"{self._safe_stem(rule_id)}.rq"
                        if target.exists(): raise ValueError(f"规则文件已存在：{target.name}")
                        staged_rules.append((rule, target)); existing.add(rule_id)
                    except Exception as exc:
                        result["quarantined_candidates"].append({"path": str(source), "category": "rule", "reason": str(exc)})
                for source, target in staged_knowledge:
                    await asyncio.to_thread(shutil.copy2, source, target); result["published_knowledge"].append(target.relative_to(self.root).as_posix())
                registry.setdefault("rules", [])
                for rule, target in staged_rules:
                    target.write_text(str(rule.get("query") or "").strip() + "\n", encoding="utf-8")
                    registered = {key: value for key, value in rule.items() if key != "query"}; registered["implementation"] = target.relative_to(self.root).as_posix(); registry["rules"].append(registered); result["published_rules"].append(str(rule.get("rule_id")))
                if staged_rules:
                    rules_path.parent.mkdir(parents=True, exist_ok=True); rules_path.write_text(json.dumps(registry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                article_dir = self.root / "knowledge" / "articles" / "agent-rounds"; article_dir.mkdir(parents=True, exist_ok=True)
                for index, source in enumerate(article_sources, 1):
                    target = article_dir / f"{self._safe_stem(source.parents[2].name if len(source.parents) > 2 else 'round')}-{self._safe_stem(source.parent.name)}-{index}.md"
                    if not target.exists(): await asyncio.to_thread(shutil.copy2, source, target)
                    result["published_articles"].append(target.relative_to(self.root).as_posix())
                result["published"] = bool(result["published_knowledge"] or result["published_rules"] or result["published_articles"])
                result["accepted_candidates"] = {"knowledge": len(result["published_knowledge"]), "rules": len(result["published_rules"]), "articles": len(result["published_articles"])}
                self.invalidate_cache()
                return result
            except BaseException:
                for _, target in locals().get("staged_knowledge", []): target.unlink(missing_ok=True)
                for _, target in locals().get("staged_rules", []): target.unlink(missing_ok=True)
                if registry_backup is None: rules_path.unlink(missing_ok=True)
                else: rules_path.write_bytes(registry_backup)
                raise

    async def process_candidates(self, candidates: dict[str, list[Path]], publish: bool, *, semantic_batch_prechecked: bool = False) -> dict:
        """Stage candidates under the engine lock, run all gates, and optionally publish.

        Semantic publication is delegated to semi-kb's atomic merger. Simulation files are
        staged first so the same full-chain validation sees them, and removed on any failure.

        ``semantic_batch_prechecked`` 由 ``partial_publish`` 传入：调用方刚对**完全相同的
        语义候选批次**跑过一次整图 ``--check`` 且整批通过。current.ttl / 本体模块 /
        current.trig 与 pending 内容在两次调用之间不会被改动（cross_validate、property-map
        合并都不进入语义门禁的输入），因此可以跳过这里的批量预检,避免对同一张大图重复推理。
        最终的 apply(683 行)仍是权威门禁并会在失败时回滚——被跳过的只是预检,不是发布门禁。
        """
        self._ensure_root()
        semantic_sources = [path for path in candidates.get("semantic", []) if path.is_file()]
        business_sources = [path for path in candidates.get("business", []) if path.is_file()]
        simulation_sources = [path for path in candidates.get("simulation", []) if path.is_file()]
        knowledge_sources = [path for path in candidates.get("knowledge", []) if path.is_file()]
        rule_sources = [path for path in candidates.get("rules", []) if path.is_file()]
        article_sources = [path for path in candidates.get("articles", []) if path.is_file()]
        mapping_sources = [path for path in candidates.get("mappings", []) if path.is_file()]
        result: dict = {"published": False, "semantic_candidates": len(semantic_sources), "business_candidates": len(business_sources), "simulation_candidates": len(simulation_sources), "knowledge_candidates": len(knowledge_sources), "rule_candidates": len(rule_sources), "mapping_candidates": len(mapping_sources), "quarantined_candidates": [], "checks": {}}
        if not semantic_sources and not business_sources and not simulation_sources and not knowledge_sources and not rule_sources and not mapping_sources:
            result["accepted_candidates"] = {"semantic": 0, "business": 0, "simulation": 0, "knowledge": 0, "rules": 0, "mappings": 0}
            result["rejected_candidates"] = 0
            result["checks"] = await self.cross_validate()
            result["checks"]["candidate_precheck"] = {"passed": True, "output": "本轮没有非空语义或仿真候选"}
            baseline = await self.validate(full=publish, defer_derived=publish)
            result["checks"]["full_publish_gate" if publish else "baseline_gate"] = {"passed": baseline["passed"], "duration_seconds": baseline["duration_seconds"], "output": baseline["output"][-8000:]}
            if not baseline["passed"]:
                raise SemiKbError("现有知识工程全链门禁失败")
            if publish and article_sources:
                article_dir = self.root / "knowledge" / "articles" / "agent-rounds"
                article_dir.mkdir(parents=True, exist_ok=True)
                published_articles = []
                for index, source in enumerate(article_sources, 1):
                    round_name = source.parents[2].name if len(source.parents) > 2 else "round"
                    target = article_dir / f"{self._safe_stem(round_name)}-{self._safe_stem(source.parent.name)}-{index}.md"
                    if not target.exists(): await asyncio.to_thread(shutil.copy2, source, target)
                    published_articles.append(target.relative_to(self.root).as_posix())
                result["published"] = True
                result["published_articles"] = published_articles
                self.invalidate_cache()
                await self._refresh_derived_after_publish(result)
            return result
        async with self._candidate_lock:
            pending = self.root / "semantic_changesets" / "pending"
            business_dir = self.root / "business" / "models"
            simulation_dir = self.root / "simulation" / "scenarios"
            pending.mkdir(parents=True, exist_ok=True)
            business_dir.mkdir(parents=True, exist_ok=True)
            simulation_dir.mkdir(parents=True, exist_ok=True)
            # apply_semantic_changeset.py 用 PENDING.glob("*.json") 处理整个目录，
            # 因此上一轮/上一次 run 崩溃留下的残留文件会被本轮一起校验，导致本轮
            # 无缘无故失败。这里持有 _candidate_lock，pending/ 理应为空；不为空即为
            # 孤儿，移到 orphans/ 保留证据而不是直接删除。
            orphans = [item for item in pending.glob("*.json")]
            if orphans:
                orphan_dir = self.root / "semantic_changesets" / "orphans" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                orphan_dir.mkdir(parents=True, exist_ok=True)
                for item in orphans:
                    await asyncio.to_thread(shutil.move, str(item), str(orphan_dir / item.name))
                result.setdefault("orphaned_candidates", []).extend(item.name for item in orphans)
            knowledge_dir = self.root / "knowledge" / "entries"
            knowledge_dir.mkdir(parents=True, exist_ok=True)
            staged_semantic: list[Path] = []
            staged_business: list[Path] = []
            staged_simulation: list[Path] = []
            staged_knowledge: list[tuple[Path, Path]] = []
            staged_rules: list[dict] = []
            staged_rule_paths: list[Path] = []
            semantic_applied = False
            rules_backup: bytes | None = None
            rules_registry_modified = False
            rules_path = self.root / "ontology" / "rules" / "registry.json"
            property_map_path = self.root / "mappings" / "feature-model" / "property-map.json"
            property_map_backup: bytes | None = None
            property_map_modified = False
            try:
                # Stage the complete semantic batch and run the expensive
                # changeset precheck once.  The previous implementation invoked
                # this command once per file, multiplying a 1–3 minute check by
                # every candidate.  Candidate-level isolation is retained as a
                # fallback only when the batch precheck actually fails.
                semantic_pairs: list[tuple[Path, Path]] = []
                for index, source in enumerate(semantic_sources, 1):
                    target = pending / f"console-{self._safe_stem(source.parent.name)}-{self._safe_stem(source.stem)}-{index}.json"
                    if target.exists():
                        raise SemiKbError(f"语义暂存文件冲突：{target.name}")
                    await asyncio.to_thread(shutil.copy2, source, target)
                    semantic_pairs.append((source, target))
                # 整批预检是否以「无隔离」的方式通过：只有此时,后续语义预检(646 行)
                # 面对的 pending 集合与这里完全一致,才能安全跳过那一遍整图推理。
                batch_precheck_passed = False
                if semantic_pairs and semantic_batch_prechecked:
                    # 调用方(partial_publish)已对同一批次跑过整图 --check 且整批通过,
                    # 直接复用,省掉这一遍整图推理。
                    batch_precheck_passed = True
                    staged_semantic.extend(target for _, target in semantic_pairs)
                    result["checks"]["semantic_batch_precheck"] = {"passed": True, "duration_seconds": 0.0, "output": "复用 partial_publish 的整批预检结果"}
                elif semantic_pairs:
                    batch_check = await self.command("apply_semantic_changeset.py", "--check", timeout=600)
                    if batch_check["exit_code"] == 0:
                        batch_precheck_passed = True
                        staged_semantic.extend(target for _, target in semantic_pairs)
                    else:
                        # Isolate only on a real batch failure.  This keeps the
                        # normal path O(1) expensive checks while preserving the
                        # existing guarantee that one bad candidate is isolated.
                        # 隔离意味着 staged_semantic 是子集,后续语义预检不能跳过。
                        for source, target in semantic_pairs:
                            target.unlink(missing_ok=True)
                        for index, source in enumerate(semantic_sources, 1):
                            target = pending / f"console-isolate-{self._safe_stem(source.parent.name)}-{self._safe_stem(source.stem)}-{index}.json"
                            await asyncio.to_thread(shutil.copy2, source, target)
                            single_check = await self.command("apply_semantic_changeset.py", "--check", timeout=600)
                            if single_check["exit_code"]:
                                result["quarantined_candidates"].append({"path": str(source), "category": "semantic", "reason": single_check["output"][-3000:]})
                                target.unlink(missing_ok=True)
                            else:
                                staged_semantic.append(target)
                # 经营模型/仿真候选的 ID 撞车不再中止整批（旧行为一撞就 raise，让本轮
                # 其余合规产物一起被回滚）。改为与 knowledge/rules 一致的幂等策略：
                #   目标已存在且内容字节一致 → 静默跳过（重复去重，非覆盖，不越『不改写已入库』红线）；
                #   目标已存在但内容不同     → 隔离该候选（绝不覆盖已发布内容），不中止其余候选。
                # 跳过/隔离都不得把目标计入 staged_*，否则回滚会误删属于上一次发布的文件。
                for index, source in enumerate(business_sources, 1):
                    doc = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
                    model_id = str((doc.get("model") or {}).get("id") or f"candidate-{index}")
                    target = business_dir / f"{self._safe_stem(model_id)}.yaml"
                    if target.exists():
                        if source.read_bytes() != target.read_bytes():
                            result["quarantined_candidates"].append({"path": str(source), "category": "business", "reason": f"经营模型候选 ID 已存在且内容不同：{model_id}"})
                        continue
                    await asyncio.to_thread(shutil.copy2, source, target)
                    staged_business.append(target)
                for index, source in enumerate(simulation_sources, 1):
                    doc = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
                    scenario_id = str((doc.get("scenario") or {}).get("id") or f"candidate-{index}")
                    target = simulation_dir / f"{self._safe_stem(scenario_id)}.yaml"
                    if target.exists():
                        if source.read_bytes() != target.read_bytes():
                            result["quarantined_candidates"].append({"path": str(source), "category": "simulation", "reason": f"仿真候选 ID 已存在且内容不同：{scenario_id}"})
                        continue
                    await asyncio.to_thread(shutil.copy2, source, target)
                    staged_simulation.append(target)

                for source in knowledge_sources:
                    try:
                        entry = json.loads(source.read_text(encoding="utf-8"))
                        entry_id = str(entry.get("id") or "")
                        if not re.match(r"^urn:pxai:semi:knowledge:[A-Za-z0-9._:%-]+$", entry_id) or len(str(entry.get("content") or "")) < 40:
                            raise ValueError("知识条目缺少合法 id 或 content 少于 40 字")
                        target = knowledge_dir / f"{self._safe_stem(entry_id)}.json"
                        if target.exists():
                            # 内容派生 slug 与历史库存相撞：字节一致则静默去重跳过，
                            # 不同则隔离（不覆盖已入库条目）。旧行为对一切撞车一律隔离，噪声大。
                            if source.read_bytes() != target.read_bytes():
                                raise ValueError(f"知识条目 ID 已存在且内容不同：{entry_id}")
                            continue
                        staged_knowledge.append((source, target))
                    except (OSError, ValueError, json.JSONDecodeError) as exc:
                        result["quarantined_candidates"].append({"path": str(source), "category": "knowledge", "reason": str(exc)})

                existing_rules: set[str] = set()
                registry = {"rules": []}
                if rules_path.is_file():
                    registry = json.loads(rules_path.read_text(encoding="utf-8"))
                    registry = registry if isinstance(registry, dict) else {"rules": registry}
                    existing_rules = {str(item.get("rule_id")) for item in registry.get("rules") or [] if isinstance(item, dict)}
                for source in rule_sources:
                    try:
                        rule = json.loads(source.read_text(encoding="utf-8"))
                        rule_id = str(rule.get("rule_id") or "")
                        implementation = str(rule.get("implementation") or "")
                        query = str(rule.get("query") or "")
                        if not re.match(r"^R-AUTO-[A-Za-z0-9._-]+$", rule_id) or rule_id in existing_rules or not rule.get("name"):
                            raise ValueError("规则 ID 重复或缺少名称")
                        if implementation != "sparql":
                            raise ValueError("规则 implementation 不受支持")
                        if implementation == "sparql" and not re.search(r"(?is)\bconstruct\b", query):
                            raise ValueError("SPARQL 规则必须是 CONSTRUCT")
                        if re.search(r"\b(?:INSERT|DELETE|LOAD|CLEAR|DROP|CREATE|MOVE|COPY|ADD)\b", query, re.I):
                            raise ValueError("SPARQL 规则只能是只读查询")
                        Graph().query(query)
                        rule["tests"] = [
                            str(test_ref) for test_ref in (rule.get("tests") or [])
                            if ".." not in Path(str(test_ref).split("::", 1)[0]).parts
                            and (self.root / str(test_ref).split("::", 1)[0]).is_file()
                        ]
                        staged_rules.append(rule); existing_rules.add(rule_id)
                    except Exception as exc:  # Candidate-local parse errors must not abort the round.
                        result["quarantined_candidates"].append({"path": str(source), "category": "rule", "reason": str(exc)})

                result["accepted_candidates"] = {
                    "semantic": len(staged_semantic), "business": len(staged_business),
                    "simulation": len(staged_simulation), "knowledge": len(staged_knowledge),
                    "rules": len(staged_rules),
                }
                result["rejected_candidates"] = len(result["quarantined_candidates"])

                # 映射候选:在既有 cross_validate（align_sources --check）门禁之前合并进
                # property-map.json。预 sanitize 已保证目标已声明、码真实且未占用，故门禁必过；
                # 发布轮保留、非发布轮在 finally 还原、任何异常在 except 还原。
                accepted_mappings = 0
                if mapping_sources:
                    property_map_backup = property_map_path.read_bytes() if property_map_path.is_file() else None
                    merged, accepted_mappings, mapping_quarantine = self._merge_property_mappings(mapping_sources)
                    result["quarantined_candidates"].extend(mapping_quarantine)
                    if accepted_mappings:
                        property_map_path.parent.mkdir(parents=True, exist_ok=True)
                        property_map_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                        property_map_modified = True
                result["accepted_candidates"]["mappings"] = accepted_mappings
                result["rejected_candidates"] = len(result["quarantined_candidates"])

                cross = await self.cross_validate()
                result["checks"].update(cross)
                if not cross["passed"]:
                    raise SemiKbError("内部特征/vFab 或能力问题交叉验证失败")
                result["checks"]["candidate_source_alignment"] = self.candidate_alignment(semantic_sources)
                if staged_semantic and batch_precheck_passed:
                    # 整批已在上面通过整图预检,且 staged_semantic 就是同一批(未发生隔离)。
                    # 其间 cross_validate / property-map 合并都不进入语义门禁输入,pending 集合
                    # 不变,因此这一遍必然同样通过——直接复用,省掉一次整图推理。
                    result["checks"]["semantic_precheck"] = {"passed": True, "duration_seconds": 0.0, "output": "复用整批预检结果(候选与 pending 集合未变)"}
                elif staged_semantic:
                    check = await self.command("apply_semantic_changeset.py", "--check", timeout=600)
                    result["checks"]["semantic_precheck"] = {"passed": check["exit_code"] == 0, "duration_seconds": check["duration_seconds"], "output": check["output"][-5000:]}
                    if check["exit_code"]:
                        raise SemiKbError("语义变更集预检失败：" + check["output"][-3000:])
                simulation = await self.command("simulate_check.py", timeout=600)
                result["checks"]["business_simulation"] = {"passed": simulation["exit_code"] == 0, "duration_seconds": simulation["duration_seconds"], "output": simulation["output"][-5000:]}
                if simulation["exit_code"]:
                    raise SemiKbError("经营模型或仿真候选校验失败：" + simulation["output"][-3000:])

                # 逐候选跑只读 simulate.py（不带 --output：各自读候选、结果打 stdout、无共享写）→ 有界并行。
                # 批语义保持「任一失败即整批失败」，并确定性报告最小 index 的失败（等价原「首失即停」）。
                sem = asyncio.Semaphore(max(1, settings.simulate_concurrency))

                async def _run_sim(path: Path) -> dict:
                    async with sem:
                        run = await self.command("simulate.py", str(path.relative_to(self.root)), timeout=300)
                    return {"path": path.relative_to(self.root).as_posix(), "passed": run["exit_code"] == 0, "output": run["output"][-8000:]}

                sim_results = await asyncio.gather(*(_run_sim(path) for path in staged_simulation), return_exceptions=True)
                simulation_runs = []
                failure_message = None
                for path, outcome in zip(staged_simulation, sim_results):
                    if isinstance(outcome, asyncio.CancelledError):
                        raise outcome
                    if isinstance(outcome, BaseException):
                        simulation_runs.append({"path": path.relative_to(self.root).as_posix(), "passed": False, "output": str(outcome)[-8000:]})
                        if failure_message is None:
                            failure_message = f"仿真执行异常：{path.name}：{outcome}"
                        continue
                    simulation_runs.append(outcome)
                    if not outcome["passed"] and failure_message is None:
                        failure_message = f"仿真执行失败：{path.name}"
                result["simulation_runs"] = simulation_runs
                if failure_message is not None:
                    raise SemiKbError(failure_message)

                if publish:
                    if staged_rules:
                        rules_backup = rules_path.read_bytes() if rules_path.is_file() else None
                        rule_dir = self.root / "ontology" / "rules" / "generated"
                        rule_dir.mkdir(parents=True, exist_ok=True)
                        registered_rules = []
                        for rule in staged_rules:
                            target = rule_dir / f"{self._safe_stem(str(rule.get('rule_id')))}.rq"
                            if target.exists():
                                raise SemiKbError(f"规则文件已存在：{target.name}")
                            target.write_text(str(rule.get("query") or "").strip() + "\n", encoding="utf-8")
                            staged_rule_paths.append(target)
                            registered = {key: value for key, value in rule.items() if key != "query"}
                            registered["implementation"] = target.relative_to(self.root).as_posix()
                            registered_rules.append(registered)
                        registry.setdefault("rules", []).extend(registered_rules)
                        rules_path.parent.mkdir(parents=True, exist_ok=True)
                        rules_path.write_text(json.dumps(registry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                        rules_registry_modified = True
                    if staged_semantic:
                        applied = await self.command("apply_semantic_changeset.py", timeout=1800)
                        result["checks"]["full_publish_gate"] = {"passed": applied["exit_code"] == 0, "duration_seconds": applied["duration_seconds"], "output": applied["output"][-8000:]}
                        if applied["exit_code"]:
                            raise SemiKbError("全链发布门禁失败，语义数据已由引擎回滚：" + applied["output"][-5000:])
                        semantic_applied = True
                    else:
                        full = await self.validate(full=True, defer_derived=True)
                        result["checks"]["full_publish_gate"] = {"passed": full["passed"], "duration_seconds": full["duration_seconds"], "output": full["output"][-8000:]}
                        if not full["passed"]:
                            raise SemiKbError("仿真发布全链门禁失败：" + full["output"][-5000:])
                    article_dir = self.root / "knowledge" / "articles" / "agent-rounds"
                    article_dir.mkdir(parents=True, exist_ok=True)
                    published_articles = []
                    for index, source in enumerate(article_sources, 1):
                        round_name = source.parents[2].name if len(source.parents) > 2 else "round"
                        target = article_dir / f"{self._safe_stem(round_name)}-{self._safe_stem(source.parent.name)}-{index}.md"
                        if not target.exists():
                            await asyncio.to_thread(shutil.copy2, source, target)
                        published_articles.append(target.relative_to(self.root).as_posix())
                    result["published"] = True
                    result["published_articles"] = published_articles
                    result["published_business_models"] = [path.relative_to(self.root).as_posix() for path in staged_business]
                    result["published_simulations"] = [path.relative_to(self.root).as_posix() for path in staged_simulation]
                    for source, target in staged_knowledge:
                        await asyncio.to_thread(shutil.copy2, source, target)
                    result["published_knowledge"] = [target.relative_to(self.root).as_posix() for _, target in staged_knowledge]
                    result["published_rules"] = [str(rule.get("rule_id")) for rule in staged_rules]
                    result["published_mappings"] = accepted_mappings
                    self.invalidate_cache()
                    # 语义路径（:834）已由 apply_semantic_changeset.py 在引擎内跑过 refresh-derived，
                    # 此处只为无语义发布路径（仿真/规则）补跑，避免重复刷新。
                    if not semantic_applied:
                        await self._refresh_derived_after_publish(result)
                else:
                    result["checks"]["publish_policy"] = {"passed": True, "output": "用户未开启自动发布；候选仅保存在控制台产物目录"}
                return result
            except BaseException:
                for path in staged_business:
                    path.unlink(missing_ok=True)
                for path in staged_simulation:
                    path.unlink(missing_ok=True)
                for _, target in staged_knowledge:
                    target.unlink(missing_ok=True)
                for path in staged_rule_paths:
                    path.unlink(missing_ok=True)
                if rules_registry_modified:
                    if rules_backup is None:
                        rules_path.unlink(missing_ok=True)
                    else:
                        rules_path.write_bytes(rules_backup)
                if property_map_modified:
                    if property_map_backup is None:
                        property_map_path.unlink(missing_ok=True)
                    else:
                        property_map_path.write_bytes(property_map_backup)
                raise
            finally:
                if not publish or not semantic_applied:
                    for path in staged_semantic:
                        path.unlink(missing_ok=True)
                if not publish:
                    for path in staged_business:
                        path.unlink(missing_ok=True)
                    for path in staged_simulation:
                        path.unlink(missing_ok=True)
                    for _, target in staged_knowledge:
                        target.unlink(missing_ok=True)
                    if property_map_modified:
                        if property_map_backup is None:
                            property_map_path.unlink(missing_ok=True)
                        else:
                            property_map_path.write_bytes(property_map_backup)

    def semantic_counts(self) -> dict[str, int]:
        self._ensure_root()
        schema, class_to_module, _ = self._load_schema_graph()
        data = self._load_data_graph()
        classes = set(schema.subjects(RDF.type, OWL.Class))
        object_properties = set(schema.subjects(RDF.type, OWL.ObjectProperty))
        datatype_properties = set(schema.subjects(RDF.type, OWL.DatatypeProperty))
        # 顶部『实例 Individual』只数真实业务/领域实例，分两步剔除：
        # (1) 按 rdf:type 去结构噪声(owl:*/owl:Ontology)与溯源/具体化记账(prov:Entity/rdf:Statement)；
        # (2) 再剔除引擎自证台账——主语的全部类型都命中 governance 词表、且无一可归入策展领域模块。
        # 台账/记账节点仍物理存在于图中，此处仅不计入头部（读侧口径，不改数据、完全可逆）。
        subj_types: dict = {}
        for s, _, o in data.triples((None, RDF.type, None)):
            if o in self._INSTANCE_NOISE_TYPES:
                continue
            subj_types.setdefault(s, set()).add(o)
        governance = {
            s for s, types in subj_types.items()
            if not any(class_to_module.get(t) in self._DOMAIN_MODULE_LABELS for t in types)
            and all(self._is_governance_type(t) for t in types)
        }
        individuals = set(subj_types) - governance
        source_split = self._individual_source_split(data)
        segment_split = self._segment_split(data)
        system_split = self._source_system_split(data)
        relation_assertions = sum(1 for subject, predicate, obj in data if predicate in object_properties and subject != obj)
        axiom_predicates = {OWL.equivalentClass, OWL.disjointWith, OWL.inverseOf, OWL.onProperty, OWL.cardinality, OWL.qualifiedCardinality, OWL.minQualifiedCardinality, OWL.maxQualifiedCardinality}
        axioms = sum(1 for _, predicate, _ in schema if predicate in axiom_predicates)
        rules_path = self.root / "ontology" / "rules" / "registry.json"
        rules = 0
        if rules_path.is_file():
            doc = json.loads(rules_path.read_text(encoding="utf-8"))
            rules = len(doc if isinstance(doc, list) else (doc.get("rules") or []))
        result = {
            "classes": len(classes),
            "properties": len(object_properties) + len(datatype_properties),
            "object_properties": len(object_properties),
            "datatype_properties": len(datatype_properties),
            "individuals": len(individuals),
            "individuals_domain": source_split["domain"],
            "individuals_knowledge": source_split["knowledge"],
            "individuals_operational": source_split["operational"],
            "individuals_untagged": source_split["untagged"],
            "relation_assertions": relation_assertions,
            "axioms": axioms,
            "rules": rules,
            "semantic_triples": len(schema) + len(data),
        }
        # processSegment 拆分：动态展开 by_segment 字典为 segment_<seg>: count 平铺键值对，便于前端/日报取数
        for seg, count in segment_split["by_segment"].items():
            result[f"segment_{seg}"] = count
        result["segment_cross"] = segment_split["cross"]
        # 级联"地基"：源系统(sourceSystem)一级维度拆分，展开为 system_<sys>: count 平铺键
        for system, count in system_split["by_system"].items():
            result[f"system_{system}"] = count
        result["system_unmapped"] = system_split["unmapped"]
        return result

    # 溯源来源类型：model_prior/web/assumption 三值，均属"知识"来源（agent 先验 / web 检索 / 推定假设）。
    # 未来接入真实产线/导入数据后，会出现这三值以外的 sourceType，届时归入"产线数据"。
    _KNOWLEDGE_SOURCE_TYPES = {"model_prior", "web", "assumption"}
    _SOURCE_TYPE_PRED = URIRef("urn:pxai:semi:sourceType")

    def _individual_source_split(self, data: Graph) -> dict[str, int]:
        """把策展模块个体按 sourceType 拆成 知识 / 产线数据 / 未标注 三类（纯只读、无副作用）。

        统计基数与 _segment_split 对齐：只统计能映射到策展领域模块的实例，排除
        业务推理层（BusinessVariable、SimulationScenario 等）与溯源/结构噪声。
        sourceType 有两条到达路径：
        (1) 直接挂在个体上；(2) 经 prov:Entity --prov:specializationOf--> 个体 回指。两路合并取并集。
        当前库内 sourceType 只有 model_prior/web/assumption（均属知识），故 operational 恒为 0，
        如实反映"尚未接入真实产线数据"。传入已缓存的 data graph，避免二次解析。
        """
        prov_spec = URIRef("http://www.w3.org/ns/prov#specializationOf")
        noise = self._INSTANCE_NOISE_TYPES

        # 与 _segment_split 相同的过滤逻辑：只保留能映射到策展领域模块的个体
        _, class_to_module, _ = self._load_schema_graph()
        domain_individuals = set()
        for s, _, o in data.triples((None, RDF.type, None)):
            if o in noise:
                continue
            module = class_to_module.get(o)
            if module and module in self._DOMAIN_MODULE_LABELS:
                domain_individuals.add(s)

        # 在这些策展模块个体上统计 sourceType 分布
        direct: dict = {}
        for subj, _, obj in data.triples((None, self._SOURCE_TYPE_PRED, None)):
            direct.setdefault(subj, set()).add(str(obj))
        chain: dict = {}
        for ent, _, indiv in data.triples((None, prov_spec, None)):
            for val in direct.get(ent, ()):  # prov:Entity 携带的 sourceType 回指领域个体
                chain.setdefault(indiv, set()).add(val)

        knowledge = operational = untagged = 0
        for indiv in domain_individuals:
            vals = direct.get(indiv, set()) | chain.get(indiv, set())
            if not vals:
                untagged += 1
            elif vals - self._KNOWLEDGE_SOURCE_TYPES:  # 出现任何非知识来源即计产线数据
                operational += 1
            else:
                knowledge += 1
        return {"domain": len(domain_individuals), "knowledge": knowledge, "operational": operational, "untagged": untagged}

    _SEGMENT_PRED = URIRef("urn:pxai:semi:processSegment")

    def _segment_split(self, data: Graph) -> dict:
        """把去噪后的领域个体按 processSegment 动态拆分（值开放、可扩展）。

        processSegment 直接挂在个体上（YAML default_attributes 编译生成），当前有 fab/ap 两值，
        未来可能新增 test/packaging 等。无此属性的个体属跨段通用（如设备域、厂务域）。
        去噪口径与 domain_coverage 对齐：只统计能映射到策展领域模块的实例，排除
        业务推理层（BusinessVariable、SimulationScenario 等）与溯源/结构噪声。

        返回 {"domain": 总数, "by_segment": {"fab": n, "ap": m, ...}, "cross": 无标注个体数}。
        """
        noise = self._INSTANCE_NOISE_TYPES
        _, class_to_module, _ = self._load_schema_graph()
        # 只统计能映射到策展领域模块的实例（与 domain_coverage 逻辑对齐）
        domain_individuals = set()
        for s, _, o in data.triples((None, RDF.type, None)):
            if o in noise:
                continue
            module = class_to_module.get(o)
            if module and module in self._DOMAIN_MODULE_LABELS:
                domain_individuals.add(s)
        by_segment: dict[str, int] = {}
        cross = 0
        for indiv in domain_individuals:
            seg_vals = {str(o) for _, _, o in data.triples((indiv, self._SEGMENT_PRED, None))}
            if seg_vals:
                seg = next(iter(seg_vals))  # 理论上单值；多值取首个
                by_segment[seg] = by_segment.get(seg, 0) + 1
            else:
                cross += 1
        return {"domain": len(domain_individuals), "by_segment": by_segment, "cross": cross}

    def _source_system_split(self, data: Graph) -> dict:
        """级联"地基"——把领域个体按一级维度 sourceSystem 拆分（制造/ERP，值开放可扩展）。

        与 processSegment(制造侧二级维度)正交：sourceSystem 是「这条知识来自哪个业务系统」的
        顶层归属。读侧纯派生——不在个体上打 sourceSystem 标，而是经 类→模块→源系统 两跳映射
        （class_to_module + _SOURCE_SYSTEM_MODULES 反查）聚合，零数据侵入、完全可逆。
        去噪与统计基数与 _segment_split / domain_coverage 完全对齐（只数策展模块个体）。

        返回 {"domain": 总数, "by_system": {"manufacturing": n, "erp": m}, "unmapped": 未归属数}。
        unmapped 恒应为 0（每个策展模块都在 _SOURCE_SYSTEM_MODULES 里有归属）；非 0 即配置漏登，
        测试据此守护"新增领域模块必须同时登记源系统"这条级联不变式。
        """
        noise = self._INSTANCE_NOISE_TYPES
        _, class_to_module, _ = self._load_schema_graph()
        # 模块 → 源系统 反查表（一个模块只属一个源系统）
        module_to_system = {
            module: system
            for system, modules in self._SOURCE_SYSTEM_MODULES.items()
            for module in modules
        }
        # 个体 → 其所属源系统集合（经类→模块→系统两跳；同一个体多类型时取并集）
        indiv_systems: dict = {}
        for s, _, o in data.triples((None, RDF.type, None)):
            if o in noise:
                continue
            module = class_to_module.get(o)
            if module and module in self._DOMAIN_MODULE_LABELS:
                system = module_to_system.get(module)
                if system:
                    indiv_systems.setdefault(s, set()).add(system)
                else:
                    indiv_systems.setdefault(s, set())  # 已登记领域但漏登源系统 → unmapped
        by_system: dict[str, int] = {}
        unmapped = 0
        for _, systems in indiv_systems.items():
            if systems:
                # 理论上单系统；跨系统个体（若未来出现）计入每个命中系统
                for system in systems:
                    by_system[system] = by_system.get(system, 0) + 1
            else:
                unmapped += 1
        return {"domain": len(indiv_systems), "by_system": by_system, "unmapped": unmapped}

    def source_system_cascade(self) -> dict:
        """级联"地基"的层级视图：源系统(一级) → 工艺段(制造侧二级) + 领域模块清单，供前端下钻渲染。

        一趟扫描领域个体（唯一主语计，去噪口径与三处拆分完全一致），同时确定每个个体的
        源系统(类→模块→系统两跳)与工艺段(processSegment 属性)，聚合出嵌套结构。
        每个系统给出：总实例数、工艺段分布(fab/ap/跨段)、其下领域模块清单(含类数/实例数)。
        纯只读、不触库、不改数据；文件缺失时优雅降级为空。与 semantic_counts 的 system_* 平铺键
        同源同口径（系统 total == system_<sys>），前端可任选平铺卡或层级视图，数字一致。
        """
        self._ensure_root()
        noise = self._INSTANCE_NOISE_TYPES
        schema, class_to_module, cached_stats = self._load_schema_graph()
        data = self._load_data_graph()
        module_to_system = {
            module: system
            for system, modules in self._SOURCE_SYSTEM_MODULES.items()
            for module in modules
        }
        # 个体 → (所属模块集合)：只保留策展领域个体
        indiv_modules: dict = {}
        for s, _, o in data.triples((None, RDF.type, None)):
            if o in noise:
                continue
            module = class_to_module.get(o)
            if module and module in self._DOMAIN_MODULE_LABELS:
                indiv_modules.setdefault(s, set()).add(module)
        # 每个个体的工艺段（单值；无标注记为跨段 cross）
        def _segment_of(indiv) -> str:
            for _, _, seg in data.triples((indiv, self._SEGMENT_PRED, None)):
                return str(seg)
            return "cross"

        # 聚合：系统 → {total, segments{seg:n}, modules{stem:instances}}
        systems_agg: dict[str, dict] = {}
        for indiv, modules in indiv_modules.items():
            hit_systems = {module_to_system[m] for m in modules if m in module_to_system}
            seg = _segment_of(indiv)
            for system in hit_systems:
                agg = systems_agg.setdefault(system, {"total": 0, "segments": {}, "modules": {}})
                agg["total"] += 1
                agg["segments"][seg] = agg["segments"].get(seg, 0) + 1
            for m in modules:
                system = module_to_system.get(m)
                if system:
                    agg = systems_agg.setdefault(system, {"total": 0, "segments": {}, "modules": {}})
                    agg["modules"][m] = agg["modules"].get(m, 0) + 1
        # 组装输出：系统按总量降序，模块按实例数降序；带中文标签与类数
        seg_labels = {"fab": "前段厂 (fab)", "ap": "后段厂 (ap)", "cross": "跨段通用"}
        systems_out = []
        for system, labels_key in ((k, k) for k in self._SOURCE_SYSTEM_MODULES):
            agg = systems_agg.get(system, {"total": 0, "segments": {}, "modules": {}})
            modules_out = []
            for stem in self._SOURCE_SYSTEM_MODULES[system]:
                stat = cached_stats.get(stem) or {"classes": 0}
                modules_out.append({
                    "module": stem,
                    "label": self._DOMAIN_MODULE_LABELS.get(stem, stem),
                    "classes": int(stat.get("classes", 0)),
                    "instances": int(agg["modules"].get(stem, 0)),
                })
            modules_out.sort(key=lambda m: (m["instances"], m["classes"]), reverse=True)
            segments_out = [
                {"segment": seg, "label": seg_labels.get(seg, seg), "instances": n}
                for seg, n in sorted(agg["segments"].items(), key=lambda kv: kv[1], reverse=True)
            ]
            systems_out.append({
                "system": system,
                "label": self._SOURCE_SYSTEM_LABELS.get(system, system),
                "total": int(agg["total"]),
                "segments": segments_out,
                "modules": modules_out,
            })
        systems_out.sort(key=lambda s: s["total"], reverse=True)
        return {
            "systems": systems_out,
            "domain_total": sum(s["total"] for s in systems_out),
        }

    # 顶部『实例』计数与三处领域拆分共用的"非实例类型"噪声集：结构公理(owl:Class/*Property)、
    # 本体头节点(owl:Ontology)、溯源/具体化记账(prov:Entity / rdf:Statement)。四处口径统一到此常量，
    # 避免逐处内联漂移。（owl:Ontology 是唯一的本体声明头节点，非业务实例。）
    _PROV_ENTITY = URIRef("http://www.w3.org/ns/prov#Entity")
    _INSTANCE_NOISE_TYPES = frozenset({
        OWL.Class, OWL.ObjectProperty, OWL.DatatypeProperty, OWL.Ontology,
        RDF.Statement, _PROV_ENTITY,
    })

    # 引擎自证/治理台账类（SHACL 形状、本体校验运行/报告、发布门禁、覆盖度与映射缺口发现、
    # 黄金基线记录、各类结构约束与校验发现等）——由管线自动断言进 current.ttl，是"引擎给自己记的账"、
    # 非业务/领域实例。顶部『实例』计数在此剔除：仅当某主语的全部 rdf:type 都命中此词表（按类
    # local name 子串匹配）、且无任一类型可归入策展领域模块时，整体判为台账并排除（读侧、不改数据、
    # 完全可逆）。边界故意从严对齐引擎内部产物；未来新增引擎产物类型时在此扩充。
    _GOVERNANCE_CLASS_KEYWORDS = (
        "SHACL", "Shape", "OntologyValidation", "OntologyIntegrity",
        "ValidationFinding", "ValidationResult", "ValidationRun", "ValidationReport",
        "ValidationProfile", "ValidationSuite", "ValidationCheck", "Constraint",
        "ReleaseGate", "ReleaseReadiness", "CoverageGate", "CoverageMetric",
        "FeatureMappingGap", "FeatureMappingCoverage", "FeatureMappingBacklog",
        "MetricGroup", "Golden", "ReasoningConsistency", "AsymmetricRelation",
        "MissingInverse", "InverseAsymmetry", "EvidenceCompleteness", "EvidenceCoverage",
        "Scorecard", "ScoreCardRecord", "Backlog", "IncompletenessFinding",
        "UncoveredAnomaly",
    )

    @staticmethod
    def _local_name(term) -> str:
        return str(term).split(":")[-1].split("#")[-1].split("/")[-1]

    def _is_governance_type(self, cls) -> bool:
        name = self._local_name(cls)
        return any(kw in name for kw in self._GOVERNANCE_CLASS_KEYWORDS)

    # 手工策展的专业领域模块 → 中文标签；common/generated 属基础设施(不计入领域)。
    # 制造侧 12 个 + ERP 侧 2 个（财务会计域 / 订单到收款域）。领域归属经 sourceSystem
    # 级联分两级：制造(fab/ap 工艺段)与 ERP(财务/O2C) 为一级源系统，见 _SOURCE_SYSTEM_MODULES。
    _DOMAIN_MODULE_LABELS = {
        "risk-diagnosis": "风险诊断",
        "equipment": "设备",
        "quality-metrology": "质量量测",
        "material-product": "物料产品",
        "application-scenario": "应用场景",
        "process-route": "工艺流程",
        "business-simulation": "经营仿真",
        "maintenance": "维护保养",
        "capacity-performance": "产能绩效",
        "facility": "厂务设施",
        "organization": "组织",
        "wip-production": "在制生产",
        "erp-financial": "ERP财务会计",
        "erp-sales-o2c": "ERP订单到收款",
    }
    _INFRA_MODULES = {"common", "generated"}

    # 级联"地基"：源系统(sourceSystem)为一级维度、工艺段(processSegment)为制造侧二级维度。
    # 每个策展领域模块归属一个源系统；读侧按模块 stem 聚合，不在个体上打标（零侵入、可逆）。
    # manufacturing = 制造/MES 侧 12 模块；erp = ERP(SAP FI+SD) 侧 2 模块。未来接入更多源系统
    # （如 MES、PLM）在此扩充即可，semantic_counts 会自动展开 system_<key> 平铺键。
    _SOURCE_SYSTEM_MODULES = {
        "manufacturing": {
            "risk-diagnosis", "equipment", "quality-metrology", "material-product",
            "application-scenario", "process-route", "business-simulation", "maintenance",
            "capacity-performance", "facility", "organization", "wip-production",
        },
        "erp": {"erp-financial", "erp-sales-o2c"},
    }
    _SOURCE_SYSTEM_LABELS = {"manufacturing": "制造/MES", "erp": "ERP/SAP"}

    def domain_coverage(self) -> dict:
        """按本体模块聚合『领域覆盖』：每个专业领域的类数与落地实例数，供日报领域覆盖小节取数。

        - 领域实例：current.ttl 中按 rdf:type 映射回『该类归属的模块』计数（归属经 subClassOf 祖先
          增强：generated/common 里声明、但上溯可达某策展领域根的类，计入该领域），已排除 prov:Entity /
          rdf:Statement / owl:* 等溯源与结构噪声，故与全局 individuals 总量口径不同。
        - auto_generated 现仅剩"未能归入任一策展领域"的自动生成类残余（引擎治理台账 + 仅挂通用根
          common:Entity/skos:Concept 的记录），不混入策展领域，避免掩盖手工领域的真实分布。
          business_baselines 来自 business/models 下的 *-baseline.yaml。
        纯只读、不触库；文件缺失时优雅降级为零。metrics() 不在热路径调用它，仅日报生成时取用。
        """
        self._ensure_root()
        noise = self._INSTANCE_NOISE_TYPES
        _, class_to_module, cached_stats = self._load_schema_graph()
        # 复制一份带 instances 计数器的本地统计，绝不就地改动共享缓存里的 module_stats。
        module_stats: dict[str, dict[str, int]] = {
            stem: {"classes": stat["classes"], "properties": stat["properties"], "instances": 0}
            for stem, stat in cached_stats.items()
        }
        for subject, _, obj in self._load_data_graph().triples((None, RDF.type, None)):
            if obj in noise:
                continue
            module = class_to_module.get(obj)
            if module and module in module_stats:
                module_stats[module]["instances"] += 1
        domains = []
        for module, label in self._DOMAIN_MODULE_LABELS.items():
            stat = module_stats.get(module) or {"classes": 0, "properties": 0, "instances": 0}
            domains.append({"module": module, "label": label, "classes": stat["classes"], "instances": stat["instances"]})
        domains.sort(key=lambda item: (item["instances"], item["classes"]), reverse=True)
        generated = module_stats.get("generated") or {"classes": 0, "instances": 0}
        baselines = []
        baseline_labels = {"ap-baseline": "应用场景", "eqp-baseline": "设备", "fab-baseline": "Fab产线", "fac-baseline": "厂务"}
        for path in sorted((self.root / "business" / "models").glob("*-baseline.yaml")):
            baselines.append(baseline_labels.get(path.stem, path.stem))
        human_models = len(list((self.root / "business" / "models").glob("business.human.*.yaml")))
        return {
            "domains": domains,
            "domain_total_classes": sum(item["classes"] for item in domains),
            "domain_total_instances": sum(item["instances"] for item in domains),
            "domains_with_instances": sum(1 for item in domains if item["instances"] > 0),
            "empty_domains": [item["label"] for item in domains if item["instances"] == 0],
            "auto_generated_classes": int(generated.get("classes", 0)),
            "auto_generated_instances": int(generated.get("instances", 0)),
            "business_baselines": baselines,
            "business_human_models": human_models,
        }

    def artifact_counts(self) -> dict[str, int | str]:
        scenario_path = self.root / "knowledge" / "scenarios" / "current.json"
        scenario_count = 0
        if scenario_path.is_file():
            scenario_count = int(json.loads(scenario_path.read_text(encoding="utf-8")).get("scenario_count", 0))
        business_models = list((self.root / "business" / "models").glob("*.yaml"))
        business_relations = 0
        for path in business_models:
            try:
                doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                model = doc.get("model") or doc
                business_relations += len(model.get("outputs") or [])
            except (OSError, yaml.YAMLError):
                continue
        simulations = len(list((self.root / "simulation" / "scenarios").glob("*.yaml")))
        vfab_state = "awaiting_source"
        report = self.root / "build" / "reports" / "source-alignment.json"
        if report.is_file():
            try:
                vfab_state = (json.loads(report.read_text(encoding="utf-8")).get("vfab") or {}).get("state", vfab_state)
            except json.JSONDecodeError:
                pass
        agent_articles = len(list((self.root / "knowledge" / "articles" / "agent-rounds").glob("*.md")))
        knowledge_entries = 0
        entries_dir = self.root / "knowledge" / "entries"
        for path in entries_dir.glob("*.json") if entries_dir.is_dir() else []:
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
                if doc.get("id") and doc.get("content"):
                    knowledge_entries += 1
            except (OSError, json.JSONDecodeError):
                continue
        return {"business_models": len(business_models), "business_relations": business_relations, "simulation_scenarios": simulations, "scenario_articles": scenario_count + agent_articles, "knowledge_entries": knowledge_entries, "vfab_state": vfab_state}

    def source_alignment_report(self) -> dict:
        """读取 align_sources.py 写的源层对齐报告（build/reports/source-alignment.json）。

        这是『来源对齐』门禁与 vFab 接入状态的权威快照：随离线 ingest/对齐即时更新，
        与 agent 轮次解耦。顶层 status=='pass' 即代表源层对齐通过（覆盖率是另一维度、
        不影响 pass）。文件缺失/损坏返回空 dict，调用方须容忍并回退。
        """
        path = self.root / "build" / "reports" / "source-alignment.json"
        if not path.is_file():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def vfab_cross_validation_report(self) -> dict:
        """读取 vfab_cross_validate.py 写的 vFab 知识库×本体 交叉验证报告（build/reports/vfab-cross-validation.json）。

        单轴口径：门禁=引用完整性（related_iris 是否都已声明）、头条=本体链接特异性（引用具体类
        的条目占比）、信息=本体触达。供日报『交叉验证结果』小节展示。与 source_alignment_report
        一样纯读取、缺失/损坏回退空 dict，调用方须容忍并回退。文件由 refresh_vfab_cross_validation 刷新。
        """
        path = self.root / "build" / "reports" / "vfab-cross-validation.json"
        if not path.is_file():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    async def refresh_vfab_cross_validation(self) -> dict:
        """best-effort 重跑 vfab_cross_validate.py 刷新报告，返回最新（或回退旧）报告。

        脚本是本地只读脚本（几秒内完成），失败/超时不抛出——直接回退读磁盘上已有报告，
        再没有则返回空 dict。这样日报拿到的是『当前指标』而非陈旧快照，同时绝不因刷新
        失败阻断日报生成。
        """
        try:
            await self.command("vfab_cross_validate.py", timeout=120)
        except Exception:
            pass
        return self.vfab_cross_validation_report()

    @staticmethod
    def _is_noise_feature(code: str) -> bool:
        code = (code or "").strip()
        if not code or code in _NOISE_FEATURE_CODES:
            return True
        if not re.search(r"[a-z]", code, re.I):  # 纯数字/中文/符号列，难以稳定映射到本体属性
            return True
        return bool(_NOISE_FEATURE_RE.search(code))

    def _source_theme_resolver(self, catalog: dict):
        """构造 sheet 名 → 业务主题名 的解析器（feature_gap 与 business_domain_coverage 共用）。

        catalog.themes 给出 code→中文名；catalog.entities 的 code/name → theme code；再叠加
        mappings/feature-model/entity-map.json 的 sheet_aliases（异名 sheet → entity code）。
        解析不到时归入『其他』。返回 (theme_of 函数, theme_name 字典)，供调用方复用同一套归类口径。
        """
        theme_name = {str(t.get("code") or "").lower(): str(t.get("name") or t.get("code") or "") for t in catalog.get("themes") or []}
        entity_theme: dict[str, str] = {}
        for entity in catalog.get("entities") or []:
            theme = str(entity.get("theme") or "").lower()
            for raw in (entity.get("code"), entity.get("name")):
                key = " ".join(str(raw or "").strip().lower().split())
                if key and theme:
                    entity_theme[key] = theme
        sheet_alias_to_code: dict[str, str] = {}
        entity_map_path = self.root / "mappings" / "feature-model" / "entity-map.json"
        if entity_map_path.is_file():
            try:
                for raw, code in ((json.loads(entity_map_path.read_text(encoding="utf-8")) or {}).get("sheet_aliases") or {}).items():
                    sheet_alias_to_code[" ".join(str(raw).strip().lower().split())] = str(code or "").lower()
            except (OSError, json.JSONDecodeError):
                pass

        def theme_of(sheet_name: str | None) -> str:
            key = " ".join(str(sheet_name or "").strip().lower().split())
            code = entity_theme.get(key) or entity_theme.get(sheet_alias_to_code.get(key, ""), "")
            return theme_name.get(code, "其他")

        return theme_of, theme_name

    def business_domain_coverage(self) -> dict:
        """按『源特征业务主题』聚合领域覆盖：每个业务域的业务特征总数/已映射/未映射/覆盖率。

        与 domain_coverage()（本体模块视角）互补——这里是业务视角：源特征目录里每个业务域
        （设备/质量/生产/原料/运维/组织/地理域）里去噪后的业务 feature_code，有多少已映射到本体属性。
        口径与 feature_gap 完全一致：同一套 theme 归类 + 同一 _is_noise_feature 去噪，故本表的
        『未映射合计』必然等于 feature_gap.business_relevant。覆盖率=已映射/业务特征总数（按去噪后的
        distinct feature_code 计，非行数——与 44.5% 那个『全特征行数覆盖率』是不同口径，日报会分别标注）。
        纯只读、文件缺失优雅降级为空；仅日报生成时取用，不进 metrics() 热路径。
        """
        report_path = self.root / "build" / "reports" / "source-alignment.json"
        catalog_path = self.root / "build" / "source" / "feature-model-catalog.json"
        if not report_path.is_file() or not catalog_path.is_file():
            return {"domains": [], "business_total": 0, "business_mapped": 0, "business_unmapped": 0, "coverage_percent": None}
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"domains": [], "business_total": 0, "business_mapped": 0, "business_unmapped": 0, "coverage_percent": None}
        unmapped = {str(item.get("feature_code") or "").strip().lower()
                    for item in (report.get("issues") or {}).get("unmapped_source_features") or [] if item.get("feature_code")}
        theme_of, _ = self._source_theme_resolver(catalog)
        seen: dict[str, str] = {}  # feature_code → theme（首个 sheet 胜，与 feature_gap 去重口径一致）
        for sheet in catalog.get("sheets") or []:
            theme = theme_of(sheet.get("name"))
            for feature in sheet.get("features") or []:
                code = str(feature.get("feature_code") or "").strip().lower()
                if code and code not in seen:
                    seen[code] = theme
        agg: dict[str, dict[str, int]] = {}
        for code, theme in seen.items():
            if self._is_noise_feature(code):
                continue
            bucket = agg.setdefault(theme, {"total": 0, "unmapped": 0})
            bucket["total"] += 1
            if code in unmapped:
                bucket["unmapped"] += 1
        domains = []
        for theme, bucket in agg.items():
            total = bucket["total"]
            mapped = total - bucket["unmapped"]
            domains.append({
                "theme": theme, "total": total, "mapped": mapped, "unmapped": bucket["unmapped"],
                "coverage_percent": round(mapped / total * 100, 1) if total else None,
            })
        domains.sort(key=lambda item: (item["total"], item["mapped"]), reverse=True)
        business_total = sum(item["total"] for item in domains)
        business_mapped = sum(item["mapped"] for item in domains)
        return {
            "domains": domains,
            "business_total": business_total,
            "business_mapped": business_mapped,
            "business_unmapped": sum(item["unmapped"] for item in domains),
            "coverage_percent": round(business_mapped / business_total * 100, 1) if business_total else None,
        }

    def feature_gap(self) -> dict:
        """反向缺口：源特征目录里存在、但 property-map.json 还没映射到本体的 feature_code。

        供子 Agent 反思与日报趋势使用。回连 feature-model-catalog 恢复 feature_name、
        按 sheet→entity→theme 归类；技术噪声只用于排序聚焦，不作硬门禁。读文件失败时
        返回零值结构，绝不影响主流程（指标看板、日报都会调用它）。
        """
        empty = {"unmapped_total": 0, "business_relevant": 0, "unmapped_rows": 0, "coverage_percent": None, "by_theme": [], "top_suspected": []}
        report_path = self.root / "build" / "reports" / "source-alignment.json"
        catalog_path = self.root / "build" / "source" / "feature-model-catalog.json"
        if not report_path.is_file() or not catalog_path.is_file():
            return empty
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return empty
        issues = report.get("issues") or {}
        unmapped = {str(item.get("feature_code") or "").strip().lower(): int(item.get("rows") or 0)
                    for item in issues.get("unmapped_source_features") or [] if item.get("feature_code")}
        if not unmapped:
            return empty
        report_metrics = report.get("internal_feature_model") or report.get("metrics") or {}
        theme_of, _ = self._source_theme_resolver(catalog)

        seen: dict[str, dict] = {}
        for sheet in catalog.get("sheets") or []:
            theme = theme_of(sheet.get("name"))
            for feature in sheet.get("features") or []:
                code = str(feature.get("feature_code") or "").strip().lower()
                if code and code in unmapped and code not in seen:
                    seen[code] = {"feature_code": code, "feature_name": str(feature.get("feature_name") or code), "theme": theme, "rows": unmapped[code]}
        business = sorted((record for record in seen.values() if not self._is_noise_feature(record["feature_code"])), key=lambda record: record["rows"], reverse=True)
        by_theme: dict[str, list] = {}
        for record in business:
            by_theme.setdefault(record["theme"], []).append(record)
        by_theme_out = sorted(
            ({"theme": theme, "count": len(items), "samples": [item["feature_name"] for item in items[:5]]} for theme, items in by_theme.items()),
            key=lambda entry: entry["count"], reverse=True,
        )[:8]
        coverage = report_metrics.get("property_mapping_coverage")
        return {
            "unmapped_total": len(unmapped),
            "business_relevant": len(business),
            "unmapped_rows": int(report_metrics.get("property_unmapped_feature_rows") or 0),
            "coverage_percent": round(float(coverage) * 100, 1) if isinstance(coverage, (int, float)) else None,
            "by_theme": by_theme_out,
            "top_suspected": [{"feature_name": record["feature_name"], "feature_code": record["feature_code"], "theme": record["theme"], "rows": record["rows"]} for record in business[:15]],
        }

    def _declared_properties(self) -> set[str]:
        """已声明的对象/数据属性 IRI 集合，作为映射候选的合法 target。

        与 align_sources.declared_terms() 解析同一批 ontology/modules/*.ttl，
        故通过本集合校验的 target 必然也能通过 align_sources --check 门禁。
        """
        schema = Graph()
        for path in sorted((self.root / "ontology" / "modules").glob("*.ttl")):
            try:
                schema.parse(path, format="turtle")
            except Exception:
                continue
        return {str(item) for item in schema.subjects(RDF.type, OWL.ObjectProperty)} | {str(item) for item in schema.subjects(RDF.type, OWL.DatatypeProperty)}

    def _internal_feature_codes(self) -> set[str]:
        catalog_path = self.root / "build" / "source" / "feature-model-catalog.json"
        if not catalog_path.is_file():
            return set()
        try:
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return set()
        codes: set[str] = set()
        for sheet in catalog.get("sheets") or []:
            for feature in sheet.get("features") or []:
                code = str(feature.get("feature_code") or "").strip().lower()
                if code:
                    codes.add(code)
        return codes

    def _merge_property_mappings(self, sources: list[Path]) -> tuple[dict, int, list[dict]]:
        """把 Agent 产出的映射候选合并进 property-map.json（不落盘，返回合并后的文档）。

        只接受:target 是已声明本体属性 ✅、feature_code 真实存在于特征目录 ✅、当前尚未
        被任何映射占用 ✅。首个写入者胜、跳过已占用码，保证合并结果不会产生
        duplicate_property_codes——即 align_sources --check 必然通过。
        """
        property_map_path = self.root / "mappings" / "feature-model" / "property-map.json"
        document: dict = {"mapping_id": "internal.feature_model.property_map", "mappings": []}
        if property_map_path.is_file():
            try:
                loaded = json.loads(property_map_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    document = loaded
            except (OSError, json.JSONDecodeError):
                pass
        mappings = document.setdefault("mappings", [])
        declared = self._declared_properties()
        real_codes = self._internal_feature_codes()
        mapped_codes = {str(code).strip().lower() for entry in mappings for code in entry.get("feature_codes") or []}
        by_target = {str(entry.get("target_property")): entry for entry in mappings if entry.get("target_property")}
        accepted = 0
        quarantine: list[dict] = []
        for source in sources:
            try:
                candidate = json.loads(source.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                quarantine.append({"path": str(source), "category": "mapping", "reason": str(exc)}); continue
            target = str(candidate.get("target_property") or "")
            if target not in declared:
                quarantine.append({"path": str(source), "category": "mapping", "reason": f"目标属性未声明：{target}"}); continue
            codes: list[str] = []
            for code in candidate.get("feature_codes") or []:
                code = str(code).strip().lower()
                if code and code in real_codes and code not in mapped_codes:
                    codes.append(code); mapped_codes.add(code)
            if not codes:
                quarantine.append({"path": str(source), "category": "mapping", "reason": "无可映射的未占用真实特征码"}); continue
            kind = str(candidate.get("mapping_kind") or "attribute")
            if target in by_target:
                existing = by_target[target].setdefault("feature_codes", [])
                existing.extend(code for code in codes if code not in existing)
            else:
                entry = {"feature_codes": codes, "target_property": target, "mapping_kind": kind, "provenance": "model_prior"}
                mappings.append(entry); by_target[target] = entry
            accepted += 1
        return document, accepted, quarantine

    async def metrics(self, db: Session | None = None, user_id: int | None = None) -> dict:
        if self._base_metrics_cache and time.monotonic() - self._base_metrics_cache[0] < self._BASE_METRICS_TTL_SECONDS:
            cached = self._base_metrics_cache[1]
            legacy, totals = cached["legacy"], dict(cached["totals"])
        else:
            legacy = await self.status()
            # semantic_counts/artifact_counts 是纯 CPU 的图计数，即便命中图缓存也在事件循环上跑。
            # 丢到线程池，避免任何一次冷计算（尤其图缓存 mtime 失效时的重解析）冻结全站其它接口。
            totals = await asyncio.to_thread(lambda: {**self.semantic_counts(), **self.artifact_counts()})
            totals.update({"legacy_entities": legacy.get("entities", 0), "knowledge_entries": int(legacy.get("kb_cases", 0)) + int(totals.get("knowledge_entries", 0)), "relations": int(legacy.get("edges", 0)) + int(totals.get("relation_assertions", 0)), "anomaly_total": legacy.get("anomaly_total", 0), "anomaly_covered": legacy.get("anomaly_covered", 0)})
            self._base_metrics_cache = (time.monotonic(), {"legacy": legacy, "totals": dict(totals)})
        totals["coverage_percent"] = round(100 * totals["anomaly_covered"] / totals["anomaly_total"], 1) if totals["anomaly_total"] else 0
        today = {key: 0 for key in ("classes", "properties", "relations", "individuals", "axioms", "rules", "knowledge_entries", "business_relations", "simulation_scenarios", "scenario_articles")}
        if db is not None and user_id is not None:
            zone = ZoneInfo(settings.timezone)
            today_text = datetime.now(zone).date().isoformat()
            # completed_partial / completed_no_change 也是正常收尾状态（部分发布、无变更），
            # 它们同样写入了 metrics_before/after，必须计入"今日新增"，否则闸门一旦触发
            # 部分发布，看板就会显示 +0。
            rounds = db.scalars(select(RunRound).join(Run, Run.id == RunRound.run_id).where(Run.user_id == user_id, RunRound.status.in_(("completed", "completed_partial", "completed_no_change")))).all()
            for item in rounds:
                if item.completed_at and item.completed_at.replace(tzinfo=item.completed_at.tzinfo or timezone.utc).astimezone(zone).date().isoformat() == today_text:
                    before = json.loads(item.metrics_before_json or "{}")
                    after = json.loads(item.metrics_after_json or "{}")
                    for key in today:
                        today[key] += max(0, int(after.get(key, 0)) - int(before.get(key, 0)))
        feature_gap = await asyncio.to_thread(self.feature_gap)
        return {"totals": totals, "today_added": today, "source_distribution": legacy.get("source_type", {}), "confidence_distribution": legacy.get("confidence", {}), "uncovered": legacy.get("uncovered", []), "feature_gap": feature_gap}

    def article_text(self) -> str:
        path = self.root / "knowledge" / "articles" / "current-scenarios.md"
        parts = [path.read_text(encoding="utf-8")] if path.is_file() else []
        generated = sorted((self.root / "knowledge" / "articles" / "agent-rounds").glob("*.md"), key=lambda item: item.stat().st_mtime, reverse=True)[:5]
        parts.extend(item.read_text(encoding="utf-8") for item in generated)
        return "\n\n---\n\n".join(parts)


semi_kb = SemiKbAdapter()
