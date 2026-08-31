"""增量执行入口：只重建受输入变化影响的产物，并始终执行最终守门。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = ROOT / "build" / "state" / "loop-state.json"


def setup_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def fingerprint(patterns: list[str]) -> str:
    digest = hashlib.sha256()
    files: set[Path] = set()
    for pattern in patterns:
        files.update(path for path in ROOT.glob(pattern) if path.is_file())
    for path in sorted(files):
        digest.update(path.relative_to(ROOT).as_posix().encode("utf-8"))
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def run(script: str, *args: str) -> tuple[int, float, str]:
    start = time.monotonic()
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / script), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    return result.returncode, time.monotonic() - start, (result.stdout or "") + (result.stderr or "")


def main() -> int:
    setup_console()
    ap = argparse.ArgumentParser(description="企业本体与知识库增量循环")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--check-only", action="store_true")
    args = ap.parse_args()
    state = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.is_file() else {"stages": {}}
    next_state = {"generated_at": datetime.now(timezone.utc).isoformat(), "stages": dict(state.get("stages") or {})}

    stages = [
        ("source", "source_ingest.py", [], ["sources/internal/feature-model/**/*", "scripts/source_ingest.py"]),
        ("vfab-source", "vfab_ingest.py", [], ["sources/internal/vfab/**/*", "scripts/vfab_ingest.py"]),
        ("alignment", "align_sources.py", ["--check"], ["build/source/feature-model-catalog.json", "mappings/**/*.json", "ontology/modules/*.ttl", "sources/internal/vfab/**/*.json"]),
        ("capability", "capability_validate.py", [], ["ontology/capability-questions.json", "ontology/application-capabilities.json", "ontology/modules/*.ttl"]),
        ("legacy-validation", "validate.py", ["--quiet"], ["config.yaml", "ontology/**/*.yaml", "kb/**/*.yaml", "scripts/common.py", "scripts/validate.py"]),
        ("index", "build_index.py", [], ["ontology/**/*.yaml", "kb/**/*.yaml", "scripts/build_index.py"]),
        ("scenario-mining", "scenario_mine.py", [], ["build/index.json", "build/graph.json", "build/reports/source-alignment.json", "business/models/*.yaml", "simulation/scenarios/*.yaml", "scripts/scenario_mine.py"]),
        ("semantic-migration", "migrate_semantic.py", [], ["ontology/**/*.yaml", "kb/**/*.yaml", "knowledge/scenarios/current.json", "knowledge/semantic/*.ttl", "business/models/*.yaml", "business/datasets/*.yaml", "simulation/scenarios/*.yaml", "scripts/common.py", "scripts/migrate_semantic.py"]),
        ("semantic-validation", "semantic_validate.py", [], ["ontology/modules/*.ttl", "ontology/shapes/*.ttl", "ontology/rules/*", "build/semantic/current.trig", "sources/internal/feature-model/**/*"]),
        ("semantic-tests", "semantic_test.py", [], ["ontology/modules/*.ttl", "ontology/rules/*", "tests/semantic/*.py", "scripts/semantic_test.py"]),
        ("business-simulation", "simulate_check.py", [], ["business/**/*.yaml", "simulation/**/*.yaml", "scripts/simulate*.py"]),
        ("golden-query-tests", "regress.py", [], ["tests/questions-*.txt", "ontology/competency-questions.yaml", "ontology/**/*.yaml", "scripts/ask.py", "scripts/regress.py"]),
    ]
    always = {"legacy-validation", "semantic-validation", "semantic-tests", "business-simulation", "golden-query-tests"}
    total = 0.0
    for name, script, script_args, inputs in stages:
        current = fingerprint(inputs)
        previous = (state.get("stages") or {}).get(name, {})
        skip = not args.force and not args.check_only and name not in always and previous.get("fingerprint") == current and previous.get("status") == "pass"
        if args.check_only and name in {"source", "semantic-migration", "index"}:
            skip = True
        if skip:
            print(f"[SKIP] {name:<24} 输入未变化")
            continue
        code, duration, output = run(script, *script_args)
        total += duration
        status = "pass" if code == 0 else "fail"
        print(f"[{'OK' if code == 0 else 'FAIL':<4}] {name:<24} {duration:>6.2f}s")
        for line in [x for x in output.splitlines() if x.strip()][-4:]:
            print("       " + line)
        next_state["stages"][name] = {"fingerprint": current, "status": status, "duration_seconds": round(duration, 3)}
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(next_state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if code != 0:
            print(f"循环中止于 {name}，总耗时 {total:.2f}s")
            return 1
    print(f"企业本体循环通过，总耗时 {total:.2f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
