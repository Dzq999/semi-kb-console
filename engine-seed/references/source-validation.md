# 来源与交叉验证

## 内部特征模型

- 原始文件：`sources/internal/feature-model/raw/AI-Fab-Feature-Model.xlsx`
- 清单与哈希：`sources/internal/feature-model/manifest.json`
- 实体映射：`mappings/feature-model/entity-map.json`
- 执行：`python scripts/source_ingest.py`，再运行 `python scripts/align_sources.py --check`

内部特征只证明其覆盖的源实体和字段。没有现场实例数据时，不生成虚构设备、批次或观测。

## vFab

资料按 `sources/internal/vfab/contract/schema-contract.json` 交付，并提供正式 manifest。未发现正式 manifest 时状态保持 `awaiting_source`。

对齐状态使用：`confirmed`、`single_source_confirmed`、`partially_supported`、`conflicting`、`not_covered`。冲突不静默覆盖，写入 `build/reports/` 并阻断依赖该事实的高风险结果。

## 来源要求

- web：必须是真实可访问 URL。
- internal_feature/vfab/observed：必须有 `source_ref`、文件哈希或数据集引用。
- model_prior/assumption：可用于候选与情景推演，不得表述为已验证现场事实。
