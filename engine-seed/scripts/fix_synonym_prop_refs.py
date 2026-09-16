#!/usr/bin/env python
"""B1a forward-fix: redirect every live reference to a deleted synonym property
onto its canonical survivor.

merge_synonyms.py rewrote only the 3 RDF graphs (generated.ttl / current.ttl /
provenance.ttl). Plain-JSON sources (feature->ontology mappings, knowledge-entry
related_iris) and the .rq rule bodies still point at the 336 deleted localnames,
which (a) breaks align_sources + vfab_cross_validate gates and (b) would be
re-materialised on the next run, silently undoing the merge. This redirects them.

Match discipline (precise, prose-safe):
  * JSON  : exact-match STANDALONE string values only (related_iris entries,
            target_property). Prose content/summary embed a bare localname
            mid-sentence -> never an exact string value -> untouched.
  * .rq   : prefixed `semi:LOCAL` (word-boundary) and `<urn:pxai:semi:LOCAL>`.
Default is dry-run; pass --apply to write. Backups -> build/reports/_b1a_reffix_backup_<stamp>/.
"""
from __future__ import annotations
import argparse, json, re, shutil, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PFX = "urn:pxai:semi:"
MAP = ROOT / "build" / "reports" / "_synonym_merge_map_props.json"


def build_redirect(map_path: Path = MAP) -> dict[str, str]:
    mm = json.loads(map_path.read_text(encoding="utf-8"))
    redir: dict[str, str] = {}
    for c in mm:
        if c.get("kind") and c["kind"] != "property":
            continue
        canon = c["canonical"]
        for m in c["merge"]:
            if m != canon:
                redir[m] = canon
    return redir


def redirect_json_value(obj, full_map: dict[str, str], stats: dict):
    """Recursively redirect standalone full-IRI string values. Dedupe lists."""
    if isinstance(obj, dict):
        return {k: redirect_json_value(v, full_map, stats) for k, v in obj.items()}
    if isinstance(obj, list):
        out, seen = [], set()
        for v in obj:
            nv = redirect_json_value(v, full_map, stats)
            key = json.dumps(nv, sort_keys=True, ensure_ascii=False) if not isinstance(nv, str) else nv
            if key in seen:
                stats["deduped"] += 1
                continue
            seen.add(key)
            out.append(nv)
        return out
    if isinstance(obj, str) and obj in full_map:
        stats["json_hits"] += 1
        return full_map[obj]
    return obj


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--map", default=str(MAP),
                    help="合并映射 JSON（默认 _synonym_merge_map_props.json；C 轨传自己的 map）")
    args = ap.parse_args()

    map_path = Path(args.map)
    if not map_path.is_file():
        print(f"找不到合并映射：{map_path}", file=sys.stderr)
        return 2
    redir = build_redirect(map_path)  # localname -> canonical localname
    full_map = {PFX + o: PFX + c for o, c in redir.items()}  # full IRI -> full IRI
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    backup = ROOT / "build" / "reports" / f"_b1a_reffix_backup_{stamp}"

    json_targets = sorted((ROOT / "mappings").rglob("*.json")) + \
                   sorted((ROOT / "knowledge" / "entries").glob("*.json"))
    rq_targets = sorted((ROOT / "ontology" / "rules").rglob("*.rq"))

    # precompiled .rq patterns: semi:LOCAL (boundary) and <urn:...:LOCAL>
    rq_pats = []
    for o, c in redir.items():
        rq_pats.append((re.compile(r'(?<![A-Za-z0-9_])semi:' + re.escape(o) + r'(?![A-Za-z0-9_])'), 'semi:' + c))
        rq_pats.append((re.compile(r'<' + re.escape(PFX + o) + r'>'), '<' + PFX + c + '>'))

    report = {"json": {}, "rq": {}, "totals": {"json_files": 0, "json_hits": 0, "deduped": 0, "rq_files": 0, "rq_hits": 0}}
    writes: list[tuple[Path, str]] = []

    for p in json_targets:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        stats = {"json_hits": 0, "deduped": 0}
        new = redirect_json_value(data, full_map, stats)
        if stats["json_hits"] or stats["deduped"]:
            txt = json.dumps(new, ensure_ascii=False, indent=2) + "\n"
            json.loads(txt)  # self-check
            rel = p.relative_to(ROOT).as_posix()
            report["json"][rel] = stats
            report["totals"]["json_files"] += 1
            report["totals"]["json_hits"] += stats["json_hits"]
            report["totals"]["deduped"] += stats["deduped"]
            writes.append((p, txt))

    for p in rq_targets:
        txt = p.read_text(encoding="utf-8")
        n = 0
        new = txt
        for pat, repl in rq_pats:
            new, k = pat.subn(repl, new)
            n += k
        if n:
            rel = p.relative_to(ROOT).as_posix()
            report["rq"][rel] = n
            report["totals"]["rq_files"] += 1
            report["totals"]["rq_hits"] += n
            writes.append((p, new))

    print(json.dumps(report["totals"], indent=2))
    print(f"planned writes: {len(writes)} files")
    if not args.apply:
        print("DRY-RUN (pass --apply to write). Top json files:")
        for k, v in list(report["json"].items())[:6]:
            print("  ", k, v)
        print("Top rq files:")
        for k, v in list(report["rq"].items())[:6]:
            print("  ", k, v)
        return 0

    backup.mkdir(parents=True, exist_ok=True)
    for p, txt in writes:
        rel = p.relative_to(ROOT).as_posix().replace("/", "__")
        shutil.copy2(p, backup / rel)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(txt, encoding="utf-8")
        tmp.replace(p)
    (ROOT / "build" / "reports" / f"_b1a_reffix_{stamp}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"APPLIED. backups -> {backup.relative_to(ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
