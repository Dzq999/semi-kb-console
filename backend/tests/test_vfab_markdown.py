"""vFab 机台场景表清洗工具的测试：多级合并表头扁平化 / 空列剔除 / equipment·phase 上下文列。

被测对象是独立离线 CLI（后端从不 in-process import 引擎脚本），故这里也用 importlib
按路径加载它的纯函数 convert，喂手写 markdown 断言扁平结构，不跑子进程、不碰引擎构建。
"""
from __future__ import annotations

import importlib.util

from app.config import settings


def _load_convert():
    script = settings.engine_root / "scripts" / "vfab_markdown_to_tables.py"
    spec = importlib.util.spec_from_file_location("vfab_markdown_to_tables", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.convert


def test_convert_merges_multilevel_header_and_drops_empty_column():
    md = "\n".join([
        "## Aleris 场景",
        "",
        "### Startup",
        "",
        "| Description | Notes | State Machines |  |",
        "| --- | --- | --- | --- |",
        "|  |  | Control State Model | Access Mode |",
        "| Purge line |  | Idle | local |",
        "| Heat up |  | Running | remote |",
    ])
    convert = _load_convert()
    tables = convert(md)
    assert len(tables) == 1
    table = tables[0]

    # equipment/phase 作为上下文列补在最前。
    assert table["columns"][:2] == ["equipment", "phase"]
    # 组头 State Machines 跨两列 + 子表头 → 拼成单层列名。
    assert "state_machines__control_state_model" in table["columns"]
    assert "state_machines__access_mode" in table["columns"]
    # notes 整列为空 → 被剔除。
    assert "notes" not in table["columns"]

    rows = table["rows"]
    assert len(rows) == 2
    assert rows[0][:2] == ["Aleris", "Startup"]
    ctrl = table["columns"].index("state_machines__control_state_model")
    assert rows[0][ctrl] == "Idle" and rows[1][ctrl] == "Running"


def test_convert_tracks_equipment_and_phase_across_sections():
    md = "\n".join([
        "## Aleris 场景",
        "### Startup",
        "| Description | Value |",
        "| --- | --- |",
        "| a | 1 |",
        "## Reflexion 场景",
        "### Idle",
        "| Description | Value |",
        "| --- | --- |",
        "| b | 2 |",
    ])
    convert = _load_convert()
    tables = convert(md)
    assert len(tables) == 2
    first, second = tables
    assert first["rows"][0][:2] == ["Aleris", "Startup"]
    assert second["rows"][0][:2] == ["Reflexion", "Idle"]
    # 不同 equipment/phase → 不同 slug。
    assert first["slug"] != second["slug"]


def test_convert_ignores_non_table_prose():
    md = "\n".join([
        "## Aleris 场景",
        "这是一段散文说明，不应被当作表格解析。",
        "### Startup",
        "更多说明文字。",
    ])
    convert = _load_convert()
    assert convert(md) == []
