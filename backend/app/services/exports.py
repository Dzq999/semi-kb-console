from __future__ import annotations

import asyncio
import hashlib
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy.orm import Session

from ..config import settings
from ..models import ExportJob


EXPORT_PATTERNS = {
    "ontology": ["ontology/modules/*.ttl", "ontology/shapes/*.ttl", "ontology/rules/*", "ontology/catalog.xml"],
    "knowledge": ["knowledge/semantic/*.ttl", "knowledge/scenarios/*", "kb/**/*.yaml"],
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
            files.update(path for path in settings.semi_kb_root.glob(pattern) if path.is_file())
    return sorted(files)


async def create_export(db: Session, job: ExportJob) -> None:
    if job.status == "cancelled":
        return
    job.status = "running"
    job.worker_id = f"export-worker-{uuid4().hex[:10]}"
    job.attempt_count = int(job.attempt_count or 0) + 1
    job.started_at = job.started_at or datetime.now(timezone.utc)
    job.heartbeat_at = datetime.now(timezone.utc)
    db.commit()
    target = settings.data_dir / "artifacts" / f"{job.id}-{job.kind}.zip"
    try:
        files = _files(job.kind)
        entries = [(path, path.relative_to(settings.semi_kb_root).as_posix()) for path in files]
        if job.kind == "complete":
            run_root = settings.data_dir / "runs"
            entries.extend((path, "console-runs/" + path.relative_to(run_root).as_posix()) for path in run_root.rglob("*") if path.is_file())
        job.total_files = len(entries)
        job.processed_files = 0
        job.progress = 5 if entries else 50
        job.heartbeat_at = datetime.now(timezone.utc)
        db.commit()
        manifest = {"created_at": datetime.now(timezone.utc).isoformat(), "kind": job.kind, "files": []}
        for path, archive_name in entries:
            if db.get(ExportJob, job.id).status == "cancelled":
                return
            data = await asyncio.to_thread(path.read_bytes)
            manifest["files"].append({"path": archive_name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)})
            job.processed_files += 1
            job.progress = min(90, 5 + (job.processed_files / max(job.total_files, 1)) * 85)
            job.heartbeat_at = datetime.now(timezone.utc)
            db.commit()
        def _write() -> None:
            with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
                for path, archive_name in entries:
                    archive.write(path, archive_name)
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
