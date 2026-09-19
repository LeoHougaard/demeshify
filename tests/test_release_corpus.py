import json

import pytest

from tools.select_release_corpus import select_corpus


def test_selection_keeps_latest_components_controls_and_unique_geometry(tmp_path):
    (tmp_path / "reference.step").write_text("reference")
    cases = []
    for identifier, content in [("a_part_0", "old"), ("a_part_2", "latest"),
                                ("b_part_1", "latest"), ("c_part_0", "third")]:
        (tmp_path / f"{identifier}.stl").write_text(content)
        cases.append({"id": identifier, "stl": f"{identifier}.stl",
                      "source_step": "reference.step"})
    fusion = tmp_path / "fusion.json"
    fusion.write_text(json.dumps({"cases": cases}))
    (tmp_path / "control.stl").write_text("control")
    controls = tmp_path / "controls.json"
    controls.write_text(json.dumps({"cases": [
        {"id": "control", "stl": "control.stl", "source_step": "reference.step"},
    ]}))

    first = select_corpus(fusion, [controls], count=3, seed=1)
    second = select_corpus(fusion, [controls], count=3, seed=1)

    assert first == second
    assert "control" in {case["id"] for case in first["cases"]}
    assert "a_part_0" not in {case["id"] for case in first["cases"]}
    assert len({case["input_sha256"] for case in first["cases"]}) == 3
    with pytest.raises(ValueError, match="Only 3 distinct inputs"):
        select_corpus(fusion, [controls], count=4, seed=1)
