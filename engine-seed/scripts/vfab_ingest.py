"""校验并标准化 vFab 交付；未交付是显式状态，不视为失败。"""
from __future__ import annotations

import csv
import hashlib
import json
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent.parent
SOURCE_ROOT = ROOT / "sources" / "internal" / "vfab"
MANIFEST = SOURCE_ROOT / "manifest.json"
ALLOWED_FORMATS = {"xlsx", "xls", "csv", "tsv", "json", "parquet"}
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
      "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}


def setup_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try: stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError): pass


def xlsx_headers(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in root.findall("m:si", NS):
                shared.append("".join(node.text or "" for node in item.findall(".//m:t", NS)))
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        targets = {x.attrib["Id"]: x.attrib["Target"] for x in rels}
        sheet = workbook.find("m:sheets/m:sheet", NS)
        if sheet is None: return []
        target = targets[sheet.attrib[f"{{{NS['r']}}}id"]].lstrip("/")
        if not target.startswith("xl/"): target = "xl/" + target
        root = ET.fromstring(archive.read(target))
        row = root.find(".//m:sheetData/m:row", NS)
        headers: list[str] = []
        for cell in row.findall("m:c", NS) if row is not None else []:
            value = cell.find("m:v", NS)
            raw = value.text if value is not None else ""
            if cell.attrib.get("t") == "s" and raw:
                raw = shared[int(raw)]
            elif cell.attrib.get("t") == "inlineStr":
                raw = "".join(x.text or "" for x in cell.findall(".//m:t", NS))
            headers.append(str(raw).strip())
        return [x for x in headers if x]


def file_headers(path: Path, fmt: str) -> list[str]:
    if fmt == "xlsx": return xlsx_headers(path)
    if fmt in {"csv", "tsv"}:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return next(csv.reader(handle, delimiter="\t" if fmt == "tsv" else ","), [])
    if fmt == "json":
        data = json.loads(path.read_text(encoding="utf-8"))
        row = data[0] if isinstance(data, list) and data else data
        return list(row) if isinstance(row, dict) else []
    if fmt == "xls":
        try: import xlrd
        except ModuleNotFoundError as exc: raise ValueError("读取 xls 需要 requirements.txt 中的 xlrd") from exc
        sheet = xlrd.open_workbook(path).sheet_by_index(0)
        return [str(sheet.cell_value(0, col)).strip() for col in range(sheet.ncols)]
    if fmt == "parquet":
        try: import pyarrow.parquet as parquet
        except ModuleNotFoundError as exc: raise ValueError("读取 parquet 需要 requirements.txt 中的 pyarrow") from exc
        return list(parquet.read_schema(path).names)
    raise ValueError(f"不支持的格式：{fmt}")


def main() -> int:
    setup_console()
    out = ROOT / "build" / "source" / "vfab-catalog.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    if not MANIFEST.is_file():
        report = {"status": "awaiting_source", "generated_at": datetime.now(timezone.utc).isoformat(), "datasets": []}
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("vFab：awaiting_source")
        return 0
    try:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        unknown = set(manifest) - {"source_id", "delivered_at", "owner", "classification", "files"}
        if unknown: raise ValueError(f"manifest 含未知字段：{', '.join(sorted(unknown))}")
        if manifest.get("source_id") != "internal.vfab": raise ValueError("source_id 必须是 internal.vfab")
        if not manifest.get("delivered_at") or not manifest.get("files"): raise ValueError("缺少 delivered_at 或 files")
        datasets = []
        for item in manifest["files"]:
            unknown_item = set(item) - {"path", "format", "sha256", "target_class", "entity_keys", "time_field"}
            if unknown_item: raise ValueError(f"vFab 文件条目含未知字段：{', '.join(sorted(unknown_item))}")
            for key in ("path", "format", "sha256", "target_class"):
                if not item.get(key): raise ValueError(f"vFab 文件条目缺少 {key}")
            fmt = str(item["format"]).lower()
            if fmt not in ALLOWED_FORMATS: raise ValueError(f"非法格式：{fmt}")
            if len(str(item["sha256"])) != 64 or any(x not in "0123456789abcdefABCDEF" for x in str(item["sha256"])):
                raise ValueError(f"非法 sha256：{item['path']}")
            path = (SOURCE_ROOT / item["path"]).resolve()
            if SOURCE_ROOT.resolve() not in path.parents: raise ValueError(f"文件必须位于 vFab 源目录：{item['path']}")
            if not path.is_file(): raise ValueError(f"文件不存在：{item['path']}")
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual.lower() != str(item["sha256"]).lower(): raise ValueError(f"哈希不匹配：{item['path']}")
            headers = file_headers(path, fmt)
            missing_keys = sorted(set(item.get("entity_keys") or []) - set(headers))
            if missing_keys: raise ValueError(f"{item['path']} 缺少主键字段：{', '.join(missing_keys)}")
            datasets.append({"path": item["path"], "format": fmt, "sha256": actual,
                             "target_class": item["target_class"], "entity_keys": item.get("entity_keys") or [],
                             "headers": headers, "field_count": len(headers)})
        report = {"status": "available", "generated_at": datetime.now(timezone.utc).isoformat(), "datasets": datasets}
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"vFab 校验通过：数据集 {len(datasets)}")
        return 0
    except (OSError, ValueError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        report = {"status": "fail", "generated_at": datetime.now(timezone.utc).isoformat(), "error": str(exc), "datasets": []}
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"vFab 校验失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
