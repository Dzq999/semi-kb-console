# 输出契约：兼容 YAML 变更提案

仅用于维护迁移期旧目录。后续 OWL/ABox 更新使用 `semantic-changeset.schema.json`。

文件位置 `changesets/pending/YYYYMMDD-<topic>.yaml`。

## 格式

```yaml
changeset:                      # 顶层键必须是 changeset，不是 meta
  id: 20260826-euv-litho
  title: 补充 EUV 光刻工艺与随机缺陷
  created_at: "2026-08-26"
  author: agent:kb-refresh
  rationale: |
    为什么要做这次变更，补的是哪个缺口。
    写清动机，审核者据此判断价值，而不是逐条猜意图。

additions:
  entities:
    - target_file: ontology/fab/entities/12-processes-patterning.yaml
      item:
        id: fab.process.euv_exposure
        type: Process
        name_zh: EUV 曝光
        name_en: EUV Exposure
        domain: fab
        description: ...
        stage: fab.stage.photo
        provenance:
          source_type: web
          ref: https://<真实可验证的 URL>
          confidence: medium
          created_at: "2026-08-26"
          reviewed_by: null

  relations:
    - target_file: ontology/fab/relations/21-risk-graph.yaml
      item:
        from: fab.process.euv_exposure
        type: may_cause
        to: fab.anomaly.stochastic_defect
        note: 可选说明

  kb_cases:
    - target_file: kb/fab/photo-etch-cases.yaml
      item:
        id: kb.fab.stochastic_defect_triage
        ...
```

## 要求

- **键名必须精确匹配**：顶层 `changeset`、条目内 `target_file`。
  写成 `meta` 或 `target` 不会报错——`apply_changeset.py` 找不到 `target_file`
  时打印"缺少 target_file，跳过"然后继续，结论是"自动放行 0 条 | 待人工审核 0 条"，
  退出码 0。定时任务看退出码会以为成功，实际什么都没合并。
  裁决输出里出现 `标题：None` 就是顶层键写错了的信号。
- `target_file` 必须是已存在的文件路径。合并是文本级追加，保留目标文件原有注释。
- 每个 `item` 自身必须完整合法，能独立通过 validate。
- `web` 来源必须有**真实可验证**的 URL。校验器只检查字段是否存在，
  填占位链接能通过校验但会污染溯源链——比标 `model_prior` 更糟，
  因为它伪装成了可核查的证据。
- 提案内有依赖时把被依赖项（实体）排在前面。即使排序正确，
  被依赖项转人工后依赖项也会一并转人工，这是预期行为。
- `rationale` 不要写成条目清单的复述，要写清补的是哪个缺口。

## 审核结果流转

- 全部自动合并 → 移入 `changesets/applied/`
- 存在转人工条目 → 留在 `pending/`，人工处理后重跑
- 人工判定不采纳 → 移入 `changesets/rejected/`
