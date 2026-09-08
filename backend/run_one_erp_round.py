"""一次性脚本：进程内跑一轮（重点 ERP），落库 + 发布，供日报核对。

- max_rounds=1 / continuous=false → 恰好一轮后 completed
- publish_changes=true → 新增类/属性/关系/实例真实写入 current.ttl 并计入新增
- 快照由本次新代码 semantic_counts 计算，故 class_system_*/property_system_*/
  relation_system_* 键会进入 metrics_before/after_json，日报四列可正常差分
运行：backend 目录下 ../.venv/Scripts/python run_one_erp_round.py
"""
import asyncio, json, uuid
from datetime import datetime, timezone

from app.db import SessionLocal
from app.config import settings
from app.models import Run, AgentRun, RunRound
from app.services.orchestrator import orchestrator
from sqlalchemy import select


def build_config() -> dict:
    with open(r"D:\AI_Coding\last_run_config.json", encoding="utf-8") as f:
        cfg = json.load(f)
    # 重点 ERP：强化 ERP 联动 agent 与语义建模 agent 的目标，明确产出新增本体
    for a in cfg["agents"]:
        if a.get("domain") == "erp":
            a["objective"] = ("重点扩充 ERP/SAP 本体：新增 FI 财务与 SD/O2C 的类、"
                              "对象/数据属性、关系（如定价条件、税码、期间、应收结算、"
                              "合同负债过账等），并建立与制造侧 WIP·产能·产线事件的跨系统关系")
        if a.get("domain") == "core":
            a["objective"] = "面向 ERP 扩充：新增类、属性、关系、公理与规则"
    cfg["max_rounds"] = 1
    cfg["continuous"] = False
    cfg["round_interval_seconds"] = 0
    cfg["publish_changes"] = True
    cfg["auto_repair"] = True
    cfg["max_auto_repair_attempts"] = 1
    return cfg


def create_run(cfg: dict) -> str:
    run_id = "run-" + uuid.uuid4().hex[:16]
    cfg = dict(cfg)
    cfg["orchestrator_engine"] = settings.orchestrator_engine
    with SessionLocal() as db:
        db.add(Run(
            id=run_id, user_id=1, model_id=cfg["model_id"],
            config_json=json.dumps(cfg, ensure_ascii=False),
            orchestrator_engine=settings.orchestrator_engine,
            checkpoint_thread_id=f"{run_id}:round:1",
        ))
        for i, a in enumerate(cfg["agents"], 1):
            db.add(AgentRun(
                id=f"{run_id}-a{i:02d}", run_id=run_id, name=a["name"], role=a["role"],
                domain=a["domain"], objective=a["objective"], source_mode=a["source_mode"],
                model_id=a.get("model_override") or cfg["model_id"],
            ))
        db.commit()
    return run_id


async def main():
    cfg = build_config()
    run_id = create_run(cfg)
    print(f"[created] {run_id}  (ERP-focused, 1 round, publish=true)", flush=True)
    await orchestrator.execute(run_id)
    # 报告结果
    with SessionLocal() as db:
        run = db.get(Run, run_id)
        rounds = db.scalars(select(RunRound).where(RunRound.run_id == run_id).order_by(RunRound.round_number)).all()
        print(f"[done] run.status={run.status} error={run.error}", flush=True)
        for r in rounds:
            before = json.loads(r.metrics_before_json or "{}")
            after = json.loads(r.metrics_after_json or "{}")
            print(f"  round {r.round_number}: status={r.status}", flush=True)
            for k in ("classes","properties","relations","individuals",
                      "class_system_manufacturing","class_system_erp",
                      "property_system_manufacturing","property_system_erp",
                      "relation_system_manufacturing","relation_system_erp",
                      "system_manufacturing","system_erp"):
                b, a = int(before.get(k,0)), int(after.get(k,0))
                if b or a:
                    print(f"    {k}: {b} -> {a}  (+{a-b})", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
