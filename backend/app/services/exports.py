from __future__ import annotations

import asyncio
import hashlib
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from ..config import settings
from ..models import ExportJob


EXPORT_PATTERNS = {
    "ontology": ["ontology/modules/*.ttl", "ontology/shapes/*.ttl", "ontology/rules/*", "ontology/catalog.xml"],
    "knowledge": ["knowledge/semantic/*.ttl", "knowledge/scenarios/*", "kb/**/*.yaml"],
    "business": ["business/models/*.yaml", "business/templates/*.yaml", "business/datasets/*.yaml"],
    "simulation": ["simulation/scenarios/*.yaml"],
    "scenarios": ["knowledge/articles/**/*", "knowledge/scenarios/*"],
}


def _files(kind: str) -> list[Path]:
    patterns = list(EXPORT_PATTERNS) if kind == "complete" else [kind]
    files: set[Path] = set()
    for key in patterns:
        for pattern in EXPORT_PATTERNS[key]:
            files.update(path for path in settings.semi_kb_root.glob(pattern) if path.is_file())
    return sorted(files)


async def create_export(db: Session, job: ExportJob) -> None:
    job.status = "running"
    db.commit()
    target = settings.data_dir / "artifacts" / f"{job.id}-{job.kind}.zip"
    try:
        files = _files(job.kind)
        entries = [(path, path.relative_to(settings.semi_kb_root).as_posix()) for path in files]
        if job.kind == "complete":
            run_root = settings.data_dir / "runs"
            entries.extend((path, "console-runs/" + path.relative_to(run_root).as_posix()) for path in run_root.rglob("*") if path.is_file())
        manifest = {"created_at": datetime.now(timezone.utc).isoformat(), "kind": job.kind, "files": []}
        for path, archive_name in entries:
            data = await asyncio.to_thread(path.read_bytes)
            manifest["files"].append({"path": archive_name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)})
        def _write() -> None:
            with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
                for path, archive_name in entries:
                    archive.write(path, archive_name)
                archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        await asyncio.to_thread(_write)
    except Exception as exc:
        job.status = "failed"
        job.error = f"{type(exc).__name__}: {exc}"
        db.commit()
        return
    job.status = "completed"
    job.path = str(target)
    db.commit()
