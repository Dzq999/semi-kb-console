"""把 vFab『机台场景表』markdown 清洗+扁平化为可摄取的 CSV（每张场景表一个）。

独立离线 CLI，后端从不 in-process import 引擎脚本，故清洗也走独立脚本。做三件事：
1. 合并多级表头：主表头里 "State Machines" 这类组头跨若干列，其子列名在 `---` 分隔行后的
   子表头行里——拼成单层列名（如 state_machines__control_state_model）。
2. 每个 `## <设备> 场景` / `### <阶段>` 作为 equipment/phase 上下文列补进每行。
3. 剔除全空列与全空行；列头去重、稳定可被 vfab_ingest.py 校验。

【不决定 target_class】——工具只做清洗+扁平化，target_class 由人在 manifest.json 里指定，
且须是已声明本体类（align_sources.py 会校验，未声明则 undeclared_vfab_targets 门禁失败）。

用法：python vfab_markdown_to_tables.py <input.md> [--out-dir sources/internal/vfab/raw]
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path


def _slug(text: str, fallback: str = "col") -> str:
    text = re.sub(r"<br\s*/?>", " ", text or "", flags=re.I)
    text = re.sub(r"[^a-z0-9]+", "_", text.strip().lower()).strip("_")
    return text or fallback


def _clean(cell: str) -> str:
    cell = re.sub(r"<br\s*/?>", " ", cell or "", flags=re.I)
    return re.sub(r"\s+", " ", cell).strip()


def _split_row(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [_clean(cell) for cell in line.split("|")]


def _is_separator(cells: list[str]) -> bool:
    """markdown 表格分隔行：非空单元格全是 --- / :--- / ---: 形态。"""
    named = [cell for cell in cells if cell]
    return bool(named) and all(re.fullmatch(r":?-{2,}:?", cell) for cell in named)


def _detect_groups(header: list[str]) -> list[int | None]:
    """识别跨列组头：一个有名列后跟 ≥1 个空列，视作组，组内每列记下组头索引。"""
    n = len(header)
    group_of: list[int | None] = [None] * n
    i = 0
    while i < n:
        if header[i]:
            j = i + 1
            while j < n and not header[j]:
                j += 1
            if j - i > 1:
                for k in range(i, j):
                    group_of[k] = i
            i = j
        else:
            i += 1
    return group_of


def _looks_like_subheader(row: list[str], header: list[str], group_of: list[int | None]) -> bool:
    """子表头行：原子有名列在此行为空，且至少一个组内列有文本（即它在给组填子列名）。"""
    if not any(g is not None for g in group_of):
        return False
    for i, g in enumerate(group_of):
        if g is None and header[i] and (row[i] if i < len(row) else ""):
            return False
    return any((row[i] if i < len(row) else "") for i, g in enumerate(group_of) if g is not None)


def _merge_headers(header: list[str], subheader: list[str] | None, group_of: list[int | None]) -> list[str]:
    cols: list[str] = []
    for i in range(len(header)):
        main = header[i]
        sub = subheader[i] if subheader and i < len(subheader) else ""
        group = group_of[i]
        if group is not None:
            base = _slug(header[group])
            cols.append(f"{base}__{_slug(sub)}" if sub else f"{base}_{i - group}")
        elif main:
            cols.append(_slug(main))
        elif sub:
            cols.append(_slug(sub))
        else:
            cols.append(f"col_{i}")
    return cols
def _parse_table(block: list[str]) -> tuple[list[str], list[list[str]]] | None:
    rows = [_split_row(line) for line in block]
    if len(rows) < 2 or not _is_separator(rows[1]):
        return None
    header = rows[0]
    body = rows[2:]
    group_of = _detect_groups(header)
    subheader = None
    if body and _looks_like_subheader(body[0], header, group_of):
        subheader = body[0]
        body = body[1:]
    return _merge_headers(header, subheader, group_of), body


def _finalize(cols: list[str], body: list[list[str]], equipment: str, phase: str) -> dict | None:
    """列头去重 + 补 equipment/phase 上下文列 + 剔除全空行与全空列。"""
    seen: dict[str, int] = {}
    final_cols: list[str] = []
    for col in cols:
        if col in seen:
            seen[col] += 1
            final_cols.append(f"{col}_{seen[col]}")
        else:
            seen[col] = 0
            final_cols.append(col)
    records: list[list[str]] = []
    for raw in body:
        cells = [(raw[i] if i < len(raw) else "") for i in range(len(final_cols))]
        if any(cells):
            records.append(cells)
    if not records:
        return None
    keep = [idx for idx in range(len(final_cols)) if any(rec[idx] for rec in records)]
    columns = ["equipment", "phase"] + [final_cols[idx] for idx in keep]
    out_rows = [[equipment, phase] + [rec[idx] for idx in keep] for rec in records]
    slug = f"{_slug(equipment)}__{_slug(phase)}" if phase else _slug(equipment, "table")
    return {"slug": slug or "table", "columns": columns, "rows": out_rows}


def convert(md_text: str) -> list[dict]:
    """解析 markdown → 每张场景表一个 {slug, columns, rows}。纯函数，供 CLI 与测试复用。"""
    lines = md_text.splitlines()
    equipment = ""
    phase = ""
    outputs: list[dict] = []
    i = 0
    n = len(lines)
    while i < n:
        stripped = lines[i].strip()
        if stripped.startswith("### "):
            phase = stripped[4:].strip()
            i += 1
        elif stripped.startswith("## "):
            # 设备标题形如 `## <设备> 场景`——「场景」是样板后缀，剥掉只留设备名做上下文列。
            equipment = re.sub(r"\s*场景\s*$", "", stripped[3:]).strip()
            phase = ""
            i += 1
        elif stripped.startswith("|"):
            block: list[str] = []
            while i < n and lines[i].strip().startswith("|"):
                block.append(lines[i])
                i += 1
            parsed = _parse_table(block)
            if parsed:
                table = _finalize(parsed[0], parsed[1], equipment, phase)
                if table:
                    outputs.append(table)
        else:
            i += 1
    return outputs


def _write(outputs: list[dict], out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    used: dict[str, int] = {}
    written: list[Path] = []
    for table in outputs:
        slug = table["slug"]
        used[slug] = used.get(slug, 0) + 1
        name = slug if used[slug] == 1 else f"{slug}_{used[slug]}"
        path = out_dir / f"{name}.csv"
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(table["columns"])
            writer.writerows(table["rows"])
        written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description="清洗 vFab 机台场景表 markdown 为扁平 CSV")
    parser.add_argument("input", help="输入 markdown 路径")
    parser.add_argument("--out-dir", default=None, help="输出目录，默认 sources/internal/vfab/raw")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parent.parent
    out_dir = Path(args.out_dir) if args.out_dir else root / "sources" / "internal" / "vfab" / "raw"
    outputs = convert(Path(args.input).read_text(encoding="utf-8"))
    written = _write(outputs, out_dir)
    for path in written:
        print(path.as_posix())
    print(f"生成 {len(written)} 个 CSV → {out_dir.as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
