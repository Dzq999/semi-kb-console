"""经营模型与仿真场景的全库校验，供 kb.py check 调用。"""
from __future__ import annotations

import sys

from simulate import validate_project


def main() -> int:
    issues = validate_project()
    for issue in issues:
        print(f"ERROR: {issue}")
    if issues:
        print(f"经营模型/仿真场景校验失败：ERROR {len(issues)}")
        return 1
    print("经营模型/仿真场景校验通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
