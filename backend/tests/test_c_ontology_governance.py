"""Track C 本体治理服务单测：同义修剪生命周期 + 治理 KPI + 定时/端点。

覆盖安全边界的关键断言：
  * scan / deprecate 只落 DB，不碰 gated 活文件；
  * merge 落盘遇活跃 orchestrator run 必须拒绝（并发写红线）；
  * cron 五段式校验；候选幂等 upsert / resolved 消解 / grace 倒计时。
不驱动真实引擎脚本（semi_kb.command 全程 monkeypatch），不跑门禁。
"""
from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.db import SessionLocal
from app.models import GovernanceCandidate, GovernanceJob, Run, User
from app.services import ontology_governance as gov

SEMI = "urn:pxai:semi:"


def _cluster(kind="property", canonical="hasFoo", merge=None, reason=None, module="manufacturing", zh=None):
    return {
        "kind": kind,
        "canonical": canonical,
        "canonical_module": module,
        "merge": merge if merge is not None else ["hasFooBar"],
        "reason": reason if reason is not None else ["same_label_zh"],
        **({"label_zh": zh} if zh else {}),
    }


# --------------------------------------------------------------------------- 候选 upsert 幂等
def test_upsert_new_detected_then_idempotent_refresh():
    with SessionLocal() as db:
        s1 = gov._upsert_candidates(db, [_cluster()], auto_deprecate=False, grace_days=14)
        assert s1["new"] == 1 and s1["auto_deprecated"] == 0
        # 同簇再扫一次：不新增，只刷新
        s2 = gov._upsert_candidates(db, [_cluster(merge=["hasFooBar", "fooAlias"])], auto_deprecate=False, grace_days=14)
        assert s2["new"] == 0 and s2["refreshed"] == 1
        rows = db.query(GovernanceCandidate).all()
        assert len(rows) == 1
        assert json.loads(rows[0].merge_json) == ["hasFooBar", "fooAlias"]  # 刷新到最新 merge 列表


def test_upsert_auto_deprecate_sets_grace():
    with SessionLocal() as db:
        stats = gov._upsert_candidates(db, [_cluster()], auto_deprecate=True, grace_days=7)
        assert stats["auto_deprecated"] == 1
        row = db.query(GovernanceCandidate).one()
        assert row.status == "deprecated" and row.grace_until is not None
        # grace ≈ 7 天后（grace_until 经 SQLite 回读为 naive，用服务同款归一再比）
        assert 6.5 < (gov._as_utc(row.grace_until) - gov._now()).total_seconds() / 86400 < 7.1


def test_upsert_vanished_cluster_marked_resolved():
    with SessionLocal() as db:
        gov._upsert_candidates(db, [_cluster(canonical="hasA"), _cluster(canonical="hasB")], auto_deprecate=False, grace_days=14)
        # 第二次扫描只剩 hasA → hasB 消解
        gov._upsert_candidates(db, [_cluster(canonical="hasA")], auto_deprecate=False, grace_days=14)
        rows = {r.canonical: r.status for r in db.query(GovernanceCandidate).all()}
        assert rows == {"hasA": "detected", "hasB": "resolved"}


def test_merged_row_not_revived_by_scan():
    with SessionLocal() as db:
        gov._upsert_candidates(db, [_cluster(canonical="hasA")], auto_deprecate=False, grace_days=14)
        row = db.query(GovernanceCandidate).one()
        row.status = "merged"
        db.commit()
        # 再次扫描出同簇：不得复活成 detected
        gov._upsert_candidates(db, [_cluster(canonical="hasA")], auto_deprecate=False, grace_days=14)
        assert db.query(GovernanceCandidate).one().status == "merged"


# --------------------------------------------------------------------------- candidate_view / grace
def test_candidate_view_merge_ready_after_grace():
    with SessionLocal() as db:
        gov._upsert_candidates(db, [_cluster()], auto_deprecate=True, grace_days=0)
        row = db.query(GovernanceCandidate).one()
        # grace_days=0 → grace_until 已到 → merge_ready
        view = gov.candidate_view(row)
        assert view["status"] == "deprecated"
        assert view["merge_ready"] is True
        assert view["grace_days_left"] is not None and view["grace_days_left"] <= 0


def test_candidate_view_not_ready_during_grace():
    with SessionLocal() as db:
        gov._upsert_candidates(db, [_cluster()], auto_deprecate=True, grace_days=30)
        row = db.query(GovernanceCandidate).one()
        view = gov.candidate_view(row)
        assert view["merge_ready"] is False and view["grace_days_left"] > 0


# --------------------------------------------------------------------------- deprecate（人工，可逆）
def test_deprecate_candidates_only_detected():
    with SessionLocal() as db:
        gov._upsert_candidates(db, [_cluster(canonical="hasA"), _cluster(canonical="hasB")], auto_deprecate=False, grace_days=14)
        rows = db.query(GovernanceCandidate).order_by(GovernanceCandidate.canonical).all()
        # 把 hasB 先设成 merged，deprecate 只应作用于 detected
        rows[1].status = "merged"; db.commit()
        n = gov.deprecate_candidates(db, [r.id for r in rows], grace_days=5)
        assert n == 1
        fresh = {r.canonical: r.status for r in db.query(GovernanceCandidate).all()}
        assert fresh == {"hasA": "deprecated", "hasB": "merged"}


def _make_user(db) -> int:
    u = User(username="gov", password_hash="x")
    db.add(u); db.commit()
    return u.id


# --------------------------------------------------------------------------- scan 作业（mock 引擎）
@pytest.mark.asyncio
async def test_run_scan_job_upserts_from_report(tmp_path, monkeypatch):
    # 假引擎根 + 假扫描报告，绝不碰真活文件（settings 是 frozen dataclass，替换整个引用）
    monkeypatch.setattr(gov, "settings", SimpleNamespace(engine_root=tmp_path))
    report = tmp_path / "build" / "reports" / "_synonym_scan.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps({
        "classes": [_cluster(kind="class", canonical="RandomDefect", merge=["Random_Defect"], reason=["uri_variant"], zh="随机缺陷")],
        "properties": [_cluster(kind="property", canonical="hasFoo", merge=["hasFooBar"])],
    }), encoding="utf-8")

    async def fake_command(script, *args, timeout=300):
        assert script == "find_synonyms.py"
        return {"exit_code": 0, "output": "ok", "duration_seconds": 0.1}
    monkeypatch.setattr(gov.semi_kb, "command", fake_command)

    with SessionLocal() as db:
        uid = _make_user(db)
        job = GovernanceJob(id="gov-scan-t1", user_id=uid, kind="scan", trigger="manual")
        db.add(job); db.commit()
        await gov.run_scan_job(db, job, auto_deprecate=False, grace_days=14)
        assert job.status == "succeeded" and job.progress == 100
        rows = {r.canonical: r.status for r in db.query(GovernanceCandidate).all()}
        assert rows == {"RandomDefect": "detected", "hasFoo": "detected"}


@pytest.mark.asyncio
async def test_run_scan_job_fails_on_nonzero(tmp_path, monkeypatch):
    monkeypatch.setattr(gov, "settings", SimpleNamespace(engine_root=tmp_path))

    async def fake_command(script, *args, timeout=300):
        return {"exit_code": 2, "output": "boom", "duration_seconds": 0.1}
    monkeypatch.setattr(gov.semi_kb, "command", fake_command)

    with SessionLocal() as db:
        uid = _make_user(db)
        job = GovernanceJob(id="gov-scan-t2", user_id=uid, kind="scan")
        db.add(job); db.commit()
        await gov.run_scan_job(db, job)
        assert job.status == "failed" and "boom" in (job.error or "")


# --------------------------------------------------------------------------- merge 并发红线
@pytest.mark.asyncio
async def test_run_merge_job_refuses_when_active_run(monkeypatch):
    called = {"cmd": False}

    async def fake_command(script, *args, timeout=2400):
        called["cmd"] = True
        return {"exit_code": 0, "output": "", "duration_seconds": 0}
    monkeypatch.setattr(gov.semi_kb, "command", fake_command)

    with SessionLocal() as db:
        uid = _make_user(db)
        # 活跃编排 run
        db.add(Run(id="run-active", user_id=uid, model_id="m", status="running", config_json="{}")); db.commit()
        gov._upsert_candidates(db, [_cluster()], auto_deprecate=True, grace_days=0)
        cid = db.query(GovernanceCandidate).one().id
        job = GovernanceJob(id="gov-merge-t1", user_id=uid, kind="merge")
        db.add(job); db.commit()
        await gov.run_merge_job(db, job, [cid])
        assert job.status == "failed"
        assert "活跃编排" in (job.error or "")
        assert called["cmd"] is False  # 绝不能跑到 prune_synonyms
        assert db.query(GovernanceCandidate).one().status == "deprecated"  # 候选未被改动


@pytest.mark.asyncio
async def test_run_merge_job_no_valid_candidates(monkeypatch):
    async def fake_command(script, *args, timeout=2400):
        return {"exit_code": 0, "output": "", "duration_seconds": 0}
    monkeypatch.setattr(gov.semi_kb, "command", fake_command)
    with SessionLocal() as db:
        uid = _make_user(db)
        job = GovernanceJob(id="gov-merge-t2", user_id=uid, kind="merge")
        db.add(job); db.commit()
        await gov.run_merge_job(db, job, [9999])  # 不存在的 id
        assert job.status == "failed" and "可合并" in (job.error or "")


# --------------------------------------------------------------------------- 崩溃恢复
def test_recover_marks_stale_failed_no_replay():
    with SessionLocal() as db:
        uid = _make_user(db)
        db.add(GovernanceJob(id="j-queued", user_id=uid, kind="merge", status="queued"))
        stale = GovernanceJob(id="j-running", user_id=uid, kind="scan", status="running")
        stale.heartbeat_at = gov._now() - timedelta(minutes=10)
        db.add(stale); db.commit()
        gov.recover_governance_jobs(db)
        statuses = {j.id: j.status for j in db.query(GovernanceJob).all()}
        assert statuses == {"j-queued": "failed", "j-running": "failed"}


# --------------------------------------------------------------------------- KPI 聚合（mock vfab）
def test_governance_kpis_shapes_and_counts(monkeypatch):
    monkeypatch.setattr(gov.semi_kb, "vfab_cross_validation_report", lambda: {
        "headline_coverage": 0.82,
        "dimensions": {"link_specificity": {"value": 0.046}, "referential_integrity": {"value": 1.0}},
    })
    with SessionLocal() as db:
        # 同一次扫描检出两簇，再把 hasB 标 deprecated 且 grace 已到（避免二次扫描把 hasA 判 resolved）
        gov._upsert_candidates(db, [_cluster(canonical="hasA"), _cluster(canonical="hasB")], auto_deprecate=False, grace_days=14)
        hasb = db.query(GovernanceCandidate).filter_by(canonical="hasB").one()
        gov.deprecate_candidates(db, [hasb.id], grace_days=0)  # grace 已到 → merge_ready
        k = gov.governance_kpis(db)
        assert k["candidate_counts"]["detected"] == 1
        assert k["candidate_counts"]["deprecated"] == 1
        assert k["active_synonym_groups"] == 2
        assert k["merge_ready"] == 1
        assert k["link_specificity"] == 0.046 and k["referential_integrity"] == 1.0
        assert k["headline_coverage"] == 0.82


def test_governance_kpis_survives_vfab_failure(monkeypatch):
    def boom():
        raise RuntimeError("no report")
    monkeypatch.setattr(gov.semi_kb, "vfab_cross_validation_report", boom)
    with SessionLocal() as db:
        k = gov.governance_kpis(db)
        assert k["link_specificity"] is None and k["headline_coverage"] is None
        assert k["candidate_counts"] == {"detected": 0, "deprecated": 0, "merged": 0, "resolved": 0}


# --------------------------------------------------------------------------- cron 校验
def test_cron_schema_accepts_and_rejects():
    from app.schemas import GovernanceScheduleUpdate
    ok = GovernanceScheduleUpdate(enabled=True, cron="0 */6 * * *", auto_deprecate=True, grace_days=14)
    assert ok.cron == "0 */6 * * *"
    with pytest.raises(ValueError):
        GovernanceScheduleUpdate(cron="not a cron")
    with pytest.raises(ValueError):
        GovernanceScheduleUpdate(cron="60 25 * * *")  # 越界字段


# --------------------------------------------------------------------------- 端点冒烟
def test_endpoints_schedule_kpis_candidates(authenticated):
    c = authenticated
    # 默认 schedule
    r = c.get("/api/governance/schedule"); assert r.status_code == 200
    assert r.json()["cron"] == "0 3 * * *"
    # 更新为合法 cron
    r = c.put("/api/governance/schedule", json={"enabled": True, "cron": "30 2 * * 1", "auto_deprecate": False, "grace_days": 21})
    assert r.status_code == 200 and r.json()["grace_days"] == 21
    assert r.json()["next_run_at"] is not None
    # 非法 cron → 422
    assert c.put("/api/governance/schedule", json={"enabled": True, "cron": "garbage"}).status_code == 422
    # KPIs / candidates 空态
    assert c.get("/api/governance/kpis").status_code == 200
    assert c.get("/api/governance/candidates").json() == {"items": []}


def test_merge_run_requires_candidate_ids(authenticated):
    r = authenticated.post("/api/governance/runs", json={"kind": "merge", "candidate_ids": []})
    assert r.status_code == 400
    # 空 merge 作业不应留下 job 行
    assert authenticated.get("/api/governance/runs").json()["items"] == []

