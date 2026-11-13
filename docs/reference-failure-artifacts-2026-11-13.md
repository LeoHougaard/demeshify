# Preserve failures rejected by reference geometry

The multiresolution grader previously decided artifact retention using only
the STL strict result. It applied the additional STEP ground-truth geometry
gate after the temporary output directory had been removed. A reconstruction
that agreed with its STL but failed the reference comparison therefore lost
its STEP and associated diagnostic files.

The shared benchmark now accepts an optional result finalizer, invoked inside
the temporary directory before deciding whether the case failed. Multiresolution
evaluation uses it to apply the same reference metrics and acceptance expression
and attach source/split/preset metadata. Failed reference-only cases now retain
the actual output STEP and the final combined result. Accepted cases still avoid
artifact copies. Converter behavior, reference metric calculations, strict
thresholds and deadlines are unchanged.

The regression uses a real 10 × 8 × 6 mm box STL, native reconstruction/export,
independent STEP reimport and reference grading. A matching reference passes
without retaining artifacts. Moving only the reference 2 mm rejects the case:
before the fix its output was missing; after the fix its retained STEP reopens
as a valid single solid and its saved result includes the reference rejection.
This is a deliberate evaluator counterfactual, not a reconstruction benchmark
or a corpus fixture. No reference geometry is passed into conversion.

The reference metric and summary function syntax trees are unchanged. The
targeted evaluator, legacy benchmark, cache and batch tests pass, as does Ruff:

```powershell
python -m pytest tests/test_reference_failure_artifacts.py tests/test_multires_corpus.py tests/test_surface_benchmark.py tests/test_multires_case_batches.py tests/test_evaluation_identity.py -q
python -m ruff check tools/benchmark_surface_corpus.py tools/benchmark_multires_corpus.py tests/test_reference_failure_artifacts.py tests/test_multires_corpus.py
```

Separate new development evidence on V21: three of four middle-resolution cases
pass strict grading. The fourth, a toroidal-blend design, exceeds the existing
120-second isolated analytic-worker limit and returns a truthful 2,072-face
fallback; it is a strict failure. This twelve-case manifest is only four cases
evaluated, not a complete run. All ten variants remain grouped by source/family,
and holdout remains untouched. The latest composed acquisition snapshot has
469 distinct sources and 4,690 actual inputs, still incomplete.
