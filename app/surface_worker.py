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
    parser.add_argument("--prompt", default="")
    arguments = parser.parse_args()
    _reconstruct_surfaces_direct(
        arguments.run_id,
        arguments.stl_path,
        arguments.original_name,
        arguments.input_units,
        arguments.prompt,
    )


if __name__ == "__main__":
    main()
