"""Parse immutable internal source files into a deterministic feature catalog."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent.parent
SOURCE_ROOT = ROOT / "sources" / "internal" / "feature-model"
MANIFEST = SOURCE_ROOT / "manifest.json"
OUTPUT = ROOT / "build" / "source" / "feature-model-catalog.json"
REPORT = ROOT / "build" / "reports" / "source-ingest.json"


def setup_console() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def clean(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


@dataclass
class SheetData:
    title: str
    values: list[list[Any]]
    max_row: int
    max_column: int


NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"


def _column_index(reference: str) -> int:
    letters = re.match(r"[A-Z]+", reference.upper())
    if not letters:
        return 0
    result = 0
    for char in letters.group(0):
        result = result * 26 + ord(char) - 64
    return result - 1


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    return ["".join(node.text or "" for node in item.iter(f"{{{NS_MAIN}}}t"))
            for item in root.findall(f"{{{NS_MAIN}}}si")]


def read_xlsx(path: Path) -> dict[str, SheetData]:
    with zipfile.ZipFile(path) as archive:
        shared = _shared_strings(archive)
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        rels_root = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        rels = {node.attrib["Id"]: node.attrib["Target"]
                for node in rels_root.findall(f"{{{NS_PKG_REL}}}Relationship")}
        result: dict[str, SheetData] = {}
        for sheet in workbook.findall(f".//{{{NS_MAIN}}}sheet"):
            title = sheet.attrib["name"]
            target = rels[sheet.attrib[f"{{{NS_REL}}}id"]].lstrip("/")
            member = target if target.startswith("xl/") else f"xl/{target}"
            root = ET.fromstring(archive.read(member))
            row_map: dict[int, dict[int, Any]] = {}
            max_row = max_col = 0
            for cell in root.findall(f".//{{{NS_MAIN}}}c"):
                reference = cell.attrib.get("r", "A1")
                row_match = re.search(r"\d+", reference)
                row_index = int(row_match.group(0)) - 1 if row_match else 0
                col_index = _column_index(reference)
                kind = cell.attrib.get("t")
                value_node = cell.find(f"{{{NS_MAIN}}}v")
                if kind == "inlineStr":
                    value = "".join(n.text or "" for n in cell.iter(f"{{{NS_MAIN}}}t"))
                elif value_node is None:
                    value = None
                elif kind == "s":
                    index = int(value_node.text or 0)
                    value = shared[index] if 0 <= index < len(shared) else None
                elif kind == "b":
                    value = value_node.text == "1"
                elif kind in {"str", "e"}:
                    value = value_node.text
                else:
                    raw = value_node.text
                    try:
                        number = float(raw) if raw is not None else None
                        value = int(number) if number is not None and number.is_integer() else number
                    except (TypeError, ValueError):
                        value = raw
                row_map.setdefault(row_index, {})[col_index] = clean(value)
                max_row, max_col = max(max_row, row_index + 1), max(max_col, col_index + 1)
            matrix = []
            for row_index in range(max_row):
                cells = row_map.get(row_index, {})
                matrix.append([cells.get(col_index) for col_index in range(max_col)])
            result[title] = SheetData(title, matrix, max_row, max_col)
        return result


def rows(ws: SheetData, min_row: int = 2) -> list[list[Any]]:
    return ws.values[min_row - 1:]


def parse_themes(wb) -> list[dict[str, Any]]:
    out = []
    for row in rows(wb["主题域"]):
        if len(row) >= 2 and row[0] and row[1]:
            out.append({"name": row[0], "code": str(row[1]), "description": row[2] if len(row) > 2 else None})
    return out


def parse_entities(wb) -> list[dict[str, Any]]:
    out = []
    for row in rows(wb["实体列表"]):
        if len(row) >= 4 and row[0] and row[1]:
            out.append({
                "name": row[0], "code": str(row[1]), "description": row[2],
                "theme": row[3], "table_shape": row[4] if len(row) > 4 else None,
                "requirement_source": row[5] if len(row) > 5 else None,
                "notes": row[6] if len(row) > 6 else None,
            })
    return out


def parse_tables(wb) -> list[dict[str, Any]]:
    out = []
    for row in rows(wb["表信息"]):
        if len(row) >= 6 and row[0]:
            out.append({
                "table": row[0], "description": row[1], "table_type": row[2],
                "theme": row[3], "theme_name": row[4], "entity": row[5],
                "feature_count": row[6] if len(row) > 6 else None,
            })
    return out


def parse_feature_sheet(ws: SheetData) -> list[dict[str, Any]]:
    out = []
    for row_number, row in enumerate(rows(ws), 2):
        padded = row + [None] * max(0, 11 - len(row))
        feature_name, feature_code = padded[3], padded[4]
        if not feature_name and not feature_code:
            continue
        out.append({
            "row": row_number,
            "source_table": padded[0], "source_field": padded[1],
            "feature_description": padded[2], "feature_name": feature_name,
            "feature_code": feature_code, "data_type": padded[5],
            "feature_type": padded[6], "notes": padded[7],
            "target_table": padded[8], "mapping_note": padded[9],
            "requirement_source": padded[10],
        })
    return out


def build_catalog() -> tuple[dict[str, Any], dict[str, Any]]:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    source = ROOT / manifest["canonical_file"]
    if not source.is_file():
        raise FileNotFoundError(f"内部特征模型不存在：{source}")
    actual_hash = sha256(source)
    expected_hash = str(manifest["sha256"]).lower()
    if actual_hash.lower() != expected_hash:
        raise ValueError(f"内部特征模型哈希不匹配：expected={expected_hash} actual={actual_hash}")

    wb = read_xlsx(source)
    summary = {"主题域", "实体列表", "表信息"}
    sheets = []
    for ws in wb.values():
        item = {"name": ws.title, "rows": ws.max_row, "columns": ws.max_column}
        if ws.title not in summary:
            item["features"] = parse_feature_sheet(ws)
        sheets.append(item)
    catalog = {
        "source_id": manifest["source_id"], "source_sha256": actual_hash,
        "themes": parse_themes(wb), "entities": parse_entities(wb),
        "tables": parse_tables(wb), "sheets": sheets,
    }
    feature_total = sum(len(sheet.get("features", [])) for sheet in sheets)
    report = {
        "status": "ok", "source_id": manifest["source_id"], "sha256": actual_hash,
        "sheet_count": len(sheets), "theme_count": len(catalog["themes"]),
        "entity_count": len(catalog["entities"]), "table_count": len(catalog["tables"]),
        "feature_row_count": feature_total,
    }
    return catalog, report


def main() -> int:
    setup_console()
    parser = argparse.ArgumentParser(description="导入内部AI Fab实体特征模型")
    parser.add_argument("--check", action="store_true", help="只校验来源，不写派生产物")
    args = parser.parse_args()
    try:
        catalog, report = build_catalog()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if not args.check:
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
        REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"内部特征模型校验通过：主题域 {report['theme_count']} | 实体 {report['entity_count']} | "
        f"表 {report['table_count']} | 工作表 {report['sheet_count']} | 特征行 {report['feature_row_count']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
