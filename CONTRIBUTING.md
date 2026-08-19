# Contributing

STL to STEP Converter is in public beta. Focused fixes with a reproducible case are the
most useful contributions.

## Before opening a change

1. Search existing issues.
2. Reduce geometry bugs to a small synthetic STL when possible. Do not publish
   customer or confidential CAD.
3. Explain which reconstruction mode, units, and result status you observed.

## Local checks

Use Python 3.12, uv, and Node.js 22. Run:

```text
uv sync --locked
uv run ruff check .
uv run pytest
npm --prefix web ci
npm --prefix web audit --audit-level=high
npm --prefix web run build
```

Add a focused regression test for a bug fix. Geometry tests should check the
requested result, such as topology, dimensions, or exported STEP validity, not
only that reconstruction returned.

Keep generated datasets, run artifacts, meshes, logs, and frontend builds out of
Git. The existing ignore rules cover them. The sample STL under `web/public/` is
the one deliberate exception.

## Pull requests

Keep changes small enough to review. Describe the user-visible problem, the fix,
and the checks you ran. Call out changes to acceptance thresholds, time budgets,
schemas, or fallback behavior because they can change corpus results even when
unit tests pass.
