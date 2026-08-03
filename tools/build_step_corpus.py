from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import cadquery as cq
import trimesh


def step_files(roots: list[Path]) -> list[Path]:
    found: set[Path] = set()
    for root in roots:
        if root.is_file() and root.suffix.lower() in {".step", ".stp"}:
            found.add(root.resolve())
        elif root.is_dir():
            for pattern in ("*.step", "*.stp"):
                found.update(path.resolve() for path in root.rglob(pattern))
    return sorted(found)


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def convert_step(
    source: Path,
    output: Path,
    tolerance: float,
    angular_tolerance: float,
    source_license: str,
) -> dict[str, object]:
    digest = file_digest(source)
    identifier = f"{source.stem[:48]}-{digest[:12]}"
    stl_name = f"{identifier}.stl"
    stl_path = output / stl_name
    model = cq.importers.importStep(str(source))
    if not model.vals():
        raise ValueError("STEP file contains no importable shape")
    cq.exporters.export(
        model,
        str(stl_path),
        tolerance=tolerance,
        angularTolerance=angular_tolerance,
    )
    mesh = trimesh.load(stl_path, force="mesh", process=True)
    return {
        "id": identifier,
        "source_step": str(source),
        "source_sha256": digest,
        "source_license": source_license,
        "stl": stl_name,
        "tessellation_tolerance_mm": tolerance,
        "angular_tolerance_rad": angular_tolerance,
        "triangles": int(len(mesh.faces)),
        "dimensions_mm": [float(value) for value in mesh.extents],
        "watertight": bool(mesh.is_watertight),
        "volume_mm3": abs(float(mesh.volume)) if mesh.is_watertight else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert licensed STEP collections into an STL reconstruction corpus."
    )
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, default=Path("datasets/corpus"))
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--tolerance", type=float, default=0.03)
    parser.add_argument("--angular-tolerance", type=float, default=0.08)
    parser.add_argument(
        "--source-license",
        default="unknown",
        help="License/provenance label recorded with every generated case.",
    )
    arguments = parser.parse_args()

    sources = step_files(arguments.inputs)
    if arguments.limit > 0:
        sources = sources[: arguments.limit]
    arguments.output.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, object]] = []
    failures: list[dict[str, str]] = []
    for index, source in enumerate(sources, start=1):
        try:
            records.append(
                convert_step(
                    source,
                    arguments.output,
                    arguments.tolerance,
                    arguments.angular_tolerance,
                    arguments.source_license,
                )
            )
            print(f"[{index}/{len(sources)}] converted {source.name}")
        except Exception as exc:
            failures.append({"source_step": str(source), "error": str(exc)})
            print(f"[{index}/{len(sources)}] failed {source.name}: {exc}")

    manifest = {
        "format_version": 1,
        "case_count": len(records),
        "failure_count": len(failures),
        "cases": records,
        "failures": failures,
    }
    manifest_path = arguments.output / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"Wrote {manifest_path} with {len(records)} cases")


if __name__ == "__main__":
    main()
