from pathlib import Path

import cadquery as cq
import trimesh

from tools.materialize_assembly_corpus import match_printable_solids, materialize


def _export_box(path: Path, dimensions: tuple[float, float, float]) -> None:
    mesh = trimesh.creation.box(extents=dimensions)
    mesh.apply_transform(trimesh.transformations.random_rotation_matrix())
    mesh.export(path)


def test_matches_rotated_printable_stls_to_unique_assembly_solids(tmp_path) -> None:
    solids = [
        cq.Workplane("XY").box(10, 8, 6).val(),
        cq.Workplane("XY").box(7, 5, 3).val(),
    ]
    first_stl = tmp_path / "first.stl"
    second_stl = tmp_path / "second.stl"
    _export_box(first_stl, (10, 8, 6))
    _export_box(second_stl, (7, 5, 3))

    matches, rejected = match_printable_solids(
        solids,
        [first_stl, second_stl],
        maximum_relative_error=0.005,
    )

    assert rejected == []
    assert [(match.stl_path.name, match.solid_index) for match in matches] == [
        ("first.stl", 0),
        ("second.stl", 1),
    ]


def test_materialized_manifest_records_provenance_and_ground_truth(tmp_path) -> None:
    assembly_path = tmp_path / "assembly.step"
    stl_root = tmp_path / "stls"
    output = tmp_path / "corpus"
    stl_root.mkdir()
    cq.exporters.export(
        cq.Workplane("XY").box(10, 8, 6),
        str(assembly_path),
    )
    _export_box(stl_root / "bracket.stl", (10, 8, 6))

    manifest = materialize(
        assembly_path,
        stl_root,
        output,
        source_name="Fixture Source",
        source_url="https://example.test/source",
        source_commit="abc123",
        source_license="GPL-3.0",
        maximum_relative_error=0.005,
    )

    assert manifest["case_count"] == 1
    case = manifest["cases"][0]
    assert case["id"].startswith("fixture-source-bracket-")
    assert case["source_commit"] == "abc123"
    assert case["ground_truth"]["brep_face_type_counts"] == {"PLANE": 6}
    assert Path(case["source_step"]).is_file()
    assert (output / case["stl"]).is_file()
