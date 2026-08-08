from __future__ import annotations

import argparse
import faulthandler
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.mesh import load_mesh  # noqa: E402
from app.surface_brep import build_surface_brep  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Profile one fitted-surface case.")
    parser.add_argument("stl", type=Path)
    parser.add_argument("--trace-interval", type=int, default=30)
    arguments = parser.parse_args()

    faulthandler.dump_traceback_later(arguments.trace_interval, repeat=True)
    started = time.perf_counter()
    print("load_start", flush=True)
    data = load_mesh(arguments.stl, arguments.stl.name, "mm")
    print(
        f"load_done elapsed={time.perf_counter() - started:.3f} "
        f"triangles={len(data.mesh.faces)}",
        flush=True,
    )
    result = build_surface_brep(
        data,
        lambda message: print(
            f"{message} elapsed={time.perf_counter() - started:.3f}",
            flush=True,
        ),
    )
    print(
        f"done elapsed={time.perf_counter() - started:.3f} "
        f"closed={result.closed} free_edges={result.free_edge_count}",
        flush=True,
    )


if __name__ == "__main__":
    main()
