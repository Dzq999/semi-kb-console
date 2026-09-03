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
from rdflib import Graph, RDF, RDFS
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


class SemiKbError(RuntimeError):
    pass


class SemiKbAdapter:
    _candidate_lock = asyncio.Lock()
    def __init__(self, root: Path | None = None):
        self.root = root or settings.engine_root
        self._base_metrics_cache: tuple[float, dict] | None = None

    def invalidate_cache(self) -> None:
        self._base_metrics_cache = None

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

    async def validate(self, full: bool = True) -> dict:
        args = ["kb.py", "check"] if full else ["kb.py", "check", "--quick"]
        result = await self.command(*args)
        result["passed"] = result["exit_code"] == 0
        return result

    def ontology_context(self, limit: int = 500) -> dict:
        schema = Graph()
        for path in sorted((self.root / "ontology" / "modules").glob("*.ttl")):
            schema.parse(path, format="turtle")
        items = []
        for rdf_type, kind in ((OWL.Class, "class"), (OWL.ObjectProperty, "object_property"), (OWL.DatatypeProperty, "datatype_property")):
            for subject in schema.subjects(RDF.type, rdf_type):
                label = next(schema.objects(subject, RDFS.label), None)
                items.append({"iri": str(subject), "label": str(label) if label else str(subject), "kind": kind})
        return {"terms": sorted(items, key=lambda item: (item["kind"], item["iri"]))[:limit], "total": len(items)}

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

    async def process_candidates(self, candidates: dict[str, list[Path]], publish: bool) -> dict:
        """Stage candidates under the engine lock, run all gates, and optionally publish.

        Semantic publication is delegated to semi-kb's atomic merger. Simulation files are
        staged first so the same full-chain validation sees them, and removed on any failure.
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
            baseline = await self.validate(full=publish)
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
                if semantic_pairs:
                    batch_check = await self.command("apply_semantic_changeset.py", "--check", timeout=600)
                    if batch_check["exit_code"] == 0:
                        staged_semantic.extend(target for _, target in semantic_pairs)
                    else:
                        # Isolate only on a real batch failure.  This keeps the
                        # normal path O(1) expensive checks while preserving the
                        # existing guarantee that one bad candidate is isolated.
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
                for index, source in enumerate(business_sources, 1):
                    doc = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
                    model_id = str((doc.get("model") or {}).get("id") or f"candidate-{index}")
                    target = business_dir / f"{self._safe_stem(model_id)}.yaml"
                    if target.exists(): raise SemiKbError(f"经营模型候选 ID 已存在：{model_id}")
                    await asyncio.to_thread(shutil.copy2, source, target)
                    staged_business.append(target)
                for index, source in enumerate(simulation_sources, 1):
                    doc = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
                    scenario_id = str((doc.get("scenario") or {}).get("id") or f"candidate-{index}")
                    target = simulation_dir / f"{self._safe_stem(scenario_id)}.yaml"
                    if target.exists():
                        raise SemiKbError(f"仿真候选 ID 已存在：{scenario_id}")
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
                            raise ValueError(f"知识条目 ID 已存在：{entry_id}")
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
                if staged_semantic:
                    check = await self.command("apply_semantic_changeset.py", "--check", timeout=600)
                    result["checks"]["semantic_precheck"] = {"passed": check["exit_code"] == 0, "duration_seconds": check["duration_seconds"], "output": check["output"][-5000:]}
                    if check["exit_code"]:
                        raise SemiKbError("语义变更集预检失败：" + check["output"][-3000:])
                simulation = await self.command("simulate_check.py", timeout=600)
                result["checks"]["business_simulation"] = {"passed": simulation["exit_code"] == 0, "duration_seconds": simulation["duration_seconds"], "output": simulation["output"][-5000:]}
                if simulation["exit_code"]:
                    raise SemiKbError("经营模型或仿真候选校验失败：" + simulation["output"][-3000:])

                simulation_runs = []
                for path in staged_simulation:
                    run = await self.command("simulate.py", str(path.relative_to(self.root)), timeout=300)
                    simulation_runs.append({"path": path.relative_to(self.root).as_posix(), "passed": run["exit_code"] == 0, "output": run["output"][-8000:]})
                    if run["exit_code"]:
                        raise SemiKbError(f"仿真执行失败：{path.name}")
                result["simulation_runs"] = simulation_runs

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
                        full = await self.validate(full=True)
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
        schema = Graph()
        data = Graph()
        for path in sorted((self.root / "ontology" / "modules").glob("*.ttl")):
            schema.parse(path, format="turtle")
        semantic_path = self.root / "knowledge" / "semantic" / "current.ttl"
        if semantic_path.is_file():
            data.parse(semantic_path, format="turtle")
        classes = set(schema.subjects(RDF.type, OWL.Class))
        object_properties = set(schema.subjects(RDF.type, OWL.ObjectProperty))
        datatype_properties = set(schema.subjects(RDF.type, OWL.DatatypeProperty))
        individuals = {s for s, _, o in data.triples((None, RDF.type, None)) if o not in {OWL.Class, OWL.ObjectProperty, OWL.DatatypeProperty}}
        relation_assertions = sum(1 for subject, predicate, obj in data if predicate in object_properties and subject != obj)
        axiom_predicates = {OWL.equivalentClass, OWL.disjointWith, OWL.inverseOf, OWL.onProperty, OWL.cardinality, OWL.qualifiedCardinality, OWL.minQualifiedCardinality, OWL.maxQualifiedCardinality}
        axioms = sum(1 for _, predicate, _ in schema if predicate in axiom_predicates)
        rules_path = self.root / "ontology" / "rules" / "registry.json"
        rules = 0
        if rules_path.is_file():
            doc = json.loads(rules_path.read_text(encoding="utf-8"))
            rules = len(doc if isinstance(doc, list) else (doc.get("rules") or []))
        return {
            "classes": len(classes),
            "properties": len(object_properties) + len(datatype_properties),
            "object_properties": len(object_properties),
            "datatype_properties": len(datatype_properties),
            "individuals": len(individuals),
            "relation_assertions": relation_assertions,
            "axioms": axioms,
            "rules": rules,
            "semantic_triples": len(schema) + len(data),
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

    @staticmethod
    def _is_noise_feature(code: str) -> bool:
        code = (code or "").strip()
        if not code or code in _NOISE_FEATURE_CODES:
            return True
        if not re.search(r"[a-z]", code, re.I):  # 纯数字/中文/符号列，难以稳定映射到本体属性
            return True
        return bool(_NOISE_FEATURE_RE.search(code))

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
        theme_name = {str(t.get("code") or "").lower(): str(t.get("name") or t.get("code") or "") for t in catalog.get("themes") or []}
        # 实体 code/name → theme code（sheet 名多与实体 code/name 一致）
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
        if self._base_metrics_cache and time.monotonic() - self._base_metrics_cache[0] < 5:
            cached = self._base_metrics_cache[1]
            legacy, totals = cached["legacy"], dict(cached["totals"])
        else:
            legacy = await self.status()
            totals = {**self.semantic_counts(), **self.artifact_counts()}
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
        return {"totals": totals, "today_added": today, "source_distribution": legacy.get("source_type", {}), "confidence_distribution": legacy.get("confidence", {}), "uncovered": legacy.get("uncovered", []), "feature_gap": self.feature_gap()}

    def article_text(self) -> str:
        path = self.root / "knowledge" / "articles" / "current-scenarios.md"
        parts = [path.read_text(encoding="utf-8")] if path.is_file() else []
        generated = sorted((self.root / "knowledge" / "articles" / "agent-rounds").glob("*.md"), key=lambda item: item.stat().st_mtime, reverse=True)[:5]
        parts.extend(item.read_text(encoding="utf-8") for item in generated)
        return "\n\n---\n\n".join(parts)


semi_kb = SemiKbAdapter()
