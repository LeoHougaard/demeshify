"""Freeze a seeded release corpus without selecting on conversion outcomes."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from pathlib import Path


def select_corpus(
    fusion_manifest: Path, controls: list[Path], count: int = 500, seed: int = 20260917,
) -> dict:
    latest = {}
    fusion = json.loads(fusion_manifest.read_text(encoding="utf-8"))
    for case in fusion["cases"]:
        component, revision = case["id"].rsplit("_", 1)
        if (
            component not in latest
            or int(revision) > int(latest[component]["id"].rsplit("_", 1)[1])
        ):
            latest[component] = case
    pool = sorted(latest.values(), key=lambda case: case["id"])
    random.Random(seed).shuffle(pool)
    cases = []
    seen = set()

    def add(case: dict, parent: Path) -> None:
        item = dict(case)
        stl = (parent / item["stl"]).resolve()
        digest = hashlib.sha256(stl.read_bytes()).hexdigest()
        if digest in seen:
            return
        source = (parent / item["source_step"]).resolve()
        item.update(
            stl=str(stl), source_step=str(source), input_sha256=digest,
            source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        )
        seen.add(digest)
        cases.append(item)

    for path in controls:
        for case in json.loads(path.read_text(encoding="utf-8"))["cases"]:
            add(case, path.parent)
    if len(cases) > count:
        raise ValueError("The requested count is smaller than the unique control set")
    for case in pool:
        if len(cases) >= count:
            break
        add(case, fusion_manifest.parent)
    if len(cases) != count:
        raise ValueError(f"Only {len(cases)} distinct inputs are available; requested {count}")
    random.Random(seed).shuffle(cases)
    return {
        "format_version": 1,
        "selection": {
            "seed": seed,
            "method": "All controls plus seeded random latest Fusion component revisions; "
                      "unique STL SHA-256; no outcome or complexity filtering",
        },
        "case_count": len(cases), "cases": cases,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fusion_manifest", type=Path)
    parser.add_argument("--control", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--count", type=int, default=500)
    parser.add_argument("--seed", type=int, default=20260917)
    args = parser.parse_args()
    manifest = select_corpus(args.fusion_manifest, args.control, args.count, args.seed)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"Selected {manifest['case_count']} distinct files")


if __name__ == "__main__":
    main()
