# 文档摄取流水线

一句话：**手上有一份新文档，它该走哪条通道、落到哪一层？** 这份文档回答它。

登记清单在 [`sources/registry.yaml`](../sources/registry.yaml)。本文讲通道选择、三层边界、和"加一份新文档"的具体命令。

## 三层与三通道

系统有三层存储，各由不同脚本从原始文件生成。**关键：只有推理层进 OWL/SHACL 推理**，另两层是供检索的派生产物。

| 层 | 存储 | 生成者 | 进 OWL 推理 |
|---|---|---|---|
| 推理层 | `knowledge/semantic/current.ttl` → `build/semantic/current.trig` | `migrate_semantic.py`（读 catalog + kb/*.yaml + business/models + parse semantic/*.ttl） | 是 |
| 检索层 | `build/index.json` | `build_index.py`（实体 + kb_case + vfab_dataset） | 否 |
| 知识层 | `knowledge/vfab/entries/*.json` | `vfab_prose_to_knowledge.py` | 否 |

对应三条摄取通道：

### 通道 1：结构化表格 → 检索层
适用：机台场景表、流程/配方/工序等**行列规整**的数据。

```bash
# 1. 清洗 md 表格为 CSV（每张表一个）
python scripts/vfab_markdown_to_tables.py <input.md> --out-dir sources/internal/vfab/raw
# 2. 人工在 sources/internal/vfab/manifest.json 为每个 CSV 指定 target_class
#    （必须是已声明的本体类，否则 align_sources.py 门禁失败）
python scripts/generate_vfab_manifest.py   # 或手工编辑
# 3. 校验 + 进检索层
python scripts/vfab_ingest.py && python scripts/build_index.py
```
产物：`build/index.json` 里 `kind=vfab_dataset` 的数据集级检索记录（含 equipment/phase/行数/字段数）。

### 通道 2：散文文档 → 知识层
适用：SEMI 标准、设备手册等 **PDF 转来的散文**，不适合表格化。

```bash
python scripts/vfab_prose_to_knowledge.py   # 拆章节 + 建术语倒排索引
```
产物：`knowledge/vfab/entries/*.json`（标题/摘要/正文/相关IRI/置信度/provenance）+ `cross-validation-index.json`。

### 通道 3：语义扩展 → 推理层
适用：需要**进 OWL 推理、被 SHACL 校验**的语义断言（新类、新个体、新关系）。

```bash
# 1. 生成符合契约的 JSON 提案，落 semantic_changesets/pending/
#    契约见 output-contracts/semantic-changeset.schema.json
# 2. 预检（OWL 推理 + SHACL 全套，不写入）
python scripts/apply_semantic_changeset.py --check
# 3. 【人工批量复核】通过后合并进 current.ttl
python scripts/apply_semantic_changeset.py
```
产物：合并进 `knowledge/semantic/current.ttl`，随后 `migrate_semantic.py` 纳入推理。

## 选通道：一个新文档来了怎么办

```
文档是行列规整的表格吗？
├─ 是 → 通道 1（结构化表格 → 检索层）
│        行级数据（如逐条 SECS 消息）过多？ index 仍按数据集级聚合，不逐行铺开
└─ 否 → 是散文/说明书吗？
         ├─ 是 → 通道 2（散文 → 知识层）
         └─ 否，是要断言的语义事实 → 通道 3（changeset → 推理层，需人工复核）
```

一份文档可能同时走多条通道：设备手册的正文走通道 2，从中抽出的"某状态机适用于某机型"这类断言走通道 3。

## 双信任模型边界（不可越过）

- **自动路径**：通道 1、2 可自动清洗、校验、建索引、落检索层/知识层。这两层不进 OWL 推理，改错了不污染推理结果。
- **人工路径**：通道 3 触及推理层。自动流程只能生成 draft 到 `semantic_changesets/pending/` 并跑 `--check` 预检，**绝不自动 promote**。合并进 `current.ttl` 必须经人工批量复核。

换句话说：机器可以起草推理层的改动并自证通过校验，但**落盘由人拍板**。

## 受限来源

文件 5（设备手册）标注 `classification: restricted`，含 "Confidential - Subject to NDA" 内容。约定：机密正文只留在知识层 entries（本地派生，不提交），**其机密文本绝不进入提交的 changeset / TTL**，推理层只保留元数据和结构性引用。

## 当前已登记来源

以 [`sources/registry.yaml`](../sources/registry.yaml) 为准，5 份文档：

| id | 通道 | 层 | 状态 |
|---|---|---|---|
| vfab.doc1.scenario_table | 结构化表格 | 检索 | 已摄取（86 场景） |
| vfab.doc2.process_data | 结构化表格 | 检索 | 已摄取（7 数据集） |
| vfab.doc3.himes_training | 散文 | 知识 | 已摄取 |
| vfab.doc4.semi_standard | 散文 | 知识 | 已摄取（有交叉校验索引） |
| vfab.doc5.equipment_manual | 散文 | 知识 | 已摄取（restricted / NDA） |
