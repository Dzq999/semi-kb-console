"""通用经营仿真算法注册表。

YAML 只声明算法名与输入；计算逻辑集中在这里，避免把复杂业务逻辑塞进公式字符串。
"""
from __future__ import annotations

import math
from typing import Callable, Sequence


Algorithm = Callable[[Sequence[float], dict], float]
ALGORITHMS: dict[str, Algorithm] = {}


def algorithm(name: str) -> Callable[[Algorithm], Algorithm]:
    def register(func: Algorithm) -> Algorithm:
        ALGORITHMS[name] = func
        return func
    return register


def _require(values: Sequence[float], minimum: int, name: str) -> None:
    if len(values) < minimum:
        raise ValueError(f"算法 {name} 至少需要 {minimum} 个输入")
    if not all(math.isfinite(float(value)) for value in values):
        raise ValueError(f"算法 {name} 输入必须是有限数")


@algorithm("yield.cascade")
def yield_cascade(values: Sequence[float], params: dict) -> float:
    _require(values, 1, "yield.cascade")
    result = 1.0
    for value in values:
        if not 0 <= value <= 1:
            raise ValueError("yield.cascade 的良率必须在 0 到 1 之间")
        result *= value
    return result


@algorithm("capacity.bottleneck")
def capacity_bottleneck(values: Sequence[float], params: dict) -> float:
    _require(values, 1, "capacity.bottleneck")
    return min(values)


@algorithm("production.saleable_output")
def saleable_output(values: Sequence[float], params: dict) -> float:
    _require(values, 2, "production.saleable_output")
    input_units, process_yield = values[0], values[1]
    return input_units * process_yield


@algorithm("production.oee_capacity")
def oee_capacity(values: Sequence[float], params: dict) -> float:
    _require(values, 4, "production.oee_capacity")
    theoretical_rate, available_time, performance, quality = values[:4]
    return theoretical_rate * available_time * performance * quality


@algorithm("finance.revenue")
def revenue(values: Sequence[float], params: dict) -> float:
    _require(values, 2, "finance.revenue")
    return values[0] * values[1]


@algorithm("cost.variable_total")
def variable_total(values: Sequence[float], params: dict) -> float:
    _require(values, 2, "cost.variable_total")
    return values[0] * values[1]


@algorithm("cost.unit")
def unit_cost(values: Sequence[float], params: dict) -> float:
    _require(values, 2, "cost.unit")
    total_cost, output = values[:2]
    return total_cost / output if output else float("inf")


@algorithm("finance.roi")
def roi(values: Sequence[float], params: dict) -> float:
    _require(values, 2, "finance.roi")
    gain, investment = values[:2]
    return gain / investment if investment else float("inf")


@algorithm("finance.payback")
def payback(values: Sequence[float], params: dict) -> float:
    _require(values, 2, "finance.payback")
    investment, periodic_gain = values[:2]
    return investment / periodic_gain if periodic_gain > 0 else float("inf")


def execute(name: str, values: Sequence[float], params: dict | None = None) -> float:
    if name not in ALGORITHMS:
        raise KeyError(name)
    return float(ALGORITHMS[name](values, params or {}))
