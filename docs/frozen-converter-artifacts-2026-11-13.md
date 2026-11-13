# Freeze converter code independently of working source

`tools/freeze_evaluation_converter.py` builds a small compiled application
artifact from an exact committed revision. It reuses an existing task-owned
Python runtime instead of copying its libraries again. Working source changes
do not enter the artifact. The repository license is retained, along with the
existing runtime's package licenses.

The manifest records committed source hashes, compiled file hashes, every base
runtime file, interpreter bytecode version, entrypoint and builder hashes. Its
own SHA-256 must be pinned when verifying. With the verified Python 3.12 Windows
runtime, verification rejects changed files,
extra files, links, junctions, CAD asset extensions including compressed STEP,
and edited manifests. A different bytecode version is refused before creating
the artifact. Existing and failed partial artifacts are preserved.

```powershell
python tools/freeze_evaluation_converter.py --python-runtime benchmarks/multires-1000/evaluation-runtime-prototype/python --revision c84f7ac864a1a251bc944f1333c7f81105db96dc --output benchmarks/multires-1000/evaluation-converters/c84f7ac-reproduction
python tools/freeze_evaluation_converter.py --verify --output benchmarks/multires-1000/evaluation-converters/c84f7ac-reproduction --manifest-sha256 SHA256_PRINTED_BY_THE_BUILD
```

Use a new output directory and the actual digest printed by that build. Run
with a compatible Python interpreter from the project environment. This tool
creates no profiles, ACLs, global configuration, source checkout or geometry
copies. It does not launch the sandbox or freeze the grader. A successful hash
check is not an isolation or reconstruction success claim; the manifest keeps
`isolation_qualified=false`.

Six protocol checks pass: real import of the produced sourceless bytecode,
no second runtime copy, preserved existing output, changed application/runtime,
additional compressed CAD, changed pinned manifest, and incompatible interpreter.
The six checks include parameterized mutation cases. Ruff also passes.

A real artifact was built from `c84f7ac864a1a251bc944f1333c7f81105db96dc`:
22 compiled application files and 11,229 reused runtime files, manifest SHA-256
`abdbaa623da4241bbcb09bf09f5162df2ef8a630c10492771d0ace13bb0fbad9`.
Native workflow proof is complete under
`experiments/frozen-c84-ten-workflow.json`: all ten exports of one existing
development source pass the unchanged strict/reference gates. Every conversion
used the frozen bytecode with the existing task-specific AppContainer prototype:
one CPU, no capabilities or inherited handles, read-locked copied STL, and
three verified AppContainer Python processes per case. The host graded the
retained actual STEP only after those processes exited and the job's access was
revoked. No second conversion or shared benchmark-cache record was produced.
Copied input hashes and the before/after sealed artifact hashes were unchanged.
This is ten resolutions of one design, not ten independent models. The prototype
host is not yet a reviewed production evaluation backend, and the manifest's
isolation qualification flag remains false.

The quantitative hill climb remains unfinished. The current composed snapshot
contains 769 distinct STEP sources and 7,690 actual STL exports, not the final
1,000/10,000. Production sandbox integration, a frozen grader, full acquisition
and the full baseline are still required. Holdout remains untouched.
