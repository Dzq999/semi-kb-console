"""本体治理（Track C）服务：同义修剪生命周期 + 治理 KPI 聚合。

安全边界（对齐用户两项决策）：
  * scan / deprecate 标记：只碰 console DB 与 build/reports 下的只读报告，绝不写 gated 活文件
    （generated/current/provenance）→ 零门禁成本、可与 orchestrator 并发。
  * merge 落盘（不可逆）：只有人在前端点确认才触发，且要求当前无活跃 orchestrator run，
    走引擎 prune_synonyms.py 原子事务（全量快照→merge→ref-fix→门禁→commit/回滚）。

作业行仿 ExportJob 的心跳 + 崩溃恢复惯例；候选行是同义簇生命周期的 DB 侧真源。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import GovernanceCandidate, GovernanceJob, Run
from .semi_kb import semi_kb

_ACTIVE_RUN_STATUSES = ["pending", "running", "recovering", "paused",
                        "between_rounds", "stopping_after_round", "cancelling"]
_SCAN_REPORT = "build/reports/_synonym_scan.json"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(dt: datetime | None) -> datetime | None:
    """把从 DB 读回的时间戳补成 UTC aware。

    DateTime(timezone=True) 在 Postgres 上读回即 aware，但 SQLite 回退路径（无 postgres 凭据时，
    见 config.database_url）读回的是 naive；与 _now() 直接比较会 TypeError。沿用 orchestrator/
    langgraph_runtime 的同一 naive→UTC 归一惯例，保证两种后端下比较都成立。
    """
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _cluster_key(kind: str, canonical: str) -> str:
    return f"{kind}|{canonical}"


def _heartbeat(db: Session, job: GovernanceJob, stage: str | None = None, progress: float | None = None) -> None:
    job.heartbeat_at = _now()
    if stage is not None:
        job.stage = stage
    if progress is not None:
        job.progress = progress
    db.commit()


def candidate_view(row: GovernanceCandidate) -> dict:
    """把候选行拍平成前端友好的 dict（含 grace 剩余天数）。"""
    grace_left = None
    grace_until = _as_utc(row.grace_until)
    if grace_until:
        grace_left = round((grace_until - _now()).total_seconds() / 86400, 1)
    return {
        "id": row.id,
        "kind": row.kind,
        "canonical": row.canonical,
        "canonical_module": row.canonical_module,
        "merge": json.loads(row.merge_json or "[]"),
        "reason": json.loads(row.reason_json or "[]"),
        "label_zh": row.label_zh,
        "status": row.status,
        "detected_at": row.detected_at.isoformat() if row.detected_at else None,
        "deprecated_at": row.deprecated_at.isoformat() if row.deprecated_at else None,
        "grace_until": row.grace_until.isoformat() if row.grace_until else None,
        "grace_days_left": grace_left,
        "merged_at": row.merged_at.isoformat() if row.merged_at else None,
        # merge 可执行的门槛：已过 grace 期（或从未设 grace 但已 deprecated 亦可，交由前端提示）。
        "merge_ready": bool(row.status == "deprecated" and grace_until and grace_until <= _now()),
    }


def _upsert_candidates(db: Session, clusters: list[dict], auto_deprecate: bool, grace_days: int) -> dict:
    """把本次扫描结果幂等 upsert 进候选表。返回本次统计。

    - 新簇 → detected（auto_deprecate=True 时直接进 deprecated + 起 grace 倒计时）。
    - 已存在簇 → 刷新 merge/reason/label、last_seen；已 merged 的不复活。
    - 本次扫描未再出现、且仍处 detected/deprecated 的旧簇 → 标 resolved（已被别的流程处理）。
    """
    seen_keys: set[str] = set()
    new_count = deprecated_count = refreshed = 0
    for c in clusters:
        key = _cluster_key(c["kind"], c["canonical"])
        seen_keys.add(key)
        row = db.scalar(select(GovernanceCandidate).where(GovernanceCandidate.cluster_key == key))
        if row is None:
            row = GovernanceCandidate(
                cluster_key=key, kind=c["kind"], canonical=c["canonical"],
                canonical_module=c.get("canonical_module"),
                merge_json=json.dumps(c.get("merge", []), ensure_ascii=False),
                reason_json=json.dumps(c.get("reason", []), ensure_ascii=False),
                label_zh=c.get("label_zh"), status="detected",
                detected_at=_now(), last_seen_at=_now(),
            )
            if auto_deprecate:
                row.status = "deprecated"
                row.deprecated_at = _now()
                row.grace_until = _now() + timedelta(days=max(grace_days, 0))
                deprecated_count += 1
            db.add(row)
            new_count += 1
        elif row.status in ("detected", "deprecated"):
            row.merge_json = json.dumps(c.get("merge", []), ensure_ascii=False)
            row.reason_json = json.dumps(c.get("reason", []), ensure_ascii=False)
            row.label_zh = c.get("label_zh") or row.label_zh
            row.canonical_module = c.get("canonical_module") or row.canonical_module
            row.last_seen_at = _now()
            if auto_deprecate and row.status == "detected":
                row.status = "deprecated"
                row.deprecated_at = _now()
                row.grace_until = _now() + timedelta(days=max(grace_days, 0))
                deprecated_count += 1
            refreshed += 1
    # 已消失的旧簇 → resolved
    resolved = 0
    for row in db.scalars(select(GovernanceCandidate).where(GovernanceCandidate.status.in_(["detected", "deprecated"]))).all():
        if row.cluster_key not in seen_keys:
            row.status = "resolved"
            resolved += 1
    db.commit()
    return {"new": new_count, "auto_deprecated": deprecated_count, "refreshed": refreshed, "resolved": resolved}


async def run_scan_job(db: Session, job: GovernanceJob, auto_deprecate: bool = False, grace_days: int = 14) -> None:
    """扫描作业：跑 find_synonyms → 读报告 → upsert 候选。绝不碰 gated 活文件。"""
    job.status = "running"
    job.worker_id = f"gov-scan-{uuid4().hex[:10]}"
    job.attempt_count = int(job.attempt_count or 0) + 1
    job.started_at = job.started_at or _now()
    _heartbeat(db, job, stage="扫描同义术语", progress=10)
    try:
        result = await semi_kb.command("find_synonyms.py", timeout=300)
        if result["exit_code"] != 0:
            raise RuntimeError(result["output"][-2000:] or "find_synonyms 非零退出")
        _heartbeat(db, job, stage="解析扫描报告", progress=60)
        report_path = settings.engine_root / _SCAN_REPORT
        scan = json.loads(report_path.read_text(encoding="utf-8"))
        clusters = list(scan.get("classes", [])) + list(scan.get("properties", []))
        stats = _upsert_candidates(db, clusters, auto_deprecate, grace_days)
        job.candidates_json = json.dumps(clusters, ensure_ascii=False)
        job.output = (f"检出 {len(clusters)} 组同义（类 {len(scan.get('classes', []))} / 属性 "
                      f"{len(scan.get('properties', []))}）；新增 {stats['new']}、自动标记 "
                      f"{stats['auto_deprecated']}、消解 {stats['resolved']}。")
        job.status = "succeeded"
        job.progress = 100
        job.stage = "完成"
        job.completed_at = _now()
        job.heartbeat_at = job.completed_at
        db.commit()
    except Exception as exc:  # noqa: BLE001
        job.status = "failed"
        job.error = f"{type(exc).__name__}: {exc}"
        job.completed_at = _now()
        job.heartbeat_at = job.completed_at
        db.commit()


async def run_merge_job(db: Session, job: GovernanceJob, candidate_ids: list[int]) -> None:
    """合并落盘作业（不可逆）：构造 merge map → prune_synonyms 原子事务（含门禁）。

    前置硬约束：当前无活跃 orchestrator run（并发写 gated 活文件红线）。
    """
    job.status = "running"
    job.worker_id = f"gov-merge-{uuid4().hex[:10]}"
    job.attempt_count = int(job.attempt_count or 0) + 1
    job.started_at = job.started_at or _now()
    _heartbeat(db, job, stage="检查并发", progress=5)
    try:
        active = db.scalar(select(Run).where(Run.status.in_(_ACTIVE_RUN_STATUSES)).limit(1))
        if active is not None:
            raise RuntimeError(f"有活跃编排任务（run={active.id} status={active.status}），"
                               f"为避免并发写本体活文件，合并已拒绝。请等其结束或暂停后重试。")
        rows = [db.get(GovernanceCandidate, cid) for cid in candidate_ids]
        rows = [r for r in rows if r is not None and r.status in ("detected", "deprecated")]
        if not rows:
            raise RuntimeError("没有可合并的候选（可能已被合并/消解，或状态不符）。")
        merge_map = [{
            "kind": r.kind, "canonical": r.canonical,
            "canonical_module": r.canonical_module,
            "merge": json.loads(r.merge_json or "[]"),
            "reason": json.loads(r.reason_json or "[]"),
            **({"label_zh": r.label_zh} if r.label_zh else {}),
        } for r in rows]
        job.candidates_json = json.dumps(merge_map, ensure_ascii=False)
        db.commit()

        # 写 merge map 到引擎 build/reports（供 prune_synonyms --map 读取）。
        ts = _now().strftime("%Y%m%dT%H%M%SZ")
        map_rel = f"build/reports/_c1_merge_map_{job.id}.json"
        map_path = settings.engine_root / map_rel
        map_path.parent.mkdir(parents=True, exist_ok=True)
        map_path.write_text(json.dumps(merge_map, ensure_ascii=False, indent=2), encoding="utf-8")

        _heartbeat(db, job, stage="合并+改引用+发布门禁（约 13 分钟）", progress=15)
        result = await semi_kb.command("prune_synonyms.py", "--map", map_rel, timeout=2400)
        job.output = result["output"][-6000:]
        if result["exit_code"] != 0:
            raise RuntimeError(f"prune_synonyms 事务失败并已回滚（exit {result['exit_code']}）。详见 output。")

        for r in rows:
            r.status = "merged"
            r.merged_at = _now()
        job.status = "succeeded"
        job.progress = 100
        job.stage = "完成"
        job.audit_path = f"build/reports/prune-synonyms-{ts}.json"
        job.completed_at = _now()
        job.heartbeat_at = job.completed_at
        db.commit()
    except Exception as exc:  # noqa: BLE001
        job.status = "failed"
        job.error = f"{type(exc).__name__}: {exc}"
        job.completed_at = _now()
        job.heartbeat_at = job.completed_at
        db.commit()


def recover_governance_jobs(db: Session) -> None:
    """启动时把中断的治理作业标为 failed（不自动重放——merge 不可逆，须人重新确认）。"""
    stale_before = _now().timestamp() - 120
    changed = False
    for job in db.scalars(select(GovernanceJob).where(GovernanceJob.status.in_(["queued", "running"]))).all():
        heartbeat = job.heartbeat_at.timestamp() if job.heartbeat_at else 0
        if job.status == "queued" or heartbeat < stale_before:
            job.status = "failed"
            job.error = "后端重启导致作业中断；请重新触发（合并不可逆，不自动重放）。"
            job.completed_at = _now()
            changed = True
    if changed:
        db.commit()


def deprecate_candidates(db: Session, candidate_ids: list[int], grace_days: int) -> int:
    """人工把 detected 候选标记为 deprecated（起 grace 倒计时）。可逆、只落 DB。"""
    count = 0
    for cid in candidate_ids:
        row = db.get(GovernanceCandidate, cid)
        if row and row.status == "detected":
            row.status = "deprecated"
            row.deprecated_at = _now()
            row.grace_until = _now() + timedelta(days=max(grace_days, 0))
            count += 1
    if count:
        db.commit()
    return count


def governance_kpis(db: Session) -> dict:
    """治理 KPI 看板聚合：候选生命周期计数 + 复用引擎既有质量报告（链接特异性/引用完整性/领域覆盖）。"""
    counts: dict[str, int] = {"detected": 0, "deprecated": 0, "merged": 0, "resolved": 0}
    merge_ready = 0
    for row in db.scalars(select(GovernanceCandidate)).all():
        counts[row.status] = counts.get(row.status, 0) + 1
        grace_until = _as_utc(row.grace_until)
        if row.status == "deprecated" and grace_until and grace_until <= _now():
            merge_ready += 1
    active_groups = counts["detected"] + counts["deprecated"]

    last_scan = db.scalar(select(GovernanceJob).where(GovernanceJob.kind == "scan", GovernanceJob.status == "succeeded").order_by(GovernanceJob.completed_at.desc()).limit(1))
    last_merge = db.scalar(select(GovernanceJob).where(GovernanceJob.kind == "merge", GovernanceJob.status == "succeeded").order_by(GovernanceJob.completed_at.desc()).limit(1))

    # 复用引擎既有质量报告（纯读、缺失回退空 dict）。
    vfab = {}
    try:
        vfab = semi_kb.vfab_cross_validation_report() or {}
    except Exception:  # noqa: BLE001
        vfab = {}
    dims = vfab.get("dimensions", {}) if isinstance(vfab, dict) else {}

    def _dim(name: str) -> float | None:
        d = dims.get(name, {})
        v = d.get("value") if isinstance(d, dict) else None
        return round(v, 4) if isinstance(v, (int, float)) else None

    return {
        "candidate_counts": counts,
        "active_synonym_groups": active_groups,
        "merge_ready": merge_ready,
        "link_specificity": _dim("link_specificity"),
        "referential_integrity": _dim("referential_integrity"),
        "headline_coverage": vfab.get("headline_coverage") if isinstance(vfab, dict) else None,
        "last_scan_at": last_scan.completed_at.isoformat() if last_scan and last_scan.completed_at else None,
        "last_merge_at": last_merge.completed_at.isoformat() if last_merge and last_merge.completed_at else None,
    }

