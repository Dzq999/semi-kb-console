from __future__ import annotations

import asyncio
import hashlib
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from rdflib import RDF, OWL, Graph, URIRef
from sqlalchemy.orm import Session

from ..config import settings
from ..models import ExportJob


EXPORT_PATTERNS = {
    "ontology": ["ontology/modules/*.ttl", "ontology/shapes/*.ttl", "ontology/rules/*", "ontology/catalog.xml"],
    "knowledge": ["knowledge/semantic/*.ttl", "knowledge/scenarios/*", "knowledge/entries/*.json", "kb/**/*.yaml"],
    "business": ["business/models/*.yaml", "business/templates/*.yaml", "business/datasets/*.yaml"],
    "simulation": ["simulation/scenarios/*.yaml"],
    # Keep the scenario-knowledge export separate from公众号草稿（knowledge/articles/generated）。
    "scenarios": ["knowledge/articles/current-scenarios.md", "knowledge/articles/agent-rounds/*.md", "knowledge/scenarios/*"],
}


def _files(kind: str) -> list[Path]:
    patterns = list(EXPORT_PATTERNS) if kind == "complete" else [kind]
    files: set[Path] = set()
    for key in patterns:
        for pattern in EXPORT_PATTERNS[key]:
            files.update(path for path in settings.engine_root.glob(pattern) if path.is_file())
    return sorted(files)


def _filter_semantic_ttl(source_path: Path, filtered: bool) -> bytes:
    """过滤语义 TTL 文件，只保留有用的个体。

    filtered=True: 只保留策展模块的2436个体，删除噪声（RDF.Statement、prov:Entity）和非策展模块个体
    filtered=False: 返回原始内容

    策展模块个体：能映射到12个专业领域模块的实例（EQP、FAC、MAT等），排除业务推理层和系统元数据。
    """
    if not filtered or not source_path.name.endswith('.ttl'):
        return source_path.read_bytes()

    try:
        # 加载语义数据
        data = Graph()
        data.parse(source_path, format="turtle")

        # 加载本体 schema，获取 class -> module 映射
        from .semi_kb import semi_kb
        _, class_to_module, _ = semi_kb._load_schema_graph()

        # 定义噪声类型
        prov_entity = URIRef("http://www.w3.org/ns/prov#Entity")
        noise_types = {OWL.Class, OWL.ObjectProperty, OWL.DatatypeProperty, RDF.Statement, prov_entity}

        # 找出策展模块个体
        curated_individuals = set()
        for s, _, o in data.triples((None, RDF.type, None)):
            if o in noise_types:
                continue
            module = class_to_module.get(o)
            if module and module in semi_kb._DOMAIN_MODULE_LABELS:
                curated_individuals.add(s)

        # 创建过滤后的图
        filtered_graph = Graph()

        # 保留所有命名空间
        for prefix, namespace in data.namespaces():
            filtered_graph.bind(prefix, namespace)

        # 只保留策展模块个体相关的三元组
        for s, p, o in data:
            # 保留：主体是策展模块个体的三元组
            if s in curated_individuals:
                filtered_graph.add((s, p, o))
            # 保留：客体是策展模块个体的三元组（关系指向）
            elif o in curated_individuals:
                filtered_graph.add((s, p, o))

        # 序列化为 Turtle 格式
        return filtered_graph.serialize(format="turtle").encode('utf-8')

    except Exception:
        # 过滤失败时返回原始内容
        return source_path.read_bytes()


async def create_export(db: Session, job: ExportJob) -> None:
    if job.status == "cancelled":
        return
    job.status = "running"
    job.worker_id = f"export-worker-{uuid4().hex[:10]}"
    job.attempt_count = int(job.attempt_count or 0) + 1
    job.started_at = job.started_at or datetime.now(timezone.utc)
    job.heartbeat_at = datetime.now(timezone.utc)
    db.commit()

    filtered = job.filtered if hasattr(job, 'filtered') else True  # 默认精简导出
    suffix = f"-{job.kind}-filtered" if filtered else f"-{job.kind}"
    target = settings.data_dir / "artifacts" / f"{job.id}{suffix}.zip"

    try:
        files = _files(job.kind)
        entries = [(path, path.relative_to(settings.engine_root).as_posix()) for path in files]
        if job.kind == "complete":
            run_root = settings.data_dir / "runs"
            entries.extend((path, "console-runs/" + path.relative_to(run_root).as_posix()) for path in run_root.rglob("*") if path.is_file())
        job.total_files = len(entries)
        job.processed_files = 0
        job.progress = 5 if entries else 50
        job.heartbeat_at = datetime.now(timezone.utc)
        db.commit()

        manifest = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "kind": job.kind,
            "filtered": filtered,
            "files": []
        }

        # 准备要写入 zip 的数据
        entries_data = []
        for path, archive_name in entries:
            if db.get(ExportJob, job.id).status == "cancelled":
                return

            # 对语义 TTL 文件应用过滤
            if filtered and 'knowledge/semantic' in archive_name and archive_name.endswith('.ttl'):
                data = await asyncio.to_thread(_filter_semantic_ttl, path, filtered)
            else:
                data = await asyncio.to_thread(path.read_bytes)

            entries_data.append((data, archive_name))
            manifest["files"].append({"path": archive_name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)})
            job.processed_files += 1
            job.progress = min(90, 5 + (job.processed_files / max(job.total_files, 1)) * 85)
            job.heartbeat_at = datetime.now(timezone.utc)
            db.commit()

        def _write() -> None:
            with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
                for data, archive_name in entries_data:
                    archive.writestr(archive_name, data)
                archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))

        await asyncio.to_thread(_write)
    except Exception as exc:
        job.status = "failed"
        job.error = f"{type(exc).__name__}: {exc}"
        job.completed_at = datetime.now(timezone.utc)
        job.heartbeat_at = job.completed_at
        db.commit()
        return

    job.status = "completed"
    job.progress = 100
    job.processed_files = job.total_files
    job.path = str(target)
    job.completed_at = datetime.now(timezone.utc)
    job.heartbeat_at = job.completed_at
    db.commit()


def recover_export_jobs(db: Session) -> list[str]:
    """Reset interrupted jobs so the API lifespan can resume them safely."""
    ids: list[str] = []
    stale_before = datetime.now(timezone.utc).timestamp() - 120
    for job in db.query(ExportJob).filter(ExportJob.status.in_(["queued", "running"])).all():
        heartbeat = job.heartbeat_at.timestamp() if job.heartbeat_at else 0
        if job.status == "queued" or heartbeat < stale_before:
            job.status = "queued"
            job.worker_id = None
            ids.append(job.id)
    if ids:
        db.commit()
    return ids
