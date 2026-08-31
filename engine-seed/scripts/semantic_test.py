"""执行 OWL 公理和 SPARQL 规则的行为测试。"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def setup_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def main() -> int:
    setup_console()
    try:
        import rdflib  # noqa: F401
        import owlrl  # noqa: F401
    except ModuleNotFoundError as exc:
        print(f"缺少语义测试依赖 {exc.name}；请安装 requirements.txt。", file=sys.stderr)
        return 2
    result = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests/semantic", "-p", "test_*.py"],
        cwd=ROOT,
    )
    if result.returncode == 0:
        print("语义推理行为测试通过。")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
