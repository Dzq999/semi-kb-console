"""equipmentCode 缺失返修：设备个体自身 IRI 应确定性派生出编码，避免 SHACL minCount 回滚。

复现的 bug：Agent 候选声明 `semi:equip_etcher_e01 a semi:Equipment` 却漏填 `semi:equipmentCode`，
EquipmentShape 的 minCount=1 触发 Violation，连累整批语义/场景产物回滚。修复在
`apply_semantic_changeset.derive_equipment_code`：从个体标识确定性补齐编码（主数据标识，非实测值），
与 BusinessVariable.identifier 由 IRI 后缀补齐同源。这里只测纯函数，无需真子进程/引擎图。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

from app.config import settings

_spec = importlib.util.spec_from_file_location(
    "engine_apply_semantic_changeset",
    settings.engine_root / "scripts" / "apply_semantic_changeset.py",
)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
derive_equipment_code = _module.derive_equipment_code


def test_derives_uppercase_hyphenated_code_from_iri_suffix():
    # 复现个体：equip_etcher_e01 → ETCHER-E01（大写连字符，符合 ET01/CVD-02/ETCH-01 现有约定）。
    assert derive_equipment_code("urn:pxai:semi:equip_etcher_e01") == "ETCHER-E01"


def test_strips_equipment_and_equip_prefixes():
    assert derive_equipment_code("urn:pxai:semi:equipment_cvd_02") == "CVD-02"
    assert derive_equipment_code("urn:pxai:semi:equip_etcher_ET01") == "ETCHER-ET01"


def test_handles_camel_and_slash_and_never_empty():
    # 带路径/驼峰的标识按后缀取用；已是编码样式的原样保留。
    assert derive_equipment_code("http://ex/EtcherToolA_07") == "ETCHERTOOLA-07"
    assert derive_equipment_code("urn:pxai:semi:EQP-XYZ-1") == "EQP-XYZ-1"
    # 纯前缀等退化输入必须回退出非空编码，绝不产出空串（空串同样会触 minCount）。
    assert derive_equipment_code("urn:pxai:semi:equip_")


def test_is_idempotent_on_already_clean_code():
    once = derive_equipment_code("urn:pxai:semi:equip_etcher_e01")
    assert derive_equipment_code(f"urn:pxai:semi:{once}") == once
