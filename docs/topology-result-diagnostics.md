# Retain native topology diagnostics beside existing benchmark results

After loading the reference and output STEP shapes, `benchmark_case` records
`reference_topology`, the complete `compare_topology(reference, output)` result.
It includes the actual source/output native metrics, supported flags and reasons,
and a three-state `topology_match`: true, false or unavailable (`null`). Its status
is `match`, `mismatch` or `indeterminate`. This uses actual STEP topology rather
than the converter's canonical graph counts.

Unsupported topology retains the helper's metrics and indeterminate reasons.
Missing shapes and diagnostic exceptions are nonfatal, explicitly unavailable
indeterminate results. They never become a zero-genus source, empty matching
signature or invented topology pass. A supported mismatch can expose a missing
through-hole even when solid and shell counts agree.

This is additive evidence. The original acceptance Boolean, geometry/quality
gates and deadlines are unchanged. The diagnostic neither qualifies a rejected
conversion nor repairs boundaries. A topology match alone cannot establish
geometric equivalence, blind-hole fidelity, minimum wall thickness, hole location
or original feature history. Supported coverage and semantic claims must remain
separate from the existing strict acceptance count.

The comparison is retained in failure artifacts before temporary STEP cleanup,
including the two existing closed-shell round-trip diagnostic filenames. New
tests use tiny actual STEP shapes with a lost through-hole and unowned geometry;
the conversion report is a protocol stub and supplies no reconstruction-yield
evidence. No development/validation/holdout conversion corpus was run.

`brep_topology.py` is included in the evaluator's source fingerprint. The identity
algorithm is unchanged; older records remain historical evidence and are not
silently treated as matching the new diagnostic implementation.
