from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path

import numpy as np
import trimesh
from PIL import Image, ImageDraw, ImageFont

VIEW_AXES = (
    (0, 1, 2, "XY"),
    (0, 2, 1, "XZ"),
    (1, 2, 0, "YZ"),
)


def _mesh(path: Path) -> trimesh.Trimesh | None:
    if not path.is_file():
        return None
    loaded = trimesh.load(path, force="mesh", process=True)
    return loaded if isinstance(loaded, trimesh.Trimesh) else None


def _projected_bounds(meshes: list[trimesh.Trimesh | None], first: int, second: int):
    points = [
        mesh.vertices[:, [first, second]]
        for mesh in meshes
        if mesh is not None and len(mesh.vertices)
    ]
    if not points:
        return np.array([-1.0, -1.0]), np.array([1.0, 1.0])
    combined = np.vstack(points)
    return combined.min(axis=0), combined.max(axis=0)


def render_view(
    mesh: trimesh.Trimesh | None,
    peer: trimesh.Trimesh | None,
    axes: tuple[int, int, int, str],
    size: tuple[int, int] = (220, 150),
    highlighted_faces: set[int] | None = None,
) -> Image.Image:
    width, height = size
    image = Image.new("RGB", size, "#f5f6f8")
    draw = ImageDraw.Draw(image)
    first, second, depth_axis, label = axes
    draw.text((6, 5), label, fill="#343a40")
    if mesh is None:
        draw.text((width // 2 - 25, height // 2), "no output", fill="#a33")
        return image

    low, high = _projected_bounds([mesh, peer], first, second)
    extent = np.maximum(high - low, 1e-9)
    scale = min((width - 20) / extent[0], (height - 24) / extent[1])
    center = (low + high) / 2
    vertices = mesh.vertices
    projected = np.empty((len(vertices), 2))
    projected[:, 0] = (vertices[:, first] - center[0]) * scale + width / 2
    projected[:, 1] = height / 2 - (vertices[:, second] - center[1]) * scale

    depths = mesh.triangles_center[:, depth_axis]
    normals = mesh.face_normals[:, depth_axis]
    for face_index in np.argsort(depths):
        face = mesh.faces[face_index]
        points = [tuple(projected[vertex]) for vertex in face]
        if highlighted_faces is not None and int(face_index) in highlighted_faces:
            fill = (218, 72, 55)
        else:
            shade = int(155 + 80 * abs(float(normals[face_index])))
            fill = (shade - 20, shade - 8, shade)
        draw.polygon(points, fill=fill)
    return image


def case_panel(
    result: dict[str, object],
    source: trimesh.Trimesh | None,
    output: trimesh.Trimesh | None,
    highlighted_faces: set[int] | None = None,
) -> Image.Image:
    panel = Image.new("RGB", (700, 350), "white")
    draw = ImageDraw.Draw(panel)
    font = ImageFont.load_default()
    missing = ",".join(result.get("missing_analytic_surface_types", [])) or "none"
    caption = (
        f"{result['id']}  {result.get('family')}/{result.get('complexity')}  "
        f"{result.get('status')}  P95={result.get('p95_mm')}  "
        f"facets={result.get('faceted_face_count', 0)}  "
        f"recall={result.get('minimum_analytic_area_recall')}  missing={missing}"
    )
    draw.text((8, 6), caption[:112], fill="#111", font=font)
    draw.text((8, 28), "SOURCE", fill="#134b76", font=font)
    draw.text((8, 186), "OUTPUT", fill="#8a341d", font=font)
    for column, axes in enumerate(VIEW_AXES):
        x = 24 + column * 224
        panel.paste(
            render_view(source, output, axes, highlighted_faces=highlighted_faces),
            (x, 25),
        )
        panel.paste(render_view(output, source, axes), (x, 183))
    return panel


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Render source/output sheets for benchmark failures."
    )
    parser.add_argument("benchmark", type=Path)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("artifacts", type=Path)
    parser.add_argument("--output", type=Path, default=Path("benchmarks/review/sheets"))
    parser.add_argument("--rows-per-sheet", type=int, default=6)
    parser.add_argument("--limit", type=int, default=0)
    arguments = parser.parse_args()

    benchmark = json.loads(arguments.benchmark.read_text(encoding="utf-8"))
    manifest = json.loads(arguments.manifest.read_text(encoding="utf-8"))
    cases = {case["id"]: case for case in manifest["cases"]}
    failures = []
    for result in benchmark["results"]:
        if "scope_status" in result:
            failed = result.get("scope_status") == "in_scope" and not result.get(
                "strict_complete"
            )
        else:
            failed = not result.get("accepted")
        if failed:
            failures.append(result)
    failures.sort(
        key=lambda result: (
            int(result.get("faceted_face_count", 0) or 0),
            -float(result.get("minimum_analytic_area_recall", 1.0) or 0.0),
        ),
        reverse=True,
    )
    if arguments.limit > 0:
        failures = failures[: arguments.limit]
    groups: dict[str, list[Image.Image]] = defaultdict(list)
    arguments.output.mkdir(parents=True, exist_ok=True)
    for result in failures:
        identifier = str(result["id"])
        source_path = arguments.manifest.parent / cases[identifier]["stl"]
        output_path = arguments.artifacts / identifier / "reconstruction.stl"
        graph_path = arguments.artifacts / identifier / "surface_graph.json"
        highlighted_faces: set[int] = set()
        if graph_path.is_file():
            graph = json.loads(graph_path.read_text(encoding="utf-8"))
            visualization = graph.get("visualization", {})
            highlighted_faces.update(
                int(index)
                for index in visualization.get("faceted_source_face_indices", [])
            )
            if result.get("source_mesh_fallback") or visualization.get(
                "global_faceted_fallback"
            ):
                source_mesh = _mesh(source_path)
                if source_mesh is not None:
                    highlighted_faces.update(range(len(source_mesh.faces)))
            else:
                source_mesh = _mesh(source_path)
        else:
            source_mesh = _mesh(source_path)
        panel = case_panel(
            result,
            source_mesh,
            _mesh(output_path),
            highlighted_faces or None,
        )
        panel.save(arguments.output / f"{identifier}.png")
        groups[str(result.get("family", "unknown"))].append(panel)

    for family, panels in groups.items():
        for page in range(math.ceil(len(panels) / arguments.rows_per_sheet)):
            selected = panels[
                page * arguments.rows_per_sheet : (page + 1) * arguments.rows_per_sheet
            ]
            sheet = Image.new("RGB", (700, 350 * len(selected)), "#dfe3e8")
            for row, panel in enumerate(selected):
                sheet.paste(panel, (0, row * 350))
            sheet.save(arguments.output / f"{family}_{page + 1}.png")
    print(f"Rendered {len(failures)} in-scope failures to {arguments.output}")


if __name__ == "__main__":
    main()
