# SEMI-KB Console

企业级半导体本体、知识库、经营模型、仿真、场景文章和日报控制台。前端所有指标来自 `semi-kb` 或控制台数据库，不使用静态演示数据。同一组 N 个 Agent 由 LangGraph 按轮持续执行，PostgreSQL 保存业务状态和 checkpoint，直到用户选择立即停止、本轮结束后停止或再运行一轮后停止。

## 启动

```powershell
Copy-Item .env.example .env
.\scripts\dev.ps1
```

访问 `http://127.0.0.1:5173`，首次进入创建管理员，再到“系统设置”录入 API Key、企业微信 Key 和 QQ SMTP 授权码。凭据由后端加密保存，前端不回显明文。

停止前后端服务：

```powershell
.\scripts\stop.ps1
```

## PostgreSQL 与迁移

正式运行使用 `SEMI_KB_DB_PASSWORD` 用户环境变量，默认连接本机 `semi_kb_console` 数据库和同名用户。首次迁移执行：

```powershell
.\scripts\migrate-postgres.ps1
```

Alembic 管理业务表结构；`langgraph-checkpoint-postgres` 管理图 checkpoint。SQLite 仍用于隔离的快速单元测试。

仅在首次把旧 SQLite 数据导入空的 PostgreSQL 时使用：

```powershell
.\scripts\migrate-postgres.ps1 -MigrateSqlite -SqliteSource .\data\console.db
```

普通升级不要带 `-MigrateSqlite`；脚本会执行 Alembic 升级并验证 PostgreSQL 与 checkpoint，避免重复导入历史数据。

## 测试

```powershell
.\scripts\test.ps1
```

## 关键约束

- 每个子 Agent 独立选择 `web`、`model_prior` 或 `hybrid`。
- Web 模式保存搜索结果正文、URL、抓取时间和内容哈希；模型先验单独标注。
- 每轮产物单独留档；正式发布必须通过语义变更契约、来源对齐、能力问题、OWL、SHACL、推理、经营模型和仿真门禁。
- 每轮是独立 LangGraph；外层控制器负责无限循环，PostgreSQL 持久化暂停、取消、目标结束轮次、心跳和重启恢复。
- Agent 子图支持部分失败继续、输入哈希幂等、已保存模型响应复用、有界修复和失败候选隔离。
- vFab 未交付时保持 `awaiting_source`。
- 日报默认需要审核；关闭审核后只有自动校验通过才会直接发送。
- 导出中心支持本体、知识库、经营模型、仿真引擎、场景文章和完整包，任务进度与下载记录持久化保存。
- 正式本体写入仍由 `semi-kb` 的 OWL、SHACL、推理、经营模型、仿真和黄金问题门禁控制。
- 不要把任何密钥写入 `.env.example`、源码、日志或导出包。
