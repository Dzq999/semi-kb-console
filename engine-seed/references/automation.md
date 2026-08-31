# 自动执行

运行环境先安装 `requirements.txt`；缺少语义依赖时完整校验必须失败，不能降级为“通过”。

`python scripts/run_loop.py` 是增量验证入口：按输入指纹跳过未变化的重建阶段，但始终执行语义、经营仿真和黄金问题守门。`--force` 强制全量重建。

定时任务中的当前 Agent 先执行 `prompts/kb-refresh.md` 生成语义提案，再运行 `python scripts/daily_refresh.py` 完成自动合并、全链校验和可回退提交。脚本不硬编码模型供应商 CLI。新增数量不限；按来源和问题域拆分提案，任何批次失败只影响该批次。

执行顺序：源文件哈希 → 来源对齐 → 问题域依赖 → YAML 兼容校验 → RDF 迁移 → OWL-RL/SHACL/规则 → 索引 → 经营仿真 → 黄金问题集。

周期执行由外部调度器调用上述入口。不要在 skill 文档中固化机器相关的 cron 表达式。
