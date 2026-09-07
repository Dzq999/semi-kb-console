"""校验 OWL、SHACL、推理规则、生成数据集和来源对齐。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def setup_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def dependency_error(name: str) -> int:
    report = {
        "status": "blocked",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "issues": [{"severity": "error", "code": "SEM-DEP", "message": f"缺少依赖 {name}；请安装 requirements.txt"}],
    }
    out = ROOT / "build" / "reports" / "semantic-validation.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(report["issues"][0]["message"], file=sys.stderr)
    return 2


# ------------------------------------------------------------------------- #
# 基线哈希跳过缓存：整轮语义校验的裁决只取决于「完整输入集」。若这组输入自上次
# PASS 起逐字节未变，则复用上次的 PASS 裁决、跳过 owlrl 全图闭包 + pyshacl + 规则
# （随库增长超线性的那部分）。sound 前提=键覆盖一切能改变裁决的输入；缺一即可能
# 「该跑不跑」，故此处宁全勿缺，并对任何异常 fail-open（当作 miss、跑全量）。
#
# 一致性是全图性质、对新增非单调（一条新公理能让旧三元组参与矛盾），故【不能】按
# 三元组打永久免检标记——只能按整输入集指纹缓存。v1 只对 PASS 跳过：FAIL 永远重跑，
# 保证失败始终可见、可复现。kill-switch：SEMANTIC_GATE_CACHE=0 完全绕过（不读不写）。
_CACHE_KEY_SCHEMA = "semgate-v3"  # 改键算法/输入集时必须递增，令旧缓存整体失效
_CACHE_STATE_PATH = ROOT / "build" / "state" / "semantic-gate-cache.json"


def _cache_enabled() -> bool:
    return os.getenv("SEMANTIC_GATE_CACHE", "1").casefold() not in {"0", "false", "no", "off"}


def _reasoner() -> str:
    """选哪套推理器物化 OWL RL entailment：native（默认，scripts/native_reasoner.py 的最小
    semi-naive 物化器，约 190× 快，仅覆盖本体现用 profile，靠依赖护栏保 sound；2026-09-06
    Cert A 真实基线对拍通过后切为默认）或 owlrl（纯 Python 全量闭包，kill-switch）。非法值
    一律当 native。kill-switch=SEMANTIC_REASONER=owlrl 秒回退（护栏遇越界构造也自动回退 owlrl）。"""
    return "owlrl" if os.getenv("SEMANTIC_REASONER", "native").casefold() == "owlrl" else "native"


def _cache_input_files() -> list[Path]:
    """收集一切能改变语义校验裁决的输入文件。

    直接读入（semantic_validate 亲自解析）：ontology/modules、ontology/shapes、
    knowledge/semantic ABox、ontology/rules/registry.json + 递归 *.rq、内部来源
    manifest 及其 canonical_file 字节。
    间接经 build/semantic/current.trig：migrate_semantic 的全部 YAML/JSON 源——
    【不哈希 trig 字节本身】，它有约 4,532 个每次 migrate 随机的空白节点、非字节可
    复现；但 trig 的【逻辑内容】完全由这些源 + 迁移代码（migrate_semantic.py /
    common.py）决定，故哈希后者即等价覆盖 trig。SCHEMA_TARGET=ontology/modules/
    generated.ttl、DATA_TARGET=knowledge/semantic/current.ttl 已分别落在上面两个
    glob 内，--semantic-only 写入它们即改键、必然 miss。
    """
    files: list[Path] = []

    def add(paths) -> None:
        files.extend(p for p in paths if p.is_file())

    add((ROOT / "ontology" / "modules").glob("*.ttl"))
    add((ROOT / "ontology" / "shapes").glob("*.ttl"))
    add((ROOT / "knowledge" / "semantic").glob("*.ttl"))
    add([ROOT / "ontology" / "rules" / "registry.json"])
    add((ROOT / "ontology" / "rules").rglob("*.rq"))  # 递归，含 generated/
    for manifest in (ROOT / "sources" / "internal").glob("*/manifest.json"):
        files.append(manifest)
        try:
            doc = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue  # 键计算会读该 manifest 字节；内容坏则由上游 SEM-SOURCE-MANIFEST 拦
        canonical = doc.get("canonical_file")
        if canonical:
            files.append(ROOT / canonical)
    # 经 current.trig 的 migrate 源（见 migrate_semantic.py / common.py）
    for domain in ("core", "fab", "ap"):
        add((ROOT / "ontology" / domain / "entities").glob("*.yaml"))
        add((ROOT / "ontology" / domain / "relations").glob("*.yaml"))
    add([ROOT / "ontology" / "meta-schema.yaml"])
    for domain in ("fab", "ap"):
        add((ROOT / "kb" / domain).glob("*.yaml"))
    add([ROOT / "knowledge" / "scenarios" / "current.json",
         ROOT / "ontology" / "application-capabilities.json"])
    add((ROOT / "business" / "models").glob("*.yaml"))
    add((ROOT / "business" / "datasets").glob("*.yaml"))
    add((ROOT / "simulation" / "scenarios").glob("*.yaml"))
    # 行为代码：改了校验/迁移/加载/推理逻辑必须使缓存失效
    add([ROOT / "scripts" / "semantic_validate.py",
         ROOT / "scripts" / "migrate_semantic.py",
         ROOT / "scripts" / "native_reasoner.py",
         ROOT / "scripts" / "common.py"])
    return files


def compute_cache_key(no_inference: bool) -> str | None:
    """整输入集 + 库版本 + 标志的 SHA-256；任何异常返回 None → 调用方按 miss 处理。"""
    try:
        h = hashlib.sha256()
        h.update(_CACHE_KEY_SCHEMA.encode("utf-8"))
        h.update(b"\x00no_inference=" + (b"1" if no_inference else b"0"))
        h.update(b"\x00reasoner=" + _reasoner().encode("utf-8"))  # native/owlrl 产量与报告不同 → 键必分
        for lib in ("rdflib", "owlrl", "pyshacl"):
            try:
                ver = metadata.version(lib)
            except metadata.PackageNotFoundError:
                ver = "?"
            h.update(f"\x00lib:{lib}={ver}".encode("utf-8"))
        for path in sorted(set(_cache_input_files()), key=lambda p: p.as_posix()):
            h.update(f"\x00f:{path.relative_to(ROOT).as_posix()}=".encode("utf-8"))
            h.update(path.read_bytes())
        return h.hexdigest()
    except Exception:  # noqa: BLE001  fail-open：算键失败一律当 miss、跑全量
        return None


def _read_cache() -> dict:
    try:
        return json.loads(_CACHE_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_cache(key: str, status: str, report: dict | None) -> None:
    try:
        _CACHE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        entry = {"key": key, "status": status, "generated_at": datetime.now(timezone.utc).isoformat()}
        if status == "pass" and report is not None:
            entry["report"] = report  # 命中时按原样回放，保持报告字段形状
        _CACHE_STATE_PATH.write_text(json.dumps(entry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass  # 写缓存失败不影响本轮裁决


def _emit_cache_hit(cache_key: str, stored_report: dict | None) -> int:
    """命中：写一份 status=pass 的报告（回放上次内容或最小合法报告），return 0。"""
    report = dict(stored_report) if isinstance(stored_report, dict) else {"status": "pass", "issues": []}
    report["status"] = "pass"
    report["cache_hit"] = True
    report["cache_key"] = cache_key
    report["generated_at"] = datetime.now(timezone.utc).isoformat()
    out = ROOT / "build" / "reports" / "semantic-validation.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"语义校验命中基线缓存（输入集未变）：跳过 owlrl 闭包 / pyshacl / 规则；key={cache_key[:12]}…")
    return 0


def main() -> int:
    setup_console()
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-inference", action="store_true")
    args = ap.parse_args()
    try:
        from rdflib import Dataset, Graph, URIRef
        from owlrl import DeductiveClosure, OWLRL_Semantics
        from pyshacl import validate as shacl_validate
    except ModuleNotFoundError as exc:
        return dependency_error(exc.name or "semantic-runtime")

    # 基线哈希跳过缓存（详见 compute_cache_key 上方注释）：输入集自上次 PASS 起未变则
    # 复用裁决、跳过超线性推理。仅 PASS 跳过；FAIL 永远重跑。fail-open + kill-switch。
    cache_key = compute_cache_key(args.no_inference) if _cache_enabled() else None
    if cache_key is not None:
        stored = _read_cache()
        if stored.get("key") == cache_key and stored.get("status") == "pass":
            return _emit_cache_hit(cache_key, stored.get("report"))

    issues: list[dict] = []
    schema = Graph()
    module_files = sorted((ROOT / "ontology" / "modules").glob("*.ttl"))
    for path in module_files:
        try:
            schema.parse(path, format="turtle")
        except Exception as exc:  # noqa: BLE001
            issues.append({"severity": "error", "code": "SEM-TTL", "file": path.relative_to(ROOT).as_posix(), "message": str(exc)})

    shapes = Graph()
    for path in sorted((ROOT / "ontology" / "shapes").glob("*.ttl")):
        try:
            shapes.parse(path, format="turtle")
        except Exception as exc:  # noqa: BLE001
            issues.append({"severity": "error", "code": "SEM-SHAPE", "file": path.relative_to(ROOT).as_posix(), "message": str(exc)})

    dataset_path = ROOT / "build" / "semantic" / "current.trig"
    dataset = Dataset()
    if dataset_path.is_file():
        try:
            dataset.parse(dataset_path, format="trig")
        except Exception as exc:  # noqa: BLE001
            issues.append({"severity": "error", "code": "SEM-DATA", "file": dataset_path.relative_to(ROOT).as_posix(), "message": str(exc)})
    else:
        issues.append({"severity": "error", "code": "SEM-DATA", "message": "缺少 build/semantic/current.trig；先运行 migrate_semantic.py"})

    registry_path = ROOT / "ontology" / "rules" / "registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    ids: set[str] = set()
    executable_rules: list[tuple[str, Path]] = []
    for rule in registry.get("rules") or []:
        rid = rule.get("rule_id")
        if not rid or rid in ids:
            issues.append({"severity": "error", "code": "SEM-RULE-ID", "message": f"规则 ID 缺失或重复：{rid}"})
        ids.add(rid)
        impl = rule.get("implementation")
        if isinstance(impl, str) and impl.endswith(".rq"):
            path = ROOT / impl
            if not path.is_file():
                issues.append({"severity": "error", "code": "SEM-RULE-PATH", "message": f"规则文件不存在：{impl}"})
            else:
                try:
                    Graph().query(path.read_text(encoding="utf-8"))
                    executable_rules.append((rid, path))
                except Exception as exc:  # noqa: BLE001
                    issues.append({"severity": "error", "code": "SEM-RULE-PARSE", "file": impl, "message": str(exc)})
        for test_ref in rule.get("tests") or []:
            test_path = ROOT / str(test_ref).split("::", 1)[0]
            if not test_path.is_file():
                issues.append({"severity": "error", "code": "SEM-RULE-TEST", "message": f"规则测试不存在：{test_ref}"})

    for manifest_path in sorted((ROOT / "sources" / "internal").glob("*/manifest.json")):
        if manifest_path.parent.name == "vfab":
            continue  # vFab 为多数据集 manifest，由 vfab_ingest.py 按交付契约校验。
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        canonical_file = manifest.get("canonical_file")
        expected_hash = manifest.get("sha256") or manifest.get("source_sha256")
        if not canonical_file or not expected_hash:
            issues.append({"severity": "error", "code": "SEM-SOURCE-MANIFEST",
                           "file": manifest_path.relative_to(ROOT).as_posix(),
                           "message": "内部来源 manifest 缺少 canonical_file 或 SHA-256"})
            continue
        source_path = ROOT / canonical_file
        actual_hash = hashlib.sha256(source_path.read_bytes()).hexdigest() if source_path.is_file() else None
        if actual_hash != expected_hash:
            issues.append({"severity": "error", "code": "SEM-SOURCE-HASH",
                           "file": manifest_path.relative_to(ROOT).as_posix(),
                           "message": f"内部来源缺失或哈希不匹配：{canonical_file}"})

    data = Graph()
    for quad in dataset.quads((None, None, None, None)):
        data.add(quad[:3])
    abox_triples = 0
    for path in sorted((ROOT / "knowledge" / "semantic").glob("*.ttl")):
        try:
            before = len(data)
            data.parse(path, format="turtle")
            abox_triples += len(data) - before
        except Exception as exc:  # noqa: BLE001
            issues.append({"severity": "error", "code": "SEM-ABOX", "file": path.relative_to(ROOT).as_posix(), "message": str(exc)})
    data += schema
    # 统一治理：所有带 sh:targetClass 的 NodeShape 一律改为"只约束建模者显式直接
    # 声明该类型的节点"。必须在 owlrl 展开【之前】采集——展开后 domain/range/子类
    # 推理出的 rdf:type 会与显式断言混进同一张图无法区分，targetClass 会把这些推理
    # 派生类型一并纳入，导致合法的局部建模被误判（如被强行要求补齐诊断三件套）。
    # 做法：把显式命中的实例作为 sh:targetNode 注入对应形状，并移除 sh:targetClass，
    # 令 pyshacl 不再对推理派生类型重新套用该形状。这里"显式"仅指直接 `a TargetClass`，
    # 不含子类——子类→父类本身就是推理派生，正是要排除的路径。
    from rdflib import RDF as _RDF
    _SH_TARGET_CLASS = URIRef("http://www.w3.org/ns/shacl#targetClass")
    _SH_TARGET_NODE = URIRef("http://www.w3.org/ns/shacl#targetNode")
    # native 推理的依赖护栏要看形状的原始结构（sh:not/sh:sparql 等）——在 targetClass→
    # targetNode 改写【之前】留一份副本，避免与改写耦合（改写不动这些约束，快照仅为清晰）。
    shapes_pre = Graph()
    for _t in shapes:
        shapes_pre.add(_t)
    explicit_targets = 0
    for _shape, _, _cls in list(shapes.triples((None, _SH_TARGET_CLASS, None))):
        for _node in set(data.subjects(_RDF.type, _cls)):
            shapes.add((_shape, _SH_TARGET_NODE, _node))
            explicit_targets += 1
        shapes.remove((_shape, _SH_TARGET_CLASS, _cls))
    reasoner_used = "owlrl"
    inferred = 0
    if not args.no_inference and not issues:
        before = len(data)
        if _reasoner() == "native":
            import native_reasoner
            rule_texts = [p.read_text(encoding="utf-8") for _rid, p in executable_rules]
            blockers = native_reasoner.equivalence_precondition(shapes_pre, rule_texts, schema)
            if blockers:
                # 有依赖越出 native profile：回退 owlrl，门禁绝不弱化（最坏＝与今天一样慢）。
                print("native 推理等价前置不满足，回退 owlrl：" + "；".join(blockers), file=sys.stderr)
                DeductiveClosure(OWLRL_Semantics).expand(data)
            else:
                native_reasoner.materialize(data)
                reasoner_used = "native"
        else:
            DeductiveClosure(OWLRL_Semantics).expand(data)
        inferred = len(data) - before
    rule_triples = 0
    rule_counts: dict[str, int] = {}
    if not issues:
        for rid, path in executable_rules:
            try:
                result = data.query(path.read_text(encoding="utf-8"))
                constructed = getattr(result, "graph", None)
                count = 0
                if constructed is not None:
                    for triple in constructed:
                        if triple not in data:
                            data.add(triple); count += 1
                rule_counts[rid] = count
                rule_triples += count
            except Exception as exc:  # noqa: BLE001
                issues.append({"severity": "error", "code": "SEM-RULE-RUN", "file": path.relative_to(ROOT).as_posix(), "message": str(exc)})
    if not issues:
        try:
            conforms, _, results_text = shacl_validate(data_graph=data, shacl_graph=shapes, ont_graph=schema, inference="none", advanced=True)
            if not conforms:
                issues.append({"severity": "error", "code": "SEM-SHACL", "message": results_text})
        except Exception as exc:  # noqa: BLE001
            issues.append({"severity": "error", "code": "SEM-SHACL-RUN", "message": str(exc)})

    # 任何自动根因输出都必须仍是 Hypothesis，不能成为确认根因类型。
    forbidden = URIRef("urn:pxai:semi:ConfirmedRootCause")
    from rdflib import RDF
    if any(True for _ in data.triples((None, RDF.type, forbidden))):
        issues.append({"severity": "error", "code": "SEM-SAFETY", "message": "检测到自动生成的 ConfirmedRootCause"})

    report = {
        "status": "pass" if not issues else "fail",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "modules": len(module_files),
        "schema_triples": len(schema),
        "data_quads": len(dataset),
        "abox_triples": abox_triples,
        "explicit_shape_targets": explicit_targets,
        "reasoner": reasoner_used,
        "inferred_triples": inferred,
        "rule_generated_triples": rule_triples,
        "rule_counts": rule_counts,
        "rules": len(ids),
        "issues": issues,
    }
    out = ROOT / "build" / "reports" / "semantic-validation.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # 回写缓存：PASS 存 {key,status:pass,report} 供下次跳过；FAIL 也存（status:fail），
    # 使下次同键仍重跑（只对 pass 跳过）。cache_key 为 None（禁用/算键失败）时不写。
    if cache_key is not None:
        _write_cache(cache_key, "fail" if issues else "pass", None if issues else report)
    if issues:
        for issue in issues:
            print(f"ERROR {issue['code']}: {issue['message']}", file=sys.stderr)
        return 1
    print(f"语义校验通过：模块 {len(module_files)} | 规则 {len(ids)} | 推理新增 {inferred}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
