from __future__ import annotations

import json
from pathlib import Path

from app.services.semantic_pipeline import validate_and_store_agent_output
from app.services.semi_kb import semi_kb


# urn:pxai:semi:severity 是 property-map.json 里现有映射的 target，必为已声明本体属性。
DECLARED_TARGET = "urn:pxai:semi:severity"


def _live_feature_codes() -> tuple[str, str]:
    """从真实引擎现算 (一个未映射的真实源特征码, 一个已映射的真实源特征码)。

    自动映射闭环会持续消费 property-map.json，硬编码某个『未映射码』会随线上进展失效
    （tech_code 就已被 hasTechnologyCode 收编）。故按生产同源逻辑现算：real = 特征目录里
    的全部 feature_code；mapped = property-map.json 已占用码。取真实但未映射的第一个作
    可接受码，取真实且已映射的第一个作已占用码——与 _merge_property_mappings / 管道
    sanitizer 判据完全一致。
    """
    catalog = json.loads(
        (semi_kb.root / "build" / "source" / "feature-model-catalog.json").read_text(encoding="utf-8")
    )
    real: list[str] = []
    for sheet in catalog.get("sheets") or []:
        for feature in sheet.get("features") or []:
            code = str(feature.get("feature_code") or "").strip().lower()
            if code and code not in real:
                real.append(code)
    real.sort()
    property_map = json.loads(
        (semi_kb.root / "mappings" / "feature-model" / "property-map.json").read_text(encoding="utf-8")
    )
    mapped = {
        str(code).strip().lower()
        for entry in property_map.get("mappings") or []
        for code in entry.get("feature_codes") or []
    }
    unmapped = next(code for code in real if code not in mapped)
    occupied = next(code for code in real if code in mapped)
    return unmapped, occupied


def _raw_with_mappings(mappings: list[dict]) -> dict:
    # 场景叙事写产线现场真实痛点（本用例只验证映射清洗，痛点文本走场景质量防护栏须合规）。
    return {
        "summary": "工艺分类信息散落在多套系统，排障时难以快速核对",
        "customer_pains": ["工程师排查异常时需跨系统人工比对工艺编码，定位缓慢"],
        "feature_mapping_candidates": mappings,
        "scenario_article_markdown": "场景：产线工艺追溯。客户痛点：跨系统工艺编码口径不一致，排障工时高。经营与仿真含义：仅用于验证流程，不代表经营承诺。" * 3,
    }


def test_mapping_sanitizer_keeps_valid_and_drops_occupied_and_fake():
    unmapped_code, occupied_code = _live_feature_codes()
    raw = _raw_with_mappings([
        {"mapping": {"feature_codes": [unmapped_code, occupied_code, "definitely_not_a_real_feature"], "target_property": DECLARED_TARGET, "mapping_kind": "classification", "note": "工艺技术编码"}},
    ])
    result = validate_and_store_agent_output(raw, run_id="run-map-keep", round_number=1, agent_id="m1", source_mode="model_prior", model_id="gpt-test", evidence=[])
    assert len(result["mapping_files"]) == 1
    document = json.loads(Path(result["mapping_files"][0]).read_text(encoding="utf-8"))
    # 已映射码已占用、fake 不是真实特征 → 都丢弃；只保留未映射的真实码。
    assert document["feature_codes"] == [unmapped_code]
    assert document["target_property"] == DECLARED_TARGET
    assert document["provenance"]["source_type"] == "model_prior"


def test_mapping_sanitizer_drops_undeclared_target():
    raw = _raw_with_mappings([
        {"mapping": {"feature_codes": ["stage_code"], "target_property": "urn:pxai:semi:totallyMadeUpProperty", "mapping_kind": "attribute"}},
    ])
    result = validate_and_store_agent_output(raw, run_id="run-map-undeclared", round_number=1, agent_id="m2", source_mode="model_prior", model_id="gpt-test", evidence=[])
    assert result["mapping_files"] == []
    assert any("目标属性未声明" in warning for warning in result["sanitization_warnings"])


def test_merge_property_mappings_is_gate_safe(tmp_path):
    unmapped_code, occupied_code = _live_feature_codes()
    valid = tmp_path / "m-valid.json"
    valid.write_text(json.dumps({"feature_codes": [unmapped_code, occupied_code], "target_property": DECLARED_TARGET, "mapping_kind": "classification"}), encoding="utf-8")
    undeclared = tmp_path / "m-undeclared.json"
    undeclared.write_text(json.dumps({"feature_codes": [unmapped_code], "target_property": "urn:pxai:semi:totallyMadeUpProperty"}), encoding="utf-8")
    occupied = tmp_path / "m-occupied.json"
    occupied.write_text(json.dumps({"feature_codes": [occupied_code], "target_property": DECLARED_TARGET}), encoding="utf-8")

    property_map_path = semi_kb.root / "mappings" / "feature-model" / "property-map.json"
    before = property_map_path.read_text(encoding="utf-8")

    document, accepted, quarantine = semi_kb._merge_property_mappings([valid, undeclared, occupied])

    # 仅 valid 里的未映射码被接受（已映射码全局已占用；undeclared 目标非法）。
    assert accepted == 1
    merged_codes = [code for entry in document["mappings"] if entry.get("target_property") == DECLARED_TARGET for code in entry["feature_codes"]]
    assert unmapped_code in merged_codes
    assert any("目标属性未声明" in item["reason"] for item in quarantine)
    assert any("未占用真实特征码" in item["reason"] for item in quarantine)
    # 合并只返回文档、不落盘：磁盘上的 property-map.json 保持不变（发布由 process_candidates 负责）。
    assert property_map_path.read_text(encoding="utf-8") == before
