from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import cadquery as cq
import trimesh


@dataclass(frozen=True)
class ShapeSignature:
    volume: float
    area: float


@dataclass(frozen=True)
class CandidateMatch:
    stl_path: Path
    solid_index: int
    volume_error: float
    area_error: float

    @property
    def score(self) -> float:
        return self.volume_error * 4.0 + self.area_error * 2.0


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def relative_error(first: float, second: float) -> float:
    return abs(first - second) / max(abs(first), abs(second), 1e-9)


def solid_signature(solid: cq.Shape) -> ShapeSignature:
    return ShapeSignature(abs(float(solid.Volume())), abs(float(solid.Area())))


def mesh_signature(mesh: trimesh.Trimesh) -> ShapeSignature:
    return ShapeSignature(abs(float(mesh.volume)), abs(float(mesh.area)))


def match_printable_solids(
    solids: list[cq.Shape],
    stl_paths: list[Path],
    maximum_relative_error: float,
) -> tuple[list[CandidateMatch], list[dict[str, object]]]:
    """Match watertight printable STLs to assembly solids without reusing bodies."""

    solid_signatures = [solid_signature(solid) for solid in solids]
    candidates: list[CandidateMatch] = []
    rejected: list[dict[str, object]] = []
    for stl_path in stl_paths:
        mesh = trimesh.load(stl_path, force="mesh", process=True)
        if not isinstance(mesh, trimesh.Trimesh) or not mesh.is_watertight:
            rejected.append(
                {
                    "source_stl": str(stl_path.resolve()),
                    "reason": "source STL is not a closed two-manifold mesh",
                }
            )
            continue
        signature = mesh_signature(mesh)
        local_candidates = 0
        for solid_index, solid_value in enumerate(solid_signatures):
            volume_error = relative_error(signature.volume, solid_value.volume)
            area_error = relative_error(signature.area, solid_value.area)
            if (
                volume_error <= maximum_relative_error
                and area_error <= maximum_relative_error
            ):
                candidates.append(
                    CandidateMatch(
                        stl_path=stl_path,
                        solid_index=solid_index,
                        volume_error=volume_error,
                        area_error=area_error,
                    )
                )
                local_candidates += 1
        if local_candidates == 0:
            rejected.append(
                {
                    "source_stl": str(stl_path.resolve()),
                    "reason": "no assembly solid matched volume and area thresholds",
                }
            )

    selected: list[CandidateMatch] = []
    used_stls: set[Path] = set()
    used_solids: set[int] = set()
    for candidate in sorted(
        candidates,
        key=lambda item: (item.score, str(item.stl_path), item.solid_index),
    ):
        if candidate.stl_path in used_stls or candidate.solid_index in used_solids:
            continue
        selected.append(candidate)
        used_stls.add(candidate.stl_path)
        used_solids.add(candidate.solid_index)

    matched_candidate_paths = {candidate.stl_path for candidate in candidates}
    for stl_path in sorted(matched_candidate_paths - used_stls):
        rejected.append(
            {
                "source_stl": str(stl_path.resolve()),
                "reason": "matching assembly solid was already assigned to a closer STL",
            }
        )
    return sorted(selected, key=lambda item: str(item.stl_path)), rejected


def safe_identifier(source_name: str, stl_path: Path) -> str:
    source = re.sub(r"[^a-z0-9]+", "-", source_name.lower()).strip("-")
    stem = re.sub(r"[^a-z0-9]+", "-", stl_path.stem.lower()).strip("-")
    digest = file_digest(stl_path)[:10]
    return f"{source or 'source'}-{stem[:42] or 'part'}-{digest}"


def shape_geometry(solid: cq.Shape) -> dict[str, object]:
    face_types = Counter(str(face.geomType()) for face in solid.Faces())
    edge_types = Counter(str(edge.geomType()) for edge in solid.Edges())
    bounds = solid.BoundingBox()
    return {
        "solid_count": 1,
        "valid_brep": bool(solid.isValid()),
        "volume": abs(float(solid.Volume())),
        "dimensions": [float(bounds.xlen), float(bounds.ylen), float(bounds.zlen)],
        "face_count": len(solid.Faces()),
        "edge_count": len(solid.Edges()),
        "brep_face_type_counts": dict(face_types.most_common()),
        "brep_edge_type_counts": dict(edge_types.most_common()),
        "effectively_planar_bspline_count": 0,
        "analytic_curved_face_count": sum(
            count for kind, count in face_types.items() if kind != "PLANE"
        ),
        "analytic_curved_edge_count": sum(
            count for kind, count in edge_types.items() if kind != "LINE"
        ),
    }


def materialize(
    assembly_step: Path,
    stl_root: Path,
    output: Path,
    source_name: str,
    source_url: str,
    source_commit: str,
    source_license: str,
    maximum_relative_error: float,
) -> dict[str, object]:
    assembly = cq.importers.importStep(str(assembly_step)).val()
    solids = list(assembly.Solids())
    if not solids:
        raise ValueError("assembly STEP contains no solids")
    stl_paths = sorted(stl_root.rglob("*.stl"))
    matches, rejected = match_printable_solids(
        solids,
        stl_paths,
        maximum_relative_error,
    )
    output.mkdir(parents=True, exist_ok=True)
    cases: list[dict[str, object]] = []
    assembly_digest = file_digest(assembly_step)
    for match in matches:
        solid = solids[match.solid_index]
        identifier = safe_identifier(source_name, match.stl_path)
        step_path = output / f"{identifier}.step"
        stl_path = output / f"{identifier}.stl"
        cq.exporters.export(cq.Workplane(obj=solid), str(step_path))
        shutil.copy2(match.stl_path, stl_path)
        mesh = trimesh.load(stl_path, force="mesh", process=True)
        geometry = shape_geometry(solid)
        triangles = int(len(mesh.faces))
        complexity = (
            "simple" if triangles <= 5_000 else "medium" if triangles <= 20_000 else "complex"
        )
        cases.append(
            {
                "id": identifier,
                "source_step": str(step_path.resolve()),
                "source_assembly_step": str(assembly_step.resolve()),
                "source_stl": str(match.stl_path.resolve()),
                "source_url": source_url,
                "source_commit": source_commit,
                "source_license": source_license,
                "source_assembly_sha256": assembly_digest,
                "source_stl_sha256": file_digest(match.stl_path),
                "source_solid_index": match.solid_index,
                "match_relative_volume_error": match.volume_error,
                "match_relative_area_error": match.area_error,
                "stl": stl_path.name,
                "triangles": triangles,
                "watertight": bool(mesh.is_watertight),
                "ground_truth": {
                    "id": identifier,
                    "component_name": match.stl_path.stem,
                    "family": "voron_printed_part",
                    "complexity": complexity,
                    **geometry,
                },
            }
        )
    manifest: dict[str, object] = {
        "format_version": 2,
        "dataset": f"{source_name} printable assembly parts",
        "license": source_license,
        "source_url": source_url,
        "source_commit": source_commit,
        "source_assembly_step": str(assembly_step.resolve()),
        "matching": {
            "invariants": ["absolute volume", "surface area"],
            "maximum_relative_error": maximum_relative_error,
            "one_to_one": True,
            "watertight_source_required": True,
        },
        "case_count": len(cases),
        "failure_count": len(rejected),
        "cases": cases,
        "failures": sorted(rejected, key=lambda item: str(item["source_stl"])),
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, indent=2),
        encoding="utf-8",
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Match printable STL exports to individual solids in an assembly STEP "
            "and build a strict reconstruction corpus."
        )
    )
    parser.add_argument("assembly_step", type=Path)
    parser.add_argument("stl_root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-name", required=True)
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--source-license", required=True)
    parser.add_argument("--maximum-relative-error", type=float, default=0.005)
    arguments = parser.parse_args()
    manifest = materialize(
        arguments.assembly_step,
        arguments.stl_root,
        arguments.output,
        arguments.source_name,
        arguments.source_url,
        arguments.source_commit,
        arguments.source_license,
        arguments.maximum_relative_error,
    )
    print(
        f"Wrote {arguments.output / 'manifest.json'} with "
        f"{manifest['case_count']} cases and {manifest['failure_count']} rejections"
    )


if __name__ == "__main__":
    main()
