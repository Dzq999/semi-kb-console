"""校验并标准化『素材导入』结构化文件（前端导入通道的引擎门禁）。

参数化版 vfab_ingest.py：对任意 source 目录（含 manifest.json + raw/）做同样的强校验——
格式白名单 / sha256 64位 / 原件哈希一致 / 路径不越界 / entity_keys 必须命中表头。额外
把 target_class 校验前移到本脚本（vfab 通道把该校验交给 align_sources.py，导入通道无对齐
环节，故就地校验）：target_class 必须是 ontology/modules/*.ttl 里已声明的类，否则门禁失败。

两处复用：
  - 采纳前轻量校验：--source-dir imports/drafts/<id>  --check（只校验不写线上目录）
  - 采纳时线上门禁：--source-dir sources/internal/imported（写 build/source/imported-catalog.json）

未交付（manifest 不存在）是显式状态，返回 0，不视为失败。
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent.parent
SEMI = "urn:pxai:semi:"
ALLOWED_FORMATS = {"xlsx", "xls", "csv", "tsv", "json", "parquet"}
ALLOWED_CLASSIFICATIONS = {"internal", "internal_confidential", "restricted"}
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
      "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}


def setup_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try: stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError): pass


def declared_terms() -> set[str]:
    """扫 ontology/modules/*.ttl 收集已声明的类/属性 IRI（与 align_sources.py 同一读法）。"""
    terms: set[str] = set()
    pattern = re.compile(r"\bsemi:([A-Za-z][A-Za-z0-9_-]*)\s+a\s+(?:owl:Class|owl:ObjectProperty|owl:DatatypeProperty)")
    modules = ROOT / "ontology" / "modules"
    if modules.is_dir():
        for path in sorted(modules.glob("*.ttl")):
            terms.update(SEMI + value for value in pattern.findall(path.read_text(encoding="utf-8")))
    return terms


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


def validate_source(source_dir: Path, terms: set[str]) -> list[dict]:
    """校验一个导入源目录，返回标准化 datasets；任何问题抛 ValueError。"""
    manifest = json.loads((source_dir / "manifest.json").read_text(encoding="utf-8"))
    unknown = set(manifest) - {"source_id", "delivered_at", "owner", "classification", "files"}
    if unknown: raise ValueError(f"manifest 含未知字段：{', '.join(sorted(unknown))}")
    if manifest.get("source_id") != "internal.imported": raise ValueError("source_id 必须是 internal.imported")
    classification = str(manifest.get("classification") or "")
    if classification not in ALLOWED_CLASSIFICATIONS: raise ValueError(f"非法 classification：{classification}")
    if classification == "restricted": raise ValueError("restricted 素材禁止采纳入库；只可暂存与记录元数据")
    if not manifest.get("delivered_at") or not manifest.get("files"): raise ValueError("缺少 delivered_at 或 files")
    datasets = []
    for item in manifest["files"]:
        unknown_item = set(item) - {"path", "format", "sha256", "target_class", "entity_keys", "time_field", "display_name"}
        if unknown_item: raise ValueError(f"文件条目含未知字段：{', '.join(sorted(unknown_item))}")
        for key in ("path", "format", "sha256", "target_class"):
            if not item.get(key): raise ValueError(f"文件条目缺少 {key}")
        fmt = str(item["format"]).lower()
        if fmt not in ALLOWED_FORMATS: raise ValueError(f"非法格式：{fmt}")
        if len(str(item["sha256"])) != 64 or any(x not in "0123456789abcdefABCDEF" for x in str(item["sha256"])):
            raise ValueError(f"非法 sha256：{item['path']}")
        target_class = str(item["target_class"])
        if target_class not in terms:
            raise ValueError(f"target_class 未在本体中声明：{target_class}（{item['path']}）")
        path = (source_dir / item["path"]).resolve()
        if source_dir.resolve() not in path.parents: raise ValueError(f"文件必须位于导入源目录内：{item['path']}")
        if not path.is_file(): raise ValueError(f"文件不存在：{item['path']}")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual.lower() != str(item["sha256"]).lower(): raise ValueError(f"哈希不匹配：{item['path']}")
        headers = file_headers(path, fmt)
        missing_keys = sorted(set(item.get("entity_keys") or []) - set(headers))
        if missing_keys: raise ValueError(f"{item['path']} 缺少主键字段：{', '.join(missing_keys)}")
        datasets.append({"path": item["path"], "format": fmt, "sha256": actual,
                         "target_class": target_class, "entity_keys": item.get("entity_keys") or [],
                         "time_field": item.get("time_field"), "headers": headers, "field_count": len(headers)})
    return datasets


def main() -> int:
    setup_console()
    parser = argparse.ArgumentParser(description="校验/标准化素材导入结构化文件")
    parser.add_argument("--source-dir", default="sources/internal/imported",
                        help="含 manifest.json + raw/ 的源目录（相对引擎根或绝对路径）")
    parser.add_argument("--check", action="store_true", help="只校验来源，不写线上派生目录/catalog")
    args = parser.parse_args()

    source_dir = Path(args.source_dir)
    if not source_dir.is_absolute():
        source_dir = (ROOT / source_dir).resolve()

    out = ROOT / "build" / "source" / "imported-catalog.json"
    if not args.check:
        out.parent.mkdir(parents=True, exist_ok=True)

    manifest_path = source_dir / "manifest.json"
    if not manifest_path.is_file():
        report = {"status": "awaiting_source", "generated_at": datetime.now(timezone.utc).isoformat(), "datasets": []}
        if not args.check:
            out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False))
        return 0
    try:
        datasets = validate_source(source_dir, declared_terms())
        report = {"status": "available", "generated_at": datetime.now(timezone.utc).isoformat(),
                  "source_dir": source_dir.as_posix(), "datasets": datasets}
        if not args.check:
            out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False))
        return 0
    except (OSError, ValueError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        report = {"status": "fail", "generated_at": datetime.now(timezone.utc).isoformat(),
                  "error": str(exc), "datasets": []}
        if not args.check:
            out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
