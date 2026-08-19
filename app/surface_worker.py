from __future__ import annotations

import argparse
from pathlib import Path

from .surface_reconstruction import _reconstruct_surfaces_direct


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--stl-path", type=Path, required=True)
    parser.add_argument("--original-name", required=True)
    parser.add_argument("--input-units", default="mm")
    parser.add_argument("--progress-path", type=Path)
    arguments = parser.parse_args()

    def update(label: str) -> None:
        if arguments.progress_path is None:
            return
        with arguments.progress_path.open("a", encoding="utf-8") as stream:
            stream.write(label.replace("\n", " ") + "\n")

    _reconstruct_surfaces_direct(
        arguments.run_id,
        arguments.stl_path,
        arguments.original_name,
        arguments.input_units,
        update,
    )


if __name__ == "__main__":
    main()
