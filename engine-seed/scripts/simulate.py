"""通用制造业/半导体经营模型执行器。YAML 声明模型，Python 执行算法。"""
from __future__ import annotations

import argparse
import ast
import math
import sys
from pathlib import Path
from typing import Any

import yaml

from simulation_algorithms import ALGORITHMS, execute

ROOT = Path(__file__).resolve().parent.parent
SOURCES = {"observed", "internal_feature", "vfab", "assumption", "model_prior", "web", "human"}


def setup_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


class ModelError(ValueError):
    pass


def compatible_unit(declared: str, actual: str) -> bool:
    """模板中的 unit 是可被领域单位细化的占位符。"""
    if declared == actual:
        return True
    if declared == "unit/month":
        return actual.endswith("/month") and not actual.startswith("CNY/")
    if declared == "CNY/unit":
        return actual.startswith("CNY/") and actual != "CNY/month"
    return False


def load_yaml(path: Path) -> dict:
    with Path(path).open("r", encoding="utf-8") as fh:
        value = yaml.safe_load(fh) or {}
    if not isinstance(value, dict):
        raise ModelError(f"YAML 顶层必须是对象：{path}")
    return value


def ontology_ids(root: Path) -> set[str]:
    ids: set[str] = set()
    for path in (root / "ontology").glob("*/entities/*.yaml"):
        for item in load_yaml(path).get("entities") or []:
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                ids.add(item["id"])
    return ids


def safe_eval(formula: str, values: dict[str, float]) -> float:
    try:
        tree = ast.parse(formula, mode="eval")
    except SyntaxError as exc:
        raise ModelError(f"公式语法错误：{formula}") from exc

    def ev(node: ast.AST) -> float:
        if isinstance(node, ast.Expression): return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)): return float(node.value)
        if isinstance(node, ast.Name):
            if node.id not in values: raise ModelError(f"公式引用未定义变量：{node.id}")
            return values[node.id]
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = ev(node.operand)
            return value if isinstance(node.op, ast.UAdd) else -value
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow)):
            left, right = ev(node.left), ev(node.right)
            if isinstance(node.op, ast.Add): return left + right
            if isinstance(node.op, ast.Sub): return left - right
            if isinstance(node.op, ast.Mult): return left * right
            if isinstance(node.op, ast.Div): return left / right
            return left ** right
        raise ModelError(f"不允许的公式表达式：{formula}")

    value = ev(tree)
    if not math.isfinite(value): raise ModelError(f"公式结果不是有限数：{formula}")
    return float(value)


def _merge(base: list[dict], extra: list[dict]) -> list[dict]:
    result, positions = list(base), {x.get("id"): i for i, x in enumerate(base)}
    for raw in extra:
        item = dict(raw); key = item.get("id")
        if key in positions:
            if raw.get("override"): result[positions[key]] = item
        else:
            positions[key] = len(result); result.append(item)
    return result


def _resolve_template(ref: str, root: Path, stack: tuple[str, ...] = ()) -> dict:
    if ref in stack: raise ModelError(f"模板继承成环：{' -> '.join(stack + (ref,))}")
    template = load_yaml(root / ref).get("template") or {}
    merged = {"variables": [], "calculations": []}
    for parent in template.get("extends", []):
        inherited = _resolve_template(parent, root, stack + (ref,))
        merged["variables"] = _merge(merged["variables"], inherited["variables"])
        merged["calculations"] = _merge(merged["calculations"], inherited["calculations"])
    merged["variables"] = _merge(merged["variables"], template.get("variables", []))
    merged["calculations"] = _merge(merged["calculations"], template.get("calculations", []))
    return merged


def load_resolved_model(model_path: Path, root: Path = ROOT) -> dict:
    model = dict(load_yaml(model_path).get("model") or {})
    template = _resolve_template(model["template_ref"], root) if model.get("template_ref") else {"variables": [], "calculations": []}
    dataset = load_yaml(root / model["dataset_ref"]).get("dataset", {}) if model.get("dataset_ref") else {}
    supplied = {x["id"]: dict(x) for x in dataset.get("values", [])}
    supplied.update({x["id"]: dict(x) for x in model.get("inputs", [])})
    inputs = []
    calculated_ids = {x["id"] for x in template.get("calculations", []) + model.get("calculations", [])}
    for definition in template["variables"]:
        if definition["id"] in supplied:
            value = {**definition, **supplied.pop(definition["id"])}
            value["_declared_unit"] = definition.get("unit")
            inputs.append(value)
        elif definition["id"] not in calculated_ids and definition.get("required", True): raise ModelError(f"经营模型缺少输入：{definition['id']}")
    inputs.extend(supplied.values())
    model["inputs"] = inputs
    model["values"] = {x["id"]: x for x in inputs}
    model["calculations"] = _merge(template["calculations"], model.get("calculations", []))
    model["equations"] = model.get("equations", [])
    return model


def validate_model(model: dict, root: Path) -> None:
    missing = [k for k in ("id", "period", "currency", "inputs", "outputs") if k not in model]
    if missing: raise ModelError(f"经营模型缺少字段：{', '.join(missing)}")
    known, seen = ontology_ids(root), set()
    for item in model["inputs"]:
        for key in ("id", "value", "unit", "source"):
            if key not in item: raise ModelError(f"经营输入缺少字段 {key}：{item}")
        if item["id"] in seen: raise ModelError(f"经营变量 ID 重复：{item['id']}")
        seen.add(item["id"])
        if item["source"] not in SOURCES: raise ModelError(f"非法输入来源：{item['source']}")
        if item.get("_declared_unit") and not compatible_unit(item["_declared_unit"], item["unit"]):
            raise ModelError(f"经营输入单位与模板不一致：{item['id']} {item['unit']} != {item['_declared_unit']}")
        value = float(item["value"])
        if not math.isfinite(value): raise ModelError(f"经营输入不是有限数：{item['id']}")
        if item["unit"] == "ratio" and not 0 <= value <= 1:
            raise ModelError(f"比例输入必须在 0 到 1：{item['id']}")
        if item["unit"] != "ratio" and value < 0:
            raise ModelError(f"经营输入不得为负：{item['id']}")
        if item["source"] in {"observed", "internal_feature", "vfab", "web"} and not item.get("source_ref"):
            raise ModelError(f"可核查来源缺少 source_ref：{item['id']}")
        if item.get("ontology_ref") and item["ontology_ref"] not in known: raise ModelError(f"本体引用不存在：{item['ontology_ref']}")
    calculations = model.get("equations", []) + model.get("calculations", [])
    for calc in calculations:
        if not all(k in calc for k in ("id", "unit")): raise ModelError(f"经营计算缺少字段：{calc}")
        if calc["id"] in seen and not calc.get("override"): raise ModelError(f"经营变量 ID 重复：{calc['id']}")
        if calc.get("algorithm") and calc["algorithm"] not in ALGORITHMS: raise ModelError(f"未知算法：{calc['algorithm']}")
        if not calc.get("algorithm") and not calc.get("formula"): raise ModelError(f"计算缺少 algorithm/formula：{calc}")
        seen.add(calc["id"])
    all_ids = set(seen)
    for calc in calculations:
        missing_inputs = sorted(set(calc.get("inputs") or []) - all_ids)
        if missing_inputs:
            raise ModelError(f"计算 {calc['id']} 引用不存在变量：{', '.join(missing_inputs)}")
    for output in model["outputs"]:
        if output not in seen: raise ModelError(f"输出变量不存在：{output}")


def evaluate(model: dict, overrides: dict[str, float] | None = None) -> dict[str, dict[str, Any]]:
    overrides = overrides or {}; values = {}; units = {}
    for item in model["inputs"]:
        values[item["id"]] = float(overrides.get(item["id"], item["value"])); units[item["id"]] = item["unit"]
    pending = list(model.get("equations", []) + model.get("calculations", []))
    while pending:
        progressed = False
        for calc in list(pending):
            dependencies = calc.get("inputs", [])
            if calc.get("algorithm") and any(key not in values for key in dependencies):
                continue
            try:
                value = execute(calc["algorithm"], [values[x] for x in dependencies], calc.get("params")) if calc.get("algorithm") else safe_eval(calc["formula"], values)
            except ModelError as exc:
                if "公式引用未定义变量" in str(exc):
                    continue
                raise
            except KeyError as exc: raise ModelError(f"未知算法：{calc.get('algorithm')}") from exc
            except ValueError as exc: raise ModelError(str(exc)) from exc
            if not math.isfinite(value): raise ModelError(f"计算结果不是有限数：{calc['id']}")
            if calc["unit"] == "ratio" and not -1e-12 <= value <= 1 + 1e-12:
                raise ModelError(f"比例计算结果越界：{calc['id']}={value}")
            values[calc["id"]] = value; units[calc["id"]] = calc["unit"]
            pending.remove(calc); progressed = True
        if not progressed:
            unresolved = ", ".join(x["id"] for x in pending)
            raise ModelError(f"计算依赖无法解析或成环：{unresolved}")
    return {k: {"value": round(values[k], 6), "unit": units[k]} for k in model["outputs"]}


def intervention_values(model: dict, interventions: list[dict]) -> dict[str, float]:
    result = {x["id"]: float(x["value"]) for x in model["inputs"]}
    for item in interventions:
        key = item.get("variable")
        if key not in result: raise ModelError(f"干预变量不是经营模型输入：{key}")
        op, value = item.get("operation"), float(item.get("value"))
        if op == "set": result[key] = value
        elif op == "multiply": result[key] *= value
        elif op == "add": result[key] += value
        else: raise ModelError(f"未知干预操作：{op}")
        definition = next(x for x in model["inputs"] if x["id"] == key)
        if definition["unit"] == "ratio" and not 0 <= result[key] <= 1:
            raise ModelError(f"干预后比例越界：{key}={result[key]}")
        if definition["unit"] != "ratio" and result[key] < 0:
            raise ModelError(f"干预后输入为负：{key}={result[key]}")
    return result


def _assert_scenario(assertions: list[dict], baseline: dict, changed: dict) -> None:
    for assertion in assertions:
        kind, variable = assertion.get("type"), assertion.get("variable")
        if variable not in baseline:
            raise ModelError(f"场景断言引用非输出变量：{variable}")
        before, after = baseline[variable]["value"], changed[variable]["value"]
        if kind == "nondecreasing" and after < before:
            raise ModelError(f"场景断言失败：{variable} 应不下降")
        if kind == "nonincreasing" and after > before:
            raise ModelError(f"场景断言失败：{variable} 应不上升")
        if kind == "unchanged" and after != before:
            raise ModelError(f"场景断言失败：{variable} 应保持不变")
        if kind not in {"nondecreasing", "nonincreasing", "unchanged"}:
            raise ModelError(f"未知场景断言：{kind}")


def run_scenario(scenario_path: Path, root: Path = ROOT) -> dict:
    scenario = load_yaml(scenario_path).get("scenario") or {}
    if not scenario.get("id") or not scenario.get("model_ref"): raise ModelError("场景必须包含 id 与 model_ref")
    model = load_resolved_model(root / scenario["model_ref"], root); validate_model(model, root)
    known, chain, touched = ontology_ids(root), [], set()
    for item in scenario.get("interventions", []):
        if item.get("target_ref") and item["target_ref"] not in known: raise ModelError(f"本体引用不存在：{item['target_ref']}")
        if item.get("variable") in touched: raise ModelError(f"同一场景重复干预变量：{item.get('variable')}")
        touched.add(item.get("variable"))
        chain.append({k: item.get(k) for k in ("target_ref", "variable", "operation", "value")})
    baseline = evaluate(model); changed = evaluate(model, intervention_values(model, scenario.get("interventions", [])))
    if baseline != evaluate(model): raise ModelError("同一模型重复执行结果不一致")
    if baseline != evaluate(model, intervention_values(model, [])): raise ModelError("零干预未保持基线")
    _assert_scenario(scenario.get("assertions") or [], baseline, changed)
    delta = {k: {"value": round(changed[k]["value"] - baseline[k]["value"], 6), "unit": baseline[k]["unit"]} for k in baseline}
    gain = delta.get("profit", {}).get("value", 0); investment = float((scenario.get("investment") or {}).get("one_time", 0))
    sources = {x["source"] for x in model["inputs"]}
    observed_sources = {"observed", "internal_feature", "vfab"}
    evidence = "observed" if sources and sources <= observed_sources else ("mixed" if sources & observed_sources else "assumption_only")
    return {"run": {"scenario": scenario["id"], "model": model["id"], "domain": model.get("domain"), "period": model["period"], "validation": "pass"}, "baseline": baseline, "intervention": changed, "delta": delta, "payback_periods": round(investment / gain, 6) if investment > 0 and gain > 0 else None, "evidence_grade": evidence, "source_types": sorted(sources), "chain_integrity": chain, "warnings": [] if evidence == "observed" else ["包含假设或行业先验，结果属于情景推演，不是经营承诺"]}


def validate_project(root: Path = ROOT) -> list[str]:
    issues, models = [], set()
    for path in sorted((root / "business" / "models").glob("*.yaml")):
        try:
            model = load_resolved_model(path, root); validate_model(model, root)
            if evaluate(model) != evaluate(model): raise ModelError("确定性复算不一致")
            models.add(path.relative_to(root).as_posix())
        except (ModelError, OSError, yaml.YAMLError) as exc: issues.append(f"{path.relative_to(root)}: {exc}")
    for path in sorted((root / "simulation" / "scenarios").glob("*.yaml")):
        try:
            ref = (load_yaml(path).get("scenario") or {}).get("model_ref")
            if ref not in models: raise ModelError(f"model_ref 不存在：{ref}")
            run_scenario(path, root)
        except (ModelError, OSError, yaml.YAMLError) as exc: issues.append(f"{path.relative_to(root)}: {exc}")
    return issues


def main() -> int:
    setup_console()
    ap = argparse.ArgumentParser(description="运行通用经营模型干预场景"); ap.add_argument("scenario", type=Path, nargs="?"); ap.add_argument("--check", action="store_true"); ap.add_argument("--output", type=Path); args = ap.parse_args()
    if args.check:
        issues = validate_project(); [print(f"ERROR: {x}", file=sys.stderr) for x in issues]; print(f"经营模型/仿真场景校验：{'通过' if not issues else '失败'}"); return bool(issues)
    if not args.scenario: ap.error("需要 scenario，或使用 --check")
    try: result = run_scenario(args.scenario)
    except (ModelError, OSError, yaml.YAMLError) as exc: print(f"ERROR: {exc}", file=sys.stderr); return 1
    text = yaml.safe_dump(result, allow_unicode=True, sort_keys=False)
    if args.output: args.output.parent.mkdir(parents=True, exist_ok=True); args.output.write_text(text, encoding="utf-8"); print(args.output)
    else: print(text, end="")
    return 0


if __name__ == "__main__": sys.exit(main())
