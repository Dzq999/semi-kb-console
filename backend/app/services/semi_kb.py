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


class SemiKbError(RuntimeError):
    pass


class SemiKbAdapter:
    _candidate_lock = asyncio.Lock()
    def __init__(self, root: Path | None = None):
        self.root = root or settings.semi_kb_root
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
        result: dict = {"published": False, "semantic_candidates": len(semantic_sources), "business_candidates": len(business_sources), "simulation_candidates": len(simulation_sources), "knowledge_candidates": len(knowledge_sources), "rule_candidates": len(rule_sources), "quarantined_candidates": [], "checks": {}}
        if not semantic_sources and not business_sources and not simulation_sources and not knowledge_sources and not rule_sources:
            result["accepted_candidates"] = {"semantic": 0, "business": 0, "simulation": 0, "knowledge": 0, "rules": 0}
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
            rounds = db.scalars(select(RunRound).join(Run, Run.id == RunRound.run_id).where(Run.user_id == user_id, RunRound.status == "completed")).all()
            for item in rounds:
                if item.completed_at and item.completed_at.replace(tzinfo=item.completed_at.tzinfo or timezone.utc).astimezone(zone).date().isoformat() == today_text:
                    before = json.loads(item.metrics_before_json or "{}")
                    after = json.loads(item.metrics_after_json or "{}")
                    for key in today:
                        today[key] += max(0, int(after.get(key, 0)) - int(before.get(key, 0)))
        return {"totals": totals, "today_added": today, "source_distribution": legacy.get("source_type", {}), "confidence_distribution": legacy.get("confidence", {}), "uncovered": legacy.get("uncovered", [])}

    def article_text(self) -> str:
        path = self.root / "knowledge" / "articles" / "current-scenarios.md"
        parts = [path.read_text(encoding="utf-8")] if path.is_file() else []
        generated = sorted((self.root / "knowledge" / "articles" / "agent-rounds").glob("*.md"), key=lambda item: item.stat().st_mtime, reverse=True)[:5]
        parts.extend(item.read_text(encoding="utf-8") for item in generated)
        return "\n\n---\n\n".join(parts)


semi_kb = SemiKbAdapter()
